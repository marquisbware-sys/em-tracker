"""
Expected Move + 0.16 Delta Calculator (v5)

v5 fixes the NaN-price failure:
  - spot now resolves from the batch download first, then ticker.history(),
    then weekly_close. A ticker with no finite spot is SKIPPED, not priced
    off a NaN (which silently picked the lowest strike in the chain).
  - NaN / inf can never reach the JSON. Output is sanitized and dumped with
    allow_nan=False, so an invalid file fails loudly instead of breaking the
    dashboard's JSON.parse().
  - Sanity flag on absurd EM values (em_suspect).

Carried over from v4:
  - weekly_close / weekly_close_date (last COMPLETED weekly candle)
  - bands anchored to weekly_close, not spot
  - weekly_is_weekly flag
  - retry logic + second pass for tickers missing monthly data

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
MAX_WEEKLY_DTE = 10        # above this, the "weekly" expiry is really a monthly
EM_SUSPECT_RATIO = 0.25    # weekly EM above this fraction of spot gets flagged
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0
THROTTLE = 0.5


# ============================================================
# NUMERIC SAFETY
# ============================================================
def safe_float(x):
    """Return a finite float, or None. Never NaN, never inf."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def sanitize(obj):
    """Recursively replace any non-finite float with None."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


# ============================================================
# RETRY HELPER
# ============================================================
def with_retry(fn, label="", attempts=RETRY_ATTEMPTS, backoff=RETRY_BACKOFF):
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
# BATCH DAILY HISTORY -> weekly close + last close (spot source)
# ============================================================
def batch_price_data(symbols):
    """
    {sym: {"weekly_close": float|None,
           "weekly_close_date": str|None,
           "last_close": float|None}}

    weekly_close = last COMPLETED weekly candle.
    last_close   = most recent daily close, used as the primary spot source
                   because ticker.history() has been returning NaN.
    """
    out = {}
    data = with_retry(
        lambda: yf.download(symbols, period="3mo", interval="1d",
                            group_by="ticker", auto_adjust=False,
                            threads=True, progress=False),
        label="batch download"
    )
    if data is None or len(data) == 0:
        print("  [WARN] batch download failed, no weekly_close and no spot fallback")
        return out

    now = datetime.now(ET)
    week_closed = now.weekday() >= 5 or (now.weekday() == 4 and now.time() >= dtime(16, 0))
    this_monday = (now - timedelta(days=now.weekday())).date()

    for sym in symbols:
        rec = {"weekly_close": None, "weekly_close_date": None, "last_close": None}
        try:
            df = data[sym] if len(symbols) > 1 else data
            df = df.dropna(subset=["Close"])
        except (KeyError, TypeError):
            out[sym] = rec
            continue
        if df.empty:
            out[sym] = rec
            continue

        rec["last_close"] = safe_float(df["Close"].iloc[-1])

        tmp = df[["Close"]].copy()
        tmp["date"] = tmp.index.date
        w = tmp.resample("W-FRI").last().dropna()

        # Drop the in-progress week ONLY when the last bin holds a current-week
        # bar. Dropping by position breaks Monday runs (deletes last Friday).
        if not week_closed and not w.empty and w["date"].iloc[-1] >= this_monday:
            w = w.iloc[:-1]

        if not w.empty:
            wc = safe_float(w["Close"].iloc[-1])
            if wc is not None:
                rec["weekly_close"] = round(wc, 2)
                rec["weekly_close_date"] = str(w["date"].iloc[-1])

        out[sym] = rec

    have_wc = sum(1 for r in out.values() if r["weekly_close"] is not None)
    have_lc = sum(1 for r in out.values() if r["last_close"] is not None)
    print(f"  Weekly close: {have_wc}/{len(symbols)}   Last close: {have_lc}/{len(symbols)}")
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
    if S is None or not math.isfinite(S) or S <= 0:
        return None

    points = []
    for _, row in chain_df.iterrows():
        K = safe_float(row.get("strike"))
        sigma = safe_float(row.get("impliedVolatility"))
        if K is None or sigma is None:
            continue
        if sigma <= 0.01 or sigma > 5.0:
            continue
        d = (call_delta(S, K, T, r, sigma) if option_type == "call"
             else put_delta(S, K, T, r, sigma))
        points.append((K, d))

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
    """Closest strike to price. Caller must guarantee price is finite."""
    return min(strikes, key=lambda s: abs(s - price))


def calculate_em_straddle(call_mid, put_mid):
    return (call_mid + put_mid) * 0.85


def get_mid(bid, ask, last):
    b, a, l = safe_float(bid), safe_float(ask), safe_float(last)
    if b and a and b > 0 and a > 0:
        return (b + a) / 2
    return l if l else 0.0


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
        rate = safe_float(hist["Close"].iloc[-1])
        if rate is None:
            return None
        rate = rate / 100.0
        return rate if 0 < rate < 0.20 else None
    rate = with_retry(_get, label="^IRX")
    return rate if rate else RISK_FREE_RATE_DEFAULT


# ============================================================
# PER-EXPIRATION PROCESSING
# ============================================================
def process_expiration(ticker_obj, exp_str, spot, anchor, risk_free_rate, prefix):
    """
    spot   = live price, used to pick the ATM strike and compute deltas
    anchor = reference price for the bands (weekly_close when available)
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
    strikes = [s for s in strikes if safe_float(s) is not None]
    if not strikes:
        return out

    atm_strike = get_atm_strike(spot, strikes)
    atm_call = calls[calls["strike"] == atm_strike].iloc[0]
    atm_put = puts[puts["strike"] == atm_strike].iloc[0]
    call_mid = get_mid(atm_call["bid"], atm_call["ask"], atm_call["lastPrice"])
    put_mid = get_mid(atm_put["bid"], atm_put["ask"], atm_put["lastPrice"])

    em = safe_float(calculate_em_straddle(call_mid, put_mid))
    if em is None or em <= 0:
        return out

    dte = (datetime.strptime(exp_str, "%Y-%m-%d").date() - datetime.now().date()).days
    T = max(dte, 1) / 365.0

    out[f"{prefix}_em"] = round(em, 2)
    out[f"{prefix}_expiry"] = exp_str
    out[f"{prefix}_dte"] = dte
    out[f"{prefix}_atm_strike"] = round(float(atm_strike), 2)
    out[f"{prefix}_upper"] = round(anchor + em, 2)
    out[f"{prefix}_lower"] = round(anchor - em, 2)

    iv = safe_float(atm_call.get("impliedVolatility"))
    out[f"{prefix}_iv"] = round(iv * 100, 2) if iv is not None else None

    if prefix == "weekly":
        out["weekly_is_weekly"] = dte <= MAX_WEEKLY_DTE
        out["em_suspect"] = (em / anchor) > EM_SUSPECT_RATIO if anchor else True

    d16_call = find_delta_strike_interpolated(calls, spot, T, risk_free_rate,
                                              TARGET_DELTA, "call")
    d16_put = find_delta_strike_interpolated(puts, spot, T, risk_free_rate,
                                             -TARGET_DELTA, "put")
    if d16_call is not None:
        out[f"{prefix}_d16_call"] = d16_call
    if d16_put is not None:
        out[f"{prefix}_d16_put"] = d16_put

    return out


def resolve_spot(symbol, ticker_obj, batch_rec):
    """
    Spot resolution order:
      1. batch download last daily close  (works today)
      2. ticker.history()                 (currently returning NaN)
      3. weekly_close                     (stale but finite)
    Returns (spot, source) or (None, None).
    """
    lc = batch_rec.get("last_close")
    if lc is not None and lc > 0:
        return lc, "batch"

    hist = with_retry(lambda: ticker_obj.history(period="5d"), label=f"{symbol} history")
    if hist is not None and not hist.empty:
        h = safe_float(hist["Close"].iloc[-1])
        if h is not None and h > 0:
            return h, "history"

    wc = batch_rec.get("weekly_close")
    if wc is not None and wc > 0:
        return wc, "weekly_close"

    return None, None


def calculate_expected_move(symbol, risk_free_rate, price_map):
    try:
        ticker = yf.Ticker(symbol)
        batch_rec = price_map.get(symbol, {})

        spot, spot_source = resolve_spot(symbol, ticker, batch_rec)
        if spot is None:
            print(f"  [SKIP] {symbol}: no finite spot price from any source")
            return None

        expirations = with_retry(lambda: ticker.options, label=f"{symbol} expirations")
        if not expirations:
            print(f"  [SKIP] {symbol}: no options chain")
            return None

        wclose = batch_rec.get("weekly_close")
        wclose_date = batch_rec.get("weekly_close_date")
        anchor = wclose if wclose is not None else spot

        result = {
            "ticker": symbol,
            "price": round(spot, 2),
            "price_source": spot_source,
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

        flags = ""
        if result.get("em_suspect"):
            flags += " [EM SUSPECT]"
        if result.get("weekly_is_weekly") is False:
            flags += " [NOT-WEEKLY]"
        if "monthly_em" not in result:
            flags += " [no monthly]"
        if spot_source != "batch":
            flags += f" [spot:{spot_source}]"

        print(f"  [OK]   {symbol}: ${spot:.2f}  anchor {anchor:.2f}  "
              f"EM±${result['weekly_em']:.2f}{flags}")
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

    print("Fetching batch price history...")
    price_map = batch_price_data(tickers)
    print()

    existing = {}
    if single_run:
        try:
            with open("expected_moves.json") as f:
                prev = json.load(f)
                existing = {t["ticker"]: t for t in prev.get("tickers", [])}
        except (FileNotFoundError, ValueError):
            pass

    for symbol in tickers:
        rec = calculate_expected_move(symbol, rfr, price_map)
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
            rec = calculate_expected_move(symbol, rfr, price_map)
            if rec and "monthly_em" in rec:
                existing[symbol] = rec
                recovered += 1
            time.sleep(THROTTLE)
        print(f"  Recovered monthly data for {recovered}/{len(incomplete)}")

    results = [sanitize(r) for r in existing.values()]

    no_monthly = [t["ticker"] for t in results if t.get("monthly_em") is None]
    no_wclose = [t["ticker"] for t in results if t.get("weekly_close") is None]
    not_weekly = [t["ticker"] for t in results if t.get("weekly_is_weekly") is False]
    suspect = [t["ticker"] for t in results if t.get("em_suspect")]
    odd_spot = [t["ticker"] for t in results if t.get("price_source") != "batch"]

    output = sanitize({
        "generated_at": datetime.now().isoformat(),
        "risk_free_rate": round(rfr, 4),
        "count": len(results),
        "tickers": sorted(results, key=lambda x: x["ticker"]),
    })

    # allow_nan=False: if any non-finite value survived, fail loudly here
    # rather than writing invalid JSON that breaks the dashboard.
    with open("expected_moves.json", "w") as f:
        json.dump(output, f, indent=2, allow_nan=False)

    print(f"\nWrote {len(results)} tickers to expected_moves.json")
    if odd_spot:
        print(f"  Spot NOT from batch ({len(odd_spot)}): {', '.join(odd_spot)}")
    if no_wclose:
        print(f"  No weekly_close ({len(no_wclose)}): {', '.join(no_wclose)}")
    if no_monthly:
        print(f"  No monthly data ({len(no_monthly)}): {', '.join(no_monthly)}")
    if not_weekly:
        print(f"  Weekly expiry is NOT a weekly ({len(not_weekly)}): {', '.join(not_weekly)}")
    if suspect:
        print(f"  *** EM SUSPECT, >{EM_SUSPECT_RATIO:.0%} of spot "
              f"({len(suspect)}): {', '.join(suspect)}")


if __name__ == "__main__":
    main()
