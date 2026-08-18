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
- M11 Windows packaging/distribution: IMPLEMENTED candidate / NOT GREEN / NOT INTEGRATED, 0% / 3%.
- M12 full-system QA/NVDA/release: IMPLEMENTED foundation only / NOT GREEN / NOT INTEGRATED, 0% / 2%.
- Overall proven final A–Z product progress: **95.0%**.

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
- M9 real semantic adapter exact green head `680a8a15dfb5a30a7c6eaba3860817c1cbda7ac5`; Core CI 193 SUCCESS on Ubuntu+Windows plus live Playwright ARIA snapshot and real Windows UIA proof; PR #24 merged as `e6af748fb655fda2719be93258b6c2a427287994`.
- M10 exact green head `17826cc041caeaa1fe03f8dbe4ce5e50c7aecad5`; Core CI 167 SUCCESS on Ubuntu+Windows; PR #22 merged as `976cb9914966a67763742a28c229730417ee63c8` and its security policy remains present in current main.

## M9 integrated capability
- Versioned plugin manifests with API compatibility ranges, capability/risk declarations and fail-closed activation checks.
- Lazy `importlib.metadata` entry-point discovery under the Nika plugin boundary; discovery does not eagerly instantiate adapters.
- Versioned workspace manifests with required plugin/API/capability contracts, workspace risk ceilings and traversal-safe paths.
- Software Factory proof workspace with framework-neutral `CodingWorkerPort`, repository-relative allowed paths, network disabled by default, worker-result path/evidence validation and capability-gap evidence. OpenHands remains an adapter candidate rather than a vendored runtime.
- Accessibility Repair/Assistant proof workspace with semantic-before-vision policy and provenance.
- Real Playwright browser semantic adapter using `aria_snapshot()` behind the Nika interaction boundary.
- Real pywinauto/UIA Windows semantic adapter scoped to a target process.
- Focused live CI proves Chromium accessibility-tree extraction and Windows UI Automation semantic inspection through Nika adapters.
- Browser/Windows interaction dependencies remain optional components rather than mandatory core bloat.

## M10 integrated capability
- Canonical workspace sandbox policy with traversal-safe writable roots, network host allowlists and executable allowlists.
- Bounded write/network/process budgets.
- Immutable exact-action intents and SHA-256 fingerprints for approval evidence.
- Expiring, timezone-aware, single-use approval evidence; HIGH_IMPACT requires exact-action approval.
- Fail-closed authorization ordering: tool grant -> sandbox boundary -> approval -> resource reservation.
- This is defense in depth and does not falsely claim a complete OS-level sandbox.

## Current M11 acceptance work
PR #23 `agent/m11-m12-release-foundation` is open and draft. Previous exact head `2b6d53dfcdd1bcd89541f3cc5c5369f83a248073` passed Core CI 190 but M11 Windows Release Candidate run 1 failed only at the packaged UIA window-discovery step after source regressions and PyInstaller build succeeded. Root cause: the reused M5 proof hard-coded the diagnostic title `Nika Core M5 Proof`, while the real product launcher uses `Nika Core 0.0.2`.

The branch has been repaired without weakening the accessibility gate: `scripts/m5_uia_proof.ps1` now accepts an explicit window title with the original M5 title as the default, and the M11 workflow passes `Nika Core 0.0.2`. Current candidate head: `0f48c3c4889478f967e383d385576e9322e55c9b`. Core CI and the focused M11 Windows Release Candidate workflow are pending on that exact head. No M11 credit until both are green and the candidate is integrated.

## Truth state
- M0–M10: IMPLEMENTED / GREEN / INTEGRATED.
- Overall proven progress: 95.0%.
- M11: IMPLEMENTED candidate, not GREEN/INTEGRATED/PACKAGED yet.
- M12: foundation only; final credit requires full-system/recovery/security evidence plus human accessibility acceptance.
- PACKAGED (release): false until exact M11 artifact workflow is green and integrated.
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false; automation must never award this state.
- Repository metadata is PUBLIC despite user wording referring to a private repository.
- Stale PR #4 and draft PR #11 remain uncredited.

## Next LARGE coherent batch
Finish M11 on PR #23 using the exact repaired candidate: inspect executable Core CI and the Windows release workflow, repair any real defect without weakening WebView2/UIA/keyboard/integrity gates, require ZIP+manifest artifact production, then merge only on green evidence. After M11 integration, run the M12 full-system recovery/security/release matrix and prepare the user-facing human NVDA acceptance protocol. Do not award HUMAN_TESTED or NVDA_VERIFIED automatically.
