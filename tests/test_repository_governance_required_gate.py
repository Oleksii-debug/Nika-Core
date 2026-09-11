from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW_ROOT = Path(".github/workflows")
_CORE_WORKFLOW = _WORKFLOW_ROOT / "ci.yml"
_JOB_HEADER = re.compile(r"^  (?P<job_id>[A-Za-z_][A-Za-z0-9_-]*):\s*$", re.MULTILINE)


def _job_block(workflow: str, job_id: str) -> str:
    marker = f"  {job_id}:\n"
    start = workflow.find(marker)
    if start < 0:
        raise AssertionError(f"missing workflow job: {job_id}")

    next_job = _JOB_HEADER.search(workflow, start + len(marker))
    end = next_job.start() if next_job is not None else len(workflow)
    return workflow[start:end]


def test_core_required_gate_is_unique_and_fail_closed() -> None:
    workflow = _CORE_WORKFLOW.read_text(encoding="utf-8")
    gate = _job_block(workflow, "required-core-gate")

    assert gate.count("name: Core required gate") == 1
    assert "if: ${{ always() }}" in gate
    assert "needs: [verify]" in gate
    assert "runs-on: ubuntu-latest" in gate
    assert "VERIFY_RESULT: ${{ needs.verify.result }}" in gate
    assert 'test "$VERIFY_RESULT" = "success"' in gate
    assert "continue-on-error" not in gate

    occurrences = sum(
        path.read_text(encoding="utf-8").count("name: Core required gate")
        for path in sorted(_WORKFLOW_ROOT.glob("*.yml"))
        + sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )
    assert occurrences == 1


def test_core_required_gate_depends_on_both_core_verify_platforms_only() -> None:
    workflow = _CORE_WORKFLOW.read_text(encoding="utf-8")
    verify = _job_block(workflow, "verify")
    gate = _job_block(workflow, "required-core-gate")

    assert "fail-fast: false" in verify
    assert "os: [ubuntu-latest, windows-latest]" in verify
    assert gate.count("needs:") == 1
    assert "needs: [verify]" in gate

    optional_jobs = (
        "embedded-foundry-sdk-proof",
        "m4-live-ollama-proof",
        "m5-packaged-webview2-uia",
        "m9-live-playwright-semantic-proof",
        "m9-live-windows-uia-proof",
    )
    assert all(job_id not in gate for job_id in optional_jobs)
