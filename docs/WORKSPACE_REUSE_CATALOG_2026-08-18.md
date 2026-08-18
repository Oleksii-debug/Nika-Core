# Nika Core — real-workspace reuse catalog (2026-08-18)

Purpose: prevent future Grant/Research, Telegram, YouTube, Transcription, Corrector, Product Search, statistics and Software Factory workspaces from reimplementing commodity file/web/media/protocol code.

This document is subordinate to `docs/REUSE_CATALOG_2026-08-18.md`: exact versions, maintenance state and licenses are rechecked immediately before adoption.

## Common file and document layer
- **REUSE standard library** `pathlib`, `shutil`, `tempfile`, `zipfile`, `tarfile`, `csv`, `json` and `sqlite3` for ordinary files, temporary staging, archives and simple structured data. Do not create wrappers that merely duplicate these APIs.
- **REUSE pypdf** for PDF text/metadata, merge/split/transform and basic encryption capabilities when PDF structure is accessible. OCR remains a separate fallback for scanned/image PDFs.
- **REUSE python-docx** for DOCX read/write/manipulation. Do not hand-edit Office Open XML ZIP/XML unless the library cannot express a required feature.
- **REUSE a currently maintained XLSX library (normally openpyxl after fresh upstream verification)** for spreadsheet file manipulation; use pandas only for actual tabular analytics, not for every spreadsheet edit.
- **REUSE lxml** where robust HTML/XML parsing is needed outside a live browser.
- **REUSE SQLite FTS5** for local deterministic full-text search before introducing a vector database.
- **REUSE RapidFuzz** for fuzzy string matching/dedup/entity-name comparison when exact matching is insufficient.
- **CUSTOM (thin)** Nika artifact identity, provenance, permissions, dedup fingerprints and workspace-specific schemas.

## Universal research / GrantScanner / Product Search
Preferred stack:
1. HTTP/API first with **HTTPX** where a stable endpoint exists.
2. Static HTML parsing with a maintained parser only when needed.
3. **Playwright** for dynamic pages, authentication flows and semantic browser interaction.
4. **Browser Use** only as an optional higher-level adapter if it beats the deterministic baseline in a proof.
5. Screenshot/OCR/vision only when structured semantics are unavailable.

Reuse existing URL, HTTP, HTML, robots/rate-limit, cache and dedup primitives. Nika owns source provenance, freshness, scoring, grant/product schemas and action permissions.

## Telegram workspace
- Do **not** select an old Python client solely by popularity. During the 2026-08-18 audit, `LonamiWebs/Telethon` is marked archived on GitHub, so it is not the default new dependency without a fresh maintenance decision.
- **ADAPT candidate: TDLib** (`tdlib/td`, Boost Software License 1.0) for full Telegram-client behavior. It is maintained, cross-platform, supports Windows, handles networking/encryption/local storage and exposes a JSON interface suitable for a language-neutral Nika adapter.
- For bot-only workflows, prefer the current official Telegram Bot API and a maintained thin client rather than full client-account machinery.
- **CUSTOM (thin)** Nika account/session reference handling, permission policy, message dedup/idempotency, human approval for sends/deletes and accessible workspace UX.
- Telegram session/auth material is local private state and never enters Git or user-facing archives.

## YouTube / media research workspace
- **REUSE/ADAPT yt-dlp** (Unlicense) for extractor/downloader/metadata/subtitle capabilities across YouTube and other supported sites instead of writing extractors.
- **ADAPT external FFmpeg/ffprobe** for codec/container conversion, audio extraction and probing where required. Exact binary build/license must be recorded because FFmpeg redistribution terms depend on build configuration; do not casually bundle an arbitrary binary.
- **REUSE** platform/media metadata libraries only when they remove concrete glue.
- **CUSTOM (thin)** Nika playlist/channel/course schema, job state, duplicate prevention, naming policy, user permissions and upload/publish approval.

## Transcription / audio workspace
- **ADAPT sherpa-onnx** for broad offline speech features where its supported models meet accuracy/performance requirements.
- **ADAPT faster-whisper** for Whisper-focused transcription where benchmarked accuracy/language behavior is superior.
- Reuse VAD/diarization/keyword/TTS capabilities from maintained engines rather than writing signal-processing algorithms from scratch.
- Keep large models outside the base Nika package and record model/version/checksum separately.
- **CUSTOM (thin)** task chunking/resume, timestamps/segments schema, model selection policy, artifact provenance and accessible result navigation.

## OCR / accessibility rescue / document scanning
- **REUSE Tesseract** for mature OCR fallback.
- **ADAPT PaddleOCR** for heavier multilingual/document-layout OCR where measured benefit justifies the dependency.
- **REUSE OpenCV** for image preprocessing only where needed.
- **ADAPT Microsoft UFO² / UI Automation** before vision for Windows controls; **Playwright/DOM semantics** before OCR for websites.
- **CUSTOM (thin)** accessible textual explanation, action permission/evidence and reusable workflow repair adapters.

## Corrector / text-processing workspace
- Reuse standard Unicode/regex libraries and maintained language-specific NLP/spell/grammar engines when a measurable task requires them.
- Use LLMs through Model Gateway for semantic rewrite/classification/explanation, not for deterministic file/state mechanics.
- Use **RapidFuzz** for fuzzy matching/dedup where applicable.
- **CUSTOM (thin)** user rules, protected terms, before/after evidence, revision history and approval semantics.

## Statistics / table-tennis / structured-data workspace
- **REUSE pandas/numpy** only for genuine tabular/statistical workloads.
- **REUSE scikit-learn** only when predictive/classification models have data and metrics.
- Use SQLite for durable normalized local data and provenance.
- Reuse plotting/report libraries only at the presentation/export boundary; no custom numerical primitives without need.

## Software Factory / code workspace
- **ADAPT OpenHands** as first coding-worker proof.
- Reuse installed `git` CLI for branch/worktree/diff operations unless a Python Git library solves a concrete portability problem.
- Consider maintained syntax parsers such as Tree-sitter only when structural code indexing/analysis is required; do not build language parsers.
- Reuse test/lint/build tools native to each target project.
- **CUSTOM (thin)** Nika project/task orchestration, branch safety, acceptance gates, accessibility review and release provenance.

## Search and knowledge retrieval
- Start with SQLite indexes/FTS5 and deterministic filters.
- Add **Qdrant local mode** only when semantic retrieval evaluation shows benefit; it uses the same client API that can later target a server.
- Reuse ONNX/FastEmbed-style compact embedding inference when appropriate instead of requiring a cloud model for every retrieval task.
- **CUSTOM (thin)** source scope, permission filtering, provenance, retention and user-approved long-term-memory rules.

## Secrets/accounts/connectors
- **REUSE Python keyring** for persistent OS-backed credential references on Windows where suitable.
- Prefer official provider APIs/SDKs for Google/YouTube/Telegram/etc. when account operations are needed.
- Never vendor OAuth credentials, token files, cookies or browser profiles.
- Nika stores opaque local references/metadata and policy, not secrets in Git-backed configuration.

## Adoption gate for every workspace
Before implementation:
1. identify every generic capability;
2. search current maintained upstream implementations;
3. record license/maintenance/Windows fit;
4. choose REUSE or ADAPT by default;
5. create CUSTOM code only for Nika/workspace semantics or a proven upstream gap;
6. test the adapter in isolation;
7. keep optional/heavy dependencies outside mandatory Nika Core when practical.
