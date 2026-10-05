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
4. require the restarted worker to expose the same execution-plan digest, trainer-protocol
   job fingerprint, and Registry-verified trainer deployment identity;
5. issue an effect-free `PAUSE` probe and require the restarted runtime to reopen
   the same job at `next_step == 1`;
6. require that probe to stop before resource admission and trainer effects;
7. persist a distinct restart-probe checkpoint;
8. resume the same authorized job to `COMPLETED`;
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
    and require its final step number to match the COMPLETED runtime boundary; and
16. bind both the runtime job fingerprint and the distinct trainer-protocol job fingerprint,
    plus the independently verified trainer deployment identity, manifest digest, trainer
    implementation, model directory, consumed-material, and runtime-manifest identities into
    the report; and
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
Artifact Registry before accepting its identity across the restart boundary.

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
in the destination directory, atomically links it into place without replacing an existing
report, re-verifies the exact canonical bytes, and removes temporary state on failure. A report
can be shared as evidence after reviewing it for the intended run.

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
effect-free restart probe. Reports use schema version 4 because the runtime job fingerprint and
the trainer-protocol job fingerprint are distinct authorities and are now recorded separately,
alongside strict candidate-manifest and persisted trainer/runtime provenance plus the
durable-reopen checkpoint.

## Evidence boundaries

A unit test, green CI run, or merely constructing a report is not physical-training proof.
The physical acceptance claim requires an observed successful run on the intended Windows/CPU
ML environment with the exact Registry/runtime/material authorities that the report binds.

This harness never sets or implies `HUMAN_TESTED`, `NVDA_VERIFIED`,
`TRAINING_WEIGHTS_PROVEN`, old-vs-new model superiority, promotion eligibility, or
`PRODUCTION_RELEASE_READY`. Those remain separate gates.
