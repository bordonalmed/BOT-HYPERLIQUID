@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo Usando: %PY%
%PY% --version
echo Instalando dependencias...
%PY% -m pip install -r requirements.txt
echo.
echo Pronto. Agora rode 2_testar_conexao.bat
pause
