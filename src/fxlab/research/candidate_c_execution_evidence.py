"""Offline, non-measuring Candidate C execution evidence.

Only the 00h UTC Dukascopy hourly BI5 partition is eligible for each pair/day.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from fxlab.data.bi5_mirror import Bi5PartitionState, download_hour
from fxlab.data.dukascopy_provider import (
    BI5_RESEARCH_END,
    BI5_RESEARCH_START,
    DukascopyBi5DirectoryTransport,
    DukascopyTransportFailure,
    decode_dukascopy_bi5_hour,
)
from fxlab.data.provider import ProviderFailureCategory

CANDIDATE_C_EXECUTION_SCHEMA = "candidate_c_execution_evidence.v1"
CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA = "candidate_c_execution_evidence_manifest.v1"
CANDIDATE_C_EXECUTION_SOURCE_REFERENCE = "dukascopy:datafeed:bi5:hourly:bid"
CANDIDATE_C_EXECUTION_DECODER_VERSION = "dukascopy_bi5_tick_decoder_v1"
CANDIDATE_C_EXECUTION_PAIRS = (
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
)
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0


class CandidateCExecutionState(StrEnum):
    AVAILABLE = "available"
    ABSENT_EVIDENCED = "absent_evidenced"
    EMPTY_EVIDENCED = "empty_evidenced"
    NO_VALID_TICK = "no_valid_tick"
    INVALID_PARTITION = "invalid_partition"
    MISSING_LOCAL_PARTITION = "missing_local_partition"


class CandidateCExecutionCohortState(StrEnum):
    AVAILABLE = "available"
    EXECUTION_EVIDENCE_INCOMPLETE = "execution_evidence_incomplete"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _validate_scope(
    start: datetime, end: datetime, pairs: Sequence[str]
) -> tuple[datetime, datetime, tuple[str, ...]]:
    start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
    if start_utc < BI5_RESEARCH_START or end_utc > BI5_RESEARCH_END:
        raise ValueError("research_window_violation")
    if start_utc >= end_utc:
        raise ValueError("range_invalid")
    if any(
        value.hour or value.minute or value.second or value.microsecond
        for value in (start_utc, end_utc)
    ):
        raise ValueError("daily_boundary_required")
    requested = tuple(pairs)
    if not requested or any(pair not in CANDIDATE_C_EXECUTION_PAIRS for pair in requested):
        raise ValueError("symbol_unsupported")
    if len(set(requested)) != len(requested):
        raise ValueError("duplicate_pair")
    ordered = tuple(pair for pair in CANDIDATE_C_EXECUTION_PAIRS if pair in requested)
    return start_utc, end_utc, ordered


@dataclass(frozen=True)
class CandidateCExecutionEvidence:
    schema_version: str
    pair: str
    intended_boundary: datetime
    window_start: datetime
    window_end: datetime
    source_reference: str
    decoder_version: str
    underlying_partition_state: str
    state: CandidateCExecutionState
    raw_sha256: str | None
    raw_byte_count: int | None
    absence_evidence_type: str | None
    selected_tick_offset_ms: int | None
    selected_tick_timestamp: datetime | None
    bid: float | None
    ask: float | None
    bid_volume: float | None
    ask_volume: float | None
    evidence_id: str


def _record_payload(record: CandidateCExecutionEvidence) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "pair": record.pair,
        "intended_boundary": _iso_utc(record.intended_boundary),
        "window_start": _iso_utc(record.window_start),
        "window_end": _iso_utc(record.window_end),
        "source_reference": record.source_reference,
        "decoder_version": record.decoder_version,
        "underlying_partition_state": record.underlying_partition_state,
        "state": record.state.value,
        "raw_sha256": record.raw_sha256,
        "raw_byte_count": record.raw_byte_count,
        "absence_evidence_type": record.absence_evidence_type,
        "selected_tick_offset_ms": record.selected_tick_offset_ms,
        "selected_tick_timestamp": (
            _iso_utc(record.selected_tick_timestamp)
            if record.selected_tick_timestamp is not None else None
        ),
        "bid": record.bid.hex() if record.bid is not None else None,
        "ask": record.ask.hex() if record.ask is not None else None,
        "bid_volume": record.bid_volume.hex() if record.bid_volume is not None else None,
        "ask_volume": record.ask_volume.hex() if record.ask_volume is not None else None,
    }


def _make_record(
    *,
    pair: str,
    boundary: datetime,
    state: CandidateCExecutionState,
    partition_state: str,
    raw_sha256: str | None = None,
    raw_byte_count: int | None = None,
    absence_evidence_type: str | None = None,
    selected_tick_offset_ms: int | None = None,
    selected_tick_timestamp: datetime | None = None,
    bid: float | None = None,
    ask: float | None = None,
    bid_volume: float | None = None,
    ask_volume: float | None = None,
) -> CandidateCExecutionEvidence:
    values = {
        "schema_version": CANDIDATE_C_EXECUTION_SCHEMA,
        "pair": pair,
        "intended_boundary": boundary,
        "window_start": boundary,
        "window_end": boundary + timedelta(hours=1),
        "source_reference": CANDIDATE_C_EXECUTION_SOURCE_REFERENCE,
        "decoder_version": CANDIDATE_C_EXECUTION_DECODER_VERSION,
        "underlying_partition_state": partition_state,
        "state": state,
        "raw_sha256": raw_sha256,
        "raw_byte_count": raw_byte_count,
        "absence_evidence_type": absence_evidence_type,
        "selected_tick_offset_ms": selected_tick_offset_ms,
        "selected_tick_timestamp": selected_tick_timestamp,
        "bid": bid,
        "ask": ask,
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "evidence_id": "",
    }
    provisional = CandidateCExecutionEvidence(**values)
    values["evidence_id"] = _sha(_record_payload(provisional))
    return CandidateCExecutionEvidence(**values)


@dataclass(frozen=True)
class CandidateCExecutionEvidenceManifest:
    schema_version: str
    start: datetime
    end: datetime
    pairs: tuple[str, ...]
    records: tuple[CandidateCExecutionEvidence, ...]
    state_counts: tuple[tuple[str, int], ...]
    total_count: int
    available_count: int
    manifest_id: str

    @property
    def complete(self) -> bool:
        return self.total_count > 0 and self.available_count == self.total_count


def _manifest_payload(
    start: datetime,
    end: datetime,
    pairs: tuple[str, ...],
    records: tuple[CandidateCExecutionEvidence, ...],
    state_counts: tuple[tuple[str, int], ...],
) -> dict[str, object]:
    return {
        "schema_version": CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "pairs": list(pairs),
        "record_evidence_ids": [record.evidence_id for record in records],
        "state_counts": [list(item) for item in state_counts],
        "total_count": len(records),
        "available_count": sum(
            record.state is CandidateCExecutionState.AVAILABLE for record in records
        ),
    }


def _inspect_partition(
    transport: DukascopyBi5DirectoryTransport, pair: str, boundary: datetime
) -> CandidateCExecutionEvidence:
    try:
        partition = transport.fetch_hour(
            native_symbol=pair,
            hour_start=boundary,
            timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
    except DukascopyTransportFailure as exc:
        if (
            exc.category is ProviderFailureCategory.NO_DATA
            and exc.reason == "missing_local_bi5_file"
        ):
            return _make_record(
                pair=pair,
                boundary=boundary,
                state=CandidateCExecutionState.MISSING_LOCAL_PARTITION,
                partition_state=Bi5PartitionState.INCOMPLETE.value,
            )
        return _make_record(
            pair=pair,
            boundary=boundary,
            state=CandidateCExecutionState.INVALID_PARTITION,
            partition_state=Bi5PartitionState.CORRUPT_LOCAL.value,
        )
    if partition.is_absent:
        is_empty = partition.absence_evidence_type == "http_200_empty_body"
        return _make_record(
            pair=pair,
            boundary=boundary,
            state=(
                CandidateCExecutionState.EMPTY_EVIDENCED
                if is_empty else CandidateCExecutionState.ABSENT_EVIDENCED
            ),
            partition_state=Bi5PartitionState.ABSENT_EVIDENCED.value,
            absence_evidence_type=partition.absence_evidence_type,
        )
    raw_sha256, raw_byte_count = partition.raw_sha256, len(partition.body)
    try:
        ticks = decode_dukascopy_bi5_hour(partition.body, boundary, pair)
    except DukascopyTransportFailure:
        return _make_record(
            pair=pair,
            boundary=boundary,
            state=CandidateCExecutionState.INVALID_PARTITION,
            partition_state=Bi5PartitionState.CORRUPT_LOCAL.value,
            raw_sha256=raw_sha256,
            raw_byte_count=raw_byte_count,
        )
    if not ticks:
        return _make_record(
            pair=pair,
            boundary=boundary,
            state=CandidateCExecutionState.NO_VALID_TICK,
            partition_state=Bi5PartitionState.PRESENT_STAGED.value,
            raw_sha256=raw_sha256,
            raw_byte_count=raw_byte_count,
        )
    tick = ticks[0]
    offset_ms = int((tick.timestamp - boundary).total_seconds() * 1_000)
    return _make_record(
        pair=pair,
        boundary=boundary,
        state=CandidateCExecutionState.AVAILABLE,
        partition_state=Bi5PartitionState.PRESENT_STAGED.value,
        raw_sha256=raw_sha256,
        raw_byte_count=raw_byte_count,
        selected_tick_offset_ms=offset_ms,
        selected_tick_timestamp=tick.timestamp,
        bid=tick.bid,
        ask=tick.ask,
        bid_volume=tick.bid_volume,
        ask_volume=tick.ask_volume,
    )


def build_candidate_c_execution_evidence_manifest(
    *,
    root: Path | str,
    start: datetime,
    end: datetime,
    pairs: Sequence[str] = CANDIDATE_C_EXECUTION_PAIRS,
) -> CandidateCExecutionEvidenceManifest:
    """Build deterministic evidence from explicit local hourly mirror paths only."""
    start_utc, end_utc, ordered_pairs = _validate_scope(start, end, pairs)
    transport = DukascopyBi5DirectoryTransport(Path(root))
    records: list[CandidateCExecutionEvidence] = []
    for pair in ordered_pairs:
        boundary = start_utc
        while boundary < end_utc:
            records.append(_inspect_partition(transport, pair, boundary))
            boundary += timedelta(days=1)
    ordered_records = tuple(records)
    counts = Counter(record.state.value for record in ordered_records)
    state_counts = tuple(sorted(counts.items()))
    payload = _manifest_payload(
        start_utc, end_utc, ordered_pairs, ordered_records, state_counts
    )
    available_count = sum(
        record.state is CandidateCExecutionState.AVAILABLE
        for record in ordered_records
    )
    return CandidateCExecutionEvidenceManifest(
        schema_version=CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        start=start_utc,
        end=end_utc,
        pairs=ordered_pairs,
        records=ordered_records,
        state_counts=state_counts,
        total_count=len(ordered_records),
        available_count=available_count,
        manifest_id=_sha(payload),
    )


@dataclass(frozen=True)
class CandidateCExecutionCohortAssessment:
    state: CandidateCExecutionCohortState
    entry_boundary: datetime
    exit_boundary: datetime
    missing: tuple[tuple[str, datetime, str], ...]


def assess_candidate_c_execution_cohort(
    manifest: CandidateCExecutionEvidenceManifest,
    *,
    entry_boundary: datetime,
    exit_boundary: datetime,
) -> CandidateCExecutionCohortAssessment:
    """Require AVAILABLE entry and exit evidence for all seven pairs."""
    entry, exit_ = _utc(entry_boundary, "entry_boundary"), _utc(
        exit_boundary, "exit_boundary"
    )
    if entry >= exit_:
        raise ValueError("cohort_boundaries_invalid")
    lookup = {
        (record.pair, record.intended_boundary): record for record in manifest.records
    }
    missing: list[tuple[str, datetime, str]] = []
    for pair in CANDIDATE_C_EXECUTION_PAIRS:
        for boundary in (entry, exit_):
            record = lookup.get((pair, boundary))
            if record is None:
                reason = CandidateCExecutionState.MISSING_LOCAL_PARTITION.value
            elif record.state is CandidateCExecutionState.AVAILABLE:
                continue
            else:
                reason = record.state.value
            missing.append((pair, boundary, reason))
    state = (
        CandidateCExecutionCohortState.EXECUTION_EVIDENCE_INCOMPLETE
        if missing else CandidateCExecutionCohortState.AVAILABLE
    )
    return CandidateCExecutionCohortAssessment(
        state=state,
        entry_boundary=entry,
        exit_boundary=exit_,
        missing=tuple(missing),
    )


@dataclass(frozen=True)
class CandidateCExecutionMirrorReport:
    scheduled_partitions: int
    present_staged: int
    absent_evidenced: int
    incomplete: int
    conflict: int
    corrupt_local: int

    @property
    def ok(self) -> bool:
        return (
            self.scheduled_partitions > 0
            and self.incomplete == self.conflict == self.corrupt_local == 0
            and self.present_staged + self.absent_evidenced
            == self.scheduled_partitions
        )


def mirror_candidate_c_execution_partitions(
    *,
    start: datetime,
    end: datetime,
    destination_root: Path | str,
    pair: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    continue_on_transient: bool = False,
    downloader: Callable[..., Bi5PartitionState] = download_hour,
) -> CandidateCExecutionMirrorReport:
    """Acquire only the deterministic 00h schedule; never select ranked pairs."""
    if not isinstance(continue_on_transient, bool):
        raise ValueError("continue_on_transient must be a boolean")
    pairs = CANDIDATE_C_EXECUTION_PAIRS if pair is None else (pair,)
    start_utc, end_utc, ordered_pairs = _validate_scope(start, end, pairs)
    destination, counts, scheduled, stop = Path(destination_root), Counter(), 0, False
    for requested_pair in ordered_pairs:
        boundary = start_utc
        while boundary < end_utc:
            state = downloader(
                symbol=requested_pair,
                hour=boundary,
                destination_root=destination,
                timeout_seconds=timeout_seconds,
            )
            scheduled += 1
            counts[state] += 1
            if state is Bi5PartitionState.INCOMPLETE:
                if not continue_on_transient:
                    stop = True
                    break
            elif state in (
                Bi5PartitionState.CONFLICT,
                Bi5PartitionState.CORRUPT_LOCAL,
            ):
                stop = True
                break
            boundary += timedelta(days=1)
        if stop:
            break
    return CandidateCExecutionMirrorReport(
        scheduled_partitions=scheduled,
        present_staged=counts[Bi5PartitionState.PRESENT_STAGED],
        absent_evidenced=counts[Bi5PartitionState.ABSENT_EVIDENCED],
        incomplete=counts[Bi5PartitionState.INCOMPLETE],
        conflict=counts[Bi5PartitionState.CONFLICT],
        corrupt_local=counts[Bi5PartitionState.CORRUPT_LOCAL],
    )


__all__ = [
    "CANDIDATE_C_EXECUTION_DECODER_VERSION",
    "CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA",
    "CANDIDATE_C_EXECUTION_PAIRS",
    "CANDIDATE_C_EXECUTION_SCHEMA",
    "CANDIDATE_C_EXECUTION_SOURCE_REFERENCE",
    "CandidateCExecutionCohortAssessment",
    "CandidateCExecutionCohortState",
    "CandidateCExecutionEvidence",
    "CandidateCExecutionEvidenceManifest",
    "CandidateCExecutionMirrorReport",
    "CandidateCExecutionState",
    "assess_candidate_c_execution_cohort",
    "build_candidate_c_execution_evidence_manifest",
    "mirror_candidate_c_execution_partitions",
]
