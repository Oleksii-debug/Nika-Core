# Loop-C real PEFT trainer worker

This document defines the first real local training backend for the existing Loop-C
\`SubprocessTrainingWorker\`. It is an adapter, not a second training runtime. Durable admission,
resource gating, scale authorization, checkpoint uncertainty handling, evaluation, promotion and
activation remain owned by Nika Core.

## Product truth

The worker can perform one real LoRA optimizer step per protocol-v3 call when a compatible local
Hugging Face causal-LM base model and the declared Python packages are installed. It is deliberately
CPU-only in this first backend so execution is deterministic and does not silently choose a GPU or
quantization path. Large models may be impractical on CPU; the existing Loop-C scale authority must
start with a small pilot and only advance after completed training plus promoted evaluation evidence.

Adding this worker does **not** by itself prove a physical training run, useful weights, a promoted
candidate, or release readiness. Those require exact run evidence on a machine with the declared
model bytes and dependencies.

## Reuse decision

The implementation follows the repository \`REUSE -> ADAPT -> CUSTOM (thin)\` rule:

- PyTorch owns tensor/autograd/AdamW execution.
- Hugging Face Transformers owns local causal-LM/tokenizer loading.
- Hugging Face PEFT owns LoRA injection, adapter resume and safetensors persistence.
- Nika's existing \`SubprocessTrainingWorker\` remains the authority for protocol-v3 request identity,
  process containment, bounded I/O and consumed-material attestation.
- Nika's existing \`TrainingRuntime\` remains the authority for scale authorization, resource
  admission/revalidation, durable checkpoints and unknown-effect reconciliation.
- The custom code is only the isolation/provenance adapter needed to bind those maintained
  libraries to Nika's exact training contract.

The package versions are **not** added to the normal project dependencies yet. A heavyweight
training stack should graduate only after a physical pilot proves the exact environment. The worker
therefore requires exact package versions in its bound config and refuses version drift.

## Files

- \`scripts/loop_c_peft_trainer.py\` — isolated protocol-v3 worker and base-manifest builder.
- \`scripts/fixtures/loop_c_peft_cpu.example.json\` — example deterministic CPU config.
- \`tests/test_loop_c_peft_trainer.py\` — dependency-free protocol/provenance/recovery regressions.

## Frozen base model and tokenizer authority

The worker never downloads a model. \`base_model_root\` must be a fully local Transformers model
directory. Before training, generate a manifest **outside** that directory:

\`\`\`powershell
python .\scripts\loop_c_peft_trainer.py --build-base-manifest \
  "C:\NikaTraining\pilot-base" \
  "C:\NikaTraining\pilot-base.manifest.json"
\`\`\`

The command prints the manifest SHA-256. That digest is the \`TrainingJobSpec.base_artifact.sha256\`
for this worker. The manifest lists every regular file under the model root with exact relative
path, byte count and SHA-256. Model and tokenizer files therefore share one frozen authority.

The worker fails closed if the tree contains a symlink/reparse point; a file changes; an undeclared
file appears; a declared file disappears; manifest path/digest/size is non-canonical; or the
manifest digest differs from the job's base artifact digest.

Keep the generated manifest outside the model directory. If it is written into the model directory,
it becomes an extra undeclared file and verification correctly fails.

## Training data contract

Protocol-v3 already supplies exact resolved training/validation paths and frozen SHA-256 evidence.
The PEFT worker opens and consumes those bytes itself, validates size and digest from the same read,
parses strict UTF-8 JSONL, and independently reconstructs \`consumed_materials_sha256\`.

Each non-empty JSONL line must currently contain exactly:

\`\`\`json
{"prompt":"text","response":"text"}
\`\`\`

\`response\` must be non-empty. Metadata or alternate schemas are intentionally rejected rather than
silently ignored. Dataset-schema expansion should be a versioned contract change.

Validation bytes are consumed and schema-validated on each step so the attestation covers the exact
frozen package. Optimizer updates use only \`training\` records. Held-out product evaluation remains
the separate canonical Loop-C evaluation stage; this worker does not award itself evaluation or
promotion credit.

## Deterministic execution config

Production mode receives exactly two absolute command-file arguments:

\`\`\`text
python.exe loop_c_peft_trainer.py CONFIG.json BASE_MANIFEST.json
\`\`\`

Under \`SubprocessTrainingWorker\`, the Python executable is a \`training_executable\`; the script,
config and base manifest must each be bound as command artifacts. The existing execution-plan
digest therefore binds the executable, worker code, config bytes and manifest bytes.

The config explicitly binds exact \`torch\`, \`transformers\` and \`peft\` versions; CPU device; seed;
PyTorch thread count; sequence length; micro-batch; gradient accumulation; learning rate; weight
decay; LoRA rank/alpha/dropout and target modules; prompt separator; immutable base-model root; and
output root.

The worker sets offline mode and calls Transformers with \`local_files_only=True\` and
\`trust_remote_code=False\`. Network/model-hub resolution and model-supplied Python code are outside
this backend.

## Step, checkpoint and restart semantics

One Nika training step equals one optimizer update. Training examples are selected deterministically
and cyclically using the durable \`next_record_index\`.

After each successful step the worker atomically publishes an adapter, optimizer state, step
metadata and a self-verifying \`checkpoint_manifest.json\` below
\`OUTPUT_ROOT/JOB_FINGERPRINT/checkpoint-NNNNNNNN\`. The adapter config is rewritten so
\`base_model_name_or_path\` contains Nika's opaque base artifact reference rather than a machine-local
path.

Resume state contains only bounded identifiers/digests, not the physical output path: schema
version, completed step count, checkpoint ID, checkpoint manifest SHA-256 and next record index.
Before a resumed optimizer effect, every checkpoint byte is rehashed and compared with durable
resume evidence. Tampered/missing checkpoint bytes fail before the training backend runs.

When \`step_index + 1 == job.max_steps\`, the response sets \`completed=true\`, returns the
adapter-tree SHA-256 as \`candidate_sha256\`, and writes \`candidate.json\` under the job output
directory. That handoff contains the requested \`candidate_artifact_ref\`, digest and relative
adapter path. It does not perform promotion or activation.

## Provisioning a pilot on Windows

Use a dedicated virtual environment rather than the packaged Nika runtime:

\`\`\`powershell
py -3.12 -m venv "C:\NikaTraining\venv"
& "C:\NikaTraining\venv\Scripts\python.exe" -m pip install --upgrade pip
\`\`\`

Install the CPU build of PyTorch using the command generated by the official PyTorch “Start
Locally” selector for Windows / Pip / CPU. Then install stable Transformers and PEFT versions and
record the **exact installed versions** in the bound config.

At implementation time, the current upstream pages exposed PyTorch stable 2.7.0, Transformers
stable 5.17.0 and PEFT stable 0.21.0. Re-check upstream before a physical pilot, then bind the exact
installed versions; the worker refuses drift.

The primary user's Ryzen 5 / 16 GB Windows machine is suitable only for a genuinely small pilot
model. Do not claim an 8B LoRA run is practical on that hardware without measured evidence. The
scale plan, not the worker, decides when larger compute is authorized.

## Required physical acceptance gate

Source can become \`IMPLEMENTED\` after exact tests and repository CI are green.
\`REAL_TRAINING_RUN_PROVEN\` remains false until a physical pilot records:

1. frozen model/tokenizer manifest and matching job base digest;
2. real training and validation JSONL from the frozen learning package;
3. exact training environment versions;
4. at least two optimizer steps with a process exit and durable resume between them;
5. checkpoint-mutation negative proof before trainer effect;
6. completed adapter SHA-256 matching the candidate handoff;
7. held-out evaluation through the canonical evaluation path;
8. promoted challenger evidence before next-tier authorization;
9. resource-pressure pause/restart preserving canonical runtime state;
10. no secret, model weights, dataset bytes or generated checkpoints committed to Git.

Until those are demonstrated, keep
\`REAL_TRAINING_RUN_PROVEN=false\`,
\`TRAINING_WEIGHTS_PROVEN=false\`,
\`PRODUCTION_RELEASE_READY=false\`.
