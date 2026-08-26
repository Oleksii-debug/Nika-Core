from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentLabTeamView:
    team_id: str
    state: str
    member_count: int
    child_count: int
    nonterminal_child_count: int
    waiting_approval_count: int
    completed_member_count: int
    failed_member_count: int
    cancelled_member_count: int
    max_total_agents: int
    max_parallel: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentLabExperimentView:
    experiment_id: str
    status: str
    observation_count: int
    event_count: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentLabOperationalSnapshot:
    schema_version: int
    team_count: int
    active_team_count: int
    waiting_approval_team_count: int
    experiment_count: int
    running_experiment_count: int
    teams: tuple[AgentLabTeamView, ...]
    experiments: tuple[AgentLabExperimentView, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "team_count": self.team_count,
            "active_team_count": self.active_team_count,
            "waiting_approval_team_count": self.waiting_approval_team_count,
            "experiment_count": self.experiment_count,
            "running_experiment_count": self.running_experiment_count,
            "teams": [asdict(item) for item in self.teams],
            "experiments": [asdict(item) for item in self.experiments],
        }

    def accessible_text(self) -> str:
        lines = [
            (
                "Лабораторія агентів. "
                f"Команд: {self.team_count}; активних: {self.active_team_count}; "
                f"очікують підтвердження: {self.waiting_approval_team_count}. "
                f"Експериментів: {self.experiment_count}; "
                f"запущених: {self.running_experiment_count}."
            )
        ]
        if self.teams:
            lines.append("Останні команди:")
            for item in self.teams:
                lines.append(
                    f"Команда {item.team_id}; стан {item.state}; "
                    f"учасників {item.member_count}; дочірніх виконавців {item.child_count}; "
                    f"активних або очікуючих дочірніх виконавців "
                    f"{item.nonterminal_child_count}; "
                    f"очікують підтвердження {item.waiting_approval_count}."
                )
        else:
            lines.append("Останні команди: немає.")
        if self.experiments:
            lines.append("Останні експерименти:")
            for item in self.experiments:
                lines.append(
                    f"Експеримент {item.experiment_id}; стан {item.status}; "
                    f"спостережень {item.observation_count}; подій {item.event_count}."
                )
        else:
            lines.append("Останні експерименти: немає.")
        return "\n".join(lines)
