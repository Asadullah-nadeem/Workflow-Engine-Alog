"""Live Website & Form Scanner.

Analyzes authorized target websites, extracts DOM structures, discovers forms,
identifies input types (usernames, passwords, OTP/MFA, email, text), buttons,
interactive elements, and detects login/logout states with heuristic CSS selector generation.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from utils.logger import get_logger

logger = get_logger("scanner")

# JavaScript evaluation script that executes directly within the browser page context
DOM_SCANNER_JS = """
() => {
    const getSelector = (el) => {
        if (!el) return '';
        if (el.id && !el.id.match(/^\\d/) && !el.id.includes(':')) {
            return '#' + CSS.escape(el.id);
        }
        if (el.name) {
            return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
        }
        if (el.getAttribute('data-testid')) {
            return `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
        }
        if (el.getAttribute('placeholder')) {
            return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(el.getAttribute('placeholder'))}"]`;
        }
        if (el.className && typeof el.className === 'string') {
            const classes = el.className.trim().split(/\\s+/).filter(c => c && !c.includes(':') && !c.includes('/'));
            if (classes.length > 0) {
                return `${el.tagName.toLowerCase()}.${CSS.escape(classes[0])}`;
            }
        }
        return el.tagName.toLowerCase();
    };

    const getLabelText = (el) => {
        if (!el) return '';
        if (el.id) {
            const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (label) return label.innerText.trim();
        }
        const parentLabel = el.closest('label');
        if (parentLabel) return parentLabel.innerText.trim();
        return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    };

    const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && el.offsetWidth > 0;
    };

    // 1. Scan Forms
    const forms = Array.from(document.querySelectorAll('form')).map((form, index) => {
        const formInputs = Array.from(form.querySelectorAll('input, select, textarea')).map(getSelector);
        return {
            id: form.id || `form_${index + 1}`,
            name: form.getAttribute('name') || '',
            action: form.getAttribute('action') || '',
            method: (form.getAttribute('method') || 'GET').toUpperCase(),
            selector: getSelector(form),
            inputs_count: formInputs.length,
            contained_inputs: formInputs
        };
    });

    // 2. Scan Inputs
    const otpKeywords = ['otp', 'totp', '2fa', 'mfa', 'code', 'pin', 'verification', 'passcode', 'token'];
    const userKeywords = ['user', 'username', 'login', 'email', 'account', 'userid', 'mobile', 'phone', 'identifier'];
    const passKeywords = ['pass', 'password', 'pwd', 'secret'];

    const inputs = Array.from(document.querySelectorAll('input, textarea, select')).map(el => {
        const type = (el.getAttribute('type') || (el.tagName === 'TEXTAREA' ? 'textarea' : 'text')).toLowerCase();
        const id = el.id || '';
        const name = el.name || el.getAttribute('name') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const label = getLabelText(el);
        const autocomplete = el.getAttribute('autocomplete') || '';
        const combinedMeta = `${id} ${name} ${placeholder} ${label} ${autocomplete}`.toLowerCase();

        const isOtp = otpKeywords.some(k => combinedMeta.includes(k)) && type !== 'password';
        const isPass = type === 'password' || passKeywords.some(k => combinedMeta.includes(k));
        const isUser = !isPass && !isOtp && (
            type === 'email' ||
            autocomplete.includes('username') ||
            autocomplete.includes('email') ||
            userKeywords.some(k => combinedMeta.includes(k))
        );

        return {
            tag: el.tagName.toLowerCase(),
            type: type,
            id: id,
            name: name,
            placeholder: placeholder,
            label: label,
            autocomplete: autocomplete,
            visible: isVisible(el),
            selector: getSelector(el),
            is_username_candidate: isUser,
            is_password_candidate: isPass,
            is_otp_candidate: isOtp
        };
    });

    // 3. Scan Buttons & Actions
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a[role="button"], a.btn, a.button')).map(el => {
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        const lowerText = text.toLowerCase();
        const id = el.id || '';
        const name = el.getAttribute('name') || '';
        const lowerMeta = `${id} ${name} ${lowerText}`.toLowerCase();

        const isSubmit = el.type === 'submit' || lowerText.includes('submit') || lowerText.includes('continue') || lowerText.includes('next');
        const isLogin = lowerMeta.includes('login') || lowerMeta.includes('sign in') || lowerMeta.includes('log in') || lowerText.includes('proceed');
        const isLogout = lowerMeta.includes('logout') || lowerMeta.includes('sign out') || lowerMeta.includes('log out') || lowerMeta.includes('exit');
        const isOtpSubmit = lowerMeta.includes('verify') || lowerMeta.includes('confirm') || (isSubmit && lowerMeta.includes('otp'));

        return {
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type') || 'button',
            text: text.length > 50 ? text.substring(0, 47) + '...' : text,
            id: id,
            name: name,
            visible: isVisible(el),
            selector: getSelector(el),
            is_submit: isSubmit,
            is_login_candidate: isLogin,
            is_logout_candidate: isLogout,
            is_otp_submit_candidate: isOtpSubmit
        };
    });

    // Also look for logout links even if plain <a> tags
    Array.from(document.querySelectorAll('a')).forEach(a => {
        const txt = (a.innerText || '').trim().toLowerCase();
        const href = (a.getAttribute('href') || '').toLowerCase();
        if (txt.includes('logout') || txt.includes('sign out') || href.includes('logout') || href.includes('signout')) {
            buttons.push({
                tag: 'a',
                type: 'link',
                text: a.innerText.trim(),
                id: a.id || '',
                name: '',
                visible: isVisible(a),
                selector: getSelector(a) || `a[href*="${CSS.escape(a.getAttribute('href'))}"]`,
                is_submit: false,
                is_login_candidate: false,
                is_logout_candidate: true,
                is_otp_submit_candidate: false
            });
        }
    });

    // 4. Select best candidates for site configuration
    const userCandidate = inputs.find(i => i.is_username_candidate && i.visible) || inputs.find(i => i.is_username_candidate);
    const passCandidate = inputs.find(i => i.is_password_candidate && i.visible) || inputs.find(i => i.is_password_candidate);
    const otpCandidate = inputs.find(i => i.is_otp_candidate && i.visible) || inputs.find(i => i.is_otp_candidate);
    const loginBtnCandidate = buttons.find(b => b.is_login_candidate && b.visible) || buttons.find(b => b.is_submit && b.visible) || buttons.find(b => b.is_login_candidate);
    const otpBtnCandidate = buttons.find(b => b.is_otp_submit_candidate && b.visible) || buttons.find(b => b.is_submit && b.visible);
    const logoutCandidate = buttons.find(b => b.is_logout_candidate);

    const hasLogin = !!(passCandidate || (userCandidate && loginBtnCandidate));
    const hasOtp = !!otpCandidate;
    const hasLogout = !!logoutCandidate;

    return {
        title: document.title || '',
        url: window.location.href,
        forms_found: forms.length,
        forms: forms,
        inputs: inputs,
        buttons: buttons,
        auth_detection: {
            login_detected: hasLogin,
            otp_detected: hasOtp,
            logout_detected: hasLogout,
            suggested_username_selector: userCandidate ? userCandidate.selector : '',
            suggested_password_selector: passCandidate ? passCandidate.selector : '',
            suggested_login_btn_selector: loginBtnCandidate ? loginBtnCandidate.selector : '',
            suggested_otp_selector: otpCandidate ? otpCandidate.selector : '',
            suggested_otp_btn_selector: otpBtnCandidate ? otpBtnCandidate.selector : '',
            suggested_logout_selector: logoutCandidate ? logoutCandidate.selector : ''
        }
    };
}
"""


class WebsiteScanner:
    """Discovers DOM structure, inputs, forms, and workflows on target websites."""

    def __init__(self, headless: bool = True):
        self.headless = headless

    async def scan_url(self, url: str, timeout_sec: int = 20) -> Dict[str, Any]:
        """Scans the specified URL and returns structured DOM metadata.

        Args:
            url: The HTTP/HTTPS target website URL.
            timeout_sec: Maximum time to wait for page load and DOM analysis.

        Returns:
            Structured dictionary with detected forms, inputs, buttons, and suggested selectors.
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        logger.info(f"Starting DOM scan on target URL: {url}")
        scan_result: Dict[str, Any] = {
            "success": False,
            "url": url,
            "error": None,
            "title": "",
            "forms": [],
            "inputs": [],
            "buttons": [],
            "auth_detection": {
                "login_detected": False,
                "otp_detected": False,
                "logout_detected": False,
                "suggested_username_selector": "",
                "suggested_password_selector": "",
                "suggested_login_btn_selector": "",
                "suggested_otp_selector": "",
                "suggested_otp_btn_selector": "",
                "suggested_logout_selector": "",
            },
        }

        playwright = None
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None

        try:
            playwright = await async_playwright().start()

            # Launch isolated Chromium instance for scanning
            browser = await playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                ignore_https_errors=True,
            )

            page = await context.new_page()

            # Navigate to URL
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            except Exception as nav_err:
                logger.warning(f"Initial navigation completed with warning for {url}: {nav_err}")

            # Brief pause for client-side frameworks (React, Angular, Vue) to mount DOM
            await asyncio.sleep(1.5)

            # Execute the DOM scanner script
            raw_scan: Dict[str, Any] = await page.evaluate(DOM_SCANNER_JS)

            scan_result["success"] = True
            scan_result["title"] = raw_scan.get("title", "")
            scan_result["url"] = raw_scan.get("url", url)
            scan_result["forms"] = raw_scan.get("forms", [])
            scan_result["inputs"] = raw_scan.get("inputs", [])
            scan_result["buttons"] = raw_scan.get("buttons", [])
            scan_result["auth_detection"] = raw_scan.get("auth_detection", scan_result["auth_detection"])

            logger.info(
                f"Scan finished for {url}: found {len(scan_result['forms'])} forms, "
                f"{len(scan_result['inputs'])} inputs, {len(scan_result['buttons'])} buttons. "
                f"Login: {scan_result['auth_detection']['login_detected']}, "
                f"OTP: {scan_result['auth_detection']['otp_detected']}"
            )

        except Exception as ex:
            error_msg = f"Failed to scan {url}: {str(ex)}"
            logger.error(error_msg)
            scan_result["error"] = error_msg
            scan_result["success"] = False

        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass

        return scan_result
