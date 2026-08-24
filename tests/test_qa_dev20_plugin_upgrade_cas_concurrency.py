from __future__ import annotations

from threading import Barrier, Thread

from nika_core.plugins import PluginManifest, PluginRuntime


class _BarrierCatalog:
    def __init__(self) -> None:
        self._barrier = Barrier(2)
        self.enabled = False

    def validate(self, manifest: PluginManifest) -> None:
        del manifest
        if self.enabled:
            self._barrier.wait(timeout=5)


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
        except Exception as exc:  # noqa: BLE001 - QA records competing result types.
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
