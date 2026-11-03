#!/usr/bin/env bash
# Atalhos no servidor. Uso: bash servidor/comandos.sh [status|log|parar|iniciar|reiniciar|fechar_tudo]
DIR="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-status}" in
  status)      systemctl --no-pager status bot-hyperliquid | head -5; tail -n 15 "$DIR/bot.log" ;;
  log)         tail -n 40 -f "$DIR/bot.log" ;;
  parar)       systemctl stop bot-hyperliquid && echo "Bot parado (posicoes continuam protegidas na corretora)." ;;
  iniciar)     rm -f "$DIR/KILL"; systemctl start bot-hyperliquid && echo "Bot iniciado." ;;
  reiniciar)   systemctl restart bot-hyperliquid dashboard-hyperliquid && echo "Bot reiniciado." ;;
  fechar_tudo) touch "$DIR/KILL_FECHAR" && echo "KILL_FECHAR criado: o bot fecha tudo a mercado e para em ate 30 s." ;;
  *) echo "Use: status | log | parar | iniciar | reiniciar | fechar_tudo" ;;
esac
