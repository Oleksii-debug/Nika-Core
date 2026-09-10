# Diagnostics / Health core service

Status: production candidate subsystem slice. This document does not claim packaged UI integration,
human accessibility verification, NVDA verification, or release readiness.

## Purpose

`Diagnostics/Health` is Nika's deterministic, read-only core health service. It answers whether the
current process has trustworthy local configuration/state/resource evidence and exposes normalized
subsystem health without making provider-controlled diagnostics part of the public contract.

It is not a monitoring SaaS, telemetry pipeline, logging framework, model-driven troubleshooter, or
replacement for subsystem-specific security/audit evidence.

## Public surface

Python API:

- `HealthService.run() -> HealthReport`
- `HealthStatus`: `pass`, `warn`, `fail`
- stable machine schema: `nika-health-report:v1`
- linear plain-text report suitable for keyboard/screen-reader workflows
- `ModelHealthSnapshot` for provider-neutral local-model facts
- `OllamaModelHealthProbe` as a metadata-only adapter into that contract

CLI:

```text
python -m nika_core.diagnostics
python -m nika_core.diagnostics --json
```

Exit codes are deterministic: `0=PASS`, `1=WARN`, `2=FAIL`.

## Checks and authority boundaries

### Configuration

The service confirms that typed `AppConfig` loaded and that its supported configuration schema and
application version are usable. It deliberately does **not** echo `model_provider` or raw validation
errors: provider identifiers and environment values are caller-controlled strings and may contain
credential material.

### SQLite

Health opens the configured SQLite database using URI `mode=ro` and `PRAGMA query_only=ON`.
It never calls `SQLiteStore.initialize()`, never creates a missing database, and never runs a
migration. Checks include database integrity, foreign keys, and exact supported migration history.

### Resources

The service reuses the existing `ResourceObserverPort` and `ResourceSnapshot` contracts. If no
observer is installed, health returns `WARN` and makes no load claim. Invalid or non-finite
measurements fail closed.

### Local model health

Local model health is explicitly decomposed into five independent facts:

1. `configured`: the selected local model target has usable explicit configuration.
2. `reachable`: the local provider answered a lightweight metadata request.
3. `model_present`: the exact selected model identity appears in the provider catalog.
4. `model_ready`: the provider supplies positive runtime-readiness evidence for that exact model.
5. `inference_proven`: separate trusted evidence says that exact provider/model previously completed
   inference successfully.

Each fact is `yes`, `no`, or `unknown`. The dependency invariant is fail-closed: `model_ready=yes`
requires `configured=yes`, `reachable=yes`, and `model_present=yes`. A server/TCP response therefore
cannot by itself become model readiness.

The Ollama adapter uses only metadata endpoints: model catalog (`/api/tags`) and running-model
inventory (`/api/ps`). It disables redirect following, performs no chat/generate call, never pulls a
model, and never turns an absent or merely installed model into READY. A model that is installed but
not reported as running has `model_ready=unknown`, because absence from the running inventory is not
proof that it cannot be loaded.

`inference_proven` is deliberately separate from readiness. Health itself never runs an inference to
obtain that proof. A caller may supply a `ModelInferenceEvidencePort`; without trusted prior evidence,
the result remains `unknown`. This keeps health cheap and prevents diagnostics from becoming an
implicit benchmark, model download, or expensive warm-up path.

`ModelHealthSnapshot.as_dict()` contains only Nika-owned fact names/states. It contains no URL, HTTP
payload, SDK object, provider diagnostics, or model content. `to_health_check()` maps the snapshot
back into the canonical Diagnostics/Health severity surface: missing configuration/reachability/model
is FAIL; partial/unknown readiness or proof is WARN; all five proven facts is PASS.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** canonical `AppConfig`; SQLite schema constants; existing `HealthCheck`/`HealthStatus`;
  existing `ResourceObserverPort`; installed HTTPX transport.
- **ADAPT:** local provider metadata into a five-fact provider-neutral `ModelHealthSnapshot`.
- **CUSTOM(thin):** fact invariants, bounded severity projection, metadata-only Ollama probe, and
  deterministic evidence-port seam.

No new dependency, database/schema, scheduler, inference call, model acquisition, telemetry backend,
approval surface, permission expansion, or second health/resource framework is introduced.

## Accessibility boundary

Plain-text output is intentionally linear and does not depend on color, layout, mouse, or visual-only
state. This is source-level accessibility design only.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

UI/WebView2 exposure remains a separate integration concern. Model Engineering/UI can consume the
same structured `ModelHealthSnapshot` without receiving provider-specific payloads.

## Acceptance for this slice

The candidate is not accepted merely because source exists. Exact candidate qualification requires
dependency consistency, Ruff, compile/import checks, focused diagnostics/local-model health tests,
full Core CI on Ubuntu and Windows, complete applicable M12 pre-human gate, and a current-main /
ownership / mergeability reread before guarded integration.

Automated tests never set `HUMAN_TESTED` or `NVDA_VERIFIED`.
