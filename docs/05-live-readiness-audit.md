# Phase 20 Live-Readiness Audit

**Audit date:** 2026-08-29  
**Readiness schema:** 1  
**Audited system commit:** `f3ce10f32f17b6f7dbe793a40967ad8c4e2e3143`  
**Report implementation commit:** intentionally unset until Phase 20 is committed  
**Canonical report fingerprint:** `cc02258939bc76545fb4cd94bfffdc32e52af94385e6e4d638ccf9ba966feca3`

> **INFRASTRUCTURE CONFIDENCE != TRADING EDGE**

This audit reports evidence. It does not enable a strategy, authorize an order, clear a
kill switch, bypass reconciliation, select an external broker, or authorize live money.
A PASS for paper, observation-service, or adapter infrastructure does not authorize
forward strategy execution or live trading.

## Evaluation method

`fxlab.readiness` evaluates a fixed set of mandatory checks for each target. A target is
GO only when every mandatory check is PASS. FAIL, BLOCKED, UNVERIFIED, and missing
mandatory checks produce NO-GO. NOT_APPLICABLE is accepted only for an explicitly
allowed check/target pair. There is no manual override and no infrastructure-based
promotion.

Evidence is bound to the audited Phase 1–19 commit above. Test totals alone are not
evidence: strict structured references must also be present in the audit's explicit
validated evidence inventory. A syntactically valid but uninventoried reference is
downgraded to UNVERIFIED.
The later commit containing this report is a separate identity and is not allowed to
make the audited baseline self-referential.

## Target verdicts

| Target | Calculated verdict | Meaning |
|---|---|---|
| Deterministic paper/replay infrastructure | **GO** | Deterministic validation infrastructure is supported within its declared model limits. It does not prove edge. |
| Local observation-only service | **GO** | The foreground, local trusted-user, PaperBroker-only/no-signal service boundary is supported. |
| OANDA Practice adapter library | **GO** | The constrained injected-client adapter contract is supported. Real Practice connectivity remains separate evidence. |
| OANDA Practice forward strategy | **NO-GO** | Research edge, R7/R8, external reconciliation, and broker-forward evidence are absent or blocked. |
| Live-money readiness | **NO-GO** | Multiple mandatory research, broker, reconciliation, realism, deployment, clock, secret, and storage checks are blocked or unverified. |

## Verified facts and named evidence

- The research record remains P4 NO-GO with no validated strategy edge:
  `docs/AUDIT-2026-08-25.md` and
  `docs/adr/0001-p4-no-go-smc-rule-baseline.md`.
- R7 paper-forward and R8 broker-demo gates are not eligible while the preceding
  research gate has not passed: `docs/03-paper-trading-architecture.md`.
- Point-in-time bar closure, future-data rejection, provider identity, deterministic
  replay, and no hidden fallback are exercised by the data-provider suites.
- Risk limits, immutable valuation evidence, conservative sizing, reservations, and
  kill-switch behavior are exercised by the risk and order-manager suites.
- PaperBroker accounting, spread, commission, valuation, margin identity, and recovery
  are exercised by the Phase 7–18 suites.
- PaperBroker exact-ID reconciliation, uncertainty/reservation preservation, and
  fail-closed non-trading reconciliation are mandatory deterministic-paper evidence.
- The OANDA adapter is Practice-only, USD-only, hedging/FOK/OPEN_ONLY, four-symbol,
  one-attempt, and fail-closed for uncertain or partial outcomes. Its inventoried
  descriptor identity is broker ID `oanda-v20`, implementation version `2`.
- SQLite event/checkpoint checksums, schema identity, sequence continuity, and corrupt
  state rejection are exercised by durable-store and recovery tests.
- The Phase 19 service is structurally observation-only and uses bounded authenticated
  local byte IPC with redacted file-backed control material.

## Current blocking evidence

Forward strategy and/or live money are blocked by:

- `research_p4_no_go`: no validated edge.
- `r7_not_eligible` and `r8_not_eligible`.
- `external_reconciliation_not_implemented`: no authoritative OANDA restart/order/
  trade/account reconciliation coordinator.
- `live_broker_not_implemented`: the external adapter rejects non-Practice authority.
- `broker_forward_evidence_missing` and `live_execution_realism_incomplete`.
- `live_secret_controls_unverified`: no Windows ACL verification, active revocation,
  crash-dump hardening, or production secret store.
- `production_deployment_evidence_missing`: no supported supervisor, rollback rehearsal,
  or complete machine-hardening proof.
- `clock_synchronization_unverified`: UTC validation exists, but deployment drift
  monitoring/enforcement does not.
- `disk_full_backup_restore_unverified`: logical store failure and corruption paths are
  covered, but physical disk-full and validated backup/restore evidence are absent.

## Execution-model limitations

- Historical replay observes a bar-close quote and makes no intrabar path claim.
- Paper fills are immediate; no nonzero latency is modeled.
- Paper slippage is deterministic and uses `norm_vol = 0`.
- Partial fills and proportional reservation accounting are unsupported.
- The paper application can explicitly use unmodeled margin; it is not broker margin.
- OANDA scope is limited to four USD-quoted FX instruments and a USD account.

These limits are compatible with the stated deterministic-paper target but block claims
of broker equivalence or live-money realism.

## Security and deployment limitations

- The Phase 19 security model is local and trusted-user oriented, not a hostile
  multi-user boundary.
- A control secret must contain at least 32 bytes; entropy is not measured.
- Mapped-network-drive locality is not guaranteed.
- Windows ACL enforcement, production rotation/revocation, service supervision, clock
  policy, capacity monitoring, and validated backup/restore remain unverified.

## Fault evidence

The inventory classifies each claim as DIRECTLY_TESTED,
PREEXISTING_TEST_EVIDENCE, or UNVERIFIED:

- DIRECTLY_TESTED: SQLite corruption, secret-read failure, instance-lock contention,
  and stalled authentication.
- PREEXISTING_TEST_EVIDENCE: submission uncertainty, exact fill/reflection repair,
  close-accounting uncertainty, audit/checkpoint failure, provider failure, broker
  timeout uncertainty, control-listener failure, and logging failure.
- UNVERIFIED: physical disk-full behavior, a combined stale/future/corrupt-data fault,
  and full shutdown completion during an active serialized cycle. Narrower component
  behavior may have tests, but those tests are not promoted into the broader claim.

## Final audit conclusion

Deterministic paper infrastructure, the local observation-only service, and the
constrained OANDA Practice adapter library may be used only within their stated scopes.
There is no validated trading edge and no permission to route a strategy to OANDA.

**LIVE-MONEY READINESS: NO-GO**
