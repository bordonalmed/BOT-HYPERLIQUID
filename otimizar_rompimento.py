#!/usr/bin/env python3
"""
Otimizador da estrategia de ROMPIMENTO com stop movel (busca de assimetria).
Somente dados publicos, sem dinheiro real.

1. Baixa ~5000 candles de 1h (~208 dias) e 4h (~833 dias) das 30 moedas de maior
   volume (minimo US$ 10 M em 24h).
2. Testa 384 variacoes: grafico, universo (10 ou 30 moedas), rompimento de 20/50 candles,
   filtro de compressao, filtro de volume, filtro EMA200, stop inicial e stop movel.
   Sem alvo fixo: o stop movel deixa o lucro correr (ganhos assimetricos).
3. Escolhe a melhor nos primeiros 60% do periodo e valida nos ultimos 40%.

Custos: taxa 0,045% por lado; slippage 0,03% nas moedas com volume >= US$ 100 M e
0,10% nas menores (book mais fino). Alavancagem maxima 4x; risco 1,5% por operacao.
Resultado: otimizacao_rompimento_resultado.txt e melhor_config_rompimento.json.

Uso: python otimizar_rompimento.py [--treino 0.6] [--sem-cache]
"""

import argparse
import itertools
import json
import os
import sys
import time

import backtest
import bot
from otimizar import metricas, periodo, selecionar_e_validar

BASE = os.path.dirname(os.path.abspath(__file__))
INTERVALOS = {"1h": 60 * 60_000, "4h": 4 * 60 * 60_000}

GRADE = {
    "intervalo": ["1h", "4h"],
    "universo": [10, 30],                  # 10 maiores (>= US$ 20 M) ou 30 maiores (>= US$ 10 M)
    "lookback": [20, 50],                  # rompe a maxima/minima de N candles
    "squeeze": [None, 1.2],                # exige compressao antes (None = nao exige)
    "vol_mult": [None, 1.5],               # exige volume 1,5x a media (None = nao exige)
    "filtro_ema200": [False, True],
    "stop_atr": [1.5, 2.5],                # stop inicial
    "trailing": [2.0, 3.0, 4.0],           # stop movel a N x ATR do melhor preco
}
SQUEEZE_JANELA = 120
SQUEEZE_RECENTE = 6


def carregar_dados(usar_cache=True):
    cfg = dict(bot.CFG, top_n=30, volume_minimo_usd=10_000_000)
    api = bot.HyperliquidAPI(cfg)
    todos = {}
    for iv_nome, iv_ms in INTERVALOS.items():
        cache = os.path.join(BASE, "cache_amplo_%s.json" % iv_nome)
        if usar_cache and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 6 * 3600:
            with open(cache) as f:
                todos[iv_nome] = json.load(f)
            print("[%s] usando cache local (%d moedas)" % (iv_nome, len(todos[iv_nome]["dados"])))
            continue
        c = dict(cfg, intervalo=iv_nome, intervalo_ms=iv_ms)
        dias = int(4990 * iv_ms / 86_400_000)
        print("[%s] baixando ~%d dias de 30 moedas..." % (iv_nome, dias))
        dados = backtest.baixar(c, dias, api)
        dados = {m: cs for m, cs in dados.items() if len(cs) > 300}
        todos[iv_nome] = {"universo": [[m, v] for m, v in backtest.UNIVERSO_BAIXADO], "dados": dados}
        with open(cache, "w") as f:
            json.dump(todos[iv_nome], f)
    return todos


def subconjunto(pacote, n):
    """As n maiores moedas por volume (10 maiores exigem US$ 20 M)."""
    minimo = 20_000_000 if n <= 10 else 10_000_000
    nomes = [m for m, v in pacote["universo"] if v >= minimo and m in pacote["dados"]][:n]
    return {m: pacote["dados"][m] for m in nomes}


def chave_sinal(p):
    return (p["lookback"], p["squeeze"], p["vol_mult"], p["filtro_ema200"])


def preparar_sinais(pacote):
    """Pre-calcula, para cada combinacao de filtros, o sinal de cada candle de cada moeda,
    usando a MESMA funcao do bot (bot.sinal_rompimento_em)."""
    cfg = dict(bot.CFG, filtro_ema=200)
    chaves = set(itertools.product(GRADE["lookback"], GRADE["squeeze"], GRADE["vol_mult"],
                                   GRADE["filtro_ema200"]))
    sinais = {k: {} for k in chaves}
    for m, cs in pacote["dados"].items():
        ind = bot.indicadores_rompimento(cs, cfg)
        acum = [0.0]
        for c in cs:
            acum.append(acum[-1] + c["c"] * c["v"])
        n = len(cs)
        h, l, c_ = ind["h"], ind["l"], ind["c"]
        for lb in GRADE["lookback"]:
            # candidatos: candles que rompem (o resto nunca da sinal, qualquer que seja o filtro)
            rompe = [i for i in range(lb, n) if c_[i] > max(h[i - lb:i]) or c_[i] < min(l[i - lb:i])]
            for k in chaves:
                if k[0] != lb:
                    continue
                p = {"lookback": lb, "squeeze_fator": k[1], "squeeze_janela": SQUEEZE_JANELA,
                     "squeeze_recente": SQUEEZE_RECENTE, "vol_mult": k[2]}
                arr = [None] * n
                for i in rompe:
                    s = bot.sinal_rompimento_em(ind, i, p, 200 if k[3] else None)
                    if s:
                        s["vol20"] = acum[i + 1] - acum[max(0, i + 1 - 20)]
                        arr[i] = s
                sinais[k][m] = arr
    return sinais


def cfg_de(p, slips):
    iv = INTERVALOS[p["intervalo"]]
    return dict(bot.CFG, estrategia="rompimento", intervalo=p["intervalo"], intervalo_ms=iv,
                stop_atr_mult=p["stop_atr"], alvo_mult_stop=None, trailing_atr_mult=p["trailing"],
                tempo_max_min=None, filtro_ema=200 if p["filtro_ema200"] else None,
                slip_moeda=slips,
                rompimento={"lookback": p["lookback"], "squeeze_fator": p["squeeze"],
                            "squeeze_janela": SQUEEZE_JANELA, "squeeze_recente": SQUEEZE_RECENTE,
                            "vol_mult": p["vol_mult"]})


def fmt(p):
    return ("%-2s top%-2d romp=%-2d compressao=%-3s volume=%-4s filtro200=%-3s stop=%.1fxATR movel=%.0fxATR"
            % (p["intervalo"], p["universo"], p["lookback"], "sim" if p["squeeze"] else "nao",
               "sim" if p["vol_mult"] else "nao", "sim" if p["filtro_ema200"] else "nao",
               p["stop_atr"], p["trailing"]))


def otimizar(todos, frac_treino=0.6, saida=print):
    t0 = time.time()
    saida("Pre-calculando sinais (mesmo codigo do bot)...")
    sinais = {iv: preparar_sinais(pac) for iv, pac in todos.items()}
    slips = {iv: {m: bot.slippage_da_moeda(v, bot.CFG) for m, v in pac["universo"]}
             for iv, pac in todos.items()}
    saida("  pronto em %.0fs" % (time.time() - t0))
    combos = [dict(zip(GRADE, v)) for v in itertools.product(*GRADE.values())]
    combos = [p for p in combos if p["intervalo"] in todos]

    def rodar(p, inicio_frac, fim_frac):
        dados = subconjunto(todos[p["intervalo"]], p["universo"])
        if not dados:
            return metricas([])
        ini, corte, fim = periodo(dados, frac_treino)
        t_ini, t_fim = (ini, corte) if inicio_frac == 0 else (corte + 1, fim)
        tab = sinais[p["intervalo"]][chave_sinal(p)]
        return metricas(backtest.simular(dados, cfg_de(p, slips[p["intervalo"]]),
                                         sinal_fn=lambda m, i: tab[m][i], t_inicio=t_ini, t_fim=t_fim))

    saida("Testando %d variacoes no TREINO..." % len(combos))
    resultados = []
    for k, p in enumerate(combos, 1):
        resultados.append((p, rodar(p, 0, frac_treino)))
        if k % 48 == 0:
            saida("  %d/%d (%.0fs)" % (k, len(combos), time.time() - t0))
    return selecionar_e_validar(resultados, lambda p: rodar(p, frac_treino, 1), fmt, frac_treino, saida)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--treino", type=float, default=0.6)
    ap.add_argument("--sem-cache", action="store_true")
    a = ap.parse_args()
    linhas = []

    def saida(txt):
        print(txt)
        sys.stdout.flush()
        linhas.append(txt)

    saida("OTIMIZADOR DE ROMPIMENTO / ASSIMETRIA (simulacao, sem dinheiro real)")
    try:
        todos = carregar_dados(not a.sem_cache)
    except Exception as e:
        saida("FALHA ao baixar dados: %s" % e)
        sys.exit(1)
    escolhida, m_teste, aprovada = otimizar(todos, a.treino, saida)
    saida("")
    saida("Limites: universo = maiores volumes de HOJE (vies de sobrevivencia, maior no top 30);")
    saida("resultado passado nao garante futuro; custos de ordem a mercado (conservador).")
    with open(os.path.join(BASE, "otimizacao_rompimento_resultado.txt"), "w") as f:
        f.write("\n".join(linhas) + "\n")
    if escolhida:
        cfg = cfg_de(escolhida, {})
        aplicar = {k: cfg[k] for k in ("estrategia", "intervalo", "intervalo_ms", "stop_atr_mult",
                                       "alvo_mult_stop", "trailing_atr_mult", "tempo_max_min",
                                       "filtro_ema", "rompimento")}
        aplicar["top_n"] = escolhida["universo"]
        aplicar["volume_minimo_usd"] = 20_000_000 if escolhida["universo"] <= 10 else 10_000_000
        with open(os.path.join(BASE, "melhor_config_rompimento.json"), "w") as f:
            json.dump({"parametros": escolhida, "aprovada": bool(aprovada), "teste": m_teste,
                       "cfg_para_o_bot": aplicar}, f, indent=2)


if __name__ == "__main__":
    main()
