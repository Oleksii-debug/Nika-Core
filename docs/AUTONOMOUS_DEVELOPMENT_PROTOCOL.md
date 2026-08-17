# Autonomous hourly development protocol

Repository: Oleksii-debug/Nika-Core

## Read first every cycle
AGENTS.md; docs/MASTER_SPEC.md; docs/ROADMAP.md; docs/THIRD_PARTY_ADOPTION.md; docs/ACCEPTANCE_GATES.md; state/PROJECT_STATUS.md; LIVE DASHBOARD and latest comments; open PRs and exact CI.

## Cycle
1. Refresh GitHub state; chat memory is not source of truth.
2. Identify current milestone and highest-value unmet acceptance gate.
3. Before subsystem code, search maintained upstream/official docs and classify REUSE/ADAPT/CUSTOM.
4. Select the largest safe coherent batch that can be completed and verified.
5. Use a dedicated branch; do not force-push main.
6. Add/update tests and docs in the same batch.
7. Run cheap tests first. Spend Windows runner minutes only for Windows-specific or milestone gates.
8. Commit/push exact coherent changes and open/update PR when appropriate.
9. Update canonical status with branch/SHA, implemented vs integrated vs packaged vs human-tested, evidence, blocker, weighted progress and next batch.
10. If regression exists, restore the last green contract before future work.

Development normally runs from Python. Build standalone EXE/ZIP only for milestone/user/release candidates or when packaging itself is the current gate. Final users must not need Python.

Autonomy limits: no destructive external action without policy; no real-money action; no secrets in repo; no bypass of CI/release gates; no direct runtime self-modification of production source. Self-code proposals go through isolated branch/sandbox + tests + integration/release gates.
