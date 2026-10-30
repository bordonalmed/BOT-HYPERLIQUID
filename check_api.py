#!/usr/bin/env python3
"""Teste real de conexao com a API publica da Hyperliquid (somente leitura)."""
import sys
import time

import bot


def main():
    api = bot.HyperliquidAPI(dict(bot.CFG, http_tentativas=2, http_espera_base_s=1))
    agora = int(time.time() * 1000)
    try:
        u = bot.parse_universo(api.meta_and_ctxs())
        print("Universo (top por volume 24h):")
        for m, v in u:
            print(f"  {m:<10} US$ {v / 1e6:,.0f} M")
        mids = bot.parse_mids(api.all_mids())
        moeda = u[0][0] if u else "BTC"
        print(f"Precos recebidos: {len(mids)} | {moeda} = {mids.get(moeda)}")
        cs = bot.parse_candles(api.candles(moeda, "15m", agora - 200 * bot.CFG["intervalo_ms"], agora), agora)
        print(f"Candles 15m fechados de {moeda}: {len(cs)} | ultimo: {cs[-1] if cs else None}")
        s = bot.avaliar_sinal(cs)
        print(f"Sinal atual de {moeda}: {s['lado'] if s else 'nenhum'}")
        print("OK: conexao e parse funcionando.")
    except Exception as e:
        print(f"FALHA na conexao/parse: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
