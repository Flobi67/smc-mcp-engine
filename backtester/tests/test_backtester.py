import numpy as np
import pandas as pd
import pytest

import smc_backtester as bt


def make_df(rows, start="2025-01-01", freq="1h"):
    """rows : liste de (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


def random_walk(n=24 * 20, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0, 1.5, n)
    return make_df(list(zip(open_, high, low, close)))


# --- detect_fvg -----------------------------------------------------------
def test_detect_fvg_bullish():
    df = make_df([(9, 10, 8, 9.5),      # bougie 0 : high = 10
                  (10, 15, 10, 14),     # bougie 1 : impulsion haussière
                  (14, 16, 12, 15)])    # bougie 2 : low 12 > 10 -> gap
    out = bt.detect_fvg(df)
    assert bool(out["bullish_fvg"].iloc[2]) is True
    assert not out["bearish_fvg"].any()


def test_detect_fvg_bearish():
    df = make_df([(20, 21, 18, 19),     # bougie 0 : low = 18
                  (18, 18, 12, 13),     # bougie 1 : impulsion baissière
                  (13, 16, 11, 12)])    # bougie 2 : high 16 < 18 -> gap
    out = bt.detect_fvg(df)
    assert bool(out["bearish_fvg"].iloc[2]) is True
    assert not out["bullish_fvg"].any()


# --- evaluate_trade -------------------------------------------------------
COST_R = 2 * (bt.FEE_RATE + bt.SLIPPAGE) * 100 / 2  # entry 100, risque 2


def test_evaluate_trade_stop_loss():
    fut = make_df([(100, 101, 97, 98)])
    r = bt.evaluate_trade(fut, 100, 98, 104, "LONG")
    assert r["reason"] == "SL" and r["bars"] == 1 and not r["win"]
    assert r["r"] == pytest.approx(-1 - COST_R)


def test_evaluate_trade_take_profit():
    fut = make_df([(100, 101, 99, 100), (100, 105, 100, 104)])
    r = bt.evaluate_trade(fut, 100, 98, 104, "LONG")
    assert r["reason"] == "TP" and r["bars"] == 2 and r["win"]
    assert r["r"] == pytest.approx(2 - COST_R)


def test_evaluate_trade_short_take_profit():
    fut = make_df([(100, 101, 95, 96)])
    r = bt.evaluate_trade(fut, 100, 102, 96, "SHORT")
    assert r["reason"] == "TP" and r["r"] == pytest.approx(2 - COST_R)


def test_evaluate_trade_sl_and_tp_same_candle_sl_wins():
    fut = make_df([(100, 105, 97, 100)])
    r = bt.evaluate_trade(fut, 100, 98, 104, "LONG")
    assert r["reason"] == "SL" and not r["win"]


def test_evaluate_trade_timeout_closes_at_last_close():
    fut = make_df([(100, 101, 99, 100.5)] * 3)
    r = bt.evaluate_trade(fut, 100, 98, 104, "LONG", horizon=3)
    assert r["reason"] == "TIMEOUT" and r["bars"] == 3
    assert r["r"] == pytest.approx(0.25 - COST_R)


# --- déterminisme ---------------------------------------------------------
def test_v1_is_deterministic_and_pure():
    df = random_walk()
    before = df.copy()
    a = bt.backtest_v1_daily_sweep(df, "TEST")
    b = bt.backtest_v1_daily_sweep(df, "TEST")
    assert a == b
    pd.testing.assert_frame_equal(df, before)


# --- load_klines ----------------------------------------------------------
def test_load_klines_roundtrip(tmp_path):
    df = random_walk(50)
    p = tmp_path / "x.csv"
    bt.save_klines(df, p)
    out = bt.load_klines(p)
    assert len(out) == 50
    np.testing.assert_allclose(out["close"], df["close"], rtol=1e-8)


def test_load_klines_rejects_modified_csv(tmp_path):
    p = tmp_path / "x.csv"
    bt.save_klines(random_walk(50), p)
    p.write_text(p.read_text() + "\n")
    with pytest.raises(ValueError, match="SHA-256"):
        bt.load_klines(p)


def test_load_klines_rejects_missing_hash(tmp_path):
    p = tmp_path / "x.csv"
    bt.save_klines(random_walk(50), p)
    (tmp_path / "x.csv.sha256").unlink()
    with pytest.raises(ValueError, match="manquante"):
        bt.load_klines(p)
