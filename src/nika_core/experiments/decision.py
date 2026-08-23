from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean

from nika_core.experiments.contracts import (
    ExperimentSnapshot,
    ExperimentStatus,
    MetricRule,
)


@dataclass(frozen=True, slots=True)
class TerminalDecision:
    status: ExperimentStatus
    selected_candidate_id: str
    previous_champion_id: str


def decide_terminal(snapshot: ExperimentSnapshot) -> TerminalDecision:
    """Evaluate the existing deterministic M8 promotion policy from durable evidence only."""
    _validate_coverage(snapshot)
    champion_id = snapshot.definition.champion.candidate_id
    policy = snapshot.definition.policy
    primary = policy.primary_metric
    champion_score = _mean(snapshot, champion_id, primary)
    direction = 1.0 if policy.primary_higher_is_better else -1.0
    eligible: list[tuple[str, float]] = []
    for challenger in snapshot.definition.challengers:
        candidate_id = challenger.candidate_id
        score = _mean(snapshot, candidate_id, primary)
        improvement = (score - champion_score) * direction
        if improvement < policy.minimum_improvement:
            continue
        if _guardrails_pass(snapshot, champion_id, candidate_id):
            eligible.append((candidate_id, score))

    if eligible:
        selected = max(eligible, key=lambda item: (direction * item[1], item[0]))[0]
        return TerminalDecision(
            status=ExperimentStatus.PROMOTED,
            selected_candidate_id=selected,
            previous_champion_id=champion_id,
        )
    return TerminalDecision(
        status=ExperimentStatus.COMPLETED,
        selected_candidate_id=champion_id,
        previous_champion_id=champion_id,
    )


def _validate_coverage(snapshot: ExperimentSnapshot) -> None:
    if not snapshot.observations:
        raise ValueError("experiment completion requires recorded evidence")
    required_replays = {item.replay_id for item in snapshot.definition.replays}
    candidate_ids = (
        snapshot.definition.champion.candidate_id,
        *(item.candidate_id for item in snapshot.definition.challengers),
    )
    metrics = (
        snapshot.definition.policy.primary_metric,
        *(rule.metric for rule in snapshot.definition.policy.guardrails),
    )
    expected_keys = {
        (candidate_id, replay_id, metric)
        for candidate_id in candidate_ids
        for replay_id in required_replays
        for metric in metrics
    }
    observed_keys = {
        (item.candidate_id, item.replay_id, item.metric)
        for item in snapshot.observations
    }
    if len(observed_keys) != len(snapshot.observations):
        raise ValueError("duplicate candidate/replay/metric observation")
    if observed_keys != expected_keys:
        for candidate_id in candidate_ids:
            for metric in metrics:
                covered = {
                    item.replay_id
                    for item in snapshot.observations
                    if item.candidate_id == candidate_id and item.metric == metric
                }
                if covered != required_replays:
                    raise ValueError(
                        f"incomplete replay coverage for {candidate_id}/{metric}: "
                        f"expected {len(required_replays)}, got {len(covered)}"
                    )
        extra = sorted(observed_keys - expected_keys)
        raise ValueError(f"experiment contains undeclared observation evidence: {extra!r}")


def _guardrails_pass(
    snapshot: ExperimentSnapshot, champion_id: str, candidate_id: str
) -> bool:
    return all(
        _guardrail_pass(snapshot, champion_id, candidate_id, rule)
        for rule in snapshot.definition.policy.guardrails
    )


def _guardrail_pass(
    snapshot: ExperimentSnapshot,
    champion_id: str,
    candidate_id: str,
    rule: MetricRule,
) -> bool:
    champion = _mean(snapshot, champion_id, rule.metric)
    challenger = _mean(snapshot, candidate_id, rule.metric)
    regression = champion - challenger if rule.higher_is_better else challenger - champion
    return regression <= rule.max_regression


def _mean(snapshot: ExperimentSnapshot, candidate_id: str, metric: str) -> float:
    values: defaultdict[str, list[float]] = defaultdict(list)
    for item in snapshot.observations:
        if item.metric == metric:
            values[item.candidate_id].append(float(item.value))
    if candidate_id not in values:
        raise ValueError(f"missing metric {metric} for candidate {candidate_id}")
    return fmean(values[candidate_id])
