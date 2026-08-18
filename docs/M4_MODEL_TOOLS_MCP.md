# M4 — Model Gateway, standardized tools and MCP

Status: IMPLEMENTED candidate on `dev/m4-model-tools-mcp`; no milestone credit until exact Ubuntu + Windows CI is green and integrated.

## Reuse decision

- **REUSE** `httpx` for bounded async HTTP transport and HTTP error types.
- **REUSE** official `mcp` Python SDK v2 for MCP client lifecycle, discovery and tool calls. Do not implement the MCP wire protocol.
- **ADAPT** OpenAI-compatible chat-completions HTTP as the common narrow provider surface for compatible cloud/local endpoints.
- **CUSTOM (thin)** direct Ollama adapter configuration because Ollama exposes a small OpenAI-compatible local endpoint and Nika should not require a broad cloud gateway for local operation.
- **CUSTOM (thin)** Nika contracts, routing/privacy policy, audit evidence, standardized tool envelopes and approval boundary because these are product safety semantics.
- LiteLLM remains optional for later broad-provider normalization. This slice does not make it a mandatory runtime dependency.

## Stable boundaries

`ModelRequest`, `ModelResponse`, `ModelProvider`, `ProviderCapabilities`, `ModelGatewayError`, `ToolSpec`, `ToolCall` and `ToolResult` are Nika-owned contracts. Third-party HTTP/MCP object types remain inside adapters.

The gateway supports explicit provider IDs or provider-kind defaults (`no_llm`, `local`, `cloud`). Ambiguous multi-provider routing fails closed. Sensitive requests cannot be sent to providers that are not explicitly marked capable of private data.

Model and tool execution are bounded by deadlines and cancellation. Adapter failures become typed/normalized Nika outcomes. Audit records contain provider/tool identifiers, risk class, model/usage/latency and error class, but do not record prompts, tool arguments, API keys or raw provider responses.

## Providers

- deterministic `DeterministicMockProvider` for no-LLM/offline tests;
- `OpenAICompatibleProvider` for compatible HTTP providers;
- `OllamaProvider` using the local `/v1/chat/completions` surface.

API keys are runtime constructor inputs only. They are never included in request metadata, persisted configuration or audit payloads by this implementation.

## Standardized tools

Tools have stable IDs, JSON-schema input metadata, risk class and deadline. `EXTERNAL_SIDE_EFFECT` and `HIGH_IMPACT` calls require explicit approval or an injected approval policy. Unknown tools, denials, timeouts and handler failures return normalized `ToolResult` failures.

## MCP boundary

`MCPClientAdapter` uses the official SDK `Client` lifecycle. Tool discovery is translated to Nika `ToolSpec` and calls return Nika `ToolResult`. MCP tools default to `EXTERNAL_SIDE_EFFECT` risk because a remote server's declaration alone is not sufficient evidence that a tool is harmless. Product code can later assign narrower risk only through trusted connector policy.

## Acceptance evidence still required

Before M4 receives its 8% weight:

1. shared verification must pass on the exact candidate SHA on Ubuntu and Windows;
2. mock/no-LLM semantic scenario must pass through `ModelGateway`;
3. at least one real provider must run through the same Nika interface; the intended low-risk real proof is local Ollama/OpenAI-compatible HTTP;
4. provider failure, timeout and cancellation must map correctly;
5. MCP discovery/call must be proven against the official SDK with an in-process or controlled test server;
6. no secrets may appear in Git or audit evidence;
7. exact green SHA must be merged before M4 weight is credited.
