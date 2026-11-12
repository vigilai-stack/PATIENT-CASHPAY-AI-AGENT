@echo off
REM One-click local demo for Windows. Needs Docker Desktop running.
cd /d "%~dp0"
docker compose up --build -d || (echo Docker Desktop is not running. Start it and try again. & pause & exit /b 1)
echo Waiting for the app...
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{if((Invoke-WebRequest -UseBasicParsing http://localhost:8000/health).StatusCode -eq 200){exit 0}}catch{};Start-Sleep 2};exit 1"
start "" http://localhost:8000
echo Demo is running at http://localhost:8000   (stop it with: docker compose down)
