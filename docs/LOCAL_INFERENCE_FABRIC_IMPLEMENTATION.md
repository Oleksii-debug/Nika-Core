# Local Inference Fabric — implementation contract

Status: implementation contract for the owner-requested true simultaneous multi-project/multi-agent intelligence capability. This document is subordinate to `docs/LOCAL_INFERENCE_FABRIC.md` and `docs/LOCAL_INFERENCE_FABRIC_ACCEPTANCE.md`.

## Goal

Implement provider-neutral inference concurrency without introducing a second agent runtime or second model gateway. Reuse the existing `ModelGateway`, provider adapters, ProductProject identity, Agent Lab team/member identity, task/runtime persistence and resource/diagnostics services.

The key architectural requirement is **many independent provider routes + many independent in-flight requests**, not a single global current model.

## Domain model

Nika must distinguish at least these identities:

- `provider_route_id`: stable Nika-owned executable route identity;
- `provider_kind`: deterministic/local/cloud/embedded/other supported class;
- `endpoint_identity`: normalized endpoint/process/node identity where applicable;
- `model_id`: provider/model artifact identity;
- `project_id`;
- `team_id` where applicable;
- `agent_id`;
- `task_id`;
- `request_id`.

`model_id` is not sufficient as route identity. Two routes may expose the same exact model and still be independently addressable.

## Provider-route registry

Extend/adapt the existing provider registration/routing layer so several configured routes may coexist. Required operations include conceptually:

- register/configure route;
- enable/disable route;
- inspect route capabilities/health;
- select exact route for a request;
- preserve route identity in request/result/audit evidence;
- remove/revoke a route without corrupting unrelated active routes.

Do not introduce a process-global mutable singleton meaning "the current model for Nika".

## Routing scope

A configured default may exist at installation level, but Nika must support explicit route selection/override at progressively narrower scopes where product policy allows:

installation default -> ProductProject -> team -> role/agent -> task/request.

A narrower explicit binding must not mutate another task's already-frozen route. Durable tasks preserve their required route identity across restart or fail explicitly if that route is unavailable.

## Concurrent invocation

The ModelGateway/service implementation must permit overlapping asynchronous provider calls from unrelated requests. It must not place every provider call behind one installation-wide semaphore/mutex.

Provider-specific serialization is permitted only when an upstream implementation genuinely requires it and applies only to that route/provider instance. A provider-specific restriction must not block calls routed to independent provider instances.

Examples that must be representable:

- one llama.cpp server route with several parallel slots;
- llama.cpp replica A on port 8081 + replica B on port 8082 serving the same model;
- local coding model + local general model active simultaneously;
- Ollama route + generic OpenAI-compatible local route simultaneously;
- local route + cloud API route simultaneously;
- many cloud/API route calls simultaneously;
- routes on other user-managed machines/nodes.

## Same-model replica semantics

Replicas are not deduplicated merely because their `model_id` or artifact digest matches. Exact model identity may be common; executable route identity remains different.

A routing policy may deliberately assign different agents to different replicas. Evidence must show which replica served each request.

## Request isolation

Every in-flight request owns independent:

- request/correlation ID;
- project/team/agent/task binding;
- provider-route/model binding;
- timeout/deadline/cancellation token;
- normalized result/error;
- audit/usage/latency evidence.

There must be no implicit shared mutable conversation state at the provider layer. Shared context/memory is introduced only through explicit Nika-owned durable contracts.

## Failure and cancellation isolation

- cancellation of request A affects only A unless the upstream provider documents a broader unavoidable effect and Nika exposes that limitation;
- failure of route A does not cancel calls on route B;
- disabling one route does not invalidate unrelated configured routes;
- endpoint health transitions are scoped to that endpoint identity;
- provider 429/rate-limit responses are normalized per affected request/route rather than causing a global Nika model shutdown;
- fallback, where explicitly configured, must preserve existing privacy/cost/policy rules and must never silently move LOCAL_FREE work to a paid route.

## Multi-project runtime composition

ProductProject and M7 Agent Lab remain the project/team authorities. This capability must compose with them rather than create a new project scheduler.

Required product semantics:

- several ProductProjects may be RUNNING concurrently;
- each project may have one or more active teams;
- each team may have several concurrently running members;
- different members may bind the same route, different replicas, different local models or API routes;
- pause/cancel/failure is scoped to the intended project/team/task;
- closing/restarting Nika restores all durable projects independently.

## Local backend adapters

Adapters remain replaceable. At minimum the design must not prevent:

- Ollama native API;
- llama.cpp OpenAI-compatible server;
- LM Studio/OpenAI-compatible local serving;
- vLLM/OpenAI-compatible serving;
- Foundry Local/embedded provider where upstream semantics permit;
- future local compatible providers.

For servers exposing their own concurrency metadata, Nika may record capabilities such as parallel slots/max concurrent requests/batching support. Such metadata is route capacity/telemetry, not a global ProductProject/agent limit.

## API provider adapters

Cloud/API providers use the same provider-route concept. Several route configurations may coexist, including several separately configured routes to the same provider/model where authorized.

No low installation-wide constant is allowed to prohibit high fan-out. Live provider rate limits, quotas and authorization are handled as provider outcomes/policies.

## Packaged Windows UX requirements

The final accessible UI/command surface must allow the user to:

- add/configure multiple provider routes;
- configure multiple same-model replicas;
- configure different local models/engines;
- configure API routes alongside local routes;
- choose defaults or exact routes per project/team/role/task where supported;
- inspect active project/team/agent route bindings;
- inspect route health without exposing credentials;
- pause/stop one project without stopping unrelated projects.

All controls remain keyboard/NVDA accessible under existing product gates.

## Implementation sequence

Use this dependency order unless live code proves a better reuse path:

1. audit current ModelGateway/provider registry for implicit single-provider/single-default assumptions;
2. introduce/normalize stable provider-route identity without breaking current provider IDs;
3. make concurrent calls request-scoped and remove/avoid global mutable current-model authority;
4. prove same-route concurrent calls;
5. prove two same-model replicas concurrently;
6. prove different local routes concurrently;
7. prove local+API concurrently;
8. bind ProductProject/team/agent/task identities to exact routes;
9. make route/task binding restart-durable;
10. expose configuration/status in packaged accessible UI;
11. run the stress/acceptance matrix from `LOCAL_INFERENCE_FABRIC_ACCEPTANCE.md`.

## Non-goals

- do not replace LangGraph/AgentRuntimePort;
- do not replace ModelGateway;
- do not create a new ProductProject scheduler;
- do not make one inference server mandatory;
- do not hard-code a particular GPU/RAM topology into Nika domain contracts;
- do not claim a specific deployment can sustain N concurrent generations without measured evidence;
- do not weaken the architectural ability to attempt true concurrency merely because a particular deployment has lower capacity.
