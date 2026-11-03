@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo OTIMIZADOR DE FREQUENCIA: testa 10/20/30 moedas, 2/3/4 posicoes e 3/5 ops por dia
echo com a estrategia aprovada. Leva de 2 a 6 minutos. O bot ao vivo NAO e afetado.
%PY% -u otimizar_frequencia.py
echo.
echo Resultado salvo em otimizacao_frequencia_resultado.txt
pause
