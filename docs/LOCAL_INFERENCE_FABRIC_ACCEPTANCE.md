# Local Inference Fabric — acceptance contract

Status: binding acceptance amendment for `docs/LOCAL_INFERENCE_FABRIC.md`.

This contract exists to prevent a partial implementation from being credited as true multi-project/multi-agent parallel intelligence.

## Required product truths

A passing Nika implementation must prove all of the following:

1. **Multiple ProductProjects active simultaneously.** At least two independent ProductProjects have model-backed work in progress at the same time, with independent durable state and no global project lock.
2. **Multiple teams active simultaneously.** Separate teams can run concurrently; pausing/cancelling/failing one does not pause/cancel/fail unrelated teams.
3. **Concurrent members inside one team.** Several agents in one team can make overlapping ModelGateway calls without context/result/audit cross-talk.
4. **Same-model same-server concurrency.** One configured local server route that advertises/supports concurrent requests can receive overlapping inference requests from different agents.
5. **Same-model replica concurrency.** Two distinct endpoint/provider-route identities exposing the same `model_id` can be used concurrently and remain distinguishable in request/result/audit evidence.
6. **Different local models concurrently.** Two different local model routes can be active and invoked concurrently.
7. **Different local engines concurrently.** Nika domain contracts do not assume one engine implementation; compatible independently configured local engines may run side by side.
8. **Local + API concurrently.** One agent/team may use a local route while another simultaneously uses a cloud/API route.
9. **High API fan-out is architecturally supported.** Nika contains no small hard-coded global model-call ceiling. A stress harness can issue a large number of overlapping provider requests using test endpoints; provider-specific live limits are separate evidence.
10. **Per-request identity.** Every model call remains bound to exact project, team/agent, task, provider route and model identity.
11. **No process-global mutable current model.** Changing the default/model selection for one project or task cannot change another already-running task's route.
12. **No installation-wide inference mutex.** Nika must not intentionally serialize all model calls through one global lock. Provider-specific synchronization may exist only where required by that particular provider and must not block unrelated provider routes.
13. **Independent cancellation.** Cancelling/timing out one inference request does not cancel sibling requests or unrelated projects.
14. **Independent failure.** One endpoint becoming unavailable does not corrupt the state or route identity of other active requests.
15. **Durable route binding.** When task semantics require stable provider/model identity, restart preserves that binding or fails explicitly; it does not silently switch intelligence source.
16. **Many active projects after restart.** Restart/recovery reconstructs multiple projects/teams independently rather than collapsing them into one sequential session.
17. **Packaged product control.** The Windows UI/command surface can configure multiple provider endpoints and inspect which project/agent is bound to which route.
18. **No single-model UI assumption.** The UI does not expose only one installation-wide model choice when per-project/team/role/task routing is supported.

## Target stress scenarios

Acceptance must include automated stress scenarios representing at least:

- 10 concurrent ProductProjects;
- 3–15 agents configured per project;
- overlapping inference calls from many agents;
- two replicas of one model on distinct endpoints;
- two different local models;
- one or more API-compatible endpoints in parallel with local routes;
- cancellation/failure injection on one route while other routes continue;
- application restart followed by independent recovery of multiple active projects.

The stress harness may use deterministic/mock/local test inference endpoints to prove Nika-owned concurrency semantics. It must not claim that a particular user's hardware or third-party API can sustain a given throughput unless that deployment was actually measured. Conversely, low hardware/provider capacity must not be used to weaken the Nika architectural requirement.

## Non-acceptable implementations

The following do **not** satisfy this capability:

- one global `current_model` variable shared by all agents;
- one global model semaphore/lock that forces unrelated routes to execute one at a time;
- one active ProductProject at a time;
- requiring a separate Nika application instance per project merely to obtain parallel work;
- treating identical model IDs on different endpoints as the same route and losing endpoint identity;
- supporting multiple providers in configuration but serializing all calls through one worker;
- allowing many agents but only one agent to progress whenever any model call is active;
- switching a running task from local to paid API merely because the local route is busy;
- claiming same-model replica support without proving two distinct endpoint identities concurrently;
- claiming multi-project support using only sequential test execution.

## Evidence

For each concurrency acceptance run record at minimum:

- exact source SHA;
- project/team/agent/task IDs;
- provider-route IDs and model IDs;
- request start/end timestamps or deterministic overlap barriers proving calls overlapped;
- result ownership/correlation IDs;
- injected cancellation/failure identity;
- proof that unrelated calls continued;
- restart/recovery evidence where applicable.

Prompt/response text and credentials are not required in acceptance artifacts. HUMAN_TESTED and NVDA_VERIFIED remain separate gates.
