# Nika Sports Lab — Paper Portfolio Foundation

Status: current-main implementation slice, paper-only, no real-money execution authority.

## Purpose

Nika Sports Lab is the sports-research and paper-simulation product line built on Nika-Core.
It must reuse Nika-Core for agent orchestration, persistence, ModelGateway, browser/computer
interaction, recovery, security, accessibility, scheduling, and product-factory infrastructure.
It must not become a copied or forked second Nika-Core.

The Sports Lab adds only domain-specific capabilities: sports-event research, causal live/pre-match
market observations, player/team research, probability and calibration services, paper bankroll,
paper tickets, singles/parlays, scenario exposure, settlement, replay, evaluation, and learning
records.

This first current-main slice implements the deterministic paper portfolio core. It does not place
wagers, control bookmaker accounts, fund accounts, redeem bookmaker credentials, or expose any
real-money bookmaker action.

## Existing canonical predecessor

PR #504 (`SPORTSBOOK: add read-only causal research foundation`) remains the canonical predecessor
for provider-neutral sports identities and causal observations. It defines Sport, Competition,
Event, Participant, Market, Selection, SportsbookSource, exact Decimal OddsSnapshot, ScoreState,
PeriodState, EventStatus, Settlement, causal `available_at` semantics, SQLite persistence/restart,
and a read-only `SportsbookSourcePort`.

PR #504 is stale relative to current main and must be selectively converged rather than duplicated.
QA PR #520 proves metadata-integrity/source-URI gaps; QA PR #539 proves float/bool-to-Decimal
coercion gaps. A current-main successor must repair those defects before the research foundation is
treated as integration-ready.

This paper slice intentionally does not copy the #504 domain model. Its identifiers are thin string
references until the repaired current-main research successor exists, at which point a narrow adapter
must bind PaperQuote to the canonical Market/Selection/OddsSnapshot identities.

## Paper-only product boundary

The authoritative workflow is:

read-only source data
-> causal normalized market mirror
-> research/probability agents
-> exact deterministic calculators
-> paper-only candidate ticket
-> virtual bankroll reservation
-> paper portfolio/scenario analysis
-> event settlement
-> paper bankroll update
-> evaluation/learning record
-> replay/out-of-sample qualification.

There is no bookmaker write/action/account/funding API in this package.

Any future real-money execution capability is outside this paper foundation and requires a separate
product/safety/legal authorization boundary. Do not infer such authority from browser interaction,
paper performance, or the existence of a ticket object.

## Current implemented contracts

`PaperQuote` stores immutable source/event/market/selection identifiers, exact decimal odds, and
`observed_at` plus causal `available_at` timestamps. A quote cannot be used by a paper ticket before
its availability boundary.

`PaperTicket` stores an exact stake, placement timestamp, and one or more quote-backed legs. One leg
is a single; multiple legs are a parlay. A ticket cannot include two selections from the same market
and cannot use duplicate quote identities.

`PaperPortfolio` owns an initial virtual bankroll and an immutable set of paper tickets. Total paper
stake cannot exceed the virtual bankroll. No external money or payment primitive exists.

`PaperSettlement` and `settle_ticket()` provide deterministic WIN/LOSS/VOID accounting. A losing leg
zeroes a parlay payout, a void leg contributes a multiplier of one, and a winning leg contributes
its captured quote odds.

`ScenarioOutcome`, `evaluate_scenario()`, and `analyze_scenarios()` compute final virtual bankroll and
P&L for explicit terminal scenarios. The envelope exposes exact worst-case and best-case P&L across
the supplied scenario set.

`enumerate_market_scenarios()` can exhaustively enumerate small finite market sets, but fails closed
when a configured scenario cap would be exceeded. This prevents accidental `2^N` explosion. Large
portfolios require a later factorized/graph/Monte-Carlo analysis layer rather than naïve exhaustive
enumeration.

## Numeric authority

All money, stakes, prices, payouts, and P&L use `Decimal`. Python floats and booleans fail closed.
This matches the exactness requirement already identified by sportsbook QA #539.

No language model is trusted to perform authoritative portfolio arithmetic. Agents may propose,
explain, research, rank, or generate hypotheses; deterministic calculators own bankroll, payout,
exposure, and scenario arithmetic.

## Causality / anti-leakage

Sports Lab must preserve the same anti-future-leakage principle as #504. Every observation and quote
must have causal availability metadata, and a paper decision at time `T` may consume only
information whose `available_at <= T`.

Historical replay must reconstruct the information boundary as it existed at each simulated time.
Final results, later odds, later scores, later injuries/news, settlement outcomes, or derived labels
must never leak backward into an earlier decision.

Every model/agent decision intended for evaluation must therefore retain enough provenance to
reproduce the exact input cut visible at its decision timestamp.

## Intended multi-agent architecture

The Sports Lab should compose specialized agents rather than one monolithic bettor-like agent.
Recommended roles are Data/Market Mirror, Participant Research, Forecast/Probability, Ticket
Candidate, Portfolio/Exposure, Independent Risk/Critic, Settlement, Replay/Evaluation, and
Learning/Strategy comparison.

Agents may have deliberately different information views. The portfolio calculator remains the
shared deterministic source of truth for ticket/exposure arithmetic. Agents must not maintain
conflicting private copies of authoritative bankroll/ticket state.

## Market Mirror

The next data product should maintain an internal causal mirror of normalized sport/competition/
event/participant/market/selection state plus timestamped odds, score, period, status, and settlement
observations. Research agents should query this local normalized state instead of repeatedly parsing
the bookmaker page as their primary database.

The mirror must support live changes while retaining full history. Updates are append-only causal
observations; current state is a projection, not destructive replacement of historical quotes.

## Replay Mode

Replay is a first-class requirement. Historical event streams should be replayable in event-time
order at accelerated speed while enforcing the original information-availability boundary.

Replay allows hundreds of historical sessions to be evaluated without waiting weeks in wall-clock
time. It must never reveal future observations to agents and must produce deterministic decision,
ticket, settlement, and evaluation records for the same exact inputs/configuration.

## Evaluation

A profitable one- or two-week paper run is not sufficient evidence by itself. Qualification should
track at least bankroll/P&L, ROI, maximum drawdown, volatility, risk of ruin, calibration, Brier or
log loss where probabilistic forecasts exist, results by sport/market/pre-match/live/single/parlay,
sensitivity to removing the largest wins, and walk-forward out-of-sample performance.

Paper qualification must distinguish forecast quality from portfolio/ticket construction quality.
A strategy can have useful probability estimates but poor staking/ticket construction, or vice versa.

## Scenario analysis

The user requirement is portfolio-level understanding: after many overlapping paper parlays, the
system must know which outcomes help or hurt the whole portfolio and must not rely on a human or LLM
remembering every leg.

For small portfolios exhaustive scenario enumeration is acceptable. For large portfolios the future
engine should build a dependency graph and use factorization, dynamic programming, pruning, and/or
Monte-Carlo methods. Scenario reports must make approximation explicit and must never label sampled
coverage as mathematical proof of every possible outcome.

A claim such as "profitable under every modeled outcome" requires a complete formally defined
scenario space or a proof-producing equivalent method. Bookmaker margin, correlated markets,
settlement rules, voids, suspended markets, and source errors must be represented rather than
silently ignored.

## Product split

Do not copy Nika-Core into a second repository and delete files. The target split is:

Nika-Core
-> reusable universal agent/runtime platform.

Nika Sports Lab
-> separate sports-domain product/repository when the extraction boundary is mature.

Nika Sports bridge/SDK
-> thin stable contracts that connect the product to Core services.

Until the separate repository exists, Sports Lab implementation slices may live in Nika-Core on
bounded branches so they can reuse the canonical runtime and be extracted later without cloning
Core internals.

## Next implementation sequence

The next developer/coordinator should first reconcile and selectively converge #504 onto current
main, carrying only its canonical research/data delta and repairing #520/#539 defects. Then bind this
paper portfolio layer to those canonical identities through a thin adapter.

After that, add durable paper ledger/restart, paper settlement ingestion, market-mirror projections,
replay clock/event stream, deterministic probability/evaluation record contracts, and an accessible
Windows presentation of virtual bankroll, committed stake, open tickets, odds changes, scenario
worst/best case, and decision provenance.

Do not begin with bookmaker execution. Complete the research + paper + replay + evaluation product
journey first.

## Acceptance boundary

This slice may claim only deterministic paper-simulation mechanics after exact-head tests and normal
Nika CI qualification.

It does not claim live-provider correctness, profitable strategy, bookmaker integration, real-money
readiness, HUMAN_TESTED, NVDA_VERIFIED, or PRODUCTION_RELEASE_READY.
