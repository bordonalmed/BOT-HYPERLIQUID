@echo off
cd /d "%~dp0"
echo Isso ARQUIVA state.json, trades.csv e bot.log numa pasta e recomeca o paper trading com US$ 50.
echo PARE O BOT ANTES (6_parar_bot.bat) e espere a janela BOT-HYPERLIQUID fechar.
choice /m "Continuar"
if errorlevel 2 exit /b
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set D=arquivo_%%i
mkdir "%D%"
for %%f in (state.json trades.csv bot.log) do if exist %%f move %%f "%D%\" >nul
echo Arquivos antigos guardados em %D%
echo Agora rode 3_iniciar_bot.bat para comecar do zero.
pause
