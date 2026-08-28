"""Constrained OANDA v20 fxTrade Practice broker adapter."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from .broker import AccountInfo, BrokerOrderRejected, OrderRequest, OrderStatus, Position, Tick
from .broker_capabilities import (
    BrokerCapability,
    BrokerDescriptor,
    BrokerEnvironment,
)

OANDA_PRACTICE_AUTHORITY = "https://api-fxpractice.oanda.com"
OANDA_SYMBOLS: Mapping[str, str] = MappingProxyType(
    {
        "EURUSD": "EUR_USD",
        "GBPUSD": "GBP_USD",
        "AUDUSD": "AUD_USD",
        "NZDUSD": "NZD_USD",
    }
)
_OANDA_CANONICAL_BY_NATIVE = MappingProxyType(
    {native: canonical for canonical, native in OANDA_SYMBOLS.items()}
)
_CONTRACT_UNITS = Decimal("100000")
_CLIENT_ID_PATTERN = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_RESPONSE_BYTES = 1_048_576

_OANDA_DESCRIPTOR = BrokerDescriptor(
    broker_id="oanda-v20",
    implementation_version="1",
    environment=BrokerEnvironment.DEMO,
    capabilities=frozenset(
        {
            BrokerCapability.MARKET_ORDERS,
            BrokerCapability.NATIVE_SL_TP,
            BrokerCapability.HEDGING,
            BrokerCapability.CLIENT_ORDER_IDS,
        }
    ),
    deterministic=False,
)


class OandaTransport(Protocol):
    authority: str

    def request(self, method: str, path: str, **kwargs: object) -> OandaResponse: ...


@dataclass(slots=True)
class OandaHttpTransport:
    """One-attempt bounded JSON transport hard-wired to OANDA practice."""

    token: str = field(repr=False)
    authority: str = field(default=OANDA_PRACTICE_AUTHORITY, init=False)
    opener: Callable[..., object] = field(default=urlopen, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("practice bearer token is required")

    def request(self, method: str, path: str, **kwargs: object) -> OandaResponse:
        timeout = kwargs.get("timeout_seconds")
        max_bytes = kwargs.get("max_response_bytes", _MAX_RESPONSE_BYTES)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        query = kwargs.get("query")
        body = kwargs.get("json_body")
        url = self.authority + path
        if query:
            from urllib.parse import urlencode

            if not isinstance(query, Mapping):
                raise ValueError("query must be a mapping")
            url += "?" + urlencode({str(key): str(value) for key, value in query.items()})
        encoded = None
        if body is not None:
            if not isinstance(body, Mapping):
                raise ValueError("json body must be a mapping")
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            response = self.opener(request, timeout=float(timeout))
            status = int(response.status)
            raw = response.read(max_bytes + 1)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(max_bytes + 1)
        except (TimeoutError, URLError, OSError):
            raise RuntimeError("oanda_network_unavailable") from None
        if len(raw) > max_bytes:
            raise RuntimeError("oanda_response_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("oanda_response_invalid") from None
        if not isinstance(payload, Mapping):
            raise RuntimeError("oanda_response_invalid")
        return OandaResponse(status, payload)


@dataclass(frozen=True, slots=True)
class OandaResponse:
    status: int
    payload: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise ValueError("response status must be an integer")
        if not isinstance(self.payload, Mapping):
            raise ValueError("response payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class _ExpectedOrder:
    client_id: str
    native_symbol: str
    signed_units: Decimal
    sl_price: Decimal
    tp_price: Decimal | None


@dataclass(slots=True)
class OandaDemoBroker:
    """OANDA practice-only adapter; no live endpoint can be configured."""

    _account_id: str = field(repr=False)
    _token: str = field(repr=False)
    timeout_seconds: float = 10.0
    max_quote_age: timedelta = timedelta(seconds=5)
    transport: OandaTransport | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _instrument_meta: dict[str, Mapping[str, object]] = field(
        default_factory=dict, init=False, repr=False
    )
    _subscriptions: set[str] = field(default_factory=set, init=False, repr=False)
    _latest_ticks: dict[str, Tick] = field(default_factory=dict, init=False, repr=False)
    _orders: dict[str, _ExpectedOrder] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._account_id, str) or not self._account_id.strip():
            raise ValueError("practice account ID is required")
        if not isinstance(self._token, str) or not self._token.strip():
            raise ValueError("practice bearer token is required")
        if not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be positive")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        if self.transport is not None and self.transport.authority != OANDA_PRACTICE_AUTHORITY:
            raise ValueError("OANDA demo transport authority is not permitted")
        self._account_id = self._account_id.strip()
        if self.transport is None:
            self.transport = OandaHttpTransport(self._token)

    @property
    def broker_descriptor(self) -> BrokerDescriptor:
        return _OANDA_DESCRIPTOR

    def connect(self) -> None:
        if self.transport is None:
            raise RuntimeError("oanda_transport_unavailable")
        with self._lock:
            self._connected = False
            self._subscriptions.clear()
        account_response = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}",
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        _validated_account(account_response, self._account_id)
        instruments_response = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}/instruments",
            query={"instruments": ",".join(OANDA_SYMBOLS.values())},
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        metadata = _validated_instruments(instruments_response)
        with self._lock:
            self._instrument_meta = metadata
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._subscriptions.clear()

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def subscribe_market_data(self, symbols: list[str]) -> None:
        normalized: set[str] = set()
        for item in symbols:
            canonical = _canonical_symbol(item)
            if canonical is None:
                raise ValueError("unsupported_oanda_symbol")
            normalized.add(canonical)
        if not normalized:
            raise ValueError("unsupported_oanda_symbol")
        with self._lock:
            if not self._connected:
                raise RuntimeError("oanda_not_connected")
            self._subscriptions.update(normalized)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        canonical = _canonical_symbol(symbol)
        if canonical is None:
            raise ValueError("unsupported_oanda_symbol")
        with self._lock:
            if not self._connected:
                raise RuntimeError("oanda_not_connected")
            if canonical not in self._subscriptions:
                raise RuntimeError("oanda_symbol_not_subscribed")
        assert self.transport is not None
        response = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}/pricing",
            query={"instruments": OANDA_SYMBOLS[canonical]},
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        tick = _validated_tick(
            response,
            canonical,
            now=self.clock(),
            max_age=self.max_quote_age,
        )
        with self._lock:
            self._latest_ticks[canonical] = tick
        return tick

    def get_account_info(self) -> AccountInfo:
        self._require_connected()
        assert self.transport is not None
        response = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}",
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        account = _validated_account(response, self._account_id)
        return _account_info(account)

    def submit_order(self, order: OrderRequest) -> str:
        self._require_connected()
        expected, body = self._submission(order)
        assert self.transport is not None
        response = self.transport.request(
            "POST",
            f"/v3/accounts/{self._account_id}/orders",
            json_body=body,
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        rejected = _proven_rejection(response)
        if rejected is not None:
            raise rejected
        broker_id = _submitted_order_id(response, expected)
        with self._lock:
            self._orders[broker_id] = expected
        return broker_id

    def get_order_status(self, order_id: str) -> dict:
        self._require_connected()
        with self._lock:
            expected = self._orders.get(order_id)
        if expected is None:
            raise RuntimeError("oanda_order_identity_unknown")
        assert self.transport is not None
        response = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}/orders/{order_id}",
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        order = _validated_order_status(response, order_id, expected)
        state = order["state"]
        if state == "FILLED":
            fill_id = order.get("fillingTransactionID")
            if not isinstance(fill_id, str) or not fill_id:
                raise RuntimeError("oanda_fill_incomplete")
            fill_response = self.transport.request(
                "GET",
                f"/v3/accounts/{self._account_id}/transactions/{fill_id}",
                timeout_seconds=float(self.timeout_seconds),
                max_response_bytes=_MAX_RESPONSE_BYTES,
            )
            trade_id = _validated_fill_transaction(fill_response, fill_id, order_id, expected)
            trade_response = self.transport.request(
                "GET",
                f"/v3/accounts/{self._account_id}/trades/{trade_id}",
                timeout_seconds=float(self.timeout_seconds),
                max_response_bytes=_MAX_RESPONSE_BYTES,
            )
            _validated_trade_reflection(trade_response, trade_id, expected)
        mapped = {
            "PENDING": OrderStatus.PENDING,
            "FILLED": OrderStatus.FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "CANCELLED": OrderStatus.CANCELLED,
        }.get(state)
        if mapped is None:
            raise RuntimeError("oanda_order_state_unsupported")
        return {"status": mapped.value}

    def cancel_order(self, order_id: str) -> bool:
        raise RuntimeError("oanda_order_cancellation_unsupported")

    def close_position(self, position_id: str) -> str | None:
        self._require_connected()
        if not _CLIENT_ID_PATTERN.fullmatch(position_id):
            raise ValueError("invalid_oanda_trade_id")
        assert self.transport is not None
        lookup = self.transport.request(
            "GET",
            f"/v3/accounts/{self._account_id}/trades/{position_id}",
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        if lookup.status == 404 and lookup.payload.get("errorCode") in {
            "NO_SUCH_TRADE",
            "TRADE_DOESNT_EXIST",
        }:
            return None
        trade = lookup.payload.get("trade") if lookup.status == 200 else None
        if (
            isinstance(trade, Mapping)
            and trade.get("id") == position_id
            and trade.get("state") == "CLOSED"
        ):
            return None
        units = _decimal(trade.get("currentUnits")) if isinstance(trade, Mapping) else None
        native = trade.get("instrument") if isinstance(trade, Mapping) else None
        if (
            not isinstance(trade, Mapping)
            or trade.get("id") != position_id
            or trade.get("state") != "OPEN"
            or native not in _OANDA_CANONICAL_BY_NATIVE
            or units is None
            or not units.is_finite()
            or units == 0
        ):
            raise RuntimeError("oanda_close_trade_invalid")
        response = self.transport.request(
            "PUT",
            f"/v3/accounts/{self._account_id}/trades/{position_id}/close",
            json_body={"units": "ALL"},
            timeout_seconds=float(self.timeout_seconds),
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        if response.status < 200 or response.status >= 300:
            raise RuntimeError("oanda_close_uncertain")
        fill = response.payload.get("orderFillTransaction")
        if not isinstance(fill, Mapping):
            raise RuntimeError("oanda_close_uncertain")
        close_id = fill.get("id")
        closed = fill.get("tradesClosed")
        if (
            not isinstance(close_id, str)
            or not close_id
            or not isinstance(closed, list)
            or len(closed) != 1
            or not isinstance(closed[0], Mapping)
            or closed[0].get("tradeID") != position_id
            or (closed_units := _decimal(closed[0].get("units"))) is None
            or abs(closed_units) != abs(units)
        ):
            raise RuntimeError("oanda_close_uncertain")
        return close_id

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        raise RuntimeError("oanda_historical_bars_unsupported")

    def _require_connected(self) -> None:
        with self._lock:
            if not self._connected:
                raise RuntimeError("oanda_not_connected")

    def _submission(self, order: OrderRequest) -> tuple[_ExpectedOrder, dict[str, object]]:
        canonical = _canonical_symbol(order.symbol)
        if canonical is None:
            raise ValueError("unsupported_oanda_symbol")
        if order.order_type != "market":
            raise ValueError("unsupported_oanda_order_type")
        if not isinstance(order.order_id, str) or not _CLIENT_ID_PATTERN.fullmatch(order.order_id):
            raise ValueError("invalid_oanda_client_order_id")
        if order.sl_price is None:
            raise ValueError("oanda_stop_loss_required")
        meta = self._instrument_meta[OANDA_SYMBOLS[canonical]]
        units = _lots_to_units(order.size, order.side, meta)
        sl_price = _exact_price(order.sl_price, meta)
        tp_price = _exact_price(order.tp_price, meta) if order.tp_price is not None else None
        reference = _exact_price(order.price, meta) if order.price is not None else None
        if reference is None:
            with self._lock:
                latest = self._latest_ticks.get(canonical)
            if latest is not None:
                now = _aware_utc(self.clock())
                if (
                    now is not None
                    and latest.timestamp <= now
                    and now - latest.timestamp <= self.max_quote_age
                ):
                    reference = _exact_price(
                        latest.ask if order.side == 1 else latest.bid, meta
                    )
        if reference is None:
            raise ValueError("oanda_entry_reference_required")
        if reference is not None:
            if order.side == 1 and not (
                sl_price < reference and (tp_price is None or tp_price > reference)
            ):
                raise ValueError("invalid_oanda_protection_direction")
            if order.side == -1 and not (
                sl_price > reference and (tp_price is None or tp_price < reference)
            ):
                raise ValueError("invalid_oanda_protection_direction")
        elif tp_price is not None and (
            (order.side == 1 and sl_price >= tp_price)
            or (order.side == -1 and sl_price <= tp_price)
        ):
            raise ValueError("invalid_oanda_protection_direction")
        native = OANDA_SYMBOLS[canonical]
        expected = _ExpectedOrder(order.order_id, native, units, sl_price, tp_price)
        payload: dict[str, object] = {
            "type": "MARKET",
            "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY",
            "instrument": native,
            "units": str(units),
            "clientExtensions": {"id": order.order_id},
            "tradeClientExtensions": {"id": order.order_id},
            "stopLossOnFill": {"price": format(sl_price, "f"), "timeInForce": "GTC"},
        }
        if tp_price is not None:
            payload["takeProfitOnFill"] = {"price": format(tp_price, "f"), "timeInForce": "GTC"}
        return expected, {"order": payload}


def _validated_account(response: object, account_id: str) -> Mapping[str, object]:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_account_unavailable")
    account = response.payload.get("account")
    if not isinstance(account, Mapping):
        raise RuntimeError("oanda_account_invalid")
    if (
        account.get("id") != account_id
        or account.get("currency") != "USD"
        or account.get("hedgingEnabled") is not True
        or account.get("tradingDisabled") is not False
        or "mt4AccountID" in account
    ):
        raise RuntimeError("oanda_account_incompatible")
    return account


def _validated_instruments(response: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_instruments_unavailable")
    raw = response.payload.get("instruments")
    if not isinstance(raw, list):
        raise RuntimeError("oanda_instruments_invalid")
    metadata: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise RuntimeError("oanda_instruments_invalid")
        if item["name"] in metadata:
            raise RuntimeError("oanda_instruments_invalid")
        minimum = _decimal(item.get("minimumTradeSize"))
        maximum = _decimal(item.get("maximumOrderUnits"))
        if (
            item.get("type") != "CURRENCY"
            or item.get("pipLocation") != -4
            or item.get("tradeUnitsPrecision") != 0
            or item.get("displayPrecision") != 5
            or minimum is None
            or maximum is None
            or not minimum.is_finite()
            or not maximum.is_finite()
            or minimum <= 0
            or maximum < minimum
        ):
            raise RuntimeError("oanda_instruments_incompatible")
        metadata[item["name"]] = MappingProxyType(dict(item))
    if set(metadata) != set(OANDA_SYMBOLS.values()):
        raise RuntimeError("oanda_instruments_incomplete")
    return metadata


def _canonical_symbol(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if canonical in OANDA_SYMBOLS else None


def _validated_tick(
    response: object,
    canonical: str,
    *,
    now: datetime,
    max_age: timedelta,
) -> Tick:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_quote_unavailable")
    prices = response.payload.get("prices")
    if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], Mapping):
        raise RuntimeError("oanda_quote_invalid")
    price = prices[0]
    if price.get("instrument") != OANDA_SYMBOLS[canonical] or price.get("tradeable") is not True:
        raise RuntimeError("oanda_quote_invalid")
    timestamp = _utc_datetime(price.get("time"))
    now_utc = _aware_utc(now)
    bid = _first_price(price.get("bids"))
    ask = _first_price(price.get("asks"))
    if (
        timestamp is None
        or now_utc is None
        or timestamp > now_utc
        or now_utc - timestamp > max_age
        or bid is None
        or ask is None
        or ask < bid
    ):
        raise RuntimeError("oanda_quote_invalid")
    return Tick(canonical, timestamp, bid, ask, (bid + ask) / 2.0)


def _first_price(value: object) -> float | None:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        return None
    return _positive_float(value[0].get("price"))


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None


def _account_info(account: Mapping[str, object]) -> AccountInfo:
    balance = _finite_float(account.get("balance"))
    equity = _positive_float(account.get("NAV"))
    margin_used = _nonnegative_float(account.get("marginUsed"))
    margin_available = _nonnegative_float(account.get("marginAvailable"))
    trades = account.get("trades")
    count = account.get("openTradeCount")
    if (
        balance is None
        or equity is None
        or margin_used is None
        or margin_available is None
        or not isinstance(trades, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(trades)
    ):
        raise RuntimeError("oanda_account_invalid")
    positions = tuple(_position_from_trade(item) for item in trades)
    return AccountInfo(balance, equity, margin_used, margin_available, list(positions))


def _position_from_trade(raw: object) -> Position:
    if not isinstance(raw, Mapping):
        raise RuntimeError("oanda_trade_invalid")
    position_id = raw.get("id")
    native_symbol = raw.get("instrument")
    timestamp = _utc_datetime(raw.get("openTime"))
    entry = _positive_float(raw.get("price"))
    unrealized = _finite_float(raw.get("unrealizedPL"))
    units = _decimal(raw.get("currentUnits"))
    canonical = (
        _OANDA_CANONICAL_BY_NATIVE.get(native_symbol) if isinstance(native_symbol, str) else None
    )
    if (
        not isinstance(position_id, str)
        or not position_id.strip()
        or canonical is None
        or timestamp is None
        or entry is None
        or unrealized is None
        or units is None
        or not units.is_finite()
        or units == 0
        or raw.get("state") != "OPEN"
    ):
        raise RuntimeError("oanda_trade_invalid")
    return Position(
        canonical,
        1 if units > 0 else -1,
        float(abs(units) / _CONTRACT_UNITS),
        entry,
        timestamp,
        unrealized,
        position_id.strip(),
    )


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative_float(value: object) -> float | None:
    result = _finite_float(value)
    return result if result is not None and result >= 0 else None


def _lots_to_units(size: object, side: object, meta: Mapping[str, object]) -> Decimal:
    lots = _decimal(size)
    precision = meta.get("tradeUnitsPrecision")
    minimum = _decimal(meta.get("minimumTradeSize"))
    maximum = _decimal(meta.get("maximumOrderUnits"))
    if (
        lots is None
        or not lots.is_finite()
        or lots <= 0
        or side not in (1, -1)
        or isinstance(precision, bool)
        or not isinstance(precision, int)
        or precision < 0
        or minimum is None
        or maximum is None
    ):
        raise ValueError("invalid_oanda_order_size")
    absolute = lots * _CONTRACT_UNITS
    quantum = Decimal(1).scaleb(-precision)
    if absolute != absolute.quantize(quantum) or absolute < minimum or absolute > maximum:
        raise ValueError("invalid_oanda_order_size")
    return absolute if side == 1 else -absolute


def _exact_price(value: object, meta: Mapping[str, object]) -> Decimal:
    price = _decimal(value)
    precision = meta.get("displayPrecision")
    if (
        price is None
        or not price.is_finite()
        or price <= 0
        or isinstance(precision, bool)
        or not isinstance(precision, int)
        or precision < 0
    ):
        raise ValueError("invalid_oanda_price")
    quantum = Decimal(1).scaleb(-precision)
    if price != price.quantize(quantum):
        raise ValueError("invalid_oanda_price_precision")
    return price


def _proven_rejection(response: object) -> BrokerOrderRejected | None:
    if not isinstance(response, OandaResponse):
        raise RuntimeError("oanda_submission_uncertain")
    if response.status != 400:
        return None
    rejected = response.payload.get("orderRejectTransaction")
    if not isinstance(rejected, Mapping):
        return None
    reason = rejected.get("rejectReason")
    transaction_id = rejected.get("id")
    if reason == "CLIENT_ORDER_ID_ALREADY_EXISTS":
        return None
    if not isinstance(reason, str) or not reason:
        raise RuntimeError("oanda_submission_uncertain")
    try:
        return BrokerOrderRejected(
            "oanda_order_rejected",
            rejection_transaction_id=str(transaction_id) if transaction_id is not None else None,
        )
    except ValueError:
        raise RuntimeError("oanda_submission_uncertain") from None


def _submitted_order_id(response: object, expected: _ExpectedOrder) -> str:
    if not isinstance(response, OandaResponse) or not 200 <= response.status < 300:
        raise RuntimeError("oanda_submission_uncertain")
    created = response.payload.get("orderCreateTransaction")
    filled = response.payload.get("orderFillTransaction")
    source = created if isinstance(created, Mapping) else filled
    if not isinstance(source, Mapping):
        raise RuntimeError("oanda_submission_uncertain")
    order_id = source.get("id") if source is created else source.get("orderID")
    if (
        not isinstance(order_id, str)
        or not _CLIENT_ID_PATTERN.fullmatch(order_id)
        or source.get("instrument") != expected.native_symbol
        or _decimal(source.get("units")) != expected.signed_units
        or _nested_client_id(source) != expected.client_id
    ):
        raise RuntimeError("oanda_submission_uncertain")
    return order_id


def _validated_order_status(
    response: object, order_id: str, expected: _ExpectedOrder
) -> Mapping[str, object]:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_order_status_unavailable")
    order = response.payload.get("order")
    if (
        not isinstance(order, Mapping)
        or order.get("id") != order_id
        or _nested_client_id(order) != expected.client_id
        or order.get("instrument") != expected.native_symbol
        or _decimal(order.get("units")) != expected.signed_units
    ):
        raise RuntimeError("oanda_order_identity_mismatch")
    state = order.get("state")
    if state not in {"PENDING", "FILLED", "REJECTED", "CANCELLED"}:
        raise RuntimeError("oanda_order_state_unsupported")
    return order


def _validated_fill_transaction(
    response: object,
    fill_id: str,
    order_id: str,
    expected: _ExpectedOrder,
) -> str:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_fill_evidence_unavailable")
    transaction = response.payload.get("transaction")
    trade_opened = transaction.get("tradeOpened") if isinstance(transaction, Mapping) else None
    client_id = transaction.get("clientOrderID") if isinstance(transaction, Mapping) else None
    if client_id is None and isinstance(transaction, Mapping):
        client_id = _nested_client_id(transaction)
    if (
        not isinstance(transaction, Mapping)
        or transaction.get("id") != fill_id
        or transaction.get("orderID") != order_id
        or transaction.get("instrument") != expected.native_symbol
        or _decimal(transaction.get("units")) != expected.signed_units
        or client_id != expected.client_id
        or transaction.get("tradeReduced") is not None
        or bool(transaction.get("tradesClosed"))
        or transaction.get("partialFill") is not None
        or transaction.get("residualUnits") not in (None, "0", "0.0")
        or not isinstance(trade_opened, Mapping)
        or _decimal(trade_opened.get("units")) != expected.signed_units
    ):
        raise RuntimeError("oanda_partial_fill_unsupported")
    trade_id = trade_opened.get("tradeID")
    if not isinstance(trade_id, str) or not trade_id:
        raise RuntimeError("oanda_fill_incomplete")
    return trade_id


def _validated_trade_reflection(response: object, trade_id: str, expected: _ExpectedOrder) -> None:
    if not isinstance(response, OandaResponse) or response.status != 200:
        raise RuntimeError("oanda_trade_reflection_unavailable")
    trade = response.payload.get("trade")
    if (
        not isinstance(trade, Mapping)
        or trade.get("id") != trade_id
        or trade.get("state") != "OPEN"
        or trade.get("instrument") != expected.native_symbol
        or _decimal(trade.get("initialUnits")) != expected.signed_units
        or _decimal(trade.get("currentUnits")) != expected.signed_units
        or _nested_client_id(trade) != expected.client_id
    ):
        raise RuntimeError("oanda_trade_reflection_invalid")
    _validate_dependent_order(trade.get("stopLossOrder"), expected.sl_price, trade_id)
    tp = trade.get("takeProfitOrder")
    if expected.tp_price is None:
        if tp is not None:
            raise RuntimeError("oanda_trade_reflection_invalid")
    else:
        _validate_dependent_order(tp, expected.tp_price, trade_id)


def _validate_dependent_order(raw: object, price: Decimal, trade_id: str) -> None:
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("id"), str)
        or not raw.get("id")
        or raw.get("state") != "PENDING"
        or raw.get("tradeID") != trade_id
        or _decimal(raw.get("price")) != price
    ):
        raise RuntimeError("oanda_trade_reflection_invalid")


def _nested_client_id(raw: Mapping[str, object]) -> str | None:
    extensions = raw.get("clientExtensions")
    if not isinstance(extensions, Mapping):
        extensions = raw.get("tradeClientExtensions")
    value = extensions.get("id") if isinstance(extensions, Mapping) else None
    return value if isinstance(value, str) else None
