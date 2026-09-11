"""Offline tests for the read-only local MT5 demo preflight boundary."""

from __future__ import annotations

import importlib
import math
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import fxlab.cli as cli_module
from fxlab.cli import app

runner = CliRunner()


class FakeMt5Api:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

    def __init__(self) -> None:
        self.initialize_result = True
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True)
        self.account = SimpleNamespace(
            login=12345678,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            currency="USD",
            server="Pepperstone-Demo",
            company="Pepperstone Group Limited",
            trade_allowed=True,
            trade_expert=True,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        )
        self.symbol = SimpleNamespace(name="EURUSD", visible=True, trade_mode=4)
        self.tick = SimpleNamespace(
            time=1_725_192_000,
            time_msc=1_725_192_000_250,
            bid=1.1042,
            ask=1.1044,
        )
        self.calls: list[object] = []

    def initialize(self) -> bool:
        self.calls.append("initialize")
        return self.initialize_result

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def terminal_info(self) -> object:
        self.calls.append("terminal_info")
        return self.terminal

    def account_info(self) -> object:
        self.calls.append("account_info")
        return self.account

    def version(self) -> tuple[int, int, str]:
        self.calls.append("version")
        return (500, 4410, "1 Sep 2024")

    def symbol_info(self, symbol: str) -> object:
        self.calls.append(("symbol_info", symbol))
        return self.symbol

    def symbol_info_tick(self, symbol: str) -> object:
        self.calls.append(("symbol_info_tick", symbol))
        return self.tick

    def order_send(self, request: object) -> object:
        raise AssertionError("read-only preflight must never call order_send")


def _module():
    return importlib.import_module("fxlab.execution.mt5_demo_preflight")


def test_demo_account_is_proven_by_exact_mt5_trade_mode_and_shutdown() -> None:
    api = FakeMt5Api()
    result = _module().Mt5DemoPreflight(api=api).run()

    assert result.environment == "demo"
    assert result.account == "****5678"
    assert result.currency == "USD"
    assert result.server == "Pepperstone-Demo"
    assert result.hedging_enabled is True
    assert result.account_trading_enabled is True
    assert result.expert_trading_enabled is True
    assert result.terminal_trading_enabled is True
    assert api.calls == ["initialize", "terminal_info", "account_info", "version", "shutdown"]
    assert "12345678" not in repr(result)
    assert not any(call == "order_send" for call in api.calls)


@pytest.mark.parametrize(
    "trade_mode",
    [
        FakeMt5Api.ACCOUNT_TRADE_MODE_REAL,
        FakeMt5Api.ACCOUNT_TRADE_MODE_CONTEST,
        999,
        None,
    ],
)
def test_live_contest_and_unknown_account_modes_fail_closed(trade_mode: object) -> None:
    api = FakeMt5Api()
    api.account.trade_mode = trade_mode

    with pytest.raises(RuntimeError, match="mt5_demo_account_required"):
        _module().Mt5DemoPreflight(api=api).run()

    assert api.calls[-1] == "shutdown"
    assert "order_send" not in api.calls


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("initialize", "mt5_initialize_failed"),
        ("terminal", "mt5_terminal_unavailable"),
        ("account", "mt5_account_unavailable"),
        ("currency", "mt5_account_incompatible"),
    ],
)
def test_connectivity_or_account_failure_is_sanitized_and_shutdown(
    change: str, reason: str
) -> None:
    api = FakeMt5Api()
    if change == "initialize":
        api.initialize_result = False
    elif change == "terminal":
        api.terminal = None
    elif change == "account":
        api.account = None
    else:
        api.account.currency = "EUR"

    with pytest.raises(RuntimeError, match=reason) as caught:
        _module().Mt5DemoPreflight(api=api).run()

    assert api.calls[-1] == "shutdown"
    assert "12345678" not in str(caught.value)


def test_preflight_without_quote_performs_no_symbol_or_tick_read() -> None:
    api = FakeMt5Api()
    _module().Mt5DemoPreflight(api=api).run()
    assert not any(isinstance(call, tuple) for call in api.calls)


def test_demo_preflight_reports_terminal_trading_disabled_without_rejecting_read_only() -> None:
    api = FakeMt5Api()
    api.terminal.trade_allowed = False

    result = _module().Mt5DemoPreflight(api=api).run()

    assert result.environment == "demo"
    assert result.terminal_trading_enabled is False
    assert api.calls[-1] == "shutdown"


def test_exact_supported_quote_is_read_once_and_preserves_bid_ask_time_and_status() -> None:
    api = FakeMt5Api()
    result = _module().Mt5DemoPreflight(api=api).run(quote="EURUSD")

    assert api.calls.count(("symbol_info", "EURUSD")) == 1
    assert api.calls.count(("symbol_info_tick", "EURUSD")) == 1
    assert result.quote is not None
    assert result.quote.symbol == "EURUSD"
    assert result.quote.bid == 1.1042
    assert result.quote.ask == 1.1044
    assert result.quote.timestamp.tzinfo is not None
    assert result.quote.symbol_trade_mode == 4
    assert api.calls[-1] == "shutdown"


def test_unsupported_symbol_fails_before_terminal_initialization() -> None:
    api = FakeMt5Api()
    with pytest.raises(ValueError, match="unsupported_mt5_symbol"):
        _module().Mt5DemoPreflight(api=api).run(quote="EURUSD.a")
    assert api.calls == []


@pytest.mark.parametrize(
    "tick",
    [
        None,
        SimpleNamespace(time_msc=0, bid=1.1, ask=1.2),
        SimpleNamespace(time_msc=1_725_192_000_000, bid=math.nan, ask=1.2),
        SimpleNamespace(time_msc=1_725_192_000_000, bid=1.2, ask=1.1),
    ],
)
def test_malformed_or_unavailable_quote_fails_closed_and_shutdown(tick: object) -> None:
    api = FakeMt5Api()
    api.tick = tick
    with pytest.raises(RuntimeError, match="mt5_quote_invalid"):
        _module().Mt5DemoPreflight(api=api).run(quote="EURUSD")
    assert api.calls[-1] == "shutdown"


def test_exact_symbol_mismatch_fails_without_guessing_or_second_quote() -> None:
    api = FakeMt5Api()
    api.symbol.name = "EURUSD.a"
    with pytest.raises(RuntimeError, match="mt5_symbol_unavailable"):
        _module().Mt5DemoPreflight(api=api).run(quote="EURUSD")
    assert ("symbol_info_tick", "EURUSD") not in api.calls
    assert api.calls[-1] == "shutdown"


class FakeCliPreflight:
    instances: list[FakeCliPreflight] = []
    error: Exception | None = None

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.instances.append(self)

    def run(self, *, quote: str | None = None) -> object:
        self.calls.append(("run", quote))
        if self.error is not None:
            raise self.error
        module = _module()
        tick = None
        if quote is not None:
            tick = module.Mt5Quote(
                "EURUSD",
                module.datetime.fromtimestamp(1_725_192_000.25, tz=module.UTC),
                1.1042,
                1.1044,
                4,
            )
        return module.Mt5PreflightResult(
            "demo",
            "MetaTrader5",
            "5.0.4410",
            "****5678",
            "Pepperstone-Demo",
            "Pepperstone Group Limited",
            "USD",
            True,
            True,
            True,
            True,
            tick,
        )


@pytest.fixture(autouse=True)
def _fake_cli_preflight(monkeypatch: pytest.MonkeyPatch):
    FakeCliPreflight.instances.clear()
    FakeCliPreflight.error = None
    monkeypatch.setattr(cli_module, "Mt5DemoPreflight", FakeCliPreflight, raising=False)
    yield
    FakeCliPreflight.error = None


def test_cli_preflight_has_no_quote_request_by_default_and_sanitizes_account() -> None:
    result = runner.invoke(app, ["mt5", "preflight"])
    assert result.exit_code == 0
    assert FakeCliPreflight.instances[0].calls == [("run", None)]
    assert "demo" in result.output.lower()
    assert "Pepperstone-Demo" in result.output
    assert "****5678" in result.output
    assert "12345678" not in result.output


def test_cli_quote_reads_exactly_the_requested_supported_symbol() -> None:
    result = runner.invoke(app, ["mt5", "preflight", "--quote", "EURUSD"])
    assert result.exit_code == 0
    assert FakeCliPreflight.instances[0].calls == [("run", "EURUSD")]
    assert "1.1042" in result.output
    assert "1.1044" in result.output


def test_cli_failure_never_prints_sensitive_exception_or_account() -> None:
    FakeCliPreflight.error = RuntimeError("login=12345678 password=secret")
    result = runner.invoke(app, ["mt5", "preflight"])
    assert result.exit_code == 1
    assert "MT5 preflight failed" in result.output
    assert "12345678" not in result.output
    assert "secret" not in result.output
    assert "12345678" not in repr(result.exception)


def test_mt5_cli_exposes_only_read_only_preflight() -> None:
    root = runner.invoke(app, ["mt5", "--help"])
    assert root.exit_code == 0
    assert "preflight" in root.output
    for forbidden in ("order", "close", "cancel", "trade", "run"):
        result = runner.invoke(app, ["mt5", forbidden])
        assert result.exit_code == 2
        assert "no such command" in result.output.lower()
