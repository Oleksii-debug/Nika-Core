from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
)
from nika_core.product_factory_coordinator import ReviewDecision, WorkState
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramHost,
    ProgramWorkDisposition,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    ArtifactEvidence,
    ChangedFile,
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    ResourceBudget,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.contracts import TestEvidence as WorkerTestEvidence

PROJECT_ID = "product-c2-messenger"
PYTHON = sys.executable
PERMISSIONS = frozenset(
    {
        "build_release",
        "read_project",
        "read_source",
        "run_tests",
        "update_project",
        "write_source",
    }
)


def _sha(index: int) -> str:
    return f"{index:040x}"[-40:]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _generated_files(component_id: str) -> dict[str, str]:
    files = {
        "identity-session": {
            "identity/__init__.py": "",
            "identity/session.py": """
                from dataclasses import dataclass


                @dataclass(frozen=True, slots=True)
                class Identity:
                    user_id: str
                    display_name: str


                @dataclass(frozen=True, slots=True)
                class Session:
                    user_id: str
                    session_id: str


                def create_session(identity: Identity) -> Session:
                    return Session(identity.user_id, f"session:{identity.user_id}")
            """,
        },
        "conversation-contact": {
            "conversation/__init__.py": "",
            "conversation/model.py": """
                from dataclasses import dataclass


                @dataclass(frozen=True, slots=True)
                class Contact:
                    owner_id: str
                    contact_id: str
                    display_name: str


                @dataclass(frozen=True, slots=True)
                class Conversation:
                    conversation_id: str
                    member_ids: tuple[str, ...]

                    def __post_init__(self) -> None:
                        if len(set(self.member_ids)) != len(self.member_ids):
                            raise ValueError("conversation members must be unique")
                        if len(self.member_ids) < 2:
                            raise ValueError("conversation requires at least two members")
            """,
        },
        "security-boundaries": {
            "security/__init__.py": "",
            "security/access.py": """
                def require_member(actor_id: str, member_ids: tuple[str, ...]) -> None:
                    if actor_id not in member_ids:
                        raise PermissionError(
                            f"actor {actor_id} is not a member of this conversation"
                        )
            """,
        },
        "local-event-transport": {
            "transport/__init__.py": "",
            "transport/local.py": """
                from collections import deque
                from dataclasses import dataclass


                @dataclass(frozen=True, slots=True)
                class LocalEvent:
                    topic: str
                    payload: str


                class LocalEventTransport:
                    def __init__(self) -> None:
                        self._events: deque[LocalEvent] = deque()

                    def publish(self, topic: str, payload: str) -> None:
                        self._events.append(LocalEvent(topic, payload))

                    def drain(self) -> tuple[LocalEvent, ...]:
                        events = tuple(self._events)
                        self._events.clear()
                        return events
            """,
        },
        "message-persistence": {
            "persistence/__init__.py": "",
            "persistence/store.py": """
                import sqlite3
                from pathlib import Path

                from security.access import require_member


                class MessengerStore:
                    def __init__(self, path: Path) -> None:
                        self._conn = sqlite3.connect(path)
                        self._conn.row_factory = sqlite3.Row
                        self._conn.executescript(
                            """
                            CREATE TABLE IF NOT EXISTS users (
                                user_id TEXT PRIMARY KEY,
                                display_name TEXT NOT NULL
                            );
                            CREATE TABLE IF NOT EXISTS conversations (
                                conversation_id TEXT PRIMARY KEY
                            );
                            CREATE TABLE IF NOT EXISTS conversation_members (
                                conversation_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                PRIMARY KEY(conversation_id, user_id)
                            );
                            CREATE TABLE IF NOT EXISTS messages (
                                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                conversation_id TEXT NOT NULL,
                                sender_id TEXT NOT NULL,
                                body TEXT NOT NULL
                            );
                            """
                        )

                    def create_user(self, user_id: str, display_name: str) -> None:
                        self._conn.execute(
                            "INSERT INTO users(user_id, display_name) VALUES (?, ?)",
                            (user_id, display_name),
                        )
                        self._conn.commit()

                    def create_conversation(
                        self,
                        conversation_id: str,
                        member_ids: tuple[str, ...],
                    ) -> None:
                        self._conn.execute(
                            "INSERT INTO conversations(conversation_id) VALUES (?)",
                            (conversation_id,),
                        )
                        self._conn.executemany(
                            "INSERT INTO conversation_members(conversation_id, user_id) "
                            "VALUES (?, ?)",
                            ((conversation_id, user_id) for user_id in member_ids),
                        )
                        self._conn.commit()

                    def _members(self, conversation_id: str) -> tuple[str, ...]:
                        rows = self._conn.execute(
                            "SELECT user_id FROM conversation_members "
                            "WHERE conversation_id=? ORDER BY user_id",
                            (conversation_id,),
                        ).fetchall()
                        return tuple(str(row["user_id"]) for row in rows)

                    def send_message(
                        self,
                        actor_id: str,
                        conversation_id: str,
                        body: str,
                    ) -> int:
                        require_member(actor_id, self._members(conversation_id))
                        cursor = self._conn.execute(
                            "INSERT INTO messages(conversation_id, sender_id, body) "
                            "VALUES (?, ?, ?)",
                            (conversation_id, actor_id, body),
                        )
                        self._conn.commit()
                        return int(cursor.lastrowid)

                    def read_messages(
                        self,
                        actor_id: str,
                        conversation_id: str,
                    ) -> tuple[tuple[str, str], ...]:
                        require_member(actor_id, self._members(conversation_id))
                        rows = self._conn.execute(
                            "SELECT sender_id, body FROM messages "
                            "WHERE conversation_id=? ORDER BY message_id",
                            (conversation_id,),
                        ).fetchall()
                        return tuple(
                            (str(row["sender_id"]), str(row["body"])) for row in rows
                        )

                    def close(self) -> None:
                        self._conn.close()
            """,
        },
        "notification-state": {
            "notifications/__init__.py": "",
            "notifications/state.py": """
                class NotificationState:
                    def __init__(self) -> None:
                        self._unread: dict[tuple[str, str], int] = {}

                    def notify(self, user_id: str, conversation_id: str) -> None:
                        key = (user_id, conversation_id)
                        self._unread[key] = self._unread.get(key, 0) + 1

                    def unread(self, user_id: str, conversation_id: str) -> int:
                        return self._unread.get((user_id, conversation_id), 0)

                    def mark_read(self, user_id: str, conversation_id: str) -> None:
                        self._unread[(user_id, conversation_id)] = 0
            """,
        },
        "desktop-client": {
            "client/index.html": """
                <!doctype html>
                <html lang="en">
                <head><meta charset="utf-8"><title>C2 Messenger</title></head>
                <body>
                  <main>
                    <h1>C2 Messenger</h1>
                    <nav aria-label="Conversations">
                      <button type="button" aria-label="Open Alice and Bob conversation">
                        Alice and Bob
                      </button>
                    </nav>
                    <section aria-labelledby="conversation-heading">
                      <h2 id="conversation-heading">Conversation</h2>
                      <div id="messages" role="log" aria-live="polite"></div>
                      <form>
                        <label for="message">Message</label>
                        <input id="message" name="message" autocomplete="off">
                        <button type="submit">Send</button>
                      </form>
                    </section>
                  </main>
                </body>
                </html>
            """,
        },
        "packaging": {
            "package/build_release.py": """
                import hashlib
                import os
                import zipfile
                from pathlib import Path

                from conversation.model import Conversation
                from identity.session import Identity, create_session
                from notifications.state import NotificationState
                from persistence.store import MessengerStore
                from transport.local import LocalEventTransport


                package_root = Path(__file__).resolve().parent
                desktop_root = Path(os.environ["C2_DESKTOP_ROOT"])
                state_path = package_root / "messenger-state.sqlite3"
                if state_path.exists():
                    state_path.unlink()

                alice = Identity("alice", "Alice")
                bob = Identity("bob", "Bob")
                eve = Identity("eve", "Eve")
                assert create_session(alice).user_id == "alice"
                conversation = Conversation("conversation-1", (alice.user_id, bob.user_id))

                store = MessengerStore(state_path)
                for identity in (alice, bob, eve):
                    store.create_user(identity.user_id, identity.display_name)
                store.create_conversation(
                    conversation.conversation_id,
                    conversation.member_ids,
                )
                message_id = store.send_message(
                    alice.user_id,
                    conversation.conversation_id,
                    "hello from the product factory",
                )
                assert message_id == 1
                store.close()

                restarted = MessengerStore(state_path)
                recovered = restarted.read_messages(
                    bob.user_id,
                    conversation.conversation_id,
                )
                assert recovered == (("alice", "hello from the product factory"),)
                unauthorized_blocked = False
                try:
                    restarted.read_messages(eve.user_id, conversation.conversation_id)
                except PermissionError:
                    unauthorized_blocked = True
                assert unauthorized_blocked
                restarted.close()

                transport = LocalEventTransport()
                transport.publish("message.sent", str(message_id))
                events = transport.drain()
                assert len(events) == 1
                assert events[0].topic == "message.sent"

                notifications = NotificationState()
                notifications.notify(bob.user_id, conversation.conversation_id)
                assert notifications.unread(bob.user_id, conversation.conversation_id) == 1
                notifications.mark_read(bob.user_id, conversation.conversation_id)
                assert notifications.unread(bob.user_id, conversation.conversation_id) == 0

                client = desktop_root / "client" / "index.html"
                html = client.read_text(encoding="utf-8")
                for marker in (
                    "<main>",
                    "<label for=\"message\">",
                    "role=\"log\"",
                    "aria-live=\"polite\"",
                    "<button",
                ):
                    assert marker in html

                proof = package_root / "PROOF.txt"
                proof.write_text(
                    "message_restart_recovery=passed\n"
                    "unauthorized_cross_user_read=blocked\n"
                    "local_event_transport=passed\n"
                    "notification_state=passed\n"
                    "semantic_desktop_surface=passed\n",
                    encoding="utf-8",
                )
                dist = package_root / "dist"
                dist.mkdir(exist_ok=True)
                archive = dist / "C2Messenger.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                    bundle.write(client, "client/index.html")
                    bundle.write(proof, "PROOF.txt")
                print(hashlib.sha256(archive.read_bytes()).hexdigest())
            """,
        },
    }
    return {
        path: textwrap.dedent(content).lstrip()
        for path, content in files[component_id].items()
    }


def _graph() -> ProductRepositoryGraph:
    component_specs = (
        ("identity-session", "backend", frozenset({"credentials", "security"}), ()),
        ("conversation-contact", "backend", frozenset({"privacy"}), ()),
        ("local-event-transport", "backend", frozenset(), ()),
        (
            "security-boundaries",
            "backend",
            frozenset({"security", "privacy"}),
            ("identity-session", "conversation-contact"),
        ),
        (
            "notification-state",
            "backend",
            frozenset({"privacy"}),
            ("conversation-contact",),
        ),
        (
            "message-persistence",
            "data",
            frozenset({"privacy", "security"}),
            ("identity-session", "conversation-contact", "security-boundaries"),
        ),
        (
            "desktop-client",
            "desktop",
            frozenset({"accessibility"}),
            ("identity-session", "conversation-contact", "notification-state"),
        ),
        (
            "packaging",
            "infra",
            frozenset({"deployment"}),
            (
                "desktop-client",
                "local-event-transport",
                "message-persistence",
                "notification-state",
                "security-boundaries",
            ),
        ),
    )
    repositories = tuple(
        RepositoryRef(
            repository_id=f"repo-{component_id}",
            provider="local",
            locator=f"fixture/c2/{component_id}",
            default_branch="main",
        )
        for component_id, _kind, _risk, _dependencies in component_specs
    )
    components = []
    for component_id, _kind, _risk, dependencies in component_specs:
        root = {
            "identity-session": "identity",
            "conversation-contact": "conversation",
            "local-event-transport": "transport",
            "security-boundaries": "security",
            "notification-state": "notifications",
            "message-persistence": "persistence",
            "desktop-client": "client",
            "packaging": "package",
        }[component_id]
        if component_id == "desktop-client":
            test_command = (
                PYTHON,
                "-c",
                (
                    "from pathlib import Path; "
                    "s=Path('client/index.html').read_text(encoding='utf-8'); "
                    "assert '<main>' in s and 'aria-live=\"polite\"' in s and '<label' in s"
                ),
            )
        elif component_id == "packaging":
            test_command = (PYTHON, "package/build_release.py")
        else:
            main_file = next(
                path for path in _generated_files(component_id) if path.endswith(".py")
            )
            test_command = (PYTHON, "-m", "py_compile", main_file)
        components.append(
            ProductComponent(
                component_id=component_id,
                repository_id=f"repo-{component_id}",
                paths=(root,),
                dependencies=dependencies,
                build_commands=(
                    ((PYTHON, "package/build_release.py"),)
                    if component_id == "packaging"
                    else ()
                ),
                test_commands=(test_command,),
            )
        )
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=repositories,
        components=tuple(components),
    )


class LocalSandboxContexts:
    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = roots

    async def context_for(self, request):
        root = self.roots[request.repository_id]
        return CodingWorkerDispatchContext(
            repository_tree_digest=_tree_digest(root),
            lease=WorkspaceLease(
                lease_id=f"lease:{request.work_id}",
                workspace_root=root,
                isolation_class=IsolationClass.PROCESS_CONTAINED,
                expires_at="2099-01-01T00:00:00Z",
            ),
            process_policy=ProcessPolicy((PYTHON,)),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(120, 4 * 1024 * 1024, 20),
        )


class LocalRepositoryEvidence:
    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = roots

    async def collect(self, request, job, result):
        root = self.roots[request.repository_id]
        changed_payload = "\n".join(
            f"{item.path}:{item.sha256}:{item.size_bytes}" for item in result.changed_files
        ).encode("utf-8")
        return CodingWorkerExecutionEvidence(
            work_id=request.work_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=_tree_sha(root),
            diff_digest=_digest(changed_payload),
        )


class DeterministicMessengerCodingWorker:
    """Local CodingWorker fixture that performs real bounded filesystem/process work."""

    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = roots
        self.failed_persistence_once = False
        self.execution_order: list[tuple[str, int]] = []

    async def execute(self, job):
        component_id = job.task_id.rsplit(":", 1)[-1]
        self.execution_order.append((component_id, job.job_id.count(":")))
        root = job.lease.workspace_root
        if component_id == "message-persistence" and not self.failed_persistence_once:
            self.failed_persistence_once = True
            target = root / "persistence" / "store.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "raise RuntimeError('injected first-attempt worker failure')\n",
                encoding="utf-8",
            )
            return CodingResult(
                job_id=job.job_id,
                changed_files=self._changed_files(root),
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "injected persistence worker failure",
                    retryable=True,
                ),
            )

        for relative, content in _generated_files(component_id).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        test_evidence = []
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in self.roots.values())
        env["C2_DESKTOP_ROOT"] = str(self.roots["repo-desktop-client"])
        failure = None
        for command in job.acceptance_commands:
            if command.argv[0] not in job.process_policy.allowed_executables:
                failure = WorkerFailure(
                    WorkerFailureKind.POLICY_VIOLATION,
                    "acceptance executable is outside process policy",
                    retryable=False,
                )
                break
            completed = subprocess.run(
                command.argv,
                cwd=root / command.cwd,
                env=env,
                capture_output=True,
                check=False,
                text=True,
                timeout=command.timeout_seconds or job.resource_budget.timeout_seconds,
            )
            output = (completed.stdout + completed.stderr).encode("utf-8")
            test_evidence.append(
                WorkerTestEvidence(command.argv, completed.returncode, _digest(output))
            )
            if completed.returncode != 0:
                failure = WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    f"acceptance command failed with {completed.returncode}",
                    retryable=True,
                )
                break

        artifacts = ()
        archive = root / "package" / "dist" / "C2Messenger.zip"
        if component_id == "packaging" and archive.is_file():
            artifacts = (
                ArtifactEvidence(
                    "C2Messenger.zip",
                    _digest(archive.read_bytes()),
                    "application/zip",
                ),
            )
        return CodingResult(
            job_id=job.job_id,
            changed_files=self._changed_files(root),
            test_evidence=tuple(test_evidence),
            artifacts=artifacts,
            failure=failure,
        )

    async def cancel(self, job_id: str) -> None:
        del job_id

    async def inspect(self, job_id: str) -> RecoveryState | None:
        del job_id
        return None

    async def recover(self, job, state):
        del state
        return await self.execute(job)

    @staticmethod
    def _changed_files(root: Path) -> tuple[ChangedFile, ...]:
        changed = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            data = path.read_bytes()
            changed.append(
                ChangedFile(
                    path.relative_to(root).as_posix(),
                    _digest(data),
                    len(data),
                )
            )
        return tuple(changed)


def _run(coroutine):
    return asyncio.run(coroutine)


def _review_ready(host, task_id, binding, coordinator) -> None:
    for record in coordinator.snapshot().records:
        if record.state is not WorkState.REVIEW_REQUIRED:
            continue
        result = record.result
        assert result is not None
        host.review_and_checkpoint(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            component_id=record.request.component_id,
            decision=ReviewDecision(
                reviewer_id="qa-c2-independent",
                accepted=True,
                reason="deterministic worker evidence passed independent C2 review",
                evidence_refs=(f"qa:c2:{record.request.component_id}:{result.diff_digest}",),
            ),
        )


def test_c2_messenger_product_factory_multi_repo_restart_repair_and_package(tmp_path) -> None:
    graph = _graph()
    roots = {
        repository.repository_id: tmp_path / "repos" / repository.repository_id
        for repository in graph.repositories
    }
    for root in roots.values():
        root.mkdir(parents=True)

    team_request = TeamCompositionRequest(
        project_id=PROJECT_ID,
        components=tuple(
            ComponentBrief(component.component_id, kind, risk)
            for component, (_component_id, kind, risk, _deps) in zip(
                graph.components,
                (
                    ("identity-session", "backend", frozenset({"credentials", "security"}), ()),
                    ("conversation-contact", "backend", frozenset({"privacy"}), ()),
                    ("local-event-transport", "backend", frozenset(), ()),
                    (
                        "security-boundaries",
                        "backend",
                        frozenset({"security", "privacy"}),
                        (),
                    ),
                    ("notification-state", "backend", frozenset({"privacy"}), ()),
                    (
                        "message-persistence",
                        "data",
                        frozenset({"privacy", "security"}),
                        (),
                    ),
                    ("desktop-client", "desktop", frozenset({"accessibility"}), ()),
                    ("packaging", "infra", frozenset({"deployment"}), ()),
                ),
            )
        ),
        acceptance_criteria=(
            "persist and recover a message after restart",
            "reject unauthorized cross-user reads",
            "package an accessible desktop surface",
        ),
        permission_ceiling=PERMISSIONS,
        scale=ProjectScale.LARGE,
    )
    team = DynamicTeamComposer().compose(team_request)
    capabilities = {capability for role in team.roles for capability in role.capabilities}
    assert {"accessibility", "qa", "release", "security", "windows"} <= capabilities
    assert any(role.independent_review for role in team.roles)
    assert all(role.permissions <= PERMISSIONS for role in team.roles)

    db_path = tmp_path / "nika-c2.db"
    store = SQLiteStore(db_path)
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id=PROJECT_ID,
        name="C2 Messenger Vertical",
        spec=ProductProjectSpec(
            goal="Build a local accessible messenger-like desktop product",
            desired_outcome="Durable authorized messaging packaged as a desktop surface",
            requirements=(
                ProductRequirement(
                    "req-c2",
                    "Local messenger vertical survives restart and enforces membership",
                    (
                        "send, persist and recover a message",
                        "reject cross-user unauthorized read",
                        "produce desktop package",
                    ),
                ),
            ),
            repository_refs=tuple(repository.locator for repository in graph.repositories),
            team_refs=(f"team-plan:{team.plan_id}",),
        ),
        idempotency_key="create:c2-messenger",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-c2-messenger",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={
            repository.repository_id: _sha(index + 1)
            for index, repository in enumerate(graph.repositories)
        },
        component_goals={
            component.component_id: f"Implement C2 {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    worker = DeterministicMessengerCodingWorker(roots)
    adapter = CodingWorkerComponentAdapter(
        worker,
        LocalSandboxContexts(roots),
        LocalRepositoryEvidence(roots),
    )
    host = ProductFactoryProgramHost(store, adapter)

    first = _run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=4,
        )
    )
    assert {item.component_id for item in first} == {
        "conversation-contact",
        "identity-session",
        "local-event-transport",
    }
    assert all(item.disposition is ProgramWorkDisposition.REVIEW_REQUIRED for item in first)
    _review_ready(host, task.task_id, binding, coordinator)

    restarted_store = SQLiteStore(db_path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    host = ProductFactoryProgramHost(restarted_store, adapter)
    coordinator = host.restore_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )
    binding = restarted_binding
    assert {
        record.request.component_id
        for record in coordinator.snapshot().records
        if record.state is WorkState.ACCEPTED
    } == {"conversation-contact", "identity-session", "local-event-transport"}

    injected_failure_result_sha = None
    repair_base_sha = None
    while not all(record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records):
        outcomes = _run(
            host.dispatch_ready(
                host_task_id=task.task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=4,
            )
        )
        assert outcomes
        for outcome in outcomes:
            if outcome.disposition is not ProgramWorkDisposition.REPAIR_REQUIRED:
                continue
            assert outcome.component_id == "message-persistence"
            record = next(
                item
                for item in coordinator.snapshot().records
                if item.request.component_id == outcome.component_id
            )
            assert record.result is not None
            injected_failure_result_sha = record.result.result_sha
            repair = adapter.prepare_safe_repair(
                coordinator,
                outcome.component_id,
                reason="repair injected persistence process failure",
            )
            repair_base_sha = repair.base_sha
            assert repair.attempt == 2
            ProductFactoryCheckpointHost(restarted_store).save(
                host_task_id=task.task_id,
                checkpoint=binding.checkpoint(coordinator),
            )
        _review_ready(host, task.task_id, binding, coordinator)

    assert worker.failed_persistence_once is True
    assert injected_failure_result_sha is not None
    assert repair_base_sha == injected_failure_result_sha

    final_store = SQLiteStore(db_path)
    final_store.initialize()
    final_project = ProductProjectRepository(final_store).get(PROJECT_ID)
    final_binding = ProductProjectCoordinatorBinding(final_project, graph)
    final_host = ProductFactoryProgramHost(final_store, adapter)
    final_coordinator = final_host.restore_latest(
        host_task_id=task.task_id,
        binding=final_binding,
    )
    assert all(
        record.state is WorkState.ACCEPTED for record in final_coordinator.snapshot().records
    )

    archive = roots["repo-packaging"] / "package" / "dist" / "C2Messenger.zip"
    proof = roots["repo-packaging"] / "package" / "PROOF.txt"
    assert archive.is_file()
    assert proof.read_text(encoding="utf-8").splitlines() == [
        "message_restart_recovery=passed",
        "unauthorized_cross_user_read=blocked",
        "local_event_transport=passed",
        "notification_state=passed",
        "semantic_desktop_surface=passed",
    ]
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["PROOF.txt", "client/index.html"]
        desktop_html = bundle.read("client/index.html").decode("utf-8")
    assert 'aria-live="polite"' in desktop_html
    assert '<label for="message">' in desktop_html
