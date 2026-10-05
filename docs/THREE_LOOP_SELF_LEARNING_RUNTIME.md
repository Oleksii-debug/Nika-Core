# Nika Core — three-loop self-learning runtime

## Purpose

This document makes the living-agent architecture executable at the runtime level by separating three processes that must cooperate but must never be confused.

## Loop A — operational agent loop

`state -> choose action -> execute -> observe -> verify -> persist -> choose next action`

This loop provides autonomy. It may run continuously while the system is available. It does **not** by itself change 12-6 neural weights.

Nika Core responsibilities:
- schedule/wake/resume;
- browser/files/devices/communications/tools;
- persistent task state;
- system-resource arbitration;
- capture observations/results;
- return structured evidence to 12-6 cognition.

## Loop B — cognitive learning loop

`experience -> compare with memory -> hypotheses/abstractions -> verification -> update memory/world-model/self-model/skills`

This loop lets Nika learn immediately from conversations, books, web research, tasks and external teachers without needing an optimizer update after every event.

Nika Core provides storage, retrieval, transcription, search, local/API teacher access and evidence capture; the 12-6 cognition layer owns the semantic update.

## Loop C — neural machine-learning loop

`verified experience -> candidate dataset -> verification pyramid -> frozen learning package -> bounded training -> evaluation old vs new -> promote/reject -> rollback available`

This is the loop that actually changes model weights.

Nika Core responsibilities:
- expose available CPU/GPU/RAM/storage/network;
- run authorized training jobs;
- checkpoint/resume jobs;
- keep old/new candidate artifacts separately;
- execute evaluation suites;
- return measured results and resource/cost evidence.

12-6 owns the training/evaluation semantics and promotion decision contract.

## Relation to reinforcement learning

A reinforcement-learning loop such as robot table-tennis training is a specialized learning loop:

`observe state -> act -> receive outcome/reward -> update policy -> repeat`

Nika may later use RL, preference learning, demonstrations or other post-training methods for particular skills. RL is therefore one possible mechanism inside Loop C, not a replacement for Loops A and B and not the definition of agent autonomy itself.

## OWNER_AWAY continuous development mode

When owner policy allows autonomous use of the computer, Nika Core should keep the system alive and dynamically schedule useful activities rather than run one uncontrolled infinite job.

Eligible activities include:
- unfinished user work;
- reading/research;
- self-tests;
- local-model debates;
- evidence verification;
- memory consolidation;
- knowledge reconciliation;
- training-data candidate preparation;
- bounded/authorized ML pilots;
- evaluation of candidate versions;
- idle/low-power waiting.

The 12-6 cognition layer chooses what has expected learning/task value; Nika Core checks whether resources allow it and executes it durably.

## Resource adaptation

If Oleksii returns and uses the computer, lower-priority background work should downshift, checkpoint or pause automatically. When resources become free again, it resumes from durable state.

This is a core autonomy requirement, not an optional convenience.

## Long-term target

Oleksii should be able to say a high-level instruction such as:

“While I am away, keep learning and improving; use the laptop without interfering when I return.”

The system should then autonomously manage operational work, cognitive consolidation and eligible machine-learning cycles, while reporting in ordinary language what it learned, what changed, what candidate model versions were tested and what remains unresolved.