from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "evals" / "deterministic_brain" / "v1.json"
RUNNER = REPO_ROOT / "scripts" / "run_deterministic_brain_eval.py"


def _run_dataset(dataset: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--dataset", str(dataset)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _case(payload: dict[str, object], case_id: str) -> dict[str, object]:
    cases = payload["cases"]
    assert isinstance(cases, list)
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    raise AssertionError(f"missing evaluation case: {case_id}")


def test_deterministic_brain_v1_baseline_is_machine_readable_and_green() -> None:
    payload = _run_dataset(DATASET)

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


def test_evidence_fingerprint_binds_exact_acceptance_dataset(tmp_path: Path) -> None:
    baseline = _run_dataset(DATASET)
    mutated_dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    simple_case = next(case for case in mutated_dataset["cases"] if case["id"] == "simple-planning")
    removed = simple_case["expected"]["result"].pop("plan_steps")
    assert removed == ["prepare", "finish"]

    mutated_path = tmp_path / "mutated-baseline.json"
    mutated_path.write_text(
        json.dumps(mutated_dataset, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    mutated = _run_dataset(mutated_path)

    assert mutated["metrics"]["all_pass"] is True
    assert _case(baseline, "simple-planning")["observed_fingerprint"] == _case(
        mutated, "simple-planning"
    )["observed_fingerprint"]
    assert baseline["metrics"]["contract_checks_total"] > mutated["metrics"][
        "contract_checks_total"
    ]
    assert baseline["dataset"]["sha256"] != mutated["dataset"]["sha256"]
    assert baseline["evidence_fingerprint"] != mutated["evidence_fingerprint"]
