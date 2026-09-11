"""Offline CLI tests for the read-only OANDA Practice preflight boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import fxlab.cli as cli_module
from fxlab.cli import app
from fxlab.execution.broker import AccountInfo, Tick
from fxlab.execution.broker_capabilities import (
    BrokerCapability,
    BrokerDescriptor,
    BrokerEnvironment,
)
from fxlab.execution.oanda_demo_broker import OandaPracticeConfig

runner = CliRunner()
ACCOUNT_ENV = "FXLAB_OANDA_PRACTICE_ACCOUNT_ID"
TOKEN_ENV = "FXLAB_OANDA_PRACTICE_TOKEN"
TIMEOUT_ENV = "FXLAB_OANDA_TIMEOUT_SECONDS"
QUOTE_AGE_ENV = "FXLAB_OANDA_MAX_QUOTE_AGE_SECONDS"


class FakePracticeBroker:
    instances: list[FakePracticeBroker] = []
    account_error: Exception | None = None

    def __init__(
        self,
        account_id: str,
        token: str,
        *,
        timeout_seconds: float,
        max_quote_age: timedelta,
    ) -> None:
        self.account_id = account_id
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_quote_age = max_quote_age
        self.connected = False
        self.disconnected = False
        self.calls: list[object] = []
        self.broker_descriptor = BrokerDescriptor(
            broker_id="oanda-v20",
            implementation_version="2",
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
        self.instances.append(self)

    def connect(self) -> None:
        self.calls.append("connect")
        self.connected = True

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False
        self.disconnected = True

    def get_account_info(self) -> AccountInfo:
        self.calls.append("account")
        if self.account_error is not None:
            raise self.account_error
        return AccountInfo(10000.0, 10000.0, 0.0, 10000.0, "USD", [])

    def subscribe_market_data(self, symbols: list[str]) -> None:
        self.calls.append(("subscribe", tuple(symbols)))

    def get_latest_tick(self, symbol: str) -> Tick:
        self.calls.append(("quote", symbol))
        return Tick(symbol, datetime(2023, 12, 29, 12, tzinfo=UTC), 1.1, 1.1002, 1.1001)

    def submit_order(self, order: object) -> str:
        raise AssertionError("preflight must not submit orders")

    def close_position(self, position_id: str) -> str:
        raise AssertionError("preflight must not close positions")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("preflight must not cancel orders")


@pytest.fixture(autouse=True)
def _fake_broker(monkeypatch: pytest.MonkeyPatch):
    FakePracticeBroker.instances.clear()
    FakePracticeBroker.account_error = None
    monkeypatch.setattr(cli_module, "OandaDemoBroker", FakePracticeBroker, raising=False)
    for name in (ACCOUNT_ENV, TOKEN_ENV, TIMEOUT_ENV, QUOTE_AGE_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    FakePracticeBroker.account_error = None


def _valid_env(**changes: str) -> dict[str, str]:
    values = {
        ACCOUNT_ENV: "practice-account-1234",
        TOKEN_ENV: "private-token-never-print",
    }
    values.update(changes)
    return values


def test_practice_configuration_repr_contains_no_credentials() -> None:
    settings = OandaPracticeConfig.from_environment(_valid_env())
    visible = repr(settings)
    assert "practice-account-1234" not in visible
    assert "private-token-never-print" not in visible


@pytest.mark.parametrize(
    "environment",
    [
        {TOKEN_ENV: "token"},
        {ACCOUNT_ENV: "account"},
        {ACCOUNT_ENV: "   ", TOKEN_ENV: "token"},
        {ACCOUNT_ENV: "account", TOKEN_ENV: "   "},
    ],
)
def test_missing_or_blank_practice_credentials_fail_before_broker_construction(
    environment: dict[str, str],
) -> None:
    result = runner.invoke(app, ["oanda", "preflight"], env=environment)
    assert result.exit_code == 2
    assert FakePracticeBroker.instances == []
    assert "practice credentials unavailable" in result.output.lower()
    assert "token" not in result.output.lower()


@pytest.mark.parametrize(
    "name,value",
    [(TIMEOUT_ENV, "0"), (TIMEOUT_ENV, "nan"), (QUOTE_AGE_ENV, "bad")],
)
def test_invalid_optional_practice_settings_fail_closed(name: str, value: str) -> None:
    result = runner.invoke(app, ["oanda", "preflight"], env=_valid_env(**{name: value}))
    assert result.exit_code == 2
    assert FakePracticeBroker.instances == []
    assert "practice settings invalid" in result.output.lower()


def test_valid_preflight_reports_sanitized_capabilities_without_quote_or_order() -> None:
    result = runner.invoke(
        app,
        ["oanda", "preflight"],
        env=_valid_env(
            FXLAB_OANDA_TIMEOUT_SECONDS="7.5",
            FXLAB_OANDA_MAX_QUOTE_AGE_SECONDS="3",
        ),
    )
    assert result.exit_code == 0
    broker = FakePracticeBroker.instances[0]
    assert broker.timeout_seconds == 7.5
    assert broker.max_quote_age == timedelta(seconds=3)
    assert broker.calls == ["connect", "account", "disconnect"]
    assert broker.disconnected
    assert "demo" in result.output.lower()
    assert "oanda-v20" in result.output
    assert "USD" in result.output
    assert "****1234" in result.output
    assert "practice-account-1234" not in result.output
    assert "private-token-never-print" not in result.output
    assert "market_orders" in result.output


def test_explicit_quote_reads_exactly_one_supported_quote_and_disconnects() -> None:
    result = runner.invoke(
        app,
        ["oanda", "preflight", "--quote", "EURUSD"],
        env=_valid_env(),
    )
    assert result.exit_code == 0
    broker = FakePracticeBroker.instances[0]
    assert broker.calls == [
        "connect",
        "account",
        ("subscribe", ("EURUSD",)),
        ("quote", "EURUSD"),
        "disconnect",
    ]
    assert "1.1002" in result.output
    assert "2023-12-29T12:00:00+00:00" in result.output
    assert "tradeable: True" in result.output


def test_unsupported_quote_is_rejected_before_broker_construction() -> None:
    result = runner.invoke(
        app,
        ["oanda", "preflight", "--quote", "USDJPY"],
        env=_valid_env(),
    )
    assert result.exit_code == 2
    assert FakePracticeBroker.instances == []
    assert "unsupported oanda quote symbol" in result.output.lower()


def test_disconnect_occurs_after_connected_preflight_failure_and_error_is_sanitized() -> None:
    FakePracticeBroker.account_error = RuntimeError("private-token-never-print")
    result = runner.invoke(app, ["oanda", "preflight"], env=_valid_env())
    assert result.exit_code == 1
    broker = FakePracticeBroker.instances[0]
    assert broker.calls == ["connect", "account", "disconnect"]
    assert broker.disconnected
    assert "private-token-never-print" not in result.output
    assert "private-token-never-print" not in repr(result.exception)


def test_oanda_cli_exposes_only_read_only_preflight() -> None:
    root = runner.invoke(app, ["oanda", "--help"])
    assert root.exit_code == 0
    assert "preflight" in root.output
    for forbidden in ("order", "close", "cancel", "trade", "run"):
        result = runner.invoke(app, ["oanda", forbidden])
        assert result.exit_code == 2
        assert "no such command" in result.output.lower()
