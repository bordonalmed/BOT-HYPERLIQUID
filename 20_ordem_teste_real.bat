@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo ATENCAO: abre e fecha uma ordem de ~US$ 12 em BTC na CONTA REAL (custo ~US$ 0,02 de taxa).
%PY% testnet_check.py --real --ordem-teste
echo.
pause
