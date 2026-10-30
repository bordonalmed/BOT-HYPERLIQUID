@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
if exist KILL del KILL
echo Iniciando o bot em uma janela minimizada (BOT-HYPERLIQUID)...
echo NAO rode este arquivo duas vezes ao mesmo tempo.
start "BOT-HYPERLIQUID" /min cmd /c "%PY% -u bot.py >> bot.log 2>&1"
echo Bot iniciado. Para acompanhar, abra 4_ver_log.bat
timeout /t 5
