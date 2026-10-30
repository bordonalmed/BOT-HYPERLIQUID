@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo OTIMIZADOR DE ROMPIMENTO: baixa 1h e 4h de 30 moedas e testa 384 variacoes.
echo Leva de 5 a 15 minutos. O bot ao vivo NAO e afetado.
%PY% -u otimizar_rompimento.py
echo.
echo Resultado salvo em otimizacao_rompimento_resultado.txt
pause
