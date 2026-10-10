# AI Trader — causal PAPER time waves (Plan 5, Section 1)

## Domain/authority

This contract is a read-only view over the canonical `Dataset.temporal_view(decision_at)` and is part of the existing Trader workspace, **not** a separate broker, runtime, scheduler, grant authority, memory store, or investment executor. It is currently a repository-controlled component, not a fully wired live Trader application.

`group_visible_time_waves(view, window_minutes, max_events)` groups only **already-visible** events by UTC `event_at`. A prematch event may be future-dated only when its market data's `available_at <= decision_at`. No event beyond the temporal view can enter a wave.

* The caller supplies an exact canonical `TemporalView` as the no-lookahead boundary. Only exact built-in canonical event / time / instrument / venue carriers are accepted; behavioral subclasses and mutated future availability fail closed.
* Windows are aligned to midnight UTC and use a divisor of 1,440 minutes (1–1,440 inclusive). Iteration is bounded by `max_events`, at most 100,000.
* Output consists of frozen `TimeWave` values with UTC range, event count and sorted, de-duplicated `(venue_id, venue_timezone, instrument_id, currency)` identities. Output does not include credentials, bankroll, permissions, raw arbitrary adapter payloads or mutable event references.
* Ordering is deterministic regardless of the input dataset event order. Duplicate physical event identities are separately subject to the existing Dataset validation report; this projection does not bless duplicate data as unique market evidence.
* Invalid budget, timestamps, mutated future availability, hostile identities and untrusted view types cause explicit failures. No source order, portfolio position, financial balance or external effect is mutated.

## Scope and acceptance truth

Repository tests: `tests/test_plan5_trader_time_waves.py` checks prematch visibility, no-lookahead, deterministic order, venue timezone identity, budget rejection, attacker-owned identity, mutation after view creation and detached outputs. Authored test cases do not imply exact-head pytest or CI passed. Integration into the actual Trader host UI, recurring polling, orders/sessions, shared evidence/report UX and the complete inherited legacy Section 37 remain separately required before **terminal DONE**. No actual live betting, money movement, or production trading authority is introduced.
