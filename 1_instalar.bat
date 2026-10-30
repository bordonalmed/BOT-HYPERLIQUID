@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo Instalando dependencias...
%PY% -m pip install -r requirements.txt
echo.
echo Pronto. Agora rode 2_testar_conexao.bat
pause
