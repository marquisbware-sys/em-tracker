"""
Expected Move + 0.16 Delta Strike Calculator (v3)

For each ticker, calculates:
  - Weekly EM via ATM straddle * 0.85
  - Monthly EM via ATM straddle * 0.85
  - Weekly 0.16 delta CALL strike (upper skew-aware boundary)
  - Weekly 0.16 delta PUT  strike (lower skew-aware boundary)
  - Monthly 0.16 delta CALL/PUT strikes (same, for monthly expiry)

The 0.16 delta strikes use each individual strike's own implied volatility
(capturing real-world skew, not the ATM-IV approximation).

When two adjacent strikes straddle 0.16 delta (one at 0.15, one at 0.17),
the script linearly interpolates between them and rounds the result to the
nearest $0.10.

Output: expected_moves.json
"""

import yfinance as yf
import json
import math
from datetime import datetime, timedelta
import sys
import time


# ============================================================
# CONFIG
# ============================================================
WATCHLIST = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    "XLF", "XLK", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE",
    "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "INTC", "AVGO",
    "QCOM", "ORCL", "CRM", "ADBE", "NFLX", "MU", "AMAT", "KLAC", "LRCX",
    "SNPS", "CDNS", "TSLL", "TQQQ", "SQQQ", "UVXY", "SPXL", "SPXS",
    "SOXL", "SOXS", "MSTU", "NVDL",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "V", "MA", "AXP",
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY", "AMGN",
    "XOM", "CVX", "COP", "OXY", "SLB", "HAL",
    "GLD", "SLV", "USO", "GDX", "GDXJ", "UNG",
    "WMT", "TGT", "COST", "HD", "LOW", "DIS", "ROKU",
    "COIN", "MSTR", "HOOD", "IBIT", "PLTR", "SOFI",
    "RIVN", "LCID", "GME", "AMC",
    "BA", "RTX", "LMT", "CAT", "T", "VZ",
]

RISK_FREE_RATE_DEFAULT = 0.045
TARGET_DELTA = 0.16


# ============================================================
# BLACK-SCHOLES DELTA (no scipy dependency)
# ============================================================
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def call_delta(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or K <= 0 or S <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def put_delta(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or K <= 0 or S <= 0:
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) - 1.0


# ============================================================
# 0.16 DELTA STRIKE FINDER WITH INTERPOLATION
# ============================================================
def find_delta_strike_interpolated(chain_df, S, T, r, target_delta, option_type='call'):
    """
    Walk the option chain, compute delta at each strike using that strike's IV.
    Find the two adjacent strikes that bracket the target delta and linearly
    interpolate between them. Round result to nearest $0.10.

    For calls: target_delta = +0.16, delta decreases as strike increases (OTM)
    For puts:  target_delta = -0.16, delta increases (toward 0) as strike decreases (OTM)

    Returns interpolated strike price, or None if data is insufficient.
    """
    # Build list of (strike, delta) pairs, filtering bad IV
    points = []
    for _, row in chain_df.iterrows():
        K = row.get('strike')
        sigma = row.get('impliedVolatility')
        if K is None or sigma is None:
            continue
        if sigma <= 0.01 or sigma > 5.0:
            continue
        d = call_delta(S, K, T, r, sigma) if option_type == 'call' else put_delta(S, K, T, r, sigma)
        points.append((float(K), float(d)))

    if len(points) < 2:
        return None

    # Sort by strike ascending
    points.sort(key=lambda p: p[0])

    # Find adjacent pair (K_low, d_low) and (K_high, d_high) where target_delta
    # falls between d_low and d_high. Delta is monotonic in strike for both
    # calls (decreasing) and puts (increasing toward zero), so a simple sweep works.
    best_pair = None
    for i in range(len(points) - 1):
        k1, d1 = points[i]
        k2, d2 = points[i + 1]
        # Does target fall between these two deltas?
        lo, hi = (d1, d2) if d1 < d2 else (d2, d1)
        if lo <= target_delta <= hi:
            best_pair = ((k1, d1), (k2, d2))
            break

    if best_pair is None:
        # Target delta is outside the chain's range. Fall back to closest strike.
        closest = min(points, key=lambda p: abs(p[1] - target_delta))
        return round(closest[0] * 10) / 10  # round to $0.10

    (k1, d1), (k2, d2) = best_pair

    # Linear interpolation. If d2 == d1 (degenerate), return midpoint
    if abs(d2 - d1) < 1e-9:
        interpolated = (k1 + k2) / 2.0
    else:
        # Solve: target = d1 + t * (d2 - d1) for t in [0,1]
        t = (target_delta - d1) / (d2 - d1)
        interpolated = k1 + t * (k2 - k1)

    # Round to nearest $0.10
    return round(interpolated * 10) / 10


# ============================================================
# STRADDLE EM HELPERS (existing logic)
# ============================================================
def get_atm_strike(price, strikes):
    return min(strikes, key=lambda s: abs(s - price))


def calculate_em_straddle(call_mid, put_mid):
    return (call_mid + put_mid) * 0.85


def get_mid(bid, ask, last):
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last if last else 0


def find_next_friday_expiry(expirations, target_date=None):
    if target_date is None:
        today = datetime.now().date()
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0 and today.weekday() == 4:
            days_until_friday = 7
        target_date = today + timedelta(days=days_until_friday)
    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        if exp_date >= target_date:
            return exp_str
    return None


# ============================================================
# RISK-FREE RATE
# ============================================================
def fetch_risk_free_rate():
    """Fetch current 13-week T-bill (^IRX). Falls back to default if unavailable."""
    try:
        irx = yf.Ticker("^IRX")
        hist = irx.history(period="5d")
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1]) / 100.0
            if 0 < rate < 0.20:
                return rate
    except Exception:
        pass
    return RISK_FREE_RATE_DEFAULT


# ============================================================
# PER-TICKER CALCULATION
# ============================================================
def process_expiration(ticker_obj, exp_str, current_price, risk_free_rate, prefix):
    """
    Process one expiration. Returns dict of fields to merge into result.
    prefix: 'weekly' or 'monthly'
    """
    out = {}
    chain = ticker_obj.option_chain(exp_str)
    calls = chain.calls
    puts = chain.puts

    if calls.empty or puts.empty:
        return out

    strikes = sorted(set(calls["strike"].tolist()) & set(puts["strike"].tolist()))
    if not strikes:
        return out

    # EM via ATM straddle
    atm_strike = get_atm_strike(current_price, strikes)
    atm_call = calls[calls["strike"] == atm_strike].iloc[0]
    atm_put = puts[puts["strike"] == atm_strike].iloc[0]
    call_mid = get_mid(atm_call["bid"], atm_call["ask"], atm_call["lastPrice"])
    put_mid = get_mid(atm_put["bid"], atm_put["ask"], atm_put["lastPrice"])
    em = calculate_em_straddle(call_mid, put_mid)

    dte = (datetime.strptime(exp_str, "%Y-%m-%d").date() - datetime.now().date()).days
    T = max(dte, 1) / 365.0

    out[f"{prefix}_em"] = round(em, 2)
    out[f"{prefix}_expiry"] = exp_str
    out[f"{prefix}_dte"] = dte
    out[f"{prefix}_upper"] = round(current_price + em, 2)
    out[f"{prefix}_lower"] = round(current_price - em, 2)
    out[f"{prefix}_iv"] = round(float(atm_call.get("impliedVolatility", 0)) * 100, 2)

    # 0.16 delta strikes with interpolation
    d16_call = find_delta_strike_interpolated(calls, current_price, T, risk_free_rate,
                                                TARGET_DELTA, 'call')
    d16_put = find_delta_strike_interpolated(puts, current_price, T, risk_free_rate,
                                               -TARGET_DELTA, 'put')

    if d16_call is not None:
        out[f"{prefix}_d16_call"] = d16_call
    if d16_put is not None:
        out[f"{prefix}_d16_put"] = d16_put

    return out


def calculate_expected_move(ticker_symbol, risk_free_rate):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.history(period="5d")
        if info.empty:
            print(f"  [SKIP] {ticker_symbol}: no price data")
            return None
        current_price = float(info["Close"].iloc[-1])

        expirations = ticker.options
        if not expirations:
            print(f"  [SKIP] {ticker_symbol}: no options chain")
            return None

        weekly_exp = find_next_friday_expiry(expirations)
        monthly_target = datetime.now().date() + timedelta(days=30)
        monthly_exp = find_next_friday_expiry(expirations, monthly_target)

        result = {
            "ticker": ticker_symbol,
            "price": round(current_price, 2),
            "updated": datetime.now().isoformat(),
        }

        if weekly_exp:
            result.update(process_expiration(ticker, weekly_exp, current_price,
                                              risk_free_rate, "weekly"))

        if monthly_exp and monthly_exp != weekly_exp:
            result.update(process_expiration(ticker, monthly_exp, current_price,
                                              risk_free_rate, "monthly"))

        if "weekly_em" not in result:
            print(f"  [SKIP] {ticker_symbol}: could not calculate weekly EM")
            return None

        # Log line showing key data
        d16c = result.get("weekly_d16_call", "n/a")
        d16p = result.get("weekly_d16_put", "n/a")
        d16c_s = f"${d16c:.2f}" if isinstance(d16c, (int, float)) else d16c
        d16p_s = f"${d16p:.2f}" if isinstance(d16p, (int, float)) else d16p
        print(f"  [OK]   {ticker_symbol}: ${current_price:.2f} | "
              f"EM±${result['weekly_em']:.2f} | Δ16C={d16c_s} | Δ16P={d16p_s}")
        return result

    except Exception as e:
        print(f"  [ERR]  {ticker_symbol}: {str(e)[:80]}")
        return None


# ============================================================
# MAIN
# ============================================================
def main():
    rfr = fetch_risk_free_rate()
    print(f"Risk-free rate: {rfr*100:.2f}%\n")

    if len(sys.argv) > 1:
        tickers = [t.upper() for t in sys.argv[1:]]
        print(f"Calculating for: {', '.join(tickers)}")
    else:
        tickers = list(dict.fromkeys(WATCHLIST))
        print(f"Calculating for {len(tickers)} watchlist tickers")

    print(f"Started at {datetime.now().isoformat()}\n")

    existing_data = {}
    if len(sys.argv) > 1:
        try:
            with open("expected_moves.json", "r") as f:
                existing = json.load(f)
                existing_data = {item["ticker"]: item for item in existing.get("tickers", [])}
        except FileNotFoundError:
            pass

    for symbol in tickers:
        em_data = calculate_expected_move(symbol, risk_free_rate=rfr)
        if em_data:
            existing_data[symbol] = em_data
        time.sleep(0.3)

    all_results = list(existing_data.values())
    output = {
        "generated_at": datetime.now().isoformat(),
        "risk_free_rate": round(rfr, 4),
        "count": len(all_results),
        "tickers": sorted(all_results, key=lambda x: x["ticker"]),
    }

    with open("expected_moves.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. Wrote {len(all_results)} tickers to expected_moves.json")


if __name__ == "__main__":
    main()
