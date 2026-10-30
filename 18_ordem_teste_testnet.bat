@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Vai abrir e fechar uma ordem pequena na TESTNET (dinheiro de teste).
%PY% testnet_check.py --ordem-teste
echo.
pause
