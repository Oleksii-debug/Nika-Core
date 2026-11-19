## 2026-10-11 Owner work decomposition (no renumbering)
Each of the seven numbered Drive plans now contains A/B/C micro-closure checkpoints inside its first Section. A = reuse audited existing implementation + exact SHA/scoped tests; B = first provable failed CI job repaired with negative/recovery tests; C = full inherited acceptance, ordered integration and postmerge main SHA readback. Independently record A/Б/В status, but original Section DONE only when all required evidence exists. Do not create a separate new PR or reimplement old code to satisfy the checkpoints; they are acceptance bookkeeping, not expanded feature scope. No prompt/session changes. Terminal total section counts remain unchanged.

# Nika Core — Canonical Multi-Plan Index

## Authority
The former monolithic 50-Section plan (0–49) is SUPERSEDED FOR WORK SELECTION and remains audit/history only.

Canonical Drive folder:
https://drive.google.com/drive/folders/1ncMMl9ce2Wq6vnsMI9PxJS7-s4yq4Iam

Plan files use the universal naming convention:
1. Перший план
2. Другий план
3. Третій план
4. Четвертий план
5. П’ятий план
6. Шостий план
7. Сьомий план

Plans 1–6 are independent engineering plans with no global priority order.
Plan 7 is final whole-product convergence / physical acceptance / documentation / go-live and MUST NOT be started as a normal worker lane before required terminal outputs from Plans 1–6 exist.

## Plan themes
1. Core architecture, contracts, persistence, runtime, security, capability/model authorities.
2. Agent platform and Autonomous Product Factory.
3. Deterministic/model intelligence, memory, research and self-learning.
4. Windows product journey, browser/UIA, accessibility, media and reports.
5. Domain workspaces: Trader, Personal Nika, Business Factory.
6. Web/Cloud/Node, packaging, provenance, diagnostics and performance.
7. Whole-product convergence, physical Windows/NVDA/Web/Cloud acceptance, documentation and go-live.

## Worker selection
For Plans 1–6:
- read this file, MULTI_PLAN_PARALLELISM_CONTRACT.md, MULTI_PLAN_CLOSURE_STATE.md, AGENTS.md, the assigned Drive plan and live GitHub;
- skip terminal DONE;
- take the first unfinished ACTIONABLE Section inside the assigned plan;
- audit/reuse existing implementation/PR/evidence first;
- implement only missing acceptance-critical gaps;
- use stable contracts/fixtures for unfinished peer plans;
- do not claim whole-product evidence from mocks/fixtures;
- do not silently mutate another plan.

Plan 7 is dependency-bound and receives no ordinary parallel worker prompt until Plans 1–6 are sufficiently terminal for convergence.

Detailed mapping: LEGACY_50_TO_MULTIPLAN_COVERAGE.md.

## Owner extension 2026-10-16 — new canonical Plan 8 (separate Cinema & Voice Factory product)
**The seven historical plans above remain intact and their numbering, section statuses, active lanes, existing ownership, and original Plan 7 convergence gates are unchanged.** This is an eighth, last numbered Drive plan, NOT a replacement for Plan 7 and NOT a reason to delay existing 1–7 completion.
- Plan 8 — «8. Восьмий план — Nika Cinema & Voice Factory», 27 new numbered sections, all OPEN / NOT DONE:
- Technical ready-product/code/licensing research mirror (authoritative source is still the Plan8 Drive docs): [docs/CINEMA_VOICE_FACTORY_REUSE_AND_LICENSE_AUDIT.md](docs/CINEMA_VOICE_FACTORY_REUSE_AND_LICENSE_AUDIT.md). Includes practical MIT/Apache/GPL/LGPL/AGPL/MPL/CC-NC scenarios, complete products (Open-LLM-VTuber, OpenAvatarChat, LiveTalking, LTX Desktop, MoneyPrinterTurbo, Pipecat, LiveKit, Amica, Linly-Talker, OpenCut, OpenShot, Storyboarder, GPT-SoVITS), implementation priorities, and negative license gates.
  https://docs.google.com/document/d/1Cp-_1qfA69D0cZv1SAsVvkcAegA7CLnnsxJ8AkeEWqw/edit
- Owner narrative «Cinema & Voice Factory — Моя рефлексія та бачення автора»:
  https://docs.google.com/document/d/1ukvWtRV2jAny0_rxeBUqKEETwEqtgddETkGw94MixFM/edit
- Technology, forbidden/conditional/research-only code & model-license register:
  https://docs.google.com/document/d/1rg50_KJULpTAIbLasucj4kCuBiApCraBzVS4yU4bs5g/edit
- Theme: standalone-capable professional Voice Factory, offline NVDA TTS, multimodal Live Nika/video persona, consented digital twins, multi-character stage, cinema/animation production, storyboards, rendering, Foley/music, editing, export, licensing, monetizable optional API.
- Existing plans still own shared canonical NikaCore contracts/runtime/security/agents/model/scheduler/Windows/media/packaging. Plan 8 owns only new product-specific project schemas, voice/style/character/shot/timeline/renders, media engine adapters and tests. Reuse shared authorities via typed ports; do not fork them.
- A separate new Plan-8 owner must be explicitly assigned; ordinary Plan1–6 worker selection and Plan7 dependent release must not drift into Plan8, and the Plan8 documentation must not be counted as code or DONE.
- Plan8 has its own staged product acceptance and source/rights/HIL gates. Existing 50 legacy sections and old completion metrics stay scoped to original plans.

