from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ModelMessage
from nika_core.model_lab import (
    EvaluationCase,
    EvaluationSplit,
    EvaluationSuite,
    ModelCandidate,
)


def test_persisted_candidate_identity_rejects_url_userinfo() -> None:
    with pytest.raises(ValueError, match="URL userinfo"):
        ModelCandidate(
            candidate_id="candidate",
            version="1",
            provider_id="https://user:secret@example.invalid",
            model="model-v1",
            permission_fingerprint="inference-only-v1",
        )


def test_persisted_dataset_identity_rejects_query_and_fragment() -> None:
    case = EvaluationCase(
        case_id="case",
        messages=(ModelMessage(role="user", content="prompt"),),
        expected_text="answer",
    )
    with pytest.raises(ValueError, match="query or fragment"):
        EvaluationSuite(
            dataset_ref="https://example.invalid/eval?token=secret",
            dataset_version="1",
            split=EvaluationSplit.HELD_OUT,
            cases=(case,),
        )
    with pytest.raises(ValueError, match="query or fragment"):
        EvaluationSuite(
            dataset_ref="https://example.invalid/eval#credential-fragment",
            dataset_version="1",
            split=EvaluationSplit.REPLAY,
            cases=(case,),
        )
