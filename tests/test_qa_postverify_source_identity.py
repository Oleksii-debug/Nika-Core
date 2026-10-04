from __future__ import annotations

import sys

import pytest

from scripts import verify


@pytest.mark.parametrize("candidate", [None, "", "0" * 40])
def test_source_identity_rechecked_only_when_candidate_environment_present(
    monkeypatch: pytest.MonkeyPatch, candidate: str | None
) -> None:
    if candidate is None:
        monkeypatch.delenv("NIKA_CANDIDATE_SHA", raising=False)
    else:
        monkeypatch.setenv("NIKA_CANDIDATE_SHA", candidate)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(verify, "run_step", lambda label, command: calls.append((label, command)))
    assert verify.main() == 0
    assert len(calls) == (4 if candidate is None else 5)
    if candidate is not None:
        assert calls[-1] == (
            "Post-verification source identity",
            (sys.executable, "scripts/qa_assert_checkout_identity.py"),
        )


def test_postcheck_failure_propagates_without_success_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NIKA_CANDIDATE_SHA", "0" * 40)
    def fail_postcheck(label: str, command: tuple[str, ...]) -> None:
        if label == "Post-verification source identity":
            raise SystemExit(23)
    monkeypatch.setattr(verify, "run_step", fail_postcheck)
    with pytest.raises(SystemExit, match="23"):
        verify.main()
    assert "All verification steps passed" not in capsys.readouterr().out
