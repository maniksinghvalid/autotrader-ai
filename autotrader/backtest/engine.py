"""Bar-walking backtest simulator. Drives the PRODUCTION strategy/exit/sizing
functions (autotrader.strategies.breakout, .exits, autotrader.sizing) against
pre-fetched historical bars — no SDK, no broker, fully offline and
deterministic. See docs/BACKTESTING.md for the numbered intrabar fill rules
this module implements (each is cited by number in the code below).

No look-ahead: every decision uses only bars strictly before the current one
(the rolling reference high, the previous close used for sizing) plus the
CURRENT bar's own OHLC as trigger levels — standard stop-order semantics."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from autotrader.backtest.data import Bar
from autotrader.config import RiskConfig
from autotrader.domain import Position
from autotrader.sizing import size_position
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
from autotrader.strategies.pullback import PullbackParams, PullbackStrategy

_ET = ZoneInfo("America/New_York")
_ENTRY_OPEN_T = dtime(9, 45)
_ENTRY_CLOSE_T = dtime(15, 30)
_UNLIMITED = 1_000_000_000.0


@dataclass(frozen=True)
class BacktestConfig:
    symbols: Tuple[str, ...]
    start: date
    end: date
    multiplier: int
    timespan: str            # Massive vocabulary: "day" | "minute" | "hour" | ...
    cash: float
    commission_per_order: float = 0.0
    commission_per_share: float = 0.0
    slippage_bps: float = 0.0
    lookback: int = 20
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.10
    trailing_stop_pct: float = 0.0   # PERCENT (5.0 = 5%), matches RISK_TRAILING_STOP_PCT; 0 = off
    confidence: float = 0.7
    order_qty: int = 1
    # Strategy variant selection (defaults keep every existing call site on the
    # plain breakout). regime_sma gates entries above an R-day SMA for the two
    # gated variants; pullback_* configure the mean-reversion entry (Rules 9-12).
    strategy: str = "breakout"      # "breakout" | "breakout_regime" | "pullback"
    regime_sma: int = 200
    pullback_lookback: int = 10
    pullback_depth_pct: float = 0.0   # 0 => trigger = min of last M lows; else SMA*(1-depth)


@dataclass(frozen=True)
class Fill:
    """Backtest-local fill record. domain.Fill is broker-shaped (fill_id,
    ISO ts, client_order_id) — not what a backtest ledger needs."""
    ts: int
    symbol: str
    side: str
    qty: int
    price: float
    commission: float
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    fills: Tuple[Fill, ...]
    equity_curve: Tuple[Tuple[date, float, float], ...]   # (day, total_value, cash)
    skipped_for_cash: int
    cfg: BacktestConfig
    held_days: Dict[str, int] = field(default_factory=dict)   # symbol -> # equity-curve days held (exposure)


def default_risk_config(cfg: BacktestConfig) -> RiskConfig:
    """Permissive default used when the caller doesn't supply one (e.g. tests).
    The CLI passes the real load_risk_config() instead so a sourced
    config/risk.config gives config parity with production caps."""
    return RiskConfig(
        trading_env="PAPER",
        min_confidence=0.0,
        max_order_notional=_UNLIMITED,
        max_position_qty=1_000_000_000,
        daily_loss_limit=_UNLIMITED,
        max_gross_exposure=_UNLIMITED,
        allowed_symbols=frozenset(cfg.symbols),
        trailing_stop_pct=cfg.trailing_stop_pct,
        risk_per_trade_pct=0.0,
        daily_loss_halt=_UNLIMITED,
    )


def _in_entry_window(ts: int) -> bool:
    """Production EntryGate parity: entries only 09:45-15:30 ET. Approximated
    as a fixed clock window (production derives it from scheduler events, but
    those fire at these exact times)."""
    t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(_ET).time()
    return _ENTRY_OPEN_T <= t < _ENTRY_CLOSE_T


class _SymState:
    __slots__ = ("current_day", "forming_high", "forming_low", "forming_close",
                 "daily_highs", "daily_lows", "daily_closes", "hwm", "hwm_day")

    def __init__(self, lookback: int, pullback_lookback: int, regime_sma: int):
        self.current_day: Optional[date] = None
        self.forming_high: Optional[float] = None
        self.forming_low: Optional[float] = None
        self.forming_close: Optional[float] = None
        self.daily_highs: deque = deque(maxlen=lookback)
        self.daily_lows: deque = deque(maxlen=pullback_lookback)
        self.daily_closes: deque = deque(maxlen=regime_sma)
        self.hwm: Optional[float] = None
        self.hwm_day: Optional[date] = None

    def roll_day_if_needed(self, day: date) -> None:
        if self.current_day is None:
            self.current_day = day
            return
        if day != self.current_day:
            if self.forming_high is not None:
                self.daily_highs.append(self.forming_high)
            if self.forming_low is not None:
                self.daily_lows.append(self.forming_low)
            if self.forming_close is not None:
                self.daily_closes.append(self.forming_close)
            self.forming_high = None
            self.forming_low = None
            self.forming_close = None
            self.current_day = day

    def update_forming(self, bar: Bar) -> None:
        self.forming_high = bar.high if self.forming_high is None else max(self.forming_high, bar.high)
        self.forming_low = bar.low if self.forming_low is None else min(self.forming_low, bar.low)
        self.forming_close = bar.close

    def ref_high(self, lookback: int) -> Optional[float]:
        if len(self.daily_highs) < lookback:
            return None
        return max(self.daily_highs)

    def ref_low(self, m: int) -> Optional[float]:
        if len(self.daily_lows) < m:
            return None
        return min(self.daily_lows)

    def sma(self, r: int) -> Optional[float]:
        if len(self.daily_closes) < r:
            return None
        return sum(self.daily_closes) / r


class _Book:
    """Portfolio ledger: cash, per-symbol weighted-avg-cost position, fills."""

    def __init__(self, cash: float):
        self.cash = cash
        self.positions: Dict[str, Tuple[int, float]] = {}
        self.fills: List[Fill] = []
        self.skipped_for_cash = 0

    def qty(self, symbol: str) -> int:
        return self.positions.get(symbol, (0, 0.0))[0]

    def avg(self, symbol: str) -> float:
        return self.positions.get(symbol, (0, 0.0))[1]

    def fill(self, symbol: str, side: str, qty: int, raw_price: float, ts: int,
             reason: str, slip: float, commission_per_order: float,
             commission_per_share: float) -> float:
        px = raw_price * (1 + slip) if side == "BUY" else raw_price * (1 - slip)
        commission = commission_per_order + commission_per_share * qty
        old_qty, old_avg = self.positions.get(symbol, (0, 0.0))
        if side == "BUY":
            self.cash -= qty * px + commission
            new_qty = old_qty + qty
            new_avg = (old_qty * old_avg + qty * px) / new_qty
            self.positions[symbol] = (new_qty, new_avg)
        else:
            self.cash += qty * px - commission
            new_qty = old_qty - qty
            self.positions[symbol] = (new_qty, old_avg if new_qty > 0 else 0.0)
        self.fills.append(Fill(ts=ts, symbol=symbol, side=side, qty=qty,
                               price=px, commission=commission, reason=reason))
        return px


def _make_strategy(cfg: "BacktestConfig", symbol: str):
    """The exit path is shared (manage_long_exit) so both classes behave the
    same when held; the class choice matters for the Rule-1 open-exit call and
    for the entry-parity contract with the engine triggers."""
    if cfg.strategy in ("breakout", "breakout_regime"):
        return BreakoutStrategy(BreakoutParams(
            symbol=symbol, stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct, confidence=cfg.confidence))
    return PullbackStrategy(PullbackParams(
        symbol=symbol, stop_loss_pct=cfg.stop_loss_pct,
        take_profit_pct=cfg.take_profit_pct, confidence=cfg.confidence))


def _reason_from_rationale(rationale: str) -> str:
    if rationale.startswith("stop-loss"):
        return "stop-loss"
    if rationale.startswith("take-profit"):
        return "take-profit"
    return "exit"


def run_backtest(bars_by_symbol: Dict[str, List[Bar]], cfg: BacktestConfig,
                 risk_cfg: Optional[RiskConfig] = None) -> BacktestResult:
    risk_cfg = risk_cfg if risk_cfg is not None else default_risk_config(cfg)
    if cfg.strategy not in ("breakout", "breakout_regime", "pullback"):
        raise ValueError(f"unknown strategy {cfg.strategy!r} "
                         "(expected breakout | breakout_regime | pullback)")
    slip = cfg.slippage_bps / 1e4
    trail = cfg.trailing_stop_pct / 100.0
    daily_interval = cfg.timespan == "day"

    strategies = {s: _make_strategy(cfg, s) for s in cfg.symbols}
    bars = {s: sorted((b for b in lst if b.day() <= cfg.end), key=lambda b: b.ts)
           for s, lst in bars_by_symbol.items()}
    bar_index = {s: {b.ts: b for b in lst} for s, lst in bars.items()}
    all_ts = sorted({b.ts for lst in bars.values() for b in lst})

    ts_active: Dict[int, List[str]] = {}
    ts_day: Dict[int, date] = {}
    for ts in all_ts:
        active = sorted(s for s in bars if ts in bar_index[s])
        ts_active[ts] = active
        ts_day[ts] = bar_index[active[0]][ts].day()
    day_end = {}
    for i, ts in enumerate(all_ts):
        day_end[ts] = (i == len(all_ts) - 1) or (ts_day[all_ts[i + 1]] != ts_day[ts])

    state = {s: _SymState(cfg.lookback, cfg.pullback_lookback, cfg.regime_sma)
            for s in cfg.symbols}
    book = _Book(cfg.cash)
    last_close: Dict[str, float] = {}
    equity_curve: List[Tuple[date, float, float]] = []
    held_days: Dict[str, int] = {s: 0 for s in cfg.symbols}

    def do_fill(symbol, side, qty, raw_price, ts, reason):
        return book.fill(symbol, side, qty, raw_price, ts, reason, slip,
                         cfg.commission_per_order, cfg.commission_per_share)

    def process_exit(symbol: str, bar: Bar, exited: set) -> None:
        qty = book.qty(symbol)
        if qty <= 0:
            return
        st = state[symbol]
        avg = book.avg(symbol)
        o, h, l, c = bar.open, bar.high, bar.low, bar.close

        # Rule 1: open-price exit (gap handling) — production SL-before-TP
        # ordering (exits.py) covers a gap that breaches both. Keyword defaults
        # only: PullbackStrategy has no ref_high param, and the held branch
        # never reads the entry references anyway.
        sig = strategies[symbol].evaluate(o, Position(symbol, qty, avg))
        if sig is not None and sig.direction == "SELL":
            do_fill(symbol, "SELL", qty, o, bar.ts, _reason_from_rationale(sig.rationale))
            exited.add(symbol)
            return

        # Rule 2: intrabar downside stops (fixed stop-loss + trailing).
        triggers: List[Tuple[float, str]] = []
        p_sl = avg * (1 - cfg.stop_loss_pct)
        if l <= p_sl:
            triggers.append((p_sl, "stop-loss"))
        if trail > 0:
            if daily_interval:
                st.hwm = o
                st.hwm_day = bar.day()
                p_tr = o * (1 - trail)
                if l <= p_tr:
                    triggers.append((p_tr, "trailing-stop"))
            else:
                today = bar.day()
                if st.hwm_day != today or st.hwm is None:
                    st.hwm = o
                    st.hwm_day = today
                else:
                    st.hwm = max(st.hwm, o)
                p_tr = st.hwm * (1 - trail)
                if l <= p_tr:
                    triggers.append((p_tr, "trailing-stop"))
                st.hwm = max(st.hwm, h)   # ratchet AFTER the fire check (pessimistic)
        if triggers:
            level, reason = max(triggers, key=lambda x: x[0])
            do_fill(symbol, "SELL", qty, level, bar.ts, reason)
            exited.add(symbol)
            return

        # Rule 3: daily-interval trailing branch (reconstructs rose-then-fell;
        # without it, the daily HWM reseed means trailing could never fire off
        # an intraday peak at 1d resolution).
        if daily_interval and trail > 0:
            level = h * (1 - trail)
            if c <= level:
                do_fill(symbol, "SELL", qty, level, bar.ts, "trailing-stop")
                exited.add(symbol)
                return

        # Rule 4: intrabar take-profit. Rules 2/3 run first => stop-loss wins
        # when both are reachable inside one bar (pessimistic).
        p_tp = avg * (1 + cfg.take_profit_pct)
        if h >= p_tp:
            do_fill(symbol, "SELL", qty, p_tp, bar.ts, "take-profit")
            exited.add(symbol)

    def regime_ok(st: _SymState) -> bool:
        """Rule 10: previous completed day's close above the R-day SMA of
        completed closes. The current bar is never consulted."""
        sma = st.sma(cfg.regime_sma)
        prev_close_completed = st.daily_closes[-1] if st.daily_closes else None
        return (sma is not None and prev_close_completed is not None
                and prev_close_completed > sma)

    def entry_trigger(symbol: str, bar: Bar):
        """Per-variant entry decision -> (raw_fill_price, reason) or None.
        All reference levels come from COMPLETED bars only; the current bar
        contributes only its OHLC as trigger/fill levels (Rules 5, 9-11)."""
        st = state[symbol]
        if cfg.strategy in ("breakout", "breakout_regime"):
            ref_high = st.ref_high(cfg.lookback)
            if ref_high is None or not (bar.high > ref_high):   # Rule 5 (strict >)
                return None
            if cfg.strategy == "breakout_regime" and not regime_ok(st):
                return None
            return max(bar.open, ref_high), "breakout"   # stop-buy: gap-up fills at open
        # pullback (Rules 9-11)
        if not regime_ok(st):
            return None
        if cfg.pullback_depth_pct > 0:
            sma = st.sma(cfg.regime_sma)
            t = sma * (1 - cfg.pullback_depth_pct)   # Rule 9 (depth form)
        else:
            t = st.ref_low(cfg.pullback_lookback)    # Rule 9 (M-day-low form)
        if t is None or not (bar.low <= t):
            return None
        return min(bar.open, t), "pullback"   # Rule 11: resting buy-limit fill

    def process_entry(symbol: str, bar: Bar, exited: set, prev_close: Dict[str, float]) -> None:
        if symbol in exited or book.qty(symbol) > 0:
            return   # Rule 5 gating: no re-entry same bar, no pyramiding
        st = state[symbol]
        trigger = entry_trigger(symbol, bar)
        if trigger is None:
            return
        if not daily_interval and not _in_entry_window(bar.ts):   # Rule 7
            return
        fill_px, entry_reason = trigger
        px_slipped = fill_px * (1 + slip)

        others_value = sum(q * prev_close.get(sym2, 0.0)
                           for sym2, (q, _) in book.positions.items()
                           if q > 0 and sym2 != symbol)
        equity = book.cash + others_value
        sr = size_position(equity=equity, entry_price=px_slipped, signal_stop=None,
                           confidence=cfg.confidence, cfg=risk_cfg, current_qty=0,
                           gross_exposure=others_value, fixed_qty=cfg.order_qty)
        qty_want = sr.qty
        if qty_want > 0:
            qty_want = min(qty_want, risk_cfg.max_position_qty)
            if px_slipped > 0:
                qty_want = min(qty_want, math.floor(risk_cfg.max_order_notional / px_slipped))
            denom = px_slipped + cfg.commission_per_share
            if denom > 0:
                affordable = math.floor((book.cash - cfg.commission_per_order) / denom)
                qty_want = min(qty_want, max(affordable, 0))
        if qty_want <= 0:
            book.skipped_for_cash += 1
            return

        p_e = do_fill(symbol, "BUY", qty_want, fill_px, bar.ts, entry_reason)

        if not daily_interval:
            st.hwm = p_e
            st.hwm_day = bar.day()

        # Rule 6: same-bar exit after entry — pessimistic asymmetry (can fire
        # a same-bar stop, but never a same-bar take-profit).
        sb_triggers: List[Tuple[float, str]] = []
        p_sl_sb = p_e * (1 - cfg.stop_loss_pct)
        if bar.low <= p_sl_sb:
            sb_triggers.append((p_sl_sb, "stop-loss"))
        if trail > 0:
            p_tr_sb = p_e * (1 - trail)
            if bar.low <= p_tr_sb:
                sb_triggers.append((p_tr_sb, "trailing-stop"))
        if sb_triggers:
            level, reason = max(sb_triggers, key=lambda x: x[0])
            do_fill(symbol, "SELL", qty_want, level, bar.ts, reason)

    for i, ts in enumerate(all_ts):
        day = ts_day[ts]
        warmup = day < cfg.start
        active = ts_active[ts]
        prev_close = dict(last_close)

        for s in active:
            bar = bar_index[s][ts]
            st = state[s]
            st.roll_day_if_needed(day)
            st.update_forming(bar)
            last_close[s] = bar.close

        if warmup:
            continue

        exited: set = set()
        for s in active:
            process_exit(s, bar_index[s][ts], exited)
        for s in active:
            process_entry(s, bar_index[s][ts], exited, prev_close)

        if day_end[ts]:
            equity = book.cash + sum(q * last_close.get(sym, 0.0)
                                     for sym, (q, _) in book.positions.items() if q > 0)
            equity_curve.append((day, equity, book.cash))
            for sym, (q, _) in book.positions.items():
                if q > 0:
                    held_days[sym] = held_days.get(sym, 0) + 1

    return BacktestResult(fills=tuple(book.fills), equity_curve=tuple(equity_curve),
                          skipped_for_cash=book.skipped_for_cash, cfg=cfg,
                          held_days=held_days)
