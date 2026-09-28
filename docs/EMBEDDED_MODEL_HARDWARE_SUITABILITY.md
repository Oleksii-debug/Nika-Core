# Embedded model hardware suitability

This boundary answers one narrow question: whether the declared resource requirements for one exact
embedded model artifact are compatible with one observed machine profile. It is a read-only decision
surface. It does not load, benchmark, download, install, select, route, or promote a model.

## Reuse boundary

`EmbeddedModelSuitabilityEvaluator` consumes the existing `ResourceObserverPort` snapshot and the
provider-neutral `ModelArtifactDescriptor` / `ModelArtifactResources` provenance contract. It does
not add a second resource manager, hardware monitor, model registry, or Model Engineering Lab.
Optional GPU/VRAM facts are supplied as `AcceleratorEvidence`; the evaluator does not invent a GPU
probe where Nika has no shared GPU observation contract.

The Model Engineering Lab remains responsible for benchmark/evaluation evidence. This evaluator may
consume an externally supplied runtime estimate, but it never derives an estimated latency from RAM,
model size, CPU count, GPU presence, or any other heuristic. A configured runtime limit without such
an estimate therefore produces `MAYBE`, not fabricated benchmark evidence.

## Classification

The report schema is `nika.embedded_model_suitability.v1` and emits exactly one classification:

- `SUPPORTED`: every declared hard requirement that applies to the current state is observed and
  satisfied, with no unresolved recommendation/runtime uncertainty. This is resource-contract
  support, not proof of successful or fast inference.
- `MAYBE`: hard requirements are satisfied, but a soft practical condition is unresolved or below a
  recommendation, for example missing runtime-estimate evidence or RAM below the model's recommended
  level while still above its hard minimum.
- `UNSUITABLE`: at least one observed hard requirement is violated, such as insufficient RAM, required
  VRAM, CPU architecture/count, disk for an uncached model, or a supplied runtime estimate exceeding
  the caller's limit.
- `UNKNOWN`: Nika lacks evidence needed to evaluate a hard requirement, or the artifact does not
  declare enough hardware requirements to justify `SUPPORTED`.

`reason_codes` are stable machine-readable codes. The report identifies the exact model provenance by
its descriptor digest instead of copying model source/license material into the suitability result.

## Resource semantics

RAM is evaluated only against declared `min_system_memory_bytes`, `min_available_memory_bytes`, and
`recommended_memory_bytes`; model size is never treated as a proxy for required RAM. CPU architecture
uses normalized machine architecture (`AMD64`/`x64` -> `x86_64`, `ARM64` -> `aarch64`) against the
artifact's declared architectures. An optional caller-owned `min_logical_cpu_count` can express a
practical CPU floor without hard-coding one PC.

A declared `min_vram_bytes` is hard evidence: unknown GPU availability or unknown VRAM yields
`UNKNOWN`; no GPU or insufficient VRAM yields `UNSUITABLE`. If the artifact declares no VRAM minimum,
missing accelerator evidence does not manufacture a GPU requirement.

Disk is observed at the caller-supplied model storage location. For an uncached model, known model size
must fit available disk; unknown size/capacity yields `UNKNOWN`. A cached model is not rejected merely
because current free disk is below the artifact size, because this evaluator performs no acquisition.
No code in this boundary calls Foundry download APIs or any network transport.

## Runtime estimate truth

`estimated_runtime_ms` is optional input evidence supplied by the caller from a real measurement or an
otherwise authorized evidence source. The evaluator records the supplied number verbatim after finite,
non-negative validation. It never creates an estimate. If `max_estimated_runtime_ms` is configured and
no estimate is supplied, classification is at most `MAYBE`; if the supplied estimate exceeds the limit,
it is `UNSUITABLE` for that declared practical constraint.

## Physical acceptance boundary

Deterministic unit tests use fake resource, disk, architecture, accelerator, and runtime profiles; they
prove classification semantics only. They are not physical Windows/Foundry benchmark evidence.
A real-machine acceptance run must separately capture the exact descriptor/model identity, current
resource snapshot, applicable GPU/VRAM evidence where available, disk capacity, and a real inference
measurement if runtime suitability is to be claimed. Ordinary suitability evaluation must never trigger
model acquisition.
