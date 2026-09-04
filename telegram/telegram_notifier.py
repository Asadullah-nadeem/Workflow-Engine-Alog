from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any
import httpx

from storage.event_store import BrokerEvent
from utils.logger import get_logger

logger = get_logger("telegram")


class TelegramNotifier:
    """
    Asynchronous Telegram notification service with retry logic, rate limiting,
    and structured event formatting.
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
        self.chat_id = chat_id.strip() if chat_id else None
        self.max_retries = max_retries
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last_send_time = 0.0

        is_placeholder = (
            not self.bot_token
            or not self.chat_id
            or "your_telegram" in self.bot_token.lower()
            or "your_telegram" in self.chat_id.lower()
            or self.bot_token == "your_bot_token"
        )
        self.enabled = enabled and not is_placeholder

        if not self.enabled:
            if is_placeholder and enabled:
                logger.warning("Telegram notifications are disabled because placeholder credentials are set in .env.")
            else:
                logger.info("Telegram notifications are disabled or missing credentials.")
        else:
            logger.info(f"Telegram notifications configured for Chat ID: ***{self.chat_id[-4:] if len(self.chat_id) > 4 else self.chat_id}")

    @property
    def api_url(self) -> str:
        return f"{self.BASE_URL}/bot{self.bot_token}/sendMessage"

    def format_event_message(self, event: BrokerEvent) -> str:
        """
        Formats a BrokerEvent into a clean, legible Markdown notification.
        Uses visual emojis based on status/event_type.
        """
        # Choose status icon
        status_lower = event.status.lower()
        if any(s in status_lower for s in ("executed", "traded", "complete", "filled")):
            icon = "?"
        elif any(s in status_lower for s in ("rejected", "cancelled", "canceled", "failed")):
            icon = "?"
        elif "trigger" in status_lower:
            icon = "?"
        else:
            icon = "??"

        lines = [
            f"{icon} *Broker Alert*",
            "",
            f"*Broker:* {event.broker}",
            f"*Event:* {event.event_type}",
            "",
            f"*Symbol:* {event.symbol}",
            f"*Order Type:* {event.order_type}",
            f"*Quantity:* {event.quantity}",
            f"*Price:* {event.price}",
            f"*Status:* *{event.status}*",
            f"*Time:* {event.time_str}",
        ]

        if event.id and event.id != "N/A":
            lines.append(f"*Order ID:* {event.id}")

        # Optional non-sensitive metadata (e.g. rejection reason)
        if event.metadata:
            if "reason" in event.metadata and event.metadata["reason"]:
                lines.append(f"*Reason:* {event.metadata['reason']}")
            if "product" in event.metadata and event.metadata["product"]:
                lines.append(f"*Product:* {event.metadata['product']}")

        return "\n".join(lines)

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Sends a text message to Telegram with retry and rate limiting.
        """
        if not self.enabled:
            logger.debug(f"[Dry-Run] Telegram disabled. Message:\n{text}")
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        async with self._lock:
            # Enforce 50ms gap between consecutive API requests
            await asyncio.sleep(0.05)

            for attempt in range(1, self.max_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(self.api_url, json=payload)

                        if response.status_code == 200:
                            logger.info("Telegram notification dispatched successfully.")
                            return True

                        # Handle Telegram 429 Too Many Requests
                        if response.status_code == 429:
                            data = response.json()
                            retry_after = data.get("parameters", {}).get("retry_after", 5)
                            logger.warning(f"Telegram rate limit encountered. Backing off for {retry_after}s...")
                            await asyncio.sleep(retry_after)
                            continue

                        # Handle other HTTP errors
                        logger.warning(
                            f"Telegram API returned HTTP {response.status_code} on attempt {attempt}/{self.max_retries}. "
                            f"Response: {response.text}"
                        )

                except httpx.RequestError as exc:
                    logger.warning(f"Network error sending Telegram message (attempt {attempt}/{self.max_retries}): {exc}")

                except Exception as exc:
                    logger.error(f"Unexpected error during Telegram notification: {exc}")

                if attempt < self.max_retries:
                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)

            logger.error(f"Failed to send Telegram notification after {self.max_retries} attempts.")
            return False

    async def send_event_notification(self, event: BrokerEvent) -> bool:
        """
        Formats and sends a broker event alert.
        """
        message = self.format_event_message(event)
        return await self.send_message(message, parse_mode="Markdown")

    async def send_system_alert(self, title: str, details: str, alert_type: str = "info") -> bool:
        """
        Sends a high-level system notification (e.g. Session Expired, Application Started).
        """
        icon = "??" if alert_type == "info" else ("??" if alert_type == "warning" else "??")
        message = f"{icon} *System Notification*\n\n*{title}*\n{details}\n\n_Time: {asyncio.get_event_loop().time():.0f}_"
        return await self.send_message(message, parse_mode="Markdown")

    async def test_connection(self) -> tuple[bool, str]:
        """
        Verifies Telegram Bot Token and Chat ID validity by sending a ping.
        """
        if not self.bot_token or not self.chat_id:
            return False, "Telegram credentials not configured. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # 1. Verify Bot Token with getMe
                me_url = f"{self.BASE_URL}/bot{self.bot_token}/getMe"
                me_resp = await client.get(me_url)
                if me_resp.status_code != 200:
                    return False, f"Invalid Bot Token. Telegram response: {me_resp.text}"
                
                bot_info = me_resp.json().get("result", {})
                bot_username = bot_info.get("username", "UnknownBot")

                # 2. Verify Chat ID by sending test ping
                test_msg = f"?? *Broker Monitor Connected*\n\nBot: @{bot_username}\nChat ID: {self.chat_id}\nStatus: *Active & Ready*"
                sent = await self.send_message(test_msg)
                if sent:
                    return True, f"Successfully connected to @{bot_username} and delivered test message to chat {self.chat_id}."
                else:
                    return False, "Bot token is valid, but failed to deliver message to Chat ID. Verify Chat ID permissions."

        except Exception as exc:
            return False, f"Failed to connect to Telegram API: {exc}"
