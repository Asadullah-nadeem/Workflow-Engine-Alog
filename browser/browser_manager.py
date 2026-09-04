from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Playwright, BrowserContext, Page, Error as PlaywrightError

from config import AppConfig
from services.state_manager import state_mgr, ChromeState
from utils.logger import get_logger

logger = get_logger("browser")


class BrowserManager:
    """
    Manages Playwright browser lifecycle with support for existing Chrome profile
    reuse, Chrome DevTools Protocol (CDP) attachment, and automatic isolated fallback.
    Synchronizes browser status directly with the centralized StateManager.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    async def start(self) -> Page:
        """
        Initializes Playwright and launches/connects to the browser.
        Returns the active Page instance.
        """
        async with self._lock:
            if self._page and not self._page.is_closed():
                state_mgr.set_chrome_state(ChromeState.CONNECTED)
                return self._page

            state_mgr.set_chrome_state(ChromeState.STARTING)
            logger.info(f"Starting browser manager in '{self.config.browser_mode}' mode...")
            self._playwright = await async_playwright().start()

            try:
                if self.config.browser_mode == "cdp":
                    await self._init_cdp_mode()
                else:
                    await self._init_persistent_profile_mode()
                state_mgr.set_chrome_state(ChromeState.CONNECTED)
            except Exception as exc:
                state_mgr.set_chrome_state(ChromeState.ERROR, error=str(exc))
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                raise

            return self._page

    async def _init_persistent_profile_mode(self) -> None:
        """
        Launches Google Chrome using the user's Chrome profile directory.
        Falls back automatically to dedicated automation profile if personal Chrome is open.
        """
        user_data_path = Path(self.config.chrome_user_data_dir).resolve()
        logger.info(f"Using Chrome User Data directory: {user_data_path}")
        logger.info(f"Using Chrome Profile: {self.config.chrome_profile_dir}")

        if not user_data_path.exists():
            logger.warning(
                f"Chrome User Data directory does not exist at '{user_data_path}'. "
                f"A new persistent profile directory will be created."
            )
            user_data_path.mkdir(parents=True, exist_ok=True)

        args = [
            f"--profile-directory={self.config.chrome_profile_dir}",
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
            "--start-maximized",
        ]

        launch_kwargs = {
            "user_data_dir": str(user_data_path),
            "headless": self.config.headless,
            "args": args,
            "no_viewport": True,
            "ignore_default_args": ["--enable-automation"],
        }

        # Prefer system Google Chrome channel
        if self.config.chrome_executable_path:
            launch_kwargs["executable_path"] = self.config.chrome_executable_path
        else:
            launch_kwargs["channel"] = "chrome"

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            err_msg = str(exc)
            fallback_path = Path("chrome_profile").resolve()
            if user_data_path != fallback_path:
                logger.warning(
                    f"[PROFILE LOCKED / BUSY] Primary Chrome profile is currently open or locked.\n"
                    f"Automatically switching to dedicated automation profile at '{fallback_path}'..."
                )
                fallback_path.mkdir(parents=True, exist_ok=True)
                launch_kwargs["user_data_dir"] = str(fallback_path)
                # Strip --profile-directory flag and system channel for isolated fallback
                launch_kwargs["args"] = [a for a in launch_kwargs.get("args", []) if not a.startswith("--profile-directory=")]
                launch_kwargs.pop("channel", None)
                launch_kwargs.pop("executable_path", None)
                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                except Exception as fallback_exc:
                    logger.error(f"Fallback automation browser launch failed: {fallback_exc}")
                    raise exc
            else:
                raise

        # Obtain or create active page
        pages = self._context.pages
        if pages:
            self._page = pages[0]
        else:
            self._page = await self._context.new_page()

        self._page.set_default_timeout(self.config.page_timeout_ms)
        self._page.set_default_navigation_timeout(self.config.page_timeout_ms)

        if self._page.url == "about:blank" or not self._page.url:
            try:
                logger.info(f"Navigating initial browser tab to {self.config.broker_url}...")
                await self._page.goto(self.config.broker_url, wait_until="domcontentloaded", timeout=self.config.page_timeout_ms)
            except Exception as e:
                logger.warning(f"Initial navigation to {self.config.broker_url} warning: {e}")

        logger.info("Persistent Chrome session initialized successfully.")

    async def _init_cdp_mode(self) -> None:
        """
        Attaches to an already running Chrome instance over Chrome DevTools Protocol (CDP).
        """
        logger.info(f"Connecting to existing Chrome instance via CDP at {self.config.cdp_url}...")
        try:
            browser = await self._playwright.chromium.connect_over_cdp(self.config.cdp_url)
            contexts = browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await browser.new_context()

            pages = self._context.pages
            if pages:
                self._page = pages[0]
            else:
                self._page = await self._context.new_page()

            self._page.set_default_timeout(self.config.page_timeout_ms)
            logger.info("Connected to Chrome via CDP successfully.")
        except Exception as exc:
            logger.error(
                f"Failed to connect to Chrome at {self.config.cdp_url}. "
                f"Ensure Chrome was started with: chrome.exe --remote-debugging-port=9222. Error: {exc}"
            )
            raise

    async def get_page(self) -> Page:
        """
        Returns the active Page instance, starting the browser if not already active.
        """
        if not self._page or self._page.is_closed():
            return await self.start()
        return self._page

    async def is_alive(self) -> bool:
        """
        Checks whether the browser context and page are active.
        """
        try:
            return bool(self._page and not self._page.is_closed() and self._context)
        except Exception:
            return False

    async def restart(self) -> Page:
        """
        Restarts the browser session in case of unexpected crash or disconnect.
        """
        logger.warning("Restarting browser manager session...")
        await self.close()
        await asyncio.sleep(1.0)
        return await self.start()

    async def close(self) -> None:
        """
        Cleanly closes page, context, and Playwright process.
        """
        async with self._lock:
            try:
                if self._page and not self._page.is_closed():
                    await self._page.close()
            except Exception as e:
                logger.debug(f"Error closing page: {e}")
            finally:
                self._page = None

            try:
                if self._context:
                    await self._context.close()
            except Exception as e:
                logger.debug(f"Error closing browser context: {e}")
            finally:
                self._context = None

            try:
                if self._playwright:
                    await self._playwright.stop()
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"Error stopping Playwright: {e}")
            finally:
                self._playwright = None

            state_mgr.set_chrome_state(ChromeState.DISCONNECTED)
            logger.info("Browser manager closed.")
