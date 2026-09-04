from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
import httpx

from storage.event_store import BrokerEvent
from services.state_manager import state_mgr, TelegramState
from utils.logger import get_logger

logger = get_logger("telegram_service")


class TelegramService:
    """
    Production-grade Telegram service supporting real-time event dispatching,
    status broadcasting, error reporting, connection testing, and resilient retry logic.
    Directly updates the centralized StateManager so GUI reflects actual Telegram health.
    """

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
        max_retries: int = 3,
        timeout: float = 10.0,
    ):
        self.bot_token = bot_token.strip() if bot_token else None
        self.chat_id = str(chat_id).strip() if chat_id else None
        self.max_retries = max_retries
        self.timeout = timeout
        self._lock = asyncio.Lock()

        # Check placeholder or missing credentials
        is_placeholder = (
            not self.bot_token
            or not self.chat_id
            or "your_telegram" in (self.bot_token or "").lower()
            or "your_telegram" in (self.chat_id or "").lower()
            or self.bot_token == "your_bot_token"
        )
        self.enabled = enabled and not is_placeholder

        if not self.enabled:
            state_mgr.set_telegram_state(TelegramState.DISCONNECTED)
            if is_placeholder and enabled:
                logger.info("Telegram service disabled (placeholder credentials in .env).")
            else:
                logger.info("Telegram service disabled.")
        else:
            state_mgr.set_telegram_state(TelegramState.CONNECTED)
            logger.info(f"Telegram service configured for Chat ID: ***{self.chat_id[-4:] if len(self.chat_id) > 4 else self.chat_id}")

    @property
    def send_url(self) -> str:
        return f"{self.BASE_URL}/bot{self.bot_token}/sendMessage"

    async def initialize(self) -> bool:
        """
        Initializes the service and validates Telegram API connectivity.
        """
        if not self.enabled:
            state_mgr.set_telegram_state(TelegramState.DISCONNECTED)
            return False

        state_mgr.set_telegram_state(TelegramState.CONNECTING)
        success, _ = await self.test_connection()
        if success:
            state_mgr.set_telegram_state(TelegramState.CONNECTED)
            return True
        else:
            state_mgr.set_telegram_state(TelegramState.ERROR)
            return False

    async def test_connection(self) -> Tuple[bool, str]:
        """
        Tests credentials directly against Telegram's getMe and sendMessage endpoints.
        """
        if not self.bot_token or not self.chat_id:
            msg = "Telegram credentials not configured. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."
            state_mgr.set_telegram_state(TelegramState.DISCONNECTED, error=msg)
            return False, msg

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # 1. Validate Bot Token via getMe
                me_url = f"{self.BASE_URL}/bot{self.bot_token}/getMe"
                resp = await client.get(me_url)
                if resp.status_code != 200:
                    err_msg = f"Invalid Bot Token. Telegram response ({resp.status_code}): {resp.text}"
                    state_mgr.set_telegram_state(TelegramState.ERROR, error=err_msg)
                    return False, err_msg

                bot_info = resp.json().get("result", {})
                bot_username = bot_info.get("username", "UnknownBot")

                # 2. Validate Chat ID delivery
                test_text = (
                    f"🤖 *ALGO System Online*\n\n"
                    f"Bot: @{bot_username}\n"
                    f"Chat ID: `{self.chat_id}`\n"
                    f"Status: *Connected & Operational*\n"
                    f"Timestamp: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
                )
                sent = await self.send_message(test_text)
                if sent:
                    state_mgr.set_telegram_state(TelegramState.CONNECTED)
                    return True, f"Successfully verified @{bot_username} and delivered test alert to chat {self.chat_id}."
                else:
                    err_msg = "Bot Token is valid, but message delivery to Chat ID failed. Check chat permissions."
                    state_mgr.set_telegram_state(TelegramState.ERROR, error=err_msg)
                    return False, err_msg

        except Exception as exc:
            err_msg = f"Failed to connect to Telegram servers: {exc}"
            state_mgr.set_telegram_state(TelegramState.ERROR, error=err_msg)
            return False, err_msg

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Sends an arbitrary message to the configured chat with retry logic.
        Never throws exceptions that would crash calling services.
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.debug(f"[Dry-Run] Telegram disabled. Message:\n{text}")
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        async with self._lock:
            # Respect Telegram rate limiting: minimum 50ms gap
            await asyncio.sleep(0.05)

            for attempt in range(1, self.max_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(self.send_url, json=payload)
                        if response.status_code == 200:
                            state_mgr.set_telegram_state(TelegramState.CONNECTED)
                            return True

                        if response.status_code == 429:
                            # Telegram rate limit backoff
                            data = response.json()
                            retry_after = data.get("parameters", {}).get("retry_after", 5)
                            logger.warning(f"Telegram rate limited. Pausing for {retry_after}s...")
                            await asyncio.sleep(retry_after)
                            continue

                        logger.warning(
                            f"Telegram API error {response.status_code} (attempt {attempt}/{self.max_retries}): {response.text}"
                        )
                        if response.status_code == 401:
                            # Immediate token failure, do not spam retries
                            state_mgr.set_telegram_state(TelegramState.ERROR, error="401 Unauthorized token")
                            return False

                except httpx.RequestError as exc:
                    logger.warning(f"Network error sending Telegram alert (attempt {attempt}/{self.max_retries}): {exc}")
                    state_mgr.set_telegram_state(TelegramState.ERROR, error=str(exc))

                except Exception as exc:
                    logger.error(f"Unexpected error in Telegram send: {exc}")
                    state_mgr.set_telegram_state(TelegramState.ERROR, error=str(exc))

                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)

            return False

    async def send_status(
        self,
        app_status: str,
        chrome_status: str,
        processed_count: int,
        latest_summary: str = "",
    ) -> bool:
        """
        Dispatches structured application status update to Telegram.
        """
        icon = "🟢" if app_status.upper() == "RUNNING" else "🛑"
        text = (
            f"{icon} *ALGO Monitoring Update*\n\n"
            f"• *Status:* `{app_status}`\n"
            f"• *Chrome:* `{chrome_status}`\n"
            f"• *Processed Orders:* `{processed_count}`\n"
        )
        if latest_summary:
            text += f"• *Latest:* `{latest_summary}`\n"
        text += f"\n_Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
        return await self.send_message(text)

    async def send_result(self, event: BrokerEvent) -> bool:
        """
        Dispatches newly processed trade or order event to Telegram.
        """
        status_lower = event.status.lower()
        if any(s in status_lower for s in ("executed", "traded", "complete", "filled")):
            icon = "✅"
        elif any(s in status_lower for s in ("rejected", "cancelled", "canceled", "failed")):
            icon = "❌"
        elif "trigger" in status_lower:
            icon = "⚡"
        else:
            icon = "🔔"

        lines = [
            f"{icon} *Broker Alert: {event.broker}*",
            "",
            f"*Event:* {event.event_type}",
            f"*Symbol:* `{event.symbol}`",
            f"*Order Type:* {event.order_type}",
            f"*Quantity:* {event.quantity}",
            f"*Price:* ₹{event.price}",
            f"*Status:* *{event.status}*",
            f"*Time:* `{event.time_str}`",
        ]
        if event.id and event.id != "N/A":
            lines.append(f"*Order ID:* `{event.id}`")

        if event.metadata:
            reason = event.metadata.get("reason")
            if reason:
                lines.append(f"*Reason:* {reason}")

        message = "\n".join(lines)
        return await self.send_message(message)

    async def send_error(self, error_msg: str, context: str = "") -> bool:
        """
        Dispatches system warning or error notification to Telegram.
        """
        text = f"⚠️ *System Alert*\n\n*Error:* {error_msg}"
        if context:
            text += f"\n*Context:* {context}"
        text += f"\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
        return await self.send_message(text)

    async def send_event(self, message: str) -> bool:
        """
        Dispatches an operational event text message to Telegram.
        """
        return await self.send_message(message)

    async def send_market_alert(self, result: Any) -> bool:
        """
        Dispatches a structured, non-spam market-analysis update to Telegram.
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False

        scan_time = getattr(result, "timestamp", datetime.now().strftime("%H:%M:%S"))
        stocks_count = getattr(result, "stocks_detected", 0)
        gainers = getattr(result, "top_gainers", [])
        decliners = getattr(result, "top_decliners", [])
        scanner_state = getattr(result, "scanner_state", "RUNNING")

        lines = [
            "📊 *MARKET UPDATE*",
            "",
            f"*Scan Time:* `{scan_time}`",
            f"*Stocks Detected:* {stocks_count}",
        ]

        if gainers:
            lines.append("\n🟢 *Top Gainers:*")
            for g in gainers[:5]:
                sign = "+" if g.change >= 0 else ""
                lines.append(f"• *{g.symbol}:* ₹{g.price:,.2f} ({sign}{g.change_percent:.2f}% UP)")

        if decliners:
            lines.append("\n🔴 *Top Decliners:*")
            for d in decliners[:5]:
                lines.append(f"• *{d.symbol}:* ₹{d.price:,.2f} ({d.change_percent:.2f}% DOWN)")

        lines.extend([
            "",
            f"*Scanner:* `{scanner_state}`"
        ])

        return await self.send_message("\n".join(lines))

    async def send_shutdown(self) -> bool:
        """
        Dispatches clean shutdown notice to Telegram.
        """
        text = f"🛑 *ALGO Monitor Stopped*\n\nMonitoring engine and workers shut down cleanly.\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
        return await self.send_message(text)


from config import config

telegram_service = TelegramService(
    bot_token=config.telegram_bot_token,
    chat_id=config.telegram_chat_id,
    enabled=config.telegram_enabled,
)
