from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "evals" / "deterministic_brain" / "v1.json"
RUNNER = REPO_ROOT / "scripts" / "run_deterministic_brain_eval.py"


def test_deterministic_brain_v1_baseline_is_machine_readable_and_green() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--dataset", str(DATASET)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 1
    assert payload["dataset"]["id"] == "nika-deterministic-brain-baseline"
    assert payload["dataset"]["version"] == "1.0.0"
    assert payload["runner"]["version"] == "1.0.0"
    assert payload["metrics"]["cases_total"] == 6
    assert payload["metrics"]["cases_passed"] == 6
    assert payload["metrics"]["cases_failed"] == 0
    assert payload["metrics"]["repeatability_runs"] == 8
    assert payload["metrics"]["repeatability_mismatches"] == 0
    assert payload["metrics"]["policy_denied_handler_calls"] == 0
    assert payload["metrics"]["all_pass"] is True
    assert payload["strategy_promotion"] == {"attempted": False, "performed": False}
    assert {case["category"] for case in payload["cases"]} == {
        "simple_planning",
        "constraint_handling",
        "missing_capability",
        "policy_denial",
        "restart_replay",
        "repeatability",
    }
    assert all(case["status"] == "pass" for case in payload["cases"])
    assert len(payload["evidence_fingerprint"]) == 64
