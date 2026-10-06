from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectRepository,
    ResearchEvidencePackage,
)

_APPROVAL_DECISION_ID = "decision-one-shot-58-approve"
_REJECTION_DECISION_ID = "decision-one-shot-58-reject"
_PACKAGE_ID = "research-one-shot-58-owner-decisions"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed bounded ProductDecision fixtures for ONE-SHOT-58."
    )
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = SQLiteStore(args.database_path)
    store.initialize()
    repository = ProductProjectRepository(store)
    project = repository.get(args.project_id)
    service = ProductProjectCommandService(repository)

    repository.record_research_handoff(
        args.project_id,
        ResearchEvidencePackage(
            _PACKAGE_ID,
            (
                EvidenceRef(
                    "evidence-one-shot-58-approve",
                    "research://one-shot-58/owner-decision/approve",
                    "Bounded evidence for the packaged approval proof.",
                ),
                EvidenceRef(
                    "evidence-one-shot-58-reject",
                    "research://one-shot-58/owner-decision/reject",
                    "Bounded evidence for the packaged rejection proof.",
                ),
            ),
        ),
        (
            ProductOption(
                "option-one-shot-58-approve",
                "Approval proof option",
                "Safe option used only by the packaged pre-human proof.",
                (_PACKAGE_ID,),
            ),
            ProductOption(
                "option-one-shot-58-reject",
                "Rejection proof option",
                "Safe option used only by the packaged pre-human proof.",
                (_PACKAGE_ID,),
            ),
        ),
    )

    for decision_id, option_id, rationale in (
        (
            _APPROVAL_DECISION_ID,
            "option-one-shot-58-approve",
            "Owner must explicitly confirm the safe approval proof.",
        ),
        (
            _REJECTION_DECISION_ID,
            "option-one-shot-58-reject",
            "Owner must explicitly reject the safe rejection proof.",
        ),
    ):
        service.record_decision(
            args.project_id,
            ProductDecision(
                decision_id=decision_id,
                option_id=option_id,
                state=ProductDecisionState.PROPOSED,
                rationale=rationale,
                decided_by_ref="user://owner",
            ),
            expected_row_version=project.row_version,
            idempotency_key=f"one-shot-58:{decision_id}:proposed",
        )
        project = repository.get(args.project_id)

    print(
        json.dumps(
            {
                "project_id": args.project_id,
                "row_version": project.row_version,
                "seeded": 2,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
