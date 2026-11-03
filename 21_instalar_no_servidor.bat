@echo off
cd /d "%~dp0"
for %%I in ("%CD%") do set PASTA=%%~nxI
echo ============================================================
echo   INSTALAR O BOT NO SERVIDOR (roda 24 horas)
echo ============================================================
echo IMPORTANTE: o bot NAO pode rodar no notebook e no servidor ao mesmo tempo.
echo Se ele estiver ligado neste notebook, feche esta janela e rode 6_parar_bot.bat antes.
echo.
set IP=
set /p IP=Digite o IP do servidor e aperte Enter: 
if "%IP%"=="" exit /b
>servidor_ip.txt echo %IP%
echo.
echo 1) Copiando a pasta do bot para o servidor...
echo    Se aparecer "Are you sure you want to continue connecting", digite yes e Enter.
echo    Depois digite a senha do servidor. Ela NAO aparece enquanto voce digita; e normal.
scp -r "%CD%" root@%IP%:/opt/
if errorlevel 1 (
  echo ERRO ao copiar. Confira o IP e a senha.
  pause
  exit /b
)
echo.
echo 2) Instalando no servidor (digite a senha de novo, leva 2 a 4 minutos)...
ssh -t root@%IP% "bash /opt/%PASTA%/servidor/instalar.sh"
echo.
echo Pronto. Para ver o painel do servidor, rode 22_ver_servidor.bat
echo NAO ligue mais o bot neste notebook (3_iniciar_bot.bat), ele agora roda no servidor.
pause
