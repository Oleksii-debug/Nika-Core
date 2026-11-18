# Autonomous development cycle

Repository: Oleksii-debug/Nika-Core. Policy: DELIVERY-2026-09-09.

1. Read AGENTS.md, docs/AUTONOMOUS_WORKER_ORCHESTRATION.md, relevant #553 records and changed specifications for the assignment. Refresh actual PR/head/base/checks; avoid rereading the historical corpus hourly.
2. Verify worker/run identity, real execution capability and exclusive assigned source ownership. Reuse existing canonical PRs and maintained implementations. Justify REUSE/ADAPT/CUSTOM for a new subsystem.
3. Select the next usable Windows or actual development-factory outcome. Prefer finishing an existing blocker; respect WIP and dependencies.
4. Complete a coherent package through source, actual application wiring, relevant recovery/error behavior, necessary checks, PR update and durable checkpoint.
5. Keep meaningful regression/acceptance checks for changed risks. Do not mirror implementation with tests or repeat unchanged audits to generate activity. Existing gates remain until a verified replacement is adopted.
6. Report at most eight lines with result, role/run, PR/SHA, evidence/state, blocker and next owner/step. Avoid no-change public reports.

Development may use Python; end users must not need it. A standalone executable is necessary but not sufficient for product acceptance.

No secrets in prompts/repo/logs, permission expansion, direct runtime self-modification of production, failed-candidate promotion, or destructive external/money action outside existing authorization. Generated code retains isolation and independent review. Human NVDA evidence cannot be automated into existence.
