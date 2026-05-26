"""
Expected Move Calculator
Pulls ATM straddle prices from yfinance every Friday 4pm CT and Sunday 6pm CT.
Calculates Weekly EM (next Friday expiry) and Monthly EM (~30 DTE) using straddle × 0.85.
Writes results to expected_moves.json and calls generate_pine.py to regenerate expected_move.pine.
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

def get_next_friday(from_date=None):
    """Get the next Friday from today (or from_date)."""
    if from_date is None:
        from_date = datetime.now().date()
    days_ahead = 4 - from_date.weekday()  # 4 = Friday
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)

def get_monthly_expiry(from_date=None, target_dte=30):
    """Get the expiry ~30 DTE from today."""
    if from_date is None:
        from_date = datetime.now().date()
    target_date = from_date + timedelta(days=target_dte)
    return target_date

def calculate_em_for_ticker(ticker_sym):
    """Calculate expected move for a single ticker."""
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
            print(f"  Warning: No options data for {ticker_sym}")
            return None
        
        today = datetime.now().date()
        next_friday = get_next_friday(today)
        monthly_target = get_monthly_expiry(today)
        
        # Find weekly expiry (next Friday or closest)
        weekly_exp = None
        for exp in expirations:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            if exp_date >= next_friday:
                weekly_exp = exp
                break
        
        # Find monthly expiry (~30 DTE)
        monthly_exp = None
        best_diff = float('inf')
        for exp in expirations:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            diff = abs((exp_date - monthly_target).days)
            if diff < best_diff:
                best_diff = diff
                monthly_exp = exp
        
        if not weekly_exp or not monthly_exp:
            print(f"  Warning: Could not find expiry for {ticker_sym}")
            return None
        
        def get_straddle_price(expiry):
            """Get ATM straddle price for an expiry."""
            try:
                chain = ticker.option_chain(expiry)
                calls = chain.calls
                puts = chain.puts
                
                # Find ATM strike (closest to current price)
                strikes = sorted(set(calls['strike'].values) & set(puts['strike'].values))
                if not strikes:
                    return None
                
                atm_strike = min(strikes, key=lambda x: abs(x - price))
                
                call_row = calls[calls['strike'] == atm_strike].iloc[0]
                put_row = puts[puts['strike'] == atm_strike].iloc[0]
                
                call_mid = (float(call_row['bid']) + float(call_row['ask'])) / 2
                put_mid = (float(put_row['bid']) + float(put_row['ask'])) / 2
                
                straddle = call_mid + put_mid
                return straddle
            except Exception as e:
                print(f"    Error getting chain for {expiry}: {e}")
                return None
        
        weekly_straddle = get_straddle_price(weekly_exp)
        monthly_straddle = get_straddle_price(monthly_exp)
        
        if weekly_straddle is None and monthly_straddle is None:
            return None
        
        result = {
            "ticker": ticker_sym,
            "price": round(price, 2),
            "weekly_exp": weekly_exp,
            "monthly_exp": monthly_exp,
        }
        
        if weekly_straddle:
            result["weekly_em"] = round(weekly_straddle * 0.85, 2)
            result["weekly_upper"] = round(price + result["weekly_em"], 2)
            result["weekly_lower"] = round(price - result["weekly_em"], 2)
        
        if monthly_straddle:
            result["monthly_em"] = round(monthly_straddle * 0.85, 2)
            result["monthly_upper"] = round(price + result["monthly_em"], 2)
            result["monthly_lower"] = round(price - result["monthly_em"], 2)
        
        return result
    
    except Exception as e:
        print(f"  Error processing {ticker_sym}: {e}")
        return None

def main(single_ticker=None):
    """Main function to calculate EMs and write JSON."""
    if single_ticker:
        watchlist = [single_ticker.upper()]
    else:
        watchlist = WATCHLIST
    
    print(f"Processing {len(watchlist)} tickers...")
    
    results = []
    for sym in watchlist:
        print(f"Processing {sym}...")
        result = calculate_em_for_ticker(sym)
        if result:
            results.append(result)
            print(f"  {sym}: price={result['price']}, weekly_em={result.get('weekly_em', 'N/A')}, monthly_em={result.get('monthly_em', 'N/A')}")
        else:
            print(f"  {sym}: FAILED")
    
    # If adding a single ticker, merge with existing JSON instead of overwriting
    if single_ticker and results:
        try:
            with open("expected_moves.json", "r") as f:
                existing = json.load(f)
            existing_tickers = existing.get("tickers", [])
            # Remove old entry for this ticker if present
            ticker_upper = single_ticker.upper()
            existing_tickers = [t for t in existing_tickers if t.get("ticker") != ticker_upper]
            # Append new result
            existing_tickers.append(results[0])
            results = existing_tickers
            print(f"  Merged {ticker_upper} into existing {len(existing_tickers)} tickers")
        except Exception as e:
            print(f"  Warning: Could not read existing JSON, writing fresh: {e}")
    
    # Write JSON
    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "count": len(results),
        "tickers": results
    }
    
    with open("expected_moves.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nWrote {len(results)} tickers to expected_moves.json")
    
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
