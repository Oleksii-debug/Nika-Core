# Cinema & Voice Factory — ready-code reuse, license decisions, and integration backlog (2026-10-16)

**Authority and status:** Technical research appendix to the canonical 27-section **Plan 8** in Google Drive. **NOT** an alternative work-selection plan, NOT implementation, NOT evidence of a completed release. Plan8 = **0/27 DONE**, and historical Plans1–7 retain their original owners/sections/closure facts.

Canonical source documents:
- [Plan 8 — Technical implementation and acceptance, with expanded ready-product addendum](https://docs.google.com/document/d/1Cp-_1qfA69D0cZv1SAsVvkcAegA7CLnnsxJ8AkeEWqw/edit)
- [Author's vision/reflection — 22 themes](https://docs.google.com/document/d/1ukvWtRV2jAny0_rxeBUqKEETwEqtgddETkGw94MixFM/edit)
- [Detailed third-party software/model/asset license register and practical use cases](https://docs.google.com/document/d/1rg50_KJULpTAIbLasucj4kCuBiApCraBzVS4yU4bs5g/edit)

## Goal and product boundary

NikaCore is the one authoritative Windows/NVDA-first accessible orchestration/product runtime (ModelGateway, AgentRuntime, permissions, persistence, scheduler, memory, audit). The future Cinema & Voice Factory is a separate product extension built on *versioned ports*, not a second kernel. It targets:
- zero paid-token idle local wake word "Ніка"; real-time interruptible duplex ASR↔LLM↔TTS;
- authentic original voice design, lawful voice cloning, speech-manner extraction/transfer independently of voice identity, emotional context and expressive performance;
- offline low-latency Ukrainian NVDA TTS and separate high-quality audiobook voices;
- optional realtime interactive video persona and consented digital twins (face/body/gesture/voice);
- multi-character talk shows, coherent screenplay/storyboard/3D sets/shot-generation/foley/music/timeline/render/edit/re-export; accessible scene editing by spoken command and NVDA keyboard;
- separable future products/API, with actual licensing and compute budgets before sales.

**No assumption that a permissively licensed application also grants permissively licensed model weights, sample content, user likenesses, voice checkpoints, or services.**

## Distinguish 7 practical permission questions for every dependency

1. May owner personally install/run the program locally? Does the license grant such use?
2. May engineers view the published source and learn architectural concepts?
3. May code be copied, modified, linked, bundled or distributed?
4. May a film/audiobook/output made with the software be sold or posted to monetized YouTube?
5. May the actual code+weights be shipped inside a *closed paid Windows EXE*?
6. May the same engine be offered as *paid online SaaS/API*?
7. May pretrained checkpoints be fine-tuned, redistributed, resold or deployed in Europe?

Answers can differ for each repository subproject, model, dataset, third-party component and territory. No automatic inference of consent to clone identifiable speakers/faces.

### Licenses translated into implementer actions

- **MIT / BSD-2-Clause**: generally reuse/modify/distribute in proprietary commercial software, while preserving exact copyright/license/disclaimer notices required by the license. Does **not** cover excluded or separately licensed weights/assets.
- **Apache-2.0**: permits commercial closed-source reuse under LICENSE, relevant NOTICE, change marking and other terms including patent rules. Check exact weights and additional EULA restrictions.
- **MPL-2.0**: can coexist with proprietary code, but covered/modified MPL files need to meet source availability requirements on distribution.
- **GPL-3.0**: creating *commercial finished films* with a GPL editor normally doesn't turn film copyright into GPL; distributing combined derivative GPL software may require licensing and corresponding source code under GPL. Invoking an *independent external renderer* may be different; an API/process boundary is not automatic legal immunity.
- **LGPL**: proprietary use often feasible with library notices, source/modification/relinking rights and requirements matching its version/build. FFmpeg build options/optional codec licenses matter.
- **AGPL**: modified network-interactive services can trigger corresponding-source duties to remote users. Pay special attention to SaaS.
- **CC BY-NC(-SA)** / special noncommercial models: do not deploy in paid features, commercial film dubbing or bundled product absent new rights; do not assume fine-tuning removes inherited restriction. Some models explicitly restrict commercial **outputs**.
- **CC0**: useful for lawful asset reuse, but personality/privacy/trademark rights and hosted-site/API ToS are distinct.
- **Custom community / source-available EULA**: code visibility ≠ code commercial redistribution rights. Read exact scope for streaming, monetization, embedding, paid hosting, region and derivatives.
- **Public repository without LICENSE**: not a general permission to copy/modify/relicense/sell code.
- Independent implementation is a valid *potential* route: public conceptual documentation/papers → independent design/source and lawfully sourced training assets/weights. Do not copy restricted code in disguise or use restricted base checkpoints. Patent, contract and dataset rights remain.

## Full working-program examples worth auditing, not just standalone models

| Scope | Repository (original upstream) | Code status / decisive caveat | NikaCore use |
|---|---|---|---|
| Voice assistant | [Open-LLM-VTuber backend](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) | MIT at reviewed version; sample Live2D assets separate; verify version-specific license | ASR/LLM/TTS, barge-in and avatar event benchmark |
| Companion UI | [Open-LLM-VTuber-Web](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web) | Custom License 1.0; monetized *content* may be allowed; paid SaaS, EXE embedding/rebranding require separate grant | Study UI, do not import into paid closed NikaCore by default |
| 3D chat | [Amica](https://github.com/semperai/amica) | Majority MIT code; models/images third-party licenses | VRM character controller, Tauri desktop, emotion UX |
| Real-time digital human | [OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat) | Main repo Apache-2.0 claimed; WebUI/weights/deps separate | Avatar/video+voice pipeline |
| Live avatar streaming | [LiveTalking](https://github.com/lipku/LiveTalking) | Community Apache-2.0; README watermark/logo condition on published videos; optional Wav2Lip NC; commercial edition distinct | Streaming, WebRTC, interrupter, model-adapter boundary |
| Integrated speech+face | [Linly-Talker](https://github.com/Kedreamix/Linly-Talker) | MIT main code; integrated models separate | Learn combined digital human structure |
| Realtime media agents | [Pipecat](https://github.com/pipecat-ai/pipecat) | BSD-2-Clause; API services may cost money | Candidate primary duplex voice runtime |
| Realtime agent/media | [LiveKit Agents](https://github.com/livekit/agents) + [server](https://github.com/livekit/livekit) | Apache-2.0 framework; special LiveKit turn-model license; hosted Cloud billed separately | Benchmark alternative to Pipecat; no second agent authority |
| Windows AI video app | [LTX Desktop](https://github.com/Lightricks/LTX-Desktop) | Apache-2.0 app code; LTX 2.x weight Community Licenses distinct (usage/revenue/redistribution); major GPU/disk need | Desktop renderer architecture, installer, model catalog, job queue |
| Automated short video | [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) | MIT code; video stock and provider/media rights distinct | Story/script→TTS→material→subtitles→music→export |
| Nonlinear editor | [OpenCut](https://github.com/opencut-app/opencut) | MIT code | Accessible track/timeline implementation reference |
| Mature nonlinear editor | [OpenShot](https://github.com/OpenShot/openshot-qt) | GPL3-or-later | Standalone editor/research, no unreviewed proprietary embedding |
| Storyboarder | [Wonder Unit Storyboarder](https://github.com/wonderunit/storyboarder) | Nonstandard/ambiguous copyright/EULA; commercial redistribution restricted or not clearly granted | Research/reference only absent explicit grant |
| 3D renderer | [Blender](https://github.com/blender/blender) | GPL3; commercial rendered outputs generally free of GPL | External scene/rig/lighting/render worker |
| Graph model workflows | [ComfyUI](https://github.com/Comfy-Org/ComfyUI) | GPL3; custom node/weights each require separate review | External adapter or independent DAG model |
| Voice training UI | [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) | MIT code, downloaded submodels and corpus rights separate | Audio segmentation, transcript, speaker training UX |

Other key model/component candidates (rights on **exact files**):
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS): Apache family for voice design; UA not in its historically listed official supported languages.
- [Chatterbox](https://github.com/resemble-ai/chatterbox): MIT project expressive variants, individual weights/model terms/UA quality to test.
- [OpenVoice V2](https://github.com/myshell-ai/OpenVoice): MIT commercial-friendly voice clone/style; UA support not guaranteed.
- [MuseTalk](https://github.com/TMElyralab/MuseTalk): MIT/author-stated commercial own models; third-party VAE/detector/Whisper/test footage separate.
- [LivePortrait](https://github.com/KlingAIResearch/LivePortrait): MIT code, replace InsightFace NC detection models before commercial shipping.
- [Wan2.2](https://github.com/Wan-Video/Wan2.2): Apache weights according to upstream, substantial GPU for video.
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx): Apache code, every model license separate; speech, offline local KWS.
- [Piper1](https://github.com/OHF-Voice/piper1-gpl): GPL and voice-specific licenses; compare old Piper MIT history with actual phonemizer dependencies.
- [F5-TTS](https://github.com/SWivid/F5-TTS): MIT **code** / CC BY-NC **official pretrained weights**; do not ship NC weights in commercial path.
- [VoiceCraft](https://github.com/jasonppy/VoiceCraft): source/model NC restrictions; speech editing research only absent different grant.
- [Coqui XTTS-v2](https://huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt): Coqui model license includes non-commercial limitations covering model and outputs; separate from TTS toolkit source terms.
- [Wav2Lip](https://github.com/Rudrabha/Wav2Lip): public research code/models restrict commercial use; no direct paid application incorporation without license.
- [HunyuanVideo 1.5](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5): standard license territory excludes EU; BLOCK for Slovak intended usage absent grant.
- [VRoid Studio](https://vroid.com/en/studio/guidelines): creating licensed videos with VRoid characters differs from using default meshes/textures to build a commercial model generator.
- [Poly Haven](https://polyhaven.com/license): CC0 assets, hosted API ToS distinct.
- [Fab assets](https://www.fab.com/eula?lang=en): purchased media generally usable in covered completed projects, raw asset resale/export restrictions.

## Engineering execution order (not a schedule, NOT DONE)

1. Audit exact existing NikaCore source/PR/12-6 AI interchangeable cognitive-model ports. Establish no overlap with Plans1–7.
2. Pin exact repo SHA and dependencies; compile legal/license + model-card + copyright + media rights SBOM. Reject UNKNOWN for shipping.
3. Benchmark **Pipecat vs LiveKit** and existing Nika voice: 30 interruption turns, playback cancel vs active job cancellation, zero paid idle tokens, offline wake, microphone/privacy, echo/NVDA conflicts.
4. Benchmark **OpenAvatarChat vs LiveTalking vs Amica** for video persona at real GPU limits, latency, lip-sync, body motion capabilities, audio-only switch.
5. Benchmark Qwen3-TTS, Chatterbox, OpenVoice, GPT-SoVITS and one truly licensable offline Ukrainian low-latency TTS on identical UA/voice-likeness/intelligibility tests.
6. Benchmark LTX Desktop's *code design*, MoneyPrinterTurbo's script pipeline, OpenCut editable project model, Blender/FFmpeg output and verified Wan video checkpoints.
7. Implement *one* canonical scene/voice/asset version graph and accessible NVDA command facade. Regenerate only affected shots; check audio cues, casting identity and cost.
8. Physically qualify with Windows/NVDA, authorized camera/mic and same 2-character project through restart; verify exact artifact provenance/quality/cost.
9. Only then consider paid product binary/SaaS packaging under lawfully audited software, weights and likenesses.

License-gate negative fixtures to catch: paid Open-LLM-VTuber-Web embedding; Wav2Lip NC; F5 NC weights; XTTS-v2 outputs; LivePortrait restricted InsightFace; LiveTalking watermark ignored; Storyboarder unclear EULA; GPL editor code silently integrated into closed executable; HunyuanVideo EU territory; VRoid raw base meshes resale; unconsented faces/voices; unlicensed stock/music; insufficient NVIDIA GPU local mode; LTX model grant missing.

**Integration contract:** each candidate behind versioned typed interfaces `SpeechRuntimePort`, `VoiceFactoryPort`, `AvatarPort`, `FilmProductionPort`, `LicenseGatePort`, `CostControlPort`. All effects require the existing central runtime/permission/audit. Text-first scene/media objects are the accessible source of truth, not GUI pixels. Physical tests and signed/hashed outputs required before DONE. Future workers must read the canonical Drive Plan8 and license register, since this repo guide is a navigation/research mirror, not an alternate implementation authority.
