@echo off
cd /d "%~dp0"
for %%I in ("%CD%") do set PASTA=%%~nxI
set IP=
if exist servidor_ip.txt set /p IP=<servidor_ip.txt
if "%IP%"=="" set /p IP=Digite o IP do servidor e aperte Enter: 
echo O que voce quer fazer no servidor %IP%?
echo   1 = ver status
echo   2 = PARAR o bot (posicoes continuam protegidas na corretora)
echo   3 = LIGAR o bot
echo   4 = REINICIAR o bot
echo   5 = FECHAR TODAS AS POSICOES a mercado e parar
set OP=
set /p OP=Digite o numero e aperte Enter: 
set CMD=
if "%OP%"=="1" set CMD=status
if "%OP%"=="2" set CMD=parar
if "%OP%"=="3" set CMD=iniciar
if "%OP%"=="4" set CMD=reiniciar
if "%OP%"=="5" set CMD=fechar_tudo
if "%CMD%"=="" (
  echo Opcao invalida.
  pause
  exit /b
)
ssh root@%IP% "bash /opt/%PASTA%/servidor/comandos.sh %CMD%"
pause
