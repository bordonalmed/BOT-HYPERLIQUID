@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
type nul > KILL
echo Arquivo KILL criado. O bot salva o estado e encerra em poucos segundos.
echo (3_iniciar_bot.bat apaga o KILL automaticamente na proxima vez.)
pause
