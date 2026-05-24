# Expected Move Tracker

Automated weekly + monthly expected move calculator for your TradingView charts. Uses free yfinance data, runs on GitHub Actions, completely free to operate.

## What it does

1. Python script pulls ATM straddle prices from yfinance every Friday 4pm CT and Sunday 6pm CT
2. Calculates Weekly EM (next Friday expiry) and Monthly EM (~30 DTE) using straddle × 0.85
3. Writes results to `expected_moves.json` and regenerates `expected_move.pine`
4. You copy the Pine script into TradingView, it auto-plots EM bands on whatever ticker you load
5. Web dashboard at `dashboard.html` shows your full watchlist at a glance

## Setup (one-time, ~10 minutes)

### 1. Create GitHub repo

```bash
# On your machine, create a new repo on github.com called "em-tracker"
git clone https://github.com/YOUR_USERNAME/em-tracker.git
cd em-tracker
```

Copy all these files into that folder:
- `calculate_em.py`
- `generate_pine.py`
- `expected_move.pine.template`
- `dashboard.html`
- `.github/workflows/update-em.yml`

### 2. Customize your watchlist

Open `calculate_em.py` and edit the `WATCHLIST` array (line ~20) with your 32 tickers. Already pre-populated with common ones.

### 3. Update dashboard URL

Open `dashboard.html`, find the line:
```javascript
const JSON_URL = 'https://raw.githubusercontent.com/YOUR_USERNAME/em-tracker/main/expected_moves.json';
```
Replace `YOUR_USERNAME` with your actual GitHub username. Do the same for `REPO_URL`.

### 4. Push to GitHub

```bash
git add .
git commit -m "Initial setup"
git push origin main
```

### 5. Enable GitHub Actions

- Go to your repo on github.com
- Click **Settings** → **Actions** → **General**
- Under "Workflow permissions" select **Read and write permissions** → Save
- Go to **Actions** tab → **Update Expected Moves** → **Run workflow** → Run

Wait 1-2 minutes. The action will fetch data and commit `expected_moves.json` back to the repo.

### 6. Install Pine script in TradingView

- After the first successful action run, the repo will have `expected_move.pine`
- Copy its entire contents
- In TradingView: Pine Editor → New Indicator → paste → Save → Add to chart
- The EM bands will plot for whatever ticker you're viewing (if it's in the watchlist)

### 7. Bookmark the dashboard

Open `dashboard.html` in your browser. Either:
- Host it on GitHub Pages (free): Settings → Pages → Deploy from main branch
- Or just open the local file directly with a double-click

## Usage

### Daily/weekly workflow

- **Automatic**: Friday 4pm CT and Sunday 6pm CT, the action runs, fetches fresh EM data, regenerates the Pine script
- **TradingView**: Just open any ticker on your watchlist, the EM bands appear automatically
- **Dashboard**: Check Sunday night to see your weekly EM ranges across all tickers at once

### Adding a new ticker mid-week

You're scrolling through, want to trade something not on your list (say `RDDT`). Two options:

**Option A: Permanent add**
Edit `calculate_em.py`, add `"RDDT"` to WATCHLIST, push to GitHub. Next scheduled run picks it up.

**Option B: One-off fetch (faster)**
- Click "+ ADD & FETCH" on the dashboard, enter ticker
- It opens GitHub Actions, click "Run workflow", paste ticker, run
- 30-60 seconds later the new EM data is in the JSON
- Refresh TradingView (re-add the indicator) and you'll see bands for RDDT

The dashboard also accepts `gh workflow run update-em.yml -f ticker=RDDT` via the GitHub CLI if you want it CLI-fast.

## Pine Script update flow

The `expected_move.pine` file in your repo gets regenerated automatically. To pull updates into TradingView, you have to **re-paste** the script (Pine doesn't support live remote imports for free accounts). This takes 5 seconds, ideally done Sunday night after the action runs.

A premium TradingView feature (`request.seed`) can read external data live, but it's paid. The manual re-paste is what keeps this free.

## Math reference

- **Weekly EM** = (ATM call mid + ATM put mid) × 0.85, expiring next Friday
- **Monthly EM** = same formula, ~30 DTE expiry
- **Upper band** = current price + EM
- **Lower band** = current price − EM
- 1 standard deviation, ~68% probability the price stays within these bands by expiry

## Troubleshooting

**"No options chain" error** for a ticker: yfinance occasionally fails to return options data. Re-run the workflow.

**Action fails to commit**: check Settings → Actions → General → Workflow permissions is set to "Read and write".

**Pine script shows "ticker not in watchlist"**: the symbol isn't in `expected_moves.json` yet. Add it via dashboard or push an updated WATCHLIST.

**EM values look stale on Monday morning**: the action runs Sunday 6pm CT. If you opened TradingView before re-pasting the Pine script, you're seeing last week's data. Re-paste.

## Future improvements

- Webhook to auto-trigger action when you open a new ticker (requires a small server, not free)
- Alerts on EM band touches (TradingView's alert system can do this on the indicator)
- Volume profile integration with EM band as confluence zone
- Daily 0DTE expected move calculation for SPX/SPY scalping

## Cost

$0/month. GitHub Actions free tier gives you 2,000 minutes/month, this workflow uses ~2 minutes per run = ~8 minutes/week = ~32 minutes/month. yfinance is free.
