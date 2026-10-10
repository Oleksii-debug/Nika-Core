from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qa_assert_checkout_identity.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, encoding="utf-8"
    ).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "Ніка Core source"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    tracked = repo / "source.py"
    tracked.write_text("print('safe')\n", encoding="utf-8")
    _git(repo, "add", "source.py", ".gitignore")
    _git(
        repo, "-c", "user.name=Nika Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "initial",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _check(repo: Path, sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo,
        env={**os.environ, "NIKA_CANDIDATE_SHA": sha},
        text=True,
        capture_output=True,
        check=False,
    )


def test_clean_exact_checkout_is_accepted(checkout: tuple[Path, str]) -> None:
    repo, sha = checkout
    checked = _check(repo, sha)
    assert checked.returncode == 0, checked.stderr
    assert f"Verified exact clean checkout SHA: {sha}" in checked.stdout


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked"])
def test_dirty_checkout_cannot_claim_clean_source(
    checkout: tuple[Path, str], change: str
) -> None:
    repo, sha = checkout
    if change == "untracked":
        (repo / "новий модуль.py").write_text("print('untracked')\n", encoding="utf-8")
    else:
        (repo / "source.py").write_text("print('changed')\n", encoding="utf-8")
        if change == "staged":
            _git(repo, "add", "source.py")

    checked = _check(repo, sha)
    assert checked.returncode != 0
    assert "checkout worktree is not clean" in checked.stderr
    assert "source.py" not in checked.stderr
    assert "новий модуль.py" not in checked.stderr


def test_ignored_build_output_does_not_invalidate_source(
    checkout: tuple[Path, str]
) -> None:
    repo, sha = checkout
    # This is a separately ignored local build directory, not a source file.
    (repo / "build").mkdir()
    (repo / "build" / "result").write_text("x", encoding="utf-8")
    assert _check(repo, sha).returncode == 0


@pytest.mark.parametrize("candidate", ["invalid", " {sha}", "{sha} "])
def test_noncanonical_candidate_sha_is_rejected(
    checkout: tuple[Path, str], candidate: str
) -> None:
    repo, sha = checkout
    checked = _check(repo, candidate.replace("{sha}", sha))
    assert checked.returncode != 0
    assert "exact 40-character Git SHA" in checked.stderr


def test_wrong_commit_is_rejected_even_when_worktree_clean(
    checkout: tuple[Path, str]
) -> None:
    repo, _ = checkout
    checked = _check(repo, "0" * 40)
    assert checked.returncode != 0
    assert "checkout SHA mismatch" in checked.stderr


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_tracked_file_index_flags_cannot_hide_changed_source(
    checkout: tuple[Path, str], flag: str
) -> None:
    repo, sha = checkout
    _git(repo, "update-index", flag, "source.py")
    (repo / "source.py").write_text("print('hidden change')\n", encoding="utf-8")

    checked = _check(repo, sha)
    assert checked.returncode != 0
    assert "checkout contains unverified tracked file flags" in checked.stderr
    assert "source.py" not in checked.stderr
