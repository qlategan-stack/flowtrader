#!/usr/bin/env python3
"""
Bybit Collateral Manager
Safely manage collateral on Bybit: check status, list assets, transfer collateral.

Usage:
    python bybit_collateral_manager.py --check          # Check collateral status
    python bybit_collateral_manager.py --transfer       # Interactive transfer mode
    python bybit_collateral_manager.py --transfer-asset USDT 500  # Transfer specific asset

CRITICAL: Your Bybit API keys are loaded from .env. Keep that file secure.
"""

import os
import sys
import json
import hmac
import hashlib
import time
from urllib.parse import urlencode
from datetime import datetime
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# Bybit endpoints
if BYBIT_TESTNET:
    BASE_URL = "https://api-testnet.bybit.com"
else:
    BASE_URL = "https://api.bybit.com"

class BybitCollateralManager:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.testnet = testnet
        self.base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"

    def _generate_signature(self, params: dict, timestamp: str) -> str:
        """Generate HMAC SHA256 signature for Bybit API."""
        param_str = urlencode(params) + f"&timestamp={timestamp}"
        signature = hmac.new(
            self.secret_key.encode(),
            param_str.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_headers(self) -> dict:
        """Get headers with API key."""
        return {
            "X-BAPI-KEY": self.api_key,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-RECV-WINDOW": "5000",
            "Content-Type": "application/json",
        }

    def _make_request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """Make authenticated request to Bybit API."""
        timestamp = str(int(time.time() * 1000))
        headers = self._get_headers()

        if method == "GET":
            params = params or {}
            signature = self._generate_signature(params, timestamp)
            headers["X-BAPI-TIMESTAMP"] = timestamp
            headers["X-BAPI-SIGN"] = signature
            url = f"{self.base_url}{endpoint}?{urlencode(params)}&timestamp={timestamp}"
            response = requests.get(url, headers=headers)
        elif method == "POST":
            params = params or {}
            body = json.dumps(params)
            param_str = urlencode(params) + f"&timestamp={timestamp}"
            signature = hmac.new(
                self.secret_key.encode(),
                param_str.encode(),
                hashlib.sha256
            ).hexdigest()
            headers["X-BAPI-TIMESTAMP"] = timestamp
            headers["X-BAPI-SIGN"] = signature
            url = f"{self.base_url}{endpoint}"
            response = requests.post(url, headers=headers, json=params)

        return response.json()

    def get_account_info(self) -> dict:
        """Fetch account info including risk rate and collateral status."""
        response = self._make_request("GET", "/v5/account/info")
        return response

    def get_collateral_info(self) -> dict:
        """Fetch detailed collateral information."""
        response = self._make_request("GET", "/v5/account/collateral-info")
        return response

    def transfer_collateral(self, coin: str, quantity: str, target_type: str = "PLEDGED") -> dict:
        """
        Transfer asset to collateral.

        Args:
            coin: Asset symbol (e.g., 'USDT', 'USDC', 'ETH')
            quantity: Amount to transfer
            target_type: 'PLEDGED' (collateral) or 'UNPLEDGED' (release from collateral)
        """
        params = {
            "coin": coin,
            "quantity": str(quantity),
            "collateralType": target_type,
        }
        response = self._make_request("POST", "/v5/account/set-collateral-coin", params)
        return response


def check_collateral_status():
    """Check current collateral status and account health."""
    print("\n" + "="*60)
    print("BYBIT COLLATERAL STATUS CHECK")
    print("="*60)

    manager = BybitCollateralManager(BYBIT_API_KEY, BYBIT_SECRET_KEY, BYBIT_TESTNET)

    try:
        # Get account info
        print("\n📊 Fetching account info...")
        account = manager.get_account_info()

        if account.get("retCode") != 0:
            print(f"❌ Error: {account.get('retMsg')}")
            return

        account_data = account.get("result", {})
        risk_rate = account_data.get("riskRate", "N/A")

        print(f"✓ Account Risk Rate: {risk_rate}")

        # Get collateral info
        print("\n🔐 Fetching collateral details...")
        collateral = manager.get_collateral_info()

        if collateral.get("retCode") != 0:
            print(f"❌ Error: {collateral.get('retMsg')}")
            return

        collateral_data = collateral.get("result", {})
        collateral_coins = collateral_data.get("collateralCoins", [])

        if not collateral_coins:
            print("No collateral data available.")
            return

        print("\n📈 Collateral Balances:")
        print("-" * 60)

        btc_pledged = None
        for coin in collateral_coins:
            symbol = coin.get("coin", "")
            pledged = float(coin.get("collateralAmount", 0))
            max_capacity = float(coin.get("collateralAmountCap", 0))

            if pledged > 0 or max_capacity > 0:
                percentage = (pledged / max_capacity * 100) if max_capacity > 0 else 0
                status = "🔴 AT LIMIT" if percentage >= 95 else "🟡 WARNING" if percentage >= 80 else "🟢 OK"

                print(f"{symbol:8} | Pledged: {pledged:>12.4f} | Cap: {max_capacity:>12.4f} | {percentage:>5.1f}% {status}")

                if symbol == "BTC":
                    btc_pledged = pledged

        print("-" * 60)

        # Recommendations
        print("\n💡 Recommendations:")
        if btc_pledged is None:
            print("  • No BTC collateral found (good—no BTC limit pressure)")
        else:
            print(f"  • BTC collateral is pledged. Consider diversifying with:")
            print("    - USDT (stablecoin, most liquid)")
            print("    - USDC (alternative stablecoin)")
            print("    - ETH, SOL (other major assets)")

        print("\n" + "="*60)

    except Exception as e:
        print(f"❌ Error checking collateral: {e}")


def transfer_collateral_interactive():
    """Interactive mode to transfer collateral."""
    print("\n" + "="*60)
    print("BYBIT COLLATERAL TRANSFER")
    print("="*60)

    manager = BybitCollateralManager(BYBIT_API_KEY, BYBIT_SECRET_KEY, BYBIT_TESTNET)

    print("\nAvailable assets to transfer as collateral:")
    print("  • USDT (recommended - most liquid)")
    print("  • USDC (alternative stablecoin)")
    print("  • ETH")
    print("  • SOL")
    print("  • BTC")

    print("\n⚠️  CRITICAL WARNING:")
    print("  This will MOVE ASSETS to collateral on Bybit.")
    print("  Assets used as collateral may be liquidated if account risk rises.")
    print("  Ensure you understand the risks before proceeding.")

    # Get input
    asset = input("\nAsset to transfer (e.g., USDT): ").strip().upper()
    amount = input(f"Amount of {asset} to pledge as collateral: ").strip()

    # Confirm
    print(f"\n📋 CONFIRMATION:")
    print(f"   Asset:  {asset}")
    print(f"   Amount: {amount}")
    print(f"   Action: Transfer to collateral")

    confirm = input("\nProceed with transfer? (type 'YES' to confirm): ").strip()

    if confirm != "YES":
        print("❌ Transfer cancelled.")
        return

    try:
        print(f"\n🔄 Processing transfer of {amount} {asset}...")
        result = manager.transfer_collateral(asset, amount, "PLEDGED")

        if result.get("retCode") == 0:
            print(f"✅ Transfer successful!")
            print(f"   Result: {json.dumps(result.get('result', {}), indent=2)}")
        else:
            print(f"❌ Transfer failed: {result.get('retMsg')}")

    except Exception as e:
        print(f"❌ Error during transfer: {e}")


def transfer_collateral_direct(asset: str, amount: str):
    """Direct transfer without interaction."""
    print(f"\n🔄 Transferring {amount} {asset} to collateral...")

    manager = BybitCollateralManager(BYBIT_API_KEY, BYBIT_SECRET_KEY, BYBIT_TESTNET)

    try:
        result = manager.transfer_collateral(asset, amount, "PLEDGED")

        if result.get("retCode") == 0:
            print(f"✅ Transfer successful!")
            return True
        else:
            print(f"❌ Transfer failed: {result.get('retMsg')}")
            return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Main entry point."""
    if not BYBIT_API_KEY or not BYBIT_SECRET_KEY:
        print("❌ Error: BYBIT_API_KEY or BYBIT_SECRET_KEY not found in .env")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1].lower()

    if command == "--check":
        check_collateral_status()
    elif command == "--transfer":
        transfer_collateral_interactive()
    elif command == "--transfer-asset":
        if len(sys.argv) < 4:
            print("Usage: python bybit_collateral_manager.py --transfer-asset ASSET AMOUNT")
            print("Example: python bybit_collateral_manager.py --transfer-asset USDT 500")
            sys.exit(1)
        asset = sys.argv[2].upper()
        amount = sys.argv[3]
        transfer_collateral_direct(asset, amount)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
