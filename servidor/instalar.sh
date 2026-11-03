#!/usr/bin/env bash
# Instala o bot como servico no servidor Linux (Ubuntu/Debian), rodando 24h.
# Uso (no servidor, como root): bash servidor/instalar.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "== Instalando o bot em $DIR =="

echo "1/5 Instalando Python e firewall..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ufw >/dev/null

echo "2/5 Criando ambiente Python e instalando dependencias (1-3 min)..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "3/5 Ajustando arquivos..."
rm -f "$DIR/bot.lock" "$DIR/KILL" "$DIR/KILL_FECHAR"
rm -rf "$DIR/__pycache__"
if [ -f "$DIR/.env" ]; then
  sed -i 's/\r$//' "$DIR/.env"          # remove quebras de linha do Windows
  chmod 600 "$DIR/.env"                  # so o root le a chave
else
  echo "ATENCAO: .env nao encontrado em $DIR. O bot vai rodar em modo PAPER (simulacao)."
fi

echo "4/5 Criando servicos (reiniciam sozinhos)..."
cat > /etc/systemd/system/bot-hyperliquid.service <<UNIT
[Unit]
Description=Bot Hyperliquid
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -u $DIR/bot.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$DIR/bot.log
StandardError=append:$DIR/bot.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/dashboard-hyperliquid.service <<UNIT
[Unit]
Description=Dashboard do bot Hyperliquid (so acessivel por tunel SSH)
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -u $DIR/dashboard.py --sem-navegador
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

echo "5/5 Firewall e iniciando..."
# O painel so escuta em 127.0.0.1 (acesso por tunel SSH), entao o firewall e um extra.
# Para nao derrubar outros servicos (ex.: OpenClaw no mesmo servidor), so liga o ufw
# se ele ja estiver ativo ou se nada alem do SSH estiver aberto para a internet.
OUTROS=$(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | grep -vE ':22$' || true)
if ufw status | grep -q "Status: active"; then
  ufw allow OpenSSH >/dev/null
  echo "   ufw ja estava ativo: SSH liberado, resto mantido como estava."
elif [ -z "$OUTROS" ]; then
  ufw allow OpenSSH >/dev/null
  ufw --force enable >/dev/null
  echo "   ufw ativado: so o SSH fica aberto para a internet."
else
  echo "   Ha outros servicos abertos neste servidor; firewall NAO foi alterado para nao derruba-los."
fi
systemctl daemon-reload
systemctl enable --now bot-hyperliquid dashboard-hyperliquid >/dev/null 2>&1
systemctl restart bot-hyperliquid dashboard-hyperliquid
sleep 8
echo
echo "== Ultimas linhas do log =="
tail -n 12 "$DIR/bot.log" || true
echo
if systemctl is-active --quiet bot-hyperliquid; then
  echo "OK: BOT RODANDO 24H NO SERVIDOR."
else
  echo "ERRO: o bot nao esta rodando. Veja o log acima."
fi
