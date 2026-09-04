from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import warnings
from typing import Optional

# Suppress Python 3.14 Windows asyncio transport teardown warnings on exit
warnings.filterwarnings("ignore", category=ResourceWarning)

from config import config, AppConfig
from utils.logger import setup_logger, get_logger
from storage.event_store import EventStore, BrokerEvent
from telegram.telegram_service import TelegramService
from brokers import get_broker
from browser.browser_manager import BrowserManager
from monitoring.event_monitor import EventMonitor
from services.state_manager import state_mgr, AppState

# Initialize core application logger
logger = setup_logger(log_level=config.log_level, log_file_path=config.log_file_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Secure Brokerage Browser Automation & Monitoring System",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Test Telegram Bot connection by sending a diagnostic ping and exit.",
    )
    parser.add_argument(
        "--check-login",
        action="store_true",
        help="Launch browser with current profile, check broker authentication status, and exit.",
    )
    parser.add_argument(
        "--broker",
        type=str,
        default=None,
        help="Override broker name (e.g. dhan, zerodha).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override polling check interval in seconds.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Override Chrome profile folder name (e.g. Default, 'Profile 1').",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default=None,
        help="Optional mode command (e.g. gui to launch Desktop GUI).",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the Desktop GUI Command Center dashboard.",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run FastAPI Web server directly without native desktop window.",
    )
    parser.add_argument(
        "--scan",
        type=str,
        default=None,
        help="Scan a target website URL for forms, inputs, buttons, and auth indicators.",
    )
    return parser.parse_args()


async def run_test_telegram(telegram_service: TelegramService) -> int:
    logger.info("Executing Telegram diagnostic test...")
    success, message = await telegram_service.test_connection()
    if success:
        logger.info(f"[SUCCESS] {message}")
        return 0
    else:
        logger.error(f"[FAILED] {message}")
        return 1


async def run_check_login(browser_manager: BrowserManager, broker) -> int:
    logger.info(f"Checking login status for {broker.name} at {config.broker_url}...")
    try:
        page = await browser_manager.start()
        logger.info(f"Navigating to {config.broker_url}...")
        await page.goto(config.broker_url, wait_until="domcontentloaded", timeout=config.page_timeout_ms)
        await asyncio.sleep(3.0)

        is_logged_in = await broker.is_logged_in(page)
        if is_logged_in:
            logger.info(f"[AUTHENTICATED] Successfully verified active {broker.name} session!")
            return 0
        else:
            logger.warning(
                f"[NOT LOGGED IN] {broker.name} is not logged in. "
                f"Please log in manually via Chrome to save session cookies to your profile."
            )
            return 2
    except Exception as exc:
        logger.error(f"Error during login check: {exc}")
        return 1
    finally:
        await browser_manager.close()


async def main() -> int:
    args = parse_args()

    # Apply command-line overrides to config
    if args.broker:
        config.broker_name = args.broker.strip().lower()
    if args.headless:
        config.headless = True
    if args.interval:
        config.check_interval = max(2, args.interval)
    if args.profile:
        config.chrome_profile_dir = args.profile

    print("=" * 70)
    print("  BROKER AUTOMATION & MONITORING SYSTEM")
    print("=" * 70)
    logger.info("Starting system with configuration:")
    for line in config.masked_repr().splitlines():
        logger.info(f"  {line}")

    # 1. Initialize Subsystems
    event_store = EventStore(db_path=config.database_path)
    telegram_service = TelegramService(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        enabled=config.telegram_enabled,
    )
    broker = get_broker(name=config.broker_name, base_url=config.broker_url)
    browser_manager = BrowserManager(config=config)

    # 2. Handle Diagnostic Flags & Launchers
    if args.scan:
        logger.info(f"Initiating DOM scan on: {args.scan}")
        from scanner.website_scanner import WebsiteScanner
        scanner = WebsiteScanner(headless=True)
        scan_res = await scanner.scan_url(args.scan)
        print("\n" + "=" * 60)
        print(f"  SCAN REPORT: {scan_res.get('title')} ({args.scan})")
        print("=" * 60)
        print(f"Success: {scan_res.get('success')}")
        print(f"Forms Discovered: {len(scan_res.get('forms', []))}")
        print(f"Inputs Discovered: {len(scan_res.get('inputs', []))}")
        print(f"Buttons Discovered: {len(scan_res.get('buttons', []))}")
        auth = scan_res.get("auth_detection", {})
        print(f"Login Detected: {auth.get('login_detected')}")
        print(f"OTP Detected: {auth.get('otp_detected')}")
        print("\nSuggested Selectors:")
        for k, v in auth.items():
            if k.startswith("suggested_") and v:
                print(f"  • {k}: {v}")
        print("=" * 60 + "\n")
        return 0 if scan_res.get("success") else 1

    if args.server:
        logger.info(f"Starting FastAPI Web Server directly on http://{config.app_host}:{config.app_port}...")
        import uvicorn
        uv_config = uvicorn.Config(
            "gui.server:app",
            host=config.app_host,
            port=config.app_port,
            log_level="info",
        )
        server = uvicorn.Server(uv_config)
        await server.serve()
        return 0

    if args.gui or (args.mode and args.mode.lower() == "gui"):
        logger.info("Launching PyQt6 Desktop GUI Command Center Window...")
        from gui.desktop_app import main as launch_pyqt_gui
        launch_pyqt_gui()
        return 0

    if args.test_telegram:
        return await run_test_telegram(telegram_service)

    if args.check_login:
        return await run_check_login(browser_manager, broker)

    # 3. Create Monitoring Engine
    monitor = EventMonitor(
        config=config,
        browser_manager=browser_manager,
        broker=broker,
        event_store=event_store,
        telegram_service=telegram_service,
    )

    # 4. Graceful Shutdown Setup
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("\nReceived termination signal. Initiating graceful shutdown...")
        asyncio.create_task(shutdown(monitor, browser_manager))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    # 5. Start Continuous Monitoring Loop
    try:
        await monitor.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Keyboard interrupt received.")
    except Exception as exc:
        logger.critical(f"Fatal error in main execution loop: {exc}", exc_info=True)
        return 1
    finally:
        await shutdown(monitor, browser_manager)

    return 0


async def shutdown(monitor: EventMonitor, browser_manager: BrowserManager) -> None:
    logger.info("Shutting down monitoring engine...")
    await monitor.stop()
    logger.info("Closing browser context...")
    await browser_manager.close()

    # Cancel remaining background tasks
    current_task = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current_task]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    await asyncio.sleep(0.2)
    logger.info("System shutdown complete. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nProcess terminated by user.")
        sys.exit(0)
