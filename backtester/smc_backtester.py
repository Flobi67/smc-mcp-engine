#!/usr/bin/env python3
"""
SMC Crypto Multi-Stage Backtester pour Binance Futures (version corrigée).

  - V1 : Sweeps journaliers (PDH/PDL)
  - V2 : Sweeps intraday (liquidité de la session asiatique)
  - V3 : Scanner multi-actifs (V1 + V2 sur chaque symbole)
  - V4 : SMC avancé (Fair Value Gap + divergence SMT, longs et shorts)

Corrections par rapport à la version précédente :
  - Pagination des klines (1500 max par requête) -> 1 an de données par défaut
  - Bougie en cours (non clôturée) retirée
  - Les trades non résolus sont clôturés au marché à la fin de l'horizon
    (au lieu d'être écartés)
  - Frais + slippage inclus, résultats mesurés en R
  - Un seul trade ouvert à la fois (pas de chevauchement)
  - V2 : fenêtre future non tronquée à 18h, session asiatique complète exigée
  - V4 : ajout des shorts, RR paramétrable (2.0 par défaut, comme V1/V2)
  - Gestion des erreurs réseau avec nouvelles tentatives

Architecture : les fonctions backtest_* sont pures (DataFrames en entrée, dict en
sortie, aucun appel réseau ni horloge). Seule la commande `download` utilise le
réseau ; `run` lit des CSV locaux dont l'empreinte SHA-256 est vérifiée.

  python smc_backtester.py download --start 2025-01-01 --end 2025-11-01 --data-dir data
  python smc_backtester.py run      --start 2025-01-01 --end 2025-11-01 --data-dir data
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paramètres globaux
# ---------------------------------------------------------------------------
FEE_RATE = 0.0004        # frais taker Binance Futures par côté (0,04 %)
SLIPPAGE = 0.0002        # slippage estimé par côté (0,02 %)
MIN_RISK_PCT = 0.0015    # risque minimum (0,15 % du prix) : sinon les frais mangent le trade
HORIZON = 48             # durée max d'un trade, en bougies
RR = 2.0                 # ratio risque/rendement par défaut

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT"]
INTERVAL_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000}

BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_PER_REQUEST = 1500


# ---------------------------------------------------------------------------
# Récupération des données (Binance Futures API, paginée)
# ---------------------------------------------------------------------------
def _get_with_retry(params: dict, retries: int = 3) -> list:
    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(BINANCE_FUTURES_URL, params=params, timeout=15)
            if res.status_code == 451:
                raise RuntimeError(
                    "Erreur 451 : l'API Binance Futures est bloquée dans ton pays/réseau (VPN ?)."
                )
            res.raise_for_status()
            return res.json()
        except RuntimeError:
            raise
        except Exception as e:  # réseau, timeout, 429...
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Échec de récupération des klines : {last_err}")


def download_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Télécharge les bougies [start_ms, end_ms) en avançant dans le temps (réseau)."""
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        raw = _get_with_retry({"symbol": symbol, "interval": interval, "limit": MAX_PER_REQUEST,
                               "startTime": cursor, "endTime": end_ms - 1})
        if not raw:
            break
        rows.extend(raw)
        cursor = raw[-1][0] + INTERVAL_MS[interval]
        if len(raw) < MAX_PER_REQUEST:
            break
        time.sleep(0.1)

    cols = ['open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'count', 'taker_buy_vol',
            'taker_buy_quote_vol', 'ignore']
    df = pd.DataFrame(rows, columns=cols)
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')].sort_index()
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = df[c].astype(float)
    return df[['open', 'high', 'low', 'close', 'volume']]


# ---------------------------------------------------------------------------
# Stockage local : CSV + empreinte SHA-256
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha_path(path: Path) -> Path:
    return Path(str(path) + ".sha256")


def save_klines(df: pd.DataFrame, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, float_format="%.10g")
    _sha_path(path).write_text(_sha256(path) + "\n")


def load_klines(path) -> pd.DataFrame:
    """Charge un CSV de bougies ; refuse le fichier si son empreinte SHA-256 ne correspond pas."""
    path = Path(path)
    sha_file = _sha_path(path)
    if not sha_file.exists():
        raise ValueError(f"Empreinte manquante : {sha_file}")
    expected = sha_file.read_text().split()[0]
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"Empreinte SHA-256 invalide pour {path} (attendu {expected}, obtenu {actual})")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "timestamp"
    return df.astype(float)


def klines_path(data_dir, symbol: str, interval: str, start: str, end: str) -> Path:
    return Path(data_dir) / f"{symbol}_{interval}_{start}_{end}.csv"


# ---------------------------------------------------------------------------
# Moteur d'évaluation de trade
# ---------------------------------------------------------------------------
def evaluate_trade(future_df: pd.DataFrame, entry: float, sl: float, tp: float,
                   side: str, horizon: int = HORIZON):
    """
    Parcourt les bougies suivantes. SL prioritaire si SL et TP sont touchés
    dans la même bougie (hypothèse prudente). Si rien n'est touché dans
    l'horizon, clôture au marché sur la dernière bougie (TIMEOUT).
    Résultat net de frais et slippage, exprimé en R.
    """
    n = min(len(future_df), horizon)
    if n == 0:
        return None

    highs = future_df['high'].to_numpy()[:n]
    lows = future_df['low'].to_numpy()[:n]
    closes = future_df['close'].to_numpy()[:n]
    risk = abs(entry - sl)

    exit_price, reason, bars = closes[-1], "TIMEOUT", n
    for i in range(n):
        if side == "LONG":
            if lows[i] <= sl:
                exit_price, reason, bars = sl, "SL", i + 1
                break
            if highs[i] >= tp:
                exit_price, reason, bars = tp, "TP", i + 1
                break
        else:
            if highs[i] >= sl:
                exit_price, reason, bars = sl, "SL", i + 1
                break
            if lows[i] <= tp:
                exit_price, reason, bars = tp, "TP", i + 1
                break

    gross_r = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
    cost_r = 2 * (FEE_RATE + SLIPPAGE) * entry / risk
    net_r = gross_r - cost_r
    return {
        'r': float(net_r),
        'pnl_pct': float(net_r * risk / entry * 100),
        'win': bool(net_r > 0),
        'bars': int(bars),
        'reason': reason,
    }


def open_trade(df: pd.DataFrame, i: int, side: str, entry: float, sl: float, rr: float = RR):
    """Construit le trade à la clôture de la bougie i et l'évalue sur les bougies suivantes."""
    risk = abs(entry - sl)
    if risk <= 0 or risk / entry < MIN_RISK_PCT:
        return None
    tp = entry + rr * risk if side == "LONG" else entry - rr * risk
    return evaluate_trade(df.iloc[i + 1:i + 1 + HORIZON], entry, sl, tp, side)


# ---------------------------------------------------------------------------
# Calcul des performances
# ---------------------------------------------------------------------------
def compute_performance(trades: list) -> dict:
    if not trades:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "net_r": 0.0, "avg_r": 0.0, "max_dd_r": 0.0, "timeouts": 0}

    r = np.array([t['r'] for t in trades], dtype=float)
    wins = r[r > 0]
    losses = r[r <= 0]
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else None

    equity = np.concatenate([[0.0], np.cumsum(r)])
    max_dd = float((np.maximum.accumulate(equity) - equity).max())

    return {
        "total_trades": int(len(r)),
        "win_rate": round(float(len(wins) / len(r) * 100), 2),
        "profit_factor": round(float(pf), 2) if pf is not None else "Inf",
        "net_r": round(float(r.sum()), 2),
        "avg_r": round(float(r.mean()), 3),
        "max_dd_r": round(max_dd, 2),
        "timeouts": int(sum(1 for t in trades if t['reason'] == "TIMEOUT")),
    }


def _period(df: pd.DataFrame) -> str:
    return f"{df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d} ({len(df)} bougies)"


# ---------------------------------------------------------------------------
# V1 : Sweeps de liquidité journaliers (PDH / PDL)
# ---------------------------------------------------------------------------
def backtest_v1_daily_sweep(df: pd.DataFrame, symbol: str = "BTCUSDT") -> dict:
    """df : bougies 1h (open/high/low/close/volume, index UTC)."""
    daily = df.resample('1D').agg({'high': 'max', 'low': 'min'})
    daily['PDH'] = daily['high'].shift(1)
    daily['PDL'] = daily['low'].shift(1)

    df = df.join(daily[['PDH', 'PDL']], how='left')
    df[['PDH', 'PDL']] = df[['PDH', 'PDL']].ffill()

    trades = []
    busy_until = 0
    for i in range(1, len(df) - 1):
        if i < busy_until:
            continue
        curr = df.iloc[i]
        outcome = None
        if curr['low'] < curr['PDL'] and curr['close'] > curr['PDL']:
            outcome = open_trade(df, i, "LONG", curr['close'], curr['low'])
        elif curr['high'] > curr['PDH'] and curr['close'] < curr['PDH']:
            outcome = open_trade(df, i, "SHORT", curr['close'], curr['high'])
        if outcome:
            trades.append(outcome)
            busy_until = i + outcome['bars']

    return {"stage": "V1_Daily_Sweep", "symbol": symbol, "period": _period(df),
            "metrics": compute_performance(trades)}


# ---------------------------------------------------------------------------
# V2 : Sweeps intraday (range de la session asiatique, heures UTC)
# ---------------------------------------------------------------------------
def backtest_v2_session_sweep(df: pd.DataFrame, symbol: str = "ETHUSDT") -> dict:
    """df : bougies 15m (open/high/low/close/volume, index UTC)."""
    df = df.copy()
    df['hour'] = df.index.hour
    df['date'] = df.index.date

    trades = []
    busy_until = 0

    for d in df['date'].unique():
        day_df = df[df['date'] == d]
        asia = day_df[day_df['hour'] < 7]
        if len(asia) < 28:  # session asiatique incomplète (7h x 4 bougies)
            continue

        asia_high = asia['high'].max()
        asia_low = asia['low'].min()
        work = day_df[(day_df['hour'] >= 8) & (day_df['hour'] <= 18)]

        for ts, bar in work.iterrows():
            p = df.index.get_loc(ts)
            if p < busy_until:
                continue
            outcome = None
            if bar['low'] < asia_low and bar['close'] > asia_low:
                outcome = open_trade(df, p, "LONG", bar['close'], bar['low'])
            elif bar['high'] > asia_high and bar['close'] < asia_high:
                outcome = open_trade(df, p, "SHORT", bar['close'], bar['high'])
            if outcome:
                trades.append(outcome)
                busy_until = p + outcome['bars']
                break  # un seul trade par jour

    return {"stage": "V2_Intraday_Session_Sweep", "symbol": symbol, "period": _period(df),
            "metrics": compute_performance(trades)}


# ---------------------------------------------------------------------------
# V3 : Scanner multi-actifs (V1 + V2 sur chaque symbole)
# ---------------------------------------------------------------------------
def backtest_v3_screener(data: dict) -> dict:
    """data : {symbole: {"1h": DataFrame, "15m": DataFrame}}."""
    results = {}
    for s in sorted(data):
        try:
            results[s] = {
                "V1": backtest_v1_daily_sweep(data[s]["1h"], s)['metrics'],
                "V2": backtest_v2_session_sweep(data[s]["15m"], s)['metrics'],
            }
        except Exception as e:
            results[s] = {"error": str(e)}
    return {"stage": "V3_Multi_Asset_Screener", "results": results}


# ---------------------------------------------------------------------------
# V4 : SMC avancé (Fair Value Gap + divergence SMT), longs et shorts
# ---------------------------------------------------------------------------
def detect_fvg(df: pd.DataFrame) -> pd.DataFrame:
    df['bullish_fvg'] = (df['low'] > df['high'].shift(2)) & (df['close'].shift(1) > df['open'].shift(1))
    df['bearish_fvg'] = (df['high'] < df['low'].shift(2)) & (df['close'].shift(1) < df['open'].shift(1))
    return df


def backtest_v4_advanced_smc(lead: pd.DataFrame, lag: pd.DataFrame,
                             symbol_leader: str = "BTCUSDT", symbol_lag: str = "ETHUSDT",
                             rr: float = RR, lookback: int = 24) -> dict:
    """lead / lag : bougies 1h du leader et du suiveur."""
    idx = lead.index.intersection(lag.index)
    lead = lead.loc[idx].copy()
    lag = detect_fvg(lag.loc[idx].copy())

    for d in (lead, lag):
        d['roll_low'] = d['low'].rolling(lookback).min().shift(1)    # lookback bougies précédentes
        d['roll_high'] = d['high'].rolling(lookback).max().shift(1)

    l_low, l_high = lead['low'].to_numpy(), lead['high'].to_numpy()
    l_rlow, l_rhigh = lead['roll_low'].to_numpy(), lead['roll_high'].to_numpy()
    g_low, g_high, g_close = lag['low'].to_numpy(), lag['high'].to_numpy(), lag['close'].to_numpy()
    g_rlow, g_rhigh = lag['roll_low'].to_numpy(), lag['roll_high'].to_numpy()
    bull, bear = lag['bullish_fvg'].to_numpy(), lag['bearish_fvg'].to_numpy()

    trades = []
    busy_until = 0
    for i in range(lookback + 1, len(lag) - 1):
        if i < busy_until:
            continue
        outcome = None
        # Long : le leader balaie son low, le suiveur le tient + FVG haussier
        if l_low[i] < l_rlow[i] and g_low[i] >= g_rlow[i] and bull[i]:
            outcome = open_trade(lag, i, "LONG", g_close[i], g_low[i], rr)
        # Short : le leader balaie son high, le suiveur le tient + FVG baissier
        elif l_high[i] > l_rhigh[i] and g_high[i] <= g_rhigh[i] and bear[i]:
            outcome = open_trade(lag, i, "SHORT", g_close[i], g_high[i], rr)
        if outcome:
            trades.append(outcome)
            busy_until = i + outcome['bars']

    return {"stage": "V4_SMT_FVG_Advanced", "pair": f"{symbol_leader}/{symbol_lag}",
            "period": _period(lag), "metrics": compute_performance(trades)}


# ---------------------------------------------------------------------------
# Ligne de commande (seul `download` utilise le réseau)
# ---------------------------------------------------------------------------
def _to_ms(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)


def cmd_download(args) -> None:
    for sym in args.symbols:
        for interval in ("1h", "15m"):
            df = download_klines(sym, interval, _to_ms(args.start), _to_ms(args.end))
            path = klines_path(args.data_dir, sym, interval, args.start, args.end)
            save_klines(df, path)
            print(f"[DOWNLOAD] {path} ({len(df)} bougies)")


def cmd_run(args) -> None:
    def load(sym, interval):
        return load_klines(klines_path(args.data_dir, sym, interval, args.start, args.end))

    print("=" * 60)
    print(f"Frais {FEE_RATE*100:.2f}% + slippage {SLIPPAGE*100:.2f}% par côté | "
          f"RR {RR} | horizon {HORIZON} bougies")
    print("=" * 60)

    def show(title, fn):
        print(f"\n--- [{title}] ---")
        try:
            print(json.dumps(fn(), indent=2, default=str))
        except Exception as e:
            print(f"[ERREUR] {e}")

    show("V1 Daily Sweep - NEAR", lambda: backtest_v1_daily_sweep(load("NEARUSDT", "1h"), "NEARUSDT"))
    show("V2 Intraday Session Sweep - ETH", lambda: backtest_v2_session_sweep(load("ETHUSDT", "15m"), "ETHUSDT"))
    show("V3 Multi-Asset Screener", lambda: backtest_v3_screener(
        {s: {"1h": load(s, "1h"), "15m": load(s, "15m")} for s in args.symbols}))
    show("V4 SMT + FVG - BTC/ETH", lambda: backtest_v4_advanced_smc(load("BTCUSDT", "1h"), load("ETHUSDT", "1h")))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="SMC multi-stage backtester")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn in (("download", cmd_download), ("run", cmd_run)):
        sp = sub.add_parser(name)
        sp.add_argument("--start", required=True, help="YYYY-MM-DD (UTC, inclus)")
        sp.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, exclu)")
        sp.add_argument("--data-dir", default="data")
        sp.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
        sp.set_defaults(func=fn)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
