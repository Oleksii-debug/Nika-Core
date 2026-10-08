# Nika Core — Multi-Plan Closure State

## Rules
- PROJECT_PLAN_INDEX.md + MULTI_PLAN_PARALLELISM_CONTRACT.md + this file + the assigned Drive plan are the live work-selection authority.
- Plans 1–6 have no global earliest Section.
- Inside an assigned plan, skip terminal DONE and work the first ACTIONABLE unfinished Section.
- Every migrated Section starts AUDIT_REQUIRED / PARTIAL_EXISTING unless later live evidence closes it; existing Nika implementation is an asset, not greenfield.
- Plan 7 is WAITING_UPSTREAM and must not be used as a normal lane before required Plan 1–6 outputs exist.
- DONE is terminal under the repository closure semantics; reopen only for demonstrated regression, invalid evidence, materially changed acceptance contract or breaking integration.
- Human/NVDA/physical/real-cloud whole-product evidence belongs to Plan 7, not intermediate component blockers.

## Initial fronts
| Plan | Sections | Initial state |
|---:|---:|---|
|1|10|AUDIT_REQUIRED / PARTIAL_EXISTING|
|2|10|AUDIT_REQUIRED / PARTIAL_EXISTING|
|3|8|AUDIT_REQUIRED / PARTIAL_EXISTING|
|4|8|AUDIT_REQUIRED / PARTIAL_EXISTING|
|5|3|AUDIT_REQUIRED / PARTIAL_EXISTING|
|6|6|AUDIT_REQUIRED / PARTIAL_EXISTING|
|7|4|WAITING_UPSTREAM|

Update this registry whenever a new-plan Section reaches DONE/REOPENED or when a live audit proves an earlier migrated Section terminal.
