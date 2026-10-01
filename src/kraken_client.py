"""Zero-dependency Kraken Pro REST API Client.

Uses standard Python library (urllib, hmac, hashlib, base64) to eliminate
third-party dependencies in AWS Lambda.
"""

from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

API_BASE_URL = "https://api.kraken.com"


class KrakenAPIError(Exception):
    """Raised when the Kraken API returns an error response."""

    pass


class KrakenClient:
    """Client for interacting with the Kraken Pro REST API."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        dry_run: bool = True,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run

    # --- Authentication Helper ---

    def _get_signature(self, urlpath: str, data: dict[str, Any]) -> str:
        """Calculate HMAC-SHA512 API signature for private Kraken endpoints.

        Signature = HMAC-SHA512(path + SHA256(nonce + postdata),
        base64_decoded_secret)
        """
        postdata = urllib.parse.urlencode(data)
        encoded_nonce = str(data["nonce"]).encode("utf-8")
        encoded_data = postdata.encode("utf-8")

        # SHA256 of nonce + postdata
        sha256_hash = hashlib.sha256(encoded_nonce + encoded_data).digest()

        # HMAC-SHA512 of path + sha256_hash with base64-decoded secret
        secret_bytes = base64.b64decode(self.api_secret)
        message = urlpath.encode("utf-8") + sha256_hash
        signature = hmac.new(secret_bytes, message, hashlib.sha512).digest()

        return base64.b64encode(signature).decode("utf-8")

    def _request(
        self,
        urlpath: str,
        data: dict[str, Any] | None = None,
        is_private: bool = False,
    ) -> dict[str, Any]:
        """Send an HTTP request to the Kraken API."""
        url = f"{API_BASE_URL}{urlpath}"
        headers = {
            "User-Agent": "Kraken-Micro-Trader/1.0",
        }

        encoded_data = None
        if is_private:
            if not self.api_key or not self.api_secret:
                raise KrakenAPIError("API key and secret required for private requests.")

            if data is None:
                data = {}
            # Monotonically increasing nonce in milliseconds
            data["nonce"] = int(time.time() * 1000)

            headers["API-Key"] = self.api_key
            headers["API-Sign"] = self._get_signature(urlpath, data)
            encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        elif data:
            # Public GET with query params
            query_string = urllib.parse.urlencode(data)
            url = f"{url}?{query_string}"

        req = urllib.request.Request(
            url, data=encoded_data, headers=headers, method="POST" if is_private else "GET"
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)

                if payload.get("error"):
                    raise KrakenAPIError(f"Kraken API error: {payload['error']}")

                return payload.get("result", {})
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            raise KrakenAPIError(f"HTTP {e.code}: {error_body}") from e
        except Exception as e:
            raise KrakenAPIError(f"Request failed: {str(e)}") from e

    # --- Public Endpoints ---

    def get_server_time(self) -> int:
        """Get current Kraken server Unix timestamp."""
        result = self._request("/0/public/Time")
        return result.get("unixtime", int(time.time()))

    def get_ticker(self, pair: str) -> dict[str, Decimal]:
        """Fetch current bid, ask, and last price for a trading pair.

        Returns dict: {'bid': Decimal, 'ask': Decimal, 'last': Decimal}
        """
        result = self._request("/0/public/Ticker", data={"pair": pair})

        # Kraken keys by pair name (e.g. 'SOLGBP' or 'XETHZGBP')
        first_key = next(iter(result))
        ticker = result[first_key]

        return {
            "ask": Decimal(ticker["a"][0]),  # lowest current ask
            "bid": Decimal(ticker["b"][0]),  # highest current bid
            "last": Decimal(ticker["c"][0]),  # last trade price
        }

    def get_pair_info(self, pair: str) -> dict[str, Any]:
        """Fetch precision, lot size, and minimum order rules for a pair."""
        result = self._request("/0/public/AssetPairs", data={"pair": pair})
        first_key = next(iter(result))
        pair_data = result[first_key]

        return {
            "altname": pair_data.get("altname", pair),
            "pair_decimals": pair_data.get("pair_decimals", 2),
            "lot_decimals": pair_data.get("lot_decimals", 4),
            "ordermin": Decimal(pair_data.get("ordermin", "0.01")),
        }

    def get_ohlc(self, pair: str, interval: int = 60) -> list[list[Any]]:
        """Fetch historical candlestick data (default interval: 60 minutes).

        Returns list of candles: [time, open, high, low, close, vwap, volume, count]
        """
        result = self._request("/0/public/OHLC", data={"pair": pair, "interval": interval})
        for k in result:
            if k != "last":
                return result[k]
        return []

    # --- Private Endpoints ---

    def get_balance(self) -> dict[str, Decimal]:
        """Get account balances."""
        if self.dry_run and self.api_key == "mock_api_key":
            return {"ZGBP": Decimal("50.00"), "SOL": Decimal("0.0")}

        result = self._request("/0/private/Balance", is_private=True)
        return {k: Decimal(str(v)) for k, v in result.items()}

    def get_trade_balance(self, asset: str = "ZGBP") -> dict[str, Decimal]:
        """Get trade balance and equity summary."""
        if self.dry_run and self.api_key == "mock_api_key":
            return {"eb": Decimal("50.00"), "tb": Decimal("50.00"), "mf": Decimal("50.00"), "mfo": Decimal("50.00")}

        result = self._request("/0/private/TradeBalance", data={"asset": asset}, is_private=True)
        return {k: Decimal(str(v)) for k, v in result.items()}

    def place_limit_order(
        self,
        pair: str,
        side: str,  # "buy" or "sell"
        volume: Decimal,
        price: Decimal,
        post_only: bool = True,
    ) -> str:
        """Place a limit buy or sell order.

        Args:
            pair: Trading pair (e.g. 'SOLGBP')
            side: 'buy' or 'sell'
            volume: Amount of crypto to buy/sell
            price: Limit price
            post_only: If True, guarantees Maker status (rejects if it would
              cross as Taker)

        Returns:
            Transaction ID (txid) string.
        """
        if self.dry_run:
            mock_txid = f"DRYRUN-{side.upper()}-{int(time.time())}"
            logger.info(
                "[DRY RUN] Placed limit %s: %s %s @ £%s (txid: %s)",
                side,
                volume,
                pair,
                price,
                mock_txid,
            )
            return mock_txid

        data: dict[str, Any] = {
            "pair": pair,
            "type": side,
            "ordertype": "limit",
            "price": str(price),
            "volume": str(volume),
        }

        # 'post' flag guarantees Maker status, avoiding higher taker fees
        if post_only:
            data["oflags"] = "post"

        result = self._request("/0/private/AddOrder", data=data, is_private=True)
        txids = result.get("txid", [])
        if not txids:
            raise KrakenAPIError(f"No txid returned from AddOrder: {result}")

        return txids[0]

    def query_order(self, txid: str) -> dict[str, Any]:
        """Query status of an order.

        Returns dict containing 'status' ('open', 'closed', 'canceled',
        'expired'),
        'vol_exec' (Decimal executed volume), and 'cost' (Decimal total cost).
        """
        if self.dry_run and txid.startswith("DRYRUN-"):
            # In dry-run, fallback to state-stored order prices for realistic simulation math
            return {
                "status": "closed",
                "vol_exec": Decimal("0"),
                "cost": Decimal("0"),
                "price": Decimal("0"),
                "reason": "Dry run mock fill",
            }

        result = self._request(
            "/0/private/QueryOrders", data={"txid": txid}, is_private=True
        )

        order_data = result.get(txid, {})
        status = order_data.get("status", "unknown")
        vol_exec = Decimal(order_data.get("vol_exec", "0"))
        cost = Decimal(order_data.get("cost", "0"))
        avg_price = Decimal(order_data.get("price", "0"))

        return {
            "status": status,
            "vol_exec": vol_exec,
            "cost": cost,
            "price": avg_price,
            "raw": order_data,
        }

    def cancel_order(self, txid: str) -> bool:
        """Cancel an open order."""
        if self.dry_run and txid.startswith("DRYRUN-"):
            logger.info("[DRY RUN] Cancelled order %s", txid)
            return True

        result = self._request(
            "/0/private/CancelOrder", data={"txid": txid}, is_private=True
        )
        return result.get("count", 0) > 0
