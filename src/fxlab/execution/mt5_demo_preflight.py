"""Read-only preflight for an already-authenticated local MT5 demo terminal."""

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import ModuleType

MT5_PREFLIGHT_SYMBOLS = frozenset(
    {"AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"}
)
_SAFE_BROKER_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,127}$")


def _load_mt5() -> ModuleType:
    try:
        return importlib.import_module("MetaTrader5")
    except (ImportError, OSError):
        raise RuntimeError("mt5_package_unavailable") from None


@dataclass(frozen=True, slots=True)
class Mt5Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    symbol_trade_mode: int


@dataclass(frozen=True, slots=True)
class Mt5PreflightResult:
    environment: str
    broker: str
    terminal_version: str
    account: str
    server: str
    company: str
    currency: str
    hedging_enabled: bool
    account_trading_enabled: bool
    expert_trading_enabled: bool
    terminal_trading_enabled: bool
    quote: Mt5Quote | None = None


@dataclass(slots=True)
class Mt5DemoPreflight:
    """Inspect local terminal/account state without exposing an order API."""

    api: object = field(default_factory=_load_mt5, repr=False)

    def run(self, *, quote: str | None = None) -> Mt5PreflightResult:
        selected = _selected_symbol(quote)
        initialized = False
        try:
            initialize = getattr(self.api, "initialize", None)
            if not callable(initialize) or initialize() is not True:
                raise RuntimeError("mt5_initialize_failed")
            initialized = True
            terminal = _required_call(self.api, "terminal_info", "mt5_terminal_unavailable")
            if getattr(terminal, "connected", None) is not True:
                raise RuntimeError("mt5_terminal_unavailable")
            account = _required_call(self.api, "account_info", "mt5_account_unavailable")
            demo_mode = getattr(self.api, "ACCOUNT_TRADE_MODE_DEMO", None)
            if demo_mode is None or getattr(account, "trade_mode", None) != demo_mode:
                raise RuntimeError("mt5_demo_account_required")
            result = _validated_result(self.api, terminal, account)
            return replace(result, quote=_read_quote(self.api, selected)) if selected else result
        finally:
            shutdown = getattr(self.api, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    if initialized:
                        raise RuntimeError("mt5_shutdown_failed") from None


def _selected_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    result = value.strip().upper()
    if result not in MT5_PREFLIGHT_SYMBOLS:
        raise ValueError("unsupported_mt5_symbol")
    return result


def _required_call(api: object, name: str, reason: str) -> object:
    method = getattr(api, name, None)
    if not callable(method):
        raise RuntimeError(reason)
    try:
        result = method()
    except Exception:
        raise RuntimeError(reason) from None
    if result is None:
        raise RuntimeError(reason)
    return result


def _validated_result(api: object, terminal: object, account: object) -> Mt5PreflightResult:
    login = getattr(account, "login", None)
    currency = getattr(account, "currency", None)
    server = _safe_broker_text(getattr(account, "server", None))
    company = _safe_broker_text(getattr(account, "company", None))
    account_trading = getattr(account, "trade_allowed", None)
    expert_trading = getattr(account, "trade_expert", None)
    terminal_trading = getattr(terminal, "trade_allowed", None)
    hedging_mode = getattr(api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
    if (
        isinstance(login, bool)
        or not isinstance(login, int)
        or login <= 0
        or currency != "USD"
        or server is None
        or company is None
        or hedging_mode is None
        or getattr(account, "margin_mode", None) != hedging_mode
        or account_trading is not True
        or expert_trading is not True
        or not isinstance(terminal_trading, bool)
    ):
        raise RuntimeError("mt5_account_incompatible")
    version = _required_call(api, "version", "mt5_terminal_unavailable")
    if (
        not isinstance(version, tuple)
        or len(version) < 2
        or isinstance(version[0], bool)
        or isinstance(version[1], bool)
        or not isinstance(version[0], int)
        or not isinstance(version[1], int)
        or version[0] <= 0
        or version[1] <= 0
    ):
        raise RuntimeError("mt5_terminal_unavailable")
    return Mt5PreflightResult(
        "demo",
        "MetaTrader5",
        f"{version[0]}.{version[1]}",
        f"****{str(login)[-4:]}",
        server,
        company,
        currency,
        True,
        account_trading,
        expert_trading,
        terminal_trading,
    )


def _safe_broker_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result if _SAFE_BROKER_TEXT.fullmatch(result) else None


def _read_quote(api: object, symbol: str) -> Mt5Quote:
    symbol_info = getattr(api, "symbol_info", None)
    tick_info = getattr(api, "symbol_info_tick", None)
    if not callable(symbol_info) or not callable(tick_info):
        raise RuntimeError("mt5_quote_unavailable")
    try:
        metadata = symbol_info(symbol)
    except Exception:
        raise RuntimeError("mt5_symbol_unavailable") from None
    if metadata is None or getattr(metadata, "name", None) != symbol:
        raise RuntimeError("mt5_symbol_unavailable")
    try:
        tick = tick_info(symbol)
    except Exception:
        raise RuntimeError("mt5_quote_unavailable") from None
    timestamp_msc = getattr(tick, "time_msc", None)
    bid = _positive_float(getattr(tick, "bid", None))
    ask = _positive_float(getattr(tick, "ask", None))
    trade_mode = getattr(metadata, "trade_mode", None)
    if (
        isinstance(timestamp_msc, bool)
        or not isinstance(timestamp_msc, int)
        or timestamp_msc <= 0
        or bid is None
        or ask is None
        or ask < bid
        or isinstance(trade_mode, bool)
        or not isinstance(trade_mode, int)
    ):
        raise RuntimeError("mt5_quote_invalid")
    try:
        timestamp = datetime.fromtimestamp(timestamp_msc / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise RuntimeError("mt5_quote_invalid") from None
    return Mt5Quote(symbol, timestamp, bid, ask, trade_mode)


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None
