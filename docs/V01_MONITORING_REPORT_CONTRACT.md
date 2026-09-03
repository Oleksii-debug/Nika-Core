# V0.1 Monitoring Report Contract

Status: Worker 43 candidate for `V01-B05` with a `V01-B01` UI dependency.

Starting main: `9dd4013625979492a125080f32e307fd5d808d48`.

## Purpose

A blind user must be able to answer, from one compact text result, what Nika checked over time, whether anything materially changed, whether the declared condition matched, whether a retry/error occurred, what is scheduled next, and why monitoring stopped.

This slice is deliberately a **reporting projection**, not a scheduler or monitoring workflow engine. It does not persist or mutate deadline, condition, pause, cancel, retry, or next-run authority.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE `ResearchEvidence`, `RefreshDisposition`, recurring `ResearchProfileDelta`, durable task/result identities, and the existing `UIResult.message` text channel.
- ADAPT recurring Research delta items into compact change references without copying snippets or bodies.
- CUSTOM (thin) only for an immutable monitoring-report projection, consistency guards, safe plain-text rendering and focused tests.

No new dependency, database table, scheduler, workflow engine or UI model is introduced.

## Backend contract

`nika_core.research.monitoring_report` exposes:

- `MonitoringSourceCheck`: source identity, source kind, final disposition, attempt count/retry count, error code, optional snapshot reference;
- `MonitoringChange`: normalized change kind/document/title plus existing `ResearchEvidence` provenance;
- `MonitoringCheck`: one chronological check/cycle with explicit `condition_matched` and result-set lineage;
- `MonitoringReport`: history plus the canonical controller's `next_scheduled_check`, `terminal_reason` and optional durable state reference;
- `changes_from_profile_delta()`: adapter over the existing recurring Research delta;
- `render_monitoring_report_text()`: compact text suitable for the existing `UIResult.message` transport.

The reporting contract never becomes canonical monitor state. Worker 42/runtime must supply the authoritative condition state, next scheduled timestamp and terminal reason. A terminal report cannot also advertise a future check. A report whose most recent condition is matched cannot advertise a future check.

## Safety and privacy

The report intentionally does **not** contain:

- response bodies or raw HTML;
- HTTP request/final URLs;
- headers;
- cookies;
- credential handles or values;
- free-form fetch exception/error messages.

Source reporting uses stable `source_id`/kind. Errors use bounded error codes. Provenance renders the existing evidence source identity, kind, observed time and freshness, but deliberately omits `ResearchEvidence.locator` because an HTTP locator can contain query credentials or signed parameters.

Change titles are plain bounded labels only. Angle brackets are neutralized and common secret-assignment forms are redacted. Research snippets/body content are never copied into the projection.

## Accessibility structure

The renderer is plain UTF-8 text with deterministic headings and labels:

1. monitor summary;
2. current condition state;
3. next scheduled check;
4. terminal reason;
5. chronological `Check N` sections;
6. source outcomes with retry/error state;
7. `What changed` and provenance references.

The renderer defaults to the latest 20 checks and states how many earlier checks were omitted. This prevents an unbounded live announcement while preserving the structured full history for callers that need another page/window.

Automated semantic/text tests are not human screen-reader acceptance. `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` remain mandatory until an actual NVDA run.

## P10-07 UI dependency

Worker 43 does not edit `src/nika_core/ui/**` because B01 is actively owned by P10-07/ENG06/UIA lanes. P10-07 should consume `render_monitoring_report_text(report)` through the existing supported task/result bridge, expose it as keyboard-readable status/result text, and avoid stealing focus or repeatedly announcing the entire history on each refresh. A concise changed/terminal summary may be a polite live update; the full report should remain statically navigable text.

Packaged UI/UIA and human NVDA evidence remain P10-07 acceptance responsibilities.
