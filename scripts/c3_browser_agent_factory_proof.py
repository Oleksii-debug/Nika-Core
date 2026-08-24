from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Self

from nika_core.data.sqlite import SQLiteStore
from nika_core.interaction import (
    AmbiguousTargetError,
    BrowserSession,
    ControlLocator,
    InteractionAction,
    PlaywrightInteractionAdapter,
    StaleSnapshotError,
    TargetNotFoundError,
    resolve_strict,
    validate_snapshot,
)
from nika_core.kernel.task_queue import TaskQueue
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

PROJECT_ID = "c3-browser-agent-product"
REPOSITORY_LOCATOR = "local/c3-browser-agent"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


@dataclass(slots=True)
class CommerceState:
    orders: dict[str, dict[str, str]] = field(default_factory=dict)
    submit_attempts: dict[str, int] = field(default_factory=dict)
    api_reads: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><main>{body}</main></body></html>"
    ).encode()


def _handler(state: CommerceState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "NikaC3Fixture/1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _send(
            self,
            status: int,
            payload: bytes,
            content_type: str = "text/html; charset=utf-8",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            values = urllib.parse.parse_qs(raw, keep_blank_values=True)
            return {key: items[-1] for key, items in values.items()}

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/":
                self._send(
                    200,
                    _html(
                        "Catalog",
                        """
                        <h1>Sandbox catalog</h1>
                        <form action="/search" method="get">
                          <label for="catalog-search">Search catalog</label>
                          <input id="catalog-search" type="search" name="q">
                          <button type="submit">Search</button>
                        </form>
                        """,
                    ),
                )
                return
            if parsed.path == "/search":
                term = query.get("q", [""])[-1]
                if term.casefold() != "mug":
                    self._send(200, _html("Results", "<h1>Catalog results</h1><p>No items</p>"))
                    return
                self._send(
                    200,
                    _html(
                        "Results",
                        """
                        <h1>Catalog results</h1>
                        <p id="revision-marker">stable</p>
                        <article aria-label="Accessible Mug">
                          <h2>Accessible Mug</h2>
                          <a href="/product/mug">Choose Accessible Mug</a>
                        </article>
                        """,
                    ),
                )
                return
            if parsed.path == "/product/mug":
                self._send(
                    200,
                    _html(
                        "Accessible Mug",
                        """
                        <h1>Accessible Mug</h1>
                        <form action="/cart" method="post">
                          <label for="gift-note">Gift note</label>
                          <input id="gift-note" name="note">
                          <button type="submit">Add Accessible Mug to cart</button>
                        </form>
                        <button disabled>Disabled purchase</button>
                        <button hidden>Hidden purchase</button>
                        <section aria-label="Duplicate A">
                          <button>Duplicate choice</button>
                        </section>
                        <section aria-label="Duplicate B">
                          <button>Duplicate choice</button>
                        </section>
                        """,
                    ),
                )
                return
            if parsed.path == "/checkout":
                key = query.get("key", ["c3-order-001"])[-1]
                self._send(
                    200,
                    _html(
                        "Checkout",
                        f"""
                        <h1>Simulated checkout</h1>
                        <form action="/checkout/submit" method="post">
                          <label for="buyer">Buyer name</label>
                          <input id="buyer" name="buyer">
                          <input type="hidden" name="key" value="{key}">
                          <button type="submit">Simulate checkout</button>
                        </form>
                        """,
                    ),
                )
                return
            if parsed.path == "/api/order":
                key = query.get("key", [""])[-1]
                with state.lock:
                    state.api_reads[key] = state.api_reads.get(key, 0) + 1
                    order = state.orders.get(key)
                    visible = order is not None and state.api_reads[key] > 1
                    payload = (
                        {"status": "completed", "order": order}
                        if visible
                        else {"status": "unknown"}
                    )
                self._send(
                    200,
                    json.dumps(payload, sort_keys=True).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            if parsed.path == "/confirmation":
                key = query.get("key", [""])[-1]
                with state.lock:
                    order = state.orders.get(key)
                if order is None:
                    self._send(404, _html("Missing", "<h1>Order not found</h1>"))
                    return
                self._send(
                    200,
                    _html(
                        "Confirmation",
                        f"<h1>Simulated checkout confirmed</h1>"
                        f"<p>Order {order['order_id']} for {order['buyer']}</p>",
                    ),
                )
                return
            self._send(404, _html("Missing", "<h1>Not found</h1>"))

        def do_POST(self) -> None:
            if self.path == "/cart":
                form = self._form()
                note = urllib.parse.quote(form.get("note", ""), safe="")
                self._send(
                    200,
                    _html(
                        "Cart",
                        f"""
                        <h1>Cart</h1>
                        <p>Accessible Mug</p><p>Gift note: {note}</p>
                        <a href="/checkout?key=c3-order-001">Proceed to simulated checkout</a>
                        """,
                    ),
                )
                return
            if self.path == "/checkout/submit":
                form = self._form()
                key = form.get("key", "")
                buyer = form.get("buyer", "")
                if not key or not buyer:
                    self._send(400, _html("Invalid", "<h1>Invalid simulated checkout</h1>"))
                    return
                with state.lock:
                    state.submit_attempts[key] = state.submit_attempts.get(key, 0) + 1
                    order = state.orders.setdefault(
                        key,
                        {
                            "order_id": "SIM-0001",
                            "buyer": buyer,
                            "item": "Accessible Mug",
                        },
                    )
                    attempt = state.submit_attempts[key]
                if attempt == 1:
                    self._send(
                        503,
                        _html(
                            "Uncertain",
                            f"""
                            <h1>Checkout outcome uncertain</h1>
                            <form action="/checkout/submit" method="post">
                              <input type="hidden" name="buyer" value="{order['buyer']}">
                              <input type="hidden" name="key" value="{key}">
                              <button type="submit">Retry simulated checkout</button>
                            </form>
                            """,
                        ),
                    )
                    return
                self._send(
                    200,
                    _html(
                        "Confirmation",
                        f"<h1>Simulated checkout confirmed</h1>"
                        f"<p>Order {order['order_id']} for {order['buyer']}</p>",
                    ),
                )
                return
            self._send(404, _html("Missing", "<h1>Not found</h1>"))

    return Handler


class FixtureServer:
    def __init__(self) -> None:
        self.state = CommerceState()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _source_sha() -> str:
    value = os.environ.get("NIKA_CANDIDATE_SHA", "").strip().casefold()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError("NIKA_CANDIDATE_SHA must be an exact 40-character Git SHA")
    return value


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef(
                repository_id="c3-repo",
                provider="local",
                locator=REPOSITORY_LOCATOR,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="commerce-fixture",
                repository_id="c3-repo",
                paths=("generated/c3_browser_agent/fixture.py",),
                test_commands=(("python", "-m", "pytest", "tests/c3_fixture"),),
            ),
            ProductComponent(
                component_id="semantic-browser-agent",
                repository_id="c3-repo",
                paths=("generated/c3_browser_agent/agent.py",),
                dependencies=("commerce-fixture",),
                test_commands=(("python", "-m", "pytest", "tests/c3_agent"),),
            ),
            ProductComponent(
                component_id="package",
                repository_id="c3-repo",
                paths=("generated/c3_browser_agent/package.py",),
                dependencies=("semantic-browser-agent",),
                test_commands=(("python", "-m", "pytest", "tests/c3_package"),),
            ),
        ),
    )


def _create_factory_state(root: Path, source_sha: str) -> dict[str, Any]:
    db_path = root / "factory.db"
    store = SQLiteStore(db_path)
    store.initialize()
    projects = ProductProjectRepository(store)
    spec = ProductProjectSpec(
        goal="Build a sandbox semantic browser-agent commerce product",
        desired_outcome=(
            "Search, inspect, choose, fill, cart, simulated checkout, and confirm "
            "without real purchase or third-party account action"
        ),
        requirements=(
            ProductRequirement(
                requirement_id="c3-safe-flow",
                text="All commerce effects stay in the local sandbox fixture",
                acceptance=(
                    "semantic Playwright flow passes",
                    "uncertain simulated submit is idempotent",
                ),
            ),
        ),
        repository_refs=(REPOSITORY_LOCATOR,),
    )
    project = projects.create(
        project_id=PROJECT_ID,
        name="C3 Browser Agent",
        spec=spec,
        idempotency_key="c3:create-project",
    )
    graph = _graph()
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"c3-repo": source_sha},
        component_goals={
            "commerce-fixture": "Provide deterministic local commerce fixture",
            "semantic-browser-agent": "Drive exact accessibility semantics only",
            "package": "Package exact acceptance evidence with release manifest",
        },
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="c3-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    requests = coordinator.snapshot().records
    ownership = {
        record.request.component_id: list(record.request.allowed_paths)
        for record in requests
    }
    if set(ownership) != {"commerce-fixture", "semantic-browser-agent", "package"}:
        raise AssertionError("factory component decomposition mismatch")
    if any(len(paths) != 1 for paths in ownership.values()):
        raise AssertionError(
            "factory work isolation must expose exactly one owned path per component"
        )
    if len({paths[0] for paths in ownership.values()}) != 3:
        raise AssertionError("factory component path ownership overlaps")

    restarted_store = SQLiteStore(db_path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )
    if restored.snapshot() != coordinator.snapshot():
        raise AssertionError("factory restart did not reconstruct the exact coordinator snapshot")

    return {
        "project_id": project.project_id,
        "spec_version": project.spec_version,
        "row_version": project.row_version,
        "components": ownership,
        "task_id": task.task_id,
        "restart_exact": True,
    }


def _exact_node(adapter: PlaywrightInteractionAdapter, role: str, name: str):
    snapshot = adapter.observe()
    node = resolve_strict(snapshot, ControlLocator(role=role, name=name))
    if not node.visible or not node.enabled:
        raise RuntimeError(f"semantic target is not actionable: {role}/{name}")
    return snapshot, node


def _act(
    adapter: PlaywrightInteractionAdapter,
    role: str,
    name: str,
    action: InteractionAction,
    value: str | None = None,
) -> None:
    before, node = _exact_node(adapter, role, name)
    adapter.focus(node)
    adapter.act(node, action, value)
    after = adapter.observe()
    if not adapter.verify(before, after, node, action, value):
        raise AssertionError(f"semantic action was not verified: {role}/{name}/{action.value}")


def _read_order_api(base_url: str, key: str) -> dict[str, Any]:
    url = f"{base_url}/api/order?{urllib.parse.urlencode({'key': key})}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_browser_flow(root: Path) -> dict[str, Any]:
    with FixtureServer() as fixture:
        session = BrowserSession(download_root=root / "downloads", headless=True, timeout_ms=5000)
        try:
            page_id = session.new_page()
            assert session.registry is not None
            # Fixture bootstrapping is test setup, not an agent navigation primitive.
            session.registry.get(page_id).page.goto(fixture.base_url, wait_until="domcontentloaded")
            adapter = PlaywrightInteractionAdapter(session=session, page_id=page_id)

            _act(adapter, "searchbox", "Search catalog", InteractionAction.SET_VALUE, "mug")
            _act(adapter, "button", "Search", InteractionAction.INVOKE)

            results = adapter.observe()
            resolve_strict(results, ControlLocator(role="link", name="Choose Accessible Mug"))

            page = session.registry.get(page_id).page
            stale_before = adapter.observe()
            page.evaluate(
                "() => { document.getElementById('revision-marker').textContent = 'changed'; }"
            )
            stale_after = adapter.observe()
            try:
                validate_snapshot(stale_before, stale_after)
            except StaleSnapshotError:
                stale_dom_blocked = True
            else:
                raise AssertionError("stale DOM was accepted")

            generation_before = adapter.observe()
            _act(adapter, "link", "Choose Accessible Mug", InteractionAction.INVOKE)
            generation_after = adapter.observe()
            if generation_before.generation == generation_after.generation:
                raise AssertionError("navigation did not advance document generation")
            try:
                validate_snapshot(generation_before, generation_after)
            except StaleSnapshotError:
                navigation_stale_blocked = True
            else:
                raise AssertionError("pre-navigation snapshot remained valid")

            product = adapter.observe()
            try:
                resolve_strict(product, ControlLocator(role="button", name="Duplicate choice"))
            except AmbiguousTargetError:
                duplicate_blocked = True
            else:
                raise AssertionError("duplicate semantic labels were silently disambiguated")

            disabled = resolve_strict(
                product,
                ControlLocator(role="button", name="Disabled purchase"),
            )
            if disabled.enabled:
                raise AssertionError("disabled control was reported enabled")
            try:
                _exact_node(adapter, "button", "Disabled purchase")
            except RuntimeError:
                disabled_blocked = True
            else:
                raise AssertionError("disabled control passed actionability guard")

            try:
                resolve_strict(product, ControlLocator(role="button", name="Hidden purchase"))
            except TargetNotFoundError:
                hidden_blocked = True
            else:
                raise AssertionError("hidden control entered the semantic snapshot")

            _act(adapter, "textbox", "Gift note", InteractionAction.SET_VALUE, "Local sandbox")
            _act(adapter, "button", "Add Accessible Mug to cart", InteractionAction.INVOKE)
            _act(adapter, "link", "Proceed to simulated checkout", InteractionAction.INVOKE)
            _act(adapter, "textbox", "Buyer name", InteractionAction.SET_VALUE, "Sandbox User")
            _act(adapter, "button", "Simulate checkout", InteractionAction.INVOKE)

            uncertain = adapter.observe()
            resolve_strict(
                uncertain,
                ControlLocator(role="button", name="Retry simulated checkout"),
            )
            first_reconciliation = _read_order_api(fixture.base_url, "c3-order-001")
            if first_reconciliation.get("status") != "unknown":
                raise AssertionError("fixture did not expose the intended uncertain read model")

            _act(adapter, "button", "Retry simulated checkout", InteractionAction.INVOKE)
            second_reconciliation = _read_order_api(fixture.base_url, "c3-order-001")
            if second_reconciliation.get("status") != "completed":
                raise AssertionError("idempotent simulated checkout did not reconcile")
            confirmation = adapter.observe()
            resolve_strict(
                confirmation,
                ControlLocator(role="heading", name="Simulated checkout confirmed"),
            )

            with fixture.state.lock:
                orders = dict(fixture.state.orders)
                attempts = dict(fixture.state.submit_attempts)
            if len(orders) != 1 or attempts.get("c3-order-001") != 2:
                raise AssertionError("uncertain submit retry created duplicate simulated orders")

            return {
                "safe_flow": [
                    "search catalog",
                    "inspect semantic DOM",
                    "choose item",
                    "fill form",
                    "cart",
                    "simulated checkout",
                    "confirmation",
                ],
                "semantic_only": True,
                "vision_ocr_used": False,
                "coordinates_used": False,
                "stale_dom_blocked": stale_dom_blocked,
                "duplicate_label_blocked": duplicate_blocked,
                "disabled_control_blocked": disabled_blocked,
                "hidden_control_blocked": hidden_blocked,
                "navigation_stale_blocked": navigation_stale_blocked,
                "uncertain_submit_attempts": attempts["c3-order-001"],
                "simulated_order_count": len(orders),
                "reconciliation_path": "local read-only API before bounded idempotent retry",
            }
        finally:
            session.close()


def _package(
    output_root: Path,
    source_sha: str,
    factory: dict[str, Any],
    browser: dict[str, Any],
) -> dict[str, Any]:
    bundle = output_root / "c3-browser-agent-product"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    (bundle / "product-spec.json").write_text(
        json.dumps(factory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (bundle / "acceptance-result.json").write_text(
        json.dumps(browser, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(Path(__file__), bundle / "browser-agent-proof.py")
    manifest = build_release_manifest(
        bundle,
        product="Nika C3 Browser Agent",
        version="1.0.0-c3",
        source_sha=source_sha,
    )
    write_release_manifest(bundle, manifest)
    if verify_release_manifest(bundle, manifest):
        raise AssertionError("canonical release manifest verification failed")

    zip_path = output_root / "c3-browser-agent-product.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_root).as_posix())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return {
        "bundle": str(bundle),
        "zip": str(zip_path),
        "zip_sha256": digest,
        "manifest_files": len(manifest.files),
    }


def run(output_root: Path) -> dict[str, Any]:
    source_sha = _source_sha()
    output_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="nika-c3-factory-") as temp:
        factory = _create_factory_state(Path(temp), source_sha)
        browser = _run_browser_flow(Path(temp))
    package = _package(output_root, source_sha, factory, browser)
    result = {
        "source_sha": source_sha,
        "factory": factory,
        "browser": browser,
        "package": package,
    }
    (output_root / "c3-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts") / "c3-browser-agent",
    )
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
