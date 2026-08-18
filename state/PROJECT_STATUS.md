# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT / PARALLEL-FIRST
Repository visibility observed this cycle: PUBLIC.

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 6% / 6%.
- M1 kernel foundation: GREEN / INTEGRATED, 10% / 10%.
- M2 durable agent runtime: GREEN / INTEGRATED, 11% / 11%.
- M3 memory/scheduler/resource control: GREEN / INTEGRATED, 9% / 9%.
- M4 model gateway/tools/MCP: GREEN / INTEGRATED, 8% / 8%.
- M5 accessible web-style Windows GUI: GREEN / INTEGRATED, 11% / 11%.
- M6 Agent Builder & permissions: GREEN / INTEGRATED, 8% / 8%.
- M7 multi-agent laboratory: GREEN / INTEGRATED, 9% / 9%.
- M8 self-learning & experiment engine: GREEN / INTEGRATED, 10% / 10%.
- M9 Plugin SDK & real workspaces: GREEN / INTEGRATED, 8% / 8%.
- M10 security/sandbox/reliability: GREEN / INTEGRATED, 5% / 5%.
- M11 Windows packaging/distribution: GREEN / INTEGRATED / PACKAGED candidate evidence, 3% / 3%.
- M12 full-system QA/NVDA/release: AUTOMATED PRE-HUMAN GATE GREEN / INTEGRATED; HUMAN GATE OPEN, 0% / 2%.
- Overall proven final A–Z product progress: **98.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI 74; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI 85; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI 98 Ubuntu+Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI 112 Ubuntu+Windows+live Ollama; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 exact green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI 137 Ubuntu+Windows+packaged WebView2/UIA proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
- M6 exact green head `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI 142 Ubuntu+Windows; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.
- M7 exact green head `4feb976faa97949bacc321bcbce792d01359a58c`; Core CI 150 Ubuntu+Windows; PR #17 merged as `5a01692c1372375f040cd38558e33204b082d5a5`.
- M8 exact green head `d8f14ac097f2eb664b7f893543db359fb6245e6c`; Core CI 159 Ubuntu+Windows; PR #19 merged as `a8fd7b6c094489cebb79a4221e4124b5cfbad8f6`.
- M9 foundation exact green head `ef4bafc02d4517b3718f07e5f61b7e4e22072c25`; Core CI 188 SUCCESS; PR #21 merged as `e8967b97ec9772cb2af0b3b781468553708d4015`.
- M9 semantic adapter exact green head `680a8a15dfb5a30a7c6eaba3860817c1cbda7ac5`; Core CI 193 SUCCESS on Ubuntu+Windows plus live Playwright and Windows UIA proofs; PR #24 merged as `e6af748fb655fda2719be93258b6c2a427287994`.
- M10 exact green head `17826cc041caeaa1fe03f8dbe4ce5e50c7aecad5`; Core CI 167 SUCCESS on Ubuntu+Windows; PR #22 merged as `976cb9914966a67763742a28c229730417ee63c8`.
- M11 exact green head `0f48c3c4889478f967e383d385576e9322e55c9b`; Core CI 196 SUCCESS; focused M11 Windows Release Candidate run 3 SUCCESS; PR #23 merged as `5d86d83f6ce99fa4c9bd6a0eaadef1a6b404283c`.
- M12 automated pre-human exact green head `d7bdfd697819adf13ad7423726a004fd781d857d`; Core CI 200 SUCCESS; M12 Pre-Human Release Gate run 1 SUCCESS; PR #26 merged as `a278767de4435535cc54e5674ddc72d37051ba03`. This is automated evidence only and awards no human-only M12 credit.

## M9 integrated capability
- Versioned plugin/workspace compatibility, capability and risk contracts with lazy explicit activation.
- Software Factory workspace behind `CodingWorkerPort` with repository-relative scope, default network denial and independent result/evidence validation.
- Accessibility Repair/Assistant semantic-first workflow with provenance and visual/coordinate fallback only after semantic inspection.
- Real optional Playwright ARIA snapshot adapter and pywinauto/UIA Windows adapter proven in live CI.

## M10 integrated capability
- Traversal-safe workspace write boundaries, exact network/process allowlists, bounded execution budgets and exact-action approval fingerprints.
- Expiring, timezone-aware, single-use approval evidence and fail-closed authorization order.
- Defense-in-depth policy only; no false claim of a complete OS-level sandbox.

## M11 integrated/package evidence
- Reused PyInstaller 6.x `onedir`/`windowed` packaging and the integrated pywebview/WebView2 shell.
- Product Windows launcher uses the central Action Registry/Keymap and bundled local web assets.
- Deterministic release manifest inventories files with sizes and SHA-256; verification fails on missing/unexpected/modified files and release-root escaping symlinks.
- Exact-head Core CI 196 passed the normal source/test gate.
- M11 Windows Release Candidate run 3 passed source regressions, PyInstaller build, packaged WebView2/UIA descendant discovery, keyboard/focus flow, ZIP creation and artifact upload.
- Produced artifact `NikaCore-0.0.2-windows-x64`, artifact id `9340673484`, size 20,846,262 bytes, workflow artifact digest `sha256:4611ba73baab2ecd37ce07d5596853edd6f4d6fa7559ff58d660d5f5aee5b12e`.
- Earlier release failure was a test-harness mismatch: reused M5 UIA proof hard-coded the diagnostic window title. The repair parameterized the expected title while preserving all UIA/readiness/keyboard/focus assertions.

## M12 automated pre-human evidence
- Core CI 200 passed on exact M12 head `d7bdfd697819adf13ad7423726a004fd781d857d`.
- M12 integrated Ubuntu job passed complete dependency/Ruff/compile/test verification: 152 tests, plus a focused 58-test cross-subsystem recovery/safety matrix and live Playwright semantic proof.
- M12 integrated Windows job passed the same full verification/focused matrix and real Windows UIA semantic proof.
- Dependent packaged Windows job passed exact candidate build, deterministic manifest verification, packaged WebView2/UIA discovery, Action Registry keyboard/focus flow and release-gate truth regressions.
- Produced exact pre-human artifact `NikaCore-0.0.2-m12-prehuman-d7bdfd697819adf13ad7423726a004fd781d857d`, artifact id `9340947647`, size 20,846,871 bytes, workflow digest `sha256:ee0ec0b7506c6bc84b8052f1c1cdb80a33a6f0dea8eb6b221cee1860dc96d4c5`.
- `docs/M12_HUMAN_NVDA_ACCEPTANCE.md` binds human testing to exact SHA/artifact identity and requires Windows/NVDA keyboard, semantic navigation, focus, readable status/error and restart checks.
- Machine-readable automated evidence deliberately records `human_tested=false`, `nvda_verified=false`, `production_release_ready=false`.

## Canonical documentation integrity repair — this cycle
- The cycle detected that `docs/ROADMAP.md` was stale at 82% / M9-next while `state/PROJECT_STATUS.md` and Issue #1 already correctly reported 98% and the M12 human-only blocker.
- The cycle also detected that `AGENTS.md` required `docs/PARALLEL_DEVELOPMENT_POLICY.md` even though that file was absent from `main`.
- Docs-only PR #28 restored a current parallel-first policy with an explicit M12 pre-human release-freeze rule and synchronized the roadmap to the proven 98% state without changing production source, package inputs or runtime behavior.
- PR #28 exact head `230d7cbe54f4707dab05a42eac474686527eb79c`; Core CI #205 SUCCESS on Ubuntu and Windows; merged as `940399a3a17344f38cf29cac0f789fb3084b359d`.
- This maintenance work awards no new weighted progress and does not invalidate the exact M12 packaged candidate.

## Truth state
- M0–M11: IMPLEMENTED / GREEN / INTEGRATED.
- M12 automated pre-human infrastructure/evidence: GREEN / INTEGRATED.
- Overall proven progress remains 98.0%; the final 2% is intentionally human-gated.
- Automated PACKAGED candidate evidence exists from the exact M12 candidate.
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false; automation must never award this state.
- PRODUCTION_RELEASE_READY: false until human acceptance passes on the exact candidate.
- Repository metadata is PUBLIC despite user wording referring to a private repository.
- Stale PR #4 and draft PR #11 remain uncredited.

## Current weighted milestone
M12 — Human Windows/NVDA acceptance & final v1.0 release decision.

## Blocker
The only weighted acceptance blocker is now human execution of `docs/M12_HUMAN_NVDA_ACCEPTANCE.md` against the exact packaged candidate. This cannot be truthfully automated.

## Next LARGE coherent batch
Keep automated regressions green and preserve the M12 pre-human release freeze. Do not expand production features in a way that invalidates the tested package. Once human Windows/NVDA results are available, fix any reported accessibility/functional defects on a new exact candidate, repeat the full M12 automated gate, then award HUMAN_TESTED/NVDA_VERIFIED and the final 2% only if the human protocol passes that same exact artifact.
