# PEFT physical pilot evidence

This harness is an acceptance tool for the canonical Loop-C training stack. It does not
implement another trainer, checkpoint store, artifact registry, physical artifact verifier,
evaluation engine, or promotion authority.

## What it proves mechanically

`nika_core.training_physical_pilot.run_physical_training_pilot` is intentionally
Windows-only. A successful call must:

1. run the canonical `TrainingRuntime` with a canonical
   `SubprocessTrainingWorker` that starts without prior accepted consumed-material evidence;
2. execute one trainer step, persist a `PAUSED` checkpoint at `next_step == 1`, and
   capture the consumed-byte attestation accepted by the canonical subprocess worker;
3. construct new runtime and worker objects through the supplied restart factories;
4. require the restarted worker to expose the same execution-plan digest, trainer-protocol
   job fingerprint, and Registry-verified trainer deployment identity, with no prior accepted
   consumed-material evidence;
5. issue an effect-free `PAUSE` probe and require the restarted runtime to reopen
   the same job at `next_step == 1`;
6. require that probe to stop before resource admission and trainer effects and to leave the
   restarted worker without accepted consumed-material evidence;
7. persist a distinct restart-probe checkpoint;
8. resume the same authorized job to `COMPLETED` and require the resumed worker's accepted
   consumed-byte attestation to equal the first-step attestation;
9. require a third distinct durable completion checkpoint;
10. build the final candidate descriptor only from detached canonical completion evidence;
11. acquire a Windows read handle that denies write/delete replacement for the final
    candidate while evidence is collected;
12. reverify the final candidate through the existing canonical
    `training_artifacts.verify_candidate_artifact` boundary;
13. require the verifier receipt SHA-256 to equal the completed runtime evidence;
14. under the same stability lock, re-read the canonical strict self-contained PEFT
    candidate manifest through `training_peft_worker.candidate_adapter_manifest`;
15. require its base reference/digest and candidate reference to match canonical COMPLETED
    runtime evidence, require its trainer-protocol job fingerprint to match the exact
    `SubprocessTrainingWorker` protocol identity, require its trainer artifact ID and trainer
    deployment SHA-256 to match the worker's canonical Registry-verified deployment identity,
    require its consumed-material attestation to match the exact attestation accepted by the
    worker across the physical steps, and require its final step number to match the COMPLETED
    runtime boundary; and
16. bind both the runtime job fingerprint and the distinct trainer-protocol job fingerprint,
    plus the independently verified trainer deployment identity, manifest digest, trainer
    implementation, model directory, worker-accepted consumed-material, and runtime-manifest
    identities into the report; and
17. when persisted, publish the canonical report through the provided atomic/no-clobber
    writer rather than a direct truncating file write.

The resulting `PhysicalTrainingPilotReport` is path-free. It contains bounded identifiers,
SHA-256 identities, descriptor/registry digests, strict PEFT candidate-manifest digest,
separate runtime and trainer-protocol job fingerprints, trainer deployment/implementation/runtime
provenance, consumed-material and model-directory manifest digests, the original pause,
restart-probe, and completion checkpoint IDs, candidate byte count, completed step count,
schema version, and the literal platform value `windows`.
It does not
serialize training/validation records, model paths, credentials, environment variables,
prompts, responses, or checkpoint payloads.

## Required Windows setup

Use a disposable local training workspace. Install the repository with the `training` and
`dev` extras in the same pinned environment used to register the trainer executable.
Register the exact local trainer executable in the canonical Artifact Registry as
`training_executable`, including the required PEFT runtime-version metadata.

Resolve the frozen training package through the canonical training-material resolver. Build
the pilot-tier `TrainingScaleAuthorization` from the same material evidence and the
`SubprocessTrainingWorker.execution_plan_sha256`. The job must use `max_steps >= 2`.

The restart factory must reopen the same durable checkpoint state rather than returning the
original `TrainingRuntime` object or a new runtime backed by an empty store. The harness proves
that reopen before any resumed trainer effect by issuing a `PAUSE` control probe. The new
runtime must observe `next_step == 1`, persist a new checkpoint with
`reason == "paused_before_admission"`, and preserve the exact job identity. A fresh or wrong
store observes step 0 (or an identity mismatch) and is rejected. The worker restart factory must
construct a new `SubprocessTrainingWorker` from the same Registry-bound command and environment
authority. The harness re-verifies that trainer deployment through the worker's incumbent
Artifact Registry before accepting its identity across the restart boundary. It also requires
the new worker to start without accepted consumed-material evidence, proves the reopen probe
does not create such evidence, and then requires the resumed physical step to reproduce the
same consumed-byte attestation that the first worker accepted.

The final candidate descriptor cannot be known before a first real training run completes.
Pass a `candidate_descriptor_factory` that receives detached canonical COMPLETED evidence.
Only then should it create or retrieve the SHA-256 `ModelArtifactDescriptor` for the final
published `adapter_model.safetensors`; its size and digest must describe those exact bytes.
The factory may use the completed candidate digest plus the now-materialized file size and the
project's canonical public provenance/license references. The harness then delegates physical
byte/containment verification to the existing candidate-artifact integrity authority and strict
manifest parsing to the existing PEFT
candidate reader rather than implementing another verifier or parser. On Windows it holds a
read-only deny-write/delete handle across both checks so a pathname replacement cannot splice
manifest evidence from different candidate bytes.

Keep generated report JSON outside Git when it contains run-specific operational identifiers.
Persist it with `write_physical_training_pilot_report`, which writes a flushed temporary file
in the destination directory, holds the Windows parent directory against rename/delete while
publication is in flight, atomically links the temporary file into place without replacing an
existing report, verifies that the linked destination retains the exact staged file identity,
holds that destination against Windows write/delete replacement during byte and parse-back
verification, re-verifies the canonical bytes, and removes a failed destination only when it
still has the writer-owned file identity. A concurrent replacement is never deleted as rollback.
A report can be shared as evidence after reviewing it for the intended run.

## Repository-native Windows driver

The installed `nika-peft-physical-pilot` command composes the canonical authorities described
above. It is a Windows-only execution driver, not a second trainer, checkpoint format, candidate
manifest parser, report schema, or report publisher.

The local UTF-8 JSON manifest is bounded to 64 KiB. Before creating durable pilot state the
driver requires canonical non-linked input paths, a bounded frozen-package manifest, a valid
Windows PE trainer executable, a canonical local model-directory manifest, a fresh output
directory disjoint from the blob/model input authorities, PEFT-compatible LoRA target tokens,
canonical TrainingJobSpec identities, public logical artifact references, and a material record
count within the canonical PEFT one-million-record limit. Runtime versions for torch,
Transformers, PEFT, Accelerate, GGUF and safetensors are supplied explicitly and are registered
as trainer deployment metadata; the child independently verifies those exact installed versions
before training effects.

The driver registers the exact trainer executable, resolves the frozen training/validation bytes,
derives the two-step pilot scale authorization, applies one-concurrent ResourceManager admission,
and constructs fresh runtime/worker objects for the restart proof. After COMPLETED evidence exists,
its descriptor factory supplies only the public model provenance plus completed candidate digest
and byte size. Final candidate-byte verification, Windows stability locking, strict PEFT candidate
manifest evidence, runtime-vs-trainer job identity, and trainer deployment provenance remain owned
exclusively by `run_physical_training_pilot`. Report persistence is delegated exclusively to
`write_physical_training_pilot_report`.

Example manifest shape (replace every path, digest, public provenance reference, and exact
installed runtime version with values for the intended run):

```json
{
  "schema_version": 1,
  "workspace_id": "pilot-workspace",
  "project_id": "pilot-project",
  "owner_id": "pilot-owner",
  "job_id": "pilot-job-001",
  "blob_store_root": "C:\\NikaData\\blobs",
  "frozen_package_path": "C:\\NikaData\\pilot-package.json",
  "frozen_package_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "trainer_executable": "C:\\NikaVenv\\Scripts\\nika-peft-trainer.exe",
  "base_artifact_ref": "models/base",
  "base_gguf_path": "C:\\NikaModels\\base.gguf",
  "model_dir": "C:\\NikaModels\\transformers-base",
  "output_root": "C:\\NikaRuns\\pilot-001",
  "candidate_artifact_ref": "models/pilot-candidate-001",
  "candidate_descriptor": {
    "model_id": "nika-pilot-adapter",
    "source_reference": "https://example.invalid/model-provenance",
    "license_reference": "https://example.invalid/model-license"
  },
  "runtime_versions": {
    "torch": "EXACT_INSTALLED_VERSION",
    "transformers": "EXACT_INSTALLED_VERSION",
    "peft": "EXACT_INSTALLED_VERSION",
    "accelerate": "EXACT_INSTALLED_VERSION",
    "gguf": "EXACT_INSTALLED_VERSION",
    "safetensors": "EXACT_INSTALLED_VERSION"
  },
  "resource_budget": {
    "max_cpu_percent": 95,
    "max_memory_percent": 90
  },
  "trainer_parameters": {
    "max_sequence_length": 256,
    "learning_rate": 0.0002,
    "lora_r": 8,
    "lora_alpha": 16,
    "lora_dropout": 0.05,
    "lora_target_modules": ["q_proj", "v_proj"],
    "torch_num_threads": 2,
    "seed": 1729
  }
}
```

Run from the installed environment that owns the registered trainer executable:

```powershell
nika-peft-physical-pilot "C:\\NikaData\\physical-pilot.json"
```

A successful invocation prints the canonical path-free schema-v5 report and publishes
`physical-pilot-report.json` through the canonical atomic/no-clobber writer. Generated run
evidence remains outside Git. The command never auto-discovers runtime versions and never stores
API keys, tokens, cookies, browser profiles, or other credentials.

## Example control flow

The application or acceptance driver should perform the equivalent of:

```python
report = run_physical_training_pilot(
    runtime=runtime,
    restart_runtime=reopen_runtime,
    spec=job_spec,
    worker=worker,
    restart_worker=reopen_worker,
    scale_authorization=authorization,
    candidate_path=candidate_safetensors_path,
    candidate_descriptor_factory=build_candidate_descriptor_after_completion,
    candidate_root=candidate_artifact_root,
)
write_physical_training_pilot_report(report, report_path)
print(report.evidence_sha256)
```

The helper supplies both the control sequence required to create the one-step pause and the
effect-free restart probe. Reports use schema version 5 because the runtime job fingerprint and
the trainer-protocol job fingerprint are distinct authorities and are now recorded separately,
alongside strict candidate-manifest and persisted trainer/runtime provenance plus the
durable-reopen checkpoint.

## Evidence boundaries

The schema-v5 report and candidate-manifest-v2 evidence bind distinct pre-step and trained
canonical adapter tensor-state SHA-256 identities. Equal tensor-state identities fail closed
before durable checkpoint admission; exact serialized adapter-byte mutation remains an
independent required fence.


A unit test, green CI run, or merely constructing a report is not physical-training proof.
The physical acceptance claim requires an observed successful run on the intended Windows/CPU
ML environment with the exact Registry/runtime/material authorities that the report binds.

This harness never sets or implies `HUMAN_TESTED`, `NVDA_VERIFIED`,
`TRAINING_WEIGHTS_PROVEN`, old-vs-new model superiority, promotion eligibility, or
`PRODUCTION_RELEASE_READY`. Those remain separate gates.
