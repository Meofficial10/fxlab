# ADR 0007 — Candidate B rejected as infeasible: unavailable pre-2016 JPY policy state

- **Status:** Accepted (2026-09-08)
- **Phase gate:** Candidate B data, provenance, and completeness gate before R4
- **Decision:** **CANDIDATE B REJECTED AS INFEASIBLE**
- **Extends:** ADR 0006 without amending or superseding it

## Context

[ADR 0006](0006-r2-candidate-b-public-policy-rate-differential-preregistration.md)
freezes Candidate B before measurement. It requires:

- the seven-currency formation universe, including JPY, at every measured
  formation;
- BIS `D.JP` as the JPY policy-rate series, with no substitute policy
  instrument;
- exactly 106 measured formations from January 2015 through November 2023;
- a numeric policy state supported by eligible point-in-time official-event
  and BIS observation evidence for every required currency at every formation;
- rejection rather than a shifted start, smaller universe, missing cohort, or
  imputed observation when those requirements cannot be established; and
- rejection as infeasible if exactly 106 complete cohorts cannot be
  established without imputation.

The sealed authoritative `D.JP` publication identifies its first usable
numeric policy-rate observation as 2016-09-21. No usable numeric
`PolicyRateObservation` exists before that date. Earlier `M + NaN` evidence is
the frozen BIS nonnumeric missing marker and cannot create a numeric
observation or policy state.

The verified Bank of Japan evidence dated 2016-09-21 supports the short-term
Policy-Rate Balance rate under QQE with Yield Curve Control from that date. It
cannot establish a JPY policy state for an earlier formation. Existing
point-in-time state persistence can carry only a state already established by
eligible authoritative evidence; it cannot operate backward in time.

The controlling implementation remains consistent with this boundary:

- `APPROVED_BIS_SERIES` binds JPY to `D.JP`, and `PolicyStateReference`
  requires status `A` and a numeric value in
  [`policy_rates.py`](../../src/fxlab/data/policy_rates.py).
- `MEASURED_MONTHS` fixes all 106 measured formations in
  [`candidate_b_measurement.py`](../../src/fxlab/research/candidate_b_measurement.py).
- The formation builder requires a policy state for every approved currency
  at every measured formation and fails closed when no eligible state or
  observation exists in
  [`build_candidate_b_formations.py`](../../scripts/build_candidate_b_formations.py).

## Decision

Candidate B is **REJECTED AS INFEASIBLE** under the existing ADR 0006
fail-closed rule.

The frozen evidence and formation requirements cannot produce the exact 106
complete cohorts because JPY policy state cannot be established for the
required formations before 2016-09-21. This is a data-contract and formation
feasibility rejection made before R4. It is not a performance rejection and
does not imply that Candidate B has no economic edge.

The following possible treatments are not permitted by the frozen contract:

1. Excluding the early JPY formations or starting measurement in 2016 would
   change the exact 106-cohort schedule.
2. Removing JPY from early formations would change the frozen universe.
3. Substituting another pre-2016 Bank of Japan instrument would change the
   frozen policy-rate family and series mapping.
4. Treating the 2016-09-21 observation or event as an earlier baseline would
   leak evidence backward in time.
5. Filling, copying, interpolating, inferring, synthesizing, or numerically
   converting `M + NaN` would violate the frozen missing-observation contract.
6. Acquiring additional documents cannot rescue the candidate by changing
   the already-frozen `D.JP` numeric-observation requirement.

## Consequences

- Candidate B does not proceed to R4, qualification, formation construction,
  signal generation, return calculation, ranking, portfolio construction, or
  performance measurement.
- The 2024+ boundary remains sealed.
- No Candidate B outcome has been observed or used in this decision.
- ADR 0006 and its content hash remain unchanged. This decision applies its
  existing rejection rule and changes no hypothesis, universe, instrument,
  formation date, signal, ranking, portfolio weight, FX return construction,
  cost, statistical method, threshold, robustness gate, or cohort count.
- Existing evidence may be retained and audited. Maintenance that preserves
  the frozen contract may correct independently demonstrated provenance or
  factual defects, but no further acquisition or construction is necessary
  to qualify or measure this Candidate B specification.
- A future design with a different JPY instrument, universe, or start date
  would be a distinct, prospectively preregistered candidate. It would not be
  an amendment or rescue of Candidate B and must not use Candidate B outcomes
  to select its rules.
