@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Abrindo o dashboard em http://localhost:8050 ...
echo Deixe esta janela aberta. Fechar esta janela desliga so o dashboard (o bot continua).
%PY% dashboard.py
pause
