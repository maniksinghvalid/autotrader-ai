"""Adapter: routine *ticker sweep* output (run_id/sweep_date/from_signal/
to_signal/composite_score/direction) -> canonical RoutineSignalPayload.

Each test maps to a user-confirmed decision (see the approved plan):
  D1 to_signal semantics   D2 exit fail-safe   D3 symbol via allow-list
  D4 CLI -> inbox
"""
import pytest

from autotrader.signals.routine_adapter import adapt_routine_sweep, main
from autotrader.signals.schema import RoutineSignalPayload
from autotrader.signals.normalize import normalize_payload
from autotrader.signals.inbox import SignalInbox

ALLOW = frozenset({"US.DIVO", "CA.VDY", "US.YNVDA", "US.IBIT", "US.MARA", "US.SPCE"})


def _sweep(changes, run_id="routine-20260615-1134-20219d", sweep_date="2026-06-15"):
    return {"run_id": run_id, "sweep_date": sweep_date,
            "ticker_count": len(changes), "signal_changes": changes}


def _chg(ticker, to_signal, composite_score, direction="upgrade", from_signal="HOLD"):
    return {"ticker": ticker, "from_signal": from_signal, "to_signal": to_signal,
            "composite_score": composite_score, "price": 10.0, "direction": direction}


# A stable inline copy of the producer's *raw ticker-sweep* dialect. Kept here (not read
# from docs/routinesignal-ticker.json, which the cloud routine regenerates) so these tests
# pin the adapter contract regardless of whatever that sample file currently holds.
RAW_TICKER_SWEEP = _sweep([
    _chg("DIVO", "BUY", 71, from_signal="HOLD"),
    _chg("IBIT", "HOLD", 55, from_signal="NEUTRAL"),
    _chg("MARA", "NEUTRAL", 42, from_signal="CAUTION"),
    _chg("SPCE", "NEUTRAL", 40, from_signal="CAUTION"),
    _chg("VDY", "BUY", 77, from_signal="HOLD"),
    _chg("YNVDA", "AVOID", 22, direction="downgrade", from_signal="CAUTION"),
])


# ---------------------------------------------------------------- D1: semantics
def test_buy_to_signal_maps_to_up_entry():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "BUY", 71)]), allowed_symbols=ALLOW)
    assert len(p.signal_changes) == 1
    c = p.signal_changes[0]
    assert c.direction == "UP"
    assert c.transition == ["HOLD", "BUY"]


def test_avoid_and_caution_map_to_down_exit():
    p = adapt_routine_sweep(
        _sweep([_chg("YNVDA", "AVOID", 22, direction="downgrade", from_signal="CAUTION"),
                _chg("SPCE", "CAUTION", 35, direction="downgrade", from_signal="NEUTRAL")]),
        allowed_symbols=ALLOW)
    assert {c.ticker: c.direction for c in p.signal_changes} == {
        "US.YNVDA": "DOWN", "US.SPCE": "DOWN"}


def test_hold_and_neutral_are_skipped_entirely():
    p = adapt_routine_sweep(
        _sweep([_chg("IBIT", "HOLD", 55), _chg("MARA", "NEUTRAL", 42)]),
        allowed_symbols=ALLOW)
    assert p.signal_changes == []


def test_upgrade_to_neutral_never_becomes_a_buy():
    """The crux of D1: direction='upgrade' but to_signal=NEUTRAL must NOT buy."""
    p = adapt_routine_sweep(
        _sweep([_chg("MARA", "NEUTRAL", 42, direction="upgrade", from_signal="CAUTION")]),
        allowed_symbols=ALLOW)
    assert p.signal_changes == []


def test_strong_buy_maps_to_up():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "STRONG BUY", 90)]), allowed_symbols=ALLOW)
    assert p.signal_changes[0].direction == "UP"


def test_unknown_to_signal_label_is_skipped():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "WAT", 71)]), allowed_symbols=ALLOW)
    assert p.signal_changes == []


# ------------------------------------------------------------- D2: exit fail-safe
def test_buy_points_delta_scales_with_score():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "BUY", 71)]), allowed_symbols=ALLOW)
    assert p.signal_changes[0].points_delta == 7   # round(71/10)


def test_exit_points_delta_is_fixed_high_regardless_of_low_score():
    p = adapt_routine_sweep(
        _sweep([_chg("YNVDA", "AVOID", 22, direction="downgrade")]), allowed_symbols=ALLOW)
    assert p.signal_changes[0].points_delta == -10


def test_exit_clears_confidence_filter_after_normalize():
    """A low-score AVOID exit must survive the min_confidence=0.6 filter."""
    p = adapt_routine_sweep(
        _sweep([_chg("YNVDA", "AVOID", 22, direction="downgrade")]), allowed_symbols=ALLOW)
    sig = normalize_payload(p)[0]
    assert sig.direction == "SELL"
    assert sig.confidence == 1.0          # >= 0.6, never dropped


def test_buy_confidence_after_normalize_clears_filter():
    p = adapt_routine_sweep(_sweep([_chg("VDY", "BUY", 77)]), allowed_symbols=ALLOW)
    sig = normalize_payload(p)[0]
    assert sig.confidence == pytest.approx(0.8)   # round(77/10)=8 -> 0.8


# ------------------------------------------------------- D3: symbol via allow-list
def test_bare_ticker_resolved_to_qualified_via_allowlist():
    p = adapt_routine_sweep(_sweep([_chg("VDY", "BUY", 77)]), allowed_symbols=ALLOW)
    assert p.signal_changes[0].ticker == "CA.VDY"   # not US.VDY


def test_unknown_bare_ticker_is_skipped():
    p = adapt_routine_sweep(_sweep([_chg("ZZZZ", "BUY", 77)]), allowed_symbols=ALLOW)
    assert p.signal_changes == []


def test_ambiguous_bare_ticker_is_skipped():
    p = adapt_routine_sweep(
        _sweep([_chg("VDY", "BUY", 77)]),
        allowed_symbols=frozenset({"US.VDY", "CA.VDY"}))
    assert p.signal_changes == []


def test_already_qualified_ticker_passes_through_uppercased():
    p = adapt_routine_sweep(_sweep([_chg("ca.vdy", "BUY", 77)]), allowed_symbols=ALLOW)
    assert p.signal_changes[0].ticker == "CA.VDY"


# ----------------------------------------------------------- payload-level mapping
def test_routine_id_and_timestamp_derived_from_run_id():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "BUY", 71)]), allowed_symbols=ALLOW)
    assert p.routine_id == "routine-20260615-1134-20219d"
    assert p.timestamp.year == 2026 and p.timestamp.month == 6 and p.timestamp.day == 15
    assert (p.timestamp.hour, p.timestamp.minute) == (11, 34)


def test_timestamp_falls_back_to_sweep_date_when_run_id_unparseable():
    p = adapt_routine_sweep(
        _sweep([_chg("DIVO", "BUY", 71)], run_id="weird-id"), allowed_symbols=ALLOW)
    assert (p.timestamp.year, p.timestamp.month, p.timestamp.day) == (2026, 6, 15)


def test_output_is_valid_routine_signal_payload():
    p = adapt_routine_sweep(_sweep([_chg("DIVO", "BUY", 71)]), allowed_symbols=ALLOW)
    # round-trips through strict pydantic validation
    RoutineSignalPayload.model_validate_json(p.model_dump_json())


def test_missing_run_id_raises():
    with pytest.raises(ValueError):
        adapt_routine_sweep({"sweep_date": "2026-06-15", "signal_changes": []},
                            allowed_symbols=ALLOW)


# ------------------------------------------------ full sweep (closes verify gap)
def test_full_ticker_sweep_end_to_end():
    p = adapt_routine_sweep(RAW_TICKER_SWEEP, allowed_symbols=ALLOW)
    by_sym = {c.ticker: c.direction for c in p.signal_changes}
    assert by_sym == {"US.DIVO": "UP", "CA.VDY": "UP", "US.YNVDA": "DOWN"}
    # IBIT(HOLD), MARA(NEUTRAL), SPCE(NEUTRAL) skipped
    assert "US.IBIT" not in by_sym and "US.MARA" not in by_sym and "US.SPCE" not in by_sym


# ------------------------------------------------------------------- D4: CLI->inbox
def test_cli_writes_one_valid_file_into_inbox(tmp_path, monkeypatch):
    import json
    src = tmp_path / "sweep.json"
    src.write_text(json.dumps(RAW_TICKER_SWEEP))
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AUTOTRADER_SIGNAL_INBOX", str(inbox))
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", ",".join(sorted(ALLOW)))
    rc = main([str(src)])
    assert rc == 0
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    # the dropped file is consumable by the real inbox poller
    sigs = SignalInbox(str(inbox)).poll()
    assert {s.symbol: s.direction for s in sigs} == {
        "US.DIVO": "BUY", "CA.VDY": "BUY", "US.YNVDA": "SELL"}


def test_cli_without_inbox_env_returns_2(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOTRADER_SIGNAL_INBOX", raising=False)
    assert main([str(tmp_path / "any.json")]) == 2


def test_cli_zero_actionable_writes_no_file(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AUTOTRADER_SIGNAL_INBOX", str(inbox))
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", ",".join(sorted(ALLOW)))
    src = tmp_path / "allhold.json"
    src.write_text('{"run_id":"routine-20260615-1134-x","sweep_date":"2026-06-15",'
                   '"signal_changes":[{"ticker":"IBIT","from_signal":"NEUTRAL",'
                   '"to_signal":"HOLD","composite_score":55,"price":1.0,'
                   '"direction":"upgrade"}]}')
    rc = main([str(src)])
    assert rc == 0
    assert list(inbox.glob("*.json")) == []
