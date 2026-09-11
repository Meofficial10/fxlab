"""Offline tests for one explicitly authorized, audited MT5 demo smoke round trip."""

from __future__ import annotations

import importlib
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import fxlab.cli as cli_module
from fxlab.cli import app
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import AuditEventType, EventLedger

runner = CliRunner()
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
CONFIRMATION = "I_AUTHORIZE_MT5_DEMO_ORDER_SEND_AND_CLOSE"


def _module():
    return importlib.import_module("fxlab.execution.mt5_demo_smoke")


class FakeMt5MutationApi:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    POSITION_TYPE_BUY = 0
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self.calls: list[object] = []
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
        self.symbol = SimpleNamespace(
            name="EURUSD",
            visible=True,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            point=0.00001,
            digits=5,
            trade_stops_level=10,
            filling_mode=self.SYMBOL_FILLING_IOC,
        )
        self.base_tick_msc = int(NOW.timestamp() * 1000)
        self.tick_counter = 0
        self.auto_advance_tick = True
        self.tick_step_msc = 100
        self.custom_ticks: list[object] | None = None
        self.tick = SimpleNamespace(
            time_msc=self.base_tick_msc, bid=1.10000, ask=1.10020
        )
        self.positions: list[object] = []
        self.orders: list[object] = []
        self.entry_result: object | BaseException = SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=7001,
            deal=8001,
            volume=0.01,
            price=1.10020,
        )
        self.close_result: object | BaseException = SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=7002,
            deal=8002,
            volume=0.01,
            price=1.10000,
        )
        self.order_send_count = 0
        self.keep_position_after_close = False
        self.force_bad_correlation = False

    def initialize(self) -> bool:
        self.calls.append("initialize")
        return True

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def terminal_info(self) -> object:
        self.calls.append("terminal_info")
        return self.terminal

    def account_info(self) -> object:
        self.calls.append("account_info")
        return self.account

    def symbol_info(self, symbol: str) -> object:
        self.calls.append(("symbol_info", symbol))
        return self.symbol

    def symbol_info_tick(self, symbol: str) -> object:
        self.calls.append(("symbol_info_tick", symbol))
        if self.custom_ticks is not None:
            if not self.custom_ticks:
                return None
            return self.custom_ticks.pop(0)
        if self.auto_advance_tick:
            msc = self.base_tick_msc + self.tick_counter * self.tick_step_msc
            self.tick_counter += 1
            return SimpleNamespace(time_msc=msc, bid=1.10000, ask=1.10020)
        return self.tick

    def positions_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("positions_get", tuple(sorted(query.items()))))
        if "ticket" in query:
            return tuple(item for item in self.positions if item.ticket == query["ticket"])
        return tuple(item for item in self.positions if item.symbol == query.get("symbol"))

    def orders_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("orders_get", tuple(sorted(query.items()))))
        return tuple(item for item in self.orders if item.symbol == query.get("symbol"))

    def order_send(self, request: dict[str, object]) -> object:
        self.calls.append(("order_send", dict(request)))
        self.order_send_count += 1
        result = self.entry_result if self.order_send_count == 1 else self.close_result
        if isinstance(result, BaseException):
            raise result
        if getattr(result, "retcode", None) != self.TRADE_RETCODE_DONE:
            return result
        if self.order_send_count == 1:
            identifier = 9999 if self.force_bad_correlation else result.order
            self.positions = [
                item for item in self.positions if item.symbol != "EURUSD"
            ] + [
                SimpleNamespace(
                    ticket=9001,
                    identifier=identifier,
                    symbol="EURUSD",
                    type=self.POSITION_TYPE_BUY,
                    magic=request["magic"],
                    comment=request["comment"],
                    volume=request["volume"],
                    sl=request["sl"],
                )
            ]
        elif not self.keep_position_after_close:
            ticket = request.get("position")
            self.positions = [item for item in self.positions if item.ticket != ticket]
        return result


def _ledger(tmp_path) -> tuple[EventLedger, SQLiteEventStore]:
    store = SQLiteEventStore(tmp_path / "mt5-smoke.sqlite", "mt5-demo-smoke-test")
    return EventLedger(store.session_id, time_provider=lambda: NOW, durable_store=store), store


def _run(tmp_path, api: FakeMt5MutationApi | None = None, **kwargs):
    selected = api or FakeMt5MutationApi()
    ledger, store = _ledger(tmp_path)
    try:
        clock_fn = kwargs.get("clock", lambda: NOW)
        mono_val = [0.0]

        def default_monotonic():
            mono_val[0] += 0.001
            return mono_val[0]

        mono_fn = kwargs.get("monotonic", default_monotonic)
        sleeper_fn = kwargs.get("sleeper", lambda _: None)
        smoke = _module().Mt5DemoSmokeOrder(
            api=selected,
            clock=clock_fn,
            monotonic=mono_fn,
            sleeper=sleeper_fn,
            **{k: v for k, v in kwargs.items() if k not in ("clock", "monotonic", "sleeper")},
        )
        result = smoke.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
        )
        return selected, result, ledger.events()
    finally:
        store.close()


def test_demo_round_trip_uses_fixed_buy_minimum_volume_sl_and_exactly_two_sends(tmp_path) -> None:
    api, result, events = _run(tmp_path)
    sends = [call[1] for call in api.calls if isinstance(call, tuple) and call[0] == "order_send"]

    assert result.status == "successful_round_trip"
    assert result.account == "****5678"
    assert result.symbol == "EURUSD"
    assert result.side == "buy"
    assert result.volume == 0.01
    assert len(sends) == 2
    assert sends[0]["type"] == api.ORDER_TYPE_BUY
    assert sends[0]["volume"] == api.symbol.volume_min
    assert sends[0]["sl"] < api.tick.bid
    assert sends[1]["type"] == api.ORDER_TYPE_SELL
    assert sends[1]["position"] == 9001
    assert api.calls[-1] == "shutdown"
    assert sum(
        event.event_type is AuditEventType.ORDER_SUBMISSION_ATTEMPTED for event in events
    ) == 2
    assert sum(event.event_type is AuditEventType.ORDER_SUBMITTED for event in events) == 2
    assert sum(event.event_type is AuditEventType.ORDER_FILLED for event in events) == 2
    assert AuditEventType.POSITION_CLOSED in {event.event_type for event in events}


def test_python_package_without_symbol_filling_constants_uses_documented_flags(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.SYMBOL_FILLING_FOK = None
    api.SYMBOL_FILLING_IOC = None

    selected, result, _events = _run(tmp_path, api)

    sends = [
        call[1]
        for call in selected.calls
        if isinstance(call, tuple) and call[0] == "order_send"
    ]
    assert result.status == "successful_round_trip"
    assert [request["type_filling"] for request in sends] == [
        api.ORDER_FILLING_IOC,
        api.ORDER_FILLING_IOC,
    ]


@pytest.mark.parametrize("mode", [1, 2, 999, None])
def test_non_demo_or_unprovable_mode_rejected_before_order_send(tmp_path, mode: object) -> None:
    api = FakeMt5MutationApi()
    api.account.trade_mode = mode
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_demo_account_required"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0
    assert api.calls[-1] == "shutdown"


@pytest.mark.parametrize("confirmation", ["", "yes", "I_AUTHORIZE_MT5_LIVE_ORDER"])
def test_wrong_or_missing_confirmation_rejected_before_initialization(
    tmp_path, confirmation: str
) -> None:
    api = FakeMt5MutationApi()
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(ValueError, match="mt5_demo_smoke_confirmation_required"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=confirmation, ledger=ledger
            )
    finally:
        store.close()
    assert api.calls == []


def test_terminal_trading_disabled_rejected_before_order_send(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.terminal.trade_allowed = False
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_mutation_not_permitted"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0


@pytest.mark.parametrize("symbol", [None, SimpleNamespace(name="EURUSD.a")])
def test_wrong_or_unavailable_exact_symbol_rejected(tmp_path, symbol: object) -> None:
    api = FakeMt5MutationApi()
    api.symbol = symbol
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_symbol_invalid"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0


@pytest.mark.parametrize(
    "bad_tick",
    [
        None,
        SimpleNamespace(time_msc=0, bid=1.1, ask=1.2),
        SimpleNamespace(time_msc=-1000, bid=1.1, ask=1.2),
        SimpleNamespace(time_msc=False, bid=1.1, ask=1.2),
        SimpleNamespace(time_msc="bad", bid=1.1, ask=1.2),
        SimpleNamespace(time_msc=None, bid=1.1, ask=1.2),
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=math.nan, ask=1.2),
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=1.2, ask=1.1),
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=-1.0, ask=1.2),
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=1.1, ask=0.0),
    ],
)
def test_invalid_quote_rejected_before_order_send(tmp_path, bad_tick: object) -> None:
    api = FakeMt5MutationApi()
    api.custom_ticks = [bad_tick]
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_frozen_feed_without_tick_progression_times_out_and_rejects(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.auto_advance_tick = False
    api.tick = SimpleNamespace(
        time_msc=int(NOW.timestamp() * 1000),
        bid=1.10000,
        ask=1.10020,
    )
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_frozen_tick_at_plausible_timezone_grid_offset_rejected_fail_closed(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.auto_advance_tick = False
    api.tick = SimpleNamespace(
        time_msc=int((NOW + timedelta(hours=3)).timestamp() * 1000),
        bid=1.10000,
        ask=1.10020,
    )
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_tick_timestamp_regression_rejected_fail_closed(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.custom_ticks = [
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=1.10000, ask=1.10020),
        SimpleNamespace(time_msc=int(NOW.timestamp() * 1000) - 100, bid=1.10000, ask=1.10020),
    ]
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_demo_round_trip_with_plus_three_hour_broker_raw_timestamp_succeeds_and_audits(
    tmp_path,
) -> None:
    api = FakeMt5MutationApi()
    api.base_tick_msc = int((NOW + timedelta(hours=3)).timestamp() * 1000)
    api.auto_advance_tick = True
    api_instance, result, events = _run(tmp_path, api)

    assert result.status == "successful_round_trip"
    assert api_instance.order_send_count == 2
    bound_events = [e for e in events if e.event_type is AuditEventType.BROKER_CAPABILITIES_BOUND]
    assert len(bound_events) == 1
    payload = bound_events[0].payload
    assert payload["initial_tick_time_msc"] == api.base_tick_msc
    assert payload["fresh_tick_time_msc"] == api.base_tick_msc + 100
    assert payload["raw_tick_clock_delta_seconds"] == 10800.1
    assert 0.0 <= payload["local_observation_latency_seconds"] <= 5.0


def test_demo_round_trip_with_near_zero_broker_raw_timestamp_succeeds_and_audits(
    tmp_path,
) -> None:
    api = FakeMt5MutationApi()
    api.base_tick_msc = int(NOW.timestamp() * 1000)
    api.auto_advance_tick = True
    api_instance, result, events = _run(tmp_path, api)

    assert result.status == "successful_round_trip"
    assert api_instance.order_send_count == 2
    bound_events = [e for e in events if e.event_type is AuditEventType.BROKER_CAPABILITIES_BOUND]
    assert len(bound_events) == 1
    payload = bound_events[0].payload
    assert payload["initial_tick_time_msc"] == api.base_tick_msc
    assert payload["fresh_tick_time_msc"] == api.base_tick_msc + 100
    assert payload["raw_tick_clock_delta_seconds"] == 0.1
    assert 0.0 <= payload["local_observation_latency_seconds"] <= 5.0


def test_locally_stale_observed_tick_rejected_fail_closed(tmp_path) -> None:
    mono_time = [0.0]

    def advancing_monotonic():
        val = mono_time[0]
        mono_time[0] += 6.0
        return val

    api = FakeMt5MutationApi()
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api, monotonic=advancing_monotonic)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_negative_monotonic_elapsed_rejected_fail_closed(tmp_path) -> None:
    mono_time = [10.0]

    def regressing_monotonic():
        val = mono_time[0]
        # Regress on subsequent calls
        mono_time[0] -= 1.0
        return val

    api = FakeMt5MutationApi()
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
            _run(tmp_path, api, monotonic=regressing_monotonic)
    finally:
        store.close()
    assert api.order_send_count == 0


def test_observed_mt5_tick_assert_locally_fresh_unit_behavior() -> None:
    tick = _module().ObservedMt5Tick(
        raw_tick=object(),
        initial_tick_time_msc=1000,
        fresh_tick_time_msc=1100,
        observed_at_utc=NOW,
        observed_at_monotonic=10.0,
        raw_tick_clock_delta_seconds=0.0,
        bid=1.1,
        ask=1.2,
    )
    # Exact boundary 0.0s elapsed is allowed
    assert tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=10.0) == 0.0
    # Positive elapsed within 5s is allowed
    assert tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=12.0) == 2.0
    assert tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=15.0) == 5.0
    # Any negative elapsed must fail closed
    with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
        tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=9.999)
    with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
        tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=8.0)
    # Stale elapsed > 5s must fail closed
    with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
        tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=15.001)
    with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
        tick.assert_locally_fresh(timedelta(seconds=5), current_monotonic=16.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("volume_min", 0), ("volume_step", 0), ("volume_min", 0.015), ("volume_max", 0.001)],
)
def test_invalid_volume_metadata_rejected(tmp_path, field: str, value: float) -> None:
    api = FakeMt5MutationApi()
    setattr(api.symbol, field, value)
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_volume_invalid"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0


@pytest.mark.parametrize("kind", ["position", "order"])
def test_existing_relevant_position_or_order_rejected_without_mutation(tmp_path, kind: str) -> None:
    api = FakeMt5MutationApi()
    setattr(api, f"{kind}s", [SimpleNamespace(symbol="EURUSD", ticket=42)])
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_state_not_clean"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("point", 0), ("digits", -1), ("trade_stops_level", -1)],
)
def test_invalid_sl_constraints_rejected_before_submission(
    tmp_path, field: str, value: object
) -> None:
    api = FakeMt5MutationApi()
    setattr(api.symbol, field, value)
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_smoke_sl_invalid"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 0


def test_ambiguous_entry_is_not_retried_and_requires_reconciliation(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.entry_result = TimeoutError("account=12345678 secret")
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_entry_reconciliation_required") as caught:
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
        events = ledger.events()
    finally:
        store.close()
    assert api.order_send_count == 1
    assert "12345678" not in str(caught.value)
    assert events[-1].event_type is AuditEventType.ORDER_SUBMISSION_INDETERMINATE


def test_broker_rejection_is_distinct_and_not_retried(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.entry_result = SimpleNamespace(retcode=api.TRADE_RETCODE_REJECT)
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_entry_broker_rejected"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        events = ledger.events()
        store.close()
    assert api.order_send_count == 1
    assert events[-1].event_type is AuditEventType.ORDER_REJECTED


def test_exact_entry_correlation_is_required_and_unrelated_positions_never_closed(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.force_bad_correlation = True
    unrelated = SimpleNamespace(symbol="USDJPY", ticket=55)
    api.positions = [unrelated]
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_entry_reconciliation_required"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 1
    assert unrelated in api.positions


def test_ambiguous_close_is_not_retried_and_position_remains_for_reconciliation(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.close_result = TimeoutError("secret")
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_close_reconciliation_required"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 2
    assert len(api.positions) == 1


def test_final_correlated_position_must_be_flat(tmp_path) -> None:
    api = FakeMt5MutationApi()
    api.keep_position_after_close = True
    ledger, store = _ledger(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="mt5_close_reconciliation_required"):
            _module().Mt5DemoSmokeOrder(api=api, clock=lambda: NOW).run(
                confirmation=CONFIRMATION, ledger=ledger
            )
    finally:
        store.close()
    assert api.order_send_count == 2


def test_durable_audit_contains_sanitized_full_round_trip_evidence(tmp_path) -> None:
    api, result, events = _run(tmp_path)
    assert result.status == "successful_round_trip"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    visible = repr(events) + repr(result)
    assert "12345678" not in visible
    assert "password" not in visible.lower()
    assert {event.event_type for event in events} >= {
        AuditEventType.ACCOUNT_OBSERVED,
        AuditEventType.OPERATOR_CONTROL_ACTION,
        AuditEventType.BROKER_CAPABILITIES_BOUND,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.POSITION_CLOSED,
    }
    assert api.calls[-1] == "shutdown"


def test_cli_requires_exact_confirmation_and_explicit_audit_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "Mt5DemoSmokeOrder", object, raising=False)
    missing = runner.invoke(app, ["mt5", "demo-smoke-order"])
    assert missing.exit_code == 2
    wrong = runner.invoke(
        app,
        [
            "mt5",
            "demo-smoke-order",
            "--confirm",
            "yes",
            "--audit-db",
            str(tmp_path / "audit.sqlite"),
        ],
    )
    assert wrong.exit_code != 0


def test_cli_exact_authorized_command_invokes_smoke_workflow_once(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, EventLedger]] = []

    class FakeSmokeOrder:
        def run(self, *, confirmation: str, ledger: EventLedger) -> object:
            calls.append((confirmation, ledger))
            return SimpleNamespace(
                status="successful_round_trip",
                account="****5678",
                symbol="EURUSD",
                side="buy",
                volume=0.01,
                entry_order_id="7001",
                entry_deal_id="8001",
                position_id="9001",
                close_order_id="7002",
                close_deal_id="8002",
            )

    monkeypatch.setattr(cli_module, "Mt5DemoSmokeOrder", FakeSmokeOrder, raising=False)
    result = runner.invoke(
        app,
        [
            "mt5",
            "demo-smoke-order",
            "--confirm",
            CONFIRMATION,
            "--audit-db",
            str(tmp_path / "audit.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == CONFIRMATION
    assert calls[0][1].durable_store is not None
    assert "12345678" not in result.output
