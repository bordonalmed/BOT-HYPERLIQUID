@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Confere chave, saldo, posicoes e ordens na CONTA REAL (nao opera).
%PY% testnet_check.py --real
echo.
pause
