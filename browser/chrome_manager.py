"""Dedicated Chrome Automation Manager.

Manages Chrome browser lifecycle, multi-tab execution, DOM navigation,
element finding, safe action execution, health monitoring, and crash recovery.
Synchronizes live status with the StateManager.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    ElementHandle,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from config import AppConfig, get_config
from services.state_manager import state_mgr, ChromeState
from utils.logger import get_logger

logger = get_logger("chrome_manager")


class ChromeManager:
    """Enterprise-grade Chrome Browser Manager.

    Provides high-level automation methods:
    start(), connect(), open_url(), navigate(), find_element(),
    wait_for_element(), execute_action(), check_health(), stop(), cleanup(), restart().
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or get_config()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._primary_page: Optional[Page] = None
        self._pages_by_site: Dict[str, Page] = {}
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Lifecycle & Startup
    # -------------------------------------------------------------------------

    async def start(self, headless: Optional[bool] = None) -> Page:
        """Starts Chrome browser with persistent profile or dedicated fallback profile."""
        async with self._lock:
            if self._primary_page and not self._primary_page.is_closed():
                state_mgr.set_chrome_state(ChromeState.CONNECTED)
                return self._primary_page

            is_headless = headless if headless is not None else self.config.headless
            state_mgr.set_chrome_state(ChromeState.STARTING)
            logger.info(f"Starting Chrome browser (headless={is_headless}, mode={self.config.browser_mode})...")

            self._playwright = await async_playwright().start()

            try:
                if self.config.browser_mode == "cdp":
                    await self._connect_cdp()
                else:
                    await self._launch_persistent(is_headless)

                state_mgr.set_chrome_state(ChromeState.CONNECTED)
                logger.info("Chrome browser started and ready.")
                self._bring_to_windows_foreground()
            except Exception as exc:
                err_msg = str(exc)
                logger.error(f"Failed to start Chrome: {err_msg}")
                state_mgr.set_chrome_state(ChromeState.ERROR, error=err_msg)
                await self._force_cleanup()
                raise

            return self._primary_page

    def _bring_to_windows_foreground(self) -> None:
        """Brings the Chrome browser window to the foreground on Windows."""
        if os.name != "nt":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32

            def enum_proc(hwnd, lParam):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        title = buff.value.lower()
                        if any(k in title for k in ("chrome", "tradingview", "dhan", "google", "devtools")):
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                            user32.SetForegroundWindow(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except Exception as e:
            logger.debug(f"Window foreground notice: {e}")

    async def connect(self, cdp_url: Optional[str] = None) -> Page:
        """Connects to an existing Chrome instance via Chrome DevTools Protocol."""
        async with self._lock:
            target_url = cdp_url or self.config.cdp_url
            state_mgr.set_chrome_state(ChromeState.STARTING)
            logger.info(f"Connecting to Chrome via CDP at {target_url}...")
            self._playwright = await async_playwright().start()

            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(target_url)
                contexts = self._browser.contexts
                if contexts:
                    self._context = contexts[0]
                else:
                    self._context = await self._browser.new_context()

                pages = self._context.pages
                self._primary_page = pages[0] if pages else await self._context.new_page()
                self._configure_page_timeouts(self._primary_page)

                state_mgr.set_chrome_state(ChromeState.CONNECTED)
                logger.info("Successfully connected to Chrome via CDP.")
                return self._primary_page
            except Exception as exc:
                state_mgr.set_chrome_state(ChromeState.ERROR, error=str(exc))
                await self._force_cleanup()
                raise

    async def _launch_persistent(self, is_headless: bool) -> None:
        """Launches Chrome using persistent profile with automatic collision fallback."""
        user_data_path = Path(self.config.chrome_user_data_dir).resolve()
        profile_dir = self.config.chrome_profile_dir

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
            "--start-maximized",
            "--disable-dev-shm-usage",
            "--remote-debugging-port=9222",
            "--auto-open-devtools-for-tabs",
        ]

        fallback_path = Path("chrome_profile").resolve()
        fallback_path.mkdir(parents=True, exist_ok=True)

        # Clear stale lock files in dedicated fallback directory to avoid ProcessSingleton conflicts
        for lock_name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            lf = fallback_path / lock_name
            if lf.exists():
                try:
                    lf.unlink()
                except Exception:
                    pass

        target_user_data = user_data_path
        # Check if personal Chrome directory is locked by an active Chrome instance
        is_personal_chrome = "google\\chrome\\user data" in str(user_data_path).lower()
        lock_file = user_data_path / "lockfile"
        singleton_lock = user_data_path / "SingletonLock"

        if is_personal_chrome and (lock_file.exists() or singleton_lock.exists()):
            logger.info(
                f"Active personal Chrome detected with lockfile. "
                f"Using dedicated automation profile directory at '{fallback_path}'..."
            )
            target_user_data = fallback_path

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(target_user_data),
            "headless": is_headless,
            "args": args if target_user_data == fallback_path else [f"--profile-directory={profile_dir}"] + args,
            "no_viewport": True,
            "ignore_default_args": ["--enable-automation"],
        }

        # Auto-detect real Google Chrome executable path on Windows if not configured
        chrome_exe = self.config.chrome_executable_path
        if not chrome_exe:
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
            for c in candidates:
                if c and Path(c).exists():
                    chrome_exe = str(c)
                    break

        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe
        else:
            launch_kwargs["channel"] = "chrome"

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            logger.warning(
                f"Chrome profile conflict on '{target_user_data}': {exc}. "
                f"Switching to dedicated automation profile: {fallback_path}"
            )
            launch_kwargs["user_data_dir"] = str(fallback_path)
            launch_kwargs["args"] = args
            # Keep channel='chrome' so real Google Chrome launches
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as chrome_exc:
                logger.warning(f"Failed to launch with system Chrome channel: {chrome_exc}. Falling back to bundled Chromium.")
                launch_kwargs.pop("channel", None)
                launch_kwargs.pop("executable_path", None)
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        pages = self._context.pages
        self._primary_page = pages[0] if pages else await self._context.new_page()
        self._configure_page_timeouts(self._primary_page)

    async def _connect_cdp(self) -> None:
        """Internal helper to connect via CDP."""
        self._browser = await self._playwright.chromium.connect_over_cdp(self.config.cdp_url)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else await self._browser.new_context()
        pages = self._context.pages
        self._primary_page = pages[0] if pages else await self._context.new_page()
        self._configure_page_timeouts(self._primary_page)

    def _configure_page_timeouts(self, page: Page) -> None:
        """Configures default navigation and action timeouts on page."""
        page.set_default_timeout(self.config.page_timeout_ms)
        page.set_default_navigation_timeout(self.config.page_timeout_ms)

    # -------------------------------------------------------------------------
    # Navigation & URL Operations
    # -------------------------------------------------------------------------

    async def open_url(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: Optional[int] = None,
        site_id: Optional[str] = None,
    ) -> Page:
        """Opens a target URL in the specified or primary page."""
        page = await self.get_page_for_site(site_id) if site_id else await self.get_page()
        timeout = timeout_ms or self.config.page_timeout_ms
        logger.info(f"Opening URL: {url} (wait_until={wait_until}, timeout={timeout}ms)")

        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            self._bring_to_windows_foreground()
        except Exception as exc:
            logger.warning(f"Navigation to {url} encountered notice: {exc}")

        return page

    async def navigate(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: Optional[int] = None,
        site_id: Optional[str] = None,
    ) -> Page:
        """Alias for open_url."""
        return await self.open_url(url, wait_until, timeout_ms, site_id)

    # -------------------------------------------------------------------------
    # Element Interaction & Action Execution
    # -------------------------------------------------------------------------

    async def find_element(
        self,
        selector: str,
        timeout_ms: int = 5000,
        site_id: Optional[str] = None,
    ) -> Optional[ElementHandle]:
        """Finds an element by selector, returning None if not found within timeout."""
        page = await self.get_page_for_site(site_id) if site_id else await self.get_page()
        try:
            return await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        except Exception:
            return None

    async def wait_for_element(
        self,
        selector: str,
        state: str = "visible",
        timeout_ms: int = 10000,
        site_id: Optional[str] = None,
    ) -> Optional[ElementHandle]:
        """Waits for an element to reach specified state ('visible', 'attached', 'hidden', 'detached')."""
        page = await self.get_page_for_site(site_id) if site_id else await self.get_page()
        try:
            return await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        except Exception as exc:
            logger.debug(f"Element '{selector}' state '{state}' wait timed out: {exc}")
            return None

    async def execute_action(
        self,
        action: str,
        selector: str,
        value: Optional[str] = None,
        timeout_ms: int = 10000,
        site_id: Optional[str] = None,
    ) -> bool:
        """Executes a structured action on the page.

        Supported actions: 'click', 'fill', 'type', 'press', 'check', 'uncheck', 'hover', 'focus'.
        """
        page = await self.get_page_for_site(site_id) if site_id else await self.get_page()
        act = action.lower().strip()

        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout_ms)

            if act == "click":
                await loc.click(timeout=timeout_ms)
            elif act == "fill":
                await loc.fill(value or "", timeout=timeout_ms)
            elif act == "type":
                await loc.type(value or "", delay=30, timeout=timeout_ms)
            elif act == "press":
                await loc.press(value or "Enter", timeout=timeout_ms)
            elif act == "check":
                await loc.check(timeout=timeout_ms)
            elif act == "uncheck":
                await loc.uncheck(timeout=timeout_ms)
            elif act == "hover":
                await loc.hover(timeout=timeout_ms)
            elif act == "focus":
                await loc.focus(timeout=timeout_ms)
            else:
                logger.error(f"Unsupported action '{action}' for selector '{selector}'")
                return False

            return True
        except Exception as exc:
            logger.error(f"Action '{action}' on selector '{selector}' failed: {exc}")
            return False

    # -------------------------------------------------------------------------
    # Multi-Site Page Management
    # -------------------------------------------------------------------------

    async def get_page(self) -> Page:
        """Returns the primary Page, starting browser if not already active."""
        if not self._primary_page or self._primary_page.is_closed():
            return await self.start()
        return self._primary_page

    async def get_page_for_site(self, site_id: str) -> Page:
        """Retrieves or creates a dedicated browser tab/page for a specific site workflow."""
        if not self._context or (self._primary_page and self._primary_page.is_closed()):
            await self.start()

        assert self._context is not None

        if site_id in self._pages_by_site:
            page = self._pages_by_site[site_id]
            if not page.is_closed():
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return page

        # If primary page is on about:blank or not assigned, reuse it
        if self._primary_page and not self._primary_page.is_closed():
            assigned_pages = set(self._pages_by_site.values())
            if self._primary_page not in assigned_pages and (self._primary_page.url in ("about:blank", "") or not self._pages_by_site):
                self._pages_by_site[site_id] = self._primary_page
                try:
                    await self._primary_page.bring_to_front()
                except Exception:
                    pass
                return self._primary_page

        # Create new tab for this site
        page = await self._context.new_page()
        self._configure_page_timeouts(page)
        self._pages_by_site[site_id] = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return page

    async def close_site_page(self, site_id: str) -> None:
        """Closes the tab associated with a specific site."""
        if site_id in self._pages_by_site:
            page = self._pages_by_site.pop(site_id)
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Chromium Remote Debugging & DOM Inspection API
    # -------------------------------------------------------------------------

    async def reconnect(self, headless: Optional[bool] = None) -> Page:
        """Controlled reconnect handling for Chromium."""
        logger.info("Executing controlled Chromium reconnect...")
        return await self.restart(headless=headless)

    async def get_pages(self) -> List[Page]:
        """Returns all open pages/tabs in the current browser context."""
        if not self._context:
            return []
        return [p for p in self._context.pages if not p.is_closed()]

    async def get_active_page(self) -> Page:
        """Locates the currently active, primary, or last focused page."""
        pages = await self.get_pages()
        if not pages:
            return await self.get_page()
        if self._primary_page and not self._primary_page.is_closed():
            return self._primary_page
        return pages[-1]

    async def inspect_dom(self, selector: Optional[str] = None, page: Optional[Page] = None) -> Dict[str, Any]:
        """Inspects DOM elements, attributes, visibility, and bounding rectangles."""
        target_page = page or await self.get_active_page()
        script = """
        (sel) => {
            const el = sel ? document.querySelector(sel) : document.body;
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            return {
                tagName: el.tagName.toLowerCase(),
                id: el.id || '',
                className: String(el.className || ''),
                innerText: (el.innerText || '').slice(0, 500),
                rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
                childCount: el.children ? el.children.length : 0,
                attributes: Array.from(el.attributes || []).map(a => ({ name: a.name, value: a.value }))
            };
        }
        """
        try:
            return await target_page.evaluate(script, selector) or {}
        except Exception as exc:
            logger.debug(f"DOM inspection notice: {exc}")
            return {"error": str(exc)}

    async def execute_javascript(self, script: str, args: Optional[Any] = None, page: Optional[Page] = None) -> Any:
        """Executes JavaScript within the browser page context."""
        target_page = page or await self.get_active_page()
        return await target_page.evaluate(script, args)

    async def get_page_state(self, page: Optional[Page] = None) -> Dict[str, Any]:
        """Retrieves URL, title, readyState, viewport, and load state."""
        target_page = page or await self.get_active_page()
        try:
            url = target_page.url
            title = await target_page.title()
            ready_state = await target_page.evaluate("() => document.readyState")
            return {
                "url": url,
                "title": title,
                "readyState": ready_state,
                "is_closed": target_page.is_closed(),
            }
        except Exception as exc:
            return {"error": str(exc), "is_closed": True}

    async def capture_screenshot(
        self,
        path: Optional[str] = None,
        page: Optional[Page] = None,
        clip: Optional[Dict[str, float]] = None
    ) -> bytes:
        """Captures page or regional screenshot."""
        target_page = page or await self.get_active_page()
        kwargs: Dict[str, Any] = {"type": "png"}
        if path:
            kwargs["path"] = path
        if clip:
            kwargs["clip"] = clip
        return await target_page.screenshot(**kwargs)

    # -------------------------------------------------------------------------
    # Health Monitoring, Diagnostics & State
    # -------------------------------------------------------------------------

    async def check_health(self) -> Dict[str, Any]:
        """Checks browser connectivity, tabs count, active page URL, and responsiveness."""
        is_connected = bool(self._context and self._primary_page and not self._primary_page.is_closed())

        current_url = "about:blank"
        title = ""
        open_tabs = 0

        if is_connected and self._primary_page:
            try:
                current_url = self._primary_page.url
                title = await self._primary_page.title()
                if self._context:
                    open_tabs = len(self._context.pages)
            except Exception as exc:
                logger.debug(f"Chrome health check read warning: {exc}")
                is_connected = False

        status_str = "CONNECTED" if is_connected else "DISCONNECTED"
        return {
            "status": status_str,
            "connected": is_connected,
            "current_url": current_url,
            "title": title,
            "open_tabs": open_tabs,
            "active_site_workers": len(self._pages_by_site),
        }

    # -------------------------------------------------------------------------
    # Shutdown, Cleanup & Restart
    # -------------------------------------------------------------------------

    async def stop(self) -> None:
        """Gracefully stops Chrome browser session."""
        await self.cleanup()

    async def cleanup(self) -> None:
        """Cleanly closes all pages, contexts, and Playwright instances."""
        async with self._lock:
            await self._force_cleanup()
            state_mgr.set_chrome_state(ChromeState.DISCONNECTED)
            logger.info("Chrome browser cleaned up and disconnected.")

    async def restart(self, headless: Optional[bool] = None) -> Page:
        """Restarts Chrome browser after a crash or disconnection."""
        logger.warning("Restarting Chrome browser session...")
        await self.cleanup()
        await asyncio.sleep(1.0)
        return await self.start(headless=headless)

    async def _force_cleanup(self) -> None:
        """Internal cleanup helper without locking."""
        for site_id, page in list(self._pages_by_site.items()):
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self._pages_by_site.clear()

        try:
            if self._primary_page and not self._primary_page.is_closed():
                await self._primary_page.close()
        except Exception:
            pass
        self._primary_page = None

        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None


# Global singleton instance for easy import across modules
chrome_manager = ChromeManager()
ChromiumManager = ChromeManager
