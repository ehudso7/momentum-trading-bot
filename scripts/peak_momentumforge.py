#!/usr/bin/env python3
"""
PEAK MOMENTUMFORGE AI — Full Test + Exponential Growth Launcher

This is the ultimate "set it and watch your (paper) money grow" script.

It does as much as possible locally / for your deployment so you can:
- Get a working API key instantly
- Run the core trading bot in paper mode (the actual worker)
- Connect the beautiful MomentumForge AI frontend
- See live equity + insane exponential growth projections in the AI layer
- Have clear paths to backtests that demonstrate the edge + compounding

Run:
    python3 scripts/peak_momentumforge.py

It will guide you through a complete end-to-end test flow.
Safe by design: paper mode only by default.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

BANNER = """
╔══════════════════════════════════════════════════════════════════════════════╗
║   PEAK MOMENTUMFORGE AI — GOD-TIER TEST + EXPONENTIAL GROWTH LAUNCHER        ║
║   The MomentumForge AI does the intelligence. The bot does the work safely.  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

def print_section(title: str):
    print(f"\n{'='*78}")
    print(f"  {title}")
    print('='*78)

def generate_demo_key() -> str:
    return secrets.token_urlsafe(32)

def main():
    print(BANNER)

    print("This script helps you experience the *peak* version of the system:")
    print(" - Core momentum engine (paper) doing the actual trading work")
    print(" - MomentumForge AI frontend showing live data + beautiful exponential projections")
    print(" - Compound growth math from risk/compound.py visualized")
    print(" - Full signal intelligence from the SaaS layer\n")

    # 1. API Key (we have the dedicated tool)
    print_section("1. API KEY FOR MOMENTUMFORGE AI (Railway or local SaaS)")
    print("Running the dedicated key helper...")
    try:
        subprocess.run([sys.executable, "scripts/momentumforge_api_key.py", "--simple-only"], check=False)
    except Exception:
        key = generate_demo_key()
        print(f"\nFallback generated key (use this for TRADING_API_KEY):\n{key}\n")

    print("\n→ Set TRADING_API_KEY on Railway (Variables) or export it locally for testing.")
    print("→ Then in the live MomentumForge AI (Vercel or `npm run dev` in frontend/):")
    print("   Profile → paste key → Save → click 'Test connection'")

    # 2. Core Bot
    print_section("2. RUN THE CORE BOT (the actual worker that makes the money)")
    print("Recommended for full realistic testing:")
    print("   trading-bot --mode paper --dashboard-port 8080")
    print("\nThis starts:")
    print("  - Adaptive scanner (real data sources or fallbacks)")
    print("  - Strategy (5 setups, regime adaptive)")
    print("  - Advisor (the rule-based AI decision layer)")
    print("  - Risk engine with compound mode + circuit breaker (checked FIRST)")
    print("  - PaperBroker (realistic slippage)")
    print("  - Live dashboard at http://localhost:8080 (equity curve, positions, trades)")
    print("\nWithout valid Polygon/Alpaca keys it gracefully falls back to local paper mode.")
    print("You can let it run and it will take real setups when the market is open.")

    # 3. MomentumForge AI Frontend (the beautiful "AI" experience)
    print_section("3. MOMENTUMFORGE AI FRONTEND (where you WATCH THE MONEY GROW)")
    print("In the frontend/ directory:")
    print("   cd frontend")
    print("   npm run dev")
    print("\nThen open http://localhost:3000")
    print("Log in (Supabase) or use as guest if configured.")
    print("Go to Profile, paste your Railway/local SaaS API key, Save.")
    print("\nYou will see:")
    print("  - Live signals from the SaaS intelligence layer")
    print("  - Real equity curve from the running bot")
    print("  - ★ THE PEAK ADDITION: Growth Simulator with exponential projections")
    print("  - Intelligence panel (regime + top setups)")
    print("  - Positions & trades synced")
    print("\nThe Growth Simulator uses realistic momentum expectancy + compound math.")
    print("Tune the sliders — watch the projections update live. This is how you 'see' exponential growth.")

    # 4. Quick backtest for edge validation (shows historical exponential potential)
    print_section("4. VALIDATE THE EDGE + SEE HISTORICAL COMPOUNDING")
    print("Run a backtest (shows what the strategy actually did historically):")
    print("   trading-bot --mode backtest")
    print("\nOr for specific symbols / more control, look at trading_bot/backtest/engine.py.")
    print("Good backtest results + the live Growth Simulator = confidence to let it run.")

    # 5. Full "set and forget" test flow
    print_section("5. RECOMMENDED 'WATCH MONEY GROW' TEST FLOW (PAPER)")
    print("1. Run the core bot in one terminal: trading-bot --mode paper --dashboard-port 8080")
    print("2. In another terminal or deployed: MomentumForge AI frontend connected with the key")
    print("3. Let the bot run during market hours (or use backtest for instant gratification).")
    print("4. Watch the equity curve in both the classic dashboard (8080) and the god-tier AI frontend.")
    print("5. Open the Growth Simulator — see what disciplined execution + compounding can do over months/years.")
    print("6. Check circuit breaker status, regime badge, and AI recommendations in the frontend.")

    print("\n" + "="*78)
    print("  SAFETY REMINDERS (NON-NEGOTIABLE — ALREADY BUILT IN)")
    print("="*78)
    print("• Paper mode is default and always recommended for testing.")
    print("• Circuit breaker is checked FIRST on every tick.")
    print("• Hard time exit at 3:50pm ET.")
    print("• Position sizing via risk$ / stop distance. Compound mode is bounded.")
    print("• No live trading without explicit confirmation + understanding the risks.")
    print("\nThis system is built for serious edge + capital preservation. The 'exponential' only happens when you respect the rails.")

    print("\nRun the key script again anytime: python3 scripts/momentumforge_api_key.py")
    print("Everything is now peaked for full testing and beautiful growth visualization.\n")

    print("MomentumForge AI is now doing the heavy intelligence lifting. The bot does the disciplined work.")
    print("Go make it happen. Track your (paper) equity growth. Iterate on the edge.\n")

if __name__ == "__main__":
    main()
