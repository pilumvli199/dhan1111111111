#!/usr/bin/env python3
"""
main.py — Option Chain poller (debug-friendly)

Features:
- Does NOT use telegram.ext Application/Updater to avoid compatibility errors.
- Uses telegram.Bot (async) to send messages.
- Optional dhanhq client; falls back to HTTP placeholder.
- Very verbose get_ltp() which deep-searches responses for numeric LTP-like values.
- Graceful shutdown via SIGINT/SIGTERM.
"""

import os
import asyncio
import logging
import signal
import json
from datetime import datetime
import requests

# Try optional dhanhq import
try:
    from dhanhq import dhanhq  # type: ignore
    _HAS_DHANHQ = True
except Exception:
    _HAS_DHANHQ = False

# telegram Bot (async)
from telegram import Bot  # python-telegram-bot must be installed

# Config / env
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STRIKE_WINDOW = int(os.getenv("STRIKE_WINDOW", "5"))

# Instrument placeholders (use your real security ids from Dhan instruments)
INSTRUMENTS = {
    "NIFTY50": {"security_id": os.getenv("NIFTY_SECURITY_ID", "13"), "exchange": os.getenv("NIFTY_EXCHANGE", "IDX_I")},
    "TCS": {"security_id": os.getenv("TCS_SECURITY_ID", "11536"), "exchange": os.getenv("TCS_EXCHANGE", "NSE_EQ")},
}

# Init dhanhq client if available and credentials present
if _HAS_DHANHQ and DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN:
    try:
        dhan = dhanhq(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
        logger.info("Initialized dhanhq client.")
    except Exception as e:
        logger.warning("Failed to initialize dhanhq client: %s", e)
        dhan = None
else:
    dhan = None
    if not _HAS_DHANHQ:
        logger.info("dhanhq package not installed — using HTTP fallback if possible.")
    else:
        logger.info("DHAN credentials missing — dhanhq client not initialized.")

# Telegram Bot object (async)
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — Telegram sends will be skipped.")
bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

def current_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def random_jitter():
    # small jitter (0..1s)
    try:
        return float(os.urandom(1)[0]) / 255.0
    except Exception:
        return 0.0

class OptionChainBot:
    def __init__(self):
        self.running = True
        self._task = None

    # deep_search helper (nested dict/list scanner)
    def _deep_search_numeric_candidates(self, obj, path="root", depth=0, max_depth=4):
        results = []
        if depth > max_depth or obj is None:
            return results
        if isinstance(obj, dict):
            for k, v in obj.items():
                # direct numeric match
                if isinstance(v, (int, float)):
                    if ("ltp" in k.lower()) or ("last" in k.lower()) or ("price" in k.lower()) or ("lt" in k.lower()):
                        results.append((f"{path}.{k}", v))
                # numeric string
                if isinstance(v, str):
                    s = v.replace(",", "").strip()
                    try:
                        f = float(s)
                        if ("ltp" in k.lower()) or ("last" in k.lower()) or ("price" in k.lower()) or ("lt" in k.lower()):
                            results.append((f"{path}.{k}", f))
                    except:
                        pass
                # recurse
                results.extend(self._deep_search_numeric_candidates(v, f"{path}.{k}", depth+1, max_depth))
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:200]):  # limit depth/length
                results.extend(self._deep_search_numeric_candidates(item, f"{path}[{i}]", depth+1, max_depth))
        return results

    async def get_ltp(self, security_id: str, exchange: str):
        """
        Debug get_ltp:
        - If dhanhq client is present, use it and log entire response.
        - Otherwise use HTTP fallback (placeholder).
        - Deep-search response for numeric keys that look like LTP.
        """
        try:
            # 1) Try dhanhq client
            if dhan:
                try:
                    resp = dhan.get_market_quote(security_id, exchange)
                except Exception as e:
                    logger.exception("dhan.get_market_quote raised an exception")
                    resp = None

                # Log full response (trim to avoid massive output)
                try:
                    rep = json.dumps(resp, default=str, indent=2) if not isinstance(resp, str) else resp
                except Exception:
                    rep = repr(resp)
                logger.debug("=== DHANHQ RAW RESPONSE START ===\n%s\n=== DHANHQ RAW RESPONSE END ===", rep[:10000])

                if not resp:
                    logger.debug("dhanhq returned empty/None response.")
                    return None

                # Try common places and keys
                # 1) immediate direct numeric keys
                if isinstance(resp, dict):
                    body_candidates = []
                    for container in ("data", "result", "response", "payload"):
                        if container in resp:
                            body_candidates.append((container, resp.get(container)))
                    body_candidates.append(("root", resp))

                    # check candidates for common LTP keys
                    for name, body in body_candidates:
                        if isinstance(body, dict):
                            for key in ("LTP", "ltp", "lastPrice", "last_traded_price", "lastTradedPrice", "last"):
                                if key in body and body[key] not in (None, ""):
                                    try:
                                        val = float(str(body[key]).replace(",", ""))
                                        logger.debug("Found LTP at %s.%s => %s", name, key, val)
                                        return val
                                    except:
                                        pass

                    # deep search for numeric candidates
                    hits = self._deep_search_numeric_candidates(resp)
                    if hits:
                        logger.debug("Potential numeric hits for LTP (path -> value):")
                        for p, v in hits[:20]:
                            logger.debug("%s -> %s", p, v)
                        # return first plausible hit
                        try:
                            return float(hits[0][1])
                        except:
                            pass

                # If we reach here, no LTP found
                logger.debug("No LTP located in dhanhq response for %s@%s", security_id, exchange)
                return None

            # 2) HTTP fallback (placeholder) - use only if DHAN_ACCESS_TOKEN provided
            if not DHAN_ACCESS_TOKEN:
                logger.debug("No DHAN_ACCESS_TOKEN for HTTP fallback.")
                return None

            url = f"https://api.dhan.co/market/quote?security_id={security_id}&exchange={exchange}"
            headers = {"Authorization": f"Bearer {DHAN_ACCESS_TOKEN}", "Accept": "application/json"}
            r = requests.get(url, headers=headers, timeout=12)
            logger.debug("HTTP fallback status=%s body=%s", r.status_code, r.text[:4000])
            r.raise_for_status()
            j = r.json()
            try:
                rep = json.dumps(j, default=str, indent=2)
            except Exception:
                rep = repr(j)
            logger.debug("=== HTTP FALLBACK RAW JSON START ===\n%s\n=== HTTP FALLBACK RAW JSON END ===", rep[:10000])

            # direct keys
            if isinstance(j, dict):
                body = j.get("data") if "data" in j else j
                if isinstance(body, dict):
                    for key in ("LTP", "ltp", "lastPrice", "last_traded_price"):
                        if key in body and body[key] not in (None, ""):
                            try:
                                return float(str(body[key]).replace(",", ""))
                            except:
                                pass
            hits = self._deep_search_numeric_candidates(j)
            if hits:
                for p, v in hits:
                    logger.debug("HTTP fallback potential LTP %s -> %s", p, v)
                    try:
                        return float(v)
                    except:
                        pass
            return None

        except Exception as exc:
            logger.exception("get_ltp unexpected error for %s@%s: %s", security_id, exchange, exc)
            return None

    def get_nearest_expiry(self, symbol: str):
        # Placeholder; replace with proper expiry resolution
        return os.getenv("OPTION_EXPIRY_DEFAULT", "2025-10-03")

    def get_option_data(self, symbol: str, strike: int, side: str, expiry: str):
        # Placeholder - implement strike -> option instrument mapping and real fetch
        return {"ltp": 0.0, "oi": 0, "iv": None, "volume": 0}

    def build_option_chain(self, symbol: str, spot_price: float):
        interval = 50 if symbol.upper().startswith("NIFTY") else 50
        atm = round(spot_price / interval) * interval
        expiry = self.get_nearest_expiry(symbol)
        rows = []
        for i in range(-STRIKE_WINDOW, STRIKE_WINDOW + 1):
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
        msg += "-" * 35 + "\n"
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
                    logger.debug("Fetching LTP for %s (id=%s exch=%s)", sym, sec, exch)
                    ltp = await self.get_ltp(sec, exch)
                    if ltp is None:
                        logger.info("%s: no LTP this cycle (id=%s)", sym, sec)
                        continue
                    logger.info("%s LTP: %s", sym, ltp)
                    chain = self.build_option_chain(sym, float(ltp))
                    msg = self.format_message(sym, float(ltp), chain)
                    await self.send_telegram(msg)
                # sleep + tiny jitter
                await asyncio.sleep(POLL_INTERVAL + random_jitter())
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

# signal handlers
def install_signal_handlers(loop, bot_obj):
    def _handle(sig):
        logger.info("Signal %s received; shutting down.", sig.name)
        asyncio.create_task(bot_obj.stop())
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda s=s: _handle(s))
        except NotImplementedError:
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
        logger.info("Interrupted; exiting.")
