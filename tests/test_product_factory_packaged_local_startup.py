from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess

import pytest

import nika_core.product_factory_packaged_local_startup as startup_module
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_packaged_local_settings import (
    PackagedLocalProductFactorySettings,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartupError,
    build_packaged_local_product_factory_program,
    decode_packaged_local_product_factory_startup,
)
from nika_core.v01_model_settings import V01ModelSettings
from scripts import nika_windows


def _git(root: pathlib.Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    result = subprocess.run(
        (executable, *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path) -> pathlib.Path:
    repository = tmp_path / "репозиторій"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Nika Test")
    _git(repository, "config", "user.email", "nika@example.invalid")
    (repository / "app.py").write_text("print('old')\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    _git(repository, "commit", "-m", "base")
    return repository.resolve()


def _startup_json(
    tmp_path: pathlib.Path,
    repository: pathlib.Path,
    *,
    executable: str,
) -> str:
    workspace = tmp_path / "factory jobs"
    workspace.mkdir()
    return json.dumps(
        {
            "schema": "nika.product-factory.local-startup.v1",
            "workspace_parent": str(workspace.resolve()),
            "repositories": {"repo-1": str(repository.resolve())},
            "allowed_executables": [executable],
            "resource_budget": {
                "timeout_seconds": 30,
                "max_output_bytes": 1024 * 1024,
                "max_changed_files": 20,
            },
            "lease_seconds": 300,
            "git_executable": executable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configure_ollama(settings: V01ModelSettings) -> None:
    result = settings.configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"


def _seed_current_ollama_promotion(
    store: SQLiteStore,
    settings: V01ModelSettings,
) -> dict[str, str]:
    selection, pin = settings.current_binding()
    assert pin is None
    selection_id = hashlib.sha256(
        selection.canonical_json().encode("utf-8")
    ).hexdigest()
    digests = {
        "decision": hashlib.sha256(b"decision").hexdigest(),
        "binding": hashlib.sha256(b"binding").hexdigest(),
        "base_artifact": hashlib.sha256(b"base-artifact").hexdigest(),
        "base_descriptor": hashlib.sha256(b"base-descriptor").hexdigest(),
        "challenger_artifact": hashlib.sha256(b"challenger-artifact").hexdigest(),
        "challenger_descriptor": hashlib.sha256(b"challenger-descriptor").hexdigest(),
        "previous_selection": hashlib.sha256(b"previous-selection").hexdigest(),
    }
    with store.connection() as conn:
        row = conn.execute(
            "SELECT revision FROM v01_model_settings WHERE singleton = 1"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO v01_model_promotions("
            "decision_sha256, binding_sha256, base_artifact_sha256, "
            "base_descriptor_digest, challenger_artifact_sha256, "
            "challenger_descriptor_digest, previous_selection_id, "
            "activated_selection_id, activated_revision, rollback_revision, "
            "activation_request_sha256, activation_attestation_sha256"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                digests["decision"],
                digests["binding"],
                digests["base_artifact"],
                digests["base_descriptor"],
                digests["challenger_artifact"],
                digests["challenger_descriptor"],
                digests["previous_selection"],
                selection_id,
                row["revision"],
            ),
        )
    return digests


def test_startup_decoder_binds_only_explicit_local_host_authority(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(tmp_path, repository, executable=str(pathlib.Path(executable).resolve()))

    startup = decode_packaged_local_product_factory_startup(raw)

    assert startup is not None
    assert startup.repositories == {"repo-1": repository}
    assert startup.workspace_parent.name == "factory jobs"
    assert startup.policy.lease_seconds == 300
    assert startup.policy.resource_budget.max_changed_files == 20
    assert startup.git_executable == pathlib.Path(executable).resolve()


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema":"nika.product-factory.local-startup.v1","schema":"duplicate"}',
        '{"schema":"nika.product-factory.local-startup.v1"}',
        " []",
        '{"schema":NaN}',
    ],
)
def test_startup_decoder_rejects_ambiguous_or_incomplete_json(raw: str) -> None:
    with pytest.raises(PackagedLocalProductFactoryStartupError):
        decode_packaged_local_product_factory_startup(raw)


def test_startup_decoder_rejects_relative_repository_authority(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = json.loads(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    raw["repositories"]["repo-1"] = "relative/repository"

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="repository repo-1 path must be absolute",
    ):
        decode_packaged_local_product_factory_startup(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )


def test_program_composition_reuses_persisted_ollama_and_canonical_local_worker(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    startup = decode_packaged_local_product_factory_startup(raw)
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = V01ModelSettings(store)
    _configure_ollama(settings)

    program = build_packaged_local_product_factory_program(
        store,
        settings=settings,
        startup=startup,
    )

    assert program.multi_repository_host.store is store
    assert program.multi_repository_host._program is program.host
    assert program.host.worker.worker is program.worker
    assert program.worker.repositories == {"repo-1": repository}
    assert program.worker.planner is not None
    assert program.worker.planner.provider_id == "ollama"
    assert program.worker.planner.provider_kind.value == "local"
    assert program.worker.planner.model == "qwen3:8b"
    assert program.worker.planner.gateway.providers() == ("ollama",)


def test_program_composition_fails_closed_for_deterministic_route_before_worker(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "deterministic",
            "provider_id": None,
            "model": None,
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="requires the persisted Ollama LOCAL route",
    ):
        build_packaged_local_product_factory_program(
            store,
            settings=settings,
            startup=startup,
        )


def test_program_composition_fails_closed_when_model_selection_missing(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="select a local Ollama model",
    ):
        build_packaged_local_product_factory_program(
            store,
            settings=V01ModelSettings(store),
            startup=startup,
        )


def test_app_config_admits_bounded_startup_authority_without_interpreting_plan_data(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )

    config = AppConfig(
        database_path=(tmp_path / "configured.db").resolve(),
        product_factory_local_startup_json=raw,
    )

    assert config.product_factory_local_startup_json == raw


def test_app_config_rejects_unbounded_startup_authority_before_runtime(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(ValueError, match="exceeds the size limit"):
        AppConfig(
            database_path=(tmp_path / "configured.db").resolve(),
            product_factory_local_startup_json="{" + ("x" * (64 * 1024)) + "}",
        )


def test_windows_bridge_auto_composes_real_local_execution_host_from_config(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "ніка.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    config = AppConfig(
        database_path=database,
        product_factory_local_startup_json=raw,
    )
    cleanup: list[object] = []

    try:
        bridge, products = nika_windows.build_windows_bridge(
            config,
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )

        assert bridge is not None
        assert products is not None
        state = bridge.get_state()
        assert state["ok"] is True
        assert state["state"]["product_factory_execution_plan"] == {
            "status": "missing",
            "loaded": False,
            "project_id": None,
            "message": "JSON-план виконання Product Factory ще не завантажено.",
        }
    finally:
        for callback in reversed(cleanup):
            callback()


def test_windows_bridge_rejects_two_product_factory_host_authorities(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    config = AppConfig(
        database_path=(tmp_path / "ніка.db").resolve(),
        product_factory_local_startup_json=_startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        ),
    )

    with pytest.raises(ValueError, match="conflicts"):
        nika_windows.build_windows_bridge(
            config,
            start_startup_recovery=False,
            product_factory_execution_host=object(),  # type: ignore[arg-type]
        )


def test_program_composition_preserves_promoted_ollama_manifest_pin(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "promoted.db")
    store.initialize()
    settings = V01ModelSettings(store)
    _configure_ollama(settings)
    digests = _seed_current_ollama_promotion(store, settings)
    manifest_sha = hashlib.sha256(b"provider-manifest").hexdigest()
    observed: dict[str, object] = {}

    def resolve_manifest(_self: object, **kwargs: object) -> object:
        observed["resolve"] = kwargs

        class Prepared:
            provider_manifest_sha256 = manifest_sha

        return Prepared()

    original_provider = startup_module.OllamaProvider

    class CapturingOllamaProvider(original_provider):
        def __init__(self, **kwargs: object) -> None:
            observed["expected_manifest_sha256"] = kwargs.get(
                "expected_manifest_sha256"
            )
            super().__init__(**kwargs)

    monkeypatch.setattr(
        startup_module.OllamaPromotionManifestStore,
        "resolve",
        resolve_manifest,
    )
    monkeypatch.setattr(
        startup_module,
        "OllamaProvider",
        CapturingOllamaProvider,
    )

    program = build_packaged_local_product_factory_program(
        store,
        settings=settings,
        startup=startup,
    )

    assert program.worker.planner is not None
    assert observed["expected_manifest_sha256"] == manifest_sha
    resolved = observed["resolve"]
    assert isinstance(resolved, dict)
    assert resolved["decision_sha256"] == digests["decision"]
    assert resolved["binding_sha256"] == digests["binding"]
    assert resolved["role"] == "challenger"
    assert resolved["artifact_sha256"] == digests["challenger_artifact"]
    assert resolved["descriptor_digest"] == digests["challenger_descriptor"]
    assert resolved["route_model_id"] == "qwen3:8b"
    assert resolved["base_url"] == "http://localhost:11434"


def test_program_composition_fails_closed_when_promoted_manifest_is_missing(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "missing-manifest.db")
    store.initialize()
    settings = V01ModelSettings(store)
    _configure_ollama(settings)
    _seed_current_ollama_promotion(store, settings)

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="provider manifest",
    ):
        build_packaged_local_product_factory_program(
            store,
            settings=settings,
            startup=startup,
        )

def _bridge_command(
    bridge: object,
    *,
    request_id: str,
    command: str,
) -> dict[str, object]:
    dispatch = getattr(bridge, "dispatch")
    result = dispatch(
        {
            "request_id": request_id,
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert isinstance(result, dict)
    return result


def _changed_ollama_payload(*, revision: int, model: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": revision,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": model,
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 60.0,
    }


def test_windows_bridge_blocks_new_factory_pass_after_model_revision_changes(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "model-stale.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    cleanup: list[object] = []

    try:
        bridge, _products = nika_windows.build_windows_bridge(
            AppConfig(
                database_path=database,
                product_factory_local_startup_json=raw,
            ),
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )
        created = _bridge_command(
            bridge,
            request_id="create-before-model-change",
            command="Створи застосунок для доступного каталогу",
        )
        assert created["status"] == "completed"

        changed = bridge.dispatch(
            {
                "request_id": "change-model",
                "action_id": "settings.model.configure",
                "payload": _changed_ollama_payload(
                    revision=1,
                    model="qwen3:8b-reconfigured",
                ),
            }
        )
        assert changed["status"] == "completed"

        state = bridge.get_state()
        local_state = state["state"]["product_factory_local_startup"]
        assert local_state["runtime_status"] == "restart_required"

        run = _bridge_command(
            bridge,
            request_id="run-after-model-change",
            command="Run current Product Factory",
        )
        assert run["status"] == "rejected"
        assert "Перезапустіть Nika" in str(run["message"])
        assert run["focus_id"] == "model-route-kind"
    finally:
        for callback in reversed(cleanup):
            callback()


def test_windows_bridge_blocks_new_factory_pass_after_startup_settings_change(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "startup-stale.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    saved = PackagedLocalProductFactorySettings(seed_store).configure(
        {"revision": 0, "config_json": raw}
    )
    assert saved.status == "completed"
    cleanup: list[object] = []

    try:
        bridge, _products = nika_windows.build_windows_bridge(
            AppConfig(database_path=database),
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )
        created = _bridge_command(
            bridge,
            request_id="create-before-startup-change",
            command="Створи застосунок для доступного каталогу",
        )
        assert created["status"] == "completed"

        changed_body = json.loads(raw)
        changed_body["lease_seconds"] = 301
        changed_raw = json.dumps(
            changed_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        changed = bridge.dispatch(
            {
                "request_id": "change-local-startup",
                "action_id": "settings.product_factory_local.configure",
                "payload": {"revision": 1, "config_json": changed_raw},
            }
        )
        assert changed["status"] == "completed"

        state = bridge.get_state()
        local_state = state["state"]["product_factory_local_startup"]
        assert local_state["runtime_status"] == "restart_required"

        run = _bridge_command(
            bridge,
            request_id="run-after-startup-change",
            command="Run current Product Factory",
        )
        assert run["status"] == "rejected"
        assert "Перезапустіть Nika" in str(run["message"])
        assert run["focus_id"] == "product-factory-local-startup-json"
    finally:
        for callback in reversed(cleanup):
            callback()


def test_windows_bridge_does_not_activate_mixed_model_authority_during_startup(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "startup-model-race.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    original_build = nika_windows.build_packaged_local_product_factory_program

    def build_then_change_model(
        store: SQLiteStore,
        *,
        settings: V01ModelSettings,
        startup: object,
    ) -> object:
        program = original_build(
            store,
            settings=settings,
            startup=startup,
        )
        changed = settings.configure(
            _changed_ollama_payload(
                revision=1,
                model="qwen3:8b-raced",
            )
        )
        assert changed.status == "completed"
        return program

    monkeypatch.setattr(
        nika_windows,
        "build_packaged_local_product_factory_program",
        build_then_change_model,
    )
    cleanup: list[object] = []
    try:
        bridge, _products = nika_windows.build_windows_bridge(
            AppConfig(
                database_path=database,
                product_factory_local_startup_json=raw,
            ),
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )
        state = bridge.get_state()
        assert (
            state["state"]["product_factory_local_startup"]["runtime_status"]
            == "invalid"
        )
        assert state["state"]["product_factory_execution_plan"] is None
    finally:
        for callback in reversed(cleanup):
            callback()


def test_windows_bridge_does_not_activate_changed_startup_authority_mid_build(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "startup-settings-race.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    saved = PackagedLocalProductFactorySettings(seed_store).configure(
        {"revision": 0, "config_json": raw}
    )
    assert saved.status == "completed"
    original_build = nika_windows.build_packaged_local_product_factory_program

    def build_then_change_startup(
        store: SQLiteStore,
        *,
        settings: V01ModelSettings,
        startup: object,
    ) -> object:
        program = original_build(
            store,
            settings=settings,
            startup=startup,
        )
        changed_body = json.loads(raw)
        changed_body["lease_seconds"] = 302
        changed_raw = json.dumps(
            changed_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        changed = PackagedLocalProductFactorySettings(store).configure(
            {"revision": 1, "config_json": changed_raw}
        )
        assert changed.status == "completed"
        return program

    monkeypatch.setattr(
        nika_windows,
        "build_packaged_local_product_factory_program",
        build_then_change_startup,
    )
    cleanup: list[object] = []
    try:
        bridge, _products = nika_windows.build_windows_bridge(
            AppConfig(database_path=database),
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )
        state = bridge.get_state()
        assert (
            state["state"]["product_factory_local_startup"]["runtime_status"]
            == "invalid"
        )
        assert state["state"]["product_factory_execution_plan"] is None
    finally:
        for callback in reversed(cleanup):
            callback()

def test_packaged_local_startup_html_exposes_semantic_keyboard_controls() -> None:
    html = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "nika_core"
        / "ui"
        / "web"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert '<label for="product-factory-local-startup-json">' in html
    assert 'id="product-factory-local-startup-json"' in html
    assert 'maxlength="65536"' in html
    assert (
        'aria-describedby="product-factory-local-startup-help '
        'product-factory-local-startup-status"'
        in html
    )
    assert 'id="product-factory-local-startup-save"' in html
    assert 'data-action-id="settings.product_factory_local.configure"' in html
    assert 'id="product-factory-local-startup-reload"' in html
    assert 'data-action-id="settings.product_factory_local.refresh"' in html
    assert html.count(
        'data-error-focus-target="product-factory-local-startup-json"'
    ) >= 2


def test_packaged_local_startup_js_preserves_revision_dirty_and_fail_closed_state() -> None:
    javascript = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "nika_core"
        / "ui"
        / "web"
        / "app.js"
    ).read_text(encoding="utf-8")

    required_fragments = (
        'let productFactoryLocalStartupRevision = 0;',
        'let productFactoryLocalStartupDirty = false;',
        'function validProductFactoryLocalStartupSnapshot(snapshot)',
        'function renderProductFactoryLocalStartup(snapshot)',
        'productFactoryLocalStartupJson.disabled = true;',
        'productFactoryLocalStartupSave.disabled = true;',
        'snapshot.revision !== productFactoryLocalStartupRevision',
        'productFactoryLocalStartupDirty = true;',
        'payload.revision = productFactoryLocalStartupRevision;',
        'payload.config_json = raw || null;',
        '"settings.product_factory_local.configure"',
        '"settings.product_factory_local.refresh"',
        'productFactoryLocalStartupDirty = false;',
        'document.documentElement.dataset.nikaReady = "false";',
    )
    for fragment in required_fragments:
        assert fragment in javascript

    configure_payload = javascript.index(
        'if (actionId === "settings.product_factory_local.configure")'
    )
    revision_payload = javascript.index(
        "payload.revision = productFactoryLocalStartupRevision;",
        configure_payload,
    )
    config_payload = javascript.index(
        "payload.config_json = raw || null;",
        revision_payload,
    )
    dispatch = javascript.index("globalThis.pywebview.api.dispatch", config_payload)
    assert configure_payload < revision_payload < config_payload < dispatch
