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
- M12 full-system QA/NVDA/release: IMPLEMENTED foundation only / HUMAN GATE OPEN, 0% / 2%.
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
- The earlier release run failed because the reused M5 UIA script hard-coded the diagnostic window title. The repair parameterized the expected title while preserving the original M5 default and all descendant/keyboard/focus assertions.

## Truth state
- M0–M11: IMPLEMENTED / GREEN / INTEGRATED.
- Overall proven progress: 98.0%.
- M11 has automated PACKAGED candidate evidence from the exact integrated source. It is not HUMAN_TESTED.
- M12 remains the final 2% and requires full-system release/recovery/security evidence plus human accessibility acceptance.
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false; automation must never award this state.
- Repository metadata is PUBLIC despite user wording referring to a private repository.
- Stale PR #4 and draft PR #11 remain uncredited.

## Current weighted milestone
M12 — Full-system QA, human NVDA acceptance & v1.0 release.

## Next LARGE coherent batch
Run the largest safe automated M12 pre-human gate from latest green main: full-system import/startup and migration/recovery matrix, security/sandbox regressions, release-manifest integrity, packaged startup/WebView2/UIA/keyboard flow, plugin/workspace discovery, model mock/no-LLM path, scheduler/memory/runtime restart evidence and artifact provenance. Create a clear human NVDA acceptance protocol for the exact candidate, but do not mark HUMAN_TESTED or NVDA_VERIFIED until the user actually performs it. Final 2% remains uncredited until that human gate is satisfied.
