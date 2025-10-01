#!/usr/bin/env python3
# Simplified main.py — NO Application/Updater usage.
# Uses telegram.Bot directly and a background asyncio task for polling.

import os
import asyncio
import logging
import signal
import time
from datetime import datetime
import requests

# Try to import dhanhq if available; optional
try:
    from dhanhq import dhanhq  # type: ignore
    _HAS_DHANHQ = True
except Exception:
    _HAS_DHANHQ = False

from telegram import Bot  # from python-telegram-bot

# Config / env
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STRIKE_WINDOW = int(os.getenv("STRIKE_WINDOW", "5"))

# Instrument placeholders (replace with real security IDs from Dhan instruments)
INSTRUMENTS = {
    "NIFTY50": {"security_id": os.getenv("NIFTY_SECURITY_ID", "13"), "exchange": os.getenv("NIFTY_EXCHANGE", "IDX_I")},
    "TCS": {"security_id": os.getenv("TCS_SECURITY_ID", "11536"), "exchange": os.getenv("TCS_EXCHANGE", "NSE_EQ")},
}

# init dhanhq client if possible
if _HAS_DHANHQ and DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN:
    try:
        dhan = dhanhq(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
        logger.info("Initialized dhanhq client.")
    except Exception as e:
        logger.warning("Failed to initialize dhanhq: %s", e)
        dhan = None
else:
    dhan = None
    if not _HAS_DHANHQ:
        logger.info("dhanhq package not installed; using HTTP fallback where applicable.")
    else:
        logger.info("DHAN credentials missing; dhanhq client not initialized.")

# Telegram Bot
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("Telegram bot token or chat id missing — messages will be skipped.")
bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

def current_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class OptionChainBot:
    def __init__(self):
        self.running = True
        self._task = None

    async def get_ltp(self, security_id: str, exchange: str):
        """
        Try dhanhq client first (if available). Else try a simple HTTP fallback (placeholder).
        Logs raw responses (DEBUG) to help diagnose shapes.
        """
        try:
            if dhan:
                try:
                    resp = dhan.get_market_quote(security_id, exchange)
                except Exception as e:
                    logger.debug("dhan.get_market_quote raised: %s", e, exc_info=True)
                    resp = None
                logger.debug("dhanhq raw response for %s@%s: %s", security_id, exchange, repr(resp)[:2000])
                if not resp:
                    return None
                body = resp.get("data") if isinstance(resp, dict) and "data" in resp else resp
                # common LTP keys
                if isinstance(body, dict):
                    for key in ("LTP","ltp","lastPrice","last_traded_price","lastTradedPrice"):
                        if key in body and body[key] not in (None, ""):
                            try:
                                return float(body[key])
                            except:
                                pass
                    nested = body.get("quote") or body.get("market") or body.get("result")
                    if isinstance(nested, dict):
                        for key in ("LTP","ltp","lastPrice"):
                            if key in nested and nested[key] not in (None, ""):
                                try:
                                    return float(nested[key])
                                except:
                                    pass
                return None

            # HTTP fallback (placeholder) — replace with real Dhan REST API if you have it
            if not DHAN_ACCESS_TOKEN:
                logger.debug("No DHAN_ACCESS_TOKEN available for HTTP fallback.")
                return None
            url = f"https://api.dhan.co/market/quote?security_id={security_id}&exchange={exchange}"
            headers = {"Authorization": f"Bearer {DHAN_ACCESS_TOKEN}", "Accept": "application/json"}
            r = requests.get(url, headers=headers, timeout=10)
            logger.debug("HTTP fallback status=%s body=%s", r.status_code, r.text[:1000])
            r.raise_for_status()
            j = r.json()
            body = j.get("data") if isinstance(j, dict) and "data" in j else j
            if isinstance(body, dict):
                for key in ("LTP","ltp","lastPrice","last_traded_price"):
                    if key in body and body[key] not in (None, ""):
                        try:
                            return float(body[key])
                        except:
                            pass
                nested = body.get("quote") or body.get("market") or body.get("result")
                if isinstance(nested, dict):
                    for key in ("LTP","ltp","lastPrice"):
                        if key in nested and nested[key] not in (None, ""):
                            try:
                                return float(nested[key])
                            except:
                                pass
            return None
        except Exception as exc:
            logger.exception("get_ltp unexpected error for %s@%s: %s", security_id, exchange, exc)
            return None

    def get_nearest_expiry(self, symbol: str):
        # Placeholder - replace with real expiry logic if needed
        return os.getenv("OPTION_EXPIRY_DEFAULT", "2025-10-03")

    def get_option_data(self, symbol: str, strike: int, side: str, expiry: str):
        # Placeholder: implement mapping strike->option-instrument and fetch their LTP/oi/iv
        return {"ltp": 0.0, "oi": 0, "iv": None, "volume": 0}

    def build_option_chain(self, symbol: str, spot_price: float):
        interval = 50 if symbol.upper().startswith("NIFTY") else 50
        atm = round(spot_price / interval) * interval
        expiry = self.get_nearest_expiry(symbol)
        rows = []
        for i in range(-STRIKE_WINDOW, STRIKE_WINDOW+1):
            strike = int(atm + i * interval)
            ce = self.get_option_data(symbol, strike, "CE", expiry)
            pe = self.get_option_data(symbol, strike, "PE", expiry)
            rows.append({"strike": strike, "CE": ce, "PE": pe, "is_atm": i == 0})
        return rows

    def format_message(self, symbol: str, spot_price: float, chain):
        ts = current_ts()
        msg = f"🔔 *{symbol} Option Chain Update*\n"
        msg += f"📊 *Spot Price:* ₹{spot_price:.2f}\n"
        msg += f"⏰ *Time:* {ts}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += "```\n"
        msg += f"{'Strike':>7} {'CE LTP':>10} {'PE LTP':>10}\n"
        msg += "-"*35 + "\n"
        for r in chain:
            marker = "➤" if r.get("is_atm") else " "
            ce = r["CE"].get("ltp", 0.0) if r.get("CE") else 0.0
            pe = r["PE"].get("ltp", 0.0) if r.get("PE") else 0.0
            msg += f"{marker}{int(r['strike']):7d} {float(ce):10.2f} {float(pe):10.2f}\n"
        msg += "```\n"
        return msg

    async def send_telegram(self, text: str):
        if not bot:
            logger.debug("No telegram bot configured; skipping send.")
            return
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=True)
            logger.info("Telegram message sent.")
        except Exception as e:
            logger.warning("Failed to send Telegram message: %s", e)

    async def poll_loop(self):
        logger.info("Starting main poll loop (interval %ss)", POLL_INTERVAL)
        while self.running:
            try:
                for sym, cfg in INSTRUMENTS.items():
                    sec = cfg.get("security_id")
                    exch = cfg.get("exchange")
                    ltp = await self.get_ltp(sec, exch)
                    if ltp is None:
                        logger.info("%s: no LTP this cycle (id=%s)", sym, sec)
                        continue
                    logger.info("%s LTP: %s", sym, ltp)
                    chain = self.build_option_chain(sym, float(ltp))
                    msg = self.format_message(sym, float(ltp), chain)
                    await self.send_telegram(msg)
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                logger.info("Poll loop cancelled.")
                break
            except Exception as e:
                logger.exception("Error in poll loop: %s", e)
                await asyncio.sleep(min(60, POLL_INTERVAL))

    async def start(self):
        self._task = asyncio.create_task(self.poll_loop())

    async def stop(self):
        logger.info("Stopping OptionChainBot...")
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped.")

# graceful signals
def install_signal_handlers(loop, bot_obj):
    def _handle(sig):
        logger.info("Signal %s received; shutting down.", sig.name)
        asyncio.create_task(bot_obj.stop())
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda s=s: _handle(s))
        except NotImplementedError:
            # some envs don't support
            pass

async def main():
    logger.info("Starting Option Chain poller service")
    ocb = OptionChainBot()
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, ocb)
    await ocb.start()
    # wait until stopped
    while ocb.running:
        await asyncio.sleep(1)
    logger.info("Service exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted - exiting")
