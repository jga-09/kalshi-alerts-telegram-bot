"""
Kalshi WebSocket Client - Connects to Kalshi API for real-time market data
"""
import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, Set
import websockets
import aiohttp
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """Client for Kalshi WebSocket API using API key authentication"""

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    API_URL = "https://api.elections.kalshi.com/trade-api/v2"

    # For demo/testing, use these instead:
    # WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    # API_URL = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(self, api_key_id: str, private_key_path: str):
        """
        Initialize Kalshi WebSocket client

        Args:
            api_key_id: Your Kalshi API key ID
            private_key_path: Path to your private key PEM file
        """
        self.api_key_id = api_key_id
        self.private_key = self._load_private_key(private_key_path)
        self.websocket = None
        self.subscribed_tickers: Set[str] = set()
        self.message_handler: Callable = None
        self.reconnect_delay = 5
        self.running = False
        self.message_id = 1

    def _load_private_key(self, private_key_path: str):
        """Load private key from PEM file"""
        try:
            with open(private_key_path, 'rb') as f:
                private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None
                )
            logger.info("Private key loaded successfully")
            return private_key
        except Exception as e:
            logger.error(f"Failed to load private key: {e}")
            raise

    def _sign_message(self, message: str) -> str:
        """
        Sign a message using RSA-PSS

        Args:
            message: String to sign

        Returns:
            Base64 encoded signature
        """
        message_bytes = message.encode('utf-8')
        signature = self.private_key.sign(
            message_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _create_auth_headers(self, method: str, path: str) -> dict:
        """
        Create authentication headers for Kalshi API

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., /trade-api/ws/v2)

        Returns:
            Dictionary of authentication headers
        """
        timestamp = str(int(time.time() * 1000))
        # Create message to sign: timestamp + method + path (without query params)
        message = timestamp + method + path.split('?')[0]
        signature = self._sign_message(message)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    async def connect(self):
        """Connect to Kalshi WebSocket with authentication"""
        logger.info("Connecting to Kalshi WebSocket...")

        # Create authentication headers for WebSocket connection
        ws_headers = self._create_auth_headers("GET", "/trade-api/ws/v2")

        self.websocket = await websockets.connect(
            self.WS_URL,
            extra_headers=ws_headers
        )
        logger.info("Connected to Kalshi WebSocket")

    async def subscribe_to_ticker(self, ticker: str):
        """Subscribe to market updates for a ticker"""
        ticker = ticker.upper()

        if ticker in self.subscribed_tickers:
            logger.debug(f"Already subscribed to {ticker}")
            return

        # Subscribe to ticker channel (global price updates)
        # Note: ticker channel gives updates for all markets
        await self._send_message({
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"]
            }
        })
        self.message_id += 1

        # Subscribe to orderbook_delta for this specific market
        await self._send_message({
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_ticker": ticker
            }
        })
        self.message_id += 1

        self.subscribed_tickers.add(ticker)
        logger.info(f"Subscribed to {ticker}")

    async def unsubscribe_from_ticker(self, ticker: str):
        """Unsubscribe from market updates for a ticker"""
        ticker = ticker.upper()

        if ticker not in self.subscribed_tickers:
            return

        # Note: We can't unsubscribe from ticker channel as it's global
        # But we can unsubscribe from orderbook_delta for this market
        # For now, we'll just remove it from our tracking
        # A proper implementation would track subscription IDs

        self.subscribed_tickers.remove(ticker)
        logger.info(f"Unsubscribed from {ticker}")

    async def _send_message(self, message: dict):
        """Send a message to the WebSocket"""
        if self.websocket:
            await self.websocket.send(json.dumps(message))
            logger.debug(f"Sent message: {message}")

    async def listen(self, message_handler: Callable):
        """Listen for messages from WebSocket"""
        self.message_handler = message_handler
        self.running = True

        while self.running:
            try:
                if not self.websocket:
                    await self.connect()

                async for message in self.websocket:
                    try:
                        data = json.loads(message)
                        logger.debug(f"Received: {data.get('type', 'unknown')}")

                        if self.message_handler:
                            await self.message_handler(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode message: {e}")
                    except Exception as e:
                        logger.error(f"Error handling message: {e}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed. Reconnecting...")
                await asyncio.sleep(self.reconnect_delay)
                try:
                    await self.connect()
                    # Re-subscribe to all tickers
                    tickers = list(self.subscribed_tickers)
                    self.subscribed_tickers.clear()
                    for ticker in tickers:
                        await self.subscribe_to_ticker(ticker)
                except Exception as e:
                    logger.error(f"Reconnection failed: {e}")

            except Exception as e:
                logger.error(f"Unexpected error in listen loop: {e}")
                await asyncio.sleep(self.reconnect_delay)

    async def close(self):
        """Close the WebSocket connection"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            logger.info("Closed Kalshi WebSocket connection")

    async def get_market_info(self, ticker: str) -> Dict:
        """Get market information via REST API"""
        headers = self._create_auth_headers("GET", f"/trade-api/v2/markets/{ticker}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.API_URL}/markets/{ticker}",
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("market", {})
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get market info for {ticker}: {error_text}")
                    return {}

    async def search_markets_by_ticker_pattern(self, ticker_base: str) -> list:
        """
        Search for all markets matching a ticker base pattern
        For example, 'KXNFLGAME-25DEC15MIAPIT' returns both -PIT and -MIA variants
        """
        ticker_base = ticker_base.upper()

        # Extract series from ticker (e.g., "KXNFLGAME" from "KXNFLGAME-25DEC15MIAPIT")
        series = ticker_base.split('-')[0] if '-' in ticker_base else ticker_base

        headers = self._create_auth_headers("GET", f"/trade-api/v2/markets")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.API_URL}/markets",
                headers=headers,
                params={"series_ticker": series, "limit": 200}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    markets = data.get("markets", [])

                    # Filter markets that start with the ticker base
                    matching_markets = [
                        m for m in markets
                        if m.get("ticker", "").upper().startswith(ticker_base)
                    ]
                    return matching_markets
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to search markets: {error_text}")
                    return []
