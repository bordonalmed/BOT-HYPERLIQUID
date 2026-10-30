@echo off
cd /d "%~dp0"
if not exist .env copy .env.exemplo .env >nul
echo Abrindo o .env no Bloco de Notas. Preencha, salve (Ctrl+S) e feche.
echo NUNCA envie este arquivo nem a chave para ninguem.
notepad .env
