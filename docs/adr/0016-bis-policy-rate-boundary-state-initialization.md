# ADR 0016 — BIS Policy-Rate Boundary-State Initialization

- **Status:** Accepted — Frozen External Evidence Contract Amendment
- **Research gate:** Boundary-state evidence acquisition and normalization
- **Protocol family:** `post_test_8_research_family_v1`
- **Candidate E status:** NOT SELECTED (0 / 4 performance-test slots consumed)
- **Supplements:** ADR 0014 and ADR 0015; neither prior ADR is modified
- **Point-in-time status:** UNRESOLVED

---

## 1. Purpose and preserved history

ADR 0015 correctly fails closed when the first in-window BIS observation is missing and
no finite same-series state has been established. Canonical acquisition Attempt #3
therefore failed on `D.GB`, `2014-01-01`, `OBS_VALUE=NaN`, `OBS_STATUS=M`,
`OBS_CONF=F`, with zero canonical artifacts published.

This amendment defines how an administrative policy-rate state already in force before
the frozen research start may initialize ADR 0015 persistence. It does not expand the
research interval, define a strategy, select Candidate E, or consume a performance test.

## 2. Authoritative basis

The numerical source remains exclusively `BIS:WS_CBPOL(1.0)`. The BIS SDMX v2 API is a
subset of the official SDMX REST API and documents `lastNObservations` as the maximum
number of observations returned per matching series, counting back from the most recent
observation. The BIS OpenAPI also supports `endPeriod`.

The frozen mechanism was verified by the bounded BIS query:

`D.GB?endPeriod=2013-12-31&lastNObservations=1`

It returned exactly `2013-12-31`, value `0.5`, status `A`, confidence `F`. The returned
series metadata identifies the Bank of England as source. The Bank of England's official
Bank Rate history records 0.50% from 2009-03-05 until the next change in 2016, and its
2013-12-05 decision explicitly maintained Bank Rate at 0.50%.

An attempted BIS attribute-filter diagnostic did not reliably remove a missing
observation and is therefore **not** part of this contract.

## 3. Semantic initialization rule

For each frozen BIS series independently, the initialization state is the finite value
of the source's immediately preceding observation strictly before
`START_INCLUSIVE=2014-01-01`.

The selected observation:

- initializes only the same series;
- is evidence of the administrative state already in force at the boundary;
- is never emitted as a research observation;
- never generates a signal or return and never enters training statistics;
- does not move `START_INCLUSIVE` backward.

## 4. Frozen bounded transport rule

For every one of the eight frozen series, issue exactly one initialization request with:

- dataset: `BIS:WS_CBPOL(1.0)`;
- series key: the same frozen series being initialized;
- `endPeriod=2013-12-31` (`START_INCLUSIVE - 1 calendar day`);
- `lastNObservations=1`;
- no `startPeriod` and no data-dependent lookback;
- SDMX structure-specific XML response.

The response must contain exactly one observation. That observation must be the source's
most recent observation at or before the fixed end bound, must be strictly before
`START_INCLUSIVE`, and must contain a finite numeric value. Because the response is
limited to the single most recent observation, a finite response is necessarily the
latest finite predecessor under this contract.

If the single most recent observation is missing, non-finite, malformed, ambiguous, or
otherwise invalid, acquisition fails closed. The implementation must not search farther
back. This conservative failure rule avoids an arbitrary or post-hoc retrieval horizon.

This exact rule and fixed boundary-derived end date apply identically to all eight
series. No window was enlarged until the observed `D.GB` failure passed.

## 5. Fail-closed requirements

Initialization fails without publication if:

- the response contains zero or more than one observation;
- the series differs from the requested series;
- the selected date is not strictly before `START_INCLUSIVE`;
- the value is missing, non-finite, or malformed;
- duplicates, ordering, or conflicting metadata create ambiguity;
- source metadata indicates an unresolved structural or instrument transition;
- raw provenance or bounded request identity is incomplete;
- any series lacks its own valid initialization evidence.

Cross-series initialization is prohibited.

## 6. Normalization behavior

The normalization version is `bis_cbpol_daily_v3`. Boundary initialization changes the
meaning and evidence identity of a leading persisted state, so reusing v2 would conflate
materially different contracts.

For a leading in-window `NaN/M` observation, v3:

- preserves the source observation as `MISSING` and preserves its raw value/status;
- derives `policy_rate_state` from the same-series initialization state;
- sets `policy_rate_state_origin=PERSISTED`;
- sets `source_state_date` to the actual selected predecessor date.

The first later finite in-window observation resets the active state to that value,
`policy_rate_state_origin=OBSERVED`, and its own observation date. Later missing
observations follow ADR 0015 unchanged.

No pre-boundary record is included in normalized research records or record counts.

## 7. Provenance and canonical identity

For every series, immutable initialization evidence binds:

- provider, dataset, dataset version, and series;
- initialization contract/version and acquisition rule;
- exact request parameters and fixed end bound;
- exact raw initialization bytes, SHA-256, and byte count;
- selected predecessor date and exact decimal value;
- `START_INCLUSIVE` and `END_EXCLUSIVE`;
- normalization version.

The normalized dataset identity binds the ordered set of all eight initialization
evidence identities in addition to the in-window raw evidence identity. Retrieval time,
local path, mtime, and retry behavior remain audit-only and do not enter scientific
identity.

## 8. Research and causal firewall

The research interval remains exactly
`[2014-01-01T00:00:00Z, 2024-01-01T00:00:00Z)`. No 2024+ observation is authorized.

`POINT_IN_TIME_STATUS` remains `UNRESOLVED`. Boundary-state evidence answers what
administrative state was already in force at the start; it does not prove when later
changes were historically knowable at a strategy decision boundary. No causal or
tradable availability API is authorized.

Candidate E remains NOT SELECTED. Performance slots remain 0 / 4. ML, MT5, demo/live
deployment, and real-money trading remain unauthorized.
