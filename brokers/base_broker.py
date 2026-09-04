from __future__ import annotations

import abc
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

from storage.event_store import BrokerEvent
from utils.logger import get_logger

logger = get_logger("broker")


class BaseBroker(abc.ABC):
    """
    Abstract Base Class defining the contract for brokerage website interactions.
    Each supported broker (Dhan, Zerodha, etc.) implements this interface.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Display name of the broker (e.g. 'Dhan', 'Zerodha')."""
        pass

    @property
    @abc.abstractmethod
    def orders_url(self) -> str:
        """Direct URL or relative path to the orders section."""
        pass

    @property
    @abc.abstractmethod
    def positions_url(self) -> str:
        """Direct URL or relative path to the positions section."""
        pass

    @abc.abstractmethod
    async def is_logged_in(self, page: Page) -> bool:
        """
        Checks whether the browser page shows an active, authenticated brokerage session.
        Must return False if login screens, QR codes, or session timeout prompts are visible.
        """
        pass

    @abc.abstractmethod
    async def wait_for_login(self, page: Page, timeout_seconds: int = 300) -> bool:
        """
        Waits for the user to complete login manually in the browser window.
        Does NOT automate passwords, OTPs, or 2FA.
        """
        pass

    @abc.abstractmethod
    async def navigate_to_orders(self, page: Page) -> bool:
        """
        Navigates to the Orders section/tab.
        """
        pass

    @abc.abstractmethod
    async def extract_order_events(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts non-sensitive visible order data from the current page.
        Returns a list of BrokerEvent instances.
        """
        pass

    @abc.abstractmethod
    async def navigate_to_positions(self, page: Page) -> bool:
        """
        Navigates to the Positions section/tab.
        """
        pass

    @abc.abstractmethod
    async def extract_position_events(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts non-sensitive position updates from the current page.
        """
        pass

    @abc.abstractmethod
    async def extract_system_notifications(self, page: Page) -> List[BrokerEvent]:
        """
        Extracts visible toast messages, notification bell alerts, or popups.
        """
        pass

    async def check_session_valid(self, page: Page) -> bool:
        """
        Quick check to ensure session remains active during continuous polling.
        """
        return await self.is_logged_in(page)
