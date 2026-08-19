# M4 — Model Gateway, standardized tools and MCP

Status: historical M4 foundation is integrated. Current AUTO02 work may harden provider behavior without changing the original milestone credit; exact new behavior still requires exact-head CI before integration.

## Reuse decision

- **REUSE** `httpx` for bounded async HTTP transport and HTTP error types.
- **REUSE** official `mcp` Python SDK v2 for MCP client lifecycle, discovery and tool calls. Do not implement the MCP wire protocol.
- **ADAPT** OpenAI-compatible chat-completions HTTP as the common narrow provider surface for compatible cloud/local endpoints.
- **ADAPT** Ollama's maintained native `POST /api/chat` API for the dedicated local Ollama provider. Native Ollama-specific wire fields remain inside the adapter and do not enter Nika domain contracts.
- **CUSTOM (thin)** Nika contracts, routing/privacy policy, audit evidence, standardized tool envelopes and approval boundary because these are product safety semantics.
- LiteLLM remains optional for later broad-provider normalization. This slice does not make it a mandatory runtime dependency.

## Stable boundaries

`ModelRequest`, `ModelResponse`, `ModelProvider`, `ProviderCapabilities`, `ModelGatewayError`, `ToolSpec`, `ToolCall` and `ToolResult` are Nika-owned contracts. Third-party HTTP/MCP/Ollama object types remain inside adapters.

The gateway supports explicit provider IDs or provider-kind defaults (`no_llm`, `local`, `cloud`). Ambiguous multi-provider routing fails closed. Sensitive requests cannot be sent to providers that are not explicitly marked capable of private data.

Model and tool execution are bounded by deadlines and cancellation. Adapter failures become typed/normalized Nika outcomes. Audit records contain provider/tool identifiers, risk class, model/usage/latency and error class, but do not record prompts, tool arguments, API keys or raw provider responses.

`supports_hard_cancellation` means the underlying inference is proven stopped, not merely that the Python caller or HTTP socket was cancelled. The shared `ProviderCapabilities` contract itself defaults this field to `False`, so a newly added adapter cannot accidentally inherit hard-cancellation credit. Generic HTTP providers also default it to `False`. An adapter may opt in only when its upstream/provider path has separate evidence for hard server-side cancellation. This prevents ModelGateway from launching a fallback inference after a timeout when the first inference may still be consuming resources.

## Providers

- deterministic `DeterministicMockProvider` for no-LLM/offline tests;
- `OpenAICompatibleProvider` for compatible HTTP providers;
- `OllamaProvider` using native `http://localhost:11434/api/chat` by default.

The dedicated Ollama adapter sends `stream: false` so one Nika request produces one bounded response envelope. It sends `think: false` by default for models that accept the boolean switch, keeping reasoning traces out of the shared response contract. Ollama also documents thinking levels (`low`, `medium`, `high`, and for supported models `max`); the adapter accepts and validates those levels so models such as GPT-OSS can be used without leaking Ollama-specific wire types into Nika contracts. The `message.thinking` field is intentionally not copied into `ModelResponse`. Temperature is translated to Ollama's native `options` object. Native `prompt_eval_count` and `eval_count` are normalized to Nika `ModelUsage`.

Ollama is local and may receive private/sensitive data under Nika's local routing policy. It does **not** currently claim hard server-side cancellation because the adopted native API path does not provide a separately proven hard-cancel guarantee for an active generation.

API keys for OpenAI-compatible providers are runtime constructor inputs only. They are never included in request metadata, persisted configuration or audit payloads by this implementation.

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
3. native Ollama request contract proves `/api/chat`, `stream: false`, default `think: false`, validated thinking levels, model override and usage normalization;
4. provider failures, timeout and cancellation map correctly;
5. the shared capability contract, generic HTTP providers and Ollama fail closed on hard-cancellation claims unless evidence explicitly opts a provider in;
6. explicit fallback still works for retryable failures, but timeout fallback remains blocked when hard cancellation is unproven;
7. no secrets or prompt content appear in Git/audit evidence;
8. exact green SHA is current-main-compatible before merge.

Live Ollama execution is useful focused evidence but does not award physical-Windows Foundry inference credit. HUMAN_TESTED and NVDA_VERIFIED remain human-only states.
