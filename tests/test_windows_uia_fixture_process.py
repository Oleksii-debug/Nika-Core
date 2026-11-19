from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import m9_windows_uia_proof as proof


@pytest.mark.parametrize("inspection_fails", [False, True])
@pytest.mark.parametrize("needs_kill", [False, True])
def test_spawned_fixture_pid_remains_scoped_and_cleanup_runs(
    monkeypatch: pytest.MonkeyPatch, inspection_fails: bool, needs_kill: bool,
) -> None:
    calls = []

    class Process:
        pid = 8417
        alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            calls.append("terminate")
            self.alive = needs_kill

        def join(self, timeout):
            assert timeout == 5
            calls.append("join")

        def kill(self):
            calls.append("kill")
            self.alive = False

        def close(self):
            assert not self.alive
            calls.append("close")

    def context(method):
        assert method == "spawn"

        def create(*, target):
            assert target is proof.run_fixture
            return Process()

        return SimpleNamespace(Process=create)

    async def inspect(pid):
        assert pid == 8417
        calls.append("inspect")
        if inspection_fails:
            raise AssertionError("fixture proof failed")

    monkeypatch.setattr(proof.multiprocessing, "get_context", context)
    monkeypatch.setattr(proof.time, "sleep", lambda _: None)
    monkeypatch.setattr(proof, "inspect_until_ready", inspect)
    if inspection_fails:
        with pytest.raises(AssertionError, match="fixture proof failed"):
            proof.main()
    else:
        proof.main()
    expected = ["inspect", "terminate", "join"]
    if needs_kill:
        expected += ["kill", "join"]
    assert calls == expected + ["close"]
