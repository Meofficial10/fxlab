"""Offline Candidate B data qualification aggregation.

This module accepts already validated local contracts.  It performs no network access and contains
no signal, return, portfolio, or performance calculation.
"""

from __future__ import annotations

from collections.abc import Sequence

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    CandidateBFormationManifest,
    CandidateBQualificationResult,
    ConcordanceStatus,
    PolicyConcordanceResult,
    PolicyEventManifest,
    PolicyRateSeriesManifest,
    SpotPanelManifestReference,
    event_is_eligible,
    reconcile_policy_series,
)


def qualify_candidate_b(
    *,
    series_manifests: Sequence[PolicyRateSeriesManifest],
    event_manifest: PolicyEventManifest,
    concordance_results: Sequence[PolicyConcordanceResult],
    spot_panel: SpotPanelManifestReference,
    formation_manifest: CandidateBFormationManifest,
) -> CandidateBQualificationResult:
    series = tuple(series_manifests)
    concordance = tuple(concordance_results)
    reasons: set[str] = set()
    currencies = {item.request.series.currency for item in series}
    if len(series) != len(APPROVED_BIS_SERIES) or currencies != set(APPROVED_BIS_SERIES):
        reasons.add("missing_series_manifest")
    result_currencies = {item.currency for item in concordance}
    if len(concordance) != len(APPROVED_BIS_SERIES) or result_currencies != set(
        APPROVED_BIS_SERIES
    ):
        reasons.add("missing_concordance_result")
    if any(item.status is not ConcordanceStatus.PASS for item in concordance):
        reasons.add("concordance_failed")
    series_by_currency = {item.request.series.currency: item for item in series}
    result_by_currency = {item.currency: item for item in concordance}
    if any(
        currency not in result_by_currency
        or result_by_currency[currency].series_dataset_id != manifest.dataset_id
        or result_by_currency[currency].event_manifest_id != event_manifest.manifest_id
        for currency, manifest in series_by_currency.items()
    ):
        reasons.add("concordance_provenance_mismatch")
    for currency, manifest in series_by_currency.items():
        supplied = result_by_currency.get(currency)
        if supplied is not None:
            expected = reconcile_policy_series(manifest, event_manifest)
            if supplied.result_id != expected.result_id:
                reasons.add("concordance_result_mismatch")
    expected_spot_ids = dict(zip(APPROVED_PAIRS, spot_panel.dataset_ids, strict=True))
    required_source_manifests = {
        *(item.manifest_id for item in series),
        event_manifest.manifest_id,
        spot_panel.manifest_id,
    }
    spot_index = set(spot_panel.observations)
    events_by_id = {item.event_id: item for item in event_manifest.events}
    for formation in formation_manifest.formations:
        if not formation.complete:
            continue
        spot_ids = {item.pair: item.dataset_id for item in formation.spot_observations}
        policy_ids = {item.currency: item.dataset_id for item in formation.policy_states}
        if (
            spot_ids != expected_spot_ids
            or any(
                currency not in policy_ids or policy_ids[currency] != manifest.dataset_id
                for currency, manifest in series_by_currency.items()
            )
            or not required_source_manifests.issubset(formation.source_manifest_fingerprints)
        ):
            reasons.add("formation_provenance_mismatch")
        unresolved = False
        for spot in formation.spot_observations:
            if spot not in spot_index or spot.bar_close != formation.formation_at:
                unresolved = True
        for state in formation.policy_states:
            manifest = series_by_currency.get(state.currency)
            event = events_by_id.get(state.event_id)
            supplied_concordance = result_by_currency.get(state.currency)
            if manifest is None or event is None or supplied_concordance is None:
                unresolved = True
                continue
            observation = next(
                (item for item in manifest.observations if item.identity == state.observation_id),
                None,
            )
            eligible_events = tuple(
                item
                for item in event_manifest.events
                if item.currency == state.currency
                and item.policy_instrument_id == state.policy_instrument_id
                and event_is_eligible(item, formation.cutoff_at)
            )
            latest_event = (
                max(
                    eligible_events,
                    key=lambda item: (
                        item.effective_upper,
                        item.announcement_upper,
                        item.event_id,
                    ),
                )
                if eligible_events
                else None
            )
            if (
                observation is None
                or observation.series_key != state.series_key
                or observation.observation_date != state.observation_date
                or observation.value != state.observation_value
                or observation.status != state.observation_status
                or event.currency != state.currency
                or event.policy_instrument_id != state.policy_instrument_id
                or event.new_rate != observation.value
                or event.announcement_upper != state.announcement_upper
                or event.effective_upper != state.effective_upper
                or latest_event is None
                or latest_event.event_id != event.event_id
                or supplied_concordance.status is not ConcordanceStatus.PASS
            ):
                unresolved = True
        if unresolved:
            reasons.add("formation_reference_unresolved")
    if not formation_manifest.qualified:
        reasons.add("cohort_count_mismatch")
    return CandidateBQualificationResult(
        qualified=not reasons,
        reasons=tuple(reasons),
        series_manifest_ids=tuple(item.manifest_id for item in series),
        event_manifest_id=event_manifest.manifest_id,
        concordance_result_ids=tuple(item.result_id for item in concordance),
        spot_manifest_id=spot_panel.manifest_id,
        formation_manifest_id=formation_manifest.manifest_id,
    )


def main() -> None:
    raise SystemExit(
        "Offline qualification requires explicitly supplied validated local manifests; no default "
        "data path or network fallback exists."
    )


if __name__ == "__main__":
    main()
