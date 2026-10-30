@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo OTIMIZADOR: baixa historico (15m, 1h, 4h) e testa centenas de variacoes.
echo Leva de 3 a 10 minutos. O bot ao vivo NAO e afetado.
%PY% -u otimizar.py
echo.
echo Resultado salvo em otimizacao_resultado.txt e melhor_config.json
pause
