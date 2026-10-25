from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .contracts import StatsSnapshot


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    filename: str
    media_type: str
    content: bytes


_COLUMNS = (
    "player_id",
    "display_name",
    "matches",
    "wins",
    "losses",
    "sets_for",
    "sets_against",
    "set_difference",
    "win_rate_percent",
)


def render_text_report(snapshot: StatsSnapshot) -> ReportArtifact:
    lines = [
        "Table Tennis Statistics",
        f"Current matches: {snapshot.current_match_count}",
        f"Players: {len(snapshot.players)}",
        "",
        "\t".join(_COLUMNS),
    ]
    for player in snapshot.players:
        lines.append(
            "\t".join(
                (
                    player.player_id,
                    player.display_name,
                    str(player.matches),
                    str(player.wins),
                    str(player.losses),
                    str(player.sets_for),
                    str(player.sets_against),
                    str(player.set_difference),
                    _percent(player.win_rate_millionths),
                )
            )
        )
    content = ("\n".join(lines) + "\n").encode("utf-8")
    return ReportArtifact(
        filename="table-tennis-statistics.txt",
        media_type="text/plain; charset=utf-8",
        content=content,
    )


def render_csv_report(snapshot: StatsSnapshot) -> ReportArtifact:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(_COLUMNS)
    for player in snapshot.players:
        writer.writerow(
            (
                _safe_spreadsheet_text(player.player_id),
                _safe_spreadsheet_text(player.display_name),
                player.matches,
                player.wins,
                player.losses,
                player.sets_for,
                player.sets_against,
                player.set_difference,
                _percent(player.win_rate_millionths),
            )
        )
    return ReportArtifact(
        filename="table-tennis-statistics.csv",
        media_type="text/csv; charset=utf-8",
        content=stream.getvalue().encode("utf-8"),
    )


def _safe_spreadsheet_text(value: str) -> str:
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _percent(millionths: int) -> str:
    basis_points = millionths // 100
    return f"{basis_points // 100}.{basis_points % 100:02d}"
