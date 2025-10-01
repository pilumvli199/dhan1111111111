#!/usr/bin/env python3
# main.py — simplified runner (no telegram.ext Application/updater)
# - Uses Bot.send_message for Telegram
# - Avoids python-telegram-bot Application/Updater incompatibility
# - Tries to use dhanhq client if available, else falls back to a requests placeholder
# - Periodically fetches LTP and sends option-chain snapshot

import os
import asyncio
import json
import signal
import logging
from datetime import datetime

import requests

# Try import dhanhq client (optional). If not present, we'll use HTTP fallback.
try:
    from dhanhq import dhanhq, marketfeed  # type: ignore
    _HAS_DHANHQ = True
except Exception:
    _HAS_DHANHQ = False

# telegram Bot (async)
from telegram import Bot

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=os.getenv("LOG_LEVEL", "INFO").upper()
)
logger = logging.getLogger(__name__)

# ENV
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # seconds
STRIKE_WINDOW = int(os.getenv("STRIKE_WINDOW", "5"))   # +/- strikes to show
OPTION_EXPIRY_NIFTY = os.getenv("OPTION_EXPIRY_NIFTY", "2025-10-03")
OPTION_EXPIRY_TCS = os.getenv("OPTION_EXPIRY_TCS", "2025-10-03")

# Instruments - placeholders (replace with actual security ids from Dhan docs)
INSTRUMENTS = {
    "NIFTY50": {"security_id": os.getenv("NIFTY_SECURITY_ID", "13"), "exchange": os.getenv("NIFTY_EXCHANGE","IDX_I")},
    "TCS": {"security_id": os.getenv("TCS_SECURITY_ID", "11536"), "exchange": os.getenv("TCS_EXCHANGE","NSE_EQ")},
}

# Initialize dhanhq client if available
if _HAS_DHANHQ and DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN:
    try:
        dhan = dhanhq(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)  # adjust constructor if package differs
        logger.info("Initialized dhanhq client.")
    except Exception as e:
        logger.warning("Failed to initialize dhanhq client: %s", e)
        dhan = None
else:
    dhan = None
    if not _HAS_DHANHQ:
        logger.info("dhanhq package not available; using HTTP fallback for market data (placeholder).")
    else:
        logger.info("DHAN credentials not provided; using HTTP fallback (placeholder).")

# Telegram Bot
if not TELEGRAM_BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram sends will be skipped.")
bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

class OptionChainBot:
    def __init__(self):
        self.running = True
        self.task = None

    async def get_ltp(self, security_id: str, exchange: str):
        """
        Try dhanhq client first; if not available, use HTTP placeholder.
        NOTE: Replace placeholder HTTP with real DhanHQ REST endpoint if available.
        """
        try:
            if dhan:
                # adapt to actual dhanhq method names/response
                # Many dhanhq wrappers use something like dhan.get_market_quote(...)
                resp = dhan.get_market_quote(security_id, exchange)
                # resp shape may vary; try common keys
                if resp and isinstance(resp, dict):
                    data = resp.get("data") or resp.get("result") or resp
                    if isinstance(data, dict):
                        # common key names: LTP / ltp / lastPrice
                        return data.get("LTP") or data.get("ltp") or data.get("lastPrice") or data.get("last_traded_price")
                return None
            else:
                # Placeholder HTTP - user must change to real DhanHQ REST API if they want
                # This will most likely NOT work until you replace with real endpoint.
                if not DHAN_ACCESS_TOKEN:
                    return None
                url = f"https://api.dhan.co/market/quote?security_id={security_id}&exchange={exchange}"
                headers = {"Authorization": f"Bearer {DHAN_ACCESS_TOKEN}"}
                r = requests.get(url, headers=headers, timeout=10)
                r.raise_for_status()
                j = r.json()
                data = j.get("data") or j
                return data.get("LTP") or data.get("ltp") or data.get("lastPrice") or None
        except Exception as e:
            logger.debug("get_ltp failed for %s@%s: %s", security_id, exchange, e)
            return None

    def get_nearest_expiry(self, symbol: str):
        # Very simple placeholder — replace with actual expiry resolution logic if you want
        if symbol.upper().startswith("NIFTY"):
            return OPTION_EXPIRY_NIFTY
        return OPTION_EXPIRY_TCS

    def get_option_data(self, symbol: str, strike: float, option_type: str, expiry: str):
        """
        Placeholder for option leg data. Replace with real option security-fetch logic.
        At minimum, it should return a dict with ltp, oi, iv, volume keys.
        """
        # Users should replace this with proper mapping from strike -> option security id
        return {"ltp": 0.0, "oi": 0, "iv": 0.0, "volume": 0}

    def build_option_chain(self, symbol: str, spot_price: float):
        try:
            # choose a sensible strike interval
            strike_interval = 50 if symbol.upper().startswith("NIFTY") else 50
            atm = round(spot_price / strike_interval) * strike_interval
            expiry = self.get_nearest_expiry(symbol)
            result = []
            for i in range(-STRIKE_WINDOW*2, STRIKE_WINDOW*2 + 1):
                strike = int(atm + i * strike_interval)
                ce = self.get_option_data(symbol, strike, "CE", expiry)
                pe = self.get_option_data(symbol, strike, "PE", expiry)
                result.append({"strike": strike, "CE": ce, "PE": pe, "is_atm": (strike == atm)})
            return result
        except Exception as e:
            logger.error("build_option_chain error: %s", e)
            return []

    def format_message(self, symbol: str, spot_price: float, chain):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"🔔 *{symbol} Option Chain Update*\n"
        msg += f"📊 *Spot Price:* ₹{spot_price:.2f}\n"
        msg += f"⏰ *Time:* {ts}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "```\n"
        msg += f"{'Strike':>7} {'CE LTP':>10} {'PE LTP':>10}\n"
        msg += "-"*35 + "\n"
        for r in chain:
            marker = "➤" if r.get("is_atm") else " "
            ce_ltp = r["CE"].get("ltp", 0.0) if r.get("CE") else 0.0
            pe_ltp = r["PE"].get("ltp", 0.0) if r.get("PE") else 0.0
            msg += f"{marker}{int(r['strike']):7d} {float(ce_ltp):10.2f} {float(pe_ltp):10.2f}\n"
        msg += "```\n"
        return msg

    async def send_telegram(self, text: str):
        if not bot:
            logger.debug("No bot configured; skipping send.")
            return
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=True)
            logger.info("Sent Telegram update.")
        except Exception as e:
            logger.warning("Failed to send Telegram message: %s", e)

    async def process_and_send_data(self):
        logger.info("Starting main poll loop (interval %ss)", POLL_INTERVAL)
        while self.running:
            try:
                for symbol, cfg in INSTRUMENTS.items():
                    sec_id = cfg.get("security_id")
                    exch = cfg.get("exchange")
                    ltp = await self.get_ltp(sec_id, exch)
                    if ltp is None:
                        logger.info("%s: no LTP this cycle (id=%s)", symbol, sec_id)
                        continue
                    logger.info("%s LTP: %s", symbol, ltp)
                    chain = self.build_option_chain(symbol, float(ltp))
                    msg = self.format_message(symbol, float(ltp), chain)
                    await self.send_telegram(msg)
                # jitter small random to avoid strict schedule collisions
                await asyncio.sleep(POLL_INTERVAL + (0.1 * (random_jitter())))
            except asyncio.CancelledError:
                logger.info("process_and_send_data cancelled, exiting loop.")
                break
            except Exception as e:
                logger.exception("Error in process loop: %s", e)
                # wait a bit before next attempt
                await asyncio.sleep(min(60, POLL_INTERVAL))

    async def start(self):
        self.task = asyncio.create_task(self.process_and_send_data())

    async def stop(self):
        logger.info("Stopping bot...")
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped.")

def random_jitter():
    # small jitter in seconds (0..1)
    return float(os.urandom(1)[0]) / 255.0

# graceful shutdown
def _install_signal_handlers(loop, bot_obj):
    def _stop(sig):
        logger.info("Received signal %s - shutting down...", sig.name)
        asyncio.create_task(bot_obj.stop())
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda s=s: _stop(s))
        except NotImplementedError:
            # Windows or environments where signal handlers aren't supported
            pass

async def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not fully configured (check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). Bot will not send messages.")
    ocb = OptionChainBot()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, ocb)
    await ocb.start()
    # wait until stopped
    while ocb.running:
        await asyncio.sleep(1)
    logger.info("Main exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt - exit")
