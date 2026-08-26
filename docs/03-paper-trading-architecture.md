# Paper-Trading Infrastructure Architecture

**Date:** 2026-08-26
**Status:** IMPLEMENTED THROUGH PHASE 7; CURRENT ROADMAP ACTIVE
**Prerequisites:** Research layer frozen at P4 NO-GO; no validated strategy exists  
**Scope:** Trading-system infrastructure only — NO strategy creation, NO test window access

> **Foundation-repair clarification (2026-08-25):** The focused decisions in
> `docs/04-foundation-repair-decisions.md` supersede this draft wherever it discusses
> market-data closure/overlap, SignalEngine duplicate scope, or ownership of future
> execution-intent and risk concerns. No research-layer contract is changed.

> **Current-roadmap notice (2026-08-26):** The roadmap below records the actual
> implementation state and supersedes the original draft sequencing in §§5, 7–10,
> and 12. Those later sections remain as historical design context, not current claims.

## Current master roadmap

### Completed execution foundation

| Phase | Scope | Status |
|---|---|---|
| 1 | Broker contracts | **DONE** |
| 2 | Market data stream | **DONE** |
| 3 | Signal engine | **DONE** |
| 4 | Risk engine | **DONE** |
| 5 | Order manager | **DONE** |
| 6 | Paper session and deterministic historical replay | **DONE** |
| 7 | Economic paper execution and accounting | **DONE** |

Phase 7 provides mark-to-market and realized PnL, balance/equity accounting,
finite negative economic balances, deterministic closing, close-only SL/TP execution,
configured transaction costs, immutable close events, and RiskEngine trade-close
notification.

Current limitations are explicit:

- Monetary PnL uses a fixed `$10` pip-value-per-lot simplification. It is not universal
  account-currency conversion and may approximate JPY and cross-currency results.
- Historical replay observes one quote at each bar close; it makes no intrabar ordering
  claim.
- Margin and leverage are not modeled.
- Runtime state is in memory only; there is no persistence or restart recovery.
- No live broker is implemented.
- No dynamic symbol/account-currency conversion exists.

### Reliability and application track

#### Phase 8 — Event ledger / immutable execution audit

Add an append-only runtime event ledger with session and correlation IDs. The event
vocabulary should cover, at minimum:

`SESSION_STARTED`, `MARKET_EVENT`, `SIGNAL_EMITTED`, `SIGNAL_DECLINED`,
`RISK_APPROVED`, `RISK_REJECTED`, `KILL_SWITCH_TRIGGERED`, `ORDER_SUBMITTED`,
`ORDER_FILLED`, `ORDER_REJECTED`, `ORDER_CANCELLED`, `POSITION_OPENED`,
`POSITION_MARKED`, `POSITION_CLOSED`, `RECONCILIATION_FAILED`, and `SESSION_STOPPED`.

This ledger is not implemented yet.

#### Phase 9 — Checkpoint and crash recovery

Persist and reconstruct operational state: session ID, latest processed market timestamp,
account state, open positions, pending orders, risk counters, peak and daily-start equity,
kill-switch state, approved IDs, and reservations. Resume must verify configuration and
software-version identity. Incompatible state must fail closed; there is no silent
recovery.

#### Phase 10 — Reconciliation engine

Compare internal order/position state with authoritative broker account, order, and
position state. Any mismatch freezes new entries, triggers
`POSITION_RECONCILIATION_FAILED`, records an event, and requires controlled resolution.

#### Phase 11 — Market-data provider abstraction

Introduce a normalized provider boundary:

```text
provider-specific data
    -> FXLAB canonical data contract
    -> research and execution consumers
```

Potential providers include Dukascopy, HistoricalReplay, an optional OpenBB adapter,
and broker market-data providers. OpenBB must not become a hard dependency.

#### Phase 12 — Broker capability system / demo broker adapter

Declare capabilities explicitly: market, limit, and stop orders; native SL/TP; hedging;
netting; partial close; and client-order IDs. PaperBroker remains the first implementation.
A real or demo external adapter follows only after this boundary is stable.

#### Phase 13 — Runtime control plane

Runtime states are `RUNNING`, `PAUSED`, `KILL_SWITCHED`,
`RECONCILIATION_REQUIRED`, and `STOPPED`. Operator controls cover status, pause, resume,
stop, manual kill switch, and inspection of orders, positions, and risk. `PAUSED` blocks
new entries while monitoring and reconciliation continue.

#### Phase 14 — CLI application runner

Keep application logic outside the CLI. Potential commands are:

- `fxlab paper replay`
- `fxlab paper start`
- `fxlab paper status`
- `fxlab paper stop`
- `fxlab paper orders`
- `fxlab paper positions`
- `fxlab paper events`

#### Phase 15 — Monitoring dashboard

Build only after runtime correctness is stable. Present account balance, equity, realized
and unrealized PnL, drawdown; risk loss, consecutive losses, kill switch, reservations;
positions, orders, broker health, data freshness, reconciliation health, and latest event.

#### Phases 16–20 — Later execution realism and operations

| Phase | Planned scope |
|---|---|
| 16 | External data connectors and optional OpenBB adapter |
| 17 | External broker adapters |
| 18 | Dynamic pip value, account-currency conversion, leverage/margin, partial fills, latency, richer slippage, and variable spread |
| 19 | Deployment, secrets, and operational hardening |
| 20 | Explicit live-readiness audit |

None of Phases 8–20 is implemented by this roadmap update.

### Edge / performance research track

The execution roadmap and the research roadmap are separate workstreams. Current
research status is:

- Models A–F failed P4 robustness.
- Multi-asset TSMOM failed P4 robustness.
- No validated trading edge currently exists.
- Improving infrastructure does not automatically improve win rate or expectancy.

The objective is positive **out-of-sample expectancy after realistic costs**, with
acceptable drawdown and sufficient observations. Win rate alone is not the target.
Every assessment must report trade count, win rate, average win, average loss, payoff
ratio, expectancy, profit factor, Sharpe or another appropriate risk-adjusted measure,
maximum drawdown, cost drag, regime/sub-period stability, and cross-market stability.

#### Research R1 — Freeze execution infrastructure baseline

After the reliability-critical simulator is trustworthy, freeze its assumptions and
version. Every research result references the execution-model version or commit hash.

#### Research R2 — Candidate mechanism registry

Before testing, register the hypothesis, economic or structural prior, exact entry and
exit rules, SL/TP rule, timeframe, universe, expected failure mode, and predefined
acceptance gate. Do not tune until a mechanism first demonstrates profitability under
the predefined evaluation.

#### Research R3 — Candidate generation

Investigate genuinely different mechanisms rather than minor parameter variants.
Candidate classes may include cross-sectional/relative-value, volatility regime,
trend/regime-conditioned behavior, carry or rate differential where proper data exists,
mean reversion, momentum, session/microstructure effects, and portfolio diversification.
Rejected SMC results are not evidence for a new mechanism.

#### Research R4 — Train/validation testing

Use train and validation data only. Include spread, commission, slippage, +50% cost
stress, walk-forward validation, cross-pair or cross-instrument validation, leakage
tests, and future-invariance tests. The test window remains sealed.

#### Research R5 — Statistical robustness

Require enough observations and examine confidence intervals, appropriate t-statistics
or uncertainty estimates, parameter sensitivity, sub-period consistency, regime
dependence, and multiple-comparison risk. One lucky parameter cell is not promotable.

#### Research R6 — Untouched test gate

Only a mechanism passing all earlier gates may open the sealed test set once under the
research charter. Do not retune after observing the test result.

#### Research R7 — Deterministic paper forward validation

Run a validated strategy through the actual paper-execution system. Compare expected
backtest behavior with realized paper behavior, including signal and execution
differences, cost differences, missed trades, drawdown differences, latency, and data
problems.

#### Research R8 — Live-demo forward validation

Only a strategy that survives deterministic paper validation may enter a broker
demo/practice environment. This stage uses no real money.

#### Research R9 — Live-readiness gate

There is no automatic promotion. Live readiness eventually requires validated positive
expectancy, acceptable drawdown, execution realism, sufficient sample size, paper-forward
consistency, no unresolved reconciliation defects, stable risk controls, operational
recovery, and broker-capability verification.

### AI / agent policy

AI may later summarize experiments, discover research questions, explain failures,
classify market context, collect news or macro context, generate hypotheses, and compare
experiments. AI does not receive authority to bypass validation or risk controls.

```text
LLM says BUY -> execute trade
```

is not an allowed architecture. Every AI-produced mechanism must pass the same
deterministic research gates. No AI trading mechanism is implemented now.

### External project lessons

These projects are architectural references only:

- **Fincept:** broker modularity, terminal/monitoring separation, and paper/live
  execution boundaries.
- **OpenBB:** standardized provider abstraction, normalized data contracts, and
  connect-once/consume-many architecture.
- **TradingAgents:** verified/as-of data access, decision logging, recovery/checkpoint
  concepts, and AI kept research-oriented rather than treated as proof of edge.
- **Paperclip:** immutable activity history, approvals and governance, pause/kill
  controls, recovery, budget/cost controls, and an operational control plane.

Their strategies are not copied, and popularity or repository stars are not evidence of
profitability.

### Master rules

> **INFRASTRUCTURE CONFIDENCE != TRADING EDGE**

> **AI RESEARCH != PERMISSION TO TRADE**

The system may progress toward live execution only when a strategy independently passes
the research gates. Architecture improvements are described by the properties they
guarantee; no artificial confidence percentages are assigned without an explicitly
defined measurement.

---

## Executive Summary

This document defines the **paper-trading infrastructure** for fxlab as a standalone engineering effort, completely decoupled from the research layer (frozen at P4 NO-GO per ADR 0001–0005). The goal is to build the execution, risk, and monitoring machinery **around a future validated strategy**, should one emerge from a structurally-motivated P4 attempt.

**Critical constraints (charter + audit directive):**
- ✅ No new trading mechanisms (Models A–G blocked)
- ✅ 2024+ test window stays sealed
- ✅ No modification of P4 acceptance criteria
- ✅ No live money, no real orders, no profitability claims
- ✅ Current research result: P4 NO-GO (no robust edge)

**What this architecture provides:**
- Paper-trading simulation (simulated fills, no real money)
- Abstract broker adapter interface (for future live integration)
- Real-time market data ingestion
- Signal-to-execution bridge (Setup → orders)
- Risk engine (sizing, limits, kill-switches)
- Order/execution abstraction
- Position reconciliation
- Trade/event logging (append-only audit trail)
- Monitoring/alerting dashboard
- Clean separation between research and execution layers

---

## 1. System Overview

### 1.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         RESEARCH LAYER (FROZEN)                          │
│  ┌────────────┐   ┌──────────┐   ┌─────────┐   ┌─────────────────────┐ │
│  │  Setups    │──▶│ Backtest │──▶│ Metrics │──▶│ ADRs / Experiments  │ │
│  │ (Models    │   │  Engine  │   │         │   │  (P4 NO-GO)         │ │
│  │  A-F)      │   │          │   │         │   └─────────────────────┘ │
│  └────────────┘   └──────────┘   └─────────┘                            │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   │ IF validated strategy emerges
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      EXECUTION LAYER (THIS DESIGN)                       │
│                                                                          │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐    │
│  │  Real-Time   │───▶│  Signal Engine │───▶│   Risk Engine       │    │
│  │  Market Data │    │  (Setup.gen +  │    │  - Position sizing  │    │
│  │  Ingestion   │    │   validation)  │    │  - Exposure limits  │    │
│  └──────────────┘    └────────────────┘    │  - Kill switches    │    │
│         │                     │             └──────────┬──────────┘    │
│         │                     │                        │               │
│         ▼                     ▼                        ▼               │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐    │
│  │   Market     │    │  Order Manager │◀───│  Broker Adapter     │    │
│  │   State      │    │  - Lifecycle   │    │  (Abstract)         │    │
│  │   Cache      │    │  - Fills       │    │   ├─ Paper impl     │    │
│  └──────────────┘    │  - Slippage    │    │   └─ Live (future)  │    │
│                      └────────────────┘    └─────────────────────┘    │
│         │                     │                        │               │
│         └─────────────────────┴────────────────────────┘               │
│                               │                                        │
│                               ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │              Position Reconciliation + Event Log                  │ │
│  │  - Sync internal state ↔ broker                                   │ │
│  │  - Append-only audit trail (orders, fills, risk events)           │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                               │                                        │
│                               ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │         Monitoring Dashboard + Alerts                             │ │
│  │  - Real-time metrics (PnL, DD, Sharpe, exposure)                  │ │
│  │  - Breach alerts (risk limits, kill-switch triggers)              │ │
│  │  - Health checks (latency, connection, reconciliation)            │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Design Principles

1. **Separation of concerns:** Research (frozen) vs execution (this design)
2. **Safety-first:** No real money until explicit live approval; kill-switches mandatory
3. **Audit trail:** Append-only log of every decision (order, fill, risk event)
4. **Reconciliation:** Internal state must match broker state; detect discrepancies
5. **Observability:** Real-time monitoring, alerts, health checks
6. **Modularity:** Abstract broker adapter allows paper ↔ live swap
7. **Testability:** Every component unit-testable; integration tests before paper
8. **Charter compliance:** AI never bypasses risk engine (§1.6)

---

## 2. Component Design

### 2.1 Broker Adapter Interface

**Purpose:** Abstract interface to market data and order execution, allowing paper ↔ live swap.

**Protocol:**

```python
# src/fxlab/execution/broker.py

from __future__ import annotations
from typing import Protocol, runtime_checkable
from dataclasses import dataclass
from datetime import datetime
import pandas as pd

@dataclass
class Tick:
    """Real-time market tick."""
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    mid: float
    
@dataclass
class AccountInfo:
    """Account state snapshot."""
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    open_positions: list[Position]

@dataclass
class Position:
    """Open position."""
    symbol: str
    side: int  # +1 long, -1 short
    size: float  # lots
    entry_price: float
    entry_time: datetime
    unrealized_pnl: float
    position_id: str

@dataclass
class OrderRequest:
    """Order submission."""
    symbol: str
    side: int  # +1 long, -1 short
    size: float  # lots
    order_type: str  # "market" | "limit" | "stop"
    price: float | None  # for limit/stop
    sl_price: float | None
    tp_price: float | None
    order_id: str  # client-assigned unique ID

@dataclass
class OrderFill:
    """Fill confirmation."""
    order_id: str
    fill_price: float
    fill_time: datetime
    fill_size: float
    commission: float
    slippage_pips: float

@runtime_checkable
class BrokerAdapter(Protocol):
    """Abstract broker interface — paper or live."""
    
    def connect(self) -> None:
        """Establish connection (or no-op for paper)."""
        ...
    
    def disconnect(self) -> None:
        """Close connection."""
        ...
    
    def is_connected(self) -> bool:
        """Connection health check."""
        ...
    
    def subscribe_market_data(self, symbols: list[str]) -> None:
        """Subscribe to real-time ticks for given symbols."""
        ...
    
    def get_latest_tick(self, symbol: str) -> Tick | None:
        """Most recent tick for symbol."""
        ...
    
    def get_account_info(self) -> AccountInfo:
        """Current account state."""
        ...
    
    def submit_order(self, order: OrderRequest) -> str:
        """Submit order; returns broker order_id."""
        ...
    
    def get_order_status(self, order_id: str) -> dict:
        """Order state: pending | filled | rejected | cancelled."""
        ...
    
    def cancel_order(self, order_id: str) -> bool:
        """Attempt to cancel pending order."""
        ...
    
    def close_position(self, position_id: str) -> str:
        """Close open position; returns close order_id."""
        ...
    
    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Fetch recent bars for warm-up (e.g., ATR calculation)."""
        ...
```

**Implementations:**

1. **PaperBroker** (`src/fxlab/execution/paper_broker.py`):
   - Simulated fills using existing `CostModel`
   - No real money
   - Instant fills at bid/ask ± slippage
   - In-memory account ledger
   - Used for P8 (paper trading phase)

2. **LiveBroker** (future, P9+):
   - OANDA v20 API or MetaTrader5
   - Real orders, real fills
   - Requires explicit approval, never enabled by default
   - Not in scope for this design phase

---

### 2.2 Real-Time Market Data Ingestion

**Purpose:** Stream live tick/bar data from broker, buffer, and feed to signal engine.

**Architecture:**

```python
# src/fxlab/execution/market_data.py

from dataclasses import dataclass, field
from collections import deque
from threading import Lock
import pandas as pd

@dataclass
class MarketDataStream:
    """Real-time market data manager."""
    
    broker: BrokerAdapter
    symbols: list[str]
    tick_buffer_size: int = 1000
    
    # Thread-safe tick buffers per symbol
    _tick_buffers: dict[str, deque] = field(default_factory=dict, init=False)
    _locks: dict[str, Lock] = field(default_factory=dict, init=False)
    
    # Aggregated bar cache (for closed-candle strategy input)
    _bar_cache: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict, init=False)
    
    def start(self) -> None:
        """Start streaming ticks from broker."""
        self.broker.subscribe_market_data(self.symbols)
        for sym in self.symbols:
            self._tick_buffers[sym] = deque(maxlen=self.tick_buffer_size)
            self._locks[sym] = Lock()
    
    def on_tick(self, tick: Tick) -> None:
        """Callback for incoming tick (broker pushes or polling thread)."""
        with self._locks[tick.symbol]:
            self._tick_buffers[tick.symbol].append(tick)
    
    def get_latest_closed_bar(self, symbol: str, tf: str) -> pd.Series | None:
        """Most recent CLOSED bar for given timeframe (for Setup.generate input).
        
        Strategy must NEVER see the currently-forming bar — closed-candle discipline.
        """
        # Aggregate ticks → bars, return only closed
        ...
    
    def get_closed_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Recent N closed bars for strategy warm-up (e.g., ATR window)."""
        # Use broker.get_historical_bars() + live aggregation
        ...
```

**Key properties:**
- ✅ **Closed-candle only:** Strategy never sees forming bar
- ✅ **Thread-safe:** Tick callbacks may arrive from broker thread
- ✅ **Buffered:** Handle tick bursts without blocking
- ✅ **Leakage-safe:** Same doctrine as backtest (right-labelled, no look-ahead)

---

### 2.3 Signal Engine

**Purpose:** Bridge between `Setup.generate()` (research) and order submission (execution).

**Responsibilities:**
1. Periodically call `Setup.generate(closed_bars)` when new bar closes
2. Validate signals (symbol exists, side valid, bar is closed)
3. Pass validated signals to Risk Engine for sizing + approval
4. Do NOT execute directly — Risk Engine owns that decision

**Architecture:**

```python
# src/fxlab/execution/signal_engine.py

from dataclasses import dataclass
from typing import Callable
import pandas as pd
from ..setups.base import Setup

@dataclass
class SignalEvent:
    """Signal from Setup, not yet an order."""
    setup_name: str
    symbol: str
    side: int  # +1 long, -1 short
    signal_time: datetime
    signal_bar_index: int  # which bar triggered it
    confidence: float = 1.0  # for future ML filter (P5)

@dataclass
class SignalEngine:
    """Runs Setup.generate() on closed bars → emits SignalEvents."""
    
    setup: Setup
    market_data: MarketDataStream
    risk_engine: RiskEngine  # owns actual order submission
    
    check_interval_seconds: float = 1.0  # poll for new closed bars
    
    _last_bar_time: dict[tuple[str, str], datetime] = field(default_factory=dict, init=False)
    
    def start(self) -> None:
        """Start periodic signal generation loop (runs in background thread)."""
        ...
    
    def stop(self) -> None:
        """Stop signal loop."""
        ...
    
    def _check_for_signals(self) -> None:
        """Called every check_interval_seconds."""
        for symbol in self.symbols:
            # Get latest CLOSED bar for setup's timeframe
            latest_bar = self.market_data.get_latest_closed_bar(symbol, self.timeframe)
            
            if latest_bar is None:
                continue
            
            # Check if this is a NEW closed bar we haven't processed
            key = (symbol, self.timeframe)
            if key in self._last_bar_time and latest_bar.name <= self._last_bar_time[key]:
                continue  # already processed this bar
            
            # Get full bar window for Setup.generate (e.g., last 500 bars)
            bars = self.market_data.get_closed_bars(symbol, self.timeframe, count=500)
            
            # Call Setup.generate (RESEARCH CODE — frozen, unchanged)
            signal_idx, sides = self.setup.generate(bars)
            
            # Check if the latest bar triggered a signal
            if len(signal_idx) > 0 and signal_idx[-1] == len(bars) - 1:
                signal = SignalEvent(
                    setup_name=self.setup.name,
                    symbol=symbol,
                    side=sides[-1],
                    signal_time=datetime.now(timezone.utc),
                    signal_bar_index=signal_idx[-1],
                )
                
                # Pass to Risk Engine for sizing + approval
                self.risk_engine.on_signal(signal)
            
            # Mark this bar as processed
            self._last_bar_time[key] = latest_bar.name
```

**Critical invariant:** `Setup.generate()` is **never modified** — it's the frozen research code. The signal engine is a thin adapter that calls it on closed bars and routes output to the risk engine.

---

### 2.4 Risk Engine

**Purpose:** Enforce position sizing, exposure limits, kill-switches per charter §1.6 ("AI never bypasses risk engine").

**Architecture:**

```python
# src/fxlab/risk/engine.py

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

class KillSwitchReason(Enum):
    """Why the kill-switch triggered."""
    MAX_DRAWDOWN = "max_drawdown_breached"
    MAX_DAILY_LOSS = "max_daily_loss_breached"
    MAX_CONSECUTIVE_LOSSES = "max_consecutive_losses_breached"
    POSITION_RECONCILIATION_FAILED = "position_sync_failed"
    MANUAL = "manual_shutdown"

@dataclass
class RiskLimits:
    """Hard constraints (from config)."""
    max_risk_per_trade_pct: float = 0.01  # 1% of account per trade
    max_daily_loss_pct: float = 0.03  # 3% daily loss limit
    max_drawdown_pct: float = 0.10  # 10% drawdown from peak
    max_consecutive_losses: int = 5
    max_trades_per_day: int = 10
    max_open_positions: int = 1  # P2 baseline: one position at a time
    max_exposure_per_symbol_lots: float = 1.0
    max_total_exposure_lots: float = 2.0

@dataclass
class RiskEngine:
    """Position sizing + kill-switches + hard limits."""
    
    broker: BrokerAdapter
    risk_limits: RiskLimits
    event_log: EventLogger
    
    # State
    _kill_switch_active: bool = False
    _kill_switch_reason: KillSwitchReason | None = None
    _consecutive_losses: int = 0
    _daily_trades: int = 0
    _daily_reset_time: datetime = field(default_factory=datetime.now)
    _peak_equity: float = 0.0
    
    def on_signal(self, signal: SignalEvent) -> None:
        """Signal from Setup → sizing + approval → order submission (or rejection)."""
        
        # 1. Check kill-switch
        if self._kill_switch_active:
            self.event_log.log_signal_rejected(signal, reason="kill_switch_active")
            return
        
        # 2. Check daily trade limit
        if self._daily_trades >= self.risk_limits.max_trades_per_day:
            self.event_log.log_signal_rejected(signal, reason="max_daily_trades")
            return
        
        # 3. Check open position count
        account = self.broker.get_account_info()
        if len(account.open_positions) >= self.risk_limits.max_open_positions:
            self.event_log.log_signal_rejected(signal, reason="max_open_positions")
            return
        
        # 4. Position sizing (e.g., fixed-fractional)
        size_lots = self._calculate_position_size(signal, account)
        
        if size_lots <= 0:
            self.event_log.log_signal_rejected(signal, reason="size_calculation_failed")
            return
        
        # 5. Check exposure limits
        if size_lots > self.risk_limits.max_exposure_per_symbol_lots:
            size_lots = self.risk_limits.max_exposure_per_symbol_lots
        
        # 6. Submit order via broker
        order = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            size=size_lots,
            order_type="market",
            price=None,
            sl_price=self._calculate_sl_price(signal, size_lots),
            tp_price=self._calculate_tp_price(signal, size_lots),
            order_id=self._generate_order_id(signal),
        )
        
        broker_order_id = self.broker.submit_order(order)
        self.event_log.log_order_submitted(signal, order, broker_order_id)
        self._daily_trades += 1
    
    def _calculate_position_size(self, signal: SignalEvent, account: AccountInfo) -> float:
        """Fixed-fractional sizing: risk R% of equity per trade."""
        risk_amount = account.equity * self.risk_limits.max_risk_per_trade_pct
        # size = risk_amount / (stop_distance_pips * pip_value_per_lot)
        # Requires ATR calculation + pip value lookup
        ...
    
    def check_kill_switches(self) -> None:
        """Called periodically (e.g., every minute) + after every trade close."""
        account = self.broker.get_account_info()
        
        # Update peak equity
        if account.equity > self._peak_equity:
            self._peak_equity = account.equity
        
        # Check drawdown from peak
        drawdown_pct = (self._peak_equity - account.equity) / self._peak_equity
        if drawdown_pct >= self.risk_limits.max_drawdown_pct:
            self.trigger_kill_switch(KillSwitchReason.MAX_DRAWDOWN)
            return
        
        # Check daily loss (reset at session boundary)
        if datetime.now() - self._daily_reset_time > timedelta(days=1):
            self._daily_reset_time = datetime.now()
            self._daily_trades = 0
        
        # Daily loss check would require tracking starting equity
        ...
        
        # Check consecutive losses (tracked via on_trade_close callback)
        if self._consecutive_losses >= self.risk_limits.max_consecutive_losses:
            self.trigger_kill_switch(KillSwitchReason.MAX_CONSECUTIVE_LOSSES)
    
    def trigger_kill_switch(self, reason: KillSwitchReason) -> None:
        """STOP ALL TRADING. Close open positions, reject new signals."""
        if self._kill_switch_active:
            return  # already triggered
        
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        
        # Log the kill-switch event
        self.event_log.log_kill_switch(reason)
        
        # Close all open positions immediately
        account = self.broker.get_account_info()
        for pos in account.open_positions:
            close_order_id = self.broker.close_position(pos.position_id)
            self.event_log.log_emergency_position_close(pos, close_order_id, reason)
        
        # Alert monitoring system
        # (monitoring system polls event log for kill-switch events)
    
    def reset_kill_switch(self, manual_approval: bool = False) -> None:
        """Reset kill-switch (requires manual approval from operator)."""
        if not manual_approval:
            raise PermissionError("Kill-switch reset requires explicit manual approval")
        
        self._kill_switch_active = False
        self._kill_switch_reason = None
        self._consecutive_losses = 0
        self.event_log.log_kill_switch_reset()
```

**Charter compliance:**
- ✅ "AI never bypasses the risk engine" (§1.6)
- ✅ Position sizing is a hard constraint
- ✅ Kill-switches are mandatory
- ✅ All rejections logged in audit trail

---

### 2.5 Order Manager & Execution Abstraction

**Purpose:** Order lifecycle (pending → filled → closed), fill simulation (paper), slippage model.

**Architecture:**

```python
# src/fxlab/execution/order_manager.py

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

@dataclass
class Order:
    """Full order record."""
    order_id: str
    broker_order_id: str | None
    request: OrderRequest
    status: OrderStatus
    fill: OrderFill | None = None
    reject_reason: str | None = None
    submit_time: datetime = field(default_factory=datetime.now)
    fill_time: datetime | None = None

@dataclass
class OrderManager:
    """Track order lifecycle, handle fills."""
    
    broker: BrokerAdapter
    event_log: EventLogger
    
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    
    def submit(self, request: OrderRequest) -> Order:
        """Submit order via broker, track lifecycle."""
        broker_order_id = self.broker.submit_order(request)
        
        order = Order(
            order_id=request.order_id,
            broker_order_id=broker_order_id,
            request=request,
            status=OrderStatus.PENDING,
        )
        
        self._orders[order.order_id] = order
        self.event_log.log_order_submitted(order)
        
        return order
    
    def poll_order_status(self, order_id: str) -> Order:
        """Check order status with broker."""
        order = self._orders[order_id]
        
        broker_status = self.broker.get_order_status(order.broker_order_id)
        
        if broker_status["status"] == "filled":
            order.status = OrderStatus.FILLED
            order.fill = OrderFill(
                order_id=order_id,
                fill_price=broker_status["fill_price"],
                fill_time=broker_status["fill_time"],
                fill_size=broker_status["fill_size"],
                commission=broker_status["commission"],
                slippage_pips=broker_status["slippage_pips"],
            )
            order.fill_time = order.fill.fill_time
            self.event_log.log_order_filled(order)
        
        elif broker_status["status"] == "rejected":
            order.status = OrderStatus.REJECTED
            order.reject_reason = broker_status["reason"]
            self.event_log.log_order_rejected(order)
        
        return order
```

---

### 2.6 Paper Broker Implementation

**Purpose:** Simulated fills for P8 (paper trading), using existing `CostModel`.

**Reuse from fxlab:**
- ✅ `src/fxlab/costs/model.py` — spread, slippage, commission
- ✅ Cost stress testing (+50% already implemented)

**New implementation:**

```python
# src/fxlab/execution/paper_broker.py

from dataclasses import dataclass, field
from datetime import datetime, timezone
import pandas as pd
from ..costs.model import CostModel

@dataclass
class PaperBroker:
    """Simulated broker for paper trading (P8).
    
    Uses CostModel for realistic fills. NO REAL MONEY.
    """
    
    cost_model: CostModel
    initial_balance: float = 10000.0  # paper account starting balance
    
    # Simulated account state
    _balance: float = field(init=False)
    _equity: float = field(init=False)
    _positions: dict[str, Position] = field(default_factory=dict, init=False)
    _orders: dict[str, dict] = field(default_factory=dict, init=False)
    
    # Market data cache (for fill simulation)
    _market_data: dict[str, Tick] = field(default_factory=dict, init=False)
    
    def __post_init__(self):
        self._balance = self.initial_balance
        self._equity = self.initial_balance
    
    def connect(self) -> None:
        """No-op for paper broker."""
        pass
    
    def disconnect(self) -> None:
        """No-op for paper broker."""
        pass
    
    def is_connected(self) -> bool:
        return True
    
    def subscribe_market_data(self, symbols: list[str]) -> None:
        """Initialize market data cache (must be fed externally)."""
        for sym in symbols:
            self._market_data[sym] = None
    
    def update_market_data(self, tick: Tick) -> None:
        """External caller feeds live ticks (from real broker or simulator)."""
        self._market_data[tick.symbol] = tick
        
        # Update unrealized PnL for open positions
        for pos_id, pos in self._positions.items():
            if pos.symbol == tick.symbol:
                current_price = tick.bid if pos.side > 0 else tick.ask
                pos.unrealized_pnl = pos.side * (current_price - pos.entry_price) * pos.size * 100000  # standard lot
    
    def get_latest_tick(self, symbol: str) -> Tick | None:
        return self._market_data.get(symbol)
    
    def get_account_info(self) -> AccountInfo:
        """Current paper account state."""
        return AccountInfo(
            balance=self._balance,
            equity=self._equity,
            margin_used=sum(pos.size * 1000 for pos in self._positions.values()),  # simplified
            margin_available=self._equity - sum(pos.size * 1000 for pos in self._positions.values()),
            open_positions=list(self._positions.values()),
        )
    
    def submit_order(self, order: OrderRequest) -> str:
        """Simulate order submission → instant fill (market orders)."""
        tick = self._market_data.get(order.symbol)
        
        if tick is None:
            # No market data available
            self._orders[order.order_id] = {
                "status": "rejected",
                "reason": "no_market_data",
            }
            return order.order_id
        
        # Simulate fill using CostModel
        if order.order_type == "market":
            # Instant fill at adverse price (bid/ask + slippage)
            if order.side > 0:  # long
                fill_price = tick.ask + self.cost_model.slippage_price(norm_vol=0.0)
            else:  # short
                fill_price = tick.bid - self.cost_model.slippage_price(norm_vol=0.0)
            
            commission = self.cost_model.commission_cost(order.size)
            slippage_pips = self.cost_model.slippage_price(norm_vol=0.0) / self.cost_model.pip_size
            
            # Create position
            position = Position(
                symbol=order.symbol,
                side=order.side,
                size=order.size,
                entry_price=fill_price,
                entry_time=datetime.now(timezone.utc),
                unrealized_pnl=0.0,
                position_id=f"pos_{order.order_id}",
            )
            
            self._positions[position.position_id] = position
            
            # Deduct commission from balance
            self._balance -= commission
            self._equity = self._balance + sum(pos.unrealized_pnl for pos in self._positions.values())
            
            # Record fill
            self._orders[order.order_id] = {
                "status": "filled",
                "fill_price": fill_price,
                "fill_time": datetime.now(timezone.utc),
                "fill_size": order.size,
                "commission": commission,
                "slippage_pips": slippage_pips,
            }
            
            return order.order_id
        
        else:
            # Limit/stop orders not yet implemented
            self._orders[order.order_id] = {
                "status": "rejected",
                "reason": "order_type_not_supported",
            }
            return order.order_id
    
    def get_order_status(self, order_id: str) -> dict:
        return self._orders.get(order_id, {"status": "unknown"})
    
    def close_position(self, position_id: str) -> str:
        """Close position at current market price."""
        pos = self._positions.get(position_id)
        if pos is None:
            return None
        
        tick = self._market_data.get(pos.symbol)
        if tick is None:
            return None
        
        # Exit fill (adverse)
        if pos.side > 0:  # long → sell at bid
            exit_price = tick.bid - self.cost_model.slippage_price(norm_vol=0.0)
        else:  # short → buy at ask
            exit_price = tick.ask + self.cost_model.slippage_price(norm_vol=0.0)
        
        # Realized PnL
        price_pnl = pos.side * (exit_price - pos.entry_price) * pos.size * 100000
        commission = self.cost_model.commission_cost(pos.size)
        realized_pnl = price_pnl - commission
        
        # Update balance
        self._balance += realized_pnl
        del self._positions[position_id]
        self._equity = self._balance + sum(p.unrealized_pnl for p in self._positions.values())
        
        # Generate close order_id
        close_order_id = f"close_{position_id}"
        self._orders[close_order_id] = {
            "status": "filled",
            "fill_price": exit_price,
            "fill_time": datetime.now(timezone.utc),
            "fill_size": pos.size,
            "commission": commission,
            "realized_pnl": realized_pnl,
        }
        
        return close_order_id
    
    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Fetch from real data store (reuse fxlab data pipeline)."""
        from ..data.store import load_bars
        from ..config import load_config
        
        cfg = load_config()
        bars = load_bars(cfg.data_dir, symbol, tf, stage="processed")
        return bars.tail(count)
```

**Key properties:**
- ✅ Reuses existing `CostModel` for realistic fills
- ✅ No real money, no real orders
- ✅ Simulates adverse fills (bid/ask + slippage)
- ✅ Tracks paper account balance/equity/positions
- ✅ Instant fills for market orders (no queue simulation yet)

---

### 2.7 Position Reconciliation

**Purpose:** Detect discrepancies between internal state and broker state; trigger kill-switch on mismatch.

**Architecture:**

```python
# src/fxlab/execution/reconciliation.py

from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class ReconciliationEngine:
    """Sync internal position state with broker."""
    
    broker: BrokerAdapter
    risk_engine: RiskEngine
    event_log: EventLogger
    
    reconcile_interval_seconds: float = 60.0  # check every minute
    max_discrepancy_tolerance_usd: float = 1.0  # acceptable rounding error
    
    def start(self) -> None:
        """Start periodic reconciliation loop."""
        ...
    
    def reconcile(self) -> bool:
        """Compare internal state vs broker; return True if OK."""
        account = self.broker.get_account_info()
        
        # Check position count
        internal_positions = self._get_internal_positions()
        
        if len(internal_positions) != len(account.open_positions):
            self.event_log.log_reconciliation_failed(
                reason="position_count_mismatch",
                internal_count=len(internal_positions),
                broker_count=len(account.open_positions),
            )
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
            )
            return False
        
        # Check each position (symbol, side, size)
        for internal_pos in internal_positions:
            broker_pos = self._find_broker_position(account.open_positions, internal_pos)
            
            if broker_pos is None:
                self.event_log.log_reconciliation_failed(
                    reason="position_not_found_at_broker",
                    internal_position=internal_pos,
                )
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                return False
            
            # Check size match
            if abs(broker_pos.size - internal_pos.size) > 0.01:
                self.event_log.log_reconciliation_failed(
                    reason="position_size_mismatch",
                    internal_size=internal_pos.size,
                    broker_size=broker_pos.size,
                )
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                return False
        
        # All checks passed
        return True
    
    def _get_internal_positions(self) -> list[Position]:
        """Query internal position tracker."""
        ...
    
    def _find_broker_position(self, broker_positions: list[Position], internal_pos: Position) -> Position | None:
        """Match internal position to broker position by symbol + side."""
        for bp in broker_positions:
            if bp.symbol == internal_pos.symbol and bp.side == internal_pos.side:
                return bp
        return None
```

**Key properties:**
- ✅ Periodic sync check (every minute)
- ✅ Triggers kill-switch on mismatch
- ✅ Logs all discrepancies to audit trail
- ✅ Conservative: assume broker is source of truth

---

### 2.8 Trade & Event Logging

**Purpose:** Append-only audit trail of every decision (signal, order, fill, risk event, kill-switch).

**Architecture:**

```python
# src/fxlab/execution/event_log.py

from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timezone
import json

@dataclass
class EventLogger:
    """Append-only audit trail for trading events."""
    
    log_dir: Path
    
    def __post_init__(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_dir / f"events_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
    
    def _write(self, event_type: str, data: dict) -> None:
        """Write event to append-only log."""
        entry = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **data,
        }
        with self._log_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_signal_generated(self, signal: SignalEvent) -> None:
        self._write("signal_generated", asdict(signal))
    
    def log_signal_rejected(self, signal: SignalEvent, reason: str) -> None:
        self._write("signal_rejected", {"signal": asdict(signal), "reason": reason})
    
    def log_order_submitted(self, order: Order) -> None:
        self._write("order_submitted", asdict(order))
    
    def log_order_filled(self, order: Order) -> None:
        self._write("order_filled", asdict(order))
    
    def log_order_rejected(self, order: Order) -> None:
        self._write("order_rejected", asdict(order))
    
    def log_position_opened(self, position: Position) -> None:
        self._write("position_opened", asdict(position))
    
    def log_position_closed(self, position: Position, realized_pnl: float) -> None:
        self._write("position_closed", {**asdict(position), "realized_pnl": realized_pnl})
    
    def log_kill_switch(self, reason: KillSwitchReason) -> None:
        self._write("kill_switch_triggered", {"reason": reason.value})
    
    def log_kill_switch_reset(self) -> None:
        self._write("kill_switch_reset", {})
    
    def log_emergency_position_close(self, position: Position, close_order_id: str, reason: KillSwitchReason) -> None:
        self._write("emergency_position_close", {
            "position": asdict(position),
            "close_order_id": close_order_id,
            "reason": reason.value,
        })
    
    def log_reconciliation_failed(self, reason: str, **details) -> None:
        self._write("reconciliation_failed", {"reason": reason, **details})
    
    def log_risk_limit_breach(self, limit_name: str, current_value: float, limit_value: float) -> None:
        self._write("risk_limit_breach", {
            "limit_name": limit_name,
            "current_value": current_value,
            "limit_value": limit_value,
        })
```

**Key properties:**
- ✅ Append-only (never modified)
- ✅ One JSONL file per session
- ✅ Every event timestamped (UTC)
- ✅ Used for audit trail, post-trade analysis, monitoring alerts

---

### 2.9 Monitoring & Alerting Dashboard

**Purpose:** Real-time metrics (PnL, Sharpe, DD), breach alerts, health checks.

**Architecture:**

```python
# src/fxlab/monitoring/dashboard.py

from dataclasses import dataclass
from datetime import datetime, timedelta
import pandas as pd

@dataclass
class MonitoringDashboard:
    """Real-time trading metrics + alerts."""
    
    broker: BrokerAdapter
    event_log: EventLogger
    risk_engine: RiskEngine
    
    alert_channels: list[str] = field(default_factory=list)  # ["email", "slack", "telegram"]
    
    def get_current_metrics(self) -> dict:
        """Real-time account metrics."""
        account = self.broker.get_account_info()
        
        # Read recent trade history from event log
        trades = self._load_recent_trades(days=30)
        
        if len(trades) == 0:
            return {
                "balance": account.balance,
                "equity": account.equity,
                "unrealized_pnl": sum(pos.unrealized_pnl for pos in account.open_positions),
                "open_positions": len(account.open_positions),
                "daily_return_pct": 0.0,
                "sharpe_30d": None,
                "max_drawdown_pct": 0.0,
                "win_rate": None,
                "trades_today": 0,
            }
        
        # Calculate metrics
        daily_returns = self._calculate_daily_returns(trades, account.balance)
        
        return {
            "balance": account.balance,
            "equity": account.equity,
            "unrealized_pnl": sum(pos.unrealized_pnl for pos in account.open_positions),
            "open_positions": len(account.open_positions),
            "daily_return_pct": daily_returns[-1] if len(daily_returns) > 0 else 0.0,
            "sharpe_30d": self._calculate_sharpe(daily_returns),
            "max_drawdown_pct": self._calculate_max_drawdown(account.balance, trades),
            "win_rate": sum(1 for t in trades if t["realized_pnl"] > 0) / len(trades),
            "trades_today": self._count_trades_today(trades),
        }
    
    def check_for_alerts(self) -> list[dict]:
        """Scan event log for alert-worthy events."""
        alerts = []
        
        # Check for kill-switch events
        recent_events = self._load_recent_events(minutes=5)
        
        for event in recent_events:
            if event["event_type"] == "kill_switch_triggered":
                alerts.append({
                    "severity": "CRITICAL",
                    "type": "kill_switch",
                    "reason": event["reason"],
                    "timestamp": event["ts_utc"],
                })
            
            elif event["event_type"] == "risk_limit_breach":
                alerts.append({
                    "severity": "HIGH",
                    "type": "risk_limit_breach",
                    "limit_name": event["limit_name"],
                    "current_value": event["current_value"],
                    "limit_value": event["limit_value"],
                    "timestamp": event["ts_utc"],
                })
            
            elif event["event_type"] == "reconciliation_failed":
                alerts.append({
                    "severity": "CRITICAL",
                    "type": "reconciliation_failed",
                    "reason": event["reason"],
                    "timestamp": event["ts_utc"],
                })
        
        return alerts
    
    def send_alert(self, alert: dict) -> None:
        """Send alert to configured channels."""
        for channel in self.alert_channels:
            if channel == "email":
                self._send_email_alert(alert)
            elif channel == "slack":
                self._send_slack_alert(alert)
            # etc.
    
    def generate_health_report(self) -> dict:
        """System health checks."""
        return {
            "broker_connected": self.broker.is_connected(),
            "kill_switch_active": self.risk_engine._kill_switch_active,
            "last_reconciliation": self._get_last_reconciliation_time(),
            "event_log_writable": self._check_event_log_writable(),
            "market_data_fresh": self._check_market_data_freshness(),
        }
```

**Dashboard UI (future, not in this design):**
- Streamlit or web UI (HTML/JS)
- Real-time charts (equity curve, drawdown, PnL distribution)
- Alert panel (breaches, kill-switch, reconciliation failures)
- Position table (open positions, unrealized PnL)
- Trade history table (recent fills, win/loss)
- Health status indicators (broker connection, data freshness, log write)

---

## 3. What Can Be Reused from fxlab

| Component | Reuse | Notes |
|-----------|-------|-------|
| **CostModel** (`src/fxlab/costs/model.py`) | ✅ **100% reuse** | Paper broker uses it for simulated fills; stress testing (+50%) already implemented |
| **Data pipeline** (`src/fxlab/data/`) | ✅ **Reuse for historical bars** | `load_bars()` provides warm-up data (e.g., ATR window); real-time ingestion is new |
| **Setup interface** (`src/fxlab/setups/base.py`) | ✅ **Reuse as-is** | `Setup.generate()` is the frozen research code; signal engine calls it on closed bars |
| **Config** (`src/fxlab/config.py`) | ✅ **Extend** | Add execution config (broker, risk limits, monitoring); existing config unchanged |
| **Backtest engine** (`src/fxlab/backtest/engine.py`) | 🔶 **Partial reuse** | Event-driven, closed-candle logic is sound; paper trading needs live adaptation |
| **Triple-barrier labeling** (`src/fxlab/labeling/triple_barrier.py`) | ✅ **Reuse for SL/TP calculation** | Same ATR-based SL/TP used in live as in backtest |
| **Experiment logging** (`src/fxlab/experiment/log.py`) | 🔶 **Parallel, not reuse** | Research experiments stay separate; execution uses `EventLogger` (new) |

**Summary:**
- ✅ **Heavy reuse:** Cost model, data pipeline, setup interface, config, triple-barrier
- 🆕 **New implementations:** Broker adapter, signal engine, risk engine, order manager, reconciliation, event logger, monitoring

---

## 4. What Must Be Built from Scratch

| Component | New | Reason |
|-----------|-----|--------|
| **BrokerAdapter protocol** | ✅ | No broker integration exists in fxlab (research-only) |
| **PaperBroker implementation** | ✅ | Simulated fills for P8 (paper trading) |
| **MarketDataStream** | ✅ | Real-time tick ingestion doesn't exist (backtest uses static bars) |
| **SignalEngine** | ✅ | Bridge between `Setup.generate()` (research) and execution (new) |
| **RiskEngine** | ✅ | Position sizing, limits, kill-switches per charter (stubs only in fxlab) |
| **OrderManager** | ✅ | Order lifecycle tracking doesn't exist (backtest simulates instantly) |
| **ReconciliationEngine** | ✅ | No broker sync in research platform |
| **EventLogger** (execution) | ✅ | Separate from research experiment log (different purpose) |
| **MonitoringDashboard** | ✅ | Real-time metrics + alerts don't exist (backtest is offline) |

---

## 5. Implementation Phases

### Phase 0: Architecture Review (this document)
- ✅ Design paper-trading architecture
- ✅ Identify reuse vs build-from-scratch
- ⏳ **Next:** Get user approval before implementing

### Phase 1: Core Abstractions (foundation)
**Estimated:** 2-3 days

1. **BrokerAdapter protocol** (`src/fxlab/execution/broker.py`)
   - Define protocol (dataclasses + Protocol)
   - Unit tests for protocol compliance

2. **EventLogger** (`src/fxlab/execution/event_log.py`)
   - Append-only JSONL writer
   - Event types (signal, order, fill, risk, kill-switch)
   - Unit tests

3. **Config extension** (`config/execution.yaml`)
   - Broker settings (paper vs live)
   - Risk limits
   - Monitoring settings

### Phase 2: Paper Broker (simulation)
**Estimated:** 3-4 days

1. **PaperBroker implementation** (`src/fxlab/execution/paper_broker.py`)
   - Simulated account (balance, equity, positions)
   - Fill simulation using `CostModel`
   - Market data cache (fed externally)
   - Unit tests (50+ tests for fills, PnL, costs)

2. **MarketDataStream stub** (`src/fxlab/execution/market_data.py`)
   - Tick buffer (thread-safe)
   - Closed-bar aggregation
   - Synthetic tick generator for testing

### Phase 3: Risk Engine
**Estimated:** 4-5 days

1. **RiskEngine implementation** (`src/fxlab/risk/engine.py`)
   - Position sizing (fixed-fractional)
   - Hard limits (max risk, max DD, max consecutive losses)
   - Kill-switch logic
   - Unit tests (30+ tests for all limits + kill-switch scenarios)

2. **Position reconciliation** (`src/fxlab/execution/reconciliation.py`)
   - Periodic sync check
   - Discrepancy detection
   - Kill-switch trigger on mismatch
   - Unit tests

### Phase 4: Signal Engine & Order Manager
**Estimated:** 3-4 days

1. **SignalEngine** (`src/fxlab/execution/signal_engine.py`)
   - Periodic `Setup.generate()` invocation
   - Closed-bar discipline
   - Signal validation
   - Unit tests (integration with frozen research setups)

2. **OrderManager** (`src/fxlab/execution/order_manager.py`)
   - Order lifecycle (pending → filled → closed)
   - Fill tracking
   - Unit tests

### Phase 5: Monitoring & CLI
**Estimated:** 2-3 days

1. **MonitoringDashboard** (`src/fxlab/monitoring/dashboard.py`)
   - Real-time metrics calculator
   - Alert scanner (event log)
   - Health checks
   - Unit tests

2. **CLI extension** (`src/fxlab/cli.py`)
   - `fxlab paper-trade --setup <name> --symbols <list> --timeframe <tf>`
   - `fxlab monitor --session-id <id>`
   - `fxlab reset-kill-switch --session-id <id> --approve`

### Phase 6: Integration Testing
**Estimated:** 3-5 days

1. **End-to-end paper-trading test**
   - Synthetic tick generator → SignalEngine → RiskEngine → PaperBroker → EventLogger
   - Run full session (100+ simulated bars)
   - Verify all components interact correctly

2. **Stress tests**
   - Kill-switch scenarios (max DD, consecutive losses, reconciliation failure)
   - Rapid signals (multiple per bar)
   - Position reconciliation failures

3. **Leakage verification**
   - Ensure closed-candle discipline in live mode
   - Append future ticks → signals must not change (future-invariance)

### Phase 7: Documentation & Handoff
**Estimated:** 1-2 days

1. **User manual**
   - How to run paper trading
   - How to configure risk limits
   - How to monitor sessions
   - How to reset kill-switches

2. **Operator runbook**
   - Paper trading checklist
   - Kill-switch procedures
   - Reconciliation failure procedures
   - Alert response playbook

---

## 6. Testing Strategy

### Unit Tests (per component)
- **PaperBroker:** 50+ tests (fills, PnL, costs, positions)
- **RiskEngine:** 30+ tests (sizing, limits, kill-switches)
- **SignalEngine:** 20+ tests (closed-bar discipline, signal routing)
- **ReconciliationEngine:** 15+ tests (sync check, discrepancy detection)
- **EventLogger:** 10+ tests (append-only, event types)
- **MonitoringDashboard:** 15+ tests (metrics, alerts, health checks)

**Total:** ~140 new unit tests (targeting 100% coverage of execution layer)

### Integration Tests
- End-to-end paper trading (synthetic ticks → orders → fills → PnL)
- Kill-switch scenarios (all triggers)
- Reconciliation failure scenarios
- Rapid signal handling (queue stress)

### Leakage Tests
- Future-invariance regression (append future ticks → signals unchanged)
- Closed-candle discipline (signal engine never sees forming bar)

### Stress Tests
- High-frequency signals (multiple per second)
- Position reconciliation under load
- Event log write under load

---

## 7. Deployment & Operations

### Paper Trading Checklist (P8)

**Pre-launch:**
1. ✅ All unit tests pass (140+ execution layer tests)
2. ✅ Integration tests pass (end-to-end scenarios)
3. ✅ Leakage tests pass (future-invariance verified)
4. ✅ Risk limits configured in `config/execution.yaml`
5. ✅ Kill-switches enabled
6. ✅ Reconciliation enabled (60s interval)
7. ✅ Monitoring dashboard running
8. ✅ Alert channels configured (email/Slack)
9. ✅ Event log directory writable
10. ✅ Paper broker initialized (initial balance set)

**Launch:**
```bash
$ fxlab paper-trade \
    --setup model_b_trend_pullback \
    --symbols EURUSD,GBPUSD \
    --timeframe H1 \
    --session-name "p8_baseline_test_001"

[paper] session p8_baseline_test_001 started
[paper] initial balance: $10,000.00
[paper] risk limits: 1% per trade, 3% daily loss, 10% max DD
[paper] kill-switches: enabled
[paper] reconciliation: 60s interval
[paper] monitoring: http://localhost:8501
```

**During session:**
- Monitor dashboard for metrics (PnL, Sharpe, DD)
- Watch for alerts (breaches, kill-switch)
- Check event log for anomalies
- Verify reconciliation passes every 60s

**Post-session:**
- Generate session report (metrics, trades, events)
- Compare paper PnL vs backtest expectancy (should be within statistical tolerance)
- Review kill-switch events (if any)
- Archive event log

### Kill-Switch Procedures

**If kill-switch triggers:**
1. **Alert fires** (email/Slack: "CRITICAL: kill-switch triggered [reason]")
2. **All positions closed** (emergency close at market)
3. **New signals rejected** (system stops trading)
4. **Operator investigates** (review event log, reconciliation status, metrics)
5. **Root cause identified** (e.g., max DD breached, reconciliation failed)
6. **Fix applied** (if reconciliation: fix broker sync; if DD: review risk limits)
7. **Manual reset** (operator approval required):
   ```bash
   $ fxlab reset-kill-switch --session-id p8_baseline_test_001 --approve
   ```
8. **Resume trading** (or terminate session)

### Reconciliation Failure Procedures

**If reconciliation fails:**
1. **Kill-switch triggers** (reason: `POSITION_RECONCILIATION_FAILED`)
2. **Operator reviews event log:**
   ```bash
   $ grep reconciliation_failed execution/events/*.jsonl
   ```
3. **Identify discrepancy** (position count mismatch, size mismatch, symbol mismatch)
4. **Check broker state manually** (broker web UI / API)
5. **Sync internal state** (if broker is source of truth: update internal tracker)
6. **Re-run reconciliation** (verify fix)
7. **Reset kill-switch** (if fix confirmed)

---

## 8. Future Extensions (Out of Scope)

### Live Broker Integration (P9)
- OANDA v20 API adapter
- MetaTrader5 adapter
- Real order submission
- Real fills (not simulated)
- **Requires explicit approval, never enabled by default**

### ML Filter Integration (P5, only if P4 GO)
- Calibrated P(TP before SL) filter on signals
- Signal confidence scoring
- Filter-vs-no-filter A/B testing in paper

### Multi-Strategy Portfolio
- Multiple setups running concurrently
- Correlation-aware position sizing
- Portfolio-level risk limits

### Advanced Monitoring
- Streamlit dashboard (web UI)
- Real-time equity chart
- PnL distribution histogram
- Trade heatmap (by time-of-day, day-of-week)

### Advanced Risk Engine
- Dynamic position sizing (Kelly, conditional-progressive)
- Volatility-regime-aware sizing
- Correlation-adjusted exposure limits

---

## 9. Open Questions (For Review)

1. **Tick data source for paper trading:**
   - Option A: Synthetic tick generator (deterministic, offline)
   - Option B: Real tick stream from broker (requires broker integration first)
   - **Recommendation:** Start with Option A (synthetic) for P8 paper testing

2. **Signal engine polling interval:**
   - 1 second (high-frequency, CPU-intensive)
   - 5 seconds (reasonable for H1+ timeframes)
   - **Recommendation:** 5 seconds, configurable

3. **Kill-switch reset:**
   - Manual approval required (conservative)
   - Auto-reset after N minutes (risky)
   - **Recommendation:** Manual only (charter safety-first principle)

4. **Paper trading initial balance:**
   - $10,000 (realistic for retail)
   - $100,000 (realistic for institutional)
   - **Recommendation:** Configurable, default $10,000

5. **Event log rotation:**
   - One file per session (simple, auditable)
   - Daily rotation (scalable, complex)
   - **Recommendation:** One file per session

---

## 10. Success Criteria

**Phase completion gates:**

| Phase | Gate | Criteria |
|-------|------|----------|
| Phase 1 | Core abstractions | 15+ tests pass; protocols defined |
| Phase 2 | Paper broker | 50+ tests pass; simulated fills realistic vs `CostModel` |
| Phase 3 | Risk engine | 30+ tests pass; all kill-switch scenarios verified |
| Phase 4 | Signal + Order | 20+ tests pass; integration with frozen setups works |
| Phase 5 | Monitoring | 15+ tests pass; metrics calculator + alerts functional |
| Phase 6 | Integration | End-to-end paper session runs for 100+ bars without errors |
| Phase 7 | Documentation | User manual + operator runbook complete |

**Final gate (before P8 paper launch):**
- ✅ All 140+ unit tests pass
- ✅ Integration tests pass (end-to-end scenarios)
- ✅ Leakage tests pass (future-invariance verified)
- ✅ Paper trading checklist complete
- ✅ Operator trained on kill-switch procedures
- ✅ Monitoring dashboard operational
- ✅ Event log verified (append-only, all event types)

---

## 11. Constraints & Non-Goals

**Hard constraints:**
- ✅ No new trading strategies (Models A–G frozen)
- ✅ 2024+ test window stays sealed
- ✅ No modification of P4 acceptance criteria
- ✅ No live money until P9 (explicit approval)
- ✅ Current research result: P4 NO-GO (no validated strategy)

**Non-goals for this phase:**
- ❌ Live broker integration (P9, future)
- ❌ ML filter (P5, blocked by P4 NO-GO)
- ❌ Multi-strategy portfolio (future)
- ❌ Web dashboard UI (future, Streamlit stub only)
- ❌ Advanced risk engine features (Kelly, regime-aware, future)

**What this architecture provides:**
- ✅ Paper-trading infrastructure (P8-ready)
- ✅ Execution layer decoupled from research (clean separation)
- ✅ Risk engine (charter-compliant: AI never bypasses)
- ✅ Audit trail (append-only event log)
- ✅ Monitoring + alerts (real-time metrics)
- ✅ Foundation for future live integration (abstract broker adapter)

---

## 12. Next Steps

**Awaiting user approval:**
1. Review this architecture document
2. Confirm design decisions (open questions in §9)
3. Approve implementation phases (§5)
4. Green-light Phase 1 start (core abstractions)

**Post-approval:**
- Create implementation task list (Phases 1–7)
- Begin Phase 1 (BrokerAdapter protocol + EventLogger + config)
- Target: ~20 days for full paper-trading infrastructure (Phases 1–7)

---

**Document status:** DRAFT (awaiting review)  
**Author:** fxlab engineering  
**Date:** 2026-08-25  
**Version:** 1.0
