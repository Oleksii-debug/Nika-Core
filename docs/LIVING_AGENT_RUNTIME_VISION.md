# Nika Core — Living Agent runtime, voice and embodiment vision

## Purpose

This document defines the long-term runtime/embodiment direction for Nika as the body, nervous system and always-available operational layer around the 12-6 AI cognition/model lineage.

The owner should experience one coherent agent named **Nika**, while internally the system preserves a replaceable brain/body boundary:

- **12-6 AI** — language/model/cognition/learning/self-improvement contracts;
- **Nika Core** — voice, perception, devices, browser, files, scheduling, durable tasks, system telemetry, communications, resource arbitration and external tools.

This architecture allows the brain to scale from 20M to 100M to larger checkpoints without rebuilding the surrounding product.

## Product principle

Nika should not behave like a static assistant that is inactive until a button is pressed and then follows fixed style toggles.

The target is a **persistent dynamic agent** that can:

- remain available across long periods;
- maintain autobiographical and task continuity;
- initiate bounded useful activity during idle time;
- downshift automatically when Oleksii needs the computer;
- use speech as a primary interface;
- distinguish Oleksii’s voice from other speakers probabilistically;
- communicate over laptop, phone, Telegram/text/voice-message surfaces;
- perceive text/audio/images/video/system state through replaceable tools;
- report what it did while the owner was away;
- execute developmental/self-improvement workflows supplied by the 12-6 cognition layer.

## 1. Always-available voice interface

Voice should be an early first-class interface.

Target interaction:

- Oleksii says the wake name “Nika” without touching the keyboard;
- the system activates listening;
- speech is transcribed with low latency;
- the agent begins answering as soon as enough text is generated;
- speech synthesis streams the answer sentence/chunk by sentence/chunk;
- Oleksii can interrupt naturally (barge-in);
- the same conversation/memory continues across laptop and phone channels where configured.

## 2. Custom Nika voice

Nika should have a persistent custom female voice designed as a product asset rather than merely selecting a generic system voice.

Desired properties:

- natural young-adult female timbre chosen by the owner;
- low-latency streaming synthesis;
- clear Ukrainian first, expandable multilingual support;
- expressive prosody;
- laughter/smile-like prosodic variation where supported;
- dynamic warmth/energy/seriousness controlled by conversational context and agent state;
- no requirement that Oleksii manually choose “happy”, “angry”, “professional”, etc. for every turn.

The speech layer should accept a compact prosody/state envelope from the cognition layer, such as speaking rate, energy, certainty, social tone and urgency. These outputs are dynamic consequences of state, not permanent personality flags.

## 3. Wake word and speaker awareness

Wake-word detection and speaker verification are separate capabilities.

Nika should support:

- local wake-word detection where practical;
- voice activity detection;
- speaker verification: “is this likely Oleksii?”;
- speaker identification for enrolled known speakers where configured;
- speaker diarization for multi-person recordings;
- overlapping-speaker / unknown-speaker states;
- confidence values rather than pretending speaker identity is always certain.

A privileged command can require both:

1. wake word / explicit activation;
2. sufficient Oleksii speaker-confidence or another configured confirmation path.

Other people speaking in the room should not automatically be treated as Oleksii commands.

## 4. Ambient communication analysis mode

Nika may provide an explicit mode in which conversations are recorded/transcribed and separated by speaker for later coaching.

Potential outputs:

- which segments were Oleksii vs other speakers;
- filler words;
- speaking/listening ratio;
- interruptions;
- response latency;
- closed vs open questions;
- missed follow-up opportunities;
- excessive self-focused monologue;
- repeated verbal habits;
- alternative formulations;
- longitudinal communication/charisma progress.

Feedback should point to specific moments/timestamps when possible.

This mode must be technically separable from ordinary wake-word operation so that the product can support both “only listen after Nika is called” and an intentional analysis/recording session.

## 5. Perception channels (“eyes and ears”)

Nika Core should provide typed observation channels for the cognition layer:

- microphone/audio stream;
- speech-to-text;
- speaker identity/diarization;
- camera/image/video frames when a device grants access;
- browser/page/document observations;
- OCR when necessary;
- local files;
- PDFs/documents/spreadsheets;
- notifications/messages;
- system telemetry;
- user-activity signals;
- task/tool outcomes.

These observations should be timestamped and attributable to their source/channel.

## 6. Resource-aware autonomy

Nika should understand and manage the machine as an environment.

Inputs can include:

- CPU load;
- GPU load/VRAM;
- RAM pressure;
- battery/charging state;
- thermal signals where available;
- network status;
- foreground user activity;
- active applications/process classes;
- owner-defined resource preferences.

Target natural-language policy example:

> “When I leave, the laptop is yours. When I come back and start working, give my work priority.”

The runtime should translate this into durable policy and adapt automatically:

- high idle resources -> allow deeper background research/testing/training;
- owner returns -> reduce/pause nonessential workloads;
- memory pressure rises -> release caches / suspend lower-priority tasks;
- owner leaves again -> resume from durable checkpoints.

The owner should not need to repeat the same resource instructions every day.

## 7. Background life cycle

Nika should not be “off” merely because no user prompt is pending.

A bounded background scheduler may repeatedly evaluate:

- unfinished user tasks;
- learning/research queues;
- unresolved contradictions;
- books/sites/material assigned for study;
- self-tests;
- memory consolidation;
- local-model discussions;
- pending training candidate preparation;
- resource availability;
- whether doing nothing is currently the best action.

The runtime should support long idle periods, sleeps, wake-ups and resumption rather than an uncontrolled tight loop.

## 8. Daily autobiographical report

Nika should be able to answer naturally:

> “What did you do while I was away?”

The answer should come from real durable records, for example:

- what it read;
- why it chose a topic;
- what it learned;
- what remained uncertain;
- what experiments it ran;
- what failed;
- which local/API teachers it consulted;
- what it added to memory;
- what it proposed for future model training;
- how much compute/time it used.

No fabricated activity summaries.

## 9. Communication surfaces

The same Nika identity and memory should be able to appear through multiple adapters:

- local Windows application;
- microphone/speaker always-available mode;
- phone app or web client;
- Telegram text;
- Telegram voice messages;
- other approved messaging connectors later.

Example:

Oleksii sends a Telegram voice message -> Nika transcribes -> same cognition/memory processes it -> Nika can answer in text or synthesize and send a voice reply.

Channel adapters should not create separate personalities or separate memories.

## 10. Local and remote AI teachers

Nika Core should expose replaceable connectors for:

- the current 12-6 checkpoint;
- stronger local models;
- strong remote/API models;
- deterministic tools.

The cognition layer may orchestrate bounded discussions/debates with these systems.

Nika Core is responsible for:

- connection/runtime execution;
- budgets/quotas;
- timeouts;
- durable task state;
- cost/resource accounting;
- returning structured observations/results.

The 12-6 cognition layer decides how the evidence contributes to knowledge and learning.

## 11. Dynamic social interaction

Interaction style should depend on the current interlocutor and context rather than one global preset.

Examples:

- Oleksii -> established personal communication context;
- child -> simpler language and age-appropriate phrasing when inferred with sufficient context;
- formal work interaction -> concise/formal tone;
- casual conversation -> relaxed tone;
- uncertain speaker/context -> conservative neutral behavior until context improves.

Long-term style should be learnable from successful interactions and explicit user feedback.

## 12. Relationship to self-improvement

Nika Core must not directly decide that a new fact rewrites model weights.

Instead it provides the embodiment and execution needed for the 12-6 self-improvement pipeline:

- gather observations;
- read books/sites;
- transcribe discussions;
- consult local/API teachers;
- run deterministic tools/tests;
- store evidence;
- execute bounded training jobs when authorized by project policy;
- compare model versions;
- preserve/restore checkpoints;
- expose results to the owner in plain language.

## 13. Initial delivery order

Do not wait for a very large 12-6 model to start embodiment work.

### Early 20M-era prototype

1. text chat with learned 12-6 checkpoint;
2. low-latency speech-to-text;
3. streaming custom Nika voice;
4. wake word;
5. Oleksii speaker verification;
6. persistent conversation memory;
7. basic background scheduler;
8. system resource telemetry and downshift/resume;
9. simple book/document study queue;
10. daily activity report.

### Next stage

- diarized multi-person analysis;
- phone/Telegram voice/text continuity;
- camera/image/video observation;
- local-model teacher dialogue;
- API teacher dialogue with budget control;
- autonomous research/learning queues;
- communication coaching.

### Later

- richer multimodal world modeling;
- more autonomous developmental/self-improvement cycles;
- stronger 12-6 checkpoints plugged into the same runtime;
- distributed/server compute while preserving one Nika identity.

## 14. Acceptance examples

Nika should eventually be able to satisfy user-visible scenarios such as:

1. Oleksii: “Nika.” -> Nika wakes, recognizes the enrolled voice with confidence and answers without a keyboard action.
2. Another person says “Nika” -> no privileged Oleksii action is executed unless configured conditions are met.
3. Oleksii: “I’m leaving; the laptop is yours.” -> background work expands within policy.
4. Oleksii returns and begins active work -> Nika automatically reduces nonessential resource use.
5. Oleksii: “What did you do today?” -> Nika reports real logged activity and discoveries.
6. In explicit communication-coaching mode, Nika later distinguishes Oleksii from other speakers and gives timestamped conversational feedback.
7. A 20M checkpoint is replaced by a 50M/100M checkpoint -> voice, memory, devices and task infrastructure continue to work without product redesign.

## 15. Research/implementation candidates to evaluate

The implementation team should evaluate rather than hard-code one vendor for:

- streaming speech recognition;
- wake-word detection;
- speaker embeddings/verification;
- speaker diarization;
- expressive streaming TTS / custom voice training;
- audio front-end/noise handling;
- local on-device inference;
- mobile/Telegram adapters;
- system telemetry/resource governor.

Selection should prioritize low latency, Windows/mobile practicality, reproducibility, privacy/control, Ukrainian quality, accessibility and replaceability.

This vision is additive. Current Nika V0.1 release work remains the immediate priority; the living-agent/voice architecture should be introduced in staged increments without destabilizing the first usable release.

## Owner clarification 2026-10-16 — standby voice, autonomy, realtime embodied Nika

Canonical delivery ownership: Google Drive NikaCore Plan 5 Section 2A–2C (Personal Nika), Plan 4 Sections 3A–3C (browser) and 4A–4C (Windows UIA); 12-6 AI Plan 10 Section 1A–1C owns compatibility, not a duplicate Windows implementation. This is a binding product target only; no implementation/section DONE status is implied.

### Always-available, credit-free *standby*

- Nika starts with Windows when explicitly enabled, remains as a small local process and monitors the microphone with a **local wake-word engine** (e.g. “Ніка”) without invoking paid LLM/ASR/TTS/API services, uploading raw ambient audio or charging tokens while untriggered. CPU/energy use is not literally zero.
- User-selectable modes: **wake-name required** (default), continuous conversation *after* an authorized wake event with explicit session timeout, and hard microphone disable; visible/audible status, revocable permission and hardware/software mute. Other voices and the NVDA/system speaker output must not silently invoke owner-privileged actions.
- Separate local wake detection, speech activity, optional speaker confidence, active speech recognition and model/inference. Owner-away background jobs retain independent task budgets; a scheduled model call may cost resources even if wake detection is idle.

### Natural two-way conversation

- Full-duplex/low-latency streaming voice, barge-in while Nika speaks, echo suppression, non-speech/backchannel false-interruption handling and optional agent-initiated conversational interruptions.
- Distinguish **stop speaking**, **pause/cancel an in-flight tool effect**, **change current conversational request** and **stop a durable background job**. Never confuse cancelling audio with cancelling a task; report whether an external side effect was already committed. Preserve memory, interrupted turn context and task receipts.
- Support context-driven expressive voice/prosody (smile, laughter, natural hesitation, emotion-like tones), with no deceptive assertion of literal sentience or human emotion.

### Continuous owner-away tasks and self-operation

- Voice can open/use active Chrome tabs, applications, files, media and NikaCore's own agent/research/model-provider/task configuration by using existing typed browser/UIA/action/model/credential authorities.
- Support durable multi-step and recurring goals (“check authorized email/messaging sources every two minutes while I am at classes; process according to the approved policy”), explicit user-selected cadence, quotas, long-running state, idempotent side effects, evidence/reporting, owner return downshift, restart recovery and restrictions for permissions/credentials. Do not promise a two-minute cadence when offline, suspended or rate-limited.
- No unrestricted administrator rights or silent irreversible external sends/deletes, regardless of spoken phrasing; keep canonical approval and tool permissions.

### Optional photorealistic embodiment and personal digital twin

Two distinct modes share the same Nika identity and cognition, but NOT the same latency or rendering acceptance criteria:

1. **Interactive Live Video Nika**: a consenting fictional adult avatar with natural speech lip sync, eye/head/gaze/blinks, smiles, gesture and situational expression; optional hair/upper/full-body animation, background and sitting/standing/reclining scenes, with realistic expectations of renderer capabilities. Render as a replaceable local GPU or paid cloud adapter; never assume speaking-head animation gives arbitrary cinematic full-body behavior.
2. **Scripted content producer**: the owner's consented face/voice likeness as a digital twin for recorded videos, with user-specified clothes/background (e.g. office presenter), gesture and facial dynamics, script-driven revisions, video/audio export and provenance/disclosure for distribution. Keep source biometric assets controlled and do not permit unauthorized third-party identity cloning.

Audio-only must remain fully usable with NVDA; avatar video must not be required for any core command. Resource/budget profiles, provider switching, speaker/face consent records and user-visible live/video indicators are required.

### Acceptance and release truth

Demonstrate physical Windows 11/Chrome/NVDA scenarios: wake-only standby with zero paid idle calls, correct mic privacy behavior, owner vs stranger voice, interrupt TTS and recover same task, mutate only selected browser/app target, durable 2-minute owner-away task with failure/restart/idempotent action receipts, change Nika's own approved config, expressive speech, measured live AV latency/sync and separate scripted digital-twin export/consent. Hosted mocks/plan prose are not human/physical proof; map each gap to existing owners/PRs, preserve section numbering and completion records.

