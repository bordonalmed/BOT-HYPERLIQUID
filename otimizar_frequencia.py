#!/usr/bin/env python3
"""
Otimizador de FREQUENCIA da estrategia de tendencia aprovada (4h, stop 3xATR, alvo 3x,
filtro EMA200, RSI larga). NAO muda a estrategia: testa se da para operar mais vezes,
variando so:
  - universo: 10, 20 ou 30 moedas de maior volume
  - posicoes abertas ao mesmo tempo: 2, 3 ou 4
  - operacoes por dia: 3 ou 5
Escolhe a melhor nos primeiros 60% do historico (~833 dias de 4h) e valida nos ultimos 40%.
Custos: taxa 0,045% + slippage 0,03% (0,10% nas moedas com menos de US$ 100 M de volume).

Resultado: otimizacao_frequencia_resultado.txt e melhor_config_frequencia.json
(ative com 25_ativar_frequencia.bat, so funciona se APROVADA).
Uso: python otimizar_frequencia.py [--treino 0.6] [--sem-cache]
"""

import argparse
import itertools
import json
import os
import sys
import time

import backtest
import bot
from otimizar import fabrica_sinal, metricas, periodo, preparar, selecionar_e_validar
from otimizar_rompimento import subconjunto

BASE = os.path.dirname(os.path.abspath(__file__))
IV_MS = 4 * 60 * 60_000

GRADE = {
    "universo": [10, 20, 30],
    "max_posicoes": [2, 3, 4],
    "max_dia": [3, 5],
}
ATUAL = {"universo": 10, "max_posicoes": 2, "max_dia": 3}       # configuracao que o bot usa hoje
SINAL_APROVADO = {"rsi": "larga", "filtro_ema200": True}


def carregar_dados(usar_cache=True):
    cache = os.path.join(BASE, "cache_amplo_4h.json")              # o mesmo do otimizador de rompimento
    if usar_cache and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 6 * 3600:
        with open(cache) as f:
            pacote = json.load(f)
        print("[4h] usando cache local (%d moedas)" % len(pacote["dados"]))
        return pacote
    cfg = dict(bot.CFG, top_n=30, volume_minimo_usd=10_000_000, intervalo="4h", intervalo_ms=IV_MS)
    print("[4h] baixando ~831 dias de 30 moedas (1 a 3 min)...")
    dados = backtest.baixar(cfg, int(4990 * IV_MS / 86_400_000))
    dados = {m: cs for m, cs in dados.items() if len(cs) > 300}
    pacote = {"universo": [[m, v] for m, v in backtest.UNIVERSO_BAIXADO], "dados": dados}
    with open(cache, "w") as f:
        json.dump(pacote, f)
    return pacote


def cfg_de(p, slips):
    return dict(bot.CFG, max_posicoes=p["max_posicoes"], max_trades_dia=p["max_dia"], slip_moeda=slips)


def fmt(p):
    marca = "  <- configuracao ATUAL" if p == ATUAL else ""
    return "%2d moedas | %d posicoes juntas | ate %d ops/dia%s" % (
        p["universo"], p["max_posicoes"], p["max_dia"], marca)


def por_mes(m, dias):
    return m["n"] / max(dias / 30.0, 1e-9)


def otimizar(pacote, frac_treino=0.6, saida=print):
    t0 = time.time()
    ind = preparar(pacote["dados"])
    slips = {m: bot.slippage_da_moeda(v, bot.CFG) for m, v in pacote["universo"]}
    combos = [dict(zip(GRADE, v)) for v in itertools.product(*GRADE.values())]
    cache_sinal = {}

    def rodar(p, teste):
        dados = subconjunto(pacote, p["universo"])
        ini, corte, fim = periodo(dados, frac_treino)
        t_ini, t_fim = (corte + 1, fim) if teste else (ini, corte)
        sinal = cache_sinal.setdefault("s", fabrica_sinal(ind, SINAL_APROVADO))
        m = metricas(backtest.simular(dados, cfg_de(p, slips), sinal_fn=sinal, t_inicio=t_ini, t_fim=t_fim))
        m["por_mes"] = por_mes(m, (t_fim - t_ini) / 86_400_000)
        return m

    saida("Testando %d combinacoes no TREINO..." % len(combos))
    resultados = [(p, rodar(p, False)) for p in combos]
    saida("  pronto em %.0fs" % (time.time() - t0))
    saida("")
    saida("OPERACOES POR MES NO TREINO:")
    for p, m in sorted(resultados, key=lambda x: -x[1]["por_mes"]):
        saida("  %-58s %5.1f ops/mes | PF %4.2f | DD %4.1f%% | liq US$ %+.2f"
              % (fmt(p), m["por_mes"], m["pf"], m["dd"], m["liq"]))

    escolhida, m_teste, aprovada = selecionar_e_validar(resultados, lambda p: rodar(p, True),
                                                        fmt, frac_treino, saida)
    base = rodar(ATUAL, True)
    saida("")
    saida("COMPARACAO NO TESTE (dados que o otimizador nao viu):")
    saida("  ATUAL    : %-45s %5.1f ops/mes | PF %4.2f | DD %4.1f%% | liq US$ %+.2f"
          % (fmt(ATUAL).replace("  <- configuracao ATUAL", ""), base["por_mes"], base["pf"], base["dd"], base["liq"]))
    if escolhida:
        saida("  ESCOLHIDA: %-45s %5.1f ops/mes | PF %4.2f | DD %4.1f%% | liq US$ %+.2f"
              % (fmt(escolhida).replace("  <- configuracao ATUAL", ""), m_teste["por_mes"], m_teste["pf"],
                 m_teste["dd"], m_teste["liq"]))
        if aprovada and escolhida != ATUAL:
            saida("Para usar no bot: 25_ativar_frequencia.bat e reinicie o bot.")
        elif escolhida == ATUAL:
            saida("A configuracao atual ja e a melhor: nada a mudar.")
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

    saida("OTIMIZADOR DE FREQUENCIA (estrategia aprovada; simulacao, sem dinheiro real)")
    try:
        pacote = carregar_dados(not a.sem_cache)
    except Exception as e:
        saida("FALHA ao baixar dados: %s" % e)
        sys.exit(1)
    escolhida, m_teste, aprovada = otimizar(pacote, a.treino, saida)
    saida("")
    saida("Limites: universo = maiores volumes de HOJE (vies de sobrevivencia); passado nao garante futuro.")
    with open(os.path.join(BASE, "otimizacao_frequencia_resultado.txt"), "w") as f:
        f.write("\n".join(linhas) + "\n")
    if escolhida:
        cfg = {"top_n": escolhida["universo"],
               "volume_minimo_usd": 20_000_000 if escolhida["universo"] <= 10 else 10_000_000,
               "max_posicoes": escolhida["max_posicoes"], "max_trades_dia": escolhida["max_dia"]}
        with open(os.path.join(BASE, "melhor_config_frequencia.json"), "w") as f:
            json.dump({"parametros": escolhida, "aprovada": bool(aprovada), "teste": m_teste,
                       "cfg_para_o_bot": cfg}, f, indent=2)


if __name__ == "__main__":
    main()
