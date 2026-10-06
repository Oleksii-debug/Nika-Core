# Nika Autonomous Business Factory

Status: binding future product direction built on Nika Core and Autonomous Product Factory.
Updated: 2026-08-20.

## Purpose

Business Agent Lab is a reusable business-orchestration capability, not a hard-coded bot for one marketplace or niche. The user may state a business objective such as finding a lawful digital service niche, acquiring approved work, delivering products through Nika Product Factory, and maintaining the resulting customer/project pipeline.

The system separates research, recommendation, user/policy decisions, external representation, product delivery and money movement. An agent may never silently expand its own account, communication, contractual or financial authority.

## Lifecycle

`Business Goal -> Market Research -> Opportunity -> Lead/Channel -> Qualification -> Proposal -> Approval/Standing Policy -> Work Order -> ProductProject -> Product Factory -> QA -> Delivery -> Payment/Invoice State -> Support/Maintenance`

## Business entities

Future stable business-domain contracts should include versioned identities for:
- BusinessObjective;
- MarketOpportunity;
- Lead;
- Organization/Counterparty;
- CommunicationThread reference;
- Proposal/Estimate;
- WorkOrder;
- ProductProject link;
- Delivery;
- Invoice/PaymentState metadata;
- SupportCase;
- business policy/authorization profile.

Secrets, personal authentication material and payment credentials are stored only through CredentialBroker references.

## Dynamic business teams

Nika may compose roles such as market researcher, opportunity monitor, lead qualifier, sales/communication agent, estimator, account/project manager, Product Factory coordinator, QA, delivery worker and support worker. These are roles/capabilities, not necessarily one permanent process or LLM per role.

## External platform rule

Every platform connector must have a fresh API/automation/terms/security review. Prefer official APIs and supported automation. The product must not depend on spam, account farming, deceptive impersonation, CAPTCHA bypass or prohibited automation.

Where a platform requires a human identity/decision, Nika surfaces that gate rather than pretending it can bypass it.

## Communication autonomy

Communication may operate at user-configured autonomy levels, for example draft-only, send-with-approval, bounded autonomous communication or a higher explicitly authorized profile. The authorization profile defines allowed platforms, identities, topics, spending/contract limits, time window and escalation triggers.

## Financial and contractual boundaries

Business automation does not imply unrestricted money movement or contract formation. Financial/contractual actions use the same progressive authorization architecture as other high-impact Nika actions. Agents may recommend broader authority but cannot promote themselves to it.

## Relationship to Product Factory

A qualified business opportunity becomes a structured WorkOrder/ProductProject. Business agents should not duplicate coding, deployment, research, document, browser or interaction engines. They reuse Universal Research, Agent Lab, Product Factory, Computer Interaction, CredentialBroker, reporting and common Nika policy/audit services.

## Success metric

The objective is not agent activity volume. Business Factory is evaluated on lawful verified outcomes such as opportunity quality, conversion, delivery success, client satisfaction, margin/cost, error rate, platform-policy compliance and recovery from failure. Synthetic/sandbox tests precede unattended production operation.
