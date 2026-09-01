"""Build Candidate B formation evidence from already-validated typed inputs.

This module performs no filesystem discovery, network access, economic measurement, signal
construction, return calculation, or experiment logging.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    MAX_OBSERVATION_DATE,
    AmbiguityState,
    CandidateBFormationManifest,
    ConcordanceStatus,
    FormationSplit,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicyRateObservation,
    PolicyRateQualificationError,
    PolicyRateSeriesManifest,
    PolicyStateReference,
    SpotObservationReference,
    SpotPanelManifestReference,
    canonical_sha256,
    event_is_eligible,
    qualify_formation,
    reconcile_policy_series,
)
from fxlab.research.candidate_b_measurement import (
    MEASURED_MONTHS,
    validate_formation_month,
)


def _fail(reason: str) -> None:
    raise PolicyRateQualificationError(reason)


def _next_month(month: str) -> str:
    year = int(month[:4])
    number = int(month[5:])
    return f"{year + 1:04d}-01" if number == 12 else f"{year:04d}-{number + 1:02d}"


def _validate_series_manifest(manifest: PolicyRateSeriesManifest) -> None:
    if not isinstance(manifest, PolicyRateSeriesManifest):
        _fail("series_manifest_invalid")
    request = manifest.request
    metadata = manifest.metadata
    currency = request.series.currency
    if (
        APPROVED_BIS_SERIES.get(currency) != request.series.series_key
        or metadata.currency != currency
        or metadata.series_key != request.series.series_key
    ):
        _fail("series_manifest_invalid")
    observations = tuple(manifest.observations)
    if not observations:
        _fail("missing_policy_observation")

    dates: list[date] = []
    for item in observations:
        if not isinstance(item, PolicyRateObservation):
            _fail("policy_observation_invalid")
        observed = item.observation_date
        if not isinstance(observed, date):
            _fail("policy_observation_date_invalid")
        if observed > MAX_OBSERVATION_DATE:
            _fail("sealed_window_violation")
        if observed < request.start or observed > request.end:
            _fail("policy_observation_outside_request")
        dates.append(observed)
    if len(dates) != len(set(dates)):
        _fail("duplicate_policy_observation")
    if dates != sorted(dates):
        _fail("policy_observation_order_invalid")
    if any(
        item.series_key != request.series.series_key or item.status != "A" for item in observations
    ):
        _fail("policy_observation_not_numeric")

    observation_hash = canonical_sha256(observations)
    if (
        observation_hash != manifest.canonical_observation_hash
        or manifest.numeric_observation_count != len(observations)
        or manifest.raw_row_count < manifest.numeric_observation_count
        or manifest.parsed_min_observation_date != dates[0]
        or manifest.parsed_max_observation_date != dates[-1]
    ):
        _fail("series_manifest_invalid")
    expected_dataset_id = canonical_sha256(
        {
            "format": 1,
            "request_fingerprint": request.fingerprint,
            "metadata_identity": metadata.stable_identity,
            "raw_sha256": manifest.raw_sha256,
            "canonical_observation_hash": observation_hash,
            "raw_row_count": manifest.raw_row_count,
            "numeric_observation_count": manifest.numeric_observation_count,
            "revision": metadata.revision,
        }
    )
    if expected_dataset_id != manifest.dataset_id:
        _fail("wrong_dataset_identity")
    expected_manifest_id = canonical_sha256(
        {
            "format": 1,
            "dataset_id": expected_dataset_id,
            "retrieved_at": manifest.retrieved_at,
            "byte_count": manifest.byte_count,
            "media_type": metadata.media_type,
        }
    )
    if expected_manifest_id != manifest.manifest_id:
        _fail("series_manifest_invalid")


def _validate_event_manifest(event_manifest: PolicyEventManifest) -> None:
    if not isinstance(event_manifest, PolicyEventManifest):
        _fail("event_manifest_invalid")
    events = tuple(event_manifest.events)
    if any(not isinstance(item, PolicyRateEvent) for item in events):
        _fail("event_manifest_invalid")
    for item in events:
        if (
            item.announcement_upper.date() > MAX_OBSERVATION_DATE
            or item.effective_upper.date() > MAX_OBSERVATION_DATE
        ):
            _fail("sealed_window_violation")
        if item.ambiguity is AmbiguityState.AMBIGUOUS:
            _fail("ambiguous_official_evidence")
        if (
            item.ambiguity is AmbiguityState.CONFLICTING
            or item.conflict is AmbiguityState.CONFLICTING
        ):
            _fail("conflicting_official_evidence")
    rebuilt = PolicyEventManifest(events)
    if rebuilt.manifest_id != event_manifest.manifest_id:
        _fail("event_manifest_invalid")
    for currency in APPROVED_BIS_SERIES:
        baselines = [
            item
            for item in events
            if item.currency == currency and item.kind is PolicyEventKind.BASELINE
        ]
        if len(baselines) != 1:
            _fail("missing_official_baseline")


def _validated_spot_months(
    spot_panel: SpotPanelManifestReference,
) -> dict[str, tuple[datetime, tuple[SpotObservationReference, ...]]]:
    if not isinstance(spot_panel, SpotPanelManifestReference):
        _fail("spot_panel_invalid")
    expected_ids = dict(zip(APPROVED_PAIRS, spot_panel.dataset_ids, strict=True))
    grouped: dict[tuple[str, datetime], dict[str, SpotObservationReference]] = {}
    for item in spot_panel.observations:
        if not isinstance(item, SpotObservationReference):
            _fail("spot_observation_invalid")
        close = item.bar_close
        opened = item.bar_open
        if not isinstance(close, datetime) or close.tzinfo is None or close.utcoffset() is None:
            _fail("spot_observation_date_invalid")
        if close.astimezone(UTC).date() > MAX_OBSERVATION_DATE:
            _fail("sealed_window_violation")
        if item.pair not in expected_ids or item.dataset_id != expected_ids[item.pair]:
            _fail("wrong_dataset_identity")
        if (
            item.closed is not True
            or item.value_field != "close"
            or opened.tzinfo is None
            or close.astimezone(UTC) - opened.astimezone(UTC) != timedelta(days=1)
        ):
            _fail("non_closed_bar")
        normalized_close = close.astimezone(UTC)
        key = (normalized_close.strftime("%Y-%m"), normalized_close)
        by_pair = grouped.setdefault(key, {})
        if item.pair in by_pair:
            _fail("duplicate_spot_pair_at_formation")
        by_pair[item.pair] = item

    required_months = set(MEASURED_MONTHS) | {_next_month(item) for item in MEASURED_MONTHS}
    resolved: dict[str, tuple[datetime, tuple[SpotObservationReference, ...]]] = {}
    for month in required_months:
        month_groups = [
            (close, by_pair)
            for (candidate_month, close), by_pair in grouped.items()
            if candidate_month == month
        ]
        if not month_groups:
            _fail("missing_formation_month")
        complete = [
            (close, by_pair)
            for close, by_pair in month_groups
            if set(by_pair) == set(APPROVED_PAIRS)
        ]
        if not complete:
            _fail("missing_spot_pair")
        close, by_pair = max(complete, key=lambda item: item[0])
        resolved[month] = (close, tuple(by_pair[pair] for pair in APPROVED_PAIRS))
    return resolved


def _policy_state(
    *,
    currency: str,
    manifest: PolicyRateSeriesManifest,
    events: tuple[PolicyRateEvent, ...],
    cutoff: datetime,
) -> PolicyStateReference:
    eligible = tuple(
        item for item in events if item.currency == currency and event_is_eligible(item, cutoff)
    )
    if not eligible:
        _fail("missing_official_state")
    latest_event = max(
        eligible,
        key=lambda item: (item.effective_upper, item.announcement_upper, item.event_id),
    )
    available = tuple(
        item for item in manifest.observations if item.observation_date <= cutoff.date()
    )
    if not available:
        _fail("missing_policy_observation")
    observation = max(available, key=lambda item: item.observation_date)
    if observation.status != "A":
        _fail("policy_observation_not_numeric")
    if observation.series_key != manifest.request.series.series_key:
        _fail("event_observation_mismatch")
    if latest_event.new_rate != observation.value:
        _fail("event_observation_mismatch")
    if not event_is_eligible(latest_event, cutoff):
        _fail("policy_state_not_point_in_time_eligible")
    return PolicyStateReference(
        currency=currency,
        series_key=manifest.request.series.series_key,
        dataset_id=manifest.dataset_id,
        observation_id=observation.identity,
        event_id=latest_event.event_id,
        policy_instrument_id=latest_event.policy_instrument_id,
        observation_date=observation.observation_date,
        observation_value=observation.value,
        observation_status=observation.status,
        announcement_upper=latest_event.announcement_upper,
        effective_upper=latest_event.effective_upper,
        eligible=True,
    )


def build_candidate_b_formation_manifest(
    *,
    series_manifests: Sequence[PolicyRateSeriesManifest],
    event_manifest: PolicyEventManifest,
    spot_panel: SpotPanelManifestReference,
) -> CandidateBFormationManifest:
    """Build the exact frozen 106 measured formations from validated typed evidence."""

    manifests = tuple(series_manifests)
    currencies = tuple(
        item.request.series.currency
        for item in manifests
        if isinstance(item, PolicyRateSeriesManifest)
    )
    if len(manifests) < len(APPROVED_BIS_SERIES) or set(currencies) != set(APPROVED_BIS_SERIES):
        _fail("missing_series")
    if len(manifests) != len(APPROVED_BIS_SERIES) or len(currencies) != len(set(currencies)):
        _fail("duplicate_series")
    for manifest in manifests:
        _validate_series_manifest(manifest)
    _validate_event_manifest(event_manifest)
    concordance = tuple(reconcile_policy_series(item, event_manifest) for item in manifests)
    if any(item.status is not ConcordanceStatus.PASS for item in concordance):
        _fail("concordance_failed")
    spot_months = _validated_spot_months(spot_panel)
    by_currency = {item.request.series.currency: item for item in manifests}
    source_ids = tuple(sorted(item.manifest_id for item in manifests)) + (
        event_manifest.manifest_id,
        spot_panel.manifest_id,
    )
    events = tuple(event_manifest.events)
    formations = []
    for index, month in enumerate(MEASURED_MONTHS):
        period = validate_formation_month(month)
        formation_at, spot_references = spot_months[month]
        exit_at, _ = spot_months[period.exit_month]
        cutoff = datetime.combine(formation_at.date(), time.min, tzinfo=UTC)
        states = tuple(
            _policy_state(
                currency=currency,
                manifest=by_currency[currency],
                events=events,
                cutoff=cutoff,
            )
            for currency in APPROVED_BIS_SERIES
        )
        formation = qualify_formation(
            cohort_id=f"candidate_b_{index:03d}_{month.replace('-', '_')}",
            formation_month=month,
            formation_at=formation_at,
            cutoff_at=cutoff,
            exit_at=exit_at,
            split=FormationSplit(period.split.value),
            purged=False,
            spot_observations=spot_references,
            policy_states=states,
            source_manifest_fingerprints=source_ids,
        )
        if not formation.complete:
            _fail(formation.rejection_reason or "formation_incomplete")
        formations.append(formation)
    manifest = CandidateBFormationManifest(tuple(formations))
    if (
        tuple(item.formation_month for item in manifest.formations) != MEASURED_MONTHS
        or not manifest.qualified
    ):
        _fail("measured_formation_contract_incomplete")
    return manifest
