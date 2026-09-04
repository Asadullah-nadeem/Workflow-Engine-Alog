from __future__ import annotations

import sys
import os
import threading
import asyncio
import time
from typing import Optional

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView

import uvicorn
from config import config
from utils.logger import get_logger

logger = get_logger("desktop_app")

# Global Uvicorn Server instance holder
uvicorn_server: Optional[uvicorn.Server] = None
server_thread: Optional[threading.Thread] = None


def start_fastapi_background_server():
    global uvicorn_server, server_thread
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    is_running = sock.connect_ex((config.app_host, config.app_port)) == 0
    sock.close()

    if is_running:
        logger.info(f"GUI Backend server already running on {config.app_host}:{config.app_port}.")
        return

    uv_config = uvicorn.Config(
        "gui.server:app",
        host=config.app_host,
        port=config.app_port,
        log_level="error",
    )
    uvicorn_server = uvicorn.Server(uv_config)

    server_thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    server_thread.start()
    time.sleep(1.2)


class DesktopAppWindow(QMainWindow):
    """
    Native PyQt6 Desktop App Window embedding the UI via QWebEngineView.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ALGO Broker Automation Command Center")
        self.resize(1340, 840)
        self.setMinimumSize(900, 600)

        # Central Widget Container
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Native WebEngine View loading from configurable host and port
        self.web_view = QWebEngineView()
        self.web_view.setUrl(QUrl(f"http://{config.app_host}:{config.app_port}/"))
        layout.addWidget(self.web_view)

    def closeEvent(self, event):
        global uvicorn_server
        if uvicorn_server:
            uvicorn_server.should_exit = True
        event.accept()


def main():
    logger.info("Starting background FastAPI GUI server...")
    start_fastapi_background_server()

    logger.info("Opening Native Desktop Window...")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = DesktopAppWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
