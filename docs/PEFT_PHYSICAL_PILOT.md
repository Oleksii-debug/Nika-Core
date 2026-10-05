# PEFT physical pilot evidence

This harness is an acceptance tool for the canonical Loop-C training stack. It does not
implement another trainer, checkpoint store, artifact registry, physical artifact verifier,
evaluation engine, or promotion authority.

## What it proves mechanically

`nika_core.training_physical_pilot.run_physical_training_pilot` is intentionally
Windows-only. A successful call must:

1. run the canonical `TrainingRuntime` with the canonical
   `SubprocessTrainingWorker`;
2. execute one trainer step and persist a `PAUSED` checkpoint at `next_step == 1`;
3. construct new runtime and worker objects through the supplied restart factories;
4. require the restarted worker to expose the same execution-plan digest;
5. resume the same authorized job to `COMPLETED`;
6. require a distinct durable completion checkpoint;
7. reverify the final candidate through the existing canonical
   `training_artifacts.verify_candidate_artifact` boundary; and
8. require the verifier receipt SHA-256 to equal the completed runtime evidence.

The resulting `PhysicalTrainingPilotReport` is path-free. It contains bounded identifiers,
SHA-256 identities, descriptor/registry digests, checkpoint IDs, candidate byte count,
completed step count, schema version, and the literal platform value `windows`. It does not
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

The restart factory must reopen durable state rather than returning the original
`TrainingRuntime` object. The worker restart factory must construct a new
`SubprocessTrainingWorker` from the same Registry-bound command and environment authority.

The final candidate descriptor cannot be known before a first real training run completes.
Pass a `candidate_descriptor_factory` that receives detached canonical COMPLETED evidence.
Only then should it create or retrieve the SHA-256 `ModelArtifactDescriptor` for the final
published `adapter_model.safetensors`; its size and digest must describe those exact bytes.
The factory may use the completed candidate digest plus the now-materialized file size and the
project's canonical public provenance/license references. The harness then delegates physical
byte/containment/Windows-handle verification to the existing candidate-artifact integrity
authority rather than implementing another verifier.

Keep generated report JSON outside Git when it contains run-specific operational identifiers.
A report can be shared as evidence after reviewing it for the intended run.

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
report_path.write_text(report.to_json(), encoding="utf-8")
print(report.evidence_sha256)
```

The helper itself supplies the control sequence required to create the one-step pause.

## Evidence boundaries

A unit test, green CI run, or merely constructing a report is not physical-training proof.
The physical acceptance claim requires an observed successful run on the intended Windows/CPU
ML environment with the exact Registry/runtime/material authorities that the report binds.

This harness never sets or implies `HUMAN_TESTED`, `NVDA_VERIFIED`,
`TRAINING_WEIGHTS_PROVEN`, old-vs-new model superiority, promotion eligibility, or
`PRODUCTION_RELEASE_READY`. Those remain separate gates.
