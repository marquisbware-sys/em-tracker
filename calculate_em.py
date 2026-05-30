"""
Expected Move Calculator
Pulls ATM straddle prices from yfinance every Friday 4pm CT and Sunday 6pm CT.
Also runs daily at 9:45am ET to capture fresh 0DTE (daily) expected moves.
Calculates Daily EM (0DTE/next-day), Weekly EM (next Friday expiry) and Monthly EM (~30 DTE).
All EMs use straddle x 0.85.
Writes results to expected_moves.json and calls generate_pine.py to regenerate expected_move.pine.
When fresh options data cannot be fetched (e.g. market closed), the script preserves the
previously stored EM values so the dashboard always shows the last known good data.
"""

import json
import sys
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd

WATCHLIST = [
        # --- Broad Market ETFs ---
        "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "SPX",
        # --- Sector ETFs ---
        "XLF", "XLK", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE",
        # --- Mega-Cap Tech ---
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        # --- Large-Cap Tech ---
        "AMD", "INTC", "AVGO", "QCOM", "ORCL", "CRM", "ADBE", "NFLX",
        "MU", "AMAT", "KLAC", "LRCX", "SNPS", "CDNS",
        # --- Leveraged / High-Vol ETFs ---
        "TSLL", "TQQQ", "SQQQ", "UVXY", "SPXL", "SPXS", "SOXL", "SOXS",
        "MSTU", "NVDL",
        # --- Financials ---
        "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "V", "MA", "AXP",
        # --- Healthcare ---
        "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY", "AMGN",
        # --- Energy ---
        "XOM", "CVX", "COP", "OXY", "SLB", "HAL",
        # --- Commodities & Alternatives ---
        "GLD", "SLV", "USO", "GDX", "GDXJ", "UNG",
        # --- Consumer & Retail ---
        "WMT", "TGT", "COST", "HD", "LOW", "AMZN",
        # --- Discretionary / Media ---
        "DIS", "NFLX", "ROKU",
        # --- Crypto-adjacent ---
        "COIN", "MSTR", "HOOD", "IBIT",
        # --- High-Beta / Meme / Growth ---
        "PLTR", "SOFI", "RIVN", "LCID", "GME", "AMC",
        # --- Industrials / Defense ---
        "BA", "RTX", "LMT", "CAT",
        # --- Telecom / Utilities ---
        "T", "VZ",
]

# EM fields to carry forward from previous run when options data is unavailable
EM_CARRY_FIELDS = [
        "daily_em", "daily_upper", "daily_lower",
        "weekly_em", "weekly_upper", "weekly_lower",
        "monthly_em", "monthly_upper", "monthly_lower",
        "weekly_iv",
]

def load_existing_json(path="expected_moves.json"):
        """Load the existing JSON file and return a dict keyed by ticker symbol."""
        try:
                with open(path, "r") as f:
                        data = json.load(f)
                return {t["ticker"]: t for t in data.get("tickers", [])}
        except Exception:
                return {}

def get_next_friday(from_date=None):
        """Get the next Friday from today (or from_date)."""
        if from_date is None:
                from_date = datetime.now().date()
        days_ahead = 4 - from_date.weekday()  # 4 = Friday
        if days_ahead <= 0:
                days_ahead += 7
        return from_date + timedelta(days=days_ahead)

def get_daily_expiry(from_date=None):
        """Get today's date for 0DTE options, or tomorrow if today is weekend."""
        if from_date is None:
                from_date = datetime.now().date()
        # If weekend, return Monday
        if from_date.weekday() == 5:  # Saturday
                return from_date + timedelta(days=2)
        elif from_date.weekday() == 6:  # Sunday
                return from_date + timedelta(days=1)
        return from_date

def get_monthly_expiry(from_date=None, target_dte=30):
        """Get the expiry ~30 DTE from today."""
        if from_date is None:
                from_date = datetime.now().date()
        target_date = from_date + timedelta(days=target_dte)
        return target_date

def get_straddle_price(ticker, exp_str, price):
        """Get ATM straddle price for a given expiry. Returns (straddle, iv) or (None, None)."""
        try:
                chain = ticker.option_chain(exp_str)
                calls = chain.calls
                puts = chain.puts

                if calls.empty or puts.empty:
                        return None, None

                # Find ATM strike (closest to current price)
                strikes = sorted(set(calls['strike'].values) & set(puts['strike'].values))
                if not strikes:
                        return None, None

                atm_strike = min(strikes, key=lambda x: abs(x - price))

                call_row = calls[calls['strike'] == atm_strike].iloc[0]
                put_row = puts[puts['strike'] == atm_strike].iloc[0]

                call_mid = (float(call_row['bid']) + float(call_row['ask'])) / 2
                put_mid = (float(put_row['bid']) + float(put_row['ask'])) / 2

                straddle = call_mid + put_mid

                # Estimate IV from the call
                iv = None
                try:
                        iv_val = float(call_row.get('impliedVolatility', 0))
                        if iv_val and iv_val > 0:
                                iv = round(iv_val * 100, 1)
                except Exception:
                        pass

                return straddle, iv
        except Exception:
                return None, None

def calculate_em_for_ticker(ticker_sym, previous=None):
        """Calculate expected move for a single ticker.
        If fresh options data cannot be fetched, carries forward EM values from
        the previous run so the dashboard never shows blank data.
        """
        try:
                ticker = yf.Ticker(ticker_sym)

                # Get current price
                info = ticker.fast_info
                price = float(info.last_price)

                if not price or price <= 0:
                        print(f"  Warning: Could not get price for {ticker_sym}")
                        return None

                # Get options chain
                expirations = ticker.options
                if not expirations:
                        print(f"  Warning: No options data for {ticker_sym} - carrying forward previous EM values")
                        return _build_result_with_carried_values(ticker_sym, price, previous)

                today = datetime.now().date()
                daily_target = get_daily_expiry(today)
                next_friday = get_next_friday(today)
                monthly_target = get_monthly_expiry(today)

                # Find daily expiry (0DTE = today, or next trading day)
                daily_exp = None
                for exp in expirations:
                        exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                        if exp_date >= daily_target:
                                daily_exp = exp
                                break

                # Find weekly expiry (next Friday or closest)
                weekly_exp = None
                for exp in expirations:
                        exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                        if exp_date >= next_friday:
                                weekly_exp = exp
                                break

                # Find monthly expiry (~30 DTE, closest available)
                monthly_exp = None
                best_diff = float('inf')
                for exp in expirations:
                        exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                        diff = abs((exp_date - monthly_target).days)
                        if diff < best_diff:
                                best_diff = diff
                                monthly_exp = exp

                result = {
                        "ticker": ticker_sym,
                        "price": round(price, 2),
                        "daily_exp": daily_exp,
                        "weekly_exp": weekly_exp,
                        "monthly_exp": monthly_exp,
                }

                # --- Daily EM ---
                daily_straddle, _ = get_straddle_price(ticker, daily_exp, price) if daily_exp else (None, None)
                if daily_straddle and daily_straddle > 0:
                        result["daily_em"] = round(daily_straddle * 0.85, 2)
                        result["daily_upper"] = round(price + result["daily_em"], 2)
                        result["daily_lower"] = round(price - result["daily_em"], 2)
                elif previous:
                        for field in ["daily_em", "daily_upper", "daily_lower"]:
                                if field in previous:
                                        result[field] = previous[field]

                # --- Weekly EM ---
                weekly_straddle, weekly_iv = get_straddle_price(ticker, weekly_exp, price) if weekly_exp else (None, None)
                if weekly_straddle and weekly_straddle > 0:
                        result["weekly_em"] = round(weekly_straddle * 0.85, 2)
                        result["weekly_upper"] = round(price + result["weekly_em"], 2)
                        result["weekly_lower"] = round(price - result["weekly_em"], 2)
                        if weekly_iv is not None:
                                result["weekly_iv"] = weekly_iv
                elif previous:
                        for field in ["weekly_em", "weekly_upper", "weekly_lower", "weekly_iv"]:
                                if field in previous:
                                        result[field] = previous[field]

                # --- Monthly EM ---
                monthly_straddle, _ = get_straddle_price(ticker, monthly_exp, price) if monthly_exp else (None, None)
                if monthly_straddle and monthly_straddle > 0:
                        result["monthly_em"] = round(monthly_straddle * 0.85, 2)
                        result["monthly_upper"] = round(price + result["monthly_em"], 2)
                        result["monthly_lower"] = round(price - result["monthly_em"], 2)
                elif previous:
                        for field in ["monthly_em", "monthly_upper", "monthly_lower"]:
                                if field in previous:
                                        result[field] = previous[field]

                return result

        except Exception as e:
                print(f"  Error processing {ticker_sym}: {e}")
                return None

def _build_result_with_carried_values(ticker_sym, price, previous, daily_exp=None, weekly_exp=None, monthly_exp=None):
        """Build a result entry using the current price but carrying forward previous EM values."""
        result = {
                "ticker": ticker_sym,
                "price": round(price, 2),
                "daily_exp": daily_exp,
                "weekly_exp": weekly_exp,
                "monthly_exp": monthly_exp,
        }
        if previous:
                for field in EM_CARRY_FIELDS:
                        if field in previous:
                                result[field] = previous[field]
        return result

def main(single_ticker=None):
        """Main function to calculate EMs and write JSON."""
        if single_ticker:
                watchlist = [single_ticker.upper()]
        else:
                watchlist = WATCHLIST

        print(f"Processing {len(watchlist)} tickers...")

        # Load existing data to carry forward EM values when options data is unavailable
        existing_data = load_existing_json()
        print(f"Loaded {len(existing_data)} existing ticker records for carry-forward")

        results = []
        fresh_count = 0
        carried_count = 0

        for sym in watchlist:
                print(f"Processing {sym}...")
                previous = existing_data.get(sym)
                result = calculate_em_for_ticker(sym, previous=previous)
                if result:
                        results.append(result)
                        has_fresh = (result.get("weekly_em") and
                                     previous and
                                     result.get("weekly_em") != previous.get("weekly_em"))
                        if not previous or has_fresh:
                                fresh_count += 1
                                print(f"  {sym}: price={result['price']}, daily_em={result.get('daily_em', 'N/A')}, weekly_em={result.get('weekly_em', 'N/A')}, monthly_em={result.get('monthly_em', 'N/A')}")
                        else:
                                carried_count += 1
                                print(f"  {sym}: carried forward (price updated to {result['price']})")

        output = {
                "generated_at": datetime.utcnow().isoformat(),
                "count": len(results),
                "tickers": results
        }

        with open("expected_moves.json", "w") as f:
                json.dump(output, f, indent=2)

        print(f"\nWrote {len(results)} tickers to expected_moves.json")
        print(f"  Fresh EM data: {fresh_count} tickers")
        print(f"  Carried forward: {carried_count} tickers")

        # Regenerate Pine script
        try:
                import generate_pine
                generate_pine.generate_pine_script()
                print("Regenerated expected_move.pine")
        except Exception as e:
                print(f"Warning: Could not regenerate Pine script: {e}")

if __name__ == "__main__":
        single_ticker = sys.argv[1] if len(sys.argv) > 1 else None
        main(single_ticker)
