@echo off
cd /d "%~dp0"
set PY=
for %%v in (3.13 3.12 3.11 3.10 3.9) do if not defined PY (py -%%v --version >nul 2>nul && set "PY=py -%%v")
if not defined PY (where py >nul 2>nul && (set PY=py) || (set PY=python))
echo Abrindo o dashboard com acesso pelo celular (mesmo Wi-Fi)...
echo Feche o 8_dashboard.bat antes, se estiver aberto.
%PY% dashboard.py --rede
pause
