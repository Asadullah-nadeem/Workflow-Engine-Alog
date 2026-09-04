from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

from brokers.base_broker import BaseBroker
from storage.event_store import BrokerEvent
from utils.logger import get_logger

logger = get_logger("broker.zerodha")


class ZerodhaBroker(BaseBroker):
    """
    Implementation for Zerodha Kite platform (https://kite.zerodha.com).
    Demonstrates system extensibility for additional brokerage platforms.
    """

    def __init__(self, base_url: str = "https://kite.zerodha.com"):
        super().__init__(base_url=base_url)

    @property
    def name(self) -> str:
        return "Zerodha"

    @property
    def orders_url(self) -> str:
        return f"{self.base_url}/orders"

    @property
    def positions_url(self) -> str:
        return f"{self.base_url}/positions"

    async def is_logged_in(self, page: Page) -> bool:
        """
        Checks whether Zerodha Kite is in an authenticated state.
        """
        try:
            current_url = page.url.lower()
            if "/login" in current_url or "/forgot" in current_url:
                return False

            # Check for unauthenticated login form
            login_form = page.locator("form.login-form, button:has-text('Login to Kite')")
            if await login_form.count() > 0 and await login_form.first.is_visible():
                return False

            # Check for authenticated Kite elements: user avatar / user ID dropdown (.user-id, .avatar)
            user_badge = page.locator(".user-id, .user-nav, .avatar, a[href='/orders']")
            if await user_badge.count() > 0 and await user_badge.first.is_visible():
                return True

            return "kite.zerodha.com" in current_url and "/dashboard" in current_url
        except Exception as e:
            logger.debug(f"Error checking Zerodha login status: {e}")
            return False

    async def wait_for_login(self, page: Page, timeout_seconds: int = 300) -> bool:
        """
        Waits for manual login on Zerodha Kite.
        """
        logger.warning(
            "[ACTION REQUIRED] Zerodha Kite session not authenticated. "
            "Please log in manually in the open Chrome window."
        )
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds:
            if await self.is_logged_in(page):
                logger.info("Zerodha Kite authenticated session detected successfully.")
                return True
            await asyncio.sleep(2.0)

        logger.error(f"Zerodha login timeout ({timeout_seconds}s) expired without successful login.")
        return False

    async def navigate_to_orders(self, page: Page) -> bool:
        """
        Navigates to Zerodha Kite orders page.
        """
        try:
            if "/orders" not in page.url:
                orders_nav = page.locator("a[href*='/orders']").first
                if await orders_nav.is_visible():
                    await orders_nav.click()
                    await page.wait_for_timeout(1000)
                else:
                    await page.goto(self.orders_url, wait_until="domcontentloaded", timeout=20000)
            return True
        except Exception as e:
            logger.warning(f"Error navigating to Zerodha orders: {e}")
            return False

    async def extract_order_events(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts orders from Zerodha Kite order book table (.orders-table).
        """
        events: List[BrokerEvent] = []
        try:
            rows = page.locator(".orders-table tbody tr, .open-orders tbody tr, .executed-orders tbody tr")
            count = await rows.count()
            for idx in range(count):
                row = rows.nth(idx)
                if not await row.is_visible():
                    continue

                text = await row.inner_text()
                if not text or len(text.strip()) < 5:
                    continue

                event = self._parse_kite_row(text, idx)
                if event:
                    events.append(event)
        except Exception as e:
            logger.error(f"Error extracting Zerodha order events: {e}")
        return events

    def _parse_kite_row(self, text: str, index: int) -> Optional[BrokerEvent]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        status_pattern = re.compile(
            r'\\b(COMPLETE|EXECUTED|REJECTED|CANCELLED|CANCELED|OPEN|TRIGGER PENDING)\\b',
            re.IGNORECASE,
        )
        status_match = status_pattern.search(text)
        status = status_match.group(0).upper() if status_match else "UNKNOWN"

        if status in ("COMPLETE", "EXECUTED"):
            event_type = "Order Executed"
        elif status in ("REJECTED", "FAILED"):
            event_type = "Order Rejected"
        elif status in ("CANCELLED", "CANCELED"):
            event_type = "Order Cancelled"
        else:
            event_type = f"Order {status.title()}"

        side_match = re.search(r'\\b(BUY|SELL)\\b', text, re.IGNORECASE)
        side = side_match.group(0).upper() if side_match else "N/A"

        symbol = lines[0] if lines else "N/A"
        qty_match = re.search(r'(\\d+(?:/\\d+)?)\\s*(?:Qty)?', text)
        qty = qty_match.group(1) if qty_match else "-"

        price_match = re.search(r'([0-9,]+\\.\\d{2})', text)
        price = f"?{price_match.group(1)}" if price_match else "-"

        time_match = re.search(r'(\\d{1,2}:\\d{2}:\\d{2})', text)
        time_str = time_match.group(1) if time_match else datetime.now().strftime("%I:%M:%S %p")

        order_id = f"KITE-{symbol}-{time_str}-{index}"

        return BrokerEvent(
            id=order_id,
            broker=self.name,
            event_type=event_type,
            symbol=symbol,
            order_type=side,
            quantity=qty,
            price=price,
            status=status,
            time_str=time_str,
            metadata={"raw_snippet": text[:200]},
        )

    async def navigate_to_positions(self, page: Page) -> bool:
        try:
            positions_nav = page.locator("a[href*='/positions']").first
            if await positions_nav.is_visible():
                await positions_nav.click()
                await page.wait_for_timeout(1000)
            else:
                await page.goto(self.positions_url, wait_until="domcontentloaded", timeout=20000)
            return True
        except Exception as e:
            logger.warning(f"Error navigating to Zerodha positions: {e}")
            return False

    async def extract_position_events(self, page: Page) -> List[BrokerEvent]:
        # Position extraction for Kite
        return []

    async def extract_system_notifications(self, page: Page) -> List[BrokerEvent]:
        # Kite toast / notification extraction
        return []
