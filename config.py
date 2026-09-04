from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file explicitly if present
load_dotenv(BASE_DIR / ".env")


class AppConfig(BaseSettings):
    """
    Application Configuration loaded from environment variables and .env file.
    Provides validation, sensible defaults, and security masking for credentials.
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --------------------------------------------------------------------------
    # Application & Server Settings
    # --------------------------------------------------------------------------
    app_env: str = Field(
        default="production",
        description="Application environment: production or development",
        alias="APP_ENV",
    )
    app_debug: bool = Field(
        default=False,
        description="Debug mode for extended logging and diagnostics",
        alias="APP_DEBUG",
    )
    app_host: str = Field(
        default="127.0.0.1",
        description="GUI backend host address",
        alias="APP_HOST",
    )
    app_port: int = Field(
        default=8000,
        description="GUI backend port number",
        alias="APP_PORT",
    )

    # --------------------------------------------------------------------------
    # Broker Settings
    # --------------------------------------------------------------------------
    broker_name: str = Field(
        default="dhan",
        description="Name of the broker module to use (e.g., dhan, zerodha)",
        alias="BROKER_NAME",
    )
    broker_url: str = Field(
        default="https://web.dhan.co",
        description="Target brokerage dashboard URL",
        alias="BROKER_URL",
    )

    # --------------------------------------------------------------------------
    # Browser & Profile Settings
    # --------------------------------------------------------------------------
    browser_mode: Literal["persistent_profile", "cdp"] = Field(
        default="persistent_profile",
        description="Mode of browser automation: 'persistent_profile' or 'cdp'",
        alias="BROWSER_MODE",
    )
    chrome_user_data_dir: str = Field(
        default_factory=lambda: str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "Google"
            / "Chrome"
            / "User Data"
        ),
        description="Path to Chrome User Data directory",
        alias="CHROME_USER_DATA_DIR",
    )
    chrome_profile_dir: str = Field(
        default="Default",
        description="Name of the Chrome profile subdirectory (Default, Profile 1, etc.)",
        alias="CHROME_PROFILE_DIR",
    )
    chrome_executable_path: Optional[str] = Field(
        default=None,
        description="Optional custom path to Google Chrome binary",
        alias="CHROME_EXECUTABLE_PATH",
    )
    cdp_url: str = Field(
        default="http://localhost:9222",
        description="Chrome DevTools Protocol endpoint URL for CDP mode",
        alias="CDP_URL",
    )
    headless: bool = Field(
        default=False,
        description="Whether to run the browser in headless mode",
        alias="HEADLESS",
    )

    # --------------------------------------------------------------------------
    # Telegram Notification Settings
    # --------------------------------------------------------------------------
    telegram_enabled: bool = Field(
        default=True,
        description="Whether Telegram notifications are enabled",
        alias="TELEGRAM_ENABLED",
    )
    telegram_bot_token: Optional[str] = Field(
        default=None,
        description="Telegram Bot API Token",
        alias="TELEGRAM_BOT_TOKEN",
    )
    telegram_chat_id: Optional[str] = Field(
        default=None,
        description="Telegram Chat/Channel ID",
        alias="TELEGRAM_CHAT_ID",
    )

    # --------------------------------------------------------------------------
    # Monitoring & Storage Settings
    # --------------------------------------------------------------------------
    check_interval: int = Field(
        default=5,
        ge=2,
        description="Interval in seconds between monitoring cycles (minimum 2s)",
        alias="CHECK_INTERVAL",
    )
    page_timeout_ms: int = Field(
        default=30000,
        ge=5000,
        description="Page navigation and element wait timeout in milliseconds",
        alias="PAGE_TIMEOUT_MS",
    )
    # --------------------------------------------------------------------------
    # Stock Market Screen Analysis Settings
    # --------------------------------------------------------------------------
    stock_scanner_enabled: bool = Field(
        default=True,
        description="Whether continuous stock market screen analysis is enabled",
        alias="STOCK_SCANNER_ENABLED",
    )
    stock_scan_interval: int = Field(
        default=5,
        ge=1,
        description="Scan interval in seconds for stock market screen updates",
        alias="STOCK_SCAN_INTERVAL",
    )
    top_movers_limit: int = Field(
        default=10,
        ge=1,
        description="Maximum number of top gainers to track and display",
        alias="TOP_MOVERS_LIMIT",
    )
    top_decliners_limit: int = Field(
        default=10,
        ge=1,
        description="Maximum number of top decliners to track and display",
        alias="TOP_DECLINERS_LIMIT",
    )
    min_change_percent: float = Field(
        default=0.5,
        ge=0.0,
        description="Minimum percentage change to highlight or alert",
        alias="MIN_CHANGE_PERCENT",
    )
    telegram_market_alerts: bool = Field(
        default=True,
        description="Whether to send Telegram alerts for significant market movements",
        alias="TELEGRAM_MARKET_ALERTS",
    )
    telegram_min_change_percent: float = Field(
        default=3.0,
        ge=0.0,
        description="Minimum percentage threshold to dispatch a Telegram market alert",
        alias="TELEGRAM_MIN_CHANGE_PERCENT",
    )
    telegram_notification_cooldown: int = Field(
        default=60,
        ge=5,
        description="Cooldown in seconds between market alert dispatches to avoid spam",
        alias="TELEGRAM_NOTIFICATION_COOLDOWN",
    )
    market_region_selector: str = Field(
        default="",
        description="Custom CSS selector for main central market region (optional override)",
        alias="MARKET_REGION_SELECTOR",
    )
    stock_row_selector: str = Field(
        default="",
        description="Custom CSS selector for stock rows in a table/watchlist (optional override)",
        alias="STOCK_ROW_SELECTOR",
    )
    symbol_selector: str = Field(
        default="",
        description="Custom CSS selector for symbol in row/header (optional override)",
        alias="SYMBOL_SELECTOR",
    )
    price_selector: str = Field(
        default="",
        description="Custom CSS selector for price in row/header (optional override)",
        alias="PRICE_SELECTOR",
    )
    change_selector: str = Field(
        default="",
        description="Custom CSS selector for price change in row/header (optional override)",
        alias="CHANGE_SELECTOR",
    )
    change_percent_selector: str = Field(
        default="",
        description="Custom CSS selector for percentage change in row/header (optional override)",
        alias="CHANGE_PERCENT_SELECTOR",
    )

    event_retention_days: int = Field(
        default=30,
        ge=1,
        description="Number of days to keep processed event records in SQLite",
        alias="EVENT_RETENTION_DAYS",
    )
    data_dir: str = Field(
        default_factory=lambda: str(BASE_DIR / "data"),
        description="Directory for local database and persistent storage",
        alias="DATA_DIR",
    )
    database_path: str = Field(
        default_factory=lambda: str(BASE_DIR / "data" / "events.db"),
        description="Path to SQLite event store database file",
        alias="DATABASE_PATH",
    )
    log_dir: str = Field(
        default_factory=lambda: str(BASE_DIR / "logs"),
        description="Directory for application log files",
        alias="LOG_DIR",
    )
    log_level: str = Field(
        default="INFO",
        description="Application logging level (DEBUG, INFO, WARNING, ERROR)",
        alias="LOG_LEVEL",
    )
    log_file_path: str = Field(
        default_factory=lambda: str(BASE_DIR / "logs" / "app.log"),
        description="Path to log file",
        alias="LOG_FILE_PATH",
    )

    @field_validator("broker_name", mode="before")
    @classmethod
    def normalize_broker_name(cls, v: str) -> str:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        return v.strip().upper() if isinstance(v, str) else "INFO"

    def masked_repr(self) -> str:
        """
        Returns a string representation of configuration with secrets safely redacted.
        """
        masked_token = (
            f"***{self.telegram_bot_token[-4:]}"
            if self.telegram_bot_token and len(self.telegram_bot_token) > 4
            else "(not set)"
        )
        masked_chat = (
            f"***{self.telegram_chat_id[-3:]}"
            if self.telegram_chat_id and len(self.telegram_chat_id) > 3
            else "(not set)"
        )
        return (
            f"AppConfig(\n"
            f"  Environment: {self.app_env} (debug={self.app_debug})\n"
            f"  Server: http://{self.app_host}:{self.app_port}\n"
            f"  Broker: {self.broker_name} ({self.broker_url})\n"
            f"  Browser Mode: {self.browser_mode}\n"
            f"  Profile Dir: {self.chrome_profile_dir}\n"
            f"  User Data Dir: {self.chrome_user_data_dir}\n"
            f"  Headless: {self.headless}\n"
            f"  Telegram: enabled={self.telegram_enabled}, token={masked_token}, chat_id={masked_chat}\n"
            f"  Check Interval: {self.check_interval}s\n"
            f"  Database: {self.database_path}\n"
            f"  Log File: {self.log_file_path} (level={self.log_level})\n"
            f")"
        )


# Global singleton instance
config = AppConfig()


def get_config() -> AppConfig:
    """Returns the application config singleton."""
    return config
