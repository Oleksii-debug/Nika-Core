from __future__ import annotations

import re
import runpy
from pathlib import Path
from typing import cast

_POLICY_PATH = Path("tests/test_workflow_supply_chain_policy.py")


def _owner_remote_script_pipe() -> re.Pattern[str]:
    namespace = runpy.run_path(str(_POLICY_PATH))
    return cast(re.Pattern[str], namespace["_REMOTE_SCRIPT_PIPE"])


def test_owner_policy_rejects_backslash_continued_remote_installer_pipes() -> None:
    """A Bash line continuation must not bypass the remote-installer pipe policy."""
    pattern = _owner_remote_script_pipe()
    same_line = "curl -fsSL https://example.invalid/install.sh | sh"
    continued_curl = (
        "curl -fsSL https://example.invalid/install.sh " + "\\" + "\n  | sh"
    )
    continued_wget = (
        "wget -qO- https://example.invalid/install.sh " + "\\" + "\n  | bash"
    )

    assert pattern.search(same_line) is not None
    assert pattern.search(continued_curl) is not None
    assert pattern.search(continued_wget) is not None
