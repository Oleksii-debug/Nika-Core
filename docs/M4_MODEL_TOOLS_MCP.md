# M4 — Model Gateway, standardized tools and MCP

Status: historical M4 foundation is integrated. Current provider hardening may extend behavior without changing the original milestone credit; exact new behavior still requires exact-head CI before integration.

## Reuse decision

- **REUSE** `httpx` for bounded async HTTP transport and HTTP error types.
- **REUSE** official `mcp` Python SDK v2 for MCP client lifecycle, discovery and tool calls. Do not implement the MCP wire protocol.
- **ADAPT** OpenAI-compatible chat-completions HTTP as the common narrow provider surface for compatible cloud/local endpoints.
- **ADAPT** Ollama's maintained native `POST /api/chat` API for the dedicated local Ollama provider. Native Ollama-specific wire fields remain inside the adapter and do not enter Nika domain contracts.
- **CUSTOM (thin)** Nika contracts, routing/privacy/cost/resource policy, audit evidence, standardized tool envelopes and approval boundary because these are product safety semantics.
- LiteLLM remains optional for broad-provider expansion. The current direct Ollama plus OpenAI-compatible surface is too small to justify moving Nika-owned privacy, fallback, deadline or fail-closed policy into LiteLLM's router.

## Stable boundaries

`ModelRequest`, `ModelResponse`, `ModelProvider`, `ProviderCapabilities`, `ModelGatewayError`, `ToolSpec`, `ToolCall` and `ToolResult` are Nika-owned contracts. Third-party HTTP/MCP/Ollama object types remain inside adapters.

The gateway supports explicit provider IDs or provider-kind defaults (`no_llm`, `local`, `cloud`). Ambiguous multi-provider routing fails closed. `PRIVATE` and `SENSITIVE` requests can use only providers explicitly marked `supports_private_data=True`.

Fallback is explicit and route-wide. Before the first provider receives payload data, Nika resolves and validates the primary plus every nominated fallback for privacy, optional local-only routing, metered-cost policy and optional resource-class policy. A bad later fallback therefore cannot cause sensitive data to be sent to an otherwise-valid primary before route rejection.

`ModelRoutePolicy` is provider-neutral:
- `local_only=True` rejects every cloud candidate in the complete route;
- `allow_metered=False` rejects metered and unknown-cost candidates;
- `allowed_resource_classes` can constrain execution to explicit provider-neutral classes such as local service or remote service.

`ProviderCapabilities` exposes provider-neutral cost/resource metadata. Direct local HTTP and Ollama adapters report local-resource/local-service semantics; direct cloud HTTP reports metered/remote-service semantics unless its constructor is given a more exact classification. A provider that does not publish a classification remains unknown and fails closed when a request forbids unknown cost or requires a specific resource class.

`ModelRequest` rejects boolean, non-numeric and non-finite deadline values before routing. The total timeout therefore cannot be disabled with `NaN` or infinity, and it must remain strictly greater than zero. Temperature follows the same strict numeric boundary: booleans and non-finite values are rejected while finite values remain constrained to the existing `[0, 2]` range.

Model and tool execution are bounded by deadlines and cancellation. ModelGateway computes one total request deadline and gives each fallback only the remaining budget; a fallback never receives a fresh timeout. Adapter failures become typed Nika outcomes. Authentication, policy-denied, resource-limit, invalid-request and cancellation outcomes never fallback even if a defective adapter marks them retryable. `retryable=True` is only failure classification; it is never proof that the first provider produced no effect. `ModelFailureEffect.UNKNOWN` is the fail-closed default. For `UNAVAILABLE` or `RATE_LIMITED`, fallback additionally requires explicit provider-neutral `ModelFailureEffect.NO_EFFECT` evidence that the failed attempt did not start/commit inference, consume a billable effect, or leave another externally ambiguous outcome. Timeout fallback requires the same `NO_EFFECT` evidence plus proven hard inference cancellation. A provider-originated raw `TimeoutError` is not evidence that the gateway deadline elapsed and is normalized to non-retryable `PROVIDER_ERROR`; typed timeout fallback therefore requires `ModelGatewayError(TIMEOUT, retryable=True, failure_effect=NO_EFFECT)` plus hard-cancellation capability evidence. Other untyped provider exceptions are also normalized to non-retryable `PROVIDER_ERROR`.

Normalized provider responses fail closed at the gateway boundary. The gateway rejects wrong request/provider/kind identity, non-`ModelResponse` values and whitespace-only response text before `model.completed`. Shared `ModelUsage` rejects booleans, negative/non-integer token counts and totals smaller than the sum of all known input/output token components. A provider total may be larger than those known components because provider accounting can include categories that the shared contract does not separately expose. Shared `ModelResponse` enforces response-text type plus identity/model/usage/latency shape, but intentionally remains able to represent untrusted whitespace text so non-gateway consumers can apply their own defensive content policy. Direct HTTP/Ollama adapters reject malformed response objects, choices/messages, empty text, invalid model values and malformed usage instead of manufacturing plausible defaults.

Audit records contain provider/tool identifiers, risk class, model/usage/latency, cost/resource class, error class and the normalized `failure_effect` for provider-attempt failures/fallbacks. Route-selection and policy failures that occur before the first provider attempt also emit `model.failed` with `phase=preflight`, the typed error code and provider identity when one is known. Ambiguous preflight failures do not fabricate a provider identity. Audit evidence does **not** record prompts, request metadata, tool arguments, API keys or raw provider responses.

`supports_hard_cancellation` means the underlying inference is proven stopped, not merely that the Python caller or HTTP socket was cancelled. The shared `ProviderCapabilities` contract itself defaults this field to `False`, so a newly added adapter cannot accidentally inherit hard-cancellation credit. Generic HTTP providers also default it to `False`. An adapter may opt in only when its upstream/provider path has separate evidence for hard server-side cancellation. Even hard cancellation does not by itself prove that no billable or externally visible effect occurred before cancellation, so effect-safety evidence remains separate.

## Providers

- deterministic `DeterministicMockProvider` for no-LLM/offline tests;
- `OpenAICompatibleProvider` for compatible HTTP providers;
- `OllamaProvider` using native `http://localhost:11434/api/chat` by default.

The dedicated Ollama adapter sends `stream: false` so one Nika request produces one bounded response envelope and requires the native response to report `done: true`; an incomplete/non-final response fails closed instead of being accepted as a completed Nika response. It sends `think: false` by default for models that accept the boolean switch, keeping reasoning traces out of the shared response contract. Current official Ollama API documentation permits generic thinking levels `low`, `medium`, `high` and `max` for supported models, with `max` requesting the highest level. Model-specific constraints still apply: for example, current GPT-OSS documentation accepts `low`, `medium` or `high` rather than `max`, and boolean think values are ignored for that model. The Nika wire adapter accepts the documented generic level vocabulary but does not invent a model-capability registry; an invalid model/level combination remains an upstream provider error. The `message.thinking` field is intentionally not copied into `ModelResponse`. Temperature is translated to Ollama's native `options` object. Native `prompt_eval_count` and `eval_count` are normalized to Nika `ModelUsage`.

Ollama is local and may receive private/sensitive data under Nika's local routing policy. It does **not** currently claim hard server-side cancellation because the adopted native API path does not provide a separately proven hard-cancel guarantee for an active generation.

API keys for OpenAI-compatible providers are runtime constructor inputs only. They are never included in request metadata, persisted configuration or audit payloads by this implementation.

HTTP failure mapping is fail-closed:
- 401 -> `AUTHENTICATION`;
- 402/403 -> `POLICY_DENIED`;
- 400/404/422 -> `INVALID_REQUEST`;
- 413/507 -> `RESOURCE_LIMIT`;
- 408 -> `TIMEOUT`;
- 429 -> retryable `RATE_LIMITED`;
- 5xx -> retryable `UNAVAILABLE`, except status codes already classified above.

Those HTTP codes describe retry classification only. The direct HTTP adapter does not infer `NO_EFFECT` from a status code, because receipt of 429/5xx is not generic proof that no provider-side inference, quota, billing or other effect occurred. Such failures therefore remain `failure_effect=UNKNOWN` and do not automatically launch a fallback. A future provider-specific adapter may opt into `NO_EFFECT` only with evidence for the exact upstream failure boundary.

The deterministic test suite drives both the OpenAI-compatible provider and native Ollama provider through real HTTPX request/response transport boundaries using `MockTransport`. This is protocol/adapter evidence, not a substitute for a live-provider proof.

For the live-provider gate, focused CI may install Ollama and run `scripts/m4_ollama_proof.py` through the same `ModelGateway` and `OllamaProvider` contracts. The proof requires a non-empty response and the expected local-provider identity; it does not assert model prose. Large models are never pulled by ordinary shared CI merely to prove adapter plumbing.

## Standardized tools

Tools have stable IDs, JSON-schema input metadata, risk class and deadline. `EXTERNAL_SIDE_EFFECT` and `HIGH_IMPACT` calls require explicit approval or an injected approval policy. Unknown tools, denials, timeouts and handler failures return normalized `ToolResult` failures.

## MCP boundary

`MCPClientAdapter` uses the official SDK `Client` lifecycle. Tool discovery is translated to Nika `ToolSpec` and calls return Nika `ToolResult`. MCP tools default to `EXTERNAL_SIDE_EFFECT` risk because a remote server's declaration alone is not sufficient evidence that a tool is harmless. Product code can later assign narrower risk only through trusted connector policy.

The acceptance suite constructs a real official `MCPServer` in process, discovers its generated schema through `Client.list_tools()`, then invokes it through `Client.call_tool()`. This is the official SDK's in-memory transport, so the proof traverses MCP protocol handling without starting a subprocess or network listener. Risky MCP calls also fail closed at the adapter boundary when the `ToolCall` lacks explicit approval, preventing direct adapter use from bypassing the product approval policy.

## Acceptance evidence required for current provider hardening

1. shared verification passes on the exact candidate SHA on Ubuntu and Windows;
2. mock/no-LLM semantic scenario continues through `ModelGateway`;
3. entire private/sensitive fallback route is rejected before primary execution if any candidate violates privacy/local/cost/resource policy;
4. non-timeout fallback requires an explicitly nominated route, a safe typed code, `retryable=True` and explicit `ModelFailureEffect.NO_EFFECT`; retryable classification alone cannot trigger a second provider after an ambiguous first-provider effect;
5. auth/policy/resource/invalid/cancel failures never fallback, even if an adapter incorrectly marks them retryable;
6. request deadlines are finite positive numeric values, one total deadline is preserved across all attempts, raw provider `TimeoutError` cannot masquerade as the gateway deadline, and typed timeout fallback requires both `NO_EFFECT` and proven hard cancellation;
7. response identity, text, model, usage and latency normalization fail closed on malformed values, including token totals smaller than known input/output components while allowing larger provider totals;
8. native Ollama request contract proves `/api/chat`, `stream: false`, default `think: false`, generic documented `low`/`medium`/`high`/`max` level validation, `done: true` completion, model override and usage normalization;
9. route preflight failures produce redacted typed audit evidence without prompt/metadata leakage or fabricated provider identity, and provider-attempt audit records retain normalized failure-effect certainty;
10. no secrets, request metadata or prompt content appear in Git/audit evidence;
11. exact green SHA is current-main-compatible before merge.

Live Ollama execution is useful focused evidence but does not award physical-Windows Foundry inference credit. Embedded Foundry implementation/lifecycle remains a separate ownership lane. HUMAN_TESTED and NVDA_VERIFIED remain human-only states.
