#!/usr/bin/env python3
"""
Backtest da mesma estrategia do bot sobre o historico de candles 15m da Hyperliquid
(a API publica guarda ~5000 candles por moeda = ~52 dias).

Somente leitura de dados publicos. Nao mexe no bot, no state.json nem no trades.csv.
Resultado: backtest_trades.csv (mesmo formato do trades.csv) + relatorio na tela.

Uso: python backtest.py [--dias 50] [--saida backtest_trades.csv]

Regras da simulacao (mesmas do bot, com as adaptacoes necessarias para candles):
- Sinal avaliado no fechamento do candle (janela de 200 candles, igual ao bot);
  a entrada acontece na ABERTURA do candle seguinte, com slippage.
- Stop/alvo checados pela maxima e minima de cada candle. Se o candle tocar stop
  e alvo, conta como STOP (conservador). Se abrir alem do stop, sai na abertura.
- Saida por tempo no fechamento do candle em que completar 240 min.
- Limite diario checado no fechamento de cada candle (equity com posicoes abertas).
- Taxas, slippage, 2 posicoes, 8 operacoes/dia, risco 1,5%, alavancagem 2x: iguais ao bot.
"""

import argparse
import csv
import os
import sys
import time

import bot
import report


def baixar(cfg, dias):
    api = bot.HyperliquidAPI(cfg)
    agora = int(time.time() * 1000)
    inicio = agora - dias * 86_400_000
    universo = bot.parse_universo(api.meta_and_ctxs(), cfg)
    print("Universo (top %d por volume ATUAL): %s" % (len(universo), ", ".join(m for m, _ in universo)))
    dados = {}
    for moeda, _ in universo:
        try:
            cs = bot.parse_candles(api.candles(moeda, cfg["intervalo"], inicio, agora), agora)
        except Exception as e:
            print("  %s: erro ao baixar (%s), ignorada" % (moeda, e))
            continue
        dados[moeda] = cs
        if cs:
            print("  %-8s %5d candles  (%s -> %s)" % (moeda, len(cs), bot.iso_utc(cs[0]["t"])[:10],
                                                     bot.iso_utc(cs[-1]["t"])[:10]))
        time.sleep(0.3)
    return dados


def simular(dados, cfg=bot.CFG, saida_csv=None):
    """dados: {moeda: [candles fechados ordenados]}. Retorna lista de trades (dicts)."""
    janela = cfg["candles_historico"]
    iv = cfg["intervalo_ms"]
    idx = {m: {c["t"]: i for i, c in enumerate(cs)} for m, cs in dados.items()}
    tempos = sorted({c["t"] for cs in dados.values() for c in cs})
    slip, taxa = cfg["slippage_por_lado"], cfg["taxa_por_lado"]

    capital = cfg["capital_inicial"]
    posicoes, pendentes, trades = [], [], []
    dia, eq_dia, trades_hoje, pausado = None, capital, 0, False

    def pnl_aberto(p, preco):
        s = bot.preco_execucao(preco, p["lado"], "saida", slip)
        return bot.pnl_liquido(p["lado"], p["entrada"], s, p["tamanho"], taxa)[0]

    def fechar(p, ref, motivo, t_ms):
        nonlocal capital
        s = bot.preco_execucao(ref, p["lado"], "saida", slip)
        pnl, taxas = bot.pnl_liquido(p["lado"], p["entrada"], s, p["tamanho"], taxa)
        capital += pnl
        posicoes.remove(p)
        trades.append({
            "horario_utc": bot.iso_utc(t_ms), "moeda": p["moeda"], "lado": p["lado"],
            "entrada": "%.8g" % p["entrada"], "saida": "%.8g" % s, "tamanho": "%.8g" % p["tamanho"],
            "motivo": motivo, "pnl_usd": "%.6f" % pnl, "capital_apos": "%.6f" % capital,
            "taxas_usd": "%.6f" % taxas, "horario_entrada_utc": bot.iso_utc(p["entrada_ms"]),
        })

    ultimo_close = {}
    for t in tempos:
        candle = {m: dados[m][idx[m][t]] for m in dados if t in idx[m]}
        for m, c in candle.items():
            ultimo_close[m] = c["c"]

        # novo dia UTC
        d = bot.dia_utc(t)
        if d != dia:
            dia, trades_hoje, pausado = d, 0, False
            eq_dia = capital + sum(pnl_aberto(p, candle[p["moeda"]]["o"] if p["moeda"] in candle
                                              else ultimo_close[p["moeda"]]) for p in posicoes)

        # 1) entradas pendentes na abertura deste candle
        for s in pendentes:
            c = candle.get(s["moeda"])
            if c is None or pausado:
                continue
            entrada = bot.preco_execucao(c["o"], s["lado"], "entrada", slip)
            dist = cfg["stop_atr_mult"] * s["atr"]
            tam = bot.calcular_tamanho(capital, entrada, dist, cfg)
            if tam is None:
                continue
            if s["lado"] == "long":
                stop, alvo = entrada - dist, entrada + cfg["alvo_mult_stop"] * dist
            else:
                stop, alvo = entrada + dist, entrada - cfg["alvo_mult_stop"] * dist
            posicoes.append({"moeda": s["moeda"], "lado": s["lado"], "entrada": entrada,
                             "tamanho": tam[0], "stop": stop, "alvo": alvo, "entrada_ms": t})
            trades_hoje += 1
        pendentes = []

        # 2) stop / alvo / tempo com maxima e minima do candle
        for p in list(posicoes):
            c = candle.get(p["moeda"])
            if c is None:
                continue
            if p["lado"] == "long":
                if c["l"] <= p["stop"]:
                    fechar(p, min(c["o"], p["stop"]), "stop", c["T"])
                    continue
                if c["h"] >= p["alvo"]:
                    fechar(p, p["alvo"], "alvo", c["T"])
                    continue
            else:
                if c["h"] >= p["stop"]:
                    fechar(p, max(c["o"], p["stop"]), "stop", c["T"])
                    continue
                if c["l"] <= p["alvo"]:
                    fechar(p, p["alvo"], "alvo", c["T"])
                    continue
            if c["T"] + 1 - p["entrada_ms"] >= cfg["tempo_max_min"] * 60_000:
                fechar(p, c["c"], "tempo", c["T"])

        # 3) limite diario no fechamento
        if not pausado:
            eq = capital + sum(pnl_aberto(p, ultimo_close[p["moeda"]]) for p in posicoes)
            if eq <= eq_dia * (1 - cfg["perda_diaria_max"]):
                for p in list(posicoes):
                    fechar(p, ultimo_close[p["moeda"]], "limite_diario", t + iv - 1)
                pausado = True

        # 4) sinais no fechamento -> entram na abertura do proximo candle
        vagas = cfg["max_posicoes"] - len(posicoes)
        if pausado or vagas <= 0 or trades_hoje >= cfg["max_trades_dia"]:
            continue
        abertas = {p["moeda"] for p in posicoes}
        cands = []
        for m in candle:
            if m in abertas:
                continue
            i = idx[m][t]
            hist = dados[m][max(0, i + 1 - janela):i + 1]
            sinal = bot.avaliar_sinal(hist, cfg)
            if sinal:
                sinal["moeda"] = m
                sinal["vol20"] = bot.volume_usd_recente(hist, cfg["candles_volume_prioridade"])
                cands.append(sinal)
        cands.sort(key=lambda s: s["vol20"], reverse=True)
        pendentes = cands[:min(vagas, cfg["max_trades_dia"] - trades_hoje)]

    # fecha o que sobrou no ultimo preco
    for p in list(posicoes):
        fechar(p, ultimo_close[p["moeda"]], "fim_backtest", tempos[-1] + iv - 1)

    if saida_csv:
        with open(saida_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=bot.TRADES_CAMPOS)
            w.writeheader()
            w.writerows(trades)
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=50, help="dias de historico (max ~52 em 15m)")
    ap.add_argument("--saida", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "backtest_trades.csv"))
    a = ap.parse_args()
    cfg = dict(bot.CFG)
    print("BACKTEST (simulacao sobre historico, sem dinheiro real)")
    print("Baixando %d dias de candles 15m..." % a.dias)
    try:
        dados = baixar(cfg, a.dias)
    except Exception as e:
        print("FALHA ao baixar dados: %s" % e)
        sys.exit(1)
    dados = {m: cs for m, cs in dados.items() if len(cs) > cfg["ema_lenta"] + 10}
    if not dados:
        print("Sem dados suficientes.")
        sys.exit(1)
    print("Simulando...")
    trades = simular(dados, cfg, a.saida)
    print("Operacoes simuladas: %d  ->  %s\n" % (len(trades), a.saida))
    if trades:
        print(report.relatorio(report.carregar(a.saida)))
    print("\nATENCAO (limites do backtest):")
    print("- O universo usa as 10 moedas de maior volume HOJE (vies de sobrevivencia).")
    print("- Stop/alvo pela maxima/minima do candle; empate conta como stop (conservador).")
    print("- Resultado passado nao garante resultado futuro. O paper trading ao vivo continua")
    print("  sendo a validacao final da execucao.")


if __name__ == "__main__":
    main()
