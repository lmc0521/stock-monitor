@echo off
echo Installing PyInstaller (if needed)...
pip install pyinstaller
echo.
echo Building StockMonitor.exe ...
pyinstaller --noconfirm StockMonitor.spec
echo.
echo Done. The executable is in the "dist" folder: dist\StockMonitor.exe
echo Put your .env file next to StockMonitor.exe for AI Insights.
pause
