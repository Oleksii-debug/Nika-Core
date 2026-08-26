# Model Engineering Lab

Status: production-intended foundation lane `MANUAL-MODEL-LAB`.

## Product purpose

The Model Engineering Lab is Nika Core's controlled model-comparison surface. It evaluates
local, embedded and allowed cloud model candidates on versioned task sets without turning a
benchmark into autonomous provider reconfiguration or model promotion.

The binding product direction comes from `docs/FULL_PRODUCT_VISION_2026-08-19.md` and
`docs/WORKSPACE_REUSE_CATALOG_2026-08-19.md`.

## Ownership and reuse decision

This subsystem follows `REUSE -> ADAPT -> CUSTOM (thin)`.

REUSE:
- `nika_core.model_gateway` request/response and privacy/provider contracts;
- `ResourceObserverPort` for CPU and memory snapshots;
- the existing Experiment Engine `ExperimentDefinition`, `MetricObservation`,
  `PromotionPolicy`, `ReplayCase` and `StrategyRef` contracts;
- standard-library hashing, JSON, Unicode normalization and monotonic timing.

ADAPT:
- ModelGateway completion becomes one benchmark case execution with exact candidate identity;
- resource observations become bounded benchmark evidence;
- benchmark case metrics can be projected into the existing Experiment Engine.

CUSTOM (thin):
- versioned evaluation-set identity;
- separate engine/model provenance and license evidence;
- benchmark scoring and aggregation;
- optional accelerator telemetry port because the current shared resource snapshot has no GPU
  fields;
- secret-minimized machine evidence and a linear screen-reader-friendly text report.

There is no second model router, experiment engine, resource manager, database, downloader,
provider SDK or promotion authority.

## Evaluation identity

`EvaluationSet` binds:
- stable evaluation-set ID and version;
- provenance reference and dataset license reference;
- `development` or `held_out` purpose;
- Nika privacy class;
- ordered cases;
- each case's exact messages, expected text, pass threshold and weight.

The canonical UTF-8 JSON representation is hashed with SHA-256. Any prompt, expected answer,
threshold, weight, version, provenance, license, purpose or privacy change therefore changes the
evaluation-set identity.

Raw prompts and expected answers are needed in memory to execute the benchmark, but they are not
serialized into benchmark result evidence or accessible reports.

## Candidate identity and licensing

`ModelCandidate` keeps inference-engine evidence separate from model evidence:
- provider ID and provider kind;
- request model and exact expected response model;
- engine provenance reference;
- engine license reference;
- model provenance reference;
- model license reference;
- optional exact local model SHA-256.

The candidate evidence digest binds all of those fields. A model artifact hash is optional because
some allowed remote model services do not expose immutable model bytes. Missing model bytes must
not be misreported as checksum-verified provenance.

Ordinary benchmark execution has no model-download authority. Optional model installation remains
a separate explicit product action governed by the ModelGateway/Foundry model-download contracts
and the existing license/provenance gates.

## Execution semantics

`ModelBenchmarkRunner` executes candidates sequentially by default so one candidate does not
silently contaminate another candidate's latency/resource evidence.

Each case:
1. captures optional CPU/memory and accelerator snapshots;
2. creates a deterministic benchmark request ID bound to candidate evidence, evaluation-set
   evidence and case ID;
3. calls the existing ModelGateway-compatible completion port with an exact provider ID and model;
4. records typed `ModelGatewayError` failure without persisting exception text;
5. validates response request/provider/provider-kind/model identity before scoring;
6. validates token usage without Boolean or numeric coercion;
7. hashes response text instead of persisting response text;
8. scores through a narrow scoring port;
9. captures final telemetry.

The default scorer is deterministic Unicode NFC exact match. Domain workspaces can supply another
bounded scorer without modifying routing, permissions or provider adapters.

## Metrics

Per candidate the current foundation records:
- weighted quality score;
- task pass rate;
- completion rate;
- mean latency;
- nearest-rank P95 latency;
- peak observed CPU percent;
- peak observed memory percent;
- minimum observed available memory;
- optional peak accelerator utilization;
- optional peak accelerator memory usage.

CPU/memory/accelerator measurements are bounded point snapshots around each case. They are useful
evidence, not a claim of continuous high-frequency profiling. A future physical performance lane
may adapt an existing maintained telemetry source behind the same narrow ports if continuous GPU
or contention profiling is required.

## Experiment Engine bridge

The Model Engineering Lab does not promote a model itself.

`build_experiment_definition()` creates only an existing Experiment Engine definition and refuses
promotion-oriented definitions unless the evaluation set is explicitly `held_out`.

`benchmark_observations()` can project case evidence into these bounded metrics:
- `model_quality_score`;
- `model_task_pass`;
- `model_completion_success`;
- `model_latency_ms`.

The existing Experiment Engine remains responsible for coverage checks, champion/challenger
comparison, guardrails, promotion state and rollback state. Permission fingerprints are supplied
by the trusted caller and are never inferred from benchmark output.

## Evidence and accessibility

Machine evidence uses versioned JSON schemas and SHA-256. It contains candidate/evaluation
identity, numeric metrics, resource observations, typed error codes and response hashes. It does
not contain:
- prompt text;
- expected answer text;
- generated response text;
- provider exception text;
- API keys, cookies or credentials.

`render_text_report()` emits a simple linear report suitable for NVDA review. Automated tests may
validate its structure but never set `HUMAN_TESTED` or `NVDA_VERIFIED`.

## Failure policy

Fail closed on:
- duplicate candidate or case identity;
- malformed SHA-256 evidence;
- missing engine/model license or provenance references;
- response request/provider/provider-kind/model substitution;
- empty successful response;
- malformed or Boolean token counts;
- non-finite/out-of-range scorer results;
- invalid or Boolean CPU/memory telemetry;
- invalid accelerator telemetry;
- backwards/non-finite benchmark clock;
- development-set use for a promotion Experiment definition.

Provider errors remain benchmark evidence as typed failures. Unexpected programming errors are not
laundered into a normal provider failure.

## Current acceptance boundary

This foundation provides deterministic contracts and fake-provider tests. It does not claim:
- physical Foundry inference;
- physical Ollama performance on the user's hardware;
- continuous GPU profiler validation;
- model download/install success;
- external cloud account/provider success;
- HUMAN_TESTED;
- NVDA_VERIFIED;
- production release readiness.

Those claims require their exact existing platform/provider/human gates on the exact integrated
candidate.
