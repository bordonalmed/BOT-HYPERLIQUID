#!/usr/bin/env python3
"""
Otimizador com validacao fora da amostra (somente dados publicos, sem dinheiro real).

1. Baixa o historico das 10 moedas de maior volume em 15m, 1h e 4h
   (a API guarda ~5000 candles: ~52 dias em 15m, ~208 dias em 1h, ~833 dias em 4h).
2. Testa centenas de variacoes da estrategia nos primeiros 60% do periodo (TREINO),
   ordenando por lucro / drawdown.
3. Escolhe a melhor do treino e a testa nos ultimos 40% (TESTE), que ela nunca viu.
4. So e APROVADA se tambem passar no teste.

Custos sempre conservadores: taxa 0,045% + slippage 0,03% por lado (ordem a mercado).
Resultado: otimizacao_resultado.txt e melhor_config.json.

Uso: python otimizar.py [--treino 0.6] [--sem-cache]
"""

import argparse
import itertools
import json
import os
import sys
import time

import backtest
import bot
import report

BASE = os.path.dirname(os.path.abspath(__file__))

INTERVALOS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}

GRADE = {
    "intervalo": ["15m", "1h", "4h"],
    "stop_atr": [1.5, 2.0, 3.0],
    "rr": [1.5, 2.0, 3.0],                 # alvo = rr x distancia do stop
    "filtro_ema200": [False, True],        # so opera a favor da EMA200
    "rsi": ["original", "larga"],
    "max_dia": [3, 8],
    "tempo_candles": [16, 48],             # saida por tempo, em candles
}
RSI = {"original": ((52, 68), (32, 48)), "larga": ((50, 72), (28, 50))}

MIN_TRADES_TREINO = 30
MIN_TRADES_TESTE = 15
APROVA_PF = 1.2
APROVA_DD = 15.0


# ---------------------------------------------------------------------------
def carregar_dados(usar_cache=True):
    cfg = dict(bot.CFG)
    api = bot.HyperliquidAPI(cfg)
    todos = {}
    for iv_nome, iv_ms in INTERVALOS.items():
        cache = os.path.join(BASE, "cache_candles_%s.json" % iv_nome)
        if usar_cache and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 6 * 3600:
            with open(cache) as f:
                todos[iv_nome] = json.load(f)
            print("[%s] usando cache local (%d moedas)" % (iv_nome, len(todos[iv_nome])))
            continue
        c = dict(cfg, intervalo=iv_nome, intervalo_ms=iv_ms)
        dias = int(5000 * iv_ms / 86_400_000)
        print("[%s] baixando ~%d dias..." % (iv_nome, dias))
        dados = backtest.baixar(c, dias, api)
        dados = {m: cs for m, cs in dados.items() if len(cs) > 260}
        todos[iv_nome] = dados
        with open(cache, "w") as f:
            json.dump(dados, f)
    return todos


# ---------------------------------------------------------------------------
def preparar(dados):
    """Pre-calcula indicadores da serie inteira de cada moeda (rapido)."""
    ind = {}
    for m, cs in dados.items():
        closes = [c["c"] for c in cs]
        highs = [c["h"] for c in cs]
        lows = [c["l"] for c in cs]
        vol = [c["c"] * c["v"] for c in cs]
        acum = [0.0]
        for v in vol:
            acum.append(acum[-1] + v)
        ind[m] = {
            "c": closes, "e20": bot.ema(closes, 20), "e50": bot.ema(closes, 50),
            "e200": bot.ema(closes, 200), "rsi": bot.rsi(closes, 14),
            "atr": bot.atr(highs, lows, closes, 14), "acum": acum,
        }
    return ind


def fabrica_sinal(ind, p):
    (llo, lhi), (slo, shi) = RSI[p["rsi"]]
    filtro = p["filtro_ema200"]

    def sinal(m, i):
        d = ind[m]
        e_r, e_l, r, a, preco = d["e20"][i], d["e50"][i], d["rsi"][i], d["atr"][i], d["c"][i]
        if e_r is None or e_l is None or r is None or a is None or preco <= 0 or a <= 0:
            return None
        if abs(e_r - e_l) / preco < 0.001:
            return None
        e200 = d["e200"][i]
        if filtro and e200 is None:
            return None
        vol20 = d["acum"][i + 1] - d["acum"][max(0, i + 1 - 20)]
        if e_r > e_l and preco > e_r and llo <= r <= lhi and (not filtro or preco > e200):
            return {"lado": "long", "atr": a, "vol20": vol20}
        if e_r < e_l and preco < e_r and slo <= r <= shi and (not filtro or preco < e200):
            return {"lado": "short", "atr": a, "vol20": vol20}
        return None
    return sinal


def cfg_de(p):
    iv = INTERVALOS[p["intervalo"]]
    return dict(bot.CFG, intervalo=p["intervalo"], intervalo_ms=iv,
                stop_atr_mult=p["stop_atr"], alvo_mult_stop=p["rr"],
                max_trades_dia=p["max_dia"],
                tempo_max_min=p["tempo_candles"] * iv // 60_000)


def metricas(trades, cap_ini=50.0):
    pnls = [float(t["pnl_usd"]) for t in trades]
    if not pnls:
        return {"n": 0, "liq": 0.0, "pf": 0.0, "dd": 0.0, "acerto": 0.0, "final": cap_ini}
    ganhos = sum(x for x in pnls if x > 0)
    perdas = -sum(x for x in pnls if x < 0)
    caps = [float(t["capital_apos"]) for t in trades]
    return {
        "n": len(pnls), "liq": sum(pnls),
        "pf": (ganhos / perdas) if perdas > 0 else 99.0,
        "dd": report.drawdown_max_pct(cap_ini, caps),
        "acerto": sum(1 for x in pnls if x > 0) / len(pnls) * 100,
        "final": caps[-1],
    }


def periodo(dados, frac):
    tempos = sorted({c["t"] for cs in dados.values() for c in cs})
    ini = tempos[min(len(tempos) - 1, 200)]          # 200 candles de aquecimento
    corte = ini + int((tempos[-1] - ini) * frac)
    return ini, corte, tempos[-1]


def fmt(p):
    return ("%-3s stop=%.1fxATR alvo=%.1fx filtro200=%-3s rsi=%-8s max/dia=%d tempo=%d candles"
            % (p["intervalo"], p["stop_atr"], p["rr"], "sim" if p["filtro_ema200"] else "nao",
               p["rsi"], p["max_dia"], p["tempo_candles"]))


def fmt_m(m):
    return ("ops %4d | liq US$ %+7.2f | PF %5.2f | acerto %4.1f%% | DD %5.1f%% | final US$ %6.2f"
            % (m["n"], m["liq"], m["pf"], m["acerto"], m["dd"], m["final"]))


# ---------------------------------------------------------------------------
def otimizar(todos, frac_treino=0.6, saida=print):
    prep = {iv: preparar(d) for iv, d in todos.items() if d}
    combos = [dict(zip(GRADE, v)) for v in itertools.product(*GRADE.values())]
    combos = [p for p in combos if p["intervalo"] in prep]
    saida("Testando %d variacoes no TREINO..." % len(combos))
    resultados = []
    t0 = time.time()
    for k, p in enumerate(combos, 1):
        dados = todos[p["intervalo"]]
        ini, corte, _ = periodo(dados, frac_treino)
        tr = backtest.simular(dados, cfg_de(p), sinal_fn=fabrica_sinal(prep[p["intervalo"]], p),
                              t_inicio=ini, t_fim=corte)
        resultados.append((p, metricas(tr)))
        if k % 50 == 0:
            saida("  %d/%d (%.0fs)" % (k, len(combos), time.time() - t0))

    validos = [(p, m) for p, m in resultados if m["n"] >= MIN_TRADES_TREINO]
    # ordena por lucro em relacao ao drawdown (evita escolher variacoes muito arriscadas)
    validos.sort(key=lambda x: x[1]["liq"] / max(x[1]["dd"], 5.0), reverse=True)
    if not validos:
        saida("Nenhuma variacao teve operacoes suficientes no treino.")
        return None, None, None

    saida("")
    saida("TOP 10 NO TREINO (primeiros %.0f%% do periodo):" % (frac_treino * 100))
    for p, m in validos[:10]:
        saida("  " + fmt(p))
        saida("      " + fmt_m(m))

    saida("")
    saida("VALIDACAO NOS ULTIMOS %.0f%% (dados que o otimizador NAO viu):" % ((1 - frac_treino) * 100))
    testes = []
    for p, m in validos[:5]:
        dados = todos[p["intervalo"]]
        _, corte, fim = periodo(dados, frac_treino)
        tr = backtest.simular(dados, cfg_de(p), sinal_fn=fabrica_sinal(prep[p["intervalo"]], p),
                              t_inicio=corte + 1, t_fim=fim)
        mt = metricas(tr)
        testes.append((p, m, mt))
        saida("  " + fmt(p))
        saida("      teste: " + fmt_m(mt))

    escolhida, m_treino, m_teste = testes[0]   # a melhor do TREINO, sem escolher pelo teste
    aprovada = (m_teste["n"] >= MIN_TRADES_TESTE and m_teste["liq"] > 0
                and m_teste["pf"] >= APROVA_PF and m_teste["dd"] < APROVA_DD)
    saida("")
    saida("=" * 70)
    saida("ESCOLHIDA (melhor do treino): " + fmt(escolhida))
    saida("  treino: " + fmt_m(m_treino))
    saida("  teste : " + fmt_m(m_teste))
    if aprovada:
        saida("VEREDITO: APROVADA no teste fora da amostra (PF >= %.1f, lucro, DD < %.0f%%, >= %d ops)."
              % (APROVA_PF, APROVA_DD, MIN_TRADES_TESTE))
        saida("Proximo passo: paper trading ao vivo com esta configuracao antes de dinheiro real.")
    else:
        motivos = []
        if m_teste["n"] < MIN_TRADES_TESTE:
            motivos.append("poucas operacoes no teste (%d)" % m_teste["n"])
        if m_teste["liq"] <= 0:
            motivos.append("prejuizo no teste")
        if m_teste["pf"] < APROVA_PF:
            motivos.append("fator de lucro %.2f < %.1f" % (m_teste["pf"], APROVA_PF))
        if m_teste["dd"] >= APROVA_DD:
            motivos.append("drawdown %.1f%%" % m_teste["dd"])
        saida("VEREDITO: REPROVADA (" + "; ".join(motivos) + ").")
        saida("A melhor variacao do passado NAO se sustentou em dados novos. Nao use dinheiro real.")
    saida("=" * 70)
    return escolhida, m_teste, aprovada


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

    saida("OTIMIZADOR (simulacao sobre historico, sem dinheiro real)")
    try:
        todos = carregar_dados(not a.sem_cache)
    except Exception as e:
        saida("FALHA ao baixar dados: %s" % e)
        sys.exit(1)
    escolhida, m_teste, aprovada = otimizar(todos, a.treino, saida)
    saida("")
    saida("Limites: universo = 10 maiores volumes de HOJE (vies de sobrevivencia); resultado")
    saida("passado nao garante futuro; custos de ordem a mercado (conservador).")
    with open(os.path.join(BASE, "otimizacao_resultado.txt"), "w") as f:
        f.write("\n".join(linhas) + "\n")
    if escolhida:
        with open(os.path.join(BASE, "melhor_config.json"), "w") as f:
            json.dump({"parametros": escolhida, "aprovada": bool(aprovada), "teste": m_teste}, f, indent=2)


if __name__ == "__main__":
    main()
