# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: `Oleksii-debug/Nika-Core`
Development mode: M12 PRE-HUMAN RELEASE FREEZE
Repository visibility observed this cycle: **PUBLIC**.

## Weighted progress

| Milestone | Evidence state | Credit |
|---|---|---:|
| M0 research/reuse/governance/bootstrap | GREEN / INTEGRATED | 6% / 6% |
| M1 kernel foundation | GREEN / INTEGRATED | 10% / 10% |
| M2 durable agent runtime | GREEN / INTEGRATED | 11% / 11% |
| M3 memory/scheduler/resource control | GREEN / INTEGRATED | 9% / 9% |
| M4 model gateway/tools/MCP | GREEN / INTEGRATED | 8% / 8% |
| M5 accessible Windows GUI | GREEN / INTEGRATED | 11% / 11% |
| M6 Agent Builder & permissions | GREEN / INTEGRATED | 8% / 8% |
| M7 multi-agent laboratory | GREEN / INTEGRATED | 9% / 9% |
| M8 self-learning & experiment engine | GREEN / INTEGRATED | 10% / 10% |
| M9 Plugin SDK & real workspaces | GREEN / INTEGRATED | 8% / 8% |
| M10 security/sandbox/reliability | GREEN / INTEGRATED | 5% / 5% |
| M11 Windows packaging/distribution | GREEN / INTEGRATED / PACKAGED candidate evidence | 3% / 3% |
| M12 full-system QA/NVDA/release | AUTOMATED PRE-HUMAN GATE GREEN / INTEGRATED; HUMAN GATE OPEN | 0% / 2% |

**Overall acceptance-gate proven progress: 98.0%.**

## Exact milestone evidence
- M1 green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI 74; merge `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI 85; merge `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI 98 Ubuntu+Windows; PR #8 merge `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI 112 Ubuntu+Windows+live Ollama; PR #10 merge `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI 137 Ubuntu+Windows+packaged WebView2/UIA proof; PR #13 merge `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
- M6 green head `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI 142 Ubuntu+Windows; PR #15 merge `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
- M7 green head `4feb976faa97949bacc321bcbce792d01359a58c`; Core CI 150 Ubuntu+Windows; PR #17 merge `5a01692c1372375f040cd38558e33204b082d5a5`.
- M8 green head `d8f14ac097f2eb664b7f893543db359fb6245e6c`; Core CI 159 Ubuntu+Windows; PR #19 merge `a8fd7b6c094489cebb79a4221e4124b5cfbad8f6`.
- M9 foundation green head `ef4bafc02d4517b3718f07e5f61b7e4e22072c25`; Core CI 188; PR #21 merge `e8967b97ec9772cb2af0b3b781468553708d4015`.
- M9 semantic-adapter green head `680a8a15dfb5a30a7c6eaba3860817c1cbda7ac5`; Core CI 193 Ubuntu+Windows plus live Playwright ARIA and Windows UIA proofs; PR #24 merge `e6af748fb655fda2719be93258b6c2a427287994`.
- M10 green head `17826cc041caeaa1fe03f8dbe4ce5e50c7aecad5`; Core CI 167 Ubuntu+Windows; PR #22 merge `976cb9914966a67763742a28c229730417ee63c8`.
- M11 green head `0f48c3c4889478f967e383d385576e9322e55c9b`; Core CI 196; Windows Release Candidate run 3; PR #23 merge `5d86d83f6ce99fa4c9bd6a0eaadef1a6b404283c`.
- M12 automated pre-human green head `d7bdfd697819adf13ad7423726a004fd781d857d`; Core CI 200; M12 Pre-Human Release Gate run 1; PR #26 merge `a278767de4435535cc54e5674ddc72d37051ba03`.

## M12 exact human candidate
- Artifact: `NikaCore-0.0.2-m12-prehuman-d7bdfd697819adf13ad7423726a004fd781d857d`.
- Artifact id: `9340947647`.
- Size: `20,846,871` bytes.
- Workflow digest: `sha256:ee0ec0b7506c6bc84b8052f1c1cdb80a33a6f0dea8eb6b221cee1860dc96d4c5`.
- Automated Ubuntu evidence: complete verification, 152 tests, focused 58-test recovery/safety matrix, live Playwright semantic proof.
- Automated Windows evidence: complete verification, same focused matrix, real Windows UIA semantic proof.
- Packaged Windows evidence: PyInstaller build, deterministic manifest verification, WebView2/UIA discovery, Action Registry keyboard/focus proof and release-gate truth regressions.
- Human protocol: `docs/M12_HUMAN_NVDA_ACCEPTANCE.md`.

## Release truth
- M0–M11: IMPLEMENTED / GREEN / INTEGRATED.
- M12 automated pre-human infrastructure/evidence: GREEN / INTEGRATED.
- Automated PACKAGED candidate: yes.
- HUMAN_TESTED: **false**.
- NVDA_VERIFIED: **false**; automation must never award this state.
- PRODUCTION_RELEASE_READY: **false** until the exact candidate passes the human protocol.

## M12 freeze maintenance — current cycle
- Re-read the binding architecture, roadmap, reuse, UI, large-batch, autonomous-development, acceptance and parallel-development policies plus Issue #1, open PRs and current repository metadata before acting.
- Confirmed the tested M12 exact candidate remains the human-bound candidate; no production source, package input or runtime behavior was changed in this cycle.
- Repository metadata still reports **PUBLIC**, despite user wording that calls it private.
- Found that old PR #4 was still open although its governance branch predates integrated M2 runtime selection and is superseded by the current `docs/PARALLEL_DEVELOPMENT_POLICY.md`. PR #4 was therefore closed **without merge** as obsolete. This removes a misleading non-mergeable branch from the active queue without changing product behavior.
- PR #11 was already closed without merge and remains superseded/uncredited.
- This maintenance awards **0%** additional weighted credit and does not invalidate M12 acceptance evidence.

## M12 artifact integrity re-verification — latest cycle
- GitHub artifact id `9340947647` was fetched successfully again from the canonical repository.
- Downloaded ZIP size is exactly `20,846,871` bytes and SHA-256 is exactly `ee0ec0b7506c6bc84b8052f1c1cdb80a33a6f0dea8eb6b221cee1860dc96d4c5`, matching the recorded workflow digest.
- `m12-prehuman-evidence.json` inside the artifact still binds the package to commit `d7bdfd697819adf13ad7423726a004fd781d857d` and records `human_tested=false`, `nvda_verified=false`, `production_release_ready=false`.
- The embedded release manifest still identifies product `NikaCore` version `0.0.2`.
- No production source, package input, runtime behavior or packaged candidate was changed. This is evidence reconciliation only and awards **0%** additional weighted credit.

## Current weighted milestone
M12 — Human Windows/NVDA acceptance and final v1.0 release decision.

## Blocker
The only weighted blocker is human execution of `docs/M12_HUMAN_NVDA_ACCEPTANCE.md` against the exact packaged candidate. This cannot be truthfully automated.

## Next LARGE coherent batch
Preserve the exact M12 pre-human candidate and keep automated regressions green. Do not expand production features during the freeze. Once human Windows/NVDA results exist, fix any concrete accessibility/functional defects on a new development candidate, rerun the complete M12 automated gate, and award HUMAN_TESTED/NVDA_VERIFIED plus the final 2% only if that same exact artifact passes the human protocol.
