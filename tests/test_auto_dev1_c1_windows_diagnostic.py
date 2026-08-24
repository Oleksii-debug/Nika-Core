from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nika_core.product_factory_c1_acceptance as c1


def _tail(value: str | None, *, limit: int = 2000) -> str:
    text = value or ""
    return text[-limit:]


def _command_text(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def test_c1_failure_reports_exact_nonzero_subprocess_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []
    real_run = c1.subprocess.run

    def traced_run(*args: Any, **kwargs: Any) -> Any:
        completed = real_run(*args, **kwargs)
        if completed.returncode != 0:
            command = args[0] if args else kwargs.get("args", "<missing-command>")
            observed.append(
                {
                    "command": _command_text(command),
                    "returncode": completed.returncode,
                    "stdout_tail": _tail(completed.stdout),
                    "stderr_tail": _tail(completed.stderr),
                }
            )
        return completed

    monkeypatch.setattr(c1.subprocess, "run", traced_run)
    runner = c1.C1MediumAppAcceptanceRunner(
        root=tmp_path / "C1 diagnostic з пробілами",
        source_sha="a" * 40,
    )

    try:
        runner.run(build_windows_package=False)
    except c1.C1MediumAppAcceptanceError as exc:
        details = json.dumps(observed[-4:], ensure_ascii=False, indent=2, sort_keys=True)
        pytest.fail(
            "C1 acceptance failed before package execution; "
            f"bounded nonzero subprocess evidence follows.\n{exc}\n{details}",
            pytrace=False,
        )
