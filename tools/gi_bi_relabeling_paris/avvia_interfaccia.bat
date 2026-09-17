@echo off
cd /d "%~dp0"
start "GI-BI Server" cmd /c "python server.py"
timeout /t 2 /nobreak >nul
start "" "http://localhost:8767"
echo The map is now open in your browser.
echo A second window titled "GI-BI Server" opened - that is the server. Keep it open
echo while you use the map, and close it (or press Ctrl+C in it) when you're done.
pause
