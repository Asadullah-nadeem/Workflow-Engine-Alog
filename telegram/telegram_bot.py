from __future__ import annotations

import asyncio
from typing import Optional, Callable, Awaitable, Dict, Any
import httpx
from utils.logger import get_logger

logger = get_logger("telegram_bot")


class TelegramBotListener:
    """
    Long-polling Telegram Bot listener to process incoming user commands from Telegram chats.
    Supports /status, /checklogin, /events, /start, and /help commands.
    """

    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        enabled: bool = True,
        command_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
    ):
        self.bot_token = bot_token.strip() if bot_token else None
        self.chat_id = str(chat_id).strip() if chat_id else None
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self.command_handler = command_handler
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_offset = 0

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def start(self):
        if not self.enabled:
            logger.info("Telegram Bot listener is disabled (missing token or chat ID).")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram Bot interactive listener started successfully.")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Telegram Bot listener stopped.")

    async def _poll_loop(self):
        url = f"{self.base_url}/getUpdates"
        while self._running:
            try:
                params = {
                    "offset": self._last_offset + 1,
                    "timeout": 10,
                    "allowed_updates": ["message"],
                }
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("ok"):
                            for result in data.get("result", []):
                                self._last_offset = result["update_id"]
                                message = result.get("message", {})
                                await self._process_message(message)
            except asyncio.CancelledError:
                break
            except httpx.RequestError as exc:
                logger.debug(f"Telegram network unavailable: {exc}")
                await asyncio.sleep(10.0)
            except Exception as exc:
                logger.warning(f"Error in Telegram bot polling loop: {exc}")
                await asyncio.sleep(5.0)


    async def _process_message(self, message: Dict[str, Any]):
        chat = message.get("chat", {})
        sender_chat_id = str(chat.get("id", ""))
        text = message.get("text", "").strip()

        # Security check: only respond to authorized chat ID if specified
        if self.chat_id and sender_chat_id != self.chat_id:
            logger.warning(f"Ignored message from unauthorized Telegram chat ID: {sender_chat_id}")
            return

        if not text.startswith("/"):
            return

        command_parts = text.split(maxsplit=1)
        command = command_parts[0].lower()
        args_text = command_parts[1] if len(command_parts) > 1 else ""

        reply = ""
        if command in ("/start", "/help"):
            reply = (
                "? *ALGO Brokerage Automation Bot*\n\n"
                "Available Commands:\n"
                "• `/status` - Check monitoring status and active broker\n"
                "• `/checklogin` - Trigger login verification\n"
                "• `/events` - Show last 5 trading events\n"
                "• `/help` - Show this menu"
            )
        elif self.command_handler:
            try:
                reply = await self.command_handler(command, {"text": text, "args": args_text})
            except Exception as exc:
                reply = f"? Error executing `{command}`: {exc}"
        else:
            reply = f"? Unknown command `{command}`. Type `/help` for options."

        if reply:
            await self._send_reply(sender_chat_id, reply)

    async def _send_reply(self, chat_id: str, text: str):
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:
            logger.error(f"Failed to send Telegram bot reply: {exc}")
