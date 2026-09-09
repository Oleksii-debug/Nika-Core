# Nika Core — Local Inference Fabric and True Multi-Project Parallelism

Status: binding product requirement proposal requested by the owner on 2026-09-10.

## Product requirement

Nika must support true simultaneous execution of many independent projects and teams. Example target topology: 10 active projects, each with 3–15 agents, with all teams allowed to work at the same time. The product must not impose a single global model lock, a single-agent-at-a-time model policy, or a single-project-at-a-time execution policy.

Agent concurrency and model-backend concurrency are separate concerns. Every agent owns its own task/runtime/thread/context identity and may make an inference call independently of other agents. If 100 agents are active and 100 inference endpoints/slots are available, Nika must be able to issue 100 inference requests concurrently. Nika may expose backend capacity and failures, but it must not redefine the user's requested concurrency as a serialized workflow merely because one particular machine or model server has lower capacity.

## Supported inference topology

Nika ModelGateway must support an extensible pool of provider endpoints, including all of the following at the same time:

1. one local model server serving many concurrent client requests;
2. multiple independent replicas of the same local model/server on different endpoint identities, ports, processes, devices or hosts;
3. multiple different local models and/or different local inference engines active concurrently;
4. embedded/local providers and external local HTTP/OpenAI-compatible providers in the same installation;
5. multiple cloud/API providers concurrently;
6. multiple accounts/endpoints for the same cloud/API provider where explicitly configured and authorized;
7. mixtures of local and API-backed agents in the same project or across different projects;
8. remote user-managed inference nodes on other machines when explicitly configured.

No Nika domain rule may assume that one model ID maps to exactly one process or endpoint. A provider route is identified by stable endpoint/provider identity plus model identity and capability metadata. Multiple routes may legitimately expose the same model family or exact model artifact.

## Same-model replicas

Running several copies of the same local model is a valid supported configuration. Nika must not prohibit this topology. Examples include:

- llama.cpp server A on localhost:8081 and server B on localhost:8082, both serving the same GGUF model;
- several Ollama-compatible or OpenAI-compatible local endpoints managed independently;
- identical model replicas assigned to different GPUs or machines;
- one local replica plus one remote-LAN replica of the same model;
- several API endpoints exposing the same cloud model under distinct provider-route identities.

Nika must treat these as distinct executable routes even when `model_id` is identical.

## Different-model parallelism

Nika must also allow several different models to be active at once. A single project may deliberately use different models for architect, implementer, reviewer and specialist roles. Different projects may use completely different model sets simultaneously.

Example:

- Project A: 15 software-development agents using two local coding-model replicas plus API reviewers;
- Project B: 10 chess agents using a local general LLM plus Stockfish and a second explanatory model;
- Project C: 30 model-engineering agents using local inference servers, specialist models and cloud/API evaluation models;
- Projects D–J: independent teams using any allowed mix of providers.

All projects remain active concurrently.

## Provider-neutral concurrent invocation

ModelGateway must be safe for concurrent calls from many independent agents. Required properties:

- no global mutex around all model calls;
- no process-global mutable `current_model` that can be overwritten by another project;
- no shared conversation state between agents unless explicitly requested through a durable shared-memory contract;
- each request binds exact project, agent, task, provider route and model identity;
- response/audit evidence remains bound to the request that produced it;
- cancellation of one request does not cancel unrelated concurrent requests;
- failure of one endpoint does not corrupt other active model calls;
- per-task pinned model/provider identity survives restart where task semantics require stable routing;
- route changes are explicit and auditable rather than silently changing a running agent's intelligence source.

## Backend concurrency capabilities

Nika must discover/configure backend concurrency rather than assuming all servers behave alike. A route may describe capabilities such as:

- `max_concurrent_requests` or equivalent advertised/operator-configured capacity;
- support for continuous/dynamic batching;
- number of parallel slots;
- streaming support;
- hard-cancellation capability;
- model-loading/instance identity;
- locality: embedded, localhost, LAN, remote/private, cloud;
- optional device/node identity.

These values describe backend capability. They are not a product-level cap on how many agents/projects Nika is allowed to run.

## Local server compatibility

The architecture must remain compatible with at least these server classes behind adapters:

- Ollama/native Ollama adapter;
- llama.cpp OpenAI-compatible server, including multiple parallel slots and multiple independently launched server instances;
- LM Studio local server / compatible llama.cpp runtime;
- vLLM or another OpenAI-compatible high-throughput serving endpoint when the user deploys it on suitable hardware;
- future OpenAI-compatible local inference servers;
- embedded providers such as Foundry Local where their upstream concurrency semantics permit it.

No one server becomes the Nika domain contract.

## API-scale concurrency

Cloud/API intelligence is equally multi-agent. Nika must allow many agents to issue provider calls concurrently, subject only to provider/API credentials, explicit user policy and actual provider responses/rate limits. The architecture must not contain a hard-coded small global ceiling such as 1, 4, 8, 20 or 50 model calls for the entire Nika installation.

A deployment where 200 agents simultaneously use API-backed models is architecturally valid. Provider throttling, HTTP 429, account limits or budget policy are runtime outcomes, not reasons to serialize the entire Nika product by design.

## Project and team isolation

For simultaneous projects:

- each ProductProject has independent durable identity and state;
- each team has independent members, handoffs and runtime threads;
- repositories/workspaces remain isolated according to ProductFactory ownership rules;
- agent prompts/context/memory do not leak between projects;
- model route choice can be configured globally, per project, per team, per role or per task;
- one project may use LOCAL-only providers while another uses API providers at the same time;
- stopping, pausing or failing one project does not stop unrelated projects.

## LOCAL_FREE policy

`LOCAL_FREE` means the selected work may execute entirely through no-model/deterministic intelligence and/or user-managed local inference routes without requiring paid cloud inference. It does not mean "one local model at a time". Multiple local inference servers, replicas and different local models are explicitly permitted.

If the user also authorizes API use for other projects or agents, local and cloud-backed work may run concurrently. Nika must not automatically convert LOCAL_FREE work to paid API work without the applicable user policy/authorization.

## 24/7 operation

The runtime must support many long-lived projects and teams remaining active across application restart and machine restart. Durable state includes project/team/task identity and model-route binding needed to resume safely. A restart may reconnect to the configured local/API endpoints and resume eligible work without collapsing independent projects into a single sequential session.

## User-facing control

The Windows product must let the user configure and inspect:

- several model/provider endpoints;
- several replicas of the same model;
- several different local models;
- API providers alongside local providers;
- default route per project/team/role/task;
- which routes are currently active/healthy;
- which project/agent is using which model route;
- concurrent active projects and teams.

The UI must not imply that only one model/provider may be selected for the entire application.

## Acceptance scenarios

This capability is not complete until executable tests prove at minimum:

1. two independent projects execute model-backed agent work concurrently;
2. one team has multiple members concurrently invoking the same ModelGateway route without cross-talk;
3. two replicas of the same model ID on distinct endpoint identities are both used concurrently and remain distinguishable in evidence;
4. two different local model routes are used concurrently;
5. local and cloud/API routes are used concurrently by different agents;
6. one endpoint failure does not cancel or corrupt unrelated model calls;
7. cancellation of one model request leaves sibling requests active;
8. project A can be paused/stopped while project B continues;
9. route/task identity survives restart for durable work;
10. a stress proof demonstrates many simultaneous logical agents/projects without any Nika-owned global single-model lock or single-project serialization;
11. where the configured backend genuinely supports the requested parallelism, Nika issues requests concurrently rather than intentionally serializing them;
12. API-backed stress testing can scale to a high number of simultaneous agent requests using mocks/local test servers in CI, with live-provider scale treated as separate provider-specific evidence.

Hardware throughput, GPU count, RAM/VRAM and third-party provider limits are deployment capacity questions. They must be observable and honestly reported, but they are not allowed to narrow this product requirement or turn the architecture into a single-model/single-team sequential system.
