# Product Factory ↔ Toolsmith durable component resume

Date: 2026-08-23  
Owner: MANUAL-DEV06  
Starting live main: `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba`

## Defect closed

The integrated DEV06 component bridge proved the contract mapping, but its
`ComponentCapabilityGap` existed only in caller memory. Production Toolsmith persistence
uses the canonical task table as a foreign-key authority, while the compatibility bridge
constructed a synthetic component task ID. A process restart could therefore restore the
Product Factory `REPAIR_REQUIRED` work and independently restore Toolsmith registration,
but it could not reconstruct a trusted binding from that exact failed worker attempt to the
registered capability.

The compatibility type also rejected Toolsmith's legitimate initial escalation
`row_version=0`. The real SQLite-backed `CapabilityEscalationService.begin()` therefore
was not exercised by the original fake-port tests.

A second crash window existed after Product Factory created attempt N+1 in memory but
before a Product Factory checkpoint made that repair request durable.

## Compatibility decision

Do not create another task runtime, Toolsmith registry, ProductProject store or sandbox.
Do not change DEV27 containment or shared Toolsmith schema/contracts.

The Product Factory host now owns one thin durable cross-domain binding in the same
canonical `SQLiteStore`:

`host ProductFactory task + component + exact failed work_id + capability_id`

Toolsmith remains authoritative for capability lifecycle, pinned version and digest.
Product Factory remains authoritative for component attempt and checkpoint state.

The extension has its own ordered
`product_factory_toolsmith_schema_migrations` marker so this lane does not consume the
global research migration sequence or ProductProject schema ownership.

## Real production path

1. A real Product Factory host task already exists in `tasks`.
2. The failed `ComponentWorkRequest` is `REPAIR_REQUIRED`.
3. `begin_durable_gap()` validates that the host task payload belongs to the exact
   ProductProject and reserves the exact work/capability binding.
4. Toolsmith starts with the real host `task_id`, satisfying its existing foreign key.
5. Reuse/build/verify/register remains the existing Toolsmith lifecycle.
6. After registration, `resume_durable_registered_gap()` re-reads exact Toolsmith
   registered identity and uses the existing safe-repair policy.
7. The next deterministic `work_id`, capability version and digest are persisted as
   `RESUME_PREPARED`.
8. The canonical Product Factory checkpoint persists attempt N+1.
9. Only then is the handoff marked `CONSUMED`.

A bridge configured with a canonical store rejects legacy in-memory `begin_gap()` and
`resume_registered_gap()` calls so persistent composition cannot silently choose the
non-restart-safe path.

## Crash / replay semantics

### Crash after reserve before Toolsmith begin

The exact binding survives. `CapabilityEscalationService.begin()` is idempotent on the
existing `(task_id, capability_id)` row and the binding advances to `BEGUN`.

### Crash after Toolsmith registration before Product Factory repair

Toolsmith registration and the Product Factory binding both survive. Restart restores the
same `REPAIR_REQUIRED` worker attempt, validates exact registered pins and prepares N+1.

### Crash after `RESUME_PREPARED` before Product Factory checkpoint

The in-memory coordinator is rolled back to the pre-repair snapshot. Restart finds the
prepared next `work_id`, regenerates the deterministic N+1 repair and requires byte-level
identity through the deterministic work ID before checkpointing it. It cannot create N+2.

### Crash after Product Factory checkpoint before `CONSUMED`

Restart sees the exact N+1 `READY` request plus the prepared binding. The bridge finalizes
that existing handoff and does not call `prepare_repair()` again.

### Sequential gaps

A consumed predecessor whose `next_work_id` equals the current attempt does not mask a
new active gap on that attempt. Active exact bindings take precedence. Any active binding
that matches neither the current failed work nor its prepared next work fails closed as a
stale-attempt conflict.

### Capability rollback

The durable Product Factory binding is evidence, not capability authority. Reconciliation
always rechecks Toolsmith `REGISTERED` state and exact pinned version/digest. A prepared or
consumed binding whose capability is no longer registered fails closed.

## REUSE → ADAPT → CUSTOM (thin)

**REUSE**
- canonical `SQLiteStore` and `tasks` identity;
- existing Product Factory checkpoint host and ProductProject coordinator binding;
- deterministic `ComponentWorkRequest.work_id`;
- existing safe repair policy deriving the next base from exact worker `result_sha`;
- real `ToolsmithRepository` / `CapabilityEscalationService`;
- existing Toolsmith register/reconcile exact version+digest semantics.

**ADAPT**
- bind the real Product Factory host task to one exact component worker attempt;
- bridge Toolsmith registration evidence into the existing Product Factory repair
  checkpoint lifecycle.

**CUSTOM (thin)**
- one small Product Factory-owned binding table and four-state handoff state machine;
- exact replay/stale-attempt/cross-project checks.

No new dependency, generic workflow engine, sandbox, process containment layer, capability
registry, credential surface, permission widening or ProductProject persistence authority.

## Acceptance matrix

The focused real-SQLite tests must prove:

- configured persistent bridge rejects legacy synthetic/in-memory start;
- real Toolsmith begin uses an existing canonical host task and accepts row version zero;
- registered gap survives process reconstruction and produces exact attempt N+1 from the
  failed result SHA;
- Product Factory checkpoint survives another restart with the same N+1 identity;
- checkpoint-write failure leaves `RESUME_PREPARED` and retries the same attempt;
- checkpoint success followed by binding-finalization failure reconciles without N+2;
- foreign ProductProject host task is rejected before Toolsmith effect;
- stale old gap cannot consume a later independent attempt;
- a new gap on a resumed attempt takes precedence over its consumed predecessor;
- exact registered capability identity remains Toolsmith-authoritative.

Repository acceptance still requires exact-head Core CI on Ubuntu + Windows and complete
M12. Independent audit remains separate. Automated evidence never sets `HUMAN_TESTED` or
`NVDA_VERIFIED`.
