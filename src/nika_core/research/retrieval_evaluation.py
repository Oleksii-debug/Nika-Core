from __future__ import annotations

from dataclasses import dataclass

from nika_core.research.knowledge import KnowledgeCorpus, RetrievalScope


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCase:
    case_id: str
    scope: RetrievalScope
    query: str
    expected_artifact_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id is required")
        if not self.query.strip():
            raise ValueError("query is required")
        if not self.expected_artifact_keys:
            raise ValueError("expected_artifact_keys must not be empty")
        expected = tuple(key.strip() for key in self.expected_artifact_keys)
        if any(not key for key in expected):
            raise ValueError("expected_artifact_keys must not contain empty values")
        if len(set(expected)) != len(expected):
            raise ValueError("expected_artifact_keys must be unique")
        object.__setattr__(self, "expected_artifact_keys", expected)


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCaseResult:
    case_id: str
    retrieved_artifact_keys: tuple[str, ...]
    expected_artifact_keys: tuple[str, ...]
    matched_artifact_keys: tuple[str, ...]

    @property
    def recall(self) -> float:
        return len(self.matched_artifact_keys) / len(self.expected_artifact_keys)

    @property
    def hit_at_1(self) -> bool:
        return bool(
            self.retrieved_artifact_keys
            and self.retrieved_artifact_keys[0] in self.expected_artifact_keys
        )


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    limit: int
    cases: tuple[RetrievalEvaluationCaseResult, ...]

    @property
    def recall_at_limit(self) -> float:
        if not self.cases:
            return 0.0
        return sum(case.recall for case in self.cases) / len(self.cases)

    @property
    def hit_at_1_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for case in self.cases if case.hit_at_1) / len(self.cases)


def evaluate_fts_retrieval(
    corpus: KnowledgeCorpus,
    cases: tuple[RetrievalEvaluationCase, ...],
    *,
    limit: int = 5,
) -> RetrievalEvaluationReport:
    if not cases:
        raise ValueError("at least one retrieval evaluation case is required")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("retrieval evaluation case_id values must be unique")

    results: list[RetrievalEvaluationCaseResult] = []
    for case in cases:
        hits = corpus.search(case.scope, case.query, limit=limit)
        retrieved = tuple(hit.provenance.artifact_key for hit in hits)
        expected = set(case.expected_artifact_keys)
        matched = tuple(key for key in retrieved if key in expected)
        results.append(
            RetrievalEvaluationCaseResult(
                case_id=case.case_id,
                retrieved_artifact_keys=retrieved,
                expected_artifact_keys=case.expected_artifact_keys,
                matched_artifact_keys=matched,
            )
        )
    return RetrievalEvaluationReport(limit=limit, cases=tuple(results))
