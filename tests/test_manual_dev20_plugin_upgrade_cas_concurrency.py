from __future__ import annotations

from threading import Barrier, Event, Thread

from nika_core.plugins import (
    PluginCompatibilityError,
    PluginManifest,
    PluginRuntime,
)


class _BarrierCatalog:
    def __init__(self) -> None:
        self._barrier = Barrier(2)
        self.enabled = False

    def validate(self, manifest: PluginManifest) -> None:
        del manifest
        if self.enabled:
            self._barrier.wait(timeout=5)


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _manifest(version: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="qa.cas.plugin",
        name="QA CAS plugin",
        version=version,
        entrypoint_name="qa-cas-plugin",
    )


def test_concurrent_plugin_upgrade_compare_and_swap_has_exactly_one_winner() -> None:
    catalog = _BarrierCatalog()
    runtime = PluginRuntime(policy_catalog=catalog)  # type: ignore[arg-type]
    runtime.register(_manifest("1.0.0"), lambda: None)  # type: ignore[arg-type]
    catalog.enabled = True

    successes: list[str] = []
    failures: list[Exception] = []

    def upgrade(version: str) -> None:
        try:
            runtime.upgrade(
                _manifest(version),
                lambda: None,  # type: ignore[arg-type]
                expected_version="1.0.0",
            )
            successes.append(version)
        except Exception as exc:  # noqa: BLE001 - test records competing result types.
            failures.append(exc)

    first = Thread(target=upgrade, args=("2.0.0",))
    second = Thread(target=upgrade, args=("3.0.0",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "expected_version" in str(failures[0])


def test_activation_revalidates_registration_after_concurrent_upgrade() -> None:
    runtime = PluginRuntime()
    original = _manifest("1.0.0")
    replacement = _manifest("2.0.0")
    factory_entered = Event()
    release_factory = Event()
    created: list[_Adapter] = []
    failures: list[Exception] = []

    def original_factory() -> _Adapter:
        adapter = _Adapter(original)
        created.append(adapter)
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return adapter

    runtime.register(original, original_factory)

    def activate_original() -> None:
        try:
            runtime.activate(original.plugin_id)
        except Exception as exc:  # noqa: BLE001 - test records the exact race result.
            failures.append(exc)

    activation = Thread(target=activate_original)
    activation.start()
    assert factory_entered.wait(timeout=5)

    runtime.upgrade(
        replacement,
        lambda: _Adapter(replacement),
        expected_version=original.version,
    )
    release_factory.set()
    activation.join(timeout=10)

    assert not activation.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PluginCompatibilityError)
    assert "changed during activation" in str(failures[0])
    assert created and created[0].closed is True
    assert runtime.manifests()[original.plugin_id].version == replacement.version
