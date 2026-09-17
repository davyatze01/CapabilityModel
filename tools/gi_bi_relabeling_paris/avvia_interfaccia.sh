#!/bin/bash
# Double-click this file (or "Run as a Program" if your file manager just opens it
# as text) to open the Blue/Green Infrastructure Relabeling map in your browser.
cd "$(dirname "$0")"
(sleep 1 && (xdg-open "http://localhost:8767" >/dev/null 2>&1 || open "http://localhost:8767" >/dev/null 2>&1)) &
echo "Starting the server. This window must stay open while you use the map."
echo "Press Ctrl+C here (or close this window) when you're done to stop the server."
exec python3 server.py
