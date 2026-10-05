from __future__ import annotations

import os
from pathlib import Path

import pytest

from nika_core.toolsmith.workspace_security import (
    FileEvidence,
    WorkspaceSecurityError,
    collect_tree_evidence,
)


@pytest.mark.skipif(os.name == "nt", reason="literal backslash is a POSIX filename component")
def test_tree_evidence_rejects_literal_backslash_scope_confusion(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    outside_spelling = root / r"src\outside.py"
    outside_spelling.write_text("outside\n", encoding="utf-8")

    assert outside_spelling.parent == root
    with pytest.raises(WorkspaceSecurityError, match="canonical POSIX repository spelling"):
        collect_tree_evidence(root)


def test_file_evidence_rejects_noncanonical_repository_spelling() -> None:
    with pytest.raises(WorkspaceSecurityError, match="canonical POSIX repository spelling"):
        FileEvidence(r"src\outside.py", "a" * 64, 1)
