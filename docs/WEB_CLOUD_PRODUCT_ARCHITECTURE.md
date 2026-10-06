# Nika Core — Web/Cloud Product Architecture

Status: binding future architecture direction.
User decision recorded: 2026-09-04.

## 1. Purpose

Nika Core remains a Windows/NVDA-first product, but the end-state product must also support a commercial web/cloud edition without requiring a rewrite of the Nika domain/runtime.

The design target is one Nika product with multiple presentation/execution surfaces, not separate unrelated products.

Canonical shape:

`Web client / Windows client -> stable Nika application boundary -> Nika services/runtime -> tools/agents/state`

The browser is a user interface and secure client. The authoritative Nika task state, permissions, subscription/entitlement truth, cloud execution and premium service logic must not depend on trusting browser-side JavaScript.

## 2. Do not use remote-desktop streaming as the primary web product

A server-hosted Windows desktop streamed into a browser may be useful for diagnostics, legacy compatibility or exceptional workflows, but it is not the primary Nika Web architecture.

Reasons:
- poor accessibility compared with semantic HTML;
- high per-user CPU/RAM cost;
- difficult horizontal scaling;
- fragile focus/keyboard behavior;
- presentation and application logic remain unnecessarily coupled to Windows.

The normal web edition must expose semantic HTML controls and call server-side Nika services through a stable authenticated API/event channel.

## 3. Shared application/domain core

Windows UI and Web UI must not implement separate task, agent, scheduler, permission, memory, ProductProject or Business Factory semantics.

All user-visible commands should enter a stable application boundary, for example:
- task create/inspect/pause/resume/stop;
- agent/team create/activate/inspect;
- ProductProject operations;
- Universal Research jobs;
- Business Factory work orders;
- report/artifact requests;
- model/provider selection;
- approved tool actions.

The current packaged web assets, bridge models and desktop backend are treated as one presentation adapter, not as the domain authority. Future HTTP/WebSocket/SSE adapters should map to the same command/query contracts rather than duplicate business logic.

## 4. Nika Cloud

Nika Cloud is the future server-side execution and account plane.

It should be able to provide:
- authenticated user/workspace identities;
- durable task/project state;
- agent orchestration;
- cloud browser/worker execution where allowed;
- remote build/test/deployment nodes;
- scheduled/long-running work independent of the user's laptop power state;
- user data isolation;
- subscription/entitlement checks;
- usage budgets/quotas;
- audit logs;
- encrypted secret references through CredentialBroker;
- notification/report delivery.

Cloud execution is optional per capability. Privacy-sensitive/local-only tasks may remain local.

## 5. Nika Node — optional local Windows execution bridge

Some Nika capabilities need the user's own Windows machine: local files, local applications, local browser state, accessibility APIs or explicitly approved device actions.

For those cases the end-state product should use an optional `Nika Node`/local execution agent:

`Web UI -> Nika Cloud -> authenticated outbound channel -> Nika Node -> approved local action`

Binding rules:
- the user's PC does not need an inbound public port;
- Nika Node establishes the outbound authenticated connection;
- commands are scoped, signed/authorized and auditable;
- local permissions are separate from cloud account entitlements;
- high-impact actions still obey approval/standing-policy rules;
- loss of cloud connectivity must fail safely and preserve durable state;
- Nika Node must never expose raw stored credentials to model prompts or unrelated workers.

## 6. Web-ready development rule from now on

New domain capabilities must not be coupled directly to Windows controls.

Preferred flow:

`presentation -> command/query DTO -> application service -> domain/runtime -> result/event -> presentation`

A Windows-specific adapter may translate UIA/WebView2 events into commands. A future web adapter may translate HTTPS/WebSocket events into the same commands.

Any new subsystem should be reviewed for:
- platform-neutral domain types;
- serializable command/result contracts;
- no dependence on local filesystem paths unless explicitly local;
- explicit user/workspace identity;
- explicit permission scope;
- deterministic durable identifiers;
- remote-safe cancellation/idempotency/retry semantics;
- accessible text status/error output.

## 7. Multi-tenant and security boundary

The future commercial web edition must assume the client is untrusted.

Server-side authority must decide:
- authentication/session validity;
- product subscription/entitlements;
- workspace ownership;
- quotas/budgets;
- permission ceilings;
- secret access;
- execution-node assignment;
- destructive/publishing/financial gates.

Changing browser JavaScript or locally patching a Windows client must not grant paid capabilities or broader permissions.

Required security direction:
- tenant/user isolation;
- least-privilege service credentials;
- short-lived scoped tokens where possible;
- CSRF/XSS/session protection for web surfaces;
- rate limits and abuse controls;
- server-side validation of every state-changing command;
- audit correlation IDs end to end;
- no API keys, provider secrets or payment credentials in browser bundles or public repositories.

## 8. Commercial/account architecture

The final commercial product may offer Windows, Web and Cloud under one account/entitlement model.

The entitlement service should support product/plan features without becoming embedded into domain logic. Example categories may include:
- local/basic Nika;
- cloud task execution;
- additional agent/compute quotas;
- Product Factory/Business Factory capabilities;
- team/organization features.

Payment-provider integration remains replaceable. Payment card data should be handled by the chosen payment provider, not by Nika client code.

## 9. Accessibility for the web edition

The Web product is not allowed to regress accessibility.

Every major Web flow must be keyboard and screen-reader usable with:
- semantic HTML controls;
- correct accessible names/states;
- predictable focus;
- heading/landmark structure;
- live status without noisy uncontrolled announcements;
- text equivalents for visual-only state;
- no mouse-only critical action.

Windows NVDA verification and Web screen-reader verification are separate acceptance gates.

## 10. Product Factory impact

Nika Product Factory must be able to create products that include one or more of:
- backend/API;
- web client;
- Windows client;
- mobile client;
- local node;
- deployment/infrastructure.

This same component graph also applies when Nika evolves itself. A future Nika successor must be built in isolated workspaces and can include Web/Cloud/Node components without rewriting the stable domain contracts.

## 11. Migration strategy

Do not stop V0.1 or current Windows release work to build a full website now.

Sequence:
1. finish the current Windows product journey and release gates;
2. enforce web-ready application boundaries in all new work;
3. add contract tests proving presentation/backend separation;
4. create a minimal server adapter over existing commands/queries;
5. create a minimal accessible Web shell;
6. add authentication/workspace isolation;
7. move selected long-running workloads to cloud execution;
8. add optional Nika Node for local-machine capabilities;
9. add subscriptions/entitlements only after product journeys and threat model are stable;
10. expand Web parity feature by feature.

## 12. Completion truth

"Web-ready" does not mean a full web product exists.

Track separately:
- WEB_ARCHITECTURE_READY;
- WEB_API_CONTRACT_READY;
- WEB_UI_FOUNDATION_READY;
- MULTI_TENANT_SECURITY_READY;
- CLOUD_EXECUTION_READY;
- NIKA_NODE_READY;
- BILLING_ENTITLEMENTS_READY;
- WEB_ACCESSIBILITY_VERIFIED;
- WEB_PRODUCTION_READY.

No percentage or release claim follows merely from creating this architecture document.
