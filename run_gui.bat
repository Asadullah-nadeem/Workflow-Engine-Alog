@echo off
TITLE ALGO Brokerage Automation & Monitoring System
cls
echo =======================================================================
echo          ALGO BROKERAGE AUTOMATION & MONITORING SYSTEM
echo =======================================================================
echo.
echo [1/2] Checking Dependencies...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo [2/2] Opening PyQt6 Desktop GUI Command Center Window...
echo.

python main.py --gui

pause

