"""Explicit local providers, registry, and canonical validation gateway."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Protocol

import pandas as pd

from .provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    HistoricalBarsProvider,
    LatestTickProvider,
    ProvenanceQuality,
    ProviderCapability,
    ProviderDescriptor,
    ProviderFailure,
    ProviderFailureCategory,
    ProviderRoute,
    ReplayEventProvider,
    TickQuery,
    TickSnapshot,
    bar_content_hash,
    dataset_identity,
)
from .schema import timeframe_to_timedelta
from .store import load_bars


class ProviderGatewayError(RuntimeError):
    """A provider request failed before canonical data could be exposed."""

    def __init__(self, failure: ProviderFailure):
        super().__init__(f"{failure.category.value}:{failure.reason}")
        self.failure = failure


def _provenance(
    descriptor: ProviderDescriptor,
    query: BarQuery,
    frame: pd.DataFrame,
    *,
    provider_symbol: str,
    quality: ProvenanceQuality,
    volume_semantics: str,
    source_reference: str | None = None,
    retrieved_at: datetime | None = None,
) -> DataProvenance:
    content = bar_content_hash(frame)
    dataset_id = dataset_identity(
        descriptor.provider_id,
        descriptor.implementation_version,
        query.fingerprint,
        content,
    )
    return DataProvenance(
        provider_id=descriptor.provider_id,
        provider_version=descriptor.implementation_version,
        normalization_version=descriptor.normalization_version,
        canonical_symbol=query.instrument.symbol,
        provider_symbol=provider_symbol,
        timeframe=query.timeframe,
        query_start=query.start,
        query_end=query.end,
        query_as_of=query.as_of,
        retrieved_at=retrieved_at or query.as_of,
        actual_first_observation=(frame.index[0].to_pydatetime() if len(frame) else None),
        actual_last_observation=(frame.index[-1].to_pydatetime() if len(frame) else None),
        row_count=len(frame),
        content_hash=content,
        query_fingerprint=query.fingerprint,
        dataset_id=dataset_id,
        source_timezone="UTC",
        volume_semantics=volume_semantics,
        provenance_quality=quality,
        sanitized_source_reference=source_reference,
    )


def _bounded_frame(frame: pd.DataFrame, query: BarQuery) -> pd.DataFrame:
    bars = frame.copy(deep=True)
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("provider bars must use a DatetimeIndex")
    if bars.index.tz is None:
        raise ValueError("provider bars must be timezone-aware")
    bars.index = bars.index.tz_convert("UTC")
    bars.index.name = "ts_open"
    bars = bars[(bars.index >= pd.Timestamp(query.start)) & (bars.index < pd.Timestamp(query.end))]
    bars = bars[bars.index + timeframe_to_timedelta(query.timeframe) <= pd.Timestamp(query.as_of)]
    bars.attrs["symbol"] = query.instrument.symbol
    bars.attrs["timeframe"] = query.timeframe
    return bars


@dataclass(frozen=True)
class SymbolAliasMap:
    """An explicit bijection between canonical and provider-native symbols."""

    canonical_to_native: Mapping[str, str]
    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("mapping identity must be non-empty")
        parsed: dict[str, str] = {}
        for raw_canonical, raw_native in self.canonical_to_native.items():
            canonical = CanonicalInstrument(raw_canonical).symbol
            if not isinstance(raw_native, str) or not (native := raw_native.strip()):
                raise ValueError("provider-native symbols must be non-empty")
            if canonical in parsed or native in parsed.values():
                raise ValueError("symbol mapping must be bijective")
            parsed[canonical] = native
        object.__setattr__(self, "canonical_to_native", MappingProxyType(parsed))
        object.__setattr__(self, "identity", self.identity.strip())

    def to_native(self, instrument: CanonicalInstrument) -> str:
        try:
            return self.canonical_to_native[instrument.symbol]
        except KeyError as exc:
            raise ValueError("canonical symbol has no explicit provider mapping") from exc

    def to_canonical(self, native: str) -> CanonicalInstrument:
        matches = [key for key, value in self.canonical_to_native.items() if value == native]
        if len(matches) != 1:
            raise ValueError("provider symbol has no unambiguous canonical mapping")
        return CanonicalInstrument(matches[0])


class DescribedProvider(Protocol):
    descriptor: ProviderDescriptor


@dataclass
class ProviderRegistry:
    _providers: dict[str, DescribedProvider] = field(default_factory=dict, init=False)
    _frozen: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def register(self, provider: DescribedProvider) -> None:
        descriptor = getattr(provider, "descriptor", None)
        if not isinstance(descriptor, ProviderDescriptor):
            raise ValueError("provider must expose a ProviderDescriptor")
        with self._lock:
            if self._frozen:
                raise RuntimeError("provider registry is frozen")
            if descriptor.provider_id in self._providers:
                raise ValueError("provider_id is already registered")
            self._providers[descriptor.provider_id] = provider

    def freeze(self) -> None:
        with self._lock:
            self._frozen = True

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    def get(self, provider_id: str, capability: ProviderCapability) -> DescribedProvider:
        with self._lock:
            if not self._frozen:
                raise RuntimeError("provider registry must be frozen before requests")
            provider = self._providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        if capability not in provider.descriptor.capabilities:
            raise ValueError("provider does not declare the required capability")
        return provider

    def provider_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._providers)


@dataclass(frozen=True)
class InMemoryBarProvider:
    descriptor: ProviderDescriptor
    bars: Mapping[tuple[str, str], pd.DataFrame] = field(repr=False)
    volume_semantics: str = "provider_reported"

    def __post_init__(self) -> None:
        if ProviderCapability.HISTORICAL_BARS not in self.descriptor.capabilities:
            raise ValueError("descriptor lacks historical-bars capability")
        copied: dict[tuple[str, str], pd.DataFrame] = {}
        for (symbol, timeframe), frame in self.bars.items():
            key = (CanonicalInstrument(symbol).symbol, timeframe)
            copied[key] = frame.copy(deep=True)
        object.__setattr__(self, "bars", MappingProxyType(copied))

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure:
        frame = self.bars.get((query.instrument.symbol, query.timeframe))
        if frame is None:
            return ProviderFailure(
                ProviderFailureCategory.NO_DATA,
                "dataset_not_found",
                self.descriptor.provider_id,
            )
        try:
            bounded = _bounded_frame(frame, query)
            provenance = _provenance(
                self.descriptor,
                query,
                bounded,
                provider_symbol=query.instrument.symbol,
                quality=ProvenanceQuality.SYNTHETIC,
                volume_semantics=self.volume_semantics,
            )
            return BarDataset(query, bounded, provenance)
        except (TypeError, ValueError, OverflowError):
            return ProviderFailure(
                ProviderFailureCategory.INVALID_DATA,
                "canonical_validation_failed",
                self.descriptor.provider_id,
            )


@dataclass(frozen=True)
class LocalParquetProvider:
    descriptor: ProviderDescriptor
    data_dir: Path | str
    stage: str = "processed"

    def __post_init__(self) -> None:
        if ProviderCapability.HISTORICAL_BARS not in self.descriptor.capabilities:
            raise ValueError("descriptor lacks historical-bars capability")
        object.__setattr__(self, "data_dir", Path(self.data_dir))

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure:
        try:
            frame = load_bars(self.data_dir, query.instrument.symbol, query.timeframe, self.stage)
        except FileNotFoundError:
            return ProviderFailure(
                ProviderFailureCategory.NO_DATA,
                "dataset_not_found",
                self.descriptor.provider_id,
            )
        except (OSError, ValueError, TypeError):
            return ProviderFailure(
                ProviderFailureCategory.INVALID_DATA,
                "local_dataset_invalid",
                self.descriptor.provider_id,
            )
        try:
            bounded = _bounded_frame(frame, query)
            volume_semantics = (
                "unavailable_filled_zero"
                if len(bounded) and (bounded["volume"] == 0.0).all()
                else "legacy_unspecified"
            )
            provenance = _provenance(
                self.descriptor,
                query,
                bounded,
                provider_symbol=query.instrument.symbol,
                quality=ProvenanceQuality.LEGACY_UNVERIFIED,
                volume_semantics=volume_semantics,
                source_reference=f"local-parquet/{self.stage}/{query.instrument.symbol}/{query.timeframe}",
            )
            return BarDataset(query, bounded, provenance)
        except (TypeError, ValueError, OverflowError):
            return ProviderFailure(
                ProviderFailureCategory.INVALID_DATA,
                "canonical_validation_failed",
                self.descriptor.provider_id,
            )


@dataclass
class HistoricalReplayProvider:
    """Lock-protected adapter over the current deterministic replay cursor."""

    replay: object
    descriptor: ProviderDescriptor
    dataset_id: str
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        required = {
            ProviderCapability.REPLAY_EVENTS,
            ProviderCapability.POINT_IN_TIME,
            ProviderCapability.DETERMINISTIC_REPLAY,
        }
        if not required.issubset(self.descriptor.capabilities):
            raise ValueError("replay descriptor lacks required capabilities")
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise ValueError("dataset_id must be non-empty")

    def next_event(self, query: TickQuery) -> TickSnapshot | ProviderFailure | None:
        try:
            with self._lock:
                tick = self.replay.next_tick(until=query.as_of)
        except (TypeError, ValueError, OverflowError):
            return ProviderFailure(
                ProviderFailureCategory.INVALID_DATA,
                "replay_event_invalid",
                self.descriptor.provider_id,
            )
        if tick is None:
            return None
        if tick.symbol.strip().upper() != query.instrument.symbol:
            return ProviderFailure(
                ProviderFailureCategory.INVALID_DATA,
                "replay_symbol_mismatch",
                self.descriptor.provider_id,
            )
        try:
            content = _tick_hash(tick.symbol, tick.timestamp, tick.bid, tick.ask, tick.mid)
            provenance = DataProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.implementation_version,
                normalization_version=self.descriptor.normalization_version,
                canonical_symbol=query.instrument.symbol,
                provider_symbol=tick.symbol,
                timeframe=None,
                query_start=None,
                query_end=None,
                query_as_of=query.as_of,
                retrieved_at=query.as_of,
                actual_first_observation=tick.timestamp,
                actual_last_observation=tick.timestamp,
                row_count=1,
                content_hash=content,
                query_fingerprint=query.fingerprint,
                dataset_id=self.dataset_id,
                source_timezone="UTC",
                volume_semantics="not_applicable",
                provenance_quality=ProvenanceQuality.SYNTHETIC,
                sanitized_source_reference="historical-replay",
            )
            return TickSnapshot(
                query, query.instrument, tick.timestamp, tick.bid, tick.ask, tick.mid, provenance
            )
        except (TypeError, ValueError, OverflowError) as exc:
            category = (
                ProviderFailureCategory.STALE_DATA
                if str(exc) == ProviderFailureCategory.STALE_DATA.value
                else ProviderFailureCategory.INVALID_DATA
            )
            return ProviderFailure(
                category,
                "replay_event_invalid",
                self.descriptor.provider_id,
            )


@dataclass(frozen=True)
class ProviderGateway:
    registry: ProviderRegistry

    def fetch_bars(self, route: ProviderRoute, query: BarQuery) -> BarDataset:
        if route.required_capability is not ProviderCapability.HISTORICAL_BARS:
            raise ValueError("route does not select historical bars")
        provider = self.registry.get(route.primary_provider_id, route.required_capability)
        if not isinstance(provider, HistoricalBarsProvider):
            raise ProviderGatewayError(_unsupported(route.primary_provider_id))
        _validate_bar_support(provider.descriptor, query)
        try:
            result = provider.fetch_bars(query)
        except Exception as exc:
            raise ProviderGatewayError(
                ProviderFailure(
                    ProviderFailureCategory.INTERNAL,
                    "provider_call_failed",
                    route.primary_provider_id,
                )
            ) from exc
        if isinstance(result, ProviderFailure):
            raise ProviderGatewayError(result)
        if not isinstance(result, BarDataset):
            raise ProviderGatewayError(_invalid(route.primary_provider_id))
        if result.provenance.normalization_version != route.normalization_version:
            raise ProviderGatewayError(
                ProviderFailure(
                    ProviderFailureCategory.CONFIGURATION,
                    "normalization_version_mismatch",
                    route.primary_provider_id,
                )
            )
        return result

    def latest_tick(self, route: ProviderRoute, query: TickQuery) -> TickSnapshot:
        if route.required_capability is not ProviderCapability.LATEST_TICK:
            raise ValueError("route does not select latest ticks")
        provider = self.registry.get(route.primary_provider_id, route.required_capability)
        if not isinstance(provider, LatestTickProvider):
            raise ProviderGatewayError(_unsupported(route.primary_provider_id))
        try:
            result = provider.latest_tick(query)
        except Exception as exc:
            raise ProviderGatewayError(
                ProviderFailure(
                    ProviderFailureCategory.INTERNAL,
                    "provider_call_failed",
                    route.primary_provider_id,
                )
            ) from exc
        if isinstance(result, ProviderFailure):
            raise ProviderGatewayError(result)
        if not isinstance(result, TickSnapshot):
            raise ProviderGatewayError(_invalid(route.primary_provider_id))
        return result

    def next_replay_event(self, route: ProviderRoute, query: TickQuery) -> TickSnapshot | None:
        if route.required_capability is not ProviderCapability.REPLAY_EVENTS:
            raise ValueError("route does not select replay events")
        provider = self.registry.get(route.primary_provider_id, route.required_capability)
        if not isinstance(provider, ReplayEventProvider):
            raise ProviderGatewayError(_unsupported(route.primary_provider_id))
        try:
            result = provider.next_event(query)
        except Exception as exc:
            raise ProviderGatewayError(
                ProviderFailure(
                    ProviderFailureCategory.INTERNAL,
                    "provider_call_failed",
                    route.primary_provider_id,
                )
            ) from exc
        if isinstance(result, ProviderFailure):
            raise ProviderGatewayError(result)
        if result is not None and not isinstance(result, TickSnapshot):
            raise ProviderGatewayError(_invalid(route.primary_provider_id))
        return result


def _unsupported(provider_id: str) -> ProviderFailure:
    return ProviderFailure(
        ProviderFailureCategory.UNSUPPORTED, "provider_contract_unavailable", provider_id
    )


def _validate_bar_support(descriptor: ProviderDescriptor, query: BarQuery) -> None:
    if (
        descriptor.supported_symbols is not None
        and query.instrument not in descriptor.supported_symbols
    ):
        raise ProviderGatewayError(
            ProviderFailure(
                ProviderFailureCategory.UNSUPPORTED,
                "symbol_unsupported",
                descriptor.provider_id,
            )
        )
    if (
        descriptor.supported_timeframes is not None
        and query.timeframe not in descriptor.supported_timeframes
    ):
        raise ProviderGatewayError(
            ProviderFailure(
                ProviderFailureCategory.UNSUPPORTED,
                "timeframe_unsupported",
                descriptor.provider_id,
            )
        )
    if descriptor.coverage_start is not None and query.start < descriptor.coverage_start:
        raise ProviderGatewayError(
            ProviderFailure(
                ProviderFailureCategory.NO_DATA,
                "query_before_coverage",
                descriptor.provider_id,
            )
        )
    if descriptor.coverage_end is not None and query.end > descriptor.coverage_end:
        raise ProviderGatewayError(
            ProviderFailure(
                ProviderFailureCategory.NO_DATA,
                "query_after_coverage",
                descriptor.provider_id,
            )
        )


def _invalid(provider_id: str) -> ProviderFailure:
    return ProviderFailure(
        ProviderFailureCategory.INVALID_DATA, "provider_result_invalid", provider_id
    )


def _tick_hash(symbol: str, timestamp: datetime, bid: float, ask: float, mid: float) -> str:
    import hashlib
    import json

    document = [symbol, timestamp.astimezone(UTC).isoformat(), bid, ask, mid]
    return hashlib.sha256(
        json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
