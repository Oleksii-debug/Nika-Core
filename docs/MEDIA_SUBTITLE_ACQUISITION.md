# DEV05 platform subtitle acquisition boundary

Status: implementation candidate. This document records the directly coupled design/evidence boundary for platform subtitles; exact-head CI is required before integration credit.

## REUSE / ADAPT / CUSTOM

- **REUSE** maintained `yt-dlp` through the pinned Python package and fixed `sys.executable -m yt_dlp` subprocess boundary. Nika does not implement a site extractor.
- **REUSE** `pysubs2` for subtitle parsing. Nika owns quality thresholds and normalized transcript contracts.
- **ADAPT** yt-dlp subtitle discovery/materialization to Nika-owned stable source/version/track identity, bounded files and typed errors.
- **CUSTOM (thin)** stable track identity, rediscovery requirement, byte/time policy, checksum-bound `MediaAsset`, transcript normalization and privacy boundary.

No FFmpeg, ASR, OCR, model or browser dependency is introduced by this slice.

## Durable identity rule

An upstream subtitle CDN URL is ephemeral and may be signed or expire. It is not durable Nika identity.

The durable selector is `SubtitleTrack.track_id`, derived by the existing yt-dlp adapter from subtitle kind, language, format/name and candidate position. Callers that persist a discovery result must use `stable_subtitle_tracks()`, which removes `SubtitleTrack.url`. The URL field remains in the contract only for backward-compatible deserialization of earlier in-memory/snapshot shapes; this acquisition path never requires it.

Every materialization attempt starts from the stable source page URL, performs fresh yt-dlp metadata discovery and requires:

1. the same `MediaVersion.version_id`;
2. the same `SubtitleTrack.track_id`;
3. a supported manual or automatic subtitle kind.

If the media version changed or the selected track disappeared, materialization fails closed instead of silently downloading a different subtitle.

## Materialization boundary

The actual subtitle download uses a fixed argv and a dedicated staging directory under the caller-provided allowed root:

- `shell=False` via the existing `SafeProcessRunner`;
- `--ignore-config`;
- `--skip-download`;
- `--no-playlist`;
- no info JSON/comments/thumbnail output;
- exactly one explicit subtitle language and format;
- manual tracks use `--write-subs`; automatic tracks use `--write-auto-subs`;
- bounded timeout and configured subtitle byte limit;
- cancellation flows through the existing process runner/process-tree termination boundary.

The fresh upstream subtitle URL remains internal to yt-dlp. It is not passed as a Nika subprocess argument, artifact field, transcript field or durable locator.

A successful download must yield exactly one completed regular subtitle file. Nika validates non-empty/bounded bytes, moves them through a `.partial` file, computes SHA-256 and atomically promotes a final immutable subtitle asset. The normalized `Transcript` is then created through the existing `pysubs2` subtitle quality policy. Automatic subtitles therefore still require non-empty monotonic segments, bounded malformed ratio and configured coverage.

## Restart/cancel behavior

A cancelled/failed attempt may leave only bounded files in its deterministic dedicated staging directory. Before another attempt, Nika validates every staging entry and its aggregate size. Oversized or structurally unexpected staging state fails closed. A completed top-level `.partial` is never silently overwritten; it requires explicit reconciliation.

Platform subtitles are deliberately much smaller than remote media assets, so this slice does not invent a separate chunking protocol. Long audio transcription continues to use the existing durable chunk manifest/restart path.

## Privacy and provenance

- No cookie, browser profile, API token or URL credential is added.
- The stable source URL still passes existing remote-source validation.
- Signed/ephemeral subtitle URLs are not required for persistence or replay.
- Durable output consists of stable source/version/track identity, checksum-bound asset bytes and normalized transcript/provenance.
- Local confidential media remains local by policy; this adapter does not introduce cloud routing.

## Evidence truth

Deterministic tests cover URL-free stable projection, fresh-version/track rediscovery, manual/automatic argv separation, signed-locator non-propagation, normalization/quality integration, cancellation staging and byte-bound rejection. Normal CI uses fixtures and does not download real media. A live service-specific subtitle download remains a focused external proof, not a prerequisite for deterministic contract correctness.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
