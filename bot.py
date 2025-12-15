"""
Kalshi Alert Bot - Main bot implementation
"""
import asyncio
import logging
import os
from typing import Dict
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from kalshi_client import KalshiWebSocketClient
from subscription_manager import SubscriptionManager

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class KalshiAlertBot:
    """Main bot class that coordinates Telegram bot and Kalshi client"""

    def __init__(self):
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.kalshi_api_key_id = os.getenv("KALSHI_API_KEY_ID")
        self.kalshi_private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        self.price_threshold = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.05"))

        if not all([self.telegram_token, self.kalshi_api_key_id, self.kalshi_private_key_path]):
            raise ValueError("Missing required environment variables. Check TELEGRAM_BOT_TOKEN, KALSHI_API_KEY_ID, and KALSHI_PRIVATE_KEY_PATH")

        self.subscription_manager = SubscriptionManager()
        self.kalshi_client = KalshiWebSocketClient(
            self.kalshi_api_key_id,
            self.kalshi_private_key_path
        )
        self.application = None
        self.last_prices: Dict[str, Dict] = {}
        self.market_info_cache: Dict[str, Dict] = {}  # Cache market titles and info

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_message = """
🤖 **Welcome to Kalshi Alert Bot!**

I'll send you real-time alerts for Kalshi prediction markets.

**Available Commands:**
/subscribe <TICKER> - Subscribe to market alerts
/unsubscribe <TICKER> - Unsubscribe from market alerts
/list - Show your subscribed markets
/help - Show this help message

**Example:**
`/subscribe KXHARRIS24-LSV`

Get started by subscribing to a market!
        """
        await update.message.reply_text(welcome_message, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_message = """
📖 **Kalshi Alert Bot Help**

**Commands:**
• `/subscribe <TICKER or URL>` - Subscribe to market alerts
• `/unsubscribe <TICKER>` - Unsubscribe from alerts
• `/list` - Show all your subscriptions
• `/help` - Show this help message

**Alert Types:**
• Price changes (>5 cent moves in yes_bid/yes_ask)

**Examples:**
`/subscribe KXHARRIS24-LSV`
`/subscribe https://kalshi.com/markets/kxnflgame/professional-football-game/kxnflgame-25dec15miapit`
`/unsubscribe KXHARRIS24-LSV`
`/list`

**Easy Subscription:**
Just copy/paste any Kalshi market URL to subscribe to all related markets (e.g., both teams in a sports game).
        """
        await update.message.reply_text(help_message, parse_mode='Markdown')

    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /subscribe command - accepts tickers or Kalshi URLs"""
        chat_id = update.effective_chat.id

        if not context.args:
            await update.message.reply_text(
                "Please provide a ticker or Kalshi URL.\n\n"
                "Examples:\n"
                "`/subscribe KXHARRIS24-LSV`\n"
                "`/subscribe https://kalshi.com/markets/...`",
                parse_mode='Markdown'
            )
            return

        user_input = context.args[0]

        # Check if input is a URL
        if user_input.startswith("http"):
            await self._subscribe_from_url(update, context, user_input)
        else:
            await self._subscribe_from_ticker(update, context, user_input.upper())

    async def _subscribe_from_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        """Handle subscription from Kalshi URL"""
        chat_id = update.effective_chat.id

        # Extract ticker base from URL
        # URL format: https://kalshi.com/markets/{series}/{category}/{ticker-base}
        try:
            parts = url.rstrip('/').split('/')
            ticker_base = parts[-1].upper()

            await update.message.reply_text(f"🔍 Searching for markets matching: {ticker_base}...")

            # Search for all markets matching this ticker base
            markets = await self.kalshi_client.search_markets_by_ticker_pattern(ticker_base)

            if not markets:
                await update.message.reply_text(
                    f"❌ No markets found for: {ticker_base}\n\n"
                    "The market may have closed or the URL may be incorrect."
                )
                return

            # Subscribe to all matching markets
            subscribed = []
            already_subscribed = []

            for market in markets:
                ticker = market.get("ticker")
                title = market.get("title", "Unknown")
                yes_bid = market.get("yes_bid", 0)
                yes_ask = market.get("yes_ask", 0)

                is_new = self.subscription_manager.subscribe(chat_id, ticker)

                if is_new:
                    # Cache market info for alerts
                    self.market_info_cache[ticker] = market

                    # Subscribe to WebSocket updates
                    if ticker not in self.last_prices:
                        await self.kalshi_client.subscribe_to_ticker(ticker)
                    subscribed.append(f"• **{ticker}**\n  {title}\n  Current: {yes_bid}-{yes_ask}¢")
                else:
                    already_subscribed.append(ticker)

            # Build response message
            response = ""
            if subscribed:
                response += f"✅ **Subscribed to {len(subscribed)} market(s):**\n\n"
                response += "\n\n".join(subscribed)
                response += "\n\nYou'll receive alerts when prices change significantly."

            if already_subscribed:
                if response:
                    response += "\n\n"
                response += f"ℹ️ Already subscribed to: {', '.join(already_subscribed)}"

            await update.message.reply_text(response, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error processing URL: {e}")
            await update.message.reply_text(
                f"❌ Error processing URL. Please check the format and try again.\n\n"
                f"Expected format: `https://kalshi.com/markets/...`",
                parse_mode='Markdown'
            )

    async def _subscribe_from_ticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str):
        """Handle subscription from direct ticker"""
        chat_id = update.effective_chat.id

        # Check if ticker is valid by trying to get market info
        try:
            market_info = await self.kalshi_client.get_market_info(ticker)
            if not market_info:
                await update.message.reply_text(
                    f"❌ Could not find market with ticker: {ticker}\n"
                    "Please check the ticker and try again."
                )
                return
        except Exception as e:
            logger.error(f"Error fetching market info: {e}")
            await update.message.reply_text(
                f"❌ Error validating ticker: {ticker}\n"
                "Please try again later."
            )
            return

        # Subscribe user
        is_new = self.subscription_manager.subscribe(chat_id, ticker)

        if is_new:
            # Cache market info for alerts
            self.market_info_cache[ticker] = market_info

            # Subscribe to WebSocket updates if this is the first subscriber
            if ticker not in self.last_prices:
                await self.kalshi_client.subscribe_to_ticker(ticker)

            title = market_info.get("title", ticker)
            yes_bid = market_info.get("yes_bid", 0)
            yes_ask = market_info.get("yes_ask", 0)
            await update.message.reply_text(
                f"✅ Subscribed to **{ticker}**\n{title}\n"
                f"Current: {yes_bid}-{yes_ask}¢\n\n"
                f"You'll receive alerts when prices change significantly.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"You're already subscribed to {ticker}"
            )

    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /unsubscribe command"""
        chat_id = update.effective_chat.id

        if not context.args:
            await update.message.reply_text(
                "Please provide a ticker.\nExample: `/unsubscribe KXHARRIS24-LSV`",
                parse_mode='Markdown'
            )
            return

        ticker = context.args[0].upper()
        success = self.subscription_manager.unsubscribe(chat_id, ticker)

        if success:
            # Check if there are still subscribers for this ticker
            subscribers = self.subscription_manager.get_subscribers(ticker)
            if not subscribers:
                # No more subscribers, unsubscribe from WebSocket
                await self.kalshi_client.unsubscribe_from_ticker(ticker)
                if ticker in self.last_prices:
                    del self.last_prices[ticker]

            await update.message.reply_text(f"✅ Unsubscribed from {ticker}")
        else:
            await update.message.reply_text(f"You weren't subscribed to {ticker}")

    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command"""
        chat_id = update.effective_chat.id
        subscriptions = self.subscription_manager.get_user_subscriptions(chat_id)

        if not subscriptions:
            await update.message.reply_text(
                "You have no active subscriptions.\n"
                "Use `/subscribe <TICKER>` to subscribe to a market.",
                parse_mode='Markdown'
            )
            return

        message = f"📊 **Your Subscriptions** ({len(subscriptions)}):\n\n"
        for ticker in subscriptions:
            message += f"• {ticker}\n"

        await update.message.reply_text(message, parse_mode='Markdown')

    async def handle_kalshi_message(self, data: dict):
        """Handle incoming WebSocket messages from Kalshi"""
        msg_type = data.get("type")

        if msg_type == "ticker":
            await self.handle_ticker_update(data)
        elif msg_type == "orderbook_delta":
            await self.handle_orderbook_delta(data)

    async def handle_ticker_update(self, data: dict):
        """Handle ticker update messages"""
        msg = data.get("msg", {})
        ticker = msg.get("market_ticker")

        if not ticker:
            return

        # Debug logging to see all ticker updates
        logger.info(f"Ticker update: {ticker} - bid={msg.get('yes_bid')}, ask={msg.get('yes_ask')}")

        # Extract price information
        yes_bid = msg.get("yes_bid")
        yes_ask = msg.get("yes_ask")

        # Store current prices
        if ticker not in self.last_prices:
            self.last_prices[ticker] = {"yes_bid": yes_bid, "yes_ask": yes_ask}
            return

        # Check for significant price changes
        last_bid = self.last_prices[ticker].get("yes_bid")
        last_ask = self.last_prices[ticker].get("yes_ask")

        bid_change = abs(yes_bid - last_bid) if yes_bid and last_bid else 0
        ask_change = abs(yes_ask - last_ask) if yes_ask and last_ask else 0

        # Send alerts if price changed significantly
        if bid_change >= self.price_threshold or ask_change >= self.price_threshold:
            await self.send_price_alert(ticker, yes_bid, yes_ask, last_bid, last_ask)

        # Update stored prices
        self.last_prices[ticker] = {"yes_bid": yes_bid, "yes_ask": yes_ask}

    async def handle_orderbook_delta(self, data: dict):
        """Handle orderbook delta messages (detailed price updates)"""
        # For MVP, we'll rely on ticker updates
        # This can be enhanced later for more granular alerts
        pass

    async def send_price_alert(self, ticker: str, yes_bid: float, yes_ask: float,
                               last_bid: float, last_ask: float):
        """Send price change alert to subscribers"""
        subscribers = self.subscription_manager.get_subscribers(ticker)

        if not subscribers:
            return

        # Get market info from cache, or fetch if not available
        market_info = self.market_info_cache.get(ticker)
        if not market_info:
            # Fetch and cache market info if not available (e.g., after bot restart)
            try:
                market_info = await self.kalshi_client.get_market_info(ticker)
                if market_info:
                    self.market_info_cache[ticker] = market_info
            except Exception as e:
                logger.error(f"Failed to fetch market info for {ticker}: {e}")
                market_info = {}

        market_title = market_info.get("title", ticker)

        # Construct Kalshi market URL
        # Try to use ranged_group_name or series_ticker to build URL
        series_ticker = market_info.get("series_ticker", "")
        subtitle = market_info.get("subtitle", "")

        # Build URL - Format: https://kalshi.com/markets/{series}/{subtitle-slug}/{ticker-lower}
        if series_ticker and subtitle:
            # Convert subtitle to URL slug (lowercase, spaces to hyphens)
            subtitle_slug = subtitle.lower().replace(" ", "-").replace("'", "")
            market_url = f"https://kalshi.com/markets/{series_ticker.lower()}/{subtitle_slug}/{ticker.lower()}"
        else:
            # Fallback: use search URL with ticker
            market_url = f"https://kalshi.com/markets?q={ticker}"

        # Format prices as percentages
        bid_pct = int(yes_bid * 100) if yes_bid else 0
        ask_pct = int(yes_ask * 100) if yes_ask else 0
        last_bid_pct = int(last_bid * 100) if last_bid else 0
        last_ask_pct = int(last_ask * 100) if last_ask else 0

        bid_change = bid_pct - last_bid_pct
        ask_change = ask_pct - last_ask_pct

        # Create alert message with title and URL
        message = f"📈 **Price Alert**\n\n"
        message += f"**{market_title}**\n"
        message += f"`{ticker}`\n\n"

        if bid_change != 0:
            arrow = "🔼" if bid_change > 0 else "🔽"
            message += f"Bid: {last_bid_pct}¢ → {bid_pct}¢ {arrow} ({bid_change:+d}¢)\n"

        if ask_change != 0:
            arrow = "🔼" if ask_change > 0 else "🔽"
            message += f"Ask: {last_ask_pct}¢ → {ask_pct}¢ {arrow} ({ask_change:+d}¢)\n"

        message += f"\n[View Market on Kalshi]({market_url})"

        # Send to all subscribers
        for chat_id in subscribers:
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to send alert to {chat_id}: {e}")

    async def start(self):
        """Start the bot"""
        logger.info("Starting Kalshi Alert Bot...")

        # Build Telegram application
        self.application = Application.builder().token(self.telegram_token).build()

        # Add command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("subscribe", self.subscribe_command))
        self.application.add_handler(CommandHandler("unsubscribe", self.unsubscribe_command))
        self.application.add_handler(CommandHandler("list", self.list_command))

        # Initialize Telegram bot
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

        logger.info("Telegram bot started")

        # Start Kalshi WebSocket client
        await self.kalshi_client.listen(self.handle_kalshi_message)

    async def stop(self):
        """Stop the bot"""
        logger.info("Stopping bot...")
        await self.kalshi_client.close()
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        logger.info("Bot stopped")


async def main():
    """Main entry point"""
    bot = KalshiAlertBot()

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
