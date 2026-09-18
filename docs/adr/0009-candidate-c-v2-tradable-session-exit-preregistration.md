# ADR 0009 — Candidate C v2 tradable-session exit preregistration

- **Status:** Proposed for acceptance before Candidate C v2 measurement
- **Research gate:** P4 prospective protocol repair
- **Candidate:** Candidate C — cross-sectional five-day FX reversal
- **Protocol:** `candidate_c_cross_sectional_reversal.v2`
- **Supersedes:** Nothing; v1 remains frozen and historically authoritative
- **Repository checkpoint audited:** `9ea3a2340d0c32c612b42642123ce5be8f5b0729`

No Candidate C v2 return, hypothetical Friday/Monday return, performance statistic,
ranking output, or 2024+ observation was inspected to select this protocol. This ADR
authorizes neither measurement by itself, opening the sealed test, ML, demo/live
execution, nor real-money trading.

## 1. V1 is immutable and preserved

ADR 0008 and protocol `candidate_c_cross_sectional_reversal.v1` remain frozen. They
are not amended, reinterpreted, rerun, or overwritten by this ADR. The first real v1
measurement is preserved exactly as historical research evidence:

- Run ID: `88aed79769b0a7f38031744e678cd8996391a3ffdb94290e515417f1e5503910`
- Result ID: `dc08c6baedbbdbf652bca9e069f7349fca841366b1fd476656167663fc6ed9d7`
- Decision: `NOT_EVALUABLE`
- Reason: `exit_empty_evidenced`

V1 defined exit as the next calendar-day 00h boundary. A valid Friday entry on
`2014-01-10T00:00:00Z` therefore required an exit on Saturday
`2014-01-11T00:00:00Z`, whose genuine `EMPTY_EVIDENCED` record supplied no execution
price. This is a structural protocol infeasibility, not a performance rejection, an
implementation defect, or evidence for or against the economic hypothesis.

V2 exists only to make "one D1 bar hold" well-defined when calendar days contain no
executable D1 session boundary. It is prospective: the repair was specified after v1
produced no performance metrics and before any v2 performance was inspected.

## 2. Minimal delta and inherited v1 contract

V2 changes only the exit-boundary calendar state machine, the necessarily associated
split/seal purge rule, and the policy/RunID identity that binds those semantics. Every
other ADR 0008 rule is incorporated unchanged, including:

- the economic hypothesis, seven-pair canonical universe, Direct-D1 signal source,
  five-complete-bar score, USD-base score inversion, ranking, boundary ties, selected
  counts, signed `+0.25/-0.25` weights, gross/net exposure, causal signal timestamp,
  zero-volume signal-row treatment, and non-overlapping cohorts;
- entry execution in the established `[00:00, 01:00) UTC` partition, all-seven-pair
  evidence requirement, no six-pair fallback, and no weight redistribution;
- the train/validation/test intervals, 2014 provenance start, fixed
  `2022-01-01T00:00:00Z` embargoed signal, and sealed 2024+ test;
- pair orientation, adverse fills, observed-spread treatment, slippage, commission,
  1.5x stress, valuation, and cost fingerprint requirements;
- every metric and unit, calendar-day flat-return treatment for causal non-entry,
  Newey-West method and lag, alpha `0.00625`, robustness reports, all six P4 gates,
  and the three decisions `GO`, `NO_GO`, and `NOT_EVALUABLE`.

`GO` retains the sole meaning
**GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST**. No threshold, cost, signal parameter,
statistical rule, or P4 condition changes in v2.

## 3. Exact v2 execution-boundary state machine

### 3.1 Entry is unchanged

At a signal boundary, entry uses the first eligible tick in that boundary's existing
00h `[00:00, 01:00) UTC` execution partition. The v1 entry policy is unchanged:

- all seven `AVAILABLE` records permit the ranked four-leg entry;
- `ABSENT_EVIDENCED`, `EMPTY_EVIDENCED`, or `NO_VALID_TICK` for any pair causes a
  causal whole-cohort non-entry, with zero trade, turnover, cost, and return;
- `INVALID_PARTITION` or `MISSING_LOCAL_PARTITION` for any pair makes the relevant
  split and run `NOT_EVALUABLE`.

There is no later-hour rescue, carry-forward, synthetic price, Direct-D1-open
substitution, rank-dependent evidence selection, or partial-universe entry.

### 3.2 Next tradable D1 session

After an entry, let candidate exit boundary `k` be the UTC 00h boundary exactly `k`
calendar days after entry, for `k = 1, 2, ..., 7`. Candidate boundaries are examined
strictly in chronological order. At each boundary, the states of all seven canonical
pairs are classified before any later boundary is considered:

1. **Tradable boundary:** all seven records are `AVAILABLE`. This is the next
   tradable D1 session. The first eligible tick from each pair's established 00h
   partition supplies the exit evidence. Search stops immediately and the selected
   exit cannot be replaced by a later boundary for any price or performance reason.
2. **Evidenced non-execution boundary:** every one of the seven records is either
   `ABSENT_EVIDENCED` or `EMPTY_EVIDENCED`, with no other state present. The boundary
   supplies no price and is skipped. This is a protocol classification of immutable
   upstream non-execution evidence; it does not assert whether the cause was a
   weekend, holiday, venue schedule, or another market-closure mechanism.
3. **Unverifiable or partial boundary:** any `NO_VALID_TICK`, `INVALID_PARTITION`, or
   `MISSING_LOCAL_PARTITION`, or any mixture containing one or more `AVAILABLE`
   records without all seven being `AVAILABLE`, makes the relevant split and run
   `NOT_EVALUABLE`. It is neither closure evidence nor permission to continue.

The seven-boundary maximum is fixed prospectively as one complete calendar week. It
is a bounded safety limit, not an estimate selected from Candidate C outcomes. If no
tradable boundary is found by and including `k=7`, the relevant split and run are
`NOT_EVALUABLE` with reason `next_tradable_session_not_found_within_7_boundaries`.
The search never rolls into an eighth boundary.

This state machine handles weekends and holidays identically. A boundary may be
skipped only under rule 2; no hard-coded weekend/holiday calendar and no price data
are consulted. `NO_VALID_TICK` is deliberately not treated as closure evidence.

### 3.3 Holding and cohort ordering

The holding period is one tradable D1 session: entry at one all-seven `AVAILABLE`
boundary and exit at the first later all-seven `AVAILABLE` boundary reached under
section 3.2. Calendar duration may therefore exceed one day, but the mechanism still
holds across exactly one executable D1 session transition.

While an entered cohort is awaiting its exit, intermediate signal boundaries do not
create overlapping cohorts. After confirmed exit, the exit boundary may also be the
next signal/entry boundary under the unchanged v1 rule, with exit and entry accounted
separately. No exit search depends on ranks, selected legs, future prices, returns, or
whether a later boundary would have been more profitable.

## 4. Split, purge, embargo, and seal

A cohort remains assigned to the split containing its signal. Its signal, entry, and
resolved exit must all be strictly earlier than that split's exclusive end.

- **Train purge:** if an entered train cohort has not resolved an all-seven
  `AVAILABLE` exit before `2022-01-01T00:00:00Z`, it is purged when the search reaches
  that boundary. No validation evidence is read to resolve it.
- **Validation embargo:** the v1 fixed embargo remains unchanged:
  `2022-01-01T00:00:00Z` is not a validation signal, and the first possible validation
  signal remains `2022-01-02T00:00:00Z`. Expanded exit search does not import any
  training cohort across the split because the train purge above is absolute.
- **Validation purge and seal:** if an entered validation cohort has not resolved an
  all-seven `AVAILABLE` exit before `2024-01-01T00:00:00Z`, it is purged when the
  search would reach the sealed boundary. No 2024 record, metadata, calendar, price,
  or state is read.
- **Final signals:** `2023-12-31T00:00:00Z` remains excluded. An earlier 2023 signal
  is also purged if its entered cohort cannot resolve before the seal. There is no
  additional fixed last-signal date; the prospective state machine and exclusive
  seal determine it without outcome data.

Reaching a split end or the 2024 seal takes precedence over the seven-boundary search
and produces a boundary-crossing purge, not a fabricated exit and not a read across
the boundary. Purged cohorts are excluded under the inherited purge policy; corrupt,
missing, partial, or otherwise unverifiable evidence before the boundary remains
`NOT_EVALUABLE` and is never relabelled as purge or closure.

## 5. Known corrupt evidence remains fail-closed

This ADR does not repair or reinterpret the four known corrupt local BI5 partitions:

- `EURUSD 2018-02-06T00:00:00Z`
- `USDJPY 2022-05-10T00:00:00Z`
- `USDJPY 2022-06-14T00:00:00Z`
- `USDJPY 2022-06-15T00:00:00Z`

If any is decision-relevant as a required entry boundary or as a candidate boundary
during an entered cohort's exit search, it is `INVALID_PARTITION` and makes the split
and run `NOT_EVALUABLE`. Local absence remains `MISSING_LOCAL_PARTITION` with the same
result. Neither state may be skipped as an evidenced non-execution boundary. Repair or
reacquisition, if separately authorized, must preserve provenance and occurs outside
this protocol task.

## 6. Causality and future invariance

The exit rule is causal in the operational sense: at each boundary the strategy can
observe only the immutable evidence available for that boundary, wait through an
all-seven evidenced non-execution boundary, and exit immediately at the first later
all-seven `AVAILABLE` boundary. The search uses states, never prices or outcomes, to
decide whether a boundary is eligible.

Implementation must test at least these invariants before measurement:

- changing wall-clock timing, paths, mtimes, retrieval timestamps, retry order, or
  record arrival order cannot change the chosen exit or identities;
- reordering canonically equivalent pair records cannot change the result;
- changing prices or states strictly after the selected first tradable boundary
  cannot change that cohort;
- appending a later `AVAILABLE` boundary cannot replace an earlier eligible boundary;
- an intermediate partial, `NO_VALID_TICK`, invalid, or missing boundary cannot be
  skipped, even if a later boundary is fully available;
- changing any decision-relevant intermediate state changes the bound evidence/run
  identity and may change evaluability, but cannot trigger a price-dependent choice;
- evidence at or after `2024-01-01T00:00:00Z` is neither requested nor read;
- all seven pairs are evaluated at every required boundary, independent of ranks or
  selected legs; and
- the seven-boundary cap and split/seal cutoffs are enforced before later evidence is
  accessed.

## 7. Statistics, P4 decision, and multiplicity

ADR 0008 sections 6 through 8 apply without modification. V2 does not add a parameter
grid, bootstrap, random seed, metric, performance threshold, or alternative P4 gate.
Calendar-day return series retain explicit flat days for causal entry non-events.
Evidenced non-execution boundaries traversed by an already-entered cohort are holding
days and receive no fabricated execution or separate trade; accounting occurs only at
the selected exit.

Candidate C remains the same economic mechanism family and remains conservative
primary family test 8 with one-sided alpha `0.00625`. V1 produced no performance
metrics and was structurally `NOT_EVALUABLE`; this prospective calendar repair does
not consume an additional performance-tested family. Any later outcome-driven change,
variant, or alternative exit rule requires a new prospective protocol and a fresh
multiplicity decision.

## 8. Policy identity, RunID, and reproducibility

V2 must use a new immutable semantic policy object and must never reuse the v1 policy
ID or RunID. Its `policy_id` is repository `canonical_sha256` over the complete v2
policy object and binds the SHA-256 of the exact ADR 0009 bytes. Measurement remains
forbidden from a dirty worktree.

All ADR 0008 section 9 identity inputs remain bound. In addition, the v2 semantic
policy and run input identity bind:

- protocol `candidate_c_cross_sectional_reversal.v2` and the exact ADR 0009 byte hash;
- ordered next-session state-machine version and canonical pair order;
- the all-seven `AVAILABLE` exit rule;
- the exact all-seven `{ABSENT_EVIDENCED, EMPTY_EVIDENCED}` skip predicate;
- the fail-closed partial/`NO_VALID_TICK`/invalid/missing predicate;
- maximum search horizon of seven calendar boundaries;
- non-overlap behavior during the search;
- train/validation split cutoff, fixed validation embargo, validation/seal cutoff,
  and boundary-crossing purge semantics; and
- ordered identities and states of every execution-evidence record actually required
  to classify entry and each visited candidate exit boundary, while retaining the
  complete source manifest identity and state counts required by v1.

The run identity excludes results, local paths, mtimes, retrieval timestamps, retry
counts, completion order, and display format. Same verified evidence + same clean code
and same v2 policy must produce the same selected boundaries, canonical result bytes,
policy ID, and RunID. Any change to the seven-day cap, skip states, pair completeness,
split/seal behavior, or inherited v1 rule requires another prospective ADR/version.

## 9. Authorization boundary

This ADR is documentation only. After review and explicit acceptance, v2
implementation may be separately authorized. V2 measurement remains a separate
explicit action. Candidate C v1 must not be rerun or modified. The 2024+ test, ML,
Gate A/B2 or other execution infrastructure changes, demo/live execution, and
real-money trading remain unauthorized.
