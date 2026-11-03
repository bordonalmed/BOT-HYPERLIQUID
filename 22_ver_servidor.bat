@echo off
cd /d "%~dp0"
for %%I in ("%CD%") do set PASTA=%%~nxI
set IP=
if exist servidor_ip.txt set /p IP=<servidor_ip.txt
if "%IP%"=="" set /p IP=Digite o IP do servidor e aperte Enter: 
echo Conectando ao servidor %IP% (digite a senha)...
echo.
echo O painel do SERVIDOR abre no navegador em http://localhost:8051
echo (se abrir antes de voce digitar a senha, e so atualizar a pagina com F5).
echo Esta janela mostra o log do bot. Fechar esta janela NAO desliga o bot.
echo.
start "" cmd /c "timeout /t 10 >nul & start http://localhost:8051"
ssh -L 8051:localhost:8050 root@%IP% "tail -n 40 -f /opt/%PASTA%/bot.log"
pause
