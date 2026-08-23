"""
Expected Move + 0.16 Delta Calculator (v4)

Changes from v3:
  - Adds weekly_close / weekly_close_date (last COMPLETED weekly candle close)
  - weekly_upper / weekly_lower now anchored to weekly_close, not spot
  - Adds weekly_is_weekly flag (False when the "weekly" expiry is really a monthly)
  - Retry logic on option_chain fetches + a second pass for tickers missing monthly data

Output: expected_moves.json
"""

import yfinance as yf
import json
import math
import sys
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

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
MAX_WEEKLY_DTE = 10        # above this, the "weekly" expiry isn't really a weekly
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0        # seconds
THROTTLE = 0.5             # seconds between tickers


# ============================================================
# RETRY HELPER
# ============================================================
def with_retry(fn, label="", attempts=RETRY_ATTEMPTS, backoff=RETRY_BACKOFF):
    """Call fn(), retrying on exception. Returns None if all attempts fail."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                print(f"         retry exhausted {label}: {str(e)[:60]}")
                return None
            time.sleep(backoff * (i + 1))
    return None


# ============================================================
# WEEKLY CLOSE (batch)
# ============================================================
def weekly_closes(symbols):
    """
    {sym: (close, 'YYYY-MM-DD')} for the last COMPLETED weekly candle.
    One batched download rather than a call per ticker.
    """
    out = {}
    data = with_retry(
        lambda: yf.download(symbols, period="2mo", interval="1d",
                            group_by="ticker", auto_adjust=False,
                            threads=True, progress=False),
        label="weekly_closes batch"
    )
    if data is None or len(data) == 0:
        print("  [WARN] weekly close batch failed, all tickers will lack weekly_close")
        return out

    now = datetime.now(ET)
    week_closed = now.weekday() >= 5 or (now.weekday() == 4 and now.time() >= dtime(16, 0))
    this_monday = (now - timedelta(days=now.weekday())).date()

    for sym in symbols:
        try:
            df = data[sym] if len(symbols) > 1 else data
            df = df.dropna(subset=["Close"])
        except (KeyError, TypeError):
            continue
        if df.empty:
            continue

        tmp = df[["Close"]].copy()
        tmp["date"] = tmp.index.date
        w = tmp.resample("W-FRI").last().dropna()

        # Drop the in-progress week ONLY if the last bin holds a current-week bar.
        # Position-based dropping breaks on Monday runs (deletes last Friday).
        if not week_closed and not w.empty and w["date"].iloc[-1] >= this_monday:
            w = w.iloc[:-1]

        if w.empty:
            continue
        out[sym] = (round(float(w["Close"].iloc[-1]), 2), str(w["date"].iloc[-1]))

    print(f"  Weekly closes resolved for {len(out)}/{len(symbols)} tickers")
    return out


# ============================================================
# BLACK-SCHOLES DELTA
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


def find_delta_strike_interpolated(chain_df, S, T, r, target_delta, option_type="call"):
    """
    Delta at each strike using that strike's OWN IV (captures skew).
    Interpolates between the two strikes bracketing target_delta.
    Rounds to nearest $0.10.
    """
    points = []
    for _, row in chain_df.iterrows():
        K = row.get("strike")
        sigma = row.get("impliedVolatility")
        if K is None or sigma is None:
            continue
        if sigma <= 0.01 or sigma > 5.0:
            continue
        d = (call_delta(S, K, T, r, sigma) if option_type == "call"
             else put_delta(S, K, T, r, sigma))
        points.append((float(K), float(d)))

    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])

    best_pair = None
    for i in range(len(points) - 1):
        k1, d1 = points[i]
        k2, d2 = points[i + 1]
        lo, hi = (d1, d2) if d1 < d2 else (d2, d1)
        if lo <= target_delta <= hi:
            best_pair = ((k1, d1), (k2, d2))
            break

    if best_pair is None:
        closest = min(points, key=lambda p: abs(p[1] - target_delta))
        return round(closest[0] * 10) / 10

    (k1, d1), (k2, d2) = best_pair
    if abs(d2 - d1) < 1e-9:
        interpolated = (k1 + k2) / 2.0
    else:
        t = (target_delta - d1) / (d2 - d1)
        interpolated = k1 + t * (k2 - k1)

    return round(interpolated * 10) / 10


# ============================================================
# STRADDLE / EXPIRY HELPERS
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


def fetch_risk_free_rate():
    def _get():
        hist = yf.Ticker("^IRX").history(period="5d")
        if hist.empty:
            return None
        rate = float(hist["Close"].iloc[-1]) / 100.0
        return rate if 0 < rate < 0.20 else None
    rate = with_retry(_get, label="^IRX")
    return rate if rate else RISK_FREE_RATE_DEFAULT


# ============================================================
# PER-EXPIRATION PROCESSING
# ============================================================
def process_expiration(ticker_obj, exp_str, spot, anchor, risk_free_rate, prefix):
    """
    spot   = live price, used for delta strike selection (chain is priced off spot)
    anchor = reference price for the bands (weekly_close when available, else spot)
    """
    out = {}
    chain = with_retry(lambda: ticker_obj.option_chain(exp_str),
                       label=f"{prefix} chain {exp_str}")
    if chain is None:
        return out

    calls, puts = chain.calls, chain.puts
    if calls.empty or puts.empty:
        return out

    strikes = sorted(set(calls["strike"].tolist()) & set(puts["strike"].tolist()))
    if not strikes:
        return out

    atm_strike = get_atm_strike(spot, strikes)
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
    # Bands anchored to the reference price, NOT spot
    out[f"{prefix}_upper"] = round(anchor + em, 2)
    out[f"{prefix}_lower"] = round(anchor - em, 2)
    out[f"{prefix}_iv"] = round(float(atm_call.get("impliedVolatility", 0)) * 100, 2)

    if prefix == "weekly":
        # False when no true weekly chain exists and we fell through to a monthly
        out["weekly_is_weekly"] = dte <= MAX_WEEKLY_DTE

    d16_call = find_delta_strike_interpolated(calls, spot, T, risk_free_rate,
                                              TARGET_DELTA, "call")
    d16_put = find_delta_strike_interpolated(puts, spot, T, risk_free_rate,
                                             -TARGET_DELTA, "put")
    if d16_call is not None:
        out[f"{prefix}_d16_call"] = d16_call
    if d16_put is not None:
        out[f"{prefix}_d16_put"] = d16_put

    return out


def calculate_expected_move(symbol, risk_free_rate, wclose_map):
    try:
        ticker = yf.Ticker(symbol)

        hist = with_retry(lambda: ticker.history(period="5d"), label=f"{symbol} history")
        if hist is None or hist.empty:
            print(f"  [SKIP] {symbol}: no price data")
            return None
        spot = float(hist["Close"].iloc[-1])

        expirations = with_retry(lambda: ticker.options, label=f"{symbol} expirations")
        if not expirations:
            print(f"  [SKIP] {symbol}: no options chain")
            return None

        wclose, wclose_date = wclose_map.get(symbol, (None, None))
        anchor = wclose if wclose is not None else spot

        result = {
            "ticker": symbol,
            "price": round(spot, 2),
            "weekly_close": wclose,
            "weekly_close_date": wclose_date,
            "updated": datetime.now().isoformat(),
        }

        weekly_exp = find_next_friday_expiry(expirations)
        monthly_target = datetime.now().date() + timedelta(days=30)
        monthly_exp = find_next_friday_expiry(expirations, monthly_target)

        if weekly_exp:
            result.update(process_expiration(ticker, weekly_exp, spot, anchor,
                                             risk_free_rate, "weekly"))
        if monthly_exp and monthly_exp != weekly_exp:
            result.update(process_expiration(ticker, monthly_exp, spot, anchor,
                                             risk_free_rate, "monthly"))

        if "weekly_em" not in result:
            print(f"  [SKIP] {symbol}: could not calculate weekly EM")
            return None

        anchor_tag = "wk" if wclose is not None else "spot"
        wk_flag = "" if result.get("weekly_is_weekly", True) else " [NOT-WEEKLY]"
        mo_flag = "" if "monthly_em" in result else " [no monthly]"
        print(f"  [OK]   {symbol}: ${spot:.2f} (anchor {anchor_tag} {anchor:.2f}) "
              f"EM±${result['weekly_em']:.2f}{wk_flag}{mo_flag}")
        return result

    except Exception as e:
        print(f"  [ERR]  {symbol}: {str(e)[:70]}")
        return None


# ============================================================
# MAIN
# ============================================================
def main():
    rfr = fetch_risk_free_rate()
    print(f"Risk-free rate: {rfr*100:.2f}%")

    single_run = len(sys.argv) > 1
    if single_run:
        tickers = [t.upper() for t in sys.argv[1:]]
        print(f"Calculating for: {', '.join(tickers)}")
    else:
        tickers = list(dict.fromkeys(WATCHLIST))
        print(f"Calculating for {len(tickers)} watchlist tickers")

    print(f"Started {datetime.now().isoformat()}\n")

    print("Fetching weekly closes...")
    wclose_map = weekly_closes(tickers)
    print()

    existing = {}
    if single_run:
        try:
            with open("expected_moves.json") as f:
                prev = json.load(f)
                existing = {t["ticker"]: t for t in prev.get("tickers", [])}
        except FileNotFoundError:
            pass

    for symbol in tickers:
        rec = calculate_expected_move(symbol, rfr, wclose_map)
        if rec:
            existing[symbol] = rec
        time.sleep(THROTTLE)

    # ---- Second pass: retry anything missing monthly data ----
    incomplete = [s for s in tickers
                  if s in existing and "monthly_em" not in existing[s]]
    if incomplete:
        print(f"\nSecond pass for {len(incomplete)} tickers missing monthly data:")
        print(f"  {', '.join(incomplete)}")
        time.sleep(3)
        recovered = 0
        for symbol in incomplete:
            rec = calculate_expected_move(symbol, rfr, wclose_map)
            if rec and "monthly_em" in rec:
                existing[symbol] = rec
                recovered += 1
            time.sleep(THROTTLE)
        print(f"  Recovered monthly data for {recovered}/{len(incomplete)}")

    results = list(existing.values())
    no_monthly = [t["ticker"] for t in results if "monthly_em" not in t]
    no_wclose = [t["ticker"] for t in results if t.get("weekly_close") is None]
    not_weekly = [t["ticker"] for t in results if t.get("weekly_is_weekly") is False]

    output = {
        "generated_at": datetime.now().isoformat(),
        "risk_free_rate": round(rfr, 4),
        "count": len(results),
        "tickers": sorted(results, key=lambda x: x["ticker"]),
    }
    with open("expected_moves.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(results)} tickers to expected_moves.json")
    if no_wclose:
        print(f"  No weekly_close ({len(no_wclose)}): {', '.join(no_wclose)}")
    if no_monthly:
        print(f"  No monthly data ({len(no_monthly)}): {', '.join(no_monthly)}")
    if not_weekly:
        print(f"  Weekly expiry is NOT a weekly ({len(not_weekly)}): {', '.join(not_weekly)}")


if __name__ == "__main__":
    main()
