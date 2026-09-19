# ADR 0017 — BIS Policy-Rate Point-in-Time Causal Availability Qualification

- **Status:** Accepted — Partial evidence / implementation blocked
- **Research gate:** Historical point-in-time causal availability at the FXLab 00:00 UTC boundary
- **Protocol family:** `post_test_8_research_family_v1`
- **Candidate E status:** NOT SELECTED (0 / 4 performance-test slots consumed)
- **Supplements:** ADR 0014, ADR 0015, and ADR 0016; none is modified
- **Canonical numerical evidence:** `bis_cbpol_daily_v3`
- **Canonical normalized identity:** `e6b19f1ba520242e2d98c1a0ef55dcf6b0769f0af728324b00cb6f53a9d822ed`
- **Point-in-time decision:** `POINT_IN_TIME_PARTIALLY_PROVEN`
- **Point-in-time status:** `UNRESOLVED`

---

## 1. Decision and scope

Official sources prove that historically causal policy-rate timing can be established for
specific institutions, timing regimes, and decisions. They do not yet provide a frozen,
reproducible evidence set covering every required state change and every initialization
state for all eight frozen BIS series over 2014–2023. A complete causal availability layer
is therefore not authorized.

This is a data-governance result, not a performance result. Candidate E remains not
selected and the post-Test-8 research family remains at 0 / 4 performance tests.

## 2. Why the BIS observation date is insufficient

`BIS:WS_CBPOL(1.0)` supplies daily administrative policy-rate observations. Its daily
observation date is not an announcement timestamp, publication timestamp, or complete
knowledge timestamp. BIS states that daily data are reported by member central banks and
released around mid-week; the dataset documentation says that information about the lag
between announcement and effectiveness is included only "as far as possible".

Consequently, neither an `OBSERVED` BIS row nor a `PERSISTED` row may create
`announced_at`, `effective_at`, `available_at`, or an eligible FXLab boundary. ADR 0015
state persistence remains valid only after the originating state has independently passed a
causal-availability contract.

## 3. Required causal property

For a state `R` and FXLab boundary `T`, eligibility would require independently sourced
facts proving both public announcement and administrative applicability before the
boundary:

`AVAILABLE_AT(R) = max(ANNOUNCED_AT(R), EFFECTIVE_AT(R))`

`R` is eligible only when `AVAILABLE_AT(R) < T`.

This expression is a qualification requirement, not an implemented rule. If an official
source provides only a source-local calendar date, a future contract could conservatively
use the first 00:00 UTC boundary strictly after the entire later source-local calendar day.
That is a granularity rule derived from the uncertainty interval of a date-only fact, not a
claim that the event occurred at midnight. The rule is not adopted here because complete
event coverage, timezone evidence, and instrument mapping have not been frozen.

Same-instant ambiguity, unresolved timezone or DST, conflicting sources, missing required
announcement/effective facts, and instrument ambiguity must fail closed.

## 4. Authoritative sources examined

Only primary institutional sources were used for governance-critical findings:

| Authority | Official source | What the source proves | Limitation for this gate |
|---|---|---|---|
| BIS | [Central bank policy rates — data documentation](https://www.bis.org/statistics/cbpol/cbpol_doc.pdf) and [BIS Data Portal overview](https://data.bis.org/topics/CBPOL) | Series definitions, national sources, instrument splices/breaks, and the fact that announcement-to-effectiveness lag metadata exists only where available | Does not provide a complete historical publication timestamp for each frozen daily observation |
| RBA | [Monetary Policy Decisions archive](https://www.rba.gov.au/monetary-policy/int-rate-decisions/) and [About monetary policy](https://www.rba.gov.au/monetary-policy/about.html) | Decisions are announced at 2:30 pm Sydney time; changes take effect the following day; annual decision archives exist | A complete immutable event inventory and timezone conversion evidence has not been frozen |
| Bank of Canada | [2014 announcement schedule](https://www.bankofcanada.ca/wp-content/uploads/2013/07/press_230713.pdf), [policy interest rate](https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/), and official decision releases | The target for the overnight rate, fixed announcement dates, and the historical 10:00 ET release regime | Exceptional events and every state change have not been bound into a frozen event inventory |
| SNB | [Official interest rates](https://data.snb.ch/en/topics/snb/cube/snboffzisa), [13 June 2019 assessment](https://www.snb.ch/public/asset/en/www-snb-ch/publications/communication/press-releases/2019/pre_20190613/publications0_en/pre_20190613.en.pdf), and [15 January 2015 decision](https://www.snb.ch/en/publications/communication/press-releases/2015/pre_20150115) | The June 2019 replacement of the three-month CHF Libor target-range midpoint by the SNB policy rate, plus examples of regular and irregular decisions | Archive dates do not constitute a complete frozen set of announcement/effective timestamps; irregular actions require event-specific proof |
| Bank of England | [2014 MPC release example](https://www.bankofengland.co.uk/news/2014/april/mpc-april-2014) and official MPC documentation/archive | Bank Rate identity and the historical noon London announcement regime | Every state change and effective fact has not been frozen as causal evidence |
| Bank of Japan | [Past Monetary Policy Meetings](https://www.boj.or.jp/en/mopo/mpmsche_minu/past.htm), [statements archive](https://www.boj.or.jp/en/mopo/mpmdeci/state_all/index.htm), and BIS documentation | Official meeting/statement dates and major framework transitions | Statement release time is variable (generally immediately after meetings), and BIS documents no adopted policy rate from 4 April 2013 through 20 September 2016 |
| RBNZ | [2014 MPS archive example](https://www.rbnz.govt.nz/hub/publications/monetary-policy-statement/monetary-policy-statement-september-2014), [2021 release schedule](https://www.rbnz.govt.nz/news-and-events/news/2020/02/mps-ocr-and-fsr-dates-for-2021), and [release process](https://www.rbnz.govt.nz/news-and-events/how-we-release-information) | OCR decisions, official archives, later 2:00 pm NZT release regime, and next-working-day market implementation | The historical release-time regime and each change/effective date are not proven end-to-end for 2014–2023 |
| Federal Reserve | [FOMC calendars and statements](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) and an [official 2023 statement](https://www.federalreserve.gov/newsevents/pressreleases/monetary20230726a.htm) | Target-range decisions and exact release-time evidence on official statements | A complete frozen mapping from each midpoint change to announcement and implementation facts has not been created |
| ECB | [Key ECB interest rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html), [5 June 2014 decision](https://www.ecb.europa.eu/press/pr/date/2014/html/pr140605.en.html), and [2022 publication-time change](https://www.ecb.europa.eu/press/pr/date/2022/html/ecb.pr220627~73acedf868.en.html) | MRO effective dates, official decision releases, the 13:45 CET regime, and its change to 14:15 CET from July 2022 | The complete set of decision/effective facts and timezone-normalized identities has not been frozen |

Modern archive-page publication metadata is not treated as a historical announcement
timestamp unless the institution explicitly says so.

## 5. Coverage qualification by currency

| Currency | Timing coverage | Instrument continuity | Qualification |
|---|---|---|---|
| AUD | Official archive and institution-wide announcement/effect convention provide a reproducible route for scheduled decisions | Cash rate target is stable throughout 2014–2023 | Meaningful subset proven; event-level evidence set not frozen |
| CAD | Official schedules and releases establish the normal 10:00 ET regime and overnight-rate target | Target for the overnight rate is stable throughout the interval | Meaningful subset proven; exceptional/event-level completeness not frozen |
| CHF | Individual regular and irregular decisions are sourceable | BIS explicitly splices the three-month CHF Libor target-range midpoint to the SNB policy rate on 13 June 2019 | Partial; transition and every irregular action need event-level causal binding |
| GBP | Official MPC releases and documentation establish the noon London regime | Official Bank Rate is stable throughout the interval | Meaningful subset proven; complete event inventory not frozen |
| JPY | Official statements establish decision dates, but release time is not a single fixed schedule | **Blocking discontinuity:** BIS documents no adopted policy rate from 4 April 2013 through 20 September 2016, followed by the short-term policy interest rate under yield-curve control | Unsupported for complete 2014–2023 policy-rate-differential input |
| NZD | Official OCR decisions are archived; later releases prove a 2:00 pm NZT regime and next-working-day implementation | OCR is stable as the administrative instrument | Partial; complete historical timing-regime transition and event mapping not frozen |
| USD | Official FOMC statements can supply release times and target-range decisions | BIS midpoint of the federal-funds target range is stable throughout the interval | Meaningful subset proven; complete implementation mapping not frozen |
| EUR | Official releases supply announcement and MRO effective dates; official documentation records the July 2022 publication-time change | BIS MRO fixed rate is stable throughout the frozen interval | Meaningful subset proven; complete event inventory not frozen |

No row in this table authorizes causal promotion of the canonical v3 data.

## 6. Initialization-state causality

ADR 0016 proves the administrative value immediately preceding 2014-01-01 for each
series. It does not prove when each predecessor state was announced or became effective.
The cited Bank of England evidence is a useful example, not cross-series proof. All eight
initialization states therefore remain causally unqualified until their originating official
decisions and effective facts are independently bound.

## 7. Weekends, holidays, and persistence

Once an originating state has been causally qualified, ADR 0015 persistence across later
weekends, holidays, and eligible missing observations may retain that original eligibility.
The persisted BIS row never creates a new availability timestamp. A new or changed state
must pass the causal gate independently.

## 8. Missing evidence and fail-closed behavior

A future evidence contract must mark a state unavailable or reject the affected interval
for any of the following:

- missing required announcement or effective fact;
- unresolved timezone or DST conversion;
- ambiguous same-instant ordering;
- source conflict or chronology conflict;
- insufficient historical archive provenance;
- uncertain series/instrument mapping or transition;
- a required inference from later information;
- incomplete event or initialization coverage.

No universal `+1 day`, `+1 business day`, `+24 hours`, or other arbitrary lag is adopted.
No timestamp is manufactured.

## 9. Required evidence before reconsideration

Reconsideration requires an immutable, official-source-derived event inventory that:

1. covers every value-changing event and every initialization state for all eight series;
2. binds institution, instrument, state value/date, announcement and effective source
   facts, source timezone, and stable source/content identity;
3. explicitly represents instrument transitions and date-only uncertainty;
4. independently reconciles each event to canonical BIS v3 without using a BIS
   observation date as a knowledge timestamp; and
5. passes a frozen boundary rule before any causal availability implementation exists.

The JPY 2014–20 September 2016 no-policy-rate interval additionally requires a separate
governance decision about whether any administrative state is scientifically defined for
the intended hypothesis. It must not be filled by inference.

## 10. Authorization boundary

- **Point-in-time status:** `UNRESOLVED`
- **Causal availability implementation:** NOT AUTHORIZED
- **Candidate E:** NOT SELECTED
- **Performance tests consumed:** 0 / 4
- **Canonical BIS v3 evidence:** PRESERVED; no reacquisition or rewrite
- **2024+ FX data:** SEALED
- **Machine learning:** NOT AUTHORIZED
- **MT5 / demo / live / real-money trading:** NOT AUTHORIZED

`INFRASTRUCTURE CONFIDENCE != TRADING EDGE` and
`DATA AVAILABILITY != CAUSAL AVAILABILITY` remain controlling principles.
