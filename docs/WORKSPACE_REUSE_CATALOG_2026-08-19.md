# Nika Core — current workspace reuse catalog

Updated: 2026-08-19.
Status: current successor for active/future workspace scope. It supplements the detailed 2026-08-18 catalog; where workspace scope conflicts, this file is newer.

## Global workspace rule

A workspace owns only its domain schema, source rules, user-facing workflows and reports. Generic capabilities belong in reusable Nika Core services/adapters so future workspaces can reuse them.

All workspaces use:
- Workspace/Plugin SDK and versioned capability declarations;
- Task/runtime/checkpoint/recovery services;
- Memory/Knowledge namespaces;
- ModelGateway and Deterministic Brain rather than direct provider calls;
- Tool Registry/permission/approval/audit;
- Universal Research/document/browser adapters where applicable;
- shared accessible reporting;
- Product Journey gate for any user-facing feature.

## Common document and knowledge layer
- REUSE standard Python file/archive/structured-data libraries where sufficient.
- REUSE pypdf for PDF structure/text; OCR is fallback for scanned PDFs.
- REUSE python-docx for DOCX.
- REUSE openpyxl for XLSX manipulation; pandas only for real analytics.
- REUSE lxml/trafilatura-style maintained extraction for HTML/XML where appropriate.
- REUSE SQLite FTS5 before vector search.
- REUSE RapidFuzz when fuzzy matching/dedup is justified.
- OPTIONAL Qdrant/local embeddings only after retrieval evaluation proves benefit.
- CUSTOM thin: Nika artifact identity, provenance, workspace permission scopes, dedup fingerprints and schemas.

## Universal Research / GrantScanner / Product Search / opportunity profiles

Use one shared Universal Research Engine rather than separate crawler implementations.

Preferred source order:
1. official/stable API with HTTPX/provider SDK;
2. ordinary HTTP/static extraction;
3. Playwright semantic browser for dynamic/authenticated flows that the user is authorized to access;
4. higher-level browser agents only after a measured proof;
5. OCR/vision/coordinates only where semantics are unavailable.

Shared requirements:
- source identity/health/priority;
- incremental last-seen/hash/version state;
- rate limits and lawful source policy;
- extraction and deterministic pre-filter;
- model-free/embedded/cloud analysis routing only when needed;
- evidence fragments and uncertainty;
- deduplication;
- structured profile-specific cards;
- review state;
- only new/changed results on recurring runs;
- accessible DOCX/XLSX/CSV/TXT/HTML output.

GrantScanner is a profile with grant-specific fields/geography/deadline/eligibility/relevance, not a separate crawler architecture. Product Search and future opportunity search profiles reuse the same engine with different card schemas and filters.

## YouTube Research workspace
- Prefer official YouTube APIs for search/metadata where available/appropriate.
- Reuse yt-dlp only for capabilities and source contexts where its use is technically/legal-policy appropriate; do not rewrite media extractors.
- Reuse external FFmpeg/ffprobe only with exact binary/license decision when media conversion/probing is required.
- Transcripts may come from allowed subtitles, user-provided text/files or the Transcription worker.
- CUSTOM thin: Nika video/course schema, dedup, job state, provenance, permission and accessible knowledge/report UX.

## Transcription / audio workspace
- ADAPT sherpa-onnx for broad offline speech capabilities where its supported models win a benchmark.
- ADAPT faster-whisper for Whisper-oriented transcription when it wins language/accuracy/performance tests.
- Keep large speech models outside base Nika package and bind them by version/checksum/license.
- CUSTOM thin: chunk/resume, timestamp/segment schema, model policy, artifact provenance and accessible navigation.

## AI Trader workspace
AI Trader is a future real workspace built on Nika common services, not a claim already satisfied by the generic Experiment Engine.

Reuse direction:
- pandas/numpy for genuine tabular/time-series workloads when needed;
- scikit-learn for measured statistical/classification/ranking models;
- Gymnasium for controlled simulation/RL interfaces if later experiments justify it;
- existing Nika Experiment Engine for versioned strategies/metrics/champion-challenger/rollback;
- Deterministic Brain for explicit replay/risk/workflow planning without any LLM;
- optional embedded/Ollama/cloud model analysis through ModelGateway only where it improves a measured task.

Workspace-owned capabilities include no-lookahead replay, odds/event snapshots, virtual bank, singles/combinations/portfolio exposure, time waves, strategy schemas/versions, risk/drawdown metrics, held-out evaluation, live/prematch paper trading and accessible reports.

Any future live-money connector is a separate capability with explicit authorization scope/budget and Nika risk/approval enforcement. It cannot silently expand its own permissions.

## Model Engineering Lab
- Benchmark Microsoft Foundry Local models, Ollama/local-server models, allowed cloud providers and specialist models through stable Nika contracts.
- Measure quality, latency, RAM/CPU/GPU, resource contention and real task success on versioned evaluation sets.
- Manage model provenance/license/checksum and optional component installation.
- Test prompt/strategy/retrieval variants through the existing Experiment Engine.
- Optional PEFT/LoRA-style adaptation is isolated research only when hardware/licensing/dataset metrics justify it.

## Business Agent Lab
Use Universal Research for existing opportunities/market evidence and Software Factory for bounded creation of reusable digital products/tools. Nika owns provenance, risk, permissions, result validation and accessible reports. Do not invent a separate crawler/coding infrastructure for Business Lab.

## My Corrector
Reuse Unicode/regex/language-specific maintained text tooling for deterministic corrections where useful. Use ModelGateway only for semantic rewrite/explanation. Keep protected terms, revision history, before/after evidence and privacy rules in Nika/workspace code.

## Table Tennis Stats Collector
Treat as a small proof/workspace for source ingestion, dedup, incremental change state, statistics and accessible CSV/TXT reports. Reuse Universal Research/data primitives; do not create a second sports crawler architecture.

## Software Factory workspace
- ADAPT OpenHands as first coding-worker proof behind `CodingWorkerPort` where its adopted surface remains suitable/licensed.
- Reuse native project tooling (git, linters, test/build systems) rather than reimplementing them.
- Consider Tree-sitter only when structural code indexing solves a concrete problem.
- CUSTOM thin: Nika project state, allowed paths/tools, branch/worktree isolation, capability-gap linkage, test evidence, accessibility review and release provenance.

## Accessibility Repair / computer interaction workspace capability
- Windows semantics/UIA/UFO²-style adapter before screenshots/coordinates.
- Playwright DOM/accessibility semantics before browser vision.
- OCR/vision only as fallback.
- CUSTOM thin: explanation in accessible text, permission/evidence, reusable repaired workflow adapters.

## Secrets/accounts/connectors
- Prefer official provider APIs/SDKs where account operations are needed.
- REUSE OS-backed keyring/credential storage for persistent secret references.
- Never vendor OAuth credentials, tokens, cookies or browser profiles.
- Workspace configuration stores opaque references/policy, not Git-backed plaintext secrets.

## Removed active workspace: Telegram

Telegram is **not** an active Nika Core planned workspace as of the user's 2026-08-19 decision. Do not add Telethon, TDLib, Telegram Bot API dependencies, tables, UI or roadmap work merely because older documents mention them. Historical Telegram designs remain archival reference only.

If the user explicitly requests a Telegram workspace in the future, handle it as a brand-new optional workspace: fresh upstream/API/license/security review, dedicated permission scope, Product Journey proof and no effect on the generic Core architecture.

## Adoption gate for every real workspace
1. identify all generic capabilities;
2. search current maintained upstream implementations;
3. record exact license/version/Windows fit;
4. reuse/adapt shared Nika capability before custom workspace infrastructure;
5. custom-code only domain semantics or a proven upstream gap;
6. test the adapter/workspace in isolation;
7. integrate with real Nika runtime/memory/permissions/UI;
8. pass the Product Journey gate before claiming the user can use it;
9. keep optional/heavy dependencies outside mandatory Core where practical.
