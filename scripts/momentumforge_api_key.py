#!/usr/bin/env python3
"""MomentumForge AI — One-shot API Key Helper.

Run this script to generate a production-ready API key for your
MomentumForge AI frontend (Vercel) talking to your Railway backend.

It prints:
- A fresh, secure API key (shown once)
- Exact copy-paste commands for Railway (both quick and production paths)
- Precise instructions for pasting into the MomentumForge AI Profile page

Usage (after pip install -e ".[dev]"):
    python scripts/momentumforge_api_key.py
    trading-bot-api-issue-key   # if the console script is installed
"""
from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone

# Use the same generation + hashing logic as the real system for consistency
try:
    from trading_bot.api.keys import generate_api_key, _hash_api_key  # type: ignore
except Exception:
    # Fallback if not installed in editable mode — still produce a valid key
    def generate_api_key() -> str:
        return secrets.token_urlsafe(32)

    def _hash_api_key(api_key: str) -> str:
        import hashlib

        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def print_banner():
    print(
        """
╔══════════════════════════════════════════════════════════════════════╗
║   MomentumForge AI — Railway API Key Generator (Operator Tool)       ║
╚══════════════════════════════════════════════════════════════════════╝
"""
    )


def print_key_block(raw_key: str, tier: str, label: str):
    print("✅  FRESH API KEY (copy this now — it will not be shown again):\n")
    print("   " + raw_key)
    print("\n" + "─" * 70)
    print(f"   Tier : {tier}")
    print(f"   Label: {label}")
    print(f"   Generated: {now_iso()}")
    print("─" * 70 + "\n")


def print_quick_path(raw_key: str):
    """The fast path most MomentumForge users should start with."""
    print("══════════════════════════════════════════════════════════════════════")
    print("🚀  QUICK PATH (Recommended to get started in < 2 minutes)")
    print("══════════════════════════════════════════════════════════════════════\n")

    print("1. Set the key as a Railway environment variable:\n")
    print("   Using Railway CLI (if you have it installed and logged in):")
    print(f'   railway variables --set TRADING_API_KEY="{raw_key}"\n')

    print("   Or do it in the UI:")
    print("   • Go to your Railway project → your backend service")
    print("   • Click the 'Variables' tab")
    print("   • Add → New Variable")
    print("     Name :  TRADING_API_KEY")
    print(f"     Value:  {raw_key}")
    print("   • Click 'Add' then redeploy (or let Railway auto-redeploy)\n")

    print("2. In MomentumForge AI (the frontend, usually on Vercel):")
    print("   • Log in")
    print("   • Go to Profile (top nav → your avatar or /profile)")
    print("   • Scroll to the 'API Key' card")
    print("   • Paste the key above into the input field")
    print("   • Click 'Save'\n")

    print("3. Test it:")
    print("   • Go back to the dashboard")
    print("   • You should now be able to load signals/reports from Railway\n")

    print("⚠️  Security: Never commit this key. Never share it in public channels.\n")


def print_production_path(raw_key: str, label: str):
    """The robust multi-user path using persistent volume + manifest."""
    print("══════════════════════════════════════════════════════════════════════")
    print("🏭  PRODUCTION PATH (Multiple users + proper revocation + tiers)")
    print("══════════════════════════════════════════════════════════════════════\n")

    print("Prerequisites (do once):")
    print("• Attach a Persistent Volume to your Railway service")
    print("  (Settings → Volumes → Add volume, mount path: /app/data)")
    print("• Set these two variables in Railway (Variables tab):\n")
    print("  TRADING_API_KEYS_MANIFEST_PATH=/app/data/api_keys_manifest.jsonl")
    print("  TRADING_API_KEYS_REVOKED_PATH=/app/data/api_keys_revoked.jsonl\n")

    print("Then, from a Railway Shell (recommended):")
    print("  1. Open your service in Railway dashboard")
    print("  2. Click the 'Shell' tab (or use: railway run bash)")
    print("  3. Run the following command:\n")

    cmd = (
        f'python -m trading_bot.api.keys issue \\\n'
        f'    --tier free \\\n'
        f'    --label "{label}" \\\n'
        f'    --manifest-path /app/data/api_keys_manifest.jsonl'
    )
    print("   " + cmd.replace("\n", "\n   ") + "\n")

    print("   The raw key will be printed. Use the one printed by the command\n"
          "   (the one this script generated is an example you can also use).\n")

    print("Alternative (if you want to force this specific key into the manifest):")
    print("   You normally let the CLI generate it, but you can also run the")
    print("   simple path above for a quick single-key deployment.\n")


def print_testing_tips():
    print("══════════════════════════════════════════════════════════════════════")
    print("🧪  LOCAL TESTING TIPS")
    print("══════════════════════════════════════════════════════════════════════\n")
    print("To test the SaaS API locally with a key:")
    print("   export TRADING_API_KEY=the-key-from-above")
    print("   trading-bot-api --port 8081\n")
    print("Then in another terminal you can curl:")
    print('   curl -H "Authorization: Bearer the-key-from-above" http://localhost:8081/health\n')
    print("Or use the MomentumForge frontend against a local backend by setting")
    print("   NEXT_PUBLIC_BACKEND_URL=http://localhost:8081\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a MomentumForge AI API key + exact Railway instructions."
    )
    parser.add_argument(
        "--tier",
        choices=["free", "premium"],
        default="free",
        help="Key tier (default: free)",
    )
    parser.add_argument(
        "--label",
        default="momentumforge-operator",
        help="Human label for the key (email or name)",
    )
    parser.add_argument(
        "--simple-only",
        action="store_true",
        help="Only print the fast TRADING_API_KEY instructions",
    )
    args = parser.parse_args()

    print_banner()

    raw_key = generate_api_key()

    print_key_block(raw_key, args.tier, args.label)

    print_quick_path(raw_key)

    if not args.simple_only:
        print_production_path(raw_key, args.label)

    print_testing_tips()

    print("Done. Paste the key into MomentumForge AI → Profile → API Key and you're live.\n")
    print("Need to revoke later? Use: python -m trading_bot.api.keys revoke --help\n")


if __name__ == "__main__":
    main()
