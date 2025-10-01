import os
import asyncio
import json
from datetime import datetime
from dhanhq import dhanhq
import requests
from telegram import Bot
from telegram.ext import Application, CommandHandler
import logging

# Logging setup
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)

# Environment variables
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Initialize DhanHQ
dhan = dhanhq(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
logger.info("Initialized dhanhq client.")

# Instrument tokens (update with actual tokens from DhanHQ docs)
INSTRUMENTS = {
    "NIFTY50": {"security_id": "13", "exchange": "IDX_I"},
    "TCS": {"security_id": "11536", "exchange": "NSE_EQ"},
}

class OptionChainBot:
    def __init__(self):
        self.telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.latest_data = {}
        self.running = True

    async def get_ltp(self, security_id, exchange):
        """
        Robust debug-friendly get_ltp:
        - logs raw response from dhanhq client (or fallback HTTP)
        - tries multiple common JSON keys for LTP
        """
        try:
            if dhan:
                try:
                    resp = dhan.get_market_quote(security_id, exchange)
                except Exception as e:
                    logger.debug("dhan.get_market_quote raised: %s", e, exc_info=True)
                    resp = None
                logger.debug(
                    "dhanhq raw response for %s@%s: %s",
                    security_id,
                    exchange,
                    repr(resp)[:2000],
                )
                if not resp:
                    return None
                body = None
                if isinstance(resp, dict):
                    body = (
                        resp.get("data")
                        or resp.get("result")
                        or resp.get("response")
                        or resp
                    )
                else:
                    body = resp
                if isinstance(body, dict):
                    for key in (
                        "LTP",
                        "ltp",
                        "lastPrice",
                        "last_traded_price",
                        "lastTradedPrice",
                    ):
                        if key in body and body[key] not in (None, ""):
                            try:
                                return float(body[key])
                            except:
                                pass
                    nested = body.get("quote") or body.get("market")
                    if isinstance(nested, dict):
                        for key in ("LTP", "ltp", "lastPrice", "last_traded_price"):
                            if key in nested and nested[key] not in (None, ""):
                                try:
                                    return float(nested[key])
                                except:
                                    pass
                return None

            # Fallback HTTP
            if not DHAN_ACCESS_TOKEN:
                logger.debug("No DHAN_ACCESS_TOKEN for HTTP fallback.")
                return None
            url = f"https://api.dhan.co/market/quote?security_id={security_id}&exchange={exchange}"
            headers = {
                "Authorization": f"Bearer {DHAN_ACCESS_TOKEN}",
                "Accept": "application/json",
            }
            r = requests.get(url, headers=headers, timeout=12)
            logger.debug("HTTP fallback status=%s body=%s", r.status_code, r.text[:2000])
            r.raise_for_status()
            j = r.json()
            body = j.get("data") if isinstance(j, dict) else j
            if isinstance(body, dict):
                for key in ("LTP", "ltp", "lastPrice", "last_traded_price"):
                    if key in body and body[key] not in (None, ""):
                        try:
                            return float(body[key])
                        except:
                            pass
                nested = body.get("quote") or body.get("market") or body.get("result")
                if isinstance(nested, dict):
                    for key in ("LTP", "ltp", "lastPrice"):
                        if key in nested and nested[key] not in (None, ""):
                            try:
                                return float(nested[key])
                            except:
                                pass
            return None
        except Exception as exc:
            logger.exception(
                "get_ltp unexpected error for %s@%s: %s", security_id, exchange, exc
            )
            return None

    def get_option_chain(self, symbol, spot_price):
        try:
            strike_interval = 50
            atm_strike = round(spot_price / strike_interval) * strike_interval
            expiry = self.get_nearest_expiry(symbol)
            option_data = []
            for i in range(-5, 6):
                strike = atm_strike + (i * strike_interval)
                option_data.append(
                    {
                        "strike": strike,
                        "CE": {"ltp": 0},
                        "PE": {"ltp": 0},
                        "is_atm": i == 0,
                    }
                )
            return option_data
        except Exception as e:
            logger.error(f"Error fetching option chain: {e}")
            return []

    def get_nearest_expiry(self, symbol):
        return "2025-10-03"

    def format_message(self, symbol, spot_price, option_chain):
        msg = f"🔔 *{symbol} Option Chain Update*\n"
        msg += f"📊 *Spot Price:* ₹{spot_price:.2f}\n"
        msg += f"⏰ *Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

        msg += "```\n"
        msg += f"{'Strike':<8} {'CE LTP':<10} {'PE LTP':<10}\n"
        msg += "-" * 35 + "\n"

        for opt in option_chain:
            strike_marker = "➤" if opt["is_atm"] else " "
            ce_ltp = opt["CE"]["ltp"] if opt["CE"] else 0
            pe_ltp = opt["PE"]["ltp"] if opt["PE"] else 0
            msg += f"{strike_marker}{opt['strike']:<7} {ce_ltp:<10.2f} {pe_ltp:<10.2f}\n"

        msg += "```\n"
        return msg

    async def send_telegram_message(self, message):
        try:
            await self.telegram_bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown"
            )
            logger.info("Message sent to Telegram")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")

    async def process_and_send_data(self):
        while self.running:
            try:
                for symbol, details in INSTRUMENTS.items():
                    ltp = await self.get_ltp(details["security_id"], details["exchange"])
                    if ltp:
                        logger.info(f"{symbol} LTP: {ltp}")
                        option_chain = self.get_option_chain(symbol, ltp)
                        message = self.format_message(symbol, ltp, option_chain)
                        await self.send_telegram_message(message)
                    else:
                        logger.warning(f"{symbol}: no LTP this cycle (id={details['security_id']})")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(10)

    async def start_command(self, update, context):
        await update.message.reply_text("🚀 Bot Started!")

    async def stop_command(self, update, context):
        self.running = False
        await update.message.reply_text("⏹️ Bot stopped!")

    async def status_command(self, update, context):
        status = "🟢 Running" if self.running else "🔴 Stopped"
        await update.message.reply_text(f"Bot Status: {status}")

    async def run(self):
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("stop", self.stop_command))
        app.add_handler(CommandHandler("status", self.status_command))

        await app.initialize()
        await app.start()
        logger.info("Bot started successfully!")
        await self.process_and_send_data()
        await app.stop()

async def main():
    bot = OptionChainBot()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
