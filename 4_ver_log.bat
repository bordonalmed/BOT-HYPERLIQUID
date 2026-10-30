@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
if not exist bot.log (echo bot.log ainda nao existe. Inicie o bot primeiro. & pause & exit /b)
echo Mostrando o log ao vivo. Feche esta janela para sair (o bot continua rodando).
powershell -NoProfile -Command "Get-Content bot.log -Wait -Tail 30"
