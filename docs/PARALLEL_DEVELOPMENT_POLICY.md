# Parallel development policy

Authority: [autonomous delivery](AUTONOMOUS_WORKER_ORCHESTRATION.md), DELIVERY-2026-09-09.

Choose independent packages that can be completed and integrated. Start with six active implementation packages across 13 developers and long Codex runs. Assign remaining capacity to disjoint parts of existing packages, ready-candidate review and concrete integration defects. Increase WIP only when throughput and queue age justify it.

Each package has one source owner, explicit scope, one independent reviewer, a canonical PR and a user-visible acceptance result. Dependencies constrain integration order, not unrelated progress. Honor current ownership and recorded handoffs.

One unfinished implementation PR per owner. Finish and integrate it before creating successors. Routine QA_ONLY branches and repeated unchanged audits are not useful parallelism. Keep security, accessibility, recovery and provenance gates.

The old 98%-complete/global human-gate waiting instruction is superseded. Freeze the exact candidate for human Windows/NVDA checking; continue independent authorized work toward the full product. Automation never awards HUMAN_TESTED or NVDA_VERIFIED.
