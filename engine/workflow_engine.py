"""Universal Workflow Engine.

Orchestrates multi-URL concurrent automation workers, independent site lifecycles
(STARTING -> RUNNING -> PAUSED -> STOPPING -> STOPPED -> ERROR), credential authentication,
data processing, data storage, repeated cycles, logout detection, and Telegram alerting.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from browser.chrome_manager import chrome_manager
from config import get_config
from engine.auth_manager import auth_manager
from scanner.website_scanner import WebsiteScanner
from scanner.market_analyzer import market_analyzer, StockSnapshot, MarketAnalysisResult
from services.state_manager import state_mgr
from storage.automation_store import auto_store
from storage.event_store import EventStore
from telegram.telegram_service import telegram_service
from utils.logger import get_logger

logger = get_logger("workflow_engine")


class WorkflowEngine:
    """Universal Automation and Workflow Engine for authorized websites."""

    def __init__(self):
        self.config = get_config()
        self._site_tasks: Dict[str, asyncio.Task] = {}
        self._pause_events: Dict[str, asyncio.Event] = {}
        self._stop_flags: Dict[str, bool] = {}
        self._scanner = WebsiteScanner(headless=True)
        # Controlled concurrency semaphore (default 3 concurrent workers)
        self._concurrency_limit = 3
        self._semaphore = asyncio.Semaphore(self._concurrency_limit)
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Site Lifecycle Controls (Start, Stop, Pause, Resume)
    # -------------------------------------------------------------------------

    async def start_site(self, site_id: str) -> bool:
        """Starts automation workflow for a specific authorized site."""
        async with self._lock:
            site = auto_store.get_site(site_id)
            if not site:
                logger.error(f"Cannot start workflow: Site '{site_id}' not found.")
                return False

            # Prevent duplicate executions
            existing_task = self._site_tasks.get(site_id)
            if existing_task and not existing_task.done():
                logger.warning(f"Workflow for site [{site['name']}] is already active.")
                return True

            logger.info(f"Starting workflow for site [{site['name']}] ({site['url']})...")
            auto_store.update_site_status(site_id, automation_status="STARTING")
            state_mgr.add_event(f"[{site['name']}] Workflow initiating...", level="INFO")

            pause_event = asyncio.Event()
            pause_event.set()  # Not paused initially
            self._pause_events[site_id] = pause_event
            self._stop_flags[site_id] = False

            task = asyncio.create_task(self._run_site_worker(site_id))
            self._site_tasks[site_id] = task

            # Send operational Telegram alert
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await telegram_service.send_event(
                f"🤖 *AUTOMATION STARTED*\n\n"
                f"*Website:* {site['name']}\n"
                f"*Status:* RUNNING\n"
                f"*Time:* {now_str}"
            )

            return True

    async def stop_site(self, site_id: str) -> bool:
        """Gracefully stops workflow execution for a specific site."""
        async with self._lock:
            site = auto_store.get_site(site_id)
            name = site["name"] if site else site_id

            logger.info(f"Stopping workflow for site [{name}]...")
            auto_store.update_site_status(site_id, automation_status="STOPPING")
            self._stop_flags[site_id] = True

            # If site is paused, resume it so it can exit cleanly
            if site_id in self._pause_events:
                self._pause_events[site_id].set()

            task = self._site_tasks.get(site_id)
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            self._site_tasks.pop(site_id, None)
            self._pause_events.pop(site_id, None)
            self._stop_flags.pop(site_id, None)

            # Close isolated page tab
            await chrome_manager.close_site_page(site_id)

            auto_store.update_site_status(site_id, automation_status="STOPPED")
            state_mgr.add_event(f"[{name}] Automation stopped.", level="INFO")
            logger.info(f"Site [{name}] workflow cleanly stopped.")
            return True

    async def pause_site(self, site_id: str) -> bool:
        """Pauses the automation workflow for a specific site."""
        site = auto_store.get_site(site_id)
        name = site["name"] if site else site_id

        if site_id in self._pause_events:
            self._pause_events[site_id].clear()
            auto_store.update_site_status(site_id, automation_status="PAUSED")
            state_mgr.add_event(f"[{name}] Automation paused.", level="WARNING")
            logger.info(f"Site [{name}] paused.")
            return True
        return False

    async def resume_site(self, site_id: str) -> bool:
        """Resumes a paused automation workflow for a specific site."""
        site = auto_store.get_site(site_id)
        name = site["name"] if site else site_id

        if site_id in self._pause_events:
            self._pause_events[site_id].set()
            auto_store.update_site_status(site_id, automation_status="RUNNING")
            state_mgr.add_event(f"[{name}] Automation resumed.", level="INFO")
            logger.info(f"Site [{name}] resumed.")
            return True
        return False

    async def start_all(self) -> List[str]:
        """Starts all enabled sites."""
        sites = auto_store.list_sites()
        started = []
        for site in sites:
            if site.get("is_enabled", 1):
                res = await self.start_site(site["id"])
                if res:
                    started.append(site["id"])
        return started

    async def stop_all(self) -> List[str]:
        """Stops all running site workflows."""
        sites = auto_store.list_sites()
        stopped = []
        for site in sites:
            sid = site["id"]
            if sid in self._site_tasks:
                await self.stop_site(sid)
                stopped.append(sid)
        return stopped

    # -------------------------------------------------------------------------
    # Scanning & Discovery Integration
    # -------------------------------------------------------------------------

    async def scan_site(self, site_id: str) -> Dict[str, Any]:
        """Runs the DOM scanner on the target site and updates database configuration."""
        site = auto_store.get_site(site_id)
        if not site:
            return {"success": False, "error": f"Site {site_id} not found."}

        name = site["name"]
        url = site["url"]

        logger.info(f"Triggering DOM scan for [{name}] ({url})...")
        auto_store.update_site_status(site_id, scan_status="SCANNING")
        state_mgr.add_event(f"[{name}] Scanning DOM structure and forms...", level="INFO")

        scan_data = await self._scanner.scan_url(url)

        if scan_data.get("success"):
            auto_store.save_scan_result(site_id, scan_data)
            auth_det = scan_data.get("auth_detection", {})

            # Auto-populate site configuration with suggested selectors if currently empty
            current_cfg = auto_store.get_site_config(site_id)
            updated_cfg = dict(current_cfg)

            if not updated_cfg.get("username_selector") and auth_det.get("suggested_username_selector"):
                updated_cfg["username_selector"] = auth_det["suggested_username_selector"]
            if not updated_cfg.get("password_selector") and auth_det.get("suggested_password_selector"):
                updated_cfg["password_selector"] = auth_det["suggested_password_selector"]
            if not updated_cfg.get("submit_selector") and auth_det.get("suggested_login_btn_selector"):
                updated_cfg["submit_selector"] = auth_det["suggested_login_btn_selector"]
            if not updated_cfg.get("otp_selector") and auth_det.get("suggested_otp_selector"):
                updated_cfg["otp_selector"] = auth_det["suggested_otp_selector"]
                updated_cfg["otp_required"] = 1
            if not updated_cfg.get("otp_submit_selector") and auth_det.get("suggested_otp_btn_selector"):
                updated_cfg["otp_submit_selector"] = auth_det["suggested_otp_btn_selector"]
            if not updated_cfg.get("logout_selector") and auth_det.get("suggested_logout_selector"):
                updated_cfg["logout_selector"] = auth_det["suggested_logout_selector"]

            auto_store.save_site_config(site_id, updated_cfg)
            auto_store.update_site_status(site_id, scan_status="SCANNED")
            state_mgr.add_event(
                f"[{name}] Scan complete: {len(scan_data['forms'])} forms, {len(scan_data['inputs'])} inputs.",
                level="SUCCESS",
            )
        else:
            auto_store.update_site_status(site_id, scan_status="ERROR")
            err_msg = scan_data.get("error", "Unknown scanning error")
            state_mgr.add_event(f"[{name}] Scan failed: {err_msg}", level="ERROR")

        return scan_data

    # -------------------------------------------------------------------------
    # Core Worker Lifecycle (Discrete, independently testable steps)
    # -------------------------------------------------------------------------

    async def _run_site_worker(self, site_id: str) -> None:
        """Worker task executing discrete steps for a given site with concurrency control."""
        async with self._semaphore:
            site = auto_store.get_site(site_id)
            if not site:
                return

            name = site["name"]
            config = auto_store.get_site_config(site_id)
            repeat_interval = config.get("repeat_interval", 10)
            max_retries = config.get("max_retries", 3)
            retry_count = 0

            page = None
            try:
                auto_store.update_site_status(site_id, automation_status="RUNNING")
                state_mgr.add_event(f"[{name}] Workflow worker active.", level="INFO")

                # Step 1: Initialize browser and dedicated page
                page = await self.initialize_browser_page(site_id)

                # Step 2: Open target website
                await self.open_site(site, config, page)

                # Continuous execution loop (supports one-time or recurring intervals)
                while not self._stop_flags.get(site_id, False):
                    # Check pause state
                    pause_evt = self._pause_events.get(site_id)
                    if pause_evt and not pause_evt.is_set():
                        logger.info(f"[{name}] Automation paused. Awaiting resume...")
                        await pause_evt.wait()

                    # Step 3: Detect page state & login requirement
                    needs_login = await self.detect_page(site, config, page)

                    if needs_login:
                        # Step 4 & 5: Authenticate (handles credentials and interactive OTP)
                        auth_success = await self.authenticate(site, config, page)
                        if not auth_success:
                            retry_count += 1
                            if retry_count >= max_retries:
                                logger.error(f"[{name}] Reached max authentication retries ({max_retries}).")
                                await self.handle_error(site, "Authentication", "Exceeded max authentication retries.")
                                break
                            await asyncio.sleep(5.0)
                            continue

                    # Reset retry count after successful state
                    retry_count = 0

                    # Step 6: Execute site workflow & actions
                    workflow_data = await self.execute_workflow(site, config, page)

                    # Step 7: Process retrieved data
                    processed = await self.process_data(site, config, page, workflow_data)

                    # Step 8: Store execution result in database
                    await self.store_data(site_id, processed)

                    # Step 9: Validate result
                    await self.validate_result(site, processed)

                    # Step 10: Check if session expired / logged out
                    is_logout = await self.handle_logout(site, config, page)
                    if is_logout:
                        state_mgr.add_event(f"[{name}] 🔴 Session expired. Pausing for re-authentication.", level="WARNING")
                        await telegram_service.send_event(
                            f"🔴 *SESSION EXPIRED*\n\n"
                            f"*Website:* {name}\n"
                            f"*Status:* LOGIN_REQUIRED\n\n"
                            f"👉 *Action:* Re-authentication required."
                        )
                        auto_store.update_site_status(site_id, auth_status="EXPIRED", automation_status="PAUSED")
                        # Pause execution until user re-authenticates or resumes
                        if site_id in self._pause_events:
                            self._pause_events[site_id].clear()
                        continue

                    # If interval is 0, this is a one-time execution
                    if repeat_interval <= 0:
                        logger.info(f"[{name}] One-time execution completed successfully.")
                        break

                    # Wait for next execution cycle
                    await asyncio.sleep(float(repeat_interval))

            except asyncio.CancelledError:
                logger.info(f"[{name}] Worker task received cancellation request.")
            except Exception as exc:
                logger.exception(f"[{name}] Unhandled worker error: {exc}")
                await self.handle_error(site, "Workflow Worker", str(exc))
            finally:
                await self.cleanup_site(site_id, close_tab=False)
                auto_store.update_site_status(site_id, automation_status="STOPPED")

    # -------------------------------------------------------------------------
    # Discrete Step Implementations
    # -------------------------------------------------------------------------

    async def initialize_browser_page(self, site_id: str):
        """Step: Initializes or retrieves dedicated Chrome tab for this site."""
        return await chrome_manager.get_page_for_site(site_id)

    async def open_site(self, site: Dict[str, Any], config: Dict[str, Any], page) -> None:
        """Step: Navigates to target website URL with automatic retries for transient DNS/network drops."""
        url = site["url"]
        current_url = getattr(page, "url", "") or ""

        # Skip navigation if already at target URL
        if current_url.rstrip("/") == url.rstrip("/"):
            logger.info(f"[{site['name']}] Page already on target URL ({url}). Continuing...")
            return

        timeout_ms = config.get("timeout_sec", 30) * 1000
        logger.info(f"[{site['name']}] Navigating to {url}...")

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:
                err_str = str(exc)
                is_net_err = any(k in err_str for k in (
                    "ERR_NAME_NOT_RESOLVED",
                    "ERR_INTERNET_DISCONNECTED",
                    "ERR_CONNECTION_RESET",
                    "ERR_CONNECTION_TIMED_OUT",
                    "ERR_NETWORK_CHANGED",
                    "Timeout"
                ))
                if is_net_err and attempt < max_attempts:
                    wait_sec = 2.0 * attempt
                    logger.warning(
                        f"[{site['name']}] Network/DNS resolution delay on attempt {attempt}/{max_attempts}: {exc}. "
                        f"Retrying in {wait_sec}s..."
                    )
                    await asyncio.sleep(wait_sec)
                else:
                    # If page was already displaying content, keep running without hard crashing
                    if page.url and page.url not in ("about:blank", "chrome://newtab/"):
                        logger.warning(f"[{site['name']}] Navigation failed but retaining current page: {page.url}")
                        return
                    raise

    async def detect_page(self, site: Dict[str, Any], config: Dict[str, Any], page) -> bool:
        """Step: Analyzes current page and determines if login is required."""
        u_sel = config.get("username_selector", "").strip()
        u_val = config.get("username_val", "").strip()
        p_sel = config.get("password_selector", "").strip()
        p_val = config.get("password_val", "").strip()
        exp_sel = config.get("expected_auth_selector", "").strip()
        exp_url = config.get("expected_auth_url", "").strip()

        # If site does not configure login credentials or selectors, login is not needed
        if not (u_sel or u_val or p_sel or p_val or exp_sel or exp_url):
            return False

        is_auth = await auth_manager.verify_session(config, page)
        return not is_auth

    async def authenticate(self, site: Dict[str, Any], config: Dict[str, Any], page) -> bool:
        """Step: Delegates to AuthManager to execute login and interactive OTP flow."""
        return await auth_manager.authenticate(site, config, page)

    async def execute_workflow(self, site: Dict[str, Any], config: Dict[str, Any], page) -> Dict[str, Any]:
        """Step: Gathers live operational market analysis and telemetry from the site."""
        site_name = site["name"]

        custom_selectors = {
            "region": config.get("market_region_selector", "") or self.config.market_region_selector,
            "row": config.get("stock_row_selector", "") or self.config.stock_row_selector,
            "symbol": config.get("symbol_selector", "") or self.config.symbol_selector,
            "price": config.get("price_selector", "") or self.config.price_selector,
            "change": config.get("change_selector", "") or self.config.change_selector,
            "percent": config.get("change_percent_selector", "") or self.config.change_percent_selector,
        }

        # Run dedicated Stock Market Screen Analyzer
        analysis = await market_analyzer.analyze_page(page, custom_selectors=custom_selectors)

        # Persist structured snapshots in SQLite database
        if analysis.stocks:
            try:
                event_store = EventStore(db_path=self.config.database_path)
                event_store.save_market_snapshots_batch(analysis.stocks)
            except Exception as store_err:
                logger.debug(f"Failed to persist market snapshots: {store_err}")

        # Dispatch Telegram market alert if cooldown and change thresholds are met
        if market_analyzer.should_dispatch_telegram_alert(analysis):
            try:
                sent = await telegram_service.send_market_alert(analysis)
                if sent:
                    logger.info(f"[{site_name}] Dispatched Telegram market alert for {analysis.stocks_detected} stocks.")
                    state_mgr.add_event(f"Telegram market alert sent for {analysis.stocks_detected} stocks.", level="SUCCESS")
                else:
                    logger.warning(f"[{site_name}] Telegram market alert delivery returned False.")
            except Exception as tg_err:
                logger.error(f"[{site_name}] Telegram market alert failed: {tg_err}")

        page_title = analysis.target_page_title or (await page.title())
        current_url = analysis.target_page_url or page.url

        return {
            "site_id": site["id"],
            "page_title": page_title,
            "current_url": current_url,
            "analysis": analysis.dict(),
            "stocks_count": analysis.stocks_detected,
            "gainers_count": len(analysis.top_gainers),
            "decliners_count": len(analysis.top_decliners),
            "timestamp": datetime.now().isoformat(),
        }

    async def process_data(self, site: Dict[str, Any], config: Dict[str, Any], page, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Step: Processes and validates gathered page and market telemetry."""
        site_name = site["name"]
        stocks_count = raw_data.get("stocks_count", 0)
        analysis = raw_data.get("analysis", {})
        stocks = analysis.get("stocks", [])

        if stocks_count > 0 and stocks:
            top = stocks[0]
            sign = "+" if top.get("change", 0) >= 0 else ""
            summary = f"Analyzed {stocks_count} stocks: {top['symbol']} ₹{top['price']:,.2f} ({sign}{top['change_percent']:.2f}% {top['direction']})"
        else:
            summary = f"Active page verified: {raw_data.get('page_title', '')}"

        logger.info(f"[{site_name}] {summary} ({raw_data.get('current_url')})")
        return {
            "site_id": site["id"],
            "records_processed": max(stocks_count, 1),
            "errors_count": 0,
            "summary": summary,
            "details": raw_data,
        }

    async def store_data(self, site_id: str, processed_data: Dict[str, Any]) -> None:
        """Step: Persists execution results to SQLite execution history and updates live metrics."""
        summary = processed_data.get("summary", "Workflow iteration finished cleanly.")
        auto_store.add_execution_history(
            site_id=site_id,
            status="SUCCESS",
            records_processed=processed_data.get("records_processed", 1),
            errors_count=processed_data.get("errors_count", 0),
            summary=summary,
        )
        # Update site record in database for table visibility
        auto_store.update_site_execution(site_id=site_id, result_summary=summary)
        # Update live state metrics for dashboard cards
        state_mgr.record_processed_event(summary=summary, dispatch_success=True)

    async def validate_result(self, site: Dict[str, Any], result: Dict[str, Any]) -> bool:
        """Step: Validates that execution metrics satisfy operational thresholds."""
        return result.get("errors_count", 0) == 0

    async def handle_logout(self, site: Dict[str, Any], config: Dict[str, Any], page) -> bool:
        """Step: Checks if session expiration or logout has occurred."""
        return await auth_manager.detect_logout_or_expired(config, page)

    async def handle_error(self, site: Dict[str, Any], step_name: str, error_msg: str) -> None:
        """Step: Records error in database, updates GUI state, and alerts Telegram."""
        site_id = site["id"]
        site_name = site["name"]
        logger.error(f"[{site_name}] Error at step [{step_name}]: {error_msg}")

        auto_store.update_site_status(site_id, automation_status="ERROR")
        auto_store.add_execution_history(
            site_id=site_id,
            status="ERROR",
            records_processed=0,
            errors_count=1,
            summary=f"Error in {step_name}: {error_msg}",
        )
        state_mgr.add_event(f"[{site_name}] Error in {step_name}: {error_msg}", level="ERROR")

        # Telegram operational error notification with clean formatting
        clean_site = site_name.replace("_", "-")
        clean_step = step_name.replace("_", " ")
        clean_err = error_msg.replace("_", " ").replace("*", "").replace("`", "")[:250]
        await telegram_service.send_event(
            f"⚠️ *AUTOMATION NOTICE*\n\n"
            f"*Website:* {clean_site}\n"
            f"*Step:* {clean_step}\n"
            f"*Status:* ERROR\n"
            f"*Notice:* `{clean_err}`"
        )

    async def cleanup_site(self, site_id: str, close_tab: bool = False) -> None:
        """Step: Resets worker flags and optionally closes browser tab on explicit stop."""
        if close_tab:
            await chrome_manager.close_site_page(site_id)
        self._site_tasks.pop(site_id, None)
        self._pause_events.pop(site_id, None)
        self._stop_flags.pop(site_id, None)


# Global singleton instance
workflow_engine = WorkflowEngine()
