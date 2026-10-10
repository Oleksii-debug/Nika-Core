# Nika Core — deep full-product reuse audit — 2026-08-27

Status: **candidate/adoption-control supplement**, not implementation or acceptance credit.

Research baseline: live Nika Core `main` at `9dd4013625979492a125080f32e307fd5d808d48` plus fresh upstream documentation/repository/license checks on 2026-08-27.

This supplement extends `docs/REUSE_CATALOG_2026-08-18.md` and `docs/THIRD_PARTY_ADOPTION.md`. It does **not** supersede integrated choices, Nika-owned contracts, `docs/VERSIONED_RELEASE_TRAIN.md`, security/accessibility/release gates, or the current `V0.1_ONLY_UNTIL_RELEASE` execution rule.

No component listed here is automatically approved for installation, bundling, model download, network use, or production execution. Every actual adoption still requires a concrete Nika capability gap, exact version/source/license/provenance, focused Windows/package/security tests and the ordinary branch/CI/audit/integration path.

## 1. Decision tiers

- **REUSE / STRONG CANDIDATE** — mature commodity capability that should normally be reused instead of custom infrastructure when the need becomes active.
- **ADAPT / PROOF** — promising component that must first win a focused Nika proof/benchmark behind an existing Nika-owned port.
- **QA / CI ONLY** — useful for independent verification; should not become a production runtime dependency without a separate decision.
- **OPTIONAL WORKER / NODE** — useful for heavy or platform-specific capability outside the mandatory Windows base application.
- **CAUTION / REJECT BY DEFAULT** — licensing, maturity, platform, privacy or authority concerns mean it must not be adopted casually.

## 2. What Nika must still own

Reuse must not outsource product authority. Nika continues to own:

- task/agent/workspace/ProductProject identity and lifecycle;
- R0–R4 permission, approval and standing-permission boundaries;
- durable effect identity, replay/reconciliation authority and audit truth;
- model/tool/runtime/planner/browser/Windows ports and normalized events/errors;
- source/provenance/evidence semantics and workspace isolation;
- accessible Windows UX, Action Registry/Keymap and user-visible status/recovery;
- release identity, exact-SHA acceptance, backup/recovery/rollback policy;
- credential references/scopes/audiences and least-privilege policy;
- experiment promotion/rollback truth;
- human/NVDA acceptance states.

Third-party objects must remain behind these boundaries.

## 3. Anti-rewrite decisions

When the corresponding requirement becomes active, prefer these directions before custom framework work:

1. **Do not write a cryptographic updater protocol.** Use The Update Framework (`python-tuf`) for update metadata/trust; Nika owns channels, release identity, approvals, recovery and UX.
2. **Do not write a Windows installer/update engine.** Evaluate MSIX + App Installer/Windows App SDK packaging around the proven Nika payload before inventing an installer service.
3. **Do not invent SBOM formats/generators.** Use CycloneDX tooling; independently scan built artifacts with Syft/Trivy where useful.
4. **Do not invent supply-chain attestation/signing formats.** Evaluate in-toto and Sigstore for CI/release evidence.
5. **Do not build a Python dependency resolver/environment manager.** Evaluate `uv` for isolated Software Factory environments and lock resolution; Nika must prohibit unapproved runtime downloads.
6. **Do not build a test-matrix session runner.** Use Nox where it reduces custom subprocess/test orchestration.
7. **Do not hand-write OAuth/OIDC/JWT protocol machinery.** Use Authlib for generic standards and provider SDKs such as MSAL where a provider requires them.
8. **Do not hand-write document layout/table parsing.** Prove Docling as the rich normalization route; keep lighter/specialized parsers as measured fallbacks.
9. **Do not hand-write RSS/Atom, public-suffix or natural-date parsers.** Use feedparser, tldextract and dateparser with Nika normalization/policy around them.
10. **Do not invent HTTP cache semantics.** Evaluate Hishel for HTTPX cache behavior; Nika still owns freshness/provenance/auth decisions.
11. **Do not invent generic asyncio rate limiting.** Use aiolimiter where a simple leaky-bucket fits; Nika owns per-source/provider budgets and identities.
12. **Do not invent an analytical SQL engine.** Use DuckDB for local analytical workloads; never make it authoritative task/product state.
13. **Do not deploy a vector database by reflex.** For smaller local corpus workloads prove SQLite-vec first; retain Qdrant for measured larger/service-backed needs.
14. **Do not invent embedding/reranking inference.** Evaluate FastEmbed/ONNX-backed models under explicit model-license and download gates.
15. **Do not invent hyperparameter search.** Use Optuna for Model Lab/search experiments while Nika owns datasets, metrics, promotion and rollback.
16. **Do not build a generic ML experiment server unless needed.** Evaluate MLflow/DVC only as optional experiment/artifact backends; never as authoritative Nika task/ProductProject state.
17. **Do not home-grow accessibility rules.** Use axe-core through Playwright for HTML/WebView rule checks and an independent Windows UIA oracle such as FlaUI where appropriate; neither replaces human NVDA.
18. **Do not home-grow property/API fuzzing.** Use Hypothesis and Schemathesis for exact state-machine/contract attack families.
19. **Do not home-grow Windows process-tree containment.** Use Windows Job Objects, preferably through maintained Windows bindings, with Nika policy/evidence around them.
20. **Do not home-grow cryptographic primitives.** Use high-level TUF/Sigstore protocols and `cryptography` only when a lower-level primitive is genuinely required.
21. **Do not hand-parse media containers.** Use FFmpeg/PyAV/yt-dlp/specialist engines as isolated optional workers with exact binary/model/license provenance.
22. **Do not invent telemetry protocols.** Use OpenTelemetry for optional sanitized traces/metrics; keep Nika Audit Log separate and authoritative.
23. **Do not invent frontend/backend localization frameworks.** Use i18next on the WebView frontend and Babel on Python-side locale formatting where needed.

## 4. Secure update, release and supply chain

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **python-tuf / The Update Framework** | REUSE / STRONG CANDIDATE | MIT OR Apache-2.0; Python; Windows supported by package classifiers | Secure update metadata, threshold/signature trust, rollback/freeze protections | TUF does not own Nika release approval, source SHA, backup/recovery or UI. Pin metadata roles/keys and test offline/recovery paths. |
| **MSIX + App Installer / Windows App SDK packaging** | ADAPT / PROOF | Windows platform technology | Reliable install/uninstall, package identity and supported update channel around a proven Nika payload | Evaluate after current PyInstaller path is stable; do not invalidate portable/offline needs. Signing and rollback remain explicit release gates. |
| **Sigstore Python** | QA / CI / RELEASE PROOF | Apache-2.0 | Keyless/identity-backed signing and verification of release artifacts/attestations | Network/OIDC/transparency-service dependencies make this a release tool, not ordinary runtime authority. Preserve offline verification plan. |
| **in-toto** | REUSE / STRONG CANDIDATE | Apache-2.0 | Software Factory build/test/release step attestations | Nika owns trusted ProductProject/repository/release identity and decides which attestations are acceptable. |
| **CycloneDX Python library / `cyclonedx-bom`** | REUSE / STRONG CANDIDATE | Apache-2.0 | Generate Python/project SBOM and future component/model BOM records | Do not invent a private BOM format. Nika may extend with its own manifest references rather than replacing CycloneDX. |
| **Syft** | QA / CI ONLY | Apache-2.0; external CLI | Independent filesystem/package/container SBOM generation | Useful cross-check against Python-only BOM output; keep out of mandatory base EXE. |
| **Trivy** | QA / CI ONLY | Apache-2.0 | Repository/filesystem/SBOM vulnerability and license scanning | Full license scanning can be expensive; unknown/restricted classifications still require Nika review. |
| **Bandit** | QA / CI ONLY | Apache-2.0 | Python AST security lint in Software Factory/release gates | Supplements, not replaces, threat-model tests and review. |
| **license-expression** | REUSE / PROOF | Verify exact adopted release before graduation | Parse/normalize SPDX-style license expressions in compliance gates | Nika owns allow/deny/obligation decisions; parser output alone is not legal approval. |
| **cryptography** | REUSE SELECTIVELY | Apache-2.0 OR BSD-3-Clause | Standard crypto primitives only when high-level protocols cannot cover a narrow need | No custom crypto design; prefer TUF/Sigstore higher-level semantics. |
| **Squirrel.Windows** | CAUTION / SECONDARY PROOF | MIT; .NET-centric Windows updater | Possible alternate installer/updater if MSIX cannot satisfy a measured requirement | Lower priority than MSIX + TUF for current Python/WebView2 architecture. |

Primary sources: https://theupdateframework.io/ , https://github.com/theupdateframework/python-tuf , https://learn.microsoft.com/windows/msix/ , https://learn.microsoft.com/windows/msix/app-installer/ , https://github.com/sigstore/sigstore-python , https://in-toto.io/ , https://cyclonedx.org/ , https://github.com/anchore/syft , https://trivy.dev/ , https://bandit.readthedocs.io/ .

## 5. Software Factory development environments and build execution

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **uv** | ADAPT / HIGH-PRIORITY PROOF | MIT OR Apache-2.0; production-stable; Windows | Fast lock/resolution, virtual environments, project/tool execution for isolated CodingWorker jobs | `uv` can download missing Python runtimes. Nika must disable/gate unapproved acquisition and record exact runtime/package provenance. |
| **Nox** | REUSE / PROOF | Apache-2.0; Windows | Versioned test/lint/build matrix sessions for generated products | Nika still owns allowed commands, workspace root, timeouts, process containment and evidence identity. |
| **pre-commit** | REUSE SELECTIVELY | MIT | Fast repository hooks for formatting/lint/secret/security checks | Optional developer/worker gate; CI remains authoritative. |
| **Pyright** | QA / CI PROOF | MIT; Node-based | Strong static type checking for Python products | Node dependency must be acceptable to target ProductProject/build node. |
| **mypy** | QA / CI PROOF | MIT | Python-native type-check alternative | Choose Pyright or mypy per project; do not run both everywhere without measured value. |
| **ty** | ADAPT / EXPERIMENT ONLY | MIT; pre-1.0/0.x maturity | Very fast type-check candidate | Do not make a release gate until API/behavior maturity is sufficient. |
| **Dagger** | OPTIONAL WORKER / EXECUTION NODE | Apache-2.0; container-engine oriented | Reusable cached build/test pipelines for Product Factory remote/container nodes | On Windows it relies on a Linux container runtime/WSL-like environment. Never make it mandatory for base Nika. |
| **Windows Job Objects** | REUSE OS PRIMITIVE | Windows native API | Process-tree lifetime, memory/process/CPU/time limits and kill-on-close for local CodingWorkers | Wrap with Nika process/workspace/evidence policy; prefer maintained Windows bindings rather than bespoke process polling. |
| **Podman** | OPTIONAL WORKER / NODE | Open-source container stack; Windows through VM/WSL2 model | Optional rootless container execution node | Not a native base-app sandbox. Platform/VM startup, filesystem and network boundaries require explicit proof. |
| **Dulwich** | ADAPT / PROOF | Apache-2.0 OR GPL-2.0-or-later | Pure-Python Git operations where external Git CLI is unavailable or undesirable | Do not replace known-good Git/GitHub paths without coverage benchmark; record selected license option. |
| **pygit2** | CAUTION | GPLv2 with linking exception; libgit2 native dependency | Rich Git alternative | License/native packaging complexity makes it non-default for Nika base. |

Primary sources: https://docs.astral.sh/uv/ , https://github.com/wntrblm/nox , https://pre-commit.com/ , https://github.com/microsoft/pyright , https://mypy-lang.org/ , https://github.com/astral-sh/ty , https://dagger.io/ , https://learn.microsoft.com/windows/win32/procthread/job-objects , https://podman.io/ , https://www.dulwich.io/ .

## 6. Testing, fuzzing and independent accessibility QA

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **Hypothesis** | QA / CI STRONG | MPL-2.0 | Stateful/property tests for durable stores, schedulers, idempotency, permission invariants and parsers | Test-only by default; deterministic seeds/reproducers must be retained in failure evidence. |
| **Schemathesis** | QA / CI STRONG | MIT | OpenAPI/GraphQL property/fuzz tests for Product Factory generated services and Nika HTTP contracts | Does not replace business/security-specific acceptance assertions. |
| **RESPX** | QA / CI STRONG | BSD-3-Clause | HTTPX-specific deterministic transport mocking, failure/timeout/redirect scenarios | Strong fit because Nika already uses HTTPX. |
| **axe-core + `@axe-core/playwright`** | QA / CI STRONG | MPL-2.0 | Automated WCAG/ARIA rule checks through the existing Playwright path | Supplement only. Cannot award `NVDA_VERIFIED`. |
| **Testing Library DOM** | QA / CI PROOF | MIT | Frontend unit tests through role/name/user-observable semantics | Prefer over CSS-structure-only tests; Playwright still proves browser E2E. |
| **FlaUI** | QA / CI PROOF | MIT; .NET Windows UIA2/UIA3 | Independent packaged-Windows UI Automation oracle, useful to avoid testing only through the same Python/UIA stack | Keep as independent proof tool; Nika runtime interaction remains behind its current Windows interaction port. |

Primary sources: https://hypothesis.readthedocs.io/ , https://schemathesis.io/ , https://lundberg.github.io/respx/ , https://github.com/dequelabs/axe-core , https://testing-library.com/docs/dom-testing-library/intro/ , https://github.com/FlaUI/FlaUI .

## 7. Structured concurrency, rate limits and HTTP caching

| Component | Decision | License | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **AnyIO** | REUSE SELECTIVELY | MIT | Structured concurrency, cancellation scopes, streams/semaphores/subprocess helpers at adapter boundaries | Must not become a second agent orchestration kernel or replace LangGraph/AgentRuntimePort. |
| **aiolimiter** | REUSE / PROOF | MIT | Leaky-bucket limits for providers, sources, browsers or connector calls | Nika owns quota identity, policy, persistence and user-visible throttling state. |
| **Hishel** | ADAPT / HIGH-VALUE PROOF | BSD-3-Clause | RFC 9111 caching for HTTPX/Requests with persistent backends including SQLite | Research freshness/provenance/auth rules override generic cache hits; never cache sensitive responses outside policy. |
| **DiskCache** | OPTIONAL / PROOF | Apache-2.0 | Local persistent non-authoritative cache for expensive deterministic results | Prefer Hishel for HTTP semantics; adopt only if maintenance/performance proof justifies another cache surface. |

Primary sources: https://anyio.readthedocs.io/ , https://github.com/mjpieters/aiolimiter , https://hishel.com/ , https://grantjenks.com/docs/diskcache/ .

## 8. Universal Research and document normalization

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **Docling / docling-core** | ADAPT / HIGH-PRIORITY PROOF | MIT; model licenses separate | Unified rich document normalization for PDF, Office, HTML, images, audio/video extras, tables/layout, JSON/Markdown/text and chunk output | Nika owns source identity, provenance, permissions, cache and evidence. Heavy extras/models remain optional; no silent downloads. |
| **Microsoft MarkItDown** | ADAPT / LIGHTWEIGHT PROOF | MIT | Lightweight conversion of common Office/PDF/media/message/archive inputs to Markdown/text | Converters run with caller privileges; isolate untrusted inputs. Use as measured fast route, not a second uncontrolled canonical parser. |
| **Trafilatura** | REUSE / STRONG CANDIDATE | Apache-2.0 for current releases; older releases had different license | Main-content/metadata extraction for ordinary HTML/web research | Pin a modern permissive version; browser/semantic route still handles dynamic pages. |
| **selectolax (Lexbor backend)** | REUSE SELECTIVELY | wrapper MIT; Lexbor Apache-2.0; alternate Modest backend LGPL | Fast HTML parsing/cleaning | Prefer Lexbor backend and record transitive engine license. |
| **pdfplumber** | OPTIONAL SPECIALIST | MIT | Detailed PDF character/geometry/table extraction when primary parser needs a focused fallback | Do not make every PDF traverse multiple heavy parsers. |
| **feedparser** | REUSE / STRONG CANDIDATE | BSD-2-Clause | RSS/Atom/feed ingestion | No custom feed parser; Nika adds stable source identity/provenance/freshness. |
| **tldextract** | REUSE / STRONG CANDIDATE | BSD-3-Clause | Correct domain/public-suffix extraction for source identity and policy | Avoid hand-written TLD regex. Public Suffix List update/network behavior must be controlled for offline/reproducible runs. |
| **dateparser** | REUSE / PROOF | BSD-3-Clause | Natural-language/source date normalization | Scheduler still requires explicit timezone and canonical UTC conversion; ambiguous date parsing must fail/ask rather than fabricate. |
| **RapidFuzz** | REUSE / STRONG CANDIDATE | MIT | Deterministic fuzzy dedup/matching/ranking | Already noted in `THIRD_PARTY_ADOPTION`; promote into the broad adoption map rather than invent string metrics. |
| **charset-normalizer** | REUSE SELECTIVELY | MIT | Text encoding detection/normalization | Use only where input bytes lack authoritative charset metadata. |
| **ftfy** | REUSE SELECTIVELY | Apache-2.0 in current line; preserve attribution | Unicode/mojibake repair for user-approved text normalization | Never silently alter authoritative source bytes; preserve original/hash/evidence. |
| **lingua-py** | ADAPT / PROOF | Apache-2.0 | Local deterministic language detection | Useful for routing parsers/models without cloud calls; measure Ukrainian/English accuracy on Nika datasets. |

Primary sources: https://docling-project.github.io/docling/ , https://github.com/microsoft/markitdown , https://trafilatura.readthedocs.io/ , https://github.com/rushter/selectolax , https://github.com/jsvine/pdfplumber , https://feedparser.readthedocs.io/ , https://github.com/john-kurkowski/tldextract , https://dateparser.readthedocs.io/ , https://github.com/rapidfuzz/RapidFuzz , https://github.com/rspeer/python-ftfy .

## 9. Local analytics, full-text and semantic retrieval

| Component | Decision | License / maturity | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **DuckDB** | REUSE / STRONG CANDIDATE | MIT | In-process analytical SQL for Research, Model Lab, Trader and ProductProject datasets; CSV/Parquet analytics | Never replace authoritative SQLite task/ProductProject state. |
| **Polars** | ADAPT / BENCHMARK | MIT | High-performance dataframe engine for large columnar workloads | Choose Polars or pandas per workload; do not add both to mandatory base. |
| **SQLite FTS5** | REUSE / ALREADY PRIMARY | SQLite built-in extension | Deterministic corpus search before semantic retrieval | Keep authoritative provenance/permissions in SQLite-owned Nika state. |
| **sqlite-vec** | ADAPT / PROOF | permissive project; pre-1.0 maturity | Lightweight local vector search colocated with SQLite for small/medium corpus | Breaking changes expected before 1.0; optional extension packaging/Windows proof required. Never authoritative task state. |
| **Qdrant** | OPTIONAL / ALREADY CATALOGUED | Apache-2.0 | Larger local/service vector workloads after retrieval evaluation | Keep optional; Nika permissions/provenance checked before retrieval. |
| **FastEmbed** | ADAPT / HIGH-VALUE PROOF | Apache-2.0 code; model licenses vary | Lightweight ONNX-based local embeddings/rerankers | Model license is separate. Enforce allowlist, explicit acquisition, checksum and resource evidence; some available models are non-commercial. |
| **sentence-transformers** | OPTIONAL HEAVY FALLBACK | Apache-2.0 code; model licenses vary | Broader embedding/reranker ecosystem if FastEmbed cannot meet quality needs | Heavier dependency/model surface; benchmark and license each model. |

Primary sources: https://duckdb.org/ , https://pola.rs/ , https://github.com/asg017/sqlite-vec , https://qdrant.tech/ , https://qdrant.github.io/fastembed/ , https://www.sbert.net/ .

## 10. Model artifacts and Model Engineering Lab

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **huggingface_hub** | ADAPT / STRONG CANDIDATE | Apache-2.0 | Model/artifact discovery, explicit snapshot acquisition, local-only cache access, allow/ignore patterns | Nika owns approval, model license decision, checksum, cache identity and no-silent-download policy. Prefer `local_files_only` for ordinary inference paths. |
| **safetensors** | REUSE / STRONG CANDIDATE | Apache-2.0 | Preferred safe tensor artifact format where model ecosystem supports it | Format safety does not grant model-license or model-trust approval. |
| **Optuna** | REUSE / STRONG CANDIDATE | MIT | Hyperparameter/strategy search for Model Lab and measured classical/ML experiments | Nika Experiment Engine owns dataset split, metrics, trial provenance, champion promotion and rollback. |
| **MLflow** | OPTIONAL WORKER / EXPERIMENT BACKEND | Apache-2.0 | Experiment/tracing/evaluation/model-artifact backend for larger Model Lab or agent-eval projects | Must not become task/ProductProject authority. Local/offline privacy mode preferred; remote tracking requires explicit credential/privacy policy. |
| **DVC** | OPTIONAL WORKER / PROJECT TOOL | Apache-2.0 | Versioned large datasets/models and reproducible data pipelines for large ProductProjects/Model Lab | Nika stores references/version identity, not raw secrets; external remotes require Credential Broker. |
| **ONNX Runtime** | REUSE / ALREADY CATALOGUED | MIT | Compact specialist model inference | Specialist inference only; no general-agent claim. |
| **ONNX Runtime GenAI** | KEEP AS MEASURED FALLBACK | verify current upstream maturity at adoption | Direct generative ONNX path if a concrete measured requirement survives | Existing Nika catalog already treats it as lower-level fallback; do not add merely for backend count. |

Primary sources: https://huggingface.co/docs/huggingface_hub/ , https://github.com/huggingface/safetensors , https://optuna.org/ , https://mlflow.org/ , https://dvc.org/ , https://onnxruntime.ai/ .

## 11. Media, speech and OCR workers

| Component | Decision | License / distribution | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **yt-dlp** | ADAPT / ALREADY OPTIONAL DEPENDENCY | Python project Unlicense; packaged executable distributions can include GPL components | Media-source acquisition adapter | Current Nika already pins Python `yt-dlp`; do not blindly redistribute upstream bundled executable. Obey site terms/auth/cookie policy. |
| **FFmpeg/ffprobe** | OPTIONAL EXTERNAL BINARY | Default LGPL-2.1+ build; GPL/nonfree configure flags can change obligations or redistribution eligibility | Media decode/probe/transcode foundation | Never bundle an arbitrary downloaded FFmpeg binary. Record exact build/config/source/license/notices/checksum. |
| **PyAV** | ADAPT / PROOF | BSD-3-Clause Python binding; underlying FFmpeg obligations remain | Pythonic media decoding when it removes subprocess glue | Does not erase FFmpeg licensing/provenance. |
| **whisper.cpp** | OPTIONAL ASR BACKEND | MIT core; audit exact optional build/features | Efficient local Whisper transcription alternative | Model licenses separate; compare against sherpa-onnx/faster-whisper on Windows/RAM/accuracy. |
| **Silero VAD** | REUSE / PROOF | MIT code/model family; verify exact model artifact | Voice activity detection before ASR to reduce compute | Model identity/license/checksum still recorded. |
| **RapidOCR** | ADAPT / PROOF | Apache-2.0 toolkit; model licenses/artifacts verify | Lighter ONNX/OpenVINO OCR candidate | Benchmark before pulling in heavier PaddleOCR; model files remain optional. |
| **sherpa-onnx / faster-whisper / Tesseract / PaddleOCR / OpenCV** | KEEP EXISTING CATALOG | licenses/model rules already separately tracked | Existing optional speech/OCR/vision families | Select by measured workflow; do not install all engines into base app. |

Primary sources: https://github.com/yt-dlp/yt-dlp , https://ffmpeg.org/legal.html , https://pyav.org/ , https://github.com/ggerganov/whisper.cpp , https://github.com/snakers4/silero-vad , https://github.com/RapidAI/RapidOCR .

## 12. Credential, OAuth and policy engines

| Component | Decision | License | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **Authlib** | ADAPT / HIGH-VALUE PROOF | BSD-3-Clause | Standards-based OAuth 1/2, OIDC and JOSE/JWT for generic connector authentication | Credential Broker owns opaque references, scope/audience/lifetime/revocation and approval. Tokens never become prompts/task state. |
| **MSAL Python** | ADAPT PROVIDER-SPECIFIC | MIT | Microsoft identity adapter for Graph/Azure/Microsoft services | Keep provider-specific objects behind Credential Broker; use least privilege and supported flows. |
| **Google Auth Python** | ADAPT PROVIDER-SPECIFIC | Apache-2.0; upstream repository moved into `google-cloud-python` in 2026 | Google service credential/auth adapter | Use current maintained package/source location rather than archived historical repo. |
| **PyCasbin** | ADAPT / POLICY-MATCH PROOF | Apache-2.0 | Optional ACL/RBAC/ABAC matching engine when it demonstrably shrinks policy matching code | It must never own Nika R0–R4 classification, approval token/effect binding, credential scope or standing-permission authority. |
| **Python keyring** | REUSE / ALREADY CATALOGUED | permissive package ecosystem | OS-backed credential storage abstraction | Physical Windows Credential Manager proof remains a separate acceptance gate. |
| **pywin32** | REUSE WINDOWS PRIMITIVE | permissive BSD-like project terms; verify packaged notices | Native Windows APIs such as Job Objects, DPAPI/security/system integration when higher-level package is insufficient | Prefer to bespoke `ctypes`; still isolate dangerous/native calls behind Nika adapters. |
| **Send2Trash** | REUSE / STRONG CANDIDATE | BSD-3-Clause; Windows | OS-native Recycle Bin for ordinary user-file delete semantics | Nika approval determines whether a delete is allowed and whether hard delete is ever required. |

Primary sources: https://docs.authlib.org/ , https://github.com/AzureAD/microsoft-authentication-library-for-python , https://googleapis.dev/python/google-auth/latest/ , https://github.com/casbin/pycasbin , https://pypi.org/project/keyring/ , https://github.com/mhammond/pywin32 , https://github.com/arsenetar/send2trash .

## 13. Windows interaction, system integration and accessibility

| Component | Decision | License / platform | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **Microsoft UFO²** | KEEP EXISTING ADAPT CANDIDATE | exact current package/engine licensing required at proof | Higher-level Windows computer-use worker | Nika keeps permissions/audit/evidence; semantics/UIA before vision/coordinates. |
| **pywinauto** | KEEP EXISTING FALLBACK | BSD-3-Clause; Windows | Direct UIA/Win32 semantic interaction fallback | Do not use injection-oriented helpers as a normal control path. |
| **FlaUI** | QA / CI INDEPENDENT ORACLE | MIT; .NET Windows | Cross-stack UIA2/UIA3 packaged-app verification | Strong as an auditor harness, not necessarily production runtime adapter. |
| **PyWinRT** | ADAPT / PROOF | MIT; Windows | Projected Windows Runtime APIs for notifications, capture and system integrations | Prefer supported WinRT APIs over custom COM/ctypes when applicable. |
| **Windows Job Objects** | REUSE OS PRIMITIVE | Windows native | Worker process-tree/resource containment | See Software Factory section; Nika records limits/evidence and owns cancellation policy. |
| **Windows-Toasts** | OPTIONAL UX ADAPTER | Apache-2.0 | Native notification helper if direct PyWinRT route is needlessly verbose | Evaluate against direct WinRT to avoid duplicate notification stacks. |
| **Playwright** | REUSE / ALREADY PRIMARY BROWSER | Apache-2.0 | Semantic browser baseline | Role/name/label first; task-owned browser identity and Nika authority remain custom-thin. |
| **axe-core** | QA / CI ONLY | MPL-2.0 | Automated HTML accessibility rules | Human NVDA remains mandatory for `NVDA_VERIFIED`. |

Primary sources: https://github.com/microsoft/UFO , https://pywinauto.readthedocs.io/ , https://github.com/FlaUI/FlaUI , https://github.com/pywinrt/pywinrt , https://learn.microsoft.com/windows/win32/procthread/job-objects , https://playwright.dev/ .

## 14. Observability, diagnostics and localization

| Component | Decision | License / privacy | Nika use | Boundary / caveat |
|---|---|---|---|---|
| **OpenTelemetry Python** | ADAPT / STRONG CANDIDATE | Apache-2.0 | Vendor-neutral sanitized traces/metrics for runtime, Product Factory and execution nodes | Telemetry is optional/local-first and never the Audit Log. Strip prompts/secrets/raw content; remote export requires explicit policy. |
| **Prometheus Python client** | OPTIONAL / NODE METRICS | Apache-2.0 AND BSD-2-Clause in current package metadata | Metrics endpoint for authorized long-running/remote execution nodes | Prefer OTel metrics as shared abstraction unless Prometheus endpoint is actually needed. |
| **Sentry Python SDK** | OPTIONAL / OPT-IN ONLY | MIT SDK; hosted service has privacy/network implications | Crash/error reporting for consenting deployments | Never default for a private local assistant; scrub user content/secrets. Do not assume helper/wizard tooling has the same license. |
| **structlog** | KEEP EXISTING SELECTIVE | MIT OR Apache-2.0 | Structured internal logs when stdlib logging becomes inadequate | Audit events remain separate Nika-owned records. |
| **i18next** | REUSE / STRONG FRONTEND CANDIDATE | MIT | WebView/React string localization, pluralization and language resources | Accessible names/status text must remain deterministic and tested per locale. |
| **Babel** | REUSE / STRONG BACKEND CANDIDATE | BSD-3-Clause | Python locale-aware dates/numbers/messages | Canonical machine timestamps/identities remain locale-neutral; localization is presentation only. |

Primary sources: https://opentelemetry.io/docs/languages/python/ , https://github.com/prometheus/client_python , https://docs.sentry.io/platforms/python/ , https://www.structlog.org/ , https://www.i18next.com/ , https://babel.pocoo.org/ .

## 15. Explicit cautions and rejected-by-default patterns

These findings should prevent accidental future adoption mistakes:

- **Do not equate a catalog entry with dependency approval.** No package belongs in `pyproject.toml` until a real capability need and proof exist.
- **Do not put every optional engine in the base EXE.** Heavy models, OCR/ASR, coding workers, container engines, scanners and experiment servers remain optional components/nodes.
- **Gitleaks Action licensing must be evaluated separately from the open-source CLI.** Do not assume hosted GitHub Action terms from the CLI name alone.
- **yt-dlp executable bundles are not equivalent to the Python package licensing surface.** Current Nika should prefer its pinned Python dependency/isolated worker unless exact bundled notices are accepted.
- **FFmpeg licensing depends on build configuration.** GPL/nonfree flags can materially change distribution obligations; no random binary bundling.
- **FastEmbed/sentence-transformers model licenses are separate from engine code.** Some ecosystem models are non-commercial or otherwise restricted; enforce model allowlists.
- **Docling model/extras licenses are separate from its MIT codebase.** A format parser can be adopted without automatically approving every optional model.
- **selectolax backend choice matters.** Prefer the permissive Lexbor path; do not silently inherit a less-compatible engine.
- **sqlite-vec is pre-1.0.** Treat it as an optional measured proof, not durable schema authority.
- **`ty` is pre-1.0.** Keep it experimental until release-gate stability is proven.
- **Dagger/Podman on Windows imply a Linux/container runtime or VM boundary.** They are execution-node capabilities, not native Windows UI-process dependencies.
- **pygit2 has more license/native packaging complexity than Dulwich/Git CLI.** Non-default.
- **Sentry is a remote privacy surface when SaaS export is used.** Off by default and never an audit datastore.
- **Google Auth's historical standalone repository was archived/moved.** Follow the maintained package/current monorepo source when adopting.
- **pywinauto injection-style helpers are not a normal least-privilege interaction route.** UIA/Win32 semantic APIs remain preferred.
- **Policy engines do not replace approval authority.** Casbin-like matching may assist policy evaluation but cannot mint approval/effect authority.
- **Vector stores and analytics stores are not transactional Nika state.** SQLite remains authoritative for task/runtime/ProductProject identity.
- **MLflow/DVC do not become ProductProject truth.** They are optional experiment/artifact evidence systems.
- **Telemetry is not audit.** OpenTelemetry/Prometheus/Sentry cannot become the security record of truth.

## 16. Recommended graduation order

### 16.1 Directly useful to V0.1 / B08-B09 after ownership checks

These are candidates for focused proof or CI/release use because they reduce risk without opening future-product scope:

1. **CycloneDX Python tooling** — strengthen exact-package SBOM generation.
2. **Trivy or Syft** — independent package/filesystem/SBOM validation, selected with one focused proof rather than both by default.
3. **Bandit** — test-only Python security scan if it finds material classes not covered by current gates.
4. **Hypothesis** — property/state-machine QA for current durability/concurrency boundaries where deterministic example tests have gaps.
5. **RESPX** — normalize HTTPX fault-injection tests for ModelGateway/Research adapters.
6. **axe-core Playwright integration** — supplement the current semantic WebView/browser accessibility tests.
7. **FlaUI** — independent packaged Windows UIA oracle after the final V0.1 control path is stable.
8. **python-tuf** — architecture/proof for the updater trust layer, but do not block the first portable package if V0.1 update scope does not require it.
9. **MSIX/App Installer** — package-channel proof after the exact PyInstaller payload is stable; do not replace the current package path mid-release without measured benefit.

These additions still require separate CLAIM/owner assignment before any workflow/dependency/source mutation.

### 16.2 Post-V0.1 high-value proofs, in dependency order

1. **uv + Nox** for Software Factory environments/test sessions.
2. **Authlib** for generic Credential Broker OAuth/OIDC.
3. **Docling** for rich Research/Corpus normalization; measure MarkItDown/Trafilatura as lighter routes.
4. **Hishel + aiolimiter** for Research/provider HTTP efficiency and bounded source use.
5. **DuckDB** for analytical workspaces and Model Lab data.
6. **sqlite-vec + FastEmbed** as the smallest local semantic retrieval option; compare to existing Qdrant direction.
7. **huggingface_hub + safetensors** for controlled model artifact acquisition/cache.
8. **Optuna**, then optional **MLflow/DVC**, for Model Lab experimentation at larger scale.
9. **OpenTelemetry** for sanitized diagnostics across Product Factory/execution nodes.
10. **Dagger/Podman** only for authorized container/remote execution nodes that actually need them.

## 17. Reuse choices that should remain as already selected

This audit found no reason to replace the existing canonical choices merely because alternatives exist:

- LangGraph remains the primary `AgentRuntimePort` implementation; do not add CrewAI/Agno/another kernel for feature-count optics.
- APScheduler remains behind `SchedulerPort`; no new scheduler framework.
- SQLite remains authoritative local state; DuckDB/vector stores are secondary analytical/retrieval engines.
- HTTPX remains the direct HTTP transport baseline.
- Playwright remains the semantic browser baseline.
- pywebview/WebView2 remains the Windows UI host direction for the current product.
- PyInstaller remains the current freeze/build path until a measured package transition justifies change.
- Unified Planning remains the deterministic formal planner adapter direction.
- Ollama/Foundry/OpenAI-compatible routes remain behind ModelGateway; no provider-specific orchestration kernel.
- OpenHands remains the first CodingWorker proof candidate; `uv`/Nox/Dagger are supporting execution/build tools, not replacements for `CodingWorkerPort`.

## 18. Research-source and provenance rule

Before graduation, the owner records at minimum:

- canonical upstream repository/documentation URL;
- exact version/commit used in the proof;
- code license and any NOTICE obligations;
- **separate model/data/binary license** where applicable;
- release date/maintenance evidence;
- supported Python/Windows/platform surface;
- binary/runtime download behavior and checksum/source;
- transitive/native dependencies that affect packaging;
- measured Nika capability benefit versus the existing component;
- privacy/network/credential implications;
- exact tests and rollback/removal path.

A component may be excellent upstream and still be rejected if it widens Nika permissions, creates a second authority, forces a heavy base dependency, cannot be packaged reliably on Windows, or does not improve a measured Nika requirement.

## 19. Net result

The final Nika should be a **small Nika-owned control plane around maintained engines**, not a repository that reimplements package management, updater cryptography, SBOMs, OAuth/OIDC, document layout extraction, feed/domain/date parsing, analytics SQL, vector indexing, embedding inference, hyperparameter search, property testing, accessibility rules, process-tree containment, telemetry protocols or localization frameworks.

The durable/security/accessibility/product semantics remain intentionally custom-thin because those semantics define Nika itself. Everything else should repeatedly face the question: **can a maintained component own this commodity mechanism while Nika keeps policy and identity?**
