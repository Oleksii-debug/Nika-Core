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


## Plan 1 — checkpoint 2026-10-08 (nonterminal)
- **Section 1:** ACTIONABLE / IN_PROGRESS / NOT DONE. Existing stacked architecture/adoption lineage #1734 -> #1746 -> #1762 -> #1777 -> #1786 -> #1791, plus narrow defaulted-`getattr` import/evaluator guard PR #1804. Exact PR #1804 head `b3eb4ddf3e7ca258b13696c6a33a7ec1158d8867`; source blob `2603783ef904bbae6c97d3ce5fec71c71758cbe7`, read back from GitHub. New cases are authored, NOT executed/PASS. Exact-head Core CI #37832363833 and M12 #37832363614 were QUEUED; PF3 #37832363889 SKIPPED. Dependency provenance/resolution, full acceptance, safe ordered integration and postmerge main readback remain unproved.
- **Section 2:** ACTIONABLE / IN_PROGRESS / NOT DONE. Existing stacked runtime contract lineage #1738 -> #1748 -> #1763 -> #1778 -> #1794; latest readback head `7b586c259f835d1ba818f09287e27bf3528ae6dc`. Core CI #37829667745 and M12 #37829667780 were QUEUED, not PASS. Cross-domain versioned command/query contracts, negative/recovery integration, Section 1 predecessor and main merge/readback remain unproved.
- No terminal DONE, no fabricated Linux/Windows/NVDA qualification. Other plans' statuses remain unchanged.

## Plan 1 — follow-up implementation checkpoint 2026-10-08 (nonterminal)
- **Section 1:** ACTIONABLE / IN_PROGRESS / NOT DONE. Reused existing Section 1 source and PR stack through #1804. Child draft PR #1807, head `2716ef2d41839a6970405177a1094f201d0fb4f5`, adds manifest-level optional-engine/bounded-dependency regressions (`tests/test_plan1_dependency_adoption.py`, blob `7c5285afb698e0857e83e6008d3422cc5206c873`). Core CI #37833205365 and M12 #37833205358 QUEUED at readback, not PASS.
- **Section 2:** ACTIONABLE / IN_PROGRESS / NOT DONE. Reused existing Section 2 source/stack through #1794. Child draft PR #1808, updated head `cc1b0d5b0b02f282a2cded5eec3c99bcfc0f5144`, validates runtime request/event/result container types before durable use; source blob `86dc3c632636bd15761a49994399cc742e3cfa9b`, tests blob `2fb999551198dab86e0a7f9b1d92c078f67d72f7`. Core CI #37833296819 and M12 #37833296843 QUEUED at readback, not PASS.
- No terminal DONE or main integration. Exact-head dual-OS tests, dependency provenance/architecture evidence, end-to-end contracts/restart/security acceptance, safe ordered integration and postmerge readback remain gates. Other plans are unchanged.
