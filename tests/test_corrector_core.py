from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.corrector import (
    CorrectionProfile,
    CorrectorConflict,
    CorrectorError,
    CorrectorIntegrityError,
    CorrectorRepository,
    NormalizationForm,
    ProtectedTerm,
    ReplacementRule,
    correct_text,
    revision_result,
)
from nika_core.corrector.contracts import CorrectionOptions


class LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def profile() -> CorrectionProfile:
    return CorrectionProfile(
        profile_id="ua-basic",
        rules=(
            ReplacementRule("space-before-comma", " ,", ",", priority=10),
            ReplacementRule("fix-token", "teh", "the", whole_word=True, priority=20),
        ),
        protected_terms=(ProtectedTerm("brand", "teh LAB", case_sensitive=False),),
    )


def test_unicode_newlines_rules_and_protected_terms_are_deterministic() -> None:
    source = "Cafe\u0301 , teh\r\nteh LAB і TEH lab"
    result = correct_text(source, profile())
    assert result.after_text == "Café, the\nteh LAB і TEH lab"
    assert result.evidence.normalized_changed is True
    assert result.evidence.protected_occurrences == 2
    assert result.evidence.total_replacements == 2
    assert correct_text(result.after_text, profile()).after_text == result.after_text


def test_case_insensitive_whole_word_preserves_embedded_words() -> None:
    current = CorrectionProfile(
        profile_id="word",
        rules=(ReplacementRule("word", "cat", "dog", case_sensitive=False, whole_word=True),),
    )
    result = correct_text("CAT cat scatter category", current)
    assert result.after_text == "dog dog scatter category"
    assert result.evidence.total_replacements == 2


def test_nfkc_is_explicit_and_none_preserves_codepoints() -> None:
    nfkc = CorrectionProfile(
        profile_id="nfkc",
        options=CorrectionOptions(normalization=NormalizationForm.NFKC),
    )
    none = CorrectionProfile(
        profile_id="none",
        options=CorrectionOptions(
            normalization=NormalizationForm.NONE,
            normalize_newlines=False,
        ),
    )
    assert correct_text("Ａ", nfkc).after_text == "A"
    assert correct_text("Ａ", none).after_text == "Ａ"


def test_non_idempotent_profile_fails_closed() -> None:
    current = CorrectionProfile(
        profile_id="chain",
        rules=(
            ReplacementRule("a-to-b", "a", "b", priority=10),
            ReplacementRule("b-to-c", "b", "c", priority=5),
        ),
    )
    with pytest.raises(CorrectorError, match="not idempotent"):
        correct_text("a", current)


def test_profile_literals_follow_same_unicode_and_newline_normalization() -> None:
    current = CorrectionProfile(
        profile_id="canonical-literals",
        rules=(ReplacementRule("accent", "Cafe\u0301\r\n", "Bistro\r\n"),),
        protected_terms=(ProtectedTerm("protected", "Re\u0301serve\r\n"),),
    )
    result = correct_text("Café\n Réserve\n Cafe\u0301\r\n", current)
    assert result.after_text == "Bistro\n Réserve\n Bistro\n"
    assert result.evidence.total_replacements == 2


def test_initial_evidence_and_session_head_timestamp_tamper_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = CorrectorRepository(LocalStore(path))
    repo.initialize()
    repo.create_session("s", "text", profile())
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT evidence_json FROM corrector_revisions WHERE session_id='s' AND revision=0"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["input_digest"] = "0" * 64
        evidence_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        conn.execute(
            "UPDATE corrector_revisions SET evidence_json=? WHERE session_id='s' AND revision=0",
            (evidence_json,),
        )
    with pytest.raises(CorrectorIntegrityError):
        repo.history("s")

    path2 = tmp_path / "state2.sqlite3"
    repo2 = CorrectorRepository(LocalStore(path2))
    repo2.initialize()
    repo2.create_session("s", "text", profile())
    with sqlite3.connect(path2) as conn:
        conn.execute(
            "UPDATE corrector_sessions SET updated_at='2026-08-26T00:00:00+00:00' "
            "WHERE session_id='s'"
        )
    with pytest.raises(CorrectorIntegrityError, match="updated_at"):
        repo2.get_session("s")


def test_profile_and_result_repr_do_not_expose_user_text() -> None:
    secret = "PRIVATE-TEXT-ALPHA"
    current = CorrectionProfile(
        profile_id="privacy",
        rules=(ReplacementRule("private-rule", "ALPHA", "BETA"),),
    )
    result = correct_text(secret, current)
    assert secret not in repr(result)
    assert secret not in result.evidence.canonical_json()
    assert "PRIVATE" not in result.evidence.canonical_json()


def test_profile_canonical_round_trip_and_noncanonical_json_rejected() -> None:
    current = profile()
    encoded = current.canonical_json()
    assert CorrectionProfile.from_canonical_json(encoded) == current
    with pytest.raises(CorrectorIntegrityError, match="not canonical"):
        CorrectionProfile.from_canonical_json(encoded.replace(",", ", ", 1))


def test_durable_create_apply_restart_replay_and_revision_result(tmp_path: Path) -> None:
    db = tmp_path / "дані з пробілами" / "corrector state.sqlite3"
    repo = CorrectorRepository(LocalStore(db))
    repo.initialize()
    created = repo.create_session("session-1", "teh , text", profile())
    assert created.current_revision == 0
    revision = repo.apply("session-1", "op-1", profile(), expected_revision=0)
    assert revision.revision == 1
    assert revision.text == "the, text"

    reopened = CorrectorRepository(LocalStore(db))
    reopened.initialize()
    session = reopened.get_session("session-1")
    assert session.current_revision == 1
    assert session.text == "the, text"
    replay = reopened.apply("session-1", "op-1", profile(), expected_revision=0)
    assert replay == revision
    history = reopened.history("session-1")
    reconstructed = revision_result(history[0], history[1])
    assert reconstructed.before_text == "teh , text"
    assert reconstructed.after_text == "the, text"


def test_create_session_exact_replay_and_conflicting_rebind(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    first = repo.create_session("same", "text", profile())
    replay = repo.create_session("same", "text", profile())
    assert replay == first
    with pytest.raises(CorrectorConflict):
        repo.create_session("same", "different", profile())


def test_operation_conflict_and_optimistic_revision_conflict(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    repo.create_session("s", "teh", profile())
    repo.apply("s", "op", profile(), expected_revision=0)
    other_profile = CorrectionProfile(profile_id="other")
    with pytest.raises(CorrectorConflict, match="conflicting inputs"):
        repo.apply("s", "op", other_profile, expected_revision=0)
    with pytest.raises(CorrectorConflict, match="current is 1"):
        repo.apply("s", "op-2", profile(), expected_revision=0)


def test_concurrent_writers_allow_one_revision_winner(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    CorrectorRepository(LocalStore(path)).initialize()
    CorrectorRepository(LocalStore(path)).create_session("s", "teh", profile())

    def worker(operation_id: str) -> str:
        repo = CorrectorRepository(LocalStore(path))
        try:
            revision = repo.apply("s", operation_id, profile(), expected_revision=0)
        except CorrectorConflict:
            return "conflict"
        return f"revision-{revision.revision}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(worker, ("op-a", "op-b")))
    assert outcomes == ["conflict", "revision-1"]
    assert CorrectorRepository(LocalStore(path)).get_session("s").current_revision == 1


def test_schema_ledger_without_required_table_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = CorrectorRepository(LocalStore(path))
    repo.initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE corrector_revisions")
    with pytest.raises(CorrectorIntegrityError, match="unexpected columns"):
        repo.initialize()


def test_concurrent_exact_operation_replay_converges_to_one_revision(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = CorrectorRepository(LocalStore(path))
    repo.initialize()
    repo.create_session("s", "teh", profile())

    def worker(_: int) -> tuple[int, str]:
        current = CorrectorRepository(LocalStore(path))
        revision = current.apply("s", "same-op", profile(), expected_revision=0)
        return revision.revision, revision.text_digest

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(worker, range(16)))
    assert len(set(outcomes)) == 1
    history = CorrectorRepository(LocalStore(path)).history("s")
    assert len(history) == 2
    assert history[-1].revision == 1


def test_raw_sqlite_revision_type_tamper_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = CorrectorRepository(LocalStore(path))
    repo.initialize()
    repo.create_session("s", "text", profile())
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE corrector_sessions SET current_revision = 0.5 WHERE session_id = 's'")
    with pytest.raises(CorrectorIntegrityError, match="durable integer"):
        repo.get_session("s")


def test_raw_text_digest_and_evidence_tamper_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    repo = CorrectorRepository(LocalStore(path))
    repo.initialize()
    repo.create_session("s", "teh", profile())
    repo.apply("s", "op", profile(), expected_revision=0)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE corrector_revisions SET text = 'tampered' WHERE revision = 1")
    with pytest.raises(CorrectorIntegrityError, match="text digest mismatch"):
        repo.history("s")

    path2 = tmp_path / "state2.sqlite3"
    repo2 = CorrectorRepository(LocalStore(path2))
    repo2.initialize()
    repo2.create_session("s", "teh", profile())
    repo2.apply("s", "op", profile(), expected_revision=0)
    with sqlite3.connect(path2) as conn:
        conn.execute(
            "UPDATE corrector_revisions SET evidence_json = '{}' "
            "WHERE session_id = 's' AND revision = 1"
        )
    with pytest.raises(CorrectorIntegrityError, match="evidence schema"):
        repo2.history("s")


def test_timestamp_rewind_rejected_and_persisted_history_is_monotonic(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    start = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    repo.create_session("s", "teh", profile(), created_at=start)
    with pytest.raises(CorrectorConflict, match="cannot precede"):
        repo.apply(
            "s",
            "op-old",
            profile(),
            expected_revision=0,
            created_at=start - timedelta(seconds=1),
        )
    revision = repo.apply(
        "s",
        "op-ok",
        profile(),
        expected_revision=0,
        created_at=start + timedelta(seconds=1),
    )
    assert revision.created_at.endswith("+00:00")


def test_create_session_replay_after_later_revision_returns_current_state(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    repo.create_session("s", "teh", profile())
    repo.apply("s", "op", profile(), expected_revision=0)
    replay = repo.create_session("s", "teh", profile())
    assert replay.current_revision == 1
    assert replay.text == "the"


def test_naive_created_at_is_rejected(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    with pytest.raises(CorrectorError, match="timezone-aware"):
        repo.create_session("s", "text", profile(), created_at=datetime(2026, 8, 26, 20, 0))


def test_profile_repr_hides_literal_rules_and_protected_text() -> None:
    current = CorrectionProfile(
        profile_id="private-profile",
        rules=(ReplacementRule("rule", "SECRET_NEEDLE", "SECRET_REPLACEMENT"),),
        protected_terms=(ProtectedTerm("term", "SECRET_PROTECTED"),),
    )
    rendered = repr(current)
    assert "SECRET_NEEDLE" not in rendered
    assert "SECRET_REPLACEMENT" not in rendered
    assert "SECRET_PROTECTED" not in rendered


def test_noop_correction_is_still_durable_revision_with_digest_evidence(tmp_path: Path) -> None:
    repo = CorrectorRepository(LocalStore(tmp_path / "state.sqlite3"))
    repo.initialize()
    repo.create_session("s", "already clean", profile())
    revision = repo.apply("s", "op-noop", profile(), expected_revision=0)
    assert revision.revision == 1
    assert revision.text == "already clean"
    assert revision.evidence.input_digest == revision.evidence.output_digest
    assert revision.evidence.total_replacements == 0
    assert len(repo.history("s")) == 2
