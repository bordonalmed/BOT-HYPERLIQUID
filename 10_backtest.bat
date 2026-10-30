@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Rodando BACKTEST com ~50 dias de historico real (leva 1 a 3 minutos)...
echo O bot ao vivo NAO e afetado.
%PY% backtest.py > backtest_resultado.txt 2>&1
type backtest_resultado.txt
echo.
echo Resultado salvo em backtest_resultado.txt e backtest_trades.csv
pause
