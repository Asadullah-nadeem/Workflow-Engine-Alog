from __future__ import annotations

import asyncio
from typing import Optional
from playwright.async_api import Page, Error as PlaywrightError

from config import AppConfig
from browser.browser_manager import BrowserManager
from brokers.base_broker import BaseBroker
from storage.event_store import EventStore, BrokerEvent
from telegram.telegram_service import TelegramService
from services.state_manager import state_mgr, AppState, BrokerState
from utils.logger import get_logger

logger = get_logger("monitor")


class EventMonitor:
    """
    Core event monitoring engine. Orchestrates browser navigation, session verification,
    event extraction, deduplication, persistent storage, and Telegram broadcasting.
    Fully integrated with centralized StateManager.
    """

    def __init__(
        self,
        config: AppConfig,
        browser_manager: BrowserManager,
        broker: BaseBroker,
        event_store: EventStore,
        telegram_service: TelegramService,
    ):
        self.config = config
        self.browser_manager = browser_manager
        self.broker = broker
        self.event_store = event_store
        self.telegram_service = telegram_service
        self._running = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """
        Starts the continuous monitoring loop safely. Idempotent against duplicate starts.
        """
        async with self._lock:
            if self._running:
                logger.warning("Monitoring engine is already running. Duplicate start ignored.")
                return
            self._running = True
            self._stop_event.clear()

        state_mgr.set_app_state(AppState.RUNNING)
        logger.info(f"Starting Broker Monitoring Engine for [{self.broker.name}]...")
        logger.info(f"Target Broker URL: {self.config.broker_url}")
        logger.info(f"Check Interval: {self.config.check_interval}s")

        # Initial Telegram notification
        await self.telegram_service.send_status(
            app_status="RUNNING",
            chrome_status="STARTING",
            processed_count=state_mgr.records_processed,
            latest_summary=f"Monitoring initiated for {self.broker.name}",
        )

        page: Optional[Page] = None
        cycle_count = 0

        while self._running and not self._stop_event.is_set():
            try:
                # 1. Ensure browser page is available
                if not page or page.is_closed():
                    page = await self.browser_manager.get_page()
                    logger.info(f"Navigating to {self.config.broker_url}...")
                    await page.goto(self.config.broker_url, wait_until="domcontentloaded", timeout=self.config.page_timeout_ms)
                    await asyncio.sleep(2.0)

                # 2. Check session authentication
                is_auth = await self.broker.is_logged_in(page)
                if not is_auth:
                    state_mgr.set_broker_state(BrokerState.UNAUTHENTICATED)
                    logger.warning(f"[{self.broker.name}] Session not logged in. Waiting for user manual authentication...")
                    await self.telegram_service.send_error(
                        error_msg=f"Please log in to your {self.broker.name} account in the Chrome browser window.",
                        context="Session Not Authenticated",
                    )

                    # Wait for user to log in manually (timeout: 600s)
                    logged_in = await self.broker.wait_for_login(page, timeout_seconds=600)
                    if not logged_in:
                        logger.error("User did not log in within timeout. Pausing for 30 seconds before retry...")
                        await asyncio.sleep(30.0)
                        continue

                    state_mgr.set_broker_state(BrokerState.AUTHENTICATED)
                    logger.info(f"[{self.broker.name}] Authentication confirmed!")
                    await self.telegram_service.send_status(
                        app_status="RUNNING",
                        chrome_status="CONNECTED",
                        processed_count=state_mgr.records_processed,
                        latest_summary="Authentication confirmed. Monitoring active.",
                    )
                else:
                    state_mgr.set_broker_state(BrokerState.AUTHENTICATED)

                # 3. Ensure navigation to Orders section
                await self.broker.navigate_to_orders(page)

                # 4. Extract Events (Orders, Trades, Notifications)
                await self._process_events_cycle(page)

                cycle_count += 1
                # Periodically clean up old events in database (every 1000 cycles)
                if cycle_count % 1000 == 0:
                    self.event_store.cleanup_old_events(self.config.event_retention_days)

            except PlaywrightError as pe:
                logger.error(f"Browser error encountered: {pe}")
                state_mgr.record_error(f"Browser error: {pe}")
                page = None
                await asyncio.sleep(5.0)

            except asyncio.CancelledError:
                logger.info("Monitoring task cancelled.")
                break

            except Exception as exc:
                logger.error(f"Unexpected error in monitoring loop: {exc}", exc_info=True)
                state_mgr.record_error(str(exc))
                await asyncio.sleep(5.0)

            # 5. Non-blocking sleep between polling cycles
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.check_interval)
                break
            except asyncio.TimeoutError:
                pass

        async with self._lock:
            self._running = False
        state_mgr.set_app_state(AppState.STOPPED)
        logger.info("Broker Monitoring Engine stopped cleanly.")

    async def _process_events_cycle(self, page: Page) -> None:
        """
        Extracts and processes order events and notifications.
        """
        # A. Order Book Events
        order_events = await self.broker.extract_order_events(page)
        for event in order_events:
            await self._handle_event(event)

        # B. Notification Alerts
        notification_events = await self.broker.extract_system_notifications(page)
        for event in notification_events:
            await self._handle_event(event)

    async def _handle_event(self, event: BrokerEvent) -> None:
        """
        Deduplicates, persists, and dispatches newly detected events to Telegram and StateManager.
        """
        fp = event.fingerprint
        if self.event_store.is_processed(fp):
            logger.debug(f"Skipping duplicate event: {event.symbol} - {event.status} (fp: {fp[:8]}...)")
            return

        summary = f"{event.symbol} {event.order_type}: {event.status} @ ₹{event.price}"
        logger.info(
            f"🔔 NEW EVENT DETECTED: [{event.broker}] {event.event_type} | "
            f"{event.symbol} | {event.order_type} | Qty: {event.quantity} | "
            f"Price: {event.price} | Status: {event.status}"
        )

        # Send Telegram notification (does not crash if Telegram is down)
        dispatch_success = False
        try:
            dispatch_success = await self.telegram_service.send_result(event)
        except Exception as e:
            logger.error(f"Telegram dispatch exception: {e}")

        # Record in persistent SQLite store
        self.event_store.record_event(event, notified=True, notification_success=dispatch_success)

        # Update application state
        state_mgr.record_processed_event(summary=summary, dispatch_success=dispatch_success)

    async def stop(self) -> None:
        """
        Stops the monitoring engine cleanly and safely.
        """
        state_mgr.set_app_state(AppState.STOPPING)
        logger.info("Stopping EventMonitor...")
        self._running = False
        self._stop_event.set()
        try:
            await asyncio.wait_for(self.telegram_service.send_shutdown(), timeout=3.0)
        except Exception:
            pass
        state_mgr.set_app_state(AppState.STOPPED)
