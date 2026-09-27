from __future__ import annotations

from collections.abc import Callable
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
class _BlockingCloseAdapter(_Adapter):
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        close_started: Event,
        release_close: Event,
    ) -> None:
        super().__init__(manifest)
        self._close_started = close_started
        self._release_close = release_close

    def close(self) -> None:
        self._close_started.set()
        assert self._release_close.wait(timeout=5)
        super().close()


class _FailingCloseAdapter(_Adapter):
    def close(self) -> None:
        raise RuntimeError("adapter close failed")


def _runtime_with_blocked_active_plugin() -> tuple[
    PluginRuntime,
    PluginManifest,
    Event,
    Event,
]:
    runtime = PluginRuntime()
    manifest = _manifest("1.0.0")
    close_started = Event()
    release_close = Event()
    factory_calls = 0

    def factory() -> _Adapter:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return _BlockingCloseAdapter(
                manifest,
                close_started=close_started,
                release_close=release_close,
            )
        return _Adapter(manifest)

    runtime.register(manifest, factory)
    runtime.activate(manifest.plugin_id)
    return runtime, manifest, close_started, release_close


def _start_blocked_deactivation(
    runtime: PluginRuntime,
    plugin_id: str,
    close_started: Event,
) -> Thread:
    thread = Thread(target=runtime.deactivate, args=(plugin_id,))
    thread.start()
    assert close_started.wait(timeout=5)
    return thread


def _require_fail_closed(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - regression accepts ordinary fail-closed rejection.
        return
    raise AssertionError(
        "plugin generation mutation succeeded before prior teardown was proven complete"
    )


def test_reactivation_waits_for_prior_generation_close_completion() -> None:
    runtime, manifest, close_started, release_close = _runtime_with_blocked_active_plugin()
    thread = _start_blocked_deactivation(runtime, manifest.plugin_id, close_started)

    try:
        _require_fail_closed(lambda: runtime.activate(manifest.plugin_id))
    finally:
        release_close.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    active = runtime.activate(manifest.plugin_id)
    assert active.manifest == manifest
    runtime.deactivate(manifest.plugin_id)


def test_upgrade_waits_for_prior_generation_close_completion() -> None:
    runtime, manifest, close_started, release_close = _runtime_with_blocked_active_plugin()
    thread = _start_blocked_deactivation(runtime, manifest.plugin_id, close_started)
    replacement = _manifest("2.0.0")

    try:
        _require_fail_closed(
            lambda: runtime.upgrade(
                replacement,
                lambda: _Adapter(replacement),
                expected_version=manifest.version,
            )
        )
    finally:
        release_close.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    runtime.upgrade(
        replacement,
        lambda: _Adapter(replacement),
        expected_version=manifest.version,
    )
    active = runtime.activate(replacement.plugin_id)
    assert active.manifest == replacement
    runtime.deactivate(replacement.plugin_id)


def test_failed_close_keeps_plugin_generation_fail_stopped() -> None:
    runtime = PluginRuntime()
    manifest = _manifest("1.0.0")
    runtime.register(manifest, lambda: _FailingCloseAdapter(manifest))
    runtime.activate(manifest.plugin_id)

    try:
        runtime.deactivate(manifest.plugin_id)
    except RuntimeError as exc:
        assert "adapter close failed" in str(exc)
    else:
        raise AssertionError("failing adapter close unexpectedly succeeded")

    _require_fail_closed(lambda: runtime.activate(manifest.plugin_id))
    replacement = _manifest("2.0.0")
    _require_fail_closed(
        lambda: runtime.upgrade(
            replacement,
            lambda: _Adapter(replacement),
            expected_version=manifest.version,
        )
    )
class _ReentrantCloseAdapter(_Adapter):
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        on_close: Callable[[], object],
    ) -> None:
        super().__init__(manifest)
        self._on_close = on_close

    def close(self) -> None:
        self._on_close()
        super().close()


def test_transient_adapter_close_can_reenter_runtime_without_deadlock() -> None:
    runtime = PluginRuntime()
    manifest = _manifest("1.0.0")
    factory_barrier = Barrier(2)
    results: list[_Adapter] = []
    failures: list[Exception] = []

    def factory() -> _Adapter:
        factory_barrier.wait(timeout=5)
        return _ReentrantCloseAdapter(
            manifest,
            on_close=runtime.manifests,
        )

    runtime.register(manifest, factory)

    def activate() -> None:
        try:
            results.append(runtime.activate(manifest.plugin_id))
        except Exception as exc:  # noqa: BLE001 - regression records thread outcome.
            failures.append(exc)

    threads = [
        Thread(target=activate, daemon=True),
        Thread(target=activate, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == 2
    assert results[0] is results[1]
    runtime.deactivate(manifest.plugin_id)
def test_inflight_activation_cannot_publish_during_other_generation_teardown() -> None:
    runtime = PluginRuntime()
    manifest = _manifest("1.0.0")
    slow_factory_started = Event()
    release_slow_factory = Event()
    close_started = Event()
    release_close = Event()
    factory_calls = 0
    first_failures: list[Exception] = []
    second_results: list[_Adapter] = []

    def factory() -> _Adapter:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            slow_factory_started.set()
            assert release_slow_factory.wait(timeout=5)
            return _Adapter(manifest)
        if factory_calls == 2:
            return _BlockingCloseAdapter(
                manifest,
                close_started=close_started,
                release_close=release_close,
            )
        return _Adapter(manifest)

    runtime.register(manifest, factory)

    def first_activation() -> None:
        try:
            runtime.activate(manifest.plugin_id)
        except Exception as exc:  # noqa: BLE001 - regression records race outcome.
            first_failures.append(exc)

    first = Thread(target=first_activation, daemon=True)
    first.start()
    assert slow_factory_started.wait(timeout=5)

    second = Thread(
        target=lambda: second_results.append(runtime.activate(manifest.plugin_id)),
        daemon=True,
    )
    second.start()
    second.join(timeout=10)
    assert not second.is_alive()
    assert len(second_results) == 1

    deactivation = _start_blocked_deactivation(
        runtime,
        manifest.plugin_id,
        close_started,
    )
    release_slow_factory.set()
    first.join(timeout=10)

    assert not first.is_alive()
    assert len(first_failures) == 1
    assert isinstance(first_failures[0], RuntimeError)
    assert "deactivation is still in progress" in str(first_failures[0])

    release_close.set()
    deactivation.join(timeout=10)
    assert not deactivation.is_alive()

    active = runtime.activate(manifest.plugin_id)
    assert active.manifest == manifest
    runtime.deactivate(manifest.plugin_id)
