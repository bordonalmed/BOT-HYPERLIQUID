@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
if not exist bot.log (echo bot.log ainda nao existe. Inicie o bot primeiro. & pause & exit /b)
echo Mostrando o log ao vivo. Feche esta janela para sair (o bot continua rodando).
powershell -NoProfile -Command "Get-Content bot.log -Wait -Tail 30"
