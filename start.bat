@echo off
title OAK-D Dashboard
cd /d "%~dp0"
echo Starting OAK-D Streaming Dashboard...
.venv\Scripts\python.exe run.py
pause
