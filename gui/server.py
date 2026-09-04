from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Set, Dict, Any, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from config import config, BASE_DIR
from storage.event_store import EventStore
from storage.automation_store import auto_store
from telegram.telegram_service import TelegramService, telegram_service
from telegram.telegram_bot import TelegramBotListener
from brokers import get_broker
from browser.browser_manager import BrowserManager
from browser.chrome_manager import chrome_manager
from monitoring.event_monitor import EventMonitor
from engine.workflow_engine import workflow_engine
from engine.auth_manager import auth_manager
from scanner.market_analyzer import market_analyzer
from services.state_manager import state_mgr, AppState, ChromeState, TelegramState, BrokerState
from utils.logger import get_logger

logger = get_logger("gui_server")

app = FastAPI(title="ALGO Universal Website Automation & Workflow Engine")

TEMPLATES_DIR = BASE_DIR / "gui" / "templates"


# ------------------------------------------------------------------------------
# WebSocket Real-Time Broadcasting
# ------------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        # Send current state and pending OTP requests immediately upon connect
        try:
            await websocket.send_json({
                "type": "state",
                "data": state_mgr.to_dict(),
            })
        except Exception:
            pass

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: dict):
        dead_connections = set()
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.add(connection)
        for conn in dead_connections:
            self.active_connections.discard(conn)


manager = ConnectionManager()


# ------------------------------------------------------------------------------
# Engine State Container
# ------------------------------------------------------------------------------
class EngineContainer:
    def __init__(self):
        self.event_store: EventStore = EventStore(db_path=config.database_path)
        self.telegram_service: TelegramService = telegram_service
        self.browser_manager: Optional[BrowserManager] = None
        self.monitor: Optional[EventMonitor] = None
        self.bot_listener: Optional[TelegramBotListener] = None
        self.task: Optional[asyncio.Task] = None
        self._action_lock = asyncio.Lock()


engine = EngineContainer()


# ------------------------------------------------------------------------------
# Pydantic Request Models
# ------------------------------------------------------------------------------
class ConfigUpdateRequest(BaseModel):
    broker_name: str
    broker_url: str
    check_interval: int
    chrome_profile_dir: str
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


class AddSiteRequest(BaseModel):
    name: str
    url: str


class SiteConfigRequest(BaseModel):
    username_selector: Optional[str] = ""
    password_selector: Optional[str] = ""
    submit_selector: Optional[str] = ""
    otp_required: Optional[bool] = False
    otp_selector: Optional[str] = ""
    otp_submit_selector: Optional[str] = ""
    expected_auth_url: Optional[str] = ""
    expected_auth_selector: Optional[str] = ""
    logout_selector: Optional[str] = ""
    username_val: Optional[str] = ""
    password_val: Optional[str] = ""
    repeat_interval: Optional[int] = 10
    max_retries: Optional[int] = 3
    timeout_sec: Optional[int] = 30


class SubmitOtpRequest(BaseModel):
    otp: str


class BatchActionRequest(BaseModel):
    action: str  # 'start', 'stop', 'scan'
    site_ids: Optional[List[str]] = None


# ------------------------------------------------------------------------------
# Application Lifespan Events
# ------------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    logger.info("Initializing ALGO Universal Automation Engine Backend...")
    engine.event_store = EventStore(db_path=config.database_path)
    engine.browser_manager = BrowserManager(config=config)

    # Register state change broadcast listener
    async def on_state_change(state_dict: Dict[str, Any]):
        await manager.broadcast({
            "type": "state",
            "data": state_dict,
        })

    state_mgr.register_async_listener(on_state_change)

    # Register custom log handler to stream logs via WebSocket
    class WebSocketLogHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            level = record.levelname
            asyncio.create_task(manager.broadcast({
                "type": "log",
                "data": {"level": level, "message": msg},
            }))

    ws_handler = WebSocketLogHandler()
    ws_handler.setFormatter(logging.Formatter('%(message)s'))
    logging.getLogger().addHandler(ws_handler)

    # Initialize Telegram interactive bot listener
    async def handle_bot_command(cmd: str, ctx: Dict[str, Any]) -> str:
        if cmd == "/status":
            st = state_mgr.to_dict()
            sites = auto_store.list_sites()
            return (
                f"🤖 *ALGO Automation Platform Status*\n\n"
                f"• Engine: *{st['app_state']}*\n"
                f"• Chrome: *{st['chrome_state']}*\n"
                f"• Sites Configured: *{len(sites)}*\n"
                f"• Processed: *{st['records_processed']}*\n"
                f"• Successes: *{st['successful_dispatches']}*\n"
                f"• Latest: `{st['latest_result']}`"
            )
        elif cmd == "/sites":
            sites = auto_store.list_sites()
            if not sites:
                return "ℹ️ No websites currently configured."
            lines = ["🌐 *Configured Automation Sites:*"]
            for s in sites:
                lines.append(f"• *{s['name']}* - {s['automation_status']} (`{s['url']}`)")
            return "\n".join(lines)
        elif cmd == "/events":
            events = state_mgr.get_recent_events(limit=5)
            if not events:
                return "ℹ️ No recent events recorded."
            lines = ["📊 *Recent Automation Events:*"]
            for ev in events:
                lines.append(f"• `[{ev['time']}]` [{ev['level']}] {ev['message']}")
            return "\n".join(lines)
        return f"❓ Unknown command `{cmd}`. Use /status, /sites, /events, or /help."

    engine.bot_listener = TelegramBotListener(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        enabled=config.telegram_enabled,
        command_handler=handle_bot_command,
    )
    engine.bot_listener.start()

    state_mgr.set_app_state(AppState.READY)
    logger.info("Universal Automation Backend Server started. Ready for operations.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Universal Automation Backend Server...")
    await workflow_engine.stop_all()
    await chrome_manager.cleanup()
    if engine.bot_listener:
        await engine.bot_listener.stop()


# ------------------------------------------------------------------------------
# Web Dashboard Entry
# ------------------------------------------------------------------------------
@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(TEMPLATES_DIR / "index.html")


# ------------------------------------------------------------------------------
# System Status & Diagnostic API
# ------------------------------------------------------------------------------
@app.get("/api/status")
async def get_status():
    st = state_mgr.to_dict()
    sites = auto_store.list_sites()
    running_sites = [s for s in sites if s.get("automation_status") == "RUNNING"]
    st["active_sites_count"] = len(sites)
    st["running_sites_count"] = len(running_sites)
    return {
        **st,
        "is_running": st["app_state"] == AppState.RUNNING.value or len(running_sites) > 0,
        "broker_name": config.broker_name,
        "broker_url": config.broker_url,
        "check_interval": config.check_interval,
        "chrome_profile_dir": config.chrome_profile_dir,
        "telegram_enabled": config.telegram_enabled,
        "telegram_bot_token": config.telegram_bot_token or "",
        "telegram_chat_id": config.telegram_chat_id or "",
    }


# ------------------------------------------------------------------------------
# Multi-URL Management APIs
# ------------------------------------------------------------------------------
@app.get("/api/sites")
async def list_sites():
    """Returns list of all authorized websites with their configurations and execution states."""
    sites = auto_store.list_sites()
    for s in sites:
        # Check if site is waiting for OTP
        s["is_waiting_for_otp"] = auth_manager.is_waiting_for_otp(s["id"])
    return {"sites": sites}


@app.post("/api/sites")
async def add_site(req: AddSiteRequest):
    """Adds a new authorized website for automation."""
    if not req.name.strip() or not req.url.strip():
        raise HTTPException(status_code=400, detail="Name and URL are required.")

    site_id = auto_store.add_site(name=req.name.strip(), url=req.url.strip())
    state_mgr.add_event(f"New website added: {req.name} ({req.url})", level="INFO")
    return {"status": "ok", "site_id": site_id, "message": f"Website '{req.name}' added successfully."}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: str):
    """Removes a site and stops its active workflows."""
    await workflow_engine.stop_site(site_id)
    success = auto_store.delete_site(site_id)
    if not success:
        raise HTTPException(status_code=404, detail="Site not found.")
    state_mgr.add_event(f"Site {site_id} removed.", level="INFO")
    return {"status": "ok", "message": "Site removed successfully."}


# ------------------------------------------------------------------------------
# Scanning & DOM Discovery APIs
# ------------------------------------------------------------------------------
@app.post("/api/sites/{site_id}/scan")
async def scan_site(site_id: str):
    """Triggers DOM scanning and discovers forms, inputs, buttons, and auth indicators."""
    scan_result = await workflow_engine.scan_site(site_id)
    return scan_result


@app.get("/api/sites/{site_id}/scan-result")
async def get_site_scan_result(site_id: str):
    """Retrieves the latest structured scan result for a site."""
    res = auto_store.get_scan_result(site_id)
    if not res:
        return {"scanned": False, "forms": [], "inputs": [], "buttons": [], "auth_detection": {}}
    return {"scanned": True, **res}


# ------------------------------------------------------------------------------
# Site Configuration APIs
# ------------------------------------------------------------------------------
@app.get("/api/sites/{site_id}/config")
async def get_site_config(site_id: str):
    """Retrieves configuration parameters for a specific site."""
    cfg = auto_store.get_site_config(site_id)
    # Never expose plain password in config responses to ensure security
    sanitized = dict(cfg)
    sanitized["has_password"] = bool(sanitized.get("password_val"))
    sanitized["password_val"] = "********" if sanitized.get("password_val") else ""
    return sanitized


@app.post("/api/sites/{site_id}/config")
async def save_site_config(site_id: str, req: SiteConfigRequest):
    """Saves selectors, credentials, and execution parameters for a site."""
    cfg_data = req.dict()
    # If password is masked placeholder, keep existing password
    if cfg_data.get("password_val") == "********":
        existing = auto_store.get_site_config(site_id)
        cfg_data["password_val"] = existing.get("password_val", "")

    auto_store.save_site_config(site_id, cfg_data)
    state_mgr.add_event(f"Updated configuration for site {site_id}.", level="INFO")
    return {"status": "ok", "message": "Configuration saved successfully."}


# ------------------------------------------------------------------------------
# Automation Execution Controls (Start, Stop, Pause, Resume, OTP)
# ------------------------------------------------------------------------------
@app.post("/api/sites/{site_id}/start")
async def start_site(site_id: str):
    """Starts workflow automation for a specific site."""
    success = await workflow_engine.start_site(site_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to start site workflow.")
    return {"status": "ok", "message": "Automation started."}


@app.post("/api/sites/{site_id}/stop")
async def stop_site(site_id: str):
    """Stops workflow automation for a specific site."""
    success = await workflow_engine.stop_site(site_id)
    return {"status": "ok", "message": "Automation stopped."}


@app.post("/api/sites/{site_id}/pause")
async def pause_site(site_id: str):
    """Pauses workflow automation for a specific site."""
    success = await workflow_engine.pause_site(site_id)
    if not success:
        raise HTTPException(status_code=400, detail="Site is not currently running or cannot be paused.")
    return {"status": "ok", "message": "Automation paused."}


@app.post("/api/sites/{site_id}/resume")
async def resume_site(site_id: str):
    """Resumes a paused workflow automation for a specific site."""
    success = await workflow_engine.resume_site(site_id)
    if not success:
        raise HTTPException(status_code=400, detail="Site is not paused or cannot be resumed.")
    return {"status": "ok", "message": "Automation resumed."}


@app.post("/api/sites/{site_id}/submit-otp")
async def submit_otp(site_id: str, req: SubmitOtpRequest):
    """Accepts user-entered OTP from GUI modal and unblocks the automation workflow."""
    if not req.otp.strip():
        raise HTTPException(status_code=400, detail="OTP cannot be blank.")

    accepted = auth_manager.submit_user_otp(site_id, req.otp.strip())
    if not accepted:
        raise HTTPException(status_code=404, detail="No active OTP requirement found for this site.")

    state_mgr.add_event(f"OTP received from GUI for site {site_id}. Resuming...", level="INFO")
    return {"status": "ok", "message": "OTP submitted successfully. Resuming workflow..."}


@app.get("/api/otp/pending")
async def get_pending_otp():
    """Returns any site currently paused waiting for interactive OTP input."""
    pending = auth_manager.get_pending_otp_sites()
    results = []
    for sid in pending.keys():
        s = auto_store.get_site(sid)
        if s:
            results.append({"site_id": sid, "name": s["name"], "url": s["url"]})
    return {"pending_otps": results}


@app.get("/api/sites/{site_id}/history")
async def get_site_history(site_id: str, limit: int = 50):
    """Retrieves execution history log for a specific site."""
    history = auto_store.get_execution_history(site_id, limit=limit)
    return {"history": history}


@app.post("/api/browser/open")
async def open_browser():
    """Explicitly launches or foregrounds the Chrome browser."""
    try:
        page = await chrome_manager.start(headless=False)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        chrome_manager._bring_to_windows_foreground()
        state_mgr.add_event("Chrome browser launched and visible.", level="SUCCESS")
        return {"status": "ok", "message": "Chrome browser opened successfully."}
    except Exception as exc:
        logger.error(f"Failed to open Chrome browser: {exc}")
        state_mgr.add_event(f"Failed to open Chrome: {exc}", level="ERROR")
        raise HTTPException(status_code=500, detail=f"Failed to open Chrome: {exc}")


@app.post("/api/browser/close")
async def close_browser():
    """Explicitly closes Chrome browser."""
    await chrome_manager.cleanup()
    state_mgr.add_event("Chrome browser closed.", level="INFO")
    return {"status": "ok", "message": "Chrome browser closed."}


# ------------------------------------------------------------------------------
# Stock Market Screen Analysis APIs
# ------------------------------------------------------------------------------
@app.get("/api/market/latest")
async def get_latest_market_data():
    """Returns the latest stock market analysis result, top movers, and scan status."""
    last_res = market_analyzer.last_result
    if last_res:
        return last_res.dict()

    # Fallback to database history if available
    store = EventStore(db_path=config.database_path)
    latest_rows = store.get_latest_market_snapshots(limit=30)
    return {
        "scanner_state": "READY" if latest_rows else "IDLE",
        "screen_detected": bool(latest_rows),
        "target_page_title": "",
        "target_page_url": "",
        "central_region_found": bool(latest_rows),
        "stocks_detected": len(latest_rows),
        "stocks": latest_rows,
        "top_gainers": [r for r in latest_rows if r.get("direction") == "UP"][:10],
        "top_decliners": [r for r in latest_rows if r.get("direction") == "DOWN"][:10],
        "flat_count": sum(1 for r in latest_rows if r.get("direction") == "FLAT"),
        "uncertain_count": sum(1 for r in latest_rows if r.get("direction") == "DATA_UNCERTAIN"),
    }


@app.post("/api/market/scan-now")
async def scan_market_now():
    """Triggers an immediate DOM and central market region analysis on active Chromium tab."""
    state_mgr.add_event("Immediate Market Screen Scan initiated...", level="INFO")
    page = await chrome_manager.get_active_page()
    if not page or page.is_closed():
        raise HTTPException(status_code=400, detail="No active browser page open. Please start a site or open Chrome.")

    analysis = await market_analyzer.analyze_page(page)

    if analysis.stocks:
        store = EventStore(db_path=config.database_path)
        store.save_market_snapshots_batch(analysis.stocks)
        state_mgr.add_event(
            f"Market scan completed: {analysis.stocks_detected} stocks detected. "
            f"Gainers: {len(analysis.top_gainers)}, Decliners: {len(analysis.top_decliners)}",
            level="SUCCESS"
        )
    else:
        state_mgr.add_event(
            f"Market scan notice: {analysis.reason or 'No stocks detected in market region.'}",
            level="WARNING"
        )

    # Broadcast to WebSocket clients
    await manager.broadcast({
        "type": "market_update",
        "data": analysis.dict()
    })

    return analysis.dict()


@app.get("/api/market/history")
async def get_market_history(symbol: Optional[str] = None, limit: int = 50):
    """Returns historical market snapshots."""
    store = EventStore(db_path=config.database_path)
    if symbol:
        return {"snapshots": store.get_symbol_history(symbol, limit=limit)}
    return {"snapshots": store.get_latest_market_snapshots(limit=limit)}



# ------------------------------------------------------------------------------
# Batch Operations APIs
# ------------------------------------------------------------------------------
@app.post("/api/batch")
async def batch_action(req: BatchActionRequest):
    """Performs batch actions across multiple sites."""
    action = req.action.lower()
    target_ids = req.site_ids

    if action == "start":
        if target_ids:
            started = []
            for sid in target_ids:
                if await workflow_engine.start_site(sid):
                    started.append(sid)
            return {"status": "ok", "action": "start", "affected": started}
        else:
            started = await workflow_engine.start_all()
            return {"status": "ok", "action": "start_all", "affected": started}

    elif action == "stop":
        if target_ids:
            stopped = []
            for sid in target_ids:
                if await workflow_engine.stop_site(sid):
                    stopped.append(sid)
            return {"status": "ok", "action": "stop", "affected": stopped}
        else:
            stopped = await workflow_engine.stop_all()
            return {"status": "ok", "action": "stop_all", "affected": stopped}

    elif action == "scan":
        sites = auto_store.list_sites()
        scanned = []
        ids_to_scan = target_ids if target_ids else [s["id"] for s in sites]
        for sid in ids_to_scan:
            res = await workflow_engine.scan_site(sid)
            scanned.append({"site_id": sid, "success": res.get("success", False)})
        return {"status": "ok", "action": "scan", "results": scanned}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown batch action '{req.action}'.")


# ------------------------------------------------------------------------------
# Telegram & Global Settings APIs
# ------------------------------------------------------------------------------
@app.post("/api/test-telegram")
async def test_telegram():
    service = TelegramService(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        enabled=True,
    )
    success, msg = await service.test_connection()
    return {"success": success, "message": msg}


@app.get("/api/events")
async def get_events(limit: int = 50):
    events = state_mgr.get_recent_events(limit=limit)
    return {"events": events}


@app.post("/api/config")
async def update_global_config(req: ConfigUpdateRequest):
    env_file = BASE_DIR / ".env"
    lines = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    new_settings = {
        "BROKER_NAME": req.broker_name,
        "BROKER_URL": req.broker_url,
        "CHECK_INTERVAL": str(req.check_interval),
        "CHROME_PROFILE_DIR": req.chrome_profile_dir,
    }
    if req.telegram_bot_token:
        new_settings["TELEGRAM_BOT_TOKEN"] = req.telegram_bot_token
    if req.telegram_chat_id:
        new_settings["TELEGRAM_CHAT_ID"] = req.telegram_chat_id

    updated_keys = set()
    new_lines = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=")[0].strip()
            if key in new_settings:
                new_lines.append(f"{key}={new_settings[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    for k, v in new_settings.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    env_file.write_text("\n".join(new_lines), encoding="utf-8")

    # Reload in-memory config
    config.broker_name = req.broker_name
    config.broker_url = req.broker_url
    config.check_interval = req.check_interval
    config.chrome_profile_dir = req.chrome_profile_dir
    if req.telegram_bot_token:
        config.telegram_bot_token = req.telegram_bot_token
    if req.telegram_chat_id:
        config.telegram_chat_id = req.telegram_chat_id

    # Re-initialize Telegram service with new credentials
    engine.telegram_service = TelegramService(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        enabled=config.telegram_enabled,
    )

    logger.info("Configuration updated and saved to .env.")
    return {"status": "ok", "message": "Configuration updated and saved to .env successfully!"}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
