#!/usr/bin/env python3
"""Relatorio do paper trading a partir de trades.csv."""

import csv
import os
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime

MIN_TRADES = 40
MIN_SEMANAS_SEGUIDAS = 2
MAX_DD_PCT = 15.0


def carregar(caminho):
    with open(caminho, newline="") as f:
        linhas = list(csv.DictReader(f))
    for r in linhas:
        r["pnl"] = float(r["pnl_usd"])
        r["capital"] = float(r["capital_apos"])
        r["taxas"] = float(r.get("taxas_usd") or 0)
        r["dt"] = datetime.strptime(r["horario_utc"], "%Y-%m-%d %H:%M:%S")
    linhas.sort(key=lambda r: r["dt"])
    return linhas


def maior_sequencia_perdas(pnls):
    maior = atual = 0
    for p in pnls:
        atual = atual + 1 if p < 0 else 0
        maior = max(maior, atual)
    return maior


def drawdown_max_pct(capital_inicial, capitais):
    pico, dd = capital_inicial, 0.0
    for c in [capital_inicial] + capitais:
        pico = max(pico, c)
        if pico > 0:
            dd = max(dd, (pico - c) / pico * 100)
    return dd


def semanas_seguidas_positivas(por_semana):
    """por_semana: dict (ano, semana_iso) -> pnl. Maior sequencia de semanas
    consecutivas (no calendario) com resultado positivo."""
    maior = atual = 0
    anterior = None
    for chave in sorted(por_semana):
        ano, sem = chave
        consecutiva = False
        if anterior is not None:
            a_ano, a_sem = anterior
            ultima_sem_ano = datetime.strptime(f"{a_ano}-12-28", "%Y-%m-%d").isocalendar()[1]
            consecutiva = (ano == a_ano and sem == a_sem + 1) or \
                          (ano == a_ano + 1 and sem == 1 and a_sem == ultima_sem_ano)
        if por_semana[chave] > 0:
            atual = atual + 1 if (consecutiva and atual > 0) else 1
        else:
            atual = 0
        maior = max(maior, atual)
        anterior = chave
    return maior


def relatorio(linhas):
    out = []
    p = out.append
    n = len(linhas)
    p("=" * 60)
    p("RELATORIO DE PAPER TRADING (simulacao)")
    p("=" * 60)
    if n == 0:
        p("Nenhuma operacao fechada ainda. Dados insuficientes para qualquer conclusao.")
        return "\n".join(out)

    pnls = [r["pnl"] for r in linhas]
    ganhos = [x for x in pnls if x > 0]
    perdas = [x for x in pnls if x < 0]
    liquido = sum(pnls)
    taxas = sum(r["taxas"] for r in linhas)
    bruto = liquido + taxas
    cap_ini = linhas[0]["capital"] - linhas[0]["pnl"]
    dd = drawdown_max_pct(cap_ini, [r["capital"] for r in linhas])
    fator = (sum(ganhos) / abs(sum(perdas))) if perdas else None

    p(f"Periodo: {linhas[0]['dt']} ate {linhas[-1]['dt']} (UTC)")
    p(f"Capital inicial: US$ {cap_ini:.2f} | capital final: US$ {linhas[-1]['capital']:.2f}")
    p(f"Operacoes: {n}")
    p(f"Taxa de acerto: {len(ganhos) / n * 100:.1f}% ({len(ganhos)} ganhos / {len(perdas)} perdas)")
    p(f"PnL antes das taxas: US$ {bruto:+.4f}  (slippage ja embutido nos precos)")
    p(f"Taxas pagas: US$ {taxas:.4f}")
    p(f"PnL liquido de taxas: US$ {liquido:+.4f} ({liquido / cap_ini * 100:+.2f}% do capital inicial)")
    p(f"Ganho medio: US$ {sum(ganhos) / len(ganhos):+.4f}" if ganhos else "Ganho medio: -")
    p(f"Perda media: US$ {sum(perdas) / len(perdas):+.4f}" if perdas else "Perda media: -")
    p(f"Fator de lucro: {fator:.2f}" if fator is not None else "Fator de lucro: - (sem perdas)")
    p(f"Maior sequencia de perdas: {maior_sequencia_perdas(pnls)}")
    p(f"Drawdown maximo: {dd:.2f}%")

    motivos = defaultdict(int)
    for r in linhas:
        motivos[r["motivo"]] += 1
    p("Saidas por motivo: " + ", ".join(f"{k}={v}" for k, v in sorted(motivos.items())))

    p("")
    p("Resultado por moeda:")
    por_moeda = defaultdict(list)
    for r in linhas:
        por_moeda[r["moeda"]].append(r["pnl"])
    for m, v in sorted(por_moeda.items(), key=lambda kv: sum(kv[1]), reverse=True):
        ac = sum(1 for x in v if x > 0) / len(v) * 100
        p(f"  {m:<10} {len(v):>4} ops | acerto {ac:5.1f}% | PnL US$ {sum(v):+.4f}")

    p("")
    p("Resultado por dia (UTC):")
    por_dia = OrderedDict()
    for r in linhas:
        por_dia.setdefault(r["dt"].strftime("%Y-%m-%d"), []).append(r["pnl"])
    for d, v in por_dia.items():
        p(f"  {d}  {len(v):>3} ops | PnL US$ {sum(v):+.4f}")

    por_semana = defaultdict(float)
    for r in linhas:
        iso = r["dt"].isocalendar()
        por_semana[(iso[0], iso[1])] += r["pnl"]
    p("")
    p("Resultado por semana ISO:")
    for (a, s), v in sorted(por_semana.items()):
        p(f"  {a}-S{s:02d}  PnL US$ {v:+.4f}")
    seq_sem = semanas_seguidas_positivas(por_semana)

    # ---------------- criterios ----------------
    p("")
    p("=" * 60)
    p("CRITERIOS PARA CONSIDERAR DINHEIRO REAL")
    p("=" * 60)
    insuficiente = []
    c1 = n >= MIN_TRADES
    p(f"[{'OK' if c1 else '--'}] Pelo menos {MIN_TRADES} operacoes: {n}")
    if not c1:
        insuficiente.append(f"faltam {MIN_TRADES - n} operacoes")

    n_sem = len(por_semana)
    if n_sem < MIN_SEMANAS_SEGUIDAS:
        c2 = False
        p(f"[--] {MIN_SEMANAS_SEGUIDAS} semanas seguidas positivas: dados insuficientes "
          f"(so {n_sem} semana(s) com operacoes)")
        insuficiente.append("menos de 2 semanas de historico")
    else:
        c2 = seq_sem >= MIN_SEMANAS_SEGUIDAS
        p(f"[{'OK' if c2 else 'X '}] {MIN_SEMANAS_SEGUIDAS} semanas seguidas positivas: "
          f"maior sequencia = {seq_sem}")

    c3 = dd < MAX_DD_PCT
    p(f"[{'OK' if c3 else 'X '}] Drawdown maximo abaixo de {MAX_DD_PCT:.0f}%: {dd:.2f}%")

    p("")
    if insuficiente:
        p("CONCLUSAO: DADOS INSUFICIENTES para concluir (" + "; ".join(insuficiente) + ").")
        p("Continue rodando o paper trading. Nao use dinheiro real ainda.")
    elif c1 and c2 and c3:
        p("CONCLUSAO: os criterios minimos FORAM atingidos nesta simulacao.")
        p("Isso NAO garante lucro futuro; resultado simulado pode diferir do real.")
    else:
        p("CONCLUSAO: os criterios NAO foram atingidos. Nao use dinheiro real.")
    return "\n".join(out)


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    caminho = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "trades.csv")
    if not os.path.exists(caminho):
        print(f"Arquivo {caminho} nao encontrado: nenhuma operacao fechada ainda.")
        print("Dados insuficientes para qualquer conclusao.")
        return
    print(relatorio(carregar(caminho)))


if __name__ == "__main__":
    main()
