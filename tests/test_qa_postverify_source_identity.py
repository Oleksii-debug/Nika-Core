from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize("mutate_source", [False, True])
def test_real_postcheck_detects_changes_made_during_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate_source: bool
) -> None:
    root = tmp_path / "Ніка source with spaces"
    (root / "scripts").mkdir(parents=True)
    proof_script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "qa_assert_checkout_identity.py"
    )
    shutil.copyfile(proof_script, root / "scripts" / proof_script.name)
    source = root / "source.py"
    source.write_text("print('original')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Nika QA",
            "-c", "user.email=nika@example.invalid",
            "commit", "-qm", "initial",
        ],
        cwd=root,
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    monkeypatch.setenv("NIKA_CANDIDATE_SHA", sha)
    monkeypatch.chdir(root)

    def simulated_step(label: str, command: tuple[str, ...]) -> None:
        if label == "Tests" and mutate_source:
            source.write_text("print('changed')\n", encoding="utf-8")
        elif label == "Post-verification source identity":
            result = subprocess.run(
                command, cwd=root, text=True, capture_output=True, check=False
            )
            if result.returncode:
                assert "checkout worktree is not clean" in result.stderr
                raise SystemExit(result.returncode)
            assert "Verified exact clean checkout SHA" in result.stdout

    monkeypatch.setattr(verify, "run_step", simulated_step)
    if mutate_source:
        with pytest.raises(SystemExit):
            verify.main()
    else:
        assert verify.main() == 0
