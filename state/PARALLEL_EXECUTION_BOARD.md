# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-19
Mode: M12 PRE-HUMAN RELEASE FREEZE
Canonical progress evidence: merged PR + exact green CI + milestone acceptance evidence.

## Current product truth
Overall acceptance-gate proven progress: **98.0%**.

M0–M11 are GREEN / INTEGRATED. M11 additionally has automated PACKAGED candidate evidence. M12 automated pre-human QA is GREEN / INTEGRATED; only the human Windows/NVDA gate remains open.

Human truth:
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false.
- PRODUCTION_RELEASE_READY: false.

## Current exact human-bound candidate
The previous M12 artifact based on `d7bdfd697819adf13ad7423726a004fd781d857d` is **SUPERSEDED** because a release audit proved incomplete third-party license/notices evidence.

Current candidate:
- exact candidate head: `843d81b300fdfd031568e981cef600fbd0f8defa`;
- Core CI #225: SUCCESS;
- M11 Windows Release Candidate run #7: SUCCESS;
- M12 Pre-Human Release Gate run #5: SUCCESS;
- repair PR #38 merged as `8065cc3fedb63f9c07e1773acf2332b5709560da`;
- artifact: `NikaCore-0.0.2-m12-prehuman-843d81b300fdfd031568e981cef600fbd0f8defa`;
- artifact id: `9355471771`;
- size: `20,903,072` bytes;
- digest: `sha256:64c99f62bdde1eafe89074893e3c3ef97adbdba1d815c77e4286193d0376146c`;
- observed expiry: 2026-11-17.

## Release engineering lane
Current state: IMPLEMENTED / GREEN / INTEGRATED / PACKAGED candidate evidence.

Latest repaired evidence:
- deterministic third-party notices are generated before manifest creation;
- Python runtime and packaged runtime dependency license evidence are included;
- missing distribution/license evidence fails the build closed;
- `THIRD_PARTY_NOTICES.txt` is itself SHA-256 covered by the release manifest;
- M11 and M12 artifacts expose notices directly;
- first repair run caught a verifier normalization defect and stopped before packaging credit;
- repaired exact head then passed Core CI, M11 Windows package proof and complete M12 pre-human gate.

## Release-freeze operating rule
Normal feature expansion remains paused while human acceptance is bound to the exact candidate above.

Safe autonomous work is limited to:
1. artifact/evidence integrity checks;
2. investigation of concrete release/accessibility defects;
3. protocol clarification;
4. one coherent repair wave when a real defect is proven.

Do **not** rebuild or replace the candidate merely to keep automation busy. Do **not** create status-only PR chains whose sole purpose is recording previous status-only merges.

If human testing finds a defect, create a new development candidate, fix the complete same-root-cause family, rerun Core CI + Windows package proof + the full M12 gate, and bind human acceptance only to that exact new artifact.

## Collision policy
- Production source changes require a concrete defect during release freeze.
- Shared contracts require explicit compatibility review and targeted tests.
- Automation never self-awards HUMAN_TESTED or NVDA_VERIFIED.
- No secrets, tokens, sessions, cookies, browser profiles or private logs enter Git or release artifacts.
