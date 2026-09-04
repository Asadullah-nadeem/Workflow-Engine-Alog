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

logger = get_logger("broker.dhan")


class DhanBroker(BaseBroker):
    """
    Implementation for Dhan Web platform (https://web.dhan.co).
    Extracts orders, trade updates, positions, and notifications using resilient locators.
    """

    def __init__(self, base_url: str = "https://web.dhan.co"):
        super().__init__(base_url=base_url)

    @property
    def name(self) -> str:
        return "Dhan"

    @property
    def orders_url(self) -> str:
        return f"{self.base_url}/orders"

    @property
    def positions_url(self) -> str:
        return f"{self.base_url}/positions"

    async def is_logged_in(self, page: Page) -> bool:
        """
        Determines if Dhan Web is in an authenticated state.
        """
        try:
            current_url = page.url.lower()

            # If on an explicit login or QR scan URL
            if any(path in current_url for path in ["/login", "/qr-login", "/auth", "/signin"]):
                return False

            # Check for unauthenticated indicators in DOM
            login_buttons = page.locator(
                "button:has-text('Log in to Dhan'), button:has-text('Login'), "
                "text='Scan QR to login', text='Enter Mobile Number'"
            )
            if await login_buttons.count() > 0:
                for i in range(await login_buttons.count()):
                    if await login_buttons.nth(i).is_visible():
                        return False

            # Check for authenticated indicators in DOM
            # 1. Navigation items: Orders, Positions, Watchlist, Portfolio
            # 2. User profile avatar / user badge / account ID
            auth_markers = page.locator(
                "a[href*='/orders'], a[href*='/positions'], a[href*='/portfolio'], a[href*='/index'], "
                "a:has-text('Orders'), a:has-text('Positions'), a:has-text('Portfolio'), a:has-text('Home'), "
                "[data-testid='user-profile'], [data-testid='header-profile'], "
                "button:has-text('Orders'), button:has-text('Positions'), "
                ".header-avatar, .user-profile-icon, .profile-badge"
            )
            if await auth_markers.count() > 0:
                for i in range(await auth_markers.count()):
                    if await auth_markers.nth(i).is_visible():
                        return True

            # If page URL is on an internal logged-in route like /index/company, /orders, /positions
            if any(route in current_url for route in ["/index/", "/orders", "/positions", "/portfolio", "/watchlist", "/trader"]):
                return True

            # If page title or body contains Dhan dashboard elements
            title = await page.title()
            if "Dhan" in title and not any(k in title.lower() for k in ["login", "sign in"]):
                if "web.dhan.co" in current_url:
                    return True

            return False
        except Exception as e:
            logger.debug(f"Error checking Dhan login status: {e}")
            return False

    async def wait_for_login(self, page: Page, timeout_seconds: int = 300) -> bool:
        """
        Waits for the user to complete login manually. Never attempts to automate credentials.
        """
        logger.warning(
            "[ACTION REQUIRED] Dhan account is not logged in. "
            "Please complete login manually in the open Chrome window."
        )
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds:
            if await self.is_logged_in(page):
                logger.info("Dhan authenticated session detected successfully.")
                return True
            await asyncio.sleep(2.0)

        logger.error(f"Dhan login timeout ({timeout_seconds}s) expired without successful login.")
        return False

    async def navigate_to_orders(self, page: Page) -> bool:
        """
        Navigates to the Dhan Orders page or activates Orders tab.
        """
        try:
            if "/orders" not in page.url:
                # Try clicking Orders tab in navigation first to avoid full page reload
                orders_tab = page.locator(
                    "a[href*='/orders'], button:has-text('Orders'), div[role='tab']:has-text('Orders')"
                ).first
                if await orders_tab.is_visible():
                    await orders_tab.click()
                    await page.wait_for_timeout(1000)
                else:
                    await page.goto(self.orders_url, wait_until="domcontentloaded", timeout=20000)
            return True
        except Exception as e:
            logger.warning(f"Error navigating to Dhan Orders page: {e}")
            return False

    async def extract_order_events(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts visible order updates from Dhan's Order Book table / lists.
        """
        events: List[BrokerEvent] = []
        try:
            # 1. Identify order table rows or order cards
            # Dhan web uses standard table rows (tr) or card containers in the orders tab
            order_rows = page.locator(
                "table tbody tr, div[data-testid='order-row'], div.order-book-row, div[role='row']"
            )
            count = await order_rows.count()
            if count == 0:
                logger.debug("No order rows found on current Dhan page.")
                return events

            for idx in range(count):
                row = order_rows.nth(idx)
                if not await row.is_visible():
                    continue

                row_text = await row.inner_text()
                if not row_text or len(row_text.strip()) < 5:
                    continue

                # Parse row text using regex and heuristics
                event = self._parse_order_row_text(row_text, idx)
                if event:
                    events.append(event)

        except Exception as e:
            logger.error(f"Error extracting Dhan order events: {e}")

        return events

    def _parse_order_row_text(self, text: str, index: int) -> Optional[BrokerEvent]:
        """
        Parses raw text of an order row into a structured BrokerEvent.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        # Look for status keywords
        status_pattern = re.compile(
            r'\\b(EXECUTED|TRADED|REJECTED|CANCELLED|CANCELED|PENDING|OPEN|TRIGGER PENDING|PARTIALLY FILLED|CONFIRMED)\\b',
            re.IGNORECASE,
        )
        status_match = status_pattern.search(text)
        status = status_match.group(0).upper() if status_match else "UNKNOWN"

        # Determine event type
        if status in ("EXECUTED", "TRADED"):
            event_type = "Order Executed"
        elif status in ("REJECTED", "FAILED"):
            event_type = "Order Rejected"
        elif status in ("CANCELLED", "CANCELED"):
            event_type = "Order Cancelled"
        elif status in ("PENDING", "OPEN", "TRIGGER PENDING"):
            event_type = "Order Placed / Pending"
        else:
            event_type = f"Order {status.title()}"

        # Look for BUY / SELL side
        side_match = re.search(r'\\b(BUY|SELL|B|S)\\b', text, re.IGNORECASE)
        order_side = side_match.group(0).upper() if side_match else "N/A"
        if order_side == "B":
            order_side = "BUY"
        elif order_side == "S":
            order_side = "SELL"

        # Look for Symbol / Scrip name (typically top token or line with NSE/BSE)
        symbol = "N/A"
        for line in lines:
            # Common ticker patterns (e.g. RELIANCE, NIFTY24OCTFUT, INFY)
            clean = re.sub(r'[^A-Za-z0-9_ -]', '', line).strip()
            if clean and not status_pattern.search(clean) and not re.match(r'^(BUY|SELL|NSE|BSE|CNC|MIS|NRML|LIMIT|MARKET)$', clean, re.IGNORECASE):
                if any(char.isupper() for char in clean):
                    symbol = clean
                    break

        # Look for Quantity (e.g., 10/10, 50, 100 Qty)
        qty_match = re.search(r'(\\d+(?:/\\d+)?)\\s*(?:Qty|Shares)?', text, re.IGNORECASE)
        quantity = qty_match.group(1) if qty_match else "-"

        # Look for Price (e.g. ?2,500.50, 2500.50, Avg. 140.20)
        price_match = re.search(r'(?:?|Rs\\.?|@|Avg\\.?\\s*)?\\s*([0-9,]+\\.\\d{2})', text)
        price = f"?{price_match.group(1)}" if price_match else "-"

        # Look for Time (e.g. 10:35:12 AM, 14:20:00)
        time_match = re.search(r'\\b(\\d{1,2}:\\d{2}(?::\\d{2})?\\s*(?:AM|PM)?)\\b', text, re.IGNORECASE)
        time_str = time_match.group(1) if time_match else datetime.now().strftime("%I:%M:%S %p")

        # Look for Order ID or generate reliable pseudo ID from symbol + time + side
        order_id_match = re.search(r'\\b(?:ID|Order ID|#):?\\s*([0-9]{8,16})\\b', text, re.IGNORECASE)
        order_id = order_id_match.group(1) if order_id_match else f"DHAN-{symbol}-{time_str}-{index}"

        return BrokerEvent(
            id=order_id,
            broker=self.name,
            event_type=event_type,
            symbol=symbol,
            order_type=order_side,
            quantity=quantity,
            price=price,
            status=status,
            time_str=time_str,
            metadata={"raw_snippet": text[:200]},
        )

    async def navigate_to_positions(self, page: Page) -> bool:
        """
        Navigates to the Positions tab on Dhan.
        """
        try:
            positions_tab = page.locator(
                "a[href*='/positions'], button:has-text('Positions'), div[role='tab']:has-text('Positions')"
            ).first
            if await positions_tab.is_visible():
                await positions_tab.click()
                await page.wait_for_timeout(1000)
            else:
                await page.goto(self.positions_url, wait_until="domcontentloaded", timeout=20000)
            return True
        except Exception as e:
            logger.warning(f"Error navigating to Dhan Positions: {e}")
            return False

    async def extract_position_events(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts position summaries or changes from Dhan Positions tab.
        """
        # Dhan positions table parsing
        events: List[BrokerEvent] = []
        try:
            position_rows = page.locator(
                "table tbody tr, div[data-testid='position-row'], div.position-row"
            )
            count = await position_rows.count()
            for idx in range(count):
                row = position_rows.nth(idx)
                if not await row.is_visible():
                    continue
                text = await row.inner_text()
                if not text or len(text.strip()) < 5:
                    continue

                # Look for P&L or position data
                pnl_match = re.search(r'(?:P&L|PnL|Net):?\\s*([+-]???[0-9,]+\\.\\d{2})', text, re.IGNORECASE)
                qty_match = re.search(r'([+-]?\\d+)\\s*(?:Qty|Net)?', text)

                if pnl_match or qty_match:
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    symbol = lines[0] if lines else "POSITION"
                    pnl = pnl_match.group(1) if pnl_match else "-"
                    qty = qty_match.group(1) if qty_match else "-"

                    events.append(
                        BrokerEvent(
                            id=f"POS-{symbol}-{datetime.now().strftime('%Y%m%d')}",
                            broker=self.name,
                            event_type="Position Update",
                            symbol=symbol,
                            order_type="POSITION",
                            quantity=qty,
                            price=f"P&L: {pnl}",
                            status="OPEN",
                            time_str=datetime.now().strftime("%I:%M:%S %p"),
                            metadata={"pnl": pnl},
                        )
                    )
        except Exception as e:
            logger.debug(f"Error extracting Dhan position events: {e}")
        return events

    async def extract_system_notifications(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts visible toast alerts, popups, or notification drawer items.
        """
        events: List[BrokerEvent] = []
        try:
            toasts = page.locator(
                ".toast, .notification-item, .alert-toast, div[role='alert'], div[data-testid='toast']"
            )
            count = await toasts.count()
            for idx in range(count):
                toast = toasts.nth(idx)
                if await toast.is_visible():
                    msg = await toast.inner_text()
                    if msg and len(msg.strip()) > 3:
                        events.append(
                            BrokerEvent(
                                id=f"NOTIF-{hash(msg.strip())}",
                                broker=self.name,
                                event_type="Account Notification",
                                symbol="BROKER-ALERT",
                                order_type="ALERT",
                                quantity="-",
                                price="-",
                                status="INFO",
                                time_str=datetime.now().strftime("%I:%M:%S %p"),
                                metadata={"message": msg.strip()},
                            )
                        )
        except Exception as e:
            logger.debug(f"Error extracting Dhan notifications: {e}")
        return events
