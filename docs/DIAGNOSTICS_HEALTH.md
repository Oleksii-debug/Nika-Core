# Diagnostics / Health core service

Status: production candidate subsystem slice. This document does not claim packaged UI integration,
human accessibility verification, NVDA verification, or release readiness.

## Purpose

`Diagnostics/Health` is a deterministic, read-only core service required by `docs/MASTER_SPEC.md`.
It answers a narrow operational question: **can this Nika Core process safely trust its current local
configuration, canonical SQLite state, and available resource observation well enough to continue?**

It is not a monitoring SaaS, telemetry pipeline, logging framework, model-driven troubleshooter, or
replacement for subsystem-specific security/audit evidence.

## Public surface

Python API:

- `HealthService.run() -> HealthReport`
- `HealthStatus`: `pass`, `warn`, `fail`
- stable machine schema: `nika-health-report:v1`
- linear plain-text report suitable for keyboard/screen-reader workflows

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
migration. Checks include:

- database file present and regular;
- `PRAGMA quick_check`;
- `PRAGMA foreign_key_check`;
- exact contiguous integer core migration history through current `SCHEMA_VERSION`;
- exact contiguous integer ProductProject migration history through current
  `PRODUCT_PROJECT_SCHEMA_VERSION`.

Missing, malformed, non-integer, non-contiguous, or future migration history fails closed.

### Resources

The service reuses the existing `ResourceObserverPort` and `ResourceSnapshot` contracts. If no
observer is installed, health returns `WARN` and makes no load claim. The CLI lazily reuses
`PsutilResourceObserver` when the optional psutil component is available. Invalid, boolean,
out-of-range, or non-finite measurements fail closed. Provider exceptions are reduced to a stable
warning without exposing raw exception text.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** canonical `AppConfig`; SQLite schema version constants; SQLite read-only URI/PRAGMA
  primitives; existing `ResourceObserverPort`; existing optional `PsutilResourceObserver`.
- **ADAPT:** expose these canonical surfaces as one bounded health snapshot with stable severity and
  serialization semantics.
- **CUSTOM(thin):** Nika-specific aggregation, fail-closed migration validation, exit-code mapping,
  secret-minimized summaries, and accessible linear text only.

No new dependency, database/schema, scheduler, model call, network call, telemetry backend, approval
surface, permission expansion, or second resource manager is introduced.

## Accessibility boundary

Plain-text output is intentionally linear, numbered, and does not depend on color, layout, mouse,
or visual-only state. This is source-level accessibility design only.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

UI/WebView2 exposure is deferred because those production files have separate active ownership. A
future integration lane may surface the same `HealthReport` through standard semantic controls
without changing this core authority contract.

## Acceptance for this slice

The candidate is not accepted merely because source exists. Exact candidate qualification requires:

1. dependency consistency;
2. Ruff;
3. Python compile/import checks;
4. focused health regressions, including Unicode/spaced paths and secret canaries;
5. full Core CI on Ubuntu and Windows;
6. complete applicable M12 pre-human gate;
7. current-main/ownership/mergeability reread immediately before any guarded integration.

Automated tests never set `HUMAN_TESTED` or `NVDA_VERIFIED`.
