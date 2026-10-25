# Full Product Vision amendment — true multi-project parallel intelligence

Date: 2026-09-10.
Status: owner-requested Full Product Vision amendment. It extends, and does not reduce, `docs/FULL_PRODUCT_VISION_2026-08-19.md`.

## End-state capability

Nika is a persistent multi-project agent platform, not a single-chat/single-model desktop client. The end-state product must allow many independent projects and teams to remain active and make progress simultaneously.

Representative target:

- roughly 10 concurrent projects;
- 3–15 or more agents per project;
- all projects allowed to work at the same time;
- local-only, cloud/API-only and mixed local+API teams supported concurrently;
- 24/7 durable operation with restart/recovery.

This is a product topology requirement. The user's hardware, chosen model server and cloud provider determine achievable throughput in a deployment, but Nika must not intentionally redesign the topology into one-project-at-a-time or one-model-call-at-a-time execution.

## Intelligence topology

The four existing intelligence modes remain binding: deterministic/no-model, embedded local, external local and cloud/API. The extension is that many routes and many calls may be active concurrently.

Supported configurations include:

- one concurrent local inference server shared by many agents;
- multiple replicas of the same local model on separate endpoints/processes/devices/hosts;
- several different local models active simultaneously;
- several different local inference engines active simultaneously behind ModelGateway adapters;
- several API/cloud routes active simultaneously;
- local and API routes used at the same time by different projects or roles;
- remote user-managed local/private inference nodes.

Identical model identity does not imply identical execution endpoint. Endpoint/provider-route identity is first-class.

## Project/team/model independence

Every ProductProject, team, agent and task has independent durable identity. Model routing may be configured globally and overridden per project/team/role/task under normal policy.

A running task's pinned provider/model route cannot be silently changed because another project changes its default model. There is no process-global mutable `current_model` authority.

Stopping, pausing, cancelling or failing one ProductProject/team does not stop unrelated projects. Model-server failure is scoped to affected route/calls and must not corrupt unrelated task state.

## Same-model replicas are first-class

Nika explicitly supports several simultaneously configured endpoints that expose the same model. This is not treated as a duplicate-configuration error.

Examples:

- same GGUF model served by separate llama.cpp server processes on different ports;
- same model hosted on separate GPUs or machines;
- same cloud model accessed through separately configured provider routes;
- one local and one LAN-hosted replica of the same exact model artifact.

These routes remain separately observable, cancellable and auditable.

## Large cloud/API fan-out

The product architecture must also support high numbers of concurrent API-backed agents. A configuration with hundreds of simultaneously active API-backed agents is valid if the user/provider setup permits it. Nika handles provider responses such as rate limits honestly, but must not contain an arbitrary low installation-wide concurrency ceiling that makes such a configuration architecturally impossible.

## LOCAL_FREE

LOCAL_FREE is a provider/cost policy, not a single-model execution mode. Several local servers, same-model replicas and different local models may all serve LOCAL_FREE projects concurrently. Nika never silently switches LOCAL_FREE work to a paid API route without applicable user authorization.

## Binding implementation/acceptance references

Detailed architecture: `docs/LOCAL_INFERENCE_FABRIC.md`.

Acceptance: `docs/LOCAL_INFERENCE_FABRIC_ACCEPTANCE.md`.

ModelGateway integration: `docs/M4_MODEL_TOOLS_MCP.md`.

Multi-agent integration: `docs/M7_MULTI_AGENT_LAB.md`.

This amendment must be considered when implementing ModelGateway routing, Agent Lab, ProductProject orchestration, resource/runtime services, packaged model settings and future local/cloud provider adapters.
