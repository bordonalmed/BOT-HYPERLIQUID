@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo ATENCAO: abre e fecha uma ordem de ~US$ 12 em BTC na CONTA REAL (custo ~US$ 0,02 de taxa).
%PY% testnet_check.py --real --ordem-teste
echo.
pause
