# Agent Lab operational status

## Purpose

This slice exposes a bounded, read-only operational view of the durable Agent Lab state already
owned by M7 Multi-Agent Lab and M8 Controlled Experiments. It does not create teams, run workers,
change experiments, approve actions, resume work, cancel work, or introduce a second Agent Lab
state authority.

The intended user capability is simple: an operator can inspect whether durable teams or experiments
exist, which high-level states they are in, whether child workers are waiting for approval, and how
much recent activity is present after a process restart.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** canonical `SQLiteStore`, schema migrations v6/v7, `TeamState`, `MemberState`,
  `TeamQuota`, and `ExperimentStatus`.
- **ADAPT:** those authoritative durable tables into a small operational read model with strict
  consistency checks and bounded output.
- **CUSTOM (thin):** redaction/projection policy, corruption fail-closed checks, accessible textual
  summary, optional JSON serialization, and a state-provider adapter for later integration by the
  canonical packaged-UI owner.

No dependency, database migration, execution framework, permission system, credential surface, or
second persistence layer is added.

## Exposed operational fields

The projection intentionally exposes only:

- opaque team ID;
- team state;
- total member count and child-worker count;
- nonterminal child-worker count;
- waiting-approval, completed, failed and cancelled child-worker counts;
- persisted team `max_total_agents` and `max_parallel` limits;
- team update timestamp;
- opaque experiment ID;
- experiment status;
- observation and lifecycle-event counts;
- experiment update timestamp;
- aggregate team/experiment counts.

The root supervisory M7 member is counted as a team member but is not treated as a child execution.
This matches the canonical M7 lifecycle: `finalize_team()` evaluates child executions and may leave
the supervisory root member in its original state after the team itself becomes terminal.

## Explicitly excluded data

The read model does **not** expose or serialize:

- tool grants, scopes, risk tiers or other execution permissions;
- runtime thread IDs or resume tokens;
- handoff/task/result payloads or raw worker error text;
- experiment definition JSON;
- candidate IDs, artifact refs, dataset refs or permission fingerprints;
- metric values;
- credentials, provider sessions, cookies, tokens or protected-store handles;
- any object that grants execution, approval, cancellation, promotion or rollback authority.

The reader parses only enough durable definition/quota data to verify identity and structural
consistency. Secret-bearing source fields are not copied to the returned view or accessible text.

## Read-only and restart semantics

`AgentLabStatusReader` is deliberately not an initializer or migration command.

- If the configured database file does not exist, reading fails and does not create the database or
  its parent directory.
- Existing databases are opened through `SQLiteStore.connection()` and immediately switched to
  SQLite `PRAGMA query_only = ON` before operational reads.
- Schema versions before Agent Lab v7 fail closed with an instruction to use the normal Nika
  migration/startup path.
- A database newer than the running Nika schema fails closed instead of guessing forward
  compatibility.
- No status read writes migrations, audit events, timestamps, checkpoints or lifecycle state.
- Recreating the reader against the same database after process loss returns the same durable
  projection when the underlying state has not changed.

## Corruption checks

Before presenting operational truth, the reader rejects relevant durable-state corruption including:

- missing M7/M8 tables;
- team members whose team does not exist;
- child members whose declared parent member does not exist in the same team;
- duplicate root identities or invalid parent/depth lineage;
- unsafe control characters in operational team/experiment identifiers;
- invalid team/root/state/quota identity;
- member, depth or per-parent child counts exceeding the persisted team quota;
- terminal teams that still contain nonterminal **child** executions;
- experiment observations/events whose experiment does not exist;
- experiment definition JSON whose embedded experiment ID differs from the row identity;
- experiments with no lifecycle event;
- experiment row status that differs from the newest lifecycle-event status;
- malformed or timezone-naive durable timestamps.

These checks are read-side integrity guards. They do not replace the canonical M7/M8 writer
invariants or repair corrupt state automatically.

## Accessible command-line use on Windows

The default command output is plain UTF-8 text with one logical fact per line. It uses no ANSI
colour, table alignment, screen position, mouse operation, OCR or visual-only indicator. This is
suitable for keyboard operation and screen-reader reading in PowerShell or Windows Terminal.

Set the database path for the current PowerShell session, quoting paths that contain spaces or
Ukrainian characters:

```powershell
$env:NIKA_DB_PATH = 'C:\Users\Oleksii\Nika Core\data\nika_core.db'
py -3.12 scripts\agent_lab_status.py
```

For machine-readable output:

```powershell
py -3.12 scripts\agent_lab_status.py --json
```

To bound each recent list more tightly:

```powershell
py -3.12 scripts\agent_lab_status.py --limit 20
```

The accepted limit is `1..200`; invalid limits fail before database access.

## Packaged integration boundary

`AgentLabStateProvider` can compose the bounded `agent_lab` object into another state provider
without replacing that provider's existing keys. It fails closed if the base provider already
owns the `agent_lab` key, preventing two competing operational authorities.

This ENG11 slice intentionally does not edit `scripts/nika_windows.py`, `src/nika_core/ui/*`, or the
web presentation. Those are shared/actively-owned integration surfaces. A later compatibility
decision by the canonical packaged-UI owner can compose this provider into the existing product
state without changing M7/M8 execution authority.

## Acceptance truth

Automated tests cover restart equivalence, Unicode/space database paths, secret-canary
non-disclosure, provider composition, missing-database no-create behavior, limits, quota corruption,
legal terminal-root semantics, illegal terminal child state, orphan parent/evidence corruption,
member-depth lineage corruption, unsafe output identifiers, bounded malformed-database failure,
experiment identity substitution and lifecycle-tail mismatch.

Repository acceptance still requires exact-head dependency consistency, Ruff, compile, full pytest
on Ubuntu and Windows, the applicable pre-human gate, current-main compatibility reread, and
independent review. Automated evidence cannot set `HUMAN_TESTED` or `NVDA_VERIFIED`.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PRODUCTION_RELEASE_READY=false`
