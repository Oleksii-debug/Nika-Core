from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.components import OptionalComponentRegistry
from nika_core.media.contracts import (
    AssetKind,
    ComponentState,
    MediaAsset,
    MediaResourceClaim,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    OptionalComponent,
    ProcessingJob,
    ProcessingState,
    ResourceClass,
    SubtitleKind,
    SubtitleTrack,
    TextRevision,
)
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.ffprobe import FFprobeAdapter
from nika_core.media.hashing import sha256_bytes
from nika_core.media.local import import_local_media
from nika_core.media.privacy import redact_mapping, redact_text
from nika_core.media.process import ProcessResult, SafeProcessRunner
from nika_core.media.repository import MediaRepository
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.media.schema import MEDIA_SCHEMA_VERSION, initialize_media_schema
from nika_core.media.subtitles import SubtitlePolicy, normalize_subtitle_file, select_subtitle_track
from nika_core.media.yt_dlp import YtDlpAdapter, YtDlpPolicy
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class FakeRunner:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.argv: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd: Path, timeout_seconds: float, env=None) -> ProcessResult:
        del cwd, timeout_seconds, env
        normalized = tuple(argv)
        self.argv.append(normalized)
        output = self.outputs.pop(0)
        return ProcessResult(normalized, 0, output, b"", 0.01)


class FakeObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(cpu_percent=10.0, memory_percent=20.0, available_memory_bytes=8 << 30)


def build_store(tmp_path: Path) -> tuple[SQLiteStore, MediaRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    return store, MediaRepository(store)


def put_identity(repository: MediaRepository) -> tuple[MediaSource, MediaVersion]:
    source = MediaSource(source_id="source-1", kind=MediaSourceKind.LOCAL_FILE, locator="input.mp4")
    version = MediaVersion(
        version_id="version-1",
        source_id=source.source_id,
        metadata_sha256="a" * 64,
    )
    repository.put_source(source)
    repository.put_version(version)
    return source, version


def test_media_schema_is_ordered_and_idempotent(tmp_path: Path) -> None:
    store, _repository = build_store(tmp_path)
    initialize_media_schema(store)
    with store.connection() as conn:
        row = conn.execute("SELECT MAX(version) AS version FROM media_schema_migrations").fetchone()
    assert row["version"] == MEDIA_SCHEMA_VERSION


def test_repository_survives_restart_and_blocks_interrupted_running_job(tmp_path: Path) -> None:
    store, repository = build_store(tmp_path)
    source, version = put_identity(repository)
    repository.put_job(
        ProcessingJob(
            job_id="job-1",
            source_id=source.source_id,
            version_id=version.version_id,
            stage="metadata",
            state=ProcessingState.RUNNING,
            checkpoint_json={"metadata_saved": True},
        )
    )
    restarted = MediaRepository(SQLiteStore(store.path))
    recovered = restarted.recoverable_jobs()
    assert len(recovered) == 1
    assert recovered[0].state == ProcessingState.BLOCKED
    assert recovered[0].checkpoint_json == {"metadata_saved": True}
    assert restarted.get_job("job-1").state == ProcessingState.BLOCKED


def test_original_asset_is_immutable_once_registered(tmp_path: Path) -> None:
    _store, repository = build_store(tmp_path)
    _source, version = put_identity(repository)
    original = MediaAsset(
        asset_id="asset-1",
        version_id=version.version_id,
        kind=AssetKind.ORIGINAL,
        relative_path="input.mp4",
        sha256="b" * 64,
        size_bytes=10,
        immutable_original=True,
    )
    repository.put_asset(original)
    repository.put_asset(original)
    with pytest.raises(ValueError, match="immutable"):
        repository.put_asset(original.model_copy(update={"size_bytes": 11}))


def test_text_revisions_are_append_only_and_contiguous(tmp_path: Path) -> None:
    _store, repository = build_store(tmp_path)
    first = TextRevision(
        revision_id="r0",
        artifact_id="artifact-1",
        ordinal=0,
        text="original",
        reason="original evidence",
        accepted=True,
    )
    repository.append_revision(first)
    with pytest.raises(ValueError, match="ordinal must be 1"):
        repository.append_revision(first.model_copy(update={"revision_id": "r2", "ordinal": 2}))
    second = TextRevision(
        revision_id="r1",
        artifact_id="artifact-1",
        parent_revision_id="r0",
        ordinal=1,
        text="corrected",
        reason="accepted deterministic correction",
        accepted=True,
    )
    repository.append_revision(second)
    assert [item.text for item in repository.revisions("artifact-1")] == ["original", "corrected"]


def test_local_import_is_bounded_and_rejects_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    media = root / "відео файл.mp4"
    media.write_bytes(b"abc")
    imported = import_local_media(media, allowed_root=root, max_bytes=10)
    assert imported.asset.sha256 == sha256_bytes(b"abc")
    assert imported.asset.immutable_original is True
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(MediaError) as caught:
        import_local_media(outside, allowed_root=root)
    assert caught.value.code == MediaErrorCode.PATH_ESCAPE


def test_local_import_hard_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    media = root / "large.bin"
    media.write_bytes(b"abcdef")
    with pytest.raises(MediaError) as caught:
        import_local_media(media, allowed_root=root, max_bytes=5)
    assert caught.value.code == MediaErrorCode.SOURCE_TOO_LARGE


def test_redaction_removes_credentials_and_signed_query_values() -> None:
    redacted = redact_mapping(
        {
            "token": "secret",
            "url": "https://example.test/v?signature=abc123&x=1",
            "nested": {"password": "pw"},
        }
    )
    assert redacted["token"] == "[REDACTED]"
    assert "abc123" not in redacted["url"]
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert "secret" not in redact_text("Authorization: Bearer secret")


def test_optional_component_registry_never_installs_missing_component(tmp_path: Path) -> None:
    registry = OptionalComponentRegistry()
    missing = registry.discover_executable("ffprobe", tmp_path / "does-not-exist.exe")
    assert missing.state == ComponentState.MISSING
    assert "not download" in missing.message
    registry.set(OptionalComponent(component_id="yt-dlp", state=ComponentState.DISABLED))
    assert registry.get("yt-dlp").state == ComponentState.DISABLED


def test_safe_process_runner_captures_output_and_disables_shell(tmp_path: Path) -> None:
    runner = SafeProcessRunner(max_output_bytes=1024)
    result = runner.run(
        (sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    assert result.stdout.strip() == b"ok"
    assert result.returncode == 0


def test_safe_process_runner_enforces_timeout_and_output_limit(tmp_path: Path) -> None:
    runner = SafeProcessRunner(max_output_bytes=128)
    with pytest.raises(MediaError) as timeout:
        runner.run(
            (sys.executable, "-c", "import time; time.sleep(1)"),
            cwd=tmp_path,
            timeout_seconds=0.05,
        )
    assert timeout.value.code == MediaErrorCode.PROCESS_TIMEOUT
    with pytest.raises(MediaError) as output:
        runner.run(
            (sys.executable, "-c", "print('x' * 10000)"),
            cwd=tmp_path,
            timeout_seconds=5,
        )
    assert output.value.code == MediaErrorCode.OUTPUT_LIMIT


def test_yt_dlp_discovery_uses_module_boundary_and_normalizes_tracks(tmp_path: Path) -> None:
    payload = {
        "id": "abc",
        "title": "Course",
        "duration": 100,
        "webpage_url": "https://example.test/watch?v=abc&token=secret",
        "formats": [{"format_id": "140", "ext": "m4a", "acodec": "aac", "vcodec": "none"}],
        "subtitles": {"uk": [{"ext": "vtt", "url": "https://cdn.test/uk.vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://cdn.test/en.vtt"}]},
    }
    runner = FakeRunner([json.dumps(payload).encode()])
    result = YtDlpAdapter(runner).discover("https://example.test/watch?v=abc", cwd=tmp_path)
    argv = runner.argv[0]
    assert argv[:3] == (sys.executable, "-m", "yt_dlp")
    assert "--exec" not in argv
    assert "--cookies-from-browser" not in argv
    assert result.subtitles[0].kind == SubtitleKind.AUTOMATIC or result.subtitles[0].kind == SubtitleKind.MANUAL
    assert {track.language for track in result.subtitles} == {"uk", "en"}
    assert "secret" not in json.dumps(result.sanitized_metadata)


def test_yt_dlp_rejects_auth_implicit_playlist_and_duration(tmp_path: Path) -> None:
    adapter = YtDlpAdapter(FakeRunner([]))
    with pytest.raises(MediaError) as auth:
        adapter.discover("https://example.test/x", cwd=tmp_path, auth_ref="credential:youtube-main")
    assert auth.value.code == MediaErrorCode.AUTH_REQUIRED
    playlist = FakeRunner([json.dumps({"_type": "playlist", "entries": [{"id": "1"}]}).encode()])
    with pytest.raises(MediaError) as playlist_error:
        YtDlpAdapter(playlist).discover("https://example.test/list", cwd=tmp_path)
    assert playlist_error.value.code == MediaErrorCode.PLAYLIST_LIMIT
    long_media = FakeRunner([json.dumps({"id": "1", "title": "x", "duration": 20}).encode()])
    with pytest.raises(MediaError) as duration_error:
        YtDlpAdapter(long_media).discover(
            "https://example.test/x",
            cwd=tmp_path,
            policy=YtDlpPolicy(max_duration_seconds=10),
        )
    assert duration_error.value.code == MediaErrorCode.DURATION_LIMIT


def test_subtitle_first_policy_prefers_manual_language_then_automatic() -> None:
    tracks = (
        SubtitleTrack(track_id="auto-uk", language="uk", kind=SubtitleKind.AUTOMATIC),
        SubtitleTrack(track_id="manual-en", language="en", kind=SubtitleKind.MANUAL),
        SubtitleTrack(track_id="manual-uk", language="uk-UA", kind=SubtitleKind.MANUAL),
    )
    selected = select_subtitle_track(tracks, policy=SubtitlePolicy(preferred_languages=("uk", "en")))
    assert selected is not None
    assert selected.track_id == "manual-uk"
    forced = select_subtitle_track(tracks, policy=SubtitlePolicy(force_transcription=True))
    assert forced is None


def test_subtitle_normalization_accepts_quality_and_rejects_low_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Event:
        def __init__(self, start: int, end: int, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    fake = types.SimpleNamespace(
        load=lambda _path, encoding: [
            Event(0, 1000, "<i>Привіт</i>"),
            Event(1000, 2000, "світе"),
            Event(2000, 9000, "тест"),
        ]
    )
    monkeypatch.setitem(sys.modules, "pysubs2", fake)
    subtitle = tmp_path / "track.vtt"
    subtitle.write_text("fixture", encoding="utf-8")
    track = SubtitleTrack(track_id="auto", language="uk", kind=SubtitleKind.AUTOMATIC)
    transcript = normalize_subtitle_file(
        subtitle,
        track=track,
        version_id="v1",
        media_duration_seconds=10,
        policy=SubtitlePolicy(automatic_min_coverage_ratio=0.5),
    )
    assert transcript.method.value == "platform_subtitle"
    assert transcript.segments[0].text == "Привіт"
    with pytest.raises(MediaError) as low:
        normalize_subtitle_file(
            subtitle,
            track=track,
            version_id="v1",
            media_duration_seconds=100,
            policy=SubtitlePolicy(automatic_min_coverage_ratio=0.5),
        )
    assert low.value.code == MediaErrorCode.LOW_QUALITY_SUBTITLE


def test_ffprobe_audit_captures_build_license_and_probe_without_upstream_objects(tmp_path: Path) -> None:
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"fake-binary")
    version = b"ffprobe version 9.0.1 Copyright\n"
    buildconf = b"configuration: --enable-gpl --disable-nonfree\n"
    payload = json.dumps(
        {
            "format": {"format_name": "matroska", "duration": "2.5", "bit_rate": "1000"},
            "streams": [{"index": 0, "codec_name": "opus", "codec_type": "audio", "channels": 2}],
        }
    ).encode()
    runner = FakeRunner([version, buildconf, payload])
    adapter = FFprobeAdapter(executable, runner)
    audit = adapter.audit(cwd=tmp_path)
    assert audit.descriptor.version == "9.0.1"
    assert audit.license_classification.startswith("GPL")
    probe = adapter.probe(executable, asset_id="asset", cwd=tmp_path)
    assert probe.duration_seconds == 2.5
    assert probe.streams[0]["codec_name"] == "opus"


def test_heavy_media_resource_claims_share_one_machine_slot(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manager = ResourceManager(store, FakeObserver())
    coordinator = MediaResourceCoordinator(manager)
    first = coordinator.request(
        MediaResourceClaim(
            claim_id="asr-1",
            owner_id="job-1",
            resource_class=ResourceClass.HEAVY_MODEL,
        )
    )
    with pytest.raises(MediaError) as blocked:
        coordinator.request(
            MediaResourceClaim(
                claim_id="ocr-1",
                owner_id="job-2",
                resource_class=ResourceClass.HEAVY_MODEL,
            )
        )
    assert blocked.value.code == MediaErrorCode.RESOURCE_BLOCKED
    assert coordinator.release(first) is True


_MEDIA_CREDENTIAL_QUERY_ALIASES = (
    "api-key",
    "client_secret",
    "client-secret",
    "subscription-key",
    "subscription_key",
    "x-api-key",
)


@pytest.mark.parametrize("query_key", _MEDIA_CREDENTIAL_QUERY_ALIASES)
def test_yt_dlp_blocks_credential_query_aliases_before_process(
    tmp_path: Path,
    query_key: str,
) -> None:
    runner = FakeRunner([])
    source_url = f"https://media.example/watch?{query_key}=owner-secret-canary"

    with pytest.raises(MediaError) as caught:
        YtDlpAdapter(runner).discover(source_url, cwd=tmp_path)

    assert caught.value.code == MediaErrorCode.AUTH_REQUIRED
    assert "owner-secret-canary" not in str(caught.value)
    assert runner.argv == []


@pytest.mark.parametrize("query_key", _MEDIA_CREDENTIAL_QUERY_ALIASES)
def test_yt_dlp_minimizes_credential_query_aliases_from_upstream_metadata(
    tmp_path: Path,
    query_key: str,
) -> None:
    secret_url = f"https://media.example/watch?{query_key}=owner-secret-canary"
    payload = {
        "id": "credential-alias-fixture",
        "title": "Credential alias fixture",
        "duration": 1,
        "webpage_url": secret_url,
    }
    runner = FakeRunner([json.dumps(payload).encode()])

    result = YtDlpAdapter(runner).discover(
        "https://media.example/watch?id=public-fixture",
        cwd=tmp_path,
    )

    assert "owner-secret-canary" not in result.source.locator
    assert "owner-secret-canary" not in json.dumps(result.sanitized_metadata)


def test_yt_dlp_keeps_benign_subscription_query(tmp_path: Path) -> None:
    source_url = "https://media.example/watch?subscription=public-catalog"
    payload = {
        "id": "benign-query-fixture",
        "title": "Benign query fixture",
        "duration": 1,
        "webpage_url": source_url,
    }
    runner = FakeRunner([json.dumps(payload).encode()])

    result = YtDlpAdapter(runner).discover(source_url, cwd=tmp_path)

    assert result.source.locator == source_url
    assert len(runner.argv) == 1
