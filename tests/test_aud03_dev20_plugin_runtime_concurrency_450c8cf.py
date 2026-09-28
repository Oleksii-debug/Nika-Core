from __future__ import annotations

from threading import Barrier, Event, Thread

from nika_core.plugins import PluginCompatibilityError, PluginManifest, PluginRuntime


class _BarrierPolicy:
    def __init__(self) -> None:
        self.barrier = Barrier(2)
        self.enabled = False

    def validate(self, manifest: PluginManifest) -> None:
        del manifest
        if self.enabled:
            self.barrier.wait(timeout=5)


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _manifest(version: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="aud03.concurrent.plugin",
        name="AUD03 concurrent plugin",
        version=version,
        entrypoint_name="aud03-concurrent-plugin",
    )


def test_plugin_upgrade_cas_has_one_linearized_winner() -> None:
    policy = _BarrierPolicy()
    runtime = PluginRuntime(policy_catalog=policy)  # type: ignore[arg-type]
    initial = _manifest("1.0.0")
    runtime.register(initial, lambda: _Adapter(initial))
    policy.enabled = True

    winners: list[str] = []
    failures: list[Exception] = []

    def upgrade(version: str) -> None:
        candidate = _manifest(version)
        try:
            runtime.upgrade(
                candidate,
                lambda: _Adapter(candidate),
                expected_version=initial.version,
            )
            winners.append(version)
        except Exception as exc:  # noqa: BLE001 - independent oracle records both outcomes.
            failures.append(exc)

    threads = [
        Thread(target=upgrade, args=("2.0.0",)),
        Thread(target=upgrade, args=("3.0.0",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(winners) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert runtime.manifests()[initial.plugin_id].version == winners[0]


def test_activation_cannot_publish_adapter_for_registration_replaced_midflight() -> None:
    runtime = PluginRuntime()
    initial = _manifest("1.0.0")
    replacement = _manifest("2.0.0")
    factory_started = Event()
    release_factory = Event()
    adapters: list[_Adapter] = []
    failures: list[Exception] = []

    def slow_factory() -> _Adapter:
        adapter = _Adapter(initial)
        adapters.append(adapter)
        factory_started.set()
        assert release_factory.wait(timeout=5)
        return adapter

    runtime.register(initial, slow_factory)

    def activate() -> None:
        try:
            runtime.activate(initial.plugin_id)
        except Exception as exc:  # noqa: BLE001 - independent oracle records rejection type.
            failures.append(exc)

    thread = Thread(target=activate)
    thread.start()
    assert factory_started.wait(timeout=5)
    runtime.upgrade(
        replacement,
        lambda: _Adapter(replacement),
        expected_version=initial.version,
    )
    release_factory.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PluginCompatibilityError)
    assert adapters and adapters[0].closed is True
    assert runtime.manifests()[initial.plugin_id].version == replacement.version
