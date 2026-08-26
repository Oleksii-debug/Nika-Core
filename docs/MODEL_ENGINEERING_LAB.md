# Model Engineering Lab

Status: foundation candidate. This document does not claim that a Nika-owned language model has already been trained.

## Purpose

Nika needs a separate model-development plane for designing, training, evaluating, and promoting Nika-owned checkpoints without turning model training into the platform kernel.

The boundary is deliberate:

- `ModelGateway` remains the provider-neutral inference boundary.
- `model_lab` owns reproducible model architecture and training-plan evidence.
- M8 Controlled Experiments remains the champion/challenger evaluation and promotion mechanism.
- GPU training runs in an isolated training environment rather than adding PyTorch and large training dependencies to the base Nika runtime.
- weights, raw datasets, credentials, and large checkpoints stay outside the Git repository.

## Reuse decision

REUSE -> ADAPT -> CUSTOM thin still applies.

Primary training backend candidate: OLMo-core. It is a PyTorch library for large-scale distributed training and already exposes a `ModelLadder` abstraction, matching Nika's need to prove a small scale before moving to larger scales.

Secondary scale backend candidate: TorchTitan. It provides PyTorch-native distributed training primitives including FSDP, tensor, pipeline, and context parallelism, distributed checkpointing, and training observability. It remains a later adapter rather than the first dependency because the project is still under extensive development and its latest feature path commonly tracks recent PyTorch builds.

Nanotron was evaluated but is not selected for the in-process Nika environment in this stage. Its current first-training guide documents Python below 3.12, while Nika Core requires Python 3.12-3.13. It can still be revisited as an isolated container/backend if that compatibility boundary changes.

No broad training framework is reimplemented inside Nika Core.

## Dense scaling ladder

The first reviewed ladder uses one 32,000-token vocabulary and tied embeddings across stages so model-size comparisons do not silently change tokenizer vocabulary.

| Stage | Hidden | Layers | Attention heads | KV heads | SwiGLU intermediate | Estimated parameters |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| pilot-20m | 320 | 9 | 8 | 4 | 832 | 20,199,360 |
| core-100m | 768 | 12 | 12 | 4 | 2,048 | 100,092,672 |
| scale-1b | 1,920 | 24 | 24 | 8 | 5,120 | 1,005,252,480 |

The estimator is explicit rather than marketing-rounded. It models a bias-free, RMSNorm/SwiGLU decoder with grouped-query attention and tied input/output embeddings. A backend implementation must prove its realized parameter count against this planning estimate before a run receives evidence credit.

## Data and token-budget rule

The historical Chinchilla compute-optimal result is retained only as a planning reference: scale training tokens with model parameters, with roughly 20 tokens per parameter as a useful baseline reference for a first compute study. That would correspond to about 400M tokens for 20M parameters, 2B for 100M, and 20B for 1B.

That reference is not a quality ceiling or an automatic Nika training target. Modern small-language-model work can train far beyond it; for example, the published SmolLM2 135M model reports training on 2T tokens. Dataset quality, deduplication, curriculum/mixing, evaluation, and available compute therefore decide the real budget.

`ModelTrainingPlan` requires the real token budget to be explicit. It never silently substitutes the reference ratio.

## Fail-closed training evidence

A run is not training-ready unless all of these are true:

1. the realized model-size estimate is inside the declared target tolerance;
2. tokenizer identity, immutable version, SHA-256, vocabulary size, and license are recorded;
3. every dataset source has immutable identity/version, SHA-256, license, token count, and an explicit training-permission decision;
4. the training plan is bound to an exact 40-character Git commit SHA;
5. the token budget fits the unique corpus unless dataset repetition is explicitly enabled;
6. the random seed and external backend are explicit.

The model lab stores metadata/evidence only. It must not persist dataset credentials, raw secret-bearing source URLs, or model-provider secrets into durable audit records.

## Stage gates

### Gate A - 20M plumbing proof

Before an expensive run, the training adapter must prove model construction, exact parameter count, tokenizer/data compatibility, a tiny-batch forward/backward step, checkpoint save, process restart/resume, and deterministic evidence capture.

### Gate B - 20M pilot

Train the 20M stage on a reviewed corpus slice. Required evidence includes loss curve, tokens processed, wall-clock/accelerator utilization, checkpoint identity, validation perplexity/loss, and restart proof. A falling training loss alone is not a promotion signal.

### Gate C - 100M promotion

100M work starts only after the 20M pipeline is reproducible and evaluation shows the data/tokenizer/training recipe is worth scaling. Hyperparameters may be adapted, but every change is versioned rather than silently inherited.

### Gate D - 1B scale

1B work starts only with a proven scaling curve, explicit compute budget, checkpoint/recovery plan, data-volume plan, and evaluation suite. Distributed backend choice is made from measured hardware/resource requirements, not from parameter count alone.

## Integration with existing Nika systems

A completed checkpoint becomes a candidate model artifact, not an automatic default. Model identity/source/license/checksum/resource metadata follows the existing model-acquisition boundary. Evaluation should feed M8 Controlled Experiments, and only a proven candidate may be considered for a ModelGateway default/promotion decision.

This preserves the existing rule that language models are replaceable capabilities rather than the Nika platform kernel.

## Acceptance for this foundation

- no mandatory PyTorch/training dependency added to `nika-core`;
- deterministic parameter estimator with tests;
- reviewed 20M -> 100M -> 1B dense architecture ladder with tests;
- fail-closed data permission, tokenizer, checksum, code-revision, and token-budget contracts;
- exact candidate must pass repository Ruff, compile, and full pytest on Ubuntu and Windows before integration.

## Next implementation slice

Build an isolated OLMo-core training adapter that consumes `ModelTrainingPlan`, materializes a backend config without secrets, performs a dry-run/model-construction proof, records realized parameter count, and emits checkpoint/evaluation evidence that can later enter M8. Then wire a small reviewed tokenizer/corpus manifest and run the 20M plumbing gate before any full pretraining spend.

## Research references

- Hoffmann et al., *Training Compute-Optimal Large Language Models* (Chinchilla), 2022.
- Allen Institute for AI, OLMo-core documentation and public training scripts.
- PyTorch, TorchTitan repository and training documentation.
- Hugging Face, Nanotron first-training documentation.
- Hugging Face, SmolLM / SmolLM2 model documentation and training reports.
