"""Authentication & Interactive OTP Manager.

Manages automated credential submission, detects MFA/OTP requirements,
pauses automation to request interactive user OTP via the GUI, safely enters
the received OTP, verifies post-login session state, and detects session expirations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from services.state_manager import state_mgr
from storage.automation_store import auto_store
from telegram.telegram_service import telegram_service
from utils.logger import get_logger

logger = get_logger("auth_manager")


class AuthManager:
    """Handles end-to-end authentication flows, interactive OTP pause/resume, and session verification."""

    def __init__(self):
        # In-memory synchronization for OTP requests (never persisted to disk for security)
        self._pending_otps: Dict[str, asyncio.Event] = {}
        self._otp_buffers: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Authentication Workflow
    # -------------------------------------------------------------------------

    async def authenticate(self, site: Dict[str, Any], config: Dict[str, Any], page: Page) -> bool:
        """Executes the login workflow according to site configuration.

        Workflow:
            1. Detect if already authenticated
            2. Enter username/identifier if configured
            3. Enter password if configured
            4. Click submit/login button
            5. Check if OTP is required or detected
            6. If OTP required -> Pause & wait for user input from GUI
            7. Submit OTP and verify login
        """
        site_id = site["id"]
        site_name = site["name"]
        site_url = site["url"]

        logger.info(f"[{site_name}] Checking authentication state...")

        # If site does not configure login credentials or selectors, treat as public/open
        u_sel = config.get("username_selector", "").strip()
        u_val = config.get("username_val", "").strip()
        p_sel = config.get("password_selector", "").strip()
        p_val = config.get("password_val", "").strip()
        exp_sel = config.get("expected_auth_selector", "").strip()
        exp_url = config.get("expected_auth_url", "").strip()

        if not (u_sel or u_val or p_sel or p_val or exp_sel or exp_url):
            logger.info(f"[{site_name}] No credentials or auth selectors configured. Treating as public verified workflow.")
            auto_store.update_site_status(site_id, auth_status="NOT_REQUIRED")
            return True

        # 1. Quick check if already authenticated
        if await self.verify_session(config, page):
            logger.info(f"[{site_name}] Session already authenticated and valid.")
            auto_store.update_site_status(site_id, auth_status="AUTHENTICATED")
            return True

        auto_store.update_site_status(site_id, auth_status="AUTHENTICATING")
        state_mgr.add_event(f"[{site_name}] Initiating authentication sequence...", level="INFO")

        # 2. Fill Username / Identifier
        u_sel = config.get("username_selector", "").strip()
        u_val = config.get("username_val", "").strip()
        if u_sel and u_val:
            try:
                logger.info(f"[{site_name}] Entering username into '{u_sel}'...")
                u_elem = await page.wait_for_selector(u_sel, state="visible", timeout=7000)
                if u_elem:
                    await u_elem.fill(u_val)
                    await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"[{site_name}] Username entry notice: {e}")

        # 3. Fill Password
        p_sel = config.get("password_selector", "").strip()
        p_val = config.get("password_val", "").strip()
        if p_sel and p_val:
            try:
                logger.info(f"[{site_name}] Entering password securely...")
                p_elem = await page.wait_for_selector(p_sel, state="visible", timeout=7000)
                if p_elem:
                    await p_elem.fill(p_val)
                    await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"[{site_name}] Password entry notice: {e}")

        # 4. Click Submit / Login button
        s_sel = config.get("submit_selector", "").strip()
        if s_sel:
            try:
                logger.info(f"[{site_name}] Submitting credentials with '{s_sel}'...")
                s_elem = await page.wait_for_selector(s_sel, state="visible", timeout=7000)
                if s_elem:
                    await s_elem.click()
                    await asyncio.sleep(1.5)
            except Exception as e:
                logger.warning(f"[{site_name}] Submit click notice: {e}")

        # 5. Detect OTP requirement
        otp_req_cfg = bool(config.get("otp_required", False))
        otp_sel = config.get("otp_selector", "").strip()

        # Check if OTP input is visible or configured
        otp_prompt_visible = False
        if otp_sel:
            try:
                elem = await page.wait_for_selector(otp_sel, state="visible", timeout=4000)
                if elem:
                    otp_prompt_visible = True
            except Exception:
                pass

        if otp_req_cfg or otp_prompt_visible:
            logger.info(f"[{site_name}] OTP required. Pausing workflow for interactive user entry...")
            auto_store.update_site_status(site_id, auth_status="WAITING_FOR_OTP")
            state_mgr.add_event(f"[{site_name}] 🔐 OTP required. Enter OTP in GUI to proceed.", level="WARNING")

            # Request OTP from user in GUI
            otp_val = await self.request_otp_from_user(site_id, site_name, site_url, timeout_sec=180)

            if not otp_val:
                logger.error(f"[{site_name}] OTP input timed out or was cancelled by user.")
                auto_store.update_site_status(site_id, auth_status="OTP_TIMEOUT")
                return False

            # Submit received OTP into page
            logger.info(f"[{site_name}] Received OTP from GUI. Filling into '{otp_sel or 'input[type=password]'}'...")
            try:
                target_sel = otp_sel or "input[name*='otp'], input[placeholder*='code'], input[type='number']"
                otp_field = await page.wait_for_selector(target_sel, state="visible", timeout=10000)
                if otp_field:
                    await otp_field.fill(otp_val)
                    await asyncio.sleep(0.5)

                    # Click OTP submit button if configured
                    otp_btn_sel = config.get("otp_submit_selector", "").strip()
                    if otp_btn_sel:
                        btn = await page.wait_for_selector(otp_btn_sel, state="visible", timeout=5000)
                        if btn:
                            await btn.click()
                    else:
                        await otp_field.press("Enter")

                    await asyncio.sleep(2.0)
            except Exception as e:
                logger.error(f"[{site_name}] Failed to fill or submit OTP: {e}")
                auto_store.update_site_status(site_id, auth_status="AUTH_FAILED")
                return False

        # 6. Verify final authentication
        is_auth = await self.verify_session(config, page)
        if is_auth:
            logger.info(f"[{site_name}] Authentication verified successfully!")
            auto_store.update_site_status(site_id, auth_status="AUTHENTICATED")
            state_mgr.add_event(f"[{site_name}] Authentication successfully verified.", level="SUCCESS")
            return True
        else:
            logger.warning(f"[{site_name}] Authentication could not be confirmed after login sequence.")
            auto_store.update_site_status(site_id, auth_status="AUTH_UNCONFIRMED")
            return False

    # -------------------------------------------------------------------------
    # Interactive OTP Handling via GUI
    # -------------------------------------------------------------------------

    async def request_otp_from_user(
        self, site_id: str, site_name: str, site_url: str, timeout_sec: int = 180
    ) -> Optional[str]:
        """Pauses the workflow and registers a pending OTP request for GUI interaction."""
        event = asyncio.Event()

        async with self._lock:
            self._pending_otps[site_id] = event
            self._otp_buffers.pop(site_id, None)

        # Notify via Telegram (Safe notice without secrets)
        await telegram_service.send_event(
            f"🔐 *OTP REQUIRED*\n\n"
            f"*Website:* {site_name}\n"
            f"*URL:* {site_url}\n"
            f"*Status:* WAITING_FOR_OTP\n\n"
            f"👉 *Action:* Enter the OTP in the desktop GUI modal to continue."
        )

        logger.info(f"[{site_name}] Waiting up to {timeout_sec}s for user OTP entry in GUI...")
        try:
            await asyncio.wait_for(event.wait(), timeout=float(timeout_sec))
            otp_val = self._otp_buffers.get(site_id)
            return otp_val
        except asyncio.TimeoutError:
            logger.warning(f"[{site_name}] Timed out waiting for user OTP input.")
            return None
        finally:
            async with self._lock:
                self._pending_otps.pop(site_id, None)
                self._otp_buffers.pop(site_id, None)

    def submit_user_otp(self, site_id: str, otp: str) -> bool:
        """Called by FastAPI GUI endpoint when user submits an OTP in the modal."""
        if site_id in self._pending_otps:
            self._otp_buffers[site_id] = otp.strip()
            self._pending_otps[site_id].set()
            logger.info(f"User submitted OTP for site {site_id}. Resuming workflow...")
            return True
        logger.warning(f"No pending OTP request found for site {site_id}.")
        return False

    def is_waiting_for_otp(self, site_id: str) -> bool:
        """Returns True if site is currently paused waiting for OTP input."""
        return site_id in self._pending_otps

    def get_pending_otp_sites(self) -> Dict[str, bool]:
        """Returns a mapping of site_ids waiting for OTP."""
        return {sid: True for sid in self._pending_otps.keys()}

    # -------------------------------------------------------------------------
    # Session Verification & Logout Detection
    # -------------------------------------------------------------------------

    async def verify_session(self, config: Dict[str, Any], page: Page) -> bool:
        """Verifies if the current page represents an authenticated session."""
        expected_url = config.get("expected_auth_url", "").strip()
        expected_sel = config.get("expected_auth_selector", "").strip()
        logout_sel = config.get("logout_selector", "").strip()
        u_sel = config.get("username_selector", "").strip()
        u_val = config.get("username_val", "").strip()
        p_sel = config.get("password_selector", "").strip()
        p_val = config.get("password_val", "").strip()

        # If site doesn't configure authentication parameters, session is always valid
        if not (expected_url or expected_sel or logout_sel or u_sel or u_val or p_sel or p_val):
            return True

        # If expected selector is defined, check presence
        if expected_sel:
            try:
                elem = await page.wait_for_selector(expected_sel, state="visible", timeout=3000)
                if elem:
                    return True
            except Exception:
                pass

        # If logout selector is present, we are authenticated
        if logout_sel:
            try:
                elem = await page.wait_for_selector(logout_sel, state="attached", timeout=3000)
                if elem:
                    return True
            except Exception:
                pass

        # If expected URL is specified and matches current URL
        if expected_url and expected_url.lower() in page.url.lower():
            return True

        # If credentials were configured but no explicit post-login indicator was set,
        # consider verified if login password input is not visible
        if (u_val or p_val) and not (expected_sel or logout_sel or expected_url):
            pass_check_sel = p_sel or "input[type='password']"
            try:
                elem = await page.wait_for_selector(pass_check_sel, state="visible", timeout=1500)
                if not elem:
                    return True
            except Exception:
                return True

        return False

    async def detect_logout_or_expired(self, config: Dict[str, Any], page: Page) -> bool:
        """Returns True if the session was lost, expired, or redirected back to login."""
        expected_sel = config.get("expected_auth_selector", "").strip()
        logout_sel = config.get("logout_selector", "").strip()
        u_val = config.get("username_val", "").strip()
        p_val = config.get("password_val", "").strip()
        pass_sel = config.get("password_selector", "").strip() or "input[type='password']"

        # If auth is not configured, session cannot expire
        if not (expected_sel or logout_sel or u_val or p_val):
            return False

        # Check if login password input suddenly appeared
        try:
            elem = await page.wait_for_selector(pass_sel, state="visible", timeout=1000)
            if elem:
                return True
        except Exception:
            pass

        # Check if expected authenticated element disappeared
        if expected_sel:
            try:
                elem = await page.wait_for_selector(expected_sel, state="visible", timeout=1000)
                if not elem:
                    return True
            except Exception:
                return True

        return False


# Global singleton instance
auth_manager = AuthManager()
