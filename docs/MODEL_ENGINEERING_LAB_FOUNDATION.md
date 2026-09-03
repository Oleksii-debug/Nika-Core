# Model Engineering Lab foundation

Status: implementation candidate on the isolated `MODEL-LAB-01` lane. It is not integrated, packaged, HUMAN_TESTED, NVDA_VERIFIED, or release-ready until exact branch-head CI and later workspace/UI integration gates are green.

## Product purpose

The Full Product Vision requires a real Model Engineering Lab that can compare replaceable intelligence components on versioned task/evaluation sets and promote only from explicit held-out/replay evidence. This foundation implements the deterministic comparison/promotion core without creating another model gateway, experiment framework, resource monitor, or database schema.

The lab compares one champion against one or more challengers through the existing `ModelGateway`. Each evaluation case is executed with an exact provider/model selection and `temperature=0.0`. A caller-supplied deterministic quality scorer produces the primary quality metric and must declare a stable `scorer_id` plus `scorer_version`. The built-in `ExactMatchScorer` uses `exact_match@1` and supports model-free acceptance tests without another LLM dependency.

## REUSE -> ADAPT -> CUSTOM decision

- REUSE `ModelGateway` for provider selection, privacy routing, timeout/cancellation behavior, auditing and inference.
- REUSE M8 `ExperimentEngine`, `ExperimentDefinition`, `PromotionPolicy`, replay identity and `ExperimentRepository` for immutable definitions, append-only evidence, deterministic champion/challenger selection and durable SQLite recovery.
- REUSE `ResourceObserverPort` when host CPU/RAM pressure guardrails are explicitly requested.
- ADAPT M8 repository `save()` as one atomic observation-group append. The public M8 `record()` API records one metric at a time, which would allow a crash between quality and latency observations. Model Lab therefore appends all metrics from one inference as one repository save without modifying the actively-owned M8 package.
- CUSTOM (thin) only the model candidate identity, evaluation-suite digest, model request orchestration, response-identity checks, benchmark summaries and model-specific policy mapping.

No new dependency and no new SQLite migration are introduced.

## Evidence and durability model

An `EvaluationSuite` binds:

- dataset reference;
- dataset version;
- held-out or replay split;
- privacy class;
- ordered case IDs;
- message roles/content;
- expected text.

The canonical case payload is SHA-256 hashed. The durable M8 replay version additionally binds the benchmark request timeout and stable quality-scorer ID/version. A restart with the same friendly dataset ref/version but changed evaluation content, privacy route, timeout, or scorer version therefore fails closed as a definition mismatch instead of mixing incomparable evidence.

For every candidate/case, Model Lab commits one atomic group containing:

- `quality`;
- host-measured `latency_ms`;
- optional `host_cpu_percent`;
- optional `host_memory_percent`.

Raw model response text and raw evaluation messages are not added to M8 observations or benchmark reports. The experiment definition persists candidate/provider/model identity and dataset ref/version/digest/provenance fields. Persisted candidate and dataset identities reject URL userinfo, query strings, fragments and control-line characters so credential-bearing locators are not accepted as durable benchmark identity.

A crash after a committed case group can resume from the existing M8 experiment. Complete groups are skipped. A partial group is treated as integrity failure rather than re-running a model and mixing metrics from two different responses. SQLite M8 persistence makes the group append transactional; a process recreation can continue from the same database.

## Promotion semantics and safety boundary

M8 remains the promotion authority. Quality is the primary metric and higher is better. Host-measured latency is always a lower-is-better guardrail; callers explicitly choose the maximum permitted latency regression. Optional host CPU/RAM guardrails are also lower-is-better.

A Model Lab `PROMOTED` result means only that the M8 experiment selected the challenger. It does **not** rewrite production configuration, download a model, switch the active provider, expand permissions, mutate production source, or bypass R0-R4 approval. Activation of a selected model is a separate future integration action and must use the existing approval/registry authority.

Candidate permission fingerprints must be identical. This reuses the M8 rule that an experiment cannot widen or alter permissions.

The response must return the exact benchmark request ID, provider ID and model ID. A provider alias that resolves to a different returned model identity is rejected. For reproducible promotion evidence, callers should benchmark immutable provider/model identifiers rather than mutable aliases such as `latest`.

## Privacy and accessibility

Evaluation privacy is passed unchanged into `ModelRequest` and is also bound into durable benchmark identity; therefore a resumed experiment cannot silently reroute remaining cases under a different privacy classification. Existing ModelGateway privacy routing remains authoritative. Model Lab itself has no credential API and no model-download API.

This slice has no new UI. It does not claim HUMAN_TESTED or NVDA_VERIFIED. A later workspace integration must be coordinated with the active shared UI/workspace owner and must expose semantic keyboard/NVDA controls rather than a parallel custom shell.

## Known boundary: resource footprint

Current `ResourceObserverPort` exposes host CPU percent, host memory percent and available memory. It does not expose reliable per-process CPU/RAM attribution or GPU/VRAM footprint. Consequently this foundation labels its resource metrics `host_*` and does not claim the Full Product Vision GPU/process-footprint requirement as complete. That requires a separate compatibility decision and supported telemetry port rather than invented numbers.

## Acceptance evidence required before integration

1. dependency consistency remains unchanged because no dependency is added;
2. Ruff, compile and full pytest pass on the exact branch head, including Windows Verify when triggered by Core CI;
3. deterministic promotion and latency-regression denial pass;
4. response identity substitution fails closed;
5. dataset content, privacy, timeout, or scorer-version mutation on resume fails closed;
6. unversioned scorer and permission fingerprint drift fail before inference;
7. credential-bearing persisted identity locators are rejected;
8. crash/restart resumes without replaying committed case groups;
9. SQLite path with spaces/Unicode passes restart evidence;
10. optional host-resource guardrails require an observer and remain explicitly host-level;
11. no raw model output is persisted as experiment observation/report evidence;
12. exact SHA and Actions evidence are recorded before integration;
13. packaged workspace/NVDA credit remains false until later human/product-journey integration.
