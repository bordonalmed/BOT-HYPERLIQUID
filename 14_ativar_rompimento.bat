@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Ativa a estrategia de rompimento SO se o otimizador aprovou.
%PY% ativar_config.py
echo.
echo Depois: 6_parar_bot.bat e 3_iniciar_bot.bat
pause
