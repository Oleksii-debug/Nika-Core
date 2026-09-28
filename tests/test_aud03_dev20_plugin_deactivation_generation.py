from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread

from nika_core.plugins import PluginManifest, PluginRuntime


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


def _manifest(version: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="aud03.deactivation.plugin",
        name="AUD03 deactivation plugin",
        version=version,
        entrypoint_name="aud03-deactivation-plugin",
    )


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
    except Exception:  # noqa: BLE001 - oracle accepts any ordinary fail-closed rejection.
        return
    raise AssertionError(
        "plugin generation mutation succeeded before the prior adapter close completed"
    )


def test_reactivation_cannot_publish_while_prior_generation_is_still_closing() -> None:
    runtime, manifest, close_started, release_close = _runtime_with_blocked_active_plugin()
    thread = _start_blocked_deactivation(runtime, manifest.plugin_id, close_started)

    try:
        _require_fail_closed(lambda: runtime.activate(manifest.plugin_id))
    finally:
        release_close.set()
        thread.join(timeout=10)
        runtime.deactivate(manifest.plugin_id)

    assert not thread.is_alive()


def test_upgrade_cannot_replace_registration_while_prior_generation_is_still_closing() -> None:
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


def test_upgrade_and_reactivation_resume_after_prior_generation_close_completes() -> None:
    runtime, manifest, close_started, release_close = _runtime_with_blocked_active_plugin()
    thread = _start_blocked_deactivation(runtime, manifest.plugin_id, close_started)

    release_close.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    replacement = _manifest("2.0.0")
    runtime.upgrade(
        replacement,
        lambda: _Adapter(replacement),
        expected_version=manifest.version,
    )
    active = runtime.activate(replacement.plugin_id)

    assert active.manifest == replacement
    runtime.deactivate(replacement.plugin_id)
