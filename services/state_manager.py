from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional
from utils.logger import get_logger

logger = get_logger("state_manager")


class AppState(str, Enum):
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class ChromeState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    STARTING = "STARTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class TelegramState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class BrokerState(str, Enum):
    UNKNOWN = "UNKNOWN"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    AUTHENTICATED = "AUTHENTICATED"


class StateManager:
    """
    Centralized, thread-safe application state manager.
    Coordinates application lifecycle, browser connection status,
    Telegram integration health, multi-URL active workers, and real-time processing metrics.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.app_state: AppState = AppState.READY
        self.chrome_state: ChromeState = ChromeState.DISCONNECTED
        self.telegram_state: TelegramState = TelegramState.DISCONNECTED
        self.broker_state: BrokerState = BrokerState.UNKNOWN

        # Real performance metrics
        self.records_processed: int = 0
        self.successful_dispatches: int = 0
        self.failed_dispatches: int = 0
        self.latest_result: str = "System Initialized. Engine Ready."
        self.last_update: str = datetime.now().strftime("%H:%M:%S")
        self.last_error: Optional[str] = None

        # Multi-URL active tracking
        self.active_sites_count: int = 0
        self.running_sites_count: int = 0
        self.pending_otp_sites: Dict[str, str] = {}  # site_id -> site_name

        # Recent events ring buffer
        self._recent_events: Deque[Dict[str, Any]] = deque(maxlen=100)

        # Async listener callbacks (for WebSocket broadcasts)
        self._async_listeners: List[Callable[[Dict[str, Any]], Any]] = []

    def register_async_listener(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        """Registers a coroutine or callable listener to receive state updates."""
        with self._lock:
            if callback not in self._async_listeners:
                self._async_listeners.append(callback)

    def unregister_async_listener(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        with self._lock:
            if callback in self._async_listeners:
                self._async_listeners.remove(callback)

    def _notify_listeners(self) -> None:
        """Dispatches state dictionary to all registered async listeners."""
        data = self.to_dict()
        try:
            loop = asyncio.get_running_loop()
            for listener in list(self._async_listeners):
                if asyncio.iscoroutinefunction(listener):
                    asyncio.create_task(listener(data))
                else:
                    loop.call_soon(listener, data)
        except RuntimeError:
            pass

    def add_event(self, message: str, level: str = "INFO") -> None:
        """Records an event in the ring buffer and notifies listeners."""
        event = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level.upper(),
            "message": message,
        }
        with self._lock:
            self._recent_events.append(event)
            self.last_update = event["time"]
            if level.upper() == "ERROR":
                self.last_error = message
            elif level.upper() == "SUCCESS":
                self.successful_dispatches += 1
                self.records_processed += 1
                self.latest_result = message
        self._notify_listeners()

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent_events)[-limit:]

    def set_app_state(self, state: AppState, error: Optional[str] = None) -> None:
        with self._lock:
            self.app_state = state
            self.last_update = datetime.now().strftime("%H:%M:%S")
            if error:
                self.last_error = error
            logger.info(f"[STATE] Application -> {state.value}" + (f" (error: {error})" if error else ""))
        self._notify_listeners()

    def set_chrome_state(self, state: ChromeState, error: Optional[str] = None) -> None:
        with self._lock:
            self.chrome_state = state
            self.last_update = datetime.now().strftime("%H:%M:%S")
            if error:
                self.last_error = error
            logger.info(f"[STATE] Chrome -> {state.value}" + (f" (error: {error})" if error else ""))
        self._notify_listeners()

    def set_telegram_state(self, state: TelegramState, error: Optional[str] = None) -> None:
        with self._lock:
            self.telegram_state = state
            self.last_update = datetime.now().strftime("%H:%M:%S")
            if error:
                self.last_error = error
            logger.info(f"[STATE] Telegram -> {state.value}" + (f" (error: {error})" if error else ""))
        self._notify_listeners()

    def set_broker_state(self, state: BrokerState) -> None:
        with self._lock:
            self.broker_state = state
            self.last_update = datetime.now().strftime("%H:%M:%S")
            logger.info(f"[STATE] Broker Session -> {state.value}")
        self._notify_listeners()

    def set_pending_otp(self, site_id: str, site_name: str) -> None:
        with self._lock:
            self.pending_otp_sites[site_id] = site_name
        self.add_event(f"[{site_name}] Waiting for interactive OTP entry...", level="WARNING")

    def clear_pending_otp(self, site_id: str) -> None:
        with self._lock:
            self.pending_otp_sites.pop(site_id, None)
        self._notify_listeners()

    def update_site_counts(self, active: int, running: int) -> None:
        with self._lock:
            self.active_sites_count = active
            self.running_sites_count = running
        self._notify_listeners()

    def record_processed_event(self, summary: str, dispatch_success: bool = True) -> None:
        with self._lock:
            self.records_processed += 1
            if dispatch_success:
                self.successful_dispatches += 1
            else:
                self.failed_dispatches += 1
            self.latest_result = summary
            self.last_update = datetime.now().strftime("%H:%M:%S")
        self._notify_listeners()

    def record_error(self, error_msg: str) -> None:
        with self._lock:
            self.failed_dispatches += 1
            self.last_error = error_msg
            self.last_update = datetime.now().strftime("%H:%M:%S")
        self._notify_listeners()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "app_state": self.app_state.value,
                "chrome_state": self.chrome_state.value,
                "telegram_state": self.telegram_state.value,
                "broker_state": self.broker_state.value,
                "records_processed": self.records_processed,
                "successful_dispatches": self.successful_dispatches,
                "failed_dispatches": self.failed_dispatches,
                "latest_result": self.latest_result,
                "last_update": self.last_update,
                "last_error": self.last_error,
                "active_sites_count": self.active_sites_count,
                "running_sites_count": self.running_sites_count,
                "pending_otp_sites": dict(self.pending_otp_sites),
                "recent_events": list(self._recent_events)[-15:],
            }


# Global singleton instance
state_mgr = StateManager()
