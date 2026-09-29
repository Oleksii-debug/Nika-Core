# Model Resource Evidence Contract

Status: stacked Model Engineering Lab delta for ONE-SHOT DEV66.

## Scope

This contract makes benchmark CPU/RAM/GPU evidence machine-readable without
claiming measurements the active observers do not provide.

It reuses the existing `ResourceObserverPort`, `ResourceSnapshot`,
`AcceleratorObserverPort`, and `AcceleratorSnapshot`. It does not modify the
Resource Manager, provider routing, model selection, download/install behavior,
or Experiment Engine promotion authority.

## Canonical machine contract

`benchmark_report_json()` contains `resource_evidence` with schema
`nika-model-resource-evidence-v1`.

Every numeric resource metric is represented as:

- `status`: `observed` or `unknown`;
- `scope`: `host`, `nika_process`, or `gpu_device`;
- `unit`: `percent` or `bytes`;
- `value`: numeric only when `status=observed`, otherwise `null`;
- `unknown_reason`: `null` when observed, otherwise a stable reason code.

A real observed zero remains `status=observed, value=0`. Missing evidence is
never converted to zero.

Stable UNKNOWN reasons currently are:

- `resource_observer_not_configured`;
- `metric_unavailable`;
- `gpu_identity_unavailable`;
- `no_observed_sample`.

## CPU and RAM semantics

Live `main`'s shared `PsutilResourceObserver` reports host CPU percentage, host
RAM percentage, host available RAM bytes, and optional RSS bytes for the Nika
process. The incumbent #534 Model Engineering branch is based on an older
`ResourceSnapshot` shape that has only the three host measurements. This
evidence layer therefore capability-checks optional process RSS: if the field is
absent or unavailable it is `unknown` with `metric_unavailable`, never zero.

Those scopes are preserved literally. Host CPU/RAM are not labelled as
model-process consumption. Nika-process RSS, when available, is not labelled as
an external Ollama/Foundry/provider process measurement.

If the resource observer is not configured, the per-sample CPU/RAM metrics are
`unknown` with `resource_observer_not_configured`.

## GPU semantics

The Model Engineering foundation currently has a generic
`AcceleratorObserverPort`, but `AcceleratorSnapshot` does not attest whether
the observed accelerator is a GPU, NPU, or something else.

Therefore generic accelerator numbers remain available in the pre-existing
accelerator evidence, but **must not** be relabelled as GPU utilization or GPU
memory. `resource_evidence` reports GPU metrics as `unknown` with
`gpu_identity_unavailable`, even when an untyped accelerator observation
contains numeric values.

A later GPU-capable observer may populate GPU metrics only after its
provider-neutral contract attests GPU identity/scope. No vendor-specific SDK
or hardware name is required by this schema.

## Sampling boundary

The current benchmark runner safely calls point-snapshot observers twice per
case:

1. `before` the measured completion interval;
2. `after` completion or typed provider failure.

The resource schema records:

```json
{
  "mode": "bounded_point_snapshots",
  "capture_points": ["before", "after"],
  "during": "not_captured"
}
```

There is no fabricated `during` sample and no claim of continuous profiling.
Physical hardware measurements remain separate acceptance evidence.

## Deterministic proof

`tests/test_model_engineering_resource_evidence.py` uses fake observers and a
fake completion gateway. It proves:

- exactly two resource/accelerator snapshot calls for one benchmark case;
- observed CPU/RAM values preserve their scope and units;
- a real observed host-RAM value of zero remains observed while unavailable RSS
  remains UNKNOWN;
- missing observers produce UNKNOWN rather than zero;
- untyped accelerator readings do not become GPU evidence;
- invalid percent values and UNKNOWN-with-numeric-value constructions fail
  closed.

No real model, network, hardware probe, sleep, or production selection change is
used by these tests.
