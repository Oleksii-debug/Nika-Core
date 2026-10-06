# Physical old-vs-new PEFT evaluation

This runbook is the execution boundary after a successful repository-native
`nika-peft-physical-pilot` run. It does not define another evaluator, benchmark,
Experiment Engine, promotion algorithm, activation route, trainer, or checkpoint
format. The command composes the incumbent authorities and persists only minimized
comparison evidence.

A green CI run does not prove physical training or model improvement. The physical
truth flags remain false until an observed Windows run uses the intended lawful
model, material, and evaluator bytes.

## Preconditions

Use the exact output directory created by `nika-peft-physical-pilot`. It must still
contain:

- `physical-pilot.sqlite3`;
- `physical-pilot-report.json`;
- the exact published candidate artifact referenced by the report.

Keep the original frozen learning package and exact base-model bytes. Prepare a
held-out evaluation-set JSON whose content digest is the `evaluation_set_sha256`
already frozen into that package. Do not reuse training or validation examples as
held-out evidence.

The evaluator command must implement the existing
`RegistrySubprocessLoadedModelAttestor` stdin/stdout protocol. Its executable and
every absolute command-file argument are registered in Artifact Registry before
model effects. Champion and challenger use the same exact evaluator command
authority. The child process receives a sterile explicit environment; parent API
keys, tokens, cookies, and other environment credentials are not inherited.

## Local manifest

Save the following shape as a local UTF-8 JSON file. Do not commit a real manifest
when it contains private local paths.

```json
{
  "schema_version": 1,
  "workspace_id": "pilot-workspace",
  "project_id": "pilot-project",
  "owner_id": "pilot-owner",
  "physical_pilot_output_root": "C:\\NikaRuns\\pilot-001",
  "frozen_package_path": "C:\\NikaData\\pilot-package.json",
  "base_artifact_ref": "models/base",
  "base_model_path": "C:\\NikaModels\\base.gguf",
  "candidate_model_path": "C:\\NikaRuns\\pilot-001\\candidate-key\\adapter_model.safetensors",
  "base_model": {
    "provider_id": "local-base",
    "model_id": "base-model",
    "model_version": "exact-base-version",
    "source_reference": "https://example.invalid/model-source",
    "license_reference": "https://example.invalid/model-license",
    "capabilities": ["text"]
  },
  "candidate_model": {
    "model_id": "nika-pilot-adapter",
    "source_reference": "https://example.invalid/model-source",
    "license_reference": "https://example.invalid/model-license"
  },
  "evaluator": {
    "executable": "C:\\NikaEvaluator\\evaluator.exe",
    "command_files": [],
    "switches": [],
    "provenance_ref": "https://example.invalid/evaluator-source",
    "license_ref": "https://example.invalid/evaluator-license"
  },
  "evaluation_set_path": "C:\\NikaData\\held-out.json",
  "experiment_id": "physical-old-new-pilot-001",
  "permission_fingerprint": "evaluation-read-only",
  "benchmark": {
    "timeout_seconds": 60.0,
    "temperature": 0.0,
    "scorer_id": "exact-match-nfc-v1"
  },
  "policy": {
    "primary_metric": "model_quality_score",
    "minimum_improvement": 0.0,
    "minimum_replays": 1,
    "primary_higher_is_better": true,
    "guardrails": [
      {
        "metric": "model_task_pass",
        "higher_is_better": true,
        "max_regression": 0.0
      }
    ]
  }
}
```

The candidate descriptor is reconstructed from the physical-pilot report plus the
three public `candidate_model` fields. Its descriptor digest and Registry key must
match the exact values already written by the pilot. This prevents changing the
candidate route/provenance between training and evaluation.

The base descriptor is reconstructed from the exact physical base bytes and the
`base_model` public metadata. The canonical evaluation binding hashes and
revalidates the base and candidate bytes before any evaluator process starts.

If the evaluator needs immutable command files, list them in `command_files`.
They are appended immediately after the executable and each file is independently
registered. `switches` accepts only simple `--option` tokens. Paths, values,
credentials, or arbitrary positional arguments are deliberately not admitted
through unbound switches.

## Held-out evaluation-set file

The separate evaluation-set JSON is local evidence and may contain private prompts.
It has this strict shape:

```json
{
  "evaluation_set_id": "held-out-physical",
  "version": "v1",
  "provenance_ref": "dataset:held-out-physical",
  "license_ref": "license:held-out",
  "purpose": "held_out",
  "privacy": "private",
  "cases": [
    {
      "case_id": "case-001",
      "messages": [
        {"role": "user", "content": "held-out prompt"}
      ],
      "expected_text": "expected answer",
      "pass_score": 1.0,
      "weight": 1.0
    }
  ]
}
```

Duplicate JSON keys, unknown fields, non-finite numbers, an empty case set, or any
purpose other than `held_out` fail closed. The file is bounded before parsing.
Prompt and expected-answer text are not copied into the generated comparison
report.

## Run

From the installed Nika environment on the intended Windows machine:

```powershell
nika-peft-physical-evaluation "C:\NikaData\physical-evaluation.json"
```

The driver first verifies the pilot report, frozen package, held-out identity,
physical model descriptors, unique pilot task, and terminal completion checkpoint.
It reconstructs the exact training job and lets
`bind_training_result_for_evaluation` recompute the job fingerprint. No model
subprocess is started if those authorities disagree.

Immediately before the durable evaluation attempt is claimed, before champion
inference, before challenger inference, and before the durable Experiment Engine
comparison, the latest training checkpoint is re-read and must still equal the
completed physical-pilot report.

Before the first model inference effect, the command derives two related durable
identities. The outer `IdempotencyLedger` operation key is a hash of the physical
model effects: physical-pilot evidence, exact training/champion bindings, candidate
identities, held-out set, benchmark config, and the exact Registry-bound evaluator
attestor ID/SHA-256. The operation key deliberately does **not** depend on the
requested `experiment_id`, promotion policy, or permission fingerprint. Those
comparison inputs are instead bound into the ledger input fingerprint. Changing a
label, policy, or permission value therefore conflicts with the existing effect
reservation instead of creating a route to repeat the same inference.

After the outer reservation is won, the canonical Experiment Engine persists an
exact definition under a deterministic physical attempt ID and transitions it to
`running` before inference starts. Concurrent callers get one ledger winner; all
other callers fail closed before model effects.

If an interrupted run leaves the ledger `pending` or `uncertain`, a later
invocation refuses to repeat champion or challenger inference. Preserve
`physical-pilot.sqlite3` and treat that attempt as **inconclusive/unknown** until
the side-effect record is reconciled. Do not delete ledger/Experiment rows or change
`experiment_id` merely to bypass the fence. If prior effect state cannot be
independently reconciled, keep `OLD_VS_NEW_MODEL_EVALUATION_PROVEN=false` and
perform a completely fresh physical pilot/evaluation run in a new output root.

After a terminal comparison, the minimized report payload is first committed as the
exact COMPLETED idempotency result. Only then is the JSON report published. A crash
between those two steps is recoverable: the next invocation revalidates the current
pilot/model/evaluator authority, cross-checks the stored result against the terminal
Experiment snapshot, and republishes the same payload **without new inference**.

On success the command prints a minimized JSON payload and atomically creates:

`physical-old-new-evaluation-report.json`

inside `physical_pilot_output_root`. Report schema v2 contains the requested
experiment label, deterministic physical attempt ID, evidence digests, Experiment
Engine status/selection IDs, benchmark/binding evidence digests, attestor identity,
and the canonical Experiment definition/observations digests plus observation count.
Those Experiment identities are part of the aggregate attested-comparison digest, so
restart recovery fails closed if terminal Experiment evidence drifts after evaluation.
The report does not contain model paths, evaluator paths, held-out prompts, expected
answers, environment variables, credentials, or model bytes.

A legacy schema-v1 COMPLETED ledger result remains readable only for ordinary
same-evaluation recovery after an interrupted report publication. It does not contain
the Experiment evidence identities required to restore a trusted scale-progression
proof, so it cannot authorize a new higher training tier after restart. Re-run the
physical evaluation under the current schema before attempting scale progression;
do not synthesize missing v2 digests.

If that report already exists, the command refuses to repeat model effects. A
`promoted` Experiment Engine status is selection evidence only; it is not model
activation or release qualification. Activation remains owned by the separate
canonical activation authority.
