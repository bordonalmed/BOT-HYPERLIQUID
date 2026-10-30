@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
type nul > KILL
echo Arquivo KILL criado. O bot salva o estado e encerra em poucos segundos.
echo (3_iniciar_bot.bat apaga o KILL automaticamente na proxima vez.)
pause
