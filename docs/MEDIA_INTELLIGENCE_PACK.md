# Media Intelligence Pack — DEV05

Updated: 2026-08-19.
Status: Batch A implementation candidate. No ASR/OCR heavy adapter is included in this batch.

## Ownership and boundaries

`nika_core.media` owns media acquisition, metadata/probe, subtitle normalization, future audio/ASR/OCR adapters, media provenance, revision-preserving correction, optional media-component truth and media resource claims. DEV01 owns corpus/search/document text extraction. DEV04 owns computer-interaction and vision-request policy. ModelGateway internals, Agent Lab, Windows UI and release workflows are outside DEV05 ownership.

One durable media system is used for local files and remote media. Upstream yt-dlp/FFmpeg/pysubs2 objects never cross the Nika-owned contracts. Original assets are immutable after registration and text changes are append-only revisions.

## Reuse decisions

- **ADAPT yt-dlp 2026.06.09** through the pinned Python package and `sys.executable -m yt_dlp`. Nika uses only validated argv through `SafeProcessRunner`; no shell string, `--exec`, arbitrary output template, plugin escape or automatic browser-cookie loading is exposed. The PyPI wheel/source distribution is preferred because yt-dlp documents that this surface is Unlicense while bundled executables include broader third-party license surfaces.
- **REUSE pysubs2 1.8.1 (MIT)** for subtitle parsing/normalization. It is optional and imported lazily; absence is a typed component-missing state rather than an automatic install.
- **ADAPT external FFmpeg/ffprobe** rather than bundling a binary. Current stable upstream at implementation time is FFmpeg 9.0.1. The exact discovered executable is hashed and `ffprobe -version` plus `-buildconf` are recorded. License classification is build-dependent: base FFmpeg is LGPL-2.1-or-later, while `--enable-gpl`/related build configuration requires GPL review and `--enable-nonfree` is explicitly flagged for review.
- **CUSTOM thin** Nika contracts, durable media sub-schema, provenance, subtitle-first ranking/quality policy, privacy redaction, resource claims and typed error taxonomy because upstream engines cannot own Nika product/recovery semantics.

No FFmpeg binary, yt-dlp executable bundle, speech model, OCR model, Tesseract language pack or Paddle stack enters the base package.

## Durable persistence

Media state uses the same canonical `SQLiteStore` and database. Because open DEV01 PR #55 currently owns the next global migration number (migration 9), DEV05 does not edit global `data/schema.py`. Batch A uses an ordered `media_schema_migrations` ledger inside the same database for media-owned tables. This avoids taking DEV01's migration number or creating a second database/product-state universe. Integration may fold this sub-schema into the global chain after DEV01 lands, as an explicit compatibility decision.

Durable records include source/version identity, immutable assets, processing jobs/checkpoints, optional component state and append-only text revisions. On application restart, a media job found in RUNNING is not blindly resumed: it becomes BLOCKED with `restart_reconciliation_required`, preserving its checkpoint for explicit stage reconciliation.

## Safe process boundary

`SafeProcessRunner` requires an argv sequence, `shell=False`, `stdin=DEVNULL`, a real dedicated cwd, output byte caps and an explicit deadline. It creates a process group/session and terminates the process tree on timeout/output overflow. Process errors are normalized and redacted before entering user-visible media errors.

External media adapters may only use this runner or an equivalent stricter boundary. Downloaded/derived files in later batches must use `.partial` -> validate/probe/checksum -> atomic rename; Batch A does not yet perform media download or audio extraction.

## Local-file import

Local import resolves both allowed root and file, rejects path escape (including resolved symlink escape), applies a hard byte limit, hashes before registration and records an immutable original asset. Windows paths with spaces/Unicode are ordinary `Path`/argv values, never manually quoted shell strings.

## yt-dlp discovery

Batch A uses yt-dlp only for bounded metadata/formats/subtitle discovery with `--skip-download`, `--dump-single-json`, `--ignore-config` and `--no-playlist` by default. Playlists are disabled unless policy explicitly allows a bounded count; even then Batch A processes one media item per durable job. Duration is bounded before later expensive processing.

`auth_ref` is an opaque product reference only. Batch A deliberately does not resolve it and fails with `AUTH_REQUIRED`; no `--cookies-from-browser`, raw cookie/token/profile path or credential is accepted. Sanitized metadata keeps a narrow allow-list and redacts signed/token query values.

## Subtitle-first policy

`force_transcription=True` always bypasses subtitles. Otherwise the deterministic order is preferred-language manual exact/base match, then later preferred languages, then automatic exact/base match; translated tracks are excluded unless explicitly allowed. A track outside preferred language/base matches is not silently selected.

pysubs2 normalization removes presentation tags and deterministic whitespace noise while retaining timestamps. Automatic captions must be nonempty, monotonic, below the configured malformed ratio and meet a minimum time-coverage ratio; thresholds are fixture-tested. A usable subtitle creates `Transcript.method=platform_subtitle`, so later coordinator logic must not invoke ASR for that job.

## FFprobe evidence

The adapter captures exact executable SHA-256, reported version, build configuration and build-dependent license classification. Probe output is normalized to Nika `Probe`; only bounded stream fields are retained. Absolute executable/media paths are process inputs, not exported upstream provider objects.

## Resource policy

Media resource claims bind to the existing `ResourceManager`. `HEAVY_MODEL` claims share a machine-level scope with `max_concurrent=1` by default, establishing the target-laptop rule that ASR/OCR heavy models do not reside concurrently until measured policy proves otherwise. Batch A itself does not load a heavy model.

## Security/privacy invariants

- no cookies/tokens/browser profiles in source, logs or provenance;
- no silent cloud routing;
- no silent model/binary/component download;
- no shell execution surface;
- bounded path, duration, playlist, subprocess output and timeout policy;
- original evidence is never overwritten/deleted by correction;
- engine license, model license and binary-build license are independent evidence fields.

## Batch A acceptance evidence

Focused tests cover media schema restart/idempotence, interrupted-job reconciliation, immutable assets, append-only revisions, Unicode/path escape and hard size limits, privacy redaction, optional-component missing state, subprocess timeout/output caps, yt-dlp argv/auth/playlist/duration policy, subtitle ranking and automatic-caption coverage, FFprobe build-license normalization, and machine-level heavy-resource mutual exclusion.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`. Batch A has no user-facing Windows UI change and therefore cannot earn a Product Journey/NVDA claim.

## Next large batch after exact-head green

Batch B adds FFmpeg audio extraction/normalization with `.partial` atomic promotion, `OfflineTranscriberPort`, durable chunk manifest, deterministic overlap/core merge, optional faster-whisper and sherpa-onnx peer adapters, explicit model-missing/no-download states, cancel/restart skip of completed chunks, silence/empty-audio handling, engine/model provenance and a target-machine benchmark harness. No engine is declared the winner without physical Windows measurements.
