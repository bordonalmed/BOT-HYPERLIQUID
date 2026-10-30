#!/usr/bin/env python3
"""
Bot de PAPER TRADING (simulacao) para perpetuos da Hyperliquid.

- Usa SOMENTE dados publicos (POST https://api.hyperliquid.xyz/info).
- NAO envia ordens, NAO assina nada, NAO usa carteira nem chave privada.
- Todo o dinheiro e simulado.
"""

import csv
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests

import execucao

# ---------------------------------------------------------------------------
# CONFIGURACAO
# ---------------------------------------------------------------------------
CFG = {
    # dados
    "api_url": "https://api.hyperliquid.xyz/info",
    "http_timeout_s": 15,
    "http_tentativas": 3,
    "http_espera_base_s": 2,          # espera crescente: 2s, 4s, ...
    # universo da estrategia
    "top_n": 10,
    "volume_minimo_usd": 20_000_000,
    "atualizar_universo_s": 3600,
    # radar de assimetria (so informativo; olha mais moedas que a estrategia)
    "radar_top_n": 30,
    "radar_volume_minimo_usd": 10_000_000,
    # estrategia: "tendencia" (EMA/RSI, APROVADA pelo otimizador) ou "rompimento"
    # (compressao + rompimento + stop movel). So troque para uma estrategia aprovada.
    "estrategia": "tendencia",
    # tendencia aprovada: 4h, stop 3xATR, alvo 3x, filtro EMA200, RSI larga,
    # max 3 ops/dia, saida apos 48 candles
    "intervalo": "4h",
    "intervalo_ms": 4 * 60 * 60 * 1000,
    "candles_historico": 600,         # janela longa para a EMA200 convergir
    "ema_rapida": 20,
    "ema_lenta": 50,
    "rsi_periodo": 14,
    "atr_periodo": 14,
    "lateral_min_pct": 0.001,         # |EMA20-EMA50|/preco < 0,1% => lateral
    "filtro_ema": 200,                # so LONG acima e SHORT abaixo desta EMA (None desliga)
    "rsi_long": (50, 72),
    "rsi_short": (28, 50),
    "candles_volume_prioridade": 20,
    # parametros da estrategia de rompimento (usados quando estrategia = "rompimento")
    "rompimento": {
        "lookback": 20,               # rompe a maxima/minima dos ultimos N candles
        "squeeze_fator": 1.2,         # compressao: largura das Bandas de Bollinger perto da minima
        "squeeze_janela": 120,        #   dos ultimos 120 candles (None desliga)
        "squeeze_recente": 6,         #   ocorrida nos ultimos 6 candles
        "vol_mult": 1.5,              # volume do candle >= 1,5x a media de 20 (None desliga)
    },
    # risco
    "capital_inicial": 50.0,
    "risco_por_trade": 0.015,
    "stop_atr_mult": 3.0,
    "alvo_mult_stop": 3.0,            # None = sem alvo fixo (deixa o lucro correr)
    "trailing_atr_mult": None,        # stop movel: maior preco desde a entrada - N x ATR (None desliga)
    "tempo_max_min": 48 * 240,        # 48 candles de 4h = 8 dias (None desliga)
    "alavancagem_max": 4.0,           # teto de nocional = 4x o capital (o risco continua 1,5%)
    "nocional_minimo": 10.0,
    "max_posicoes": 2,
    "max_trades_dia": 3,
    "perda_diaria_max": 0.05,
    "taxa_por_lado": 0.00045,
    "slippage_por_lado": 0.0003,
    "slippage_pequenas": 0.001,       # moedas com volume 24h abaixo de volume_grande_usd
    "volume_grande_usd": 100_000_000,
    # operacao
    "intervalo_conferencia": "5m",    # candles usados para conferir stop/alvo apos o bot ficar parado
    "loop_s": 30,
    "heartbeat_ciclos": 20,           # loga um resumo a cada N ciclos (~10 min)
    "arquivo_estado": "state.json",
    "arquivo_trades": "trades.csv",
    "arquivo_kill": "KILL",
    "arquivo_kill_fechar": "KILL_FECHAR",  # modo testnet/real: fecha tudo a mercado e encerra
    # execucao real (MODO=testnet ou real no .env)
    "slippage_max_ordem": 0.01,       # ordem a mercado nao executa se o preco andar mais que 1%
    "capital_max_real": 60.0,         # o bot dimensiona como se a conta tivesse no maximo isto
}

TRADES_CAMPOS = [
    "horario_utc", "moeda", "lado", "entrada", "saida", "tamanho", "motivo",
    "pnl_usd", "capital_apos", "taxas_usd", "horario_entrada_utc",
]

log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def agora_ms(now_fn=time.time):
    return int(now_fn() * 1000)


def dia_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def iso_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# INDICADORES (python puro)
# ---------------------------------------------------------------------------
def ema(valores, periodo):
    """EMA semeada com a media simples dos primeiros `periodo` valores.
    Retorna lista do mesmo tamanho, com None antes de haver dados suficientes."""
    out = [None] * len(valores)
    if len(valores) < periodo:
        return out
    alpha = 2.0 / (periodo + 1)
    atual = sum(valores[:periodo]) / periodo
    out[periodo - 1] = atual
    for i in range(periodo, len(valores)):
        atual = valores[i] * alpha + atual * (1 - alpha)
        out[i] = atual
    return out


def rsi(closes, periodo=14):
    """RSI com suavizacao de Wilder."""
    out = [None] * len(closes)
    if len(closes) <= periodo:
        return out
    ganhos, perdas = [], []
    for i in range(1, periodo + 1):
        d = closes[i] - closes[i - 1]
        ganhos.append(max(d, 0.0))
        perdas.append(max(-d, 0.0))
    ag = sum(ganhos) / periodo
    ap = sum(perdas) / periodo

    def _calc(g, p):
        if p == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1 + g / p)

    out[periodo] = _calc(ag, ap)
    for i in range(periodo + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (periodo - 1) + max(d, 0.0)) / periodo
        ap = (ap * (periodo - 1) + max(-d, 0.0)) / periodo
        out[i] = _calc(ag, ap)
    return out


def atr(highs, lows, closes, periodo=14):
    """ATR com suavizacao de Wilder. Primeiro valor = media dos TR 1..periodo."""
    n = len(closes)
    out = [None] * n
    if n <= periodo:
        return out
    tr = [None]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    atual = sum(tr[1:periodo + 1]) / periodo
    out[periodo] = atual
    for i in range(periodo + 1, n):
        atual = (atual * (periodo - 1) + tr[i]) / periodo
        out[i] = atual
    return out


def largura_bollinger(closes, periodo=20):
    """(banda superior - inferior) / media, com 2 desvios-padrao. None no inicio."""
    n = len(closes)
    out = [None] * n
    s = s2 = 0.0
    for i, x in enumerate(closes):
        s += x
        s2 += x * x
        if i >= periodo:
            y = closes[i - periodo]
            s -= y
            s2 -= y * y
        if i >= periodo - 1:
            media = s / periodo
            var = max(s2 / periodo - media * media, 0.0)
            out[i] = 4 * math.sqrt(var) / media if media > 0 else None
    return out


# ---------------------------------------------------------------------------
# ESTRATEGIA
# ---------------------------------------------------------------------------
def indicadores_rompimento(candles, cfg=CFG):
    """Series usadas pela estrategia de rompimento (mesmo codigo no bot e no otimizador)."""
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    return {
        "c": closes, "h": highs, "l": lows, "v": [c["v"] for c in candles],
        "atr": atr(highs, lows, closes, cfg["atr_periodo"]),
        "ef": ema(closes, cfg["filtro_ema"]) if cfg.get("filtro_ema") else None,
        "bbw": largura_bollinger(closes, 20),
    }


def sinal_rompimento_em(ind, i, p, filtro_ema=None):
    """Sinal no candle i. p = parametros de rompimento. Retorna dict ou None.
    LONG: fecha acima da maxima dos ultimos N candles; SHORT: abaixo da minima."""
    lb = p["lookback"]
    jan = p.get("squeeze_janela") or 0
    if i < max(lb, jan, 20) + 20:
        return None
    a = ind["atr"][i]
    if a is None or a <= 0:
        return None
    c = ind["c"][i]
    maxima = max(ind["h"][i - lb:i])
    minima = min(ind["l"][i - lb:i])
    if c > maxima:
        lado = "long"
    elif c < minima:
        lado = "short"
    else:
        return None
    if p.get("squeeze_fator"):
        bbw = ind["bbw"]
        rec = bbw[i - p["squeeze_recente"]:i]
        jn = bbw[i - jan:i]
        if None in rec or None in jn or min(rec) > p["squeeze_fator"] * min(jn):
            return None
    if p.get("vol_mult"):
        media = sum(ind["v"][i - 20:i]) / 20.0
        if media <= 0 or ind["v"][i] < p["vol_mult"] * media:
            return None
    if filtro_ema:
        ef = ind["ef"][i] if ind.get("ef") else None
        if ef is None or (lado == "long" and c <= ef) or (lado == "short" and c >= ef):
            return None
    return {"lado": lado, "atr": a, "close": c, "nivel": maxima if lado == "long" else minima}


def avaliar_sinal(candles, cfg=CFG):
    if cfg.get("estrategia") == "rompimento":
        if len(candles) < 60:
            return None
        ind = indicadores_rompimento(candles, cfg)
        return sinal_rompimento_em(ind, len(candles) - 1, cfg["rompimento"], cfg.get("filtro_ema"))
    return avaliar_sinal_tendencia(candles, cfg)


def avaliar_sinal_tendencia(candles, cfg=CFG):
    """Recebe candles fechados (dicts com o,h,l,c,v float) e devolve
    dict com 'lado' ('long'/'short') e indicadores, ou None."""
    if len(candles) < cfg["ema_lenta"] + 10:
        return None
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    e_r = ema(closes, cfg["ema_rapida"])[-1]
    e_l = ema(closes, cfg["ema_lenta"])[-1]
    r = rsi(closes, cfg["rsi_periodo"])[-1]
    a = atr(highs, lows, closes, cfg["atr_periodo"])[-1]
    preco = closes[-1]
    if None in (e_r, e_l, r, a) or preco <= 0 or a <= 0:
        return None
    e_f = None
    if cfg.get("filtro_ema"):
        e_f = ema(closes, cfg["filtro_ema"])[-1]
        if e_f is None:
            return None
    info = {"ema20": e_r, "ema50": e_l, "rsi": r, "atr": a, "close": preco, "ema_filtro": e_f}
    if abs(e_r - e_l) / preco < cfg["lateral_min_pct"]:
        return None
    lo, hi = cfg["rsi_long"]
    if e_r > e_l and preco > e_r and lo <= r <= hi and (e_f is None or preco > e_f):
        return dict(info, lado="long")
    lo, hi = cfg["rsi_short"]
    if e_r < e_l and preco < e_r and lo <= r <= hi and (e_f is None or preco < e_f):
        return dict(info, lado="short")
    return None


def volume_usd_recente(candles, n=20):
    return sum(c["c"] * c["v"] for c in candles[-n:])


# ---------------------------------------------------------------------------
# EXECUCAO SIMULADA / RISCO
# ---------------------------------------------------------------------------
def preco_execucao(ref, lado, acao, slip):
    """Aplica slippage sempre contra o trader."""
    compra = (lado == "long" and acao == "entrada") or (lado == "short" and acao == "saida")
    return ref * (1 + slip) if compra else ref * (1 - slip)


def calcular_tamanho(capital, entrada, dist_stop, cfg=CFG):
    """Retorna (tamanho, nocional) ou None se abaixo do nocional minimo."""
    if dist_stop <= 0 or entrada <= 0 or capital <= 0:
        return None
    risco_usd = capital * cfg["risco_por_trade"]
    tamanho = risco_usd / dist_stop
    nocional_max = capital * cfg["alavancagem_max"]
    if tamanho * entrada > nocional_max:
        tamanho = nocional_max / entrada
    nocional = tamanho * entrada
    if nocional < cfg["nocional_minimo"]:
        return None
    return tamanho, nocional


def pnl_liquido(lado, entrada, saida, tamanho, taxa):
    bruto = (saida - entrada) * tamanho if lado == "long" else (entrada - saida) * tamanho
    taxas = (entrada * tamanho + saida * tamanho) * taxa
    return bruto - taxas, taxas


def verificar_saida_candle(pos, c):
    """Confere um candle (maxima/minima). Se tocar stop e alvo no mesmo candle,
    conta STOP (conservador, igual ao backtest). Retorna (motivo, preco) ou None."""
    alvo = pos.get("alvo")
    if pos["lado"] == "long":
        if c["l"] <= pos["stop"]:
            return "stop", min(c["o"], pos["stop"])
        if alvo is not None and c["h"] >= alvo:
            return "alvo", alvo
    else:
        if c["h"] >= pos["stop"]:
            return "stop", max(c["o"], pos["stop"])
        if alvo is not None and c["l"] <= alvo:
            return "alvo", alvo
    return None


def atualizar_trailing(pos, alta, baixa):
    """Stop movel: acompanha o melhor preco desde a entrada a N x ATR de distancia.
    So aperta o stop, nunca afrouxa. Chamar DEPOIS de conferir a saida."""
    mult = pos.get("trailing")
    if not mult:
        return
    dist = mult * pos["atr"]
    if pos["lado"] == "long":
        pos["extremo"] = max(pos.get("extremo", pos["entrada"]), alta)
        pos["stop"] = max(pos["stop"], pos["extremo"] - dist)
    else:
        pos["extremo"] = min(pos.get("extremo", pos["entrada"]), baixa)
        pos["stop"] = min(pos["stop"], pos["extremo"] + dist)


def verificar_saida(pos, mid, now_ms, cfg=CFG):
    """Retorna (motivo, preco_referencia) ou None."""
    alvo = pos.get("alvo")
    if pos["lado"] == "long":
        if mid <= pos["stop"]:
            return "stop", mid          # preenche no preco atual (pior que o stop)
        if alvo is not None and mid >= alvo:
            return "alvo", alvo         # preenche no alvo (conservador)
    else:
        if mid >= pos["stop"]:
            return "stop", mid
        if alvo is not None and mid <= alvo:
            return "alvo", alvo
    if cfg.get("tempo_max_min") and now_ms - pos["entrada_ms"] >= cfg["tempo_max_min"] * 60_000:
        return "tempo", mid
    return None


def slippage_da_moeda(volume_24h, cfg=CFG):
    """Moedas com pouco volume tem book mais fino: slippage maior na simulacao."""
    if volume_24h is not None and volume_24h < cfg.get("volume_grande_usd", 0):
        return cfg.get("slippage_pequenas", cfg["slippage_por_lado"])
    return cfg["slippage_por_lado"]


def montar_posicao(moeda, lado, entrada, tamanho, atr_val, t_ms, slip, cfg=CFG):
    dist = cfg["stop_atr_mult"] * atr_val
    sinal = 1 if lado == "long" else -1
    alvo = None
    if cfg.get("alvo_mult_stop"):
        alvo = entrada + sinal * cfg["alvo_mult_stop"] * dist
    return {"moeda": moeda, "lado": lado, "entrada": entrada, "tamanho": tamanho,
            "stop": entrada - sinal * dist, "alvo": alvo, "entrada_ms": t_ms,
            "atr": atr_val, "trailing": cfg.get("trailing_atr_mult"), "extremo": entrada,
            "slip": slip}


# ---------------------------------------------------------------------------
# RADAR DE ASSIMETRIA (informativo: NAO e a estrategia validada)
# ---------------------------------------------------------------------------
def _lim(x):
    return max(0.0, min(1.0, x))


def metricas_radar(cs, cs_btc=None, ctx=None):
    """Nota 0-100 de 'potencial de movimento grande' de uma moeda.
    - compressao: largura das Bandas de Bollinger perto da minima de 120 candles
    - volume: media dos ultimos 6 candles vs. 30 anteriores
    - forca relativa: retorno de 30 candles contra o BTC
    - proximidade do rompimento: distancia ate a maxima/minima de 20 candles
    - funding extremo: muita gente posicionada para um lado (risco de squeeze)"""
    if len(cs) < 160:
        return None
    closes = [c["c"] for c in cs]
    bbw = largura_bollinger(closes, 20)
    jan = [x for x in bbw[-120:] if x]
    if not jan or not bbw[-1]:
        return None
    compressao = bbw[-1] / min(jan)                       # 1.0 = mais comprimido em 120 candles
    v = [c["v"] * c["c"] for c in cs]
    vol_ratio = (sum(v[-6:]) / 6) / max(sum(v[-36:-6]) / 30, 1e-9)
    ret = closes[-1] / closes[-31] - 1
    rs = ret
    if cs_btc and len(cs_btc) >= 31:
        rs = ret - (cs_btc[-1]["c"] / cs_btc[-31]["c"] - 1)
    maxima = max(c["h"] for c in cs[-21:-1])
    minima = min(c["l"] for c in cs[-21:-1])
    preco = closes[-1]
    dist_max = maxima / preco - 1                          # quanto falta para romper para cima
    dist_min = 1 - minima / preco                          # quanto falta para romper para baixo
    e200 = ema(closes, 200)[-1]
    tendencia = (preco / e200 - 1) if e200 else 0.0
    funding_apr = None
    if ctx and ctx.get("funding") is not None:
        funding_apr = ctx["funding"] * 24 * 365           # funding da Hyperliquid e por hora
    lado = "-"
    if tendencia > 0 and rs > 0:
        lado = "long"
    elif tendencia < 0 and rs < 0:
        lado = "short"
    prox = dist_max if lado != "short" else dist_min
    nota = 100 * (0.30 * _lim(2.0 - compressao)
                  + 0.20 * _lim((vol_ratio - 1) / 1.5)
                  + 0.20 * _lim(abs(rs) / 0.20)
                  + 0.20 * _lim(1 - max(prox, 0) / 0.05)
                  + 0.10 * _lim(abs(funding_apr or 0) / 0.5))
    return {"nota": round(nota, 1), "lado": lado, "compressao": round(compressao, 2),
            "vol_ratio": round(vol_ratio, 2), "rs_pct": round(rs * 100, 1),
            "tendencia_pct": round(tendencia * 100, 1), "dist_rompimento_pct": round(prox * 100, 2),
            "funding_apr_pct": None if funding_apr is None else round(funding_apr * 100, 1),
            "preco": preco}


# ---------------------------------------------------------------------------
# API PUBLICA (somente leitura)
# ---------------------------------------------------------------------------
def parse_universo(raw, cfg=CFG):
    meta, ctxs = raw[0], raw[1]
    lista = []
    for i, ativo in enumerate(meta["universe"]):
        if ativo.get("isDelisted"):
            continue
        if i >= len(ctxs):
            break
        try:
            vol = float(ctxs[i].get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        if vol >= cfg["volume_minimo_usd"]:
            lista.append((ativo["name"], vol))
    lista.sort(key=lambda x: x[1], reverse=True)
    return lista[:cfg["top_n"]]


def parse_ctxs(raw):
    """{moeda: {funding, oi, volume}} a partir do metaAndAssetCtxs."""
    meta, ctxs = raw[0], raw[1]
    out = {}
    for i, ativo in enumerate(meta["universe"]):
        if i >= len(ctxs):
            break
        c = ctxs[i]
        try:
            out[ativo["name"]] = {"funding": float(c.get("funding") or 0),
                                  "oi": float(c.get("openInterest") or 0),
                                  "volume": float(c.get("dayNtlVlm") or 0)}
        except (TypeError, ValueError):
            continue
    return out


def parse_mids(raw):
    out = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def parse_candles(raw, now_ms):
    """Converte e mantem apenas candles fechados (T < agora), ordenados por t."""
    out = []
    for c in raw:
        try:
            item = {"t": int(c["t"]), "T": int(c["T"]), "o": float(c["o"]),
                    "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"]),
                    "v": float(c["v"])}
        except (KeyError, TypeError, ValueError):
            continue
        if item["T"] < now_ms:
            out.append(item)
    out.sort(key=lambda x: x["t"])
    return out


class HyperliquidAPI:
    def __init__(self, cfg=CFG):
        self.cfg = cfg
        self.sess = requests.Session()

    def _post(self, payload):
        ultimo_erro = None
        for tentativa in range(1, self.cfg["http_tentativas"] + 1):
            try:
                r = self.sess.post(self.cfg["api_url"], json=payload,
                                   timeout=self.cfg["http_timeout_s"])
                r.raise_for_status()
                return r.json()
            except Exception as e:  # rede, http, json
                ultimo_erro = e
                if tentativa < self.cfg["http_tentativas"]:
                    time.sleep(self.cfg["http_espera_base_s"] * (2 ** (tentativa - 1)))
        raise RuntimeError(f"falha na API ({payload.get('type')}): {ultimo_erro}")

    def meta_and_ctxs(self):
        return self._post({"type": "metaAndAssetCtxs"})

    def all_mids(self):
        return self._post({"type": "allMids"})

    def candles(self, coin, interval, start_ms, end_ms):
        return self._post({"type": "candleSnapshot",
                           "req": {"coin": coin, "interval": interval,
                                   "startTime": start_ms, "endTime": end_ms}})


# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------
class PaperBot:
    def __init__(self, cfg=CFG, api=None, base_dir=".", now_fn=time.time):
        self.cfg = cfg
        self.api = api or HyperliquidAPI(cfg)
        self.base = base_dir
        self.now_fn = now_fn
        self.path_estado = os.path.join(base_dir, cfg["arquivo_estado"])
        self.path_trades = os.path.join(base_dir, cfg["arquivo_trades"])
        self.path_kill = os.path.join(base_dir, cfg["arquivo_kill"])
        self.universo = []           # [(moeda, volume)] da estrategia
        self.universo_amplo = []     # [(moeda, volume)] do radar
        self.ctxs = {}               # {moeda: {funding, oi, volume}}
        self.cache_candles = {}      # {moeda: candles fechados} do ultimo candle avaliado
        self.universo_ms = 0
        self.mids = {}
        self.ciclos = 0
        self.parar = False
        self.estado = self._carregar_estado()

    # ---------- estado ----------
    def _estado_novo(self):
        return {"capital": self.cfg["capital_inicial"], "posicoes": [],
                "dia": None, "equity_inicio_dia": None, "trades_hoje": 0,
                "pausado": False, "ultimo_candle": {}}

    def _carregar_estado(self):
        if os.path.exists(self.path_estado):
            try:
                with open(self.path_estado) as f:
                    est = json.load(f)
                base = self._estado_novo()
                base.update(est)
                log.info("Estado retomado: capital %.2f USD, %d posicao(oes) aberta(s)",
                         base["capital"], len(base["posicoes"]))
                return base
            except Exception as e:
                log.error("Erro ao ler %s (%s); iniciando estado novo", self.path_estado, e)
        return self._estado_novo()

    def salvar_estado(self):
        tmp = self.path_estado + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.estado, f, indent=2)
        # no Windows o replace falha se outro programa (ex.: dashboard) estiver lendo
        for tentativa in range(10):
            try:
                os.replace(tmp, self.path_estado)
                return
            except PermissionError:
                if tentativa == 9:
                    raise
                time.sleep(0.1)

    def _registrar_trade(self, linha):
        novo = not os.path.exists(self.path_trades)
        with open(self.path_trades, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADES_CAMPOS)
            if novo:
                w.writeheader()
            w.writerow(linha)

    # ---------- contas ----------
    def pnl_aberto(self, pos):
        """PnL liquido estimado se a posicao fosse fechada agora (com custos)."""
        mid = self.mids.get(pos["moeda"])
        if mid is None:
            mid = pos.get("ultimo_mid", pos["entrada"])
        saida = preco_execucao(mid, pos["lado"], "saida", pos.get("slip", self.cfg["slippage_por_lado"]))
        pnl, _ = pnl_liquido(pos["lado"], pos["entrada"], saida, pos["tamanho"],
                             self.cfg["taxa_por_lado"])
        return pnl

    def equity(self):
        return self.estado["capital"] + sum(self.pnl_aberto(p) for p in self.estado["posicoes"])

    # ---------- abrir / fechar ----------
    def _volume(self, moeda):
        info = self.ctxs.get(moeda)
        if info:
            return info["volume"]
        for m, v in self.universo_amplo or self.universo:
            if m == moeda:
                return v
        return None

    def abrir(self, moeda, lado, mid, atr_val, now_ms):
        cfg = self.cfg
        slip = slippage_da_moeda(self._volume(moeda), cfg)
        entrada = preco_execucao(mid, lado, "entrada", slip)
        dist = cfg["stop_atr_mult"] * atr_val
        tam = calcular_tamanho(self.estado["capital"], entrada, dist, cfg)
        if tam is None:
            log.info("IGNORADA %s %s: nocional abaixo de US$ %.0f", moeda, lado.upper(),
                     cfg["nocional_minimo"])
            return False
        tamanho, nocional = tam
        pos = montar_posicao(moeda, lado, entrada, tamanho, atr_val, now_ms, slip, cfg)
        pos["ultimo_mid"] = mid
        pos["conferido_ms"] = now_ms
        self.estado["posicoes"].append(pos)
        self.estado["trades_hoje"] += 1
        log.info("ABRIU %s %s | entrada %.6g | tamanho %.6g | nocional US$ %.2f (%.1fx) | stop %.6g | alvo %s%s",
                 lado.upper(), moeda, entrada, tamanho, nocional, nocional / self.estado["capital"],
                 pos["stop"], "%.6g" % pos["alvo"] if pos["alvo"] is not None else "livre",
                 " | stop movel %.1fxATR" % pos["trailing"] if pos["trailing"] else "")
        return True

    def fechar(self, pos, ref, motivo, now_ms):
        cfg = self.cfg
        saida = preco_execucao(ref, pos["lado"], "saida", pos.get("slip", cfg["slippage_por_lado"]))
        pnl, taxas = pnl_liquido(pos["lado"], pos["entrada"], saida, pos["tamanho"],
                                 cfg["taxa_por_lado"])
        self.estado["capital"] += pnl
        self.estado["posicoes"].remove(pos)
        self._registrar_trade({
            "horario_utc": iso_utc(now_ms), "moeda": pos["moeda"], "lado": pos["lado"],
            "entrada": f"{pos['entrada']:.8g}", "saida": f"{saida:.8g}",
            "tamanho": f"{pos['tamanho']:.8g}", "motivo": motivo,
            "pnl_usd": f"{pnl:.6f}", "capital_apos": f"{self.estado['capital']:.6f}",
            "taxas_usd": f"{taxas:.6f}", "horario_entrada_utc": iso_utc(pos["entrada_ms"]),
        })
        log.info("FECHOU %s %s | motivo %s | entrada %.6g | saida %.6g | PnL US$ %+.4f | capital US$ %.4f",
                 pos["lado"].upper(), pos["moeda"], motivo, pos["entrada"], saida, pnl,
                 self.estado["capital"])
        return pnl

    # ---------- etapas do ciclo ----------
    def _checar_novo_dia(self, now_ms):
        hoje = dia_utc(now_ms)
        if self.estado["dia"] != hoje:
            self.estado["dia"] = hoje
            self.estado["trades_hoje"] = 0
            self.estado["pausado"] = False
            self.estado["equity_inicio_dia"] = self.equity()
            log.info("NOVO DIA %s (UTC) | capital US$ %.4f | equity US$ %.4f",
                     hoje, self.estado["capital"], self.estado["equity_inicio_dia"])

    def _atualizar_universo(self, now_ms):
        if self.universo and now_ms - self.universo_ms < self.cfg["atualizar_universo_s"] * 1000:
            return
        try:
            raw = self.api.meta_and_ctxs()
            cfg = self.cfg
            self.universo_amplo = parse_universo(raw, dict(
                cfg, top_n=max(cfg.get("radar_top_n", 0), cfg["top_n"]),
                volume_minimo_usd=min(cfg.get("radar_volume_minimo_usd", cfg["volume_minimo_usd"]),
                                      cfg["volume_minimo_usd"])))
            self.universo = [(m, v) for m, v in self.universo_amplo
                             if v >= cfg["volume_minimo_usd"]][:cfg["top_n"]]
            self.ctxs = parse_ctxs(raw)
            self.universo_ms = now_ms
            self.estado["universo"] = [[m, v] for m, v in self.universo]
            log.info("UNIVERSO atualizado: %s",
                     ", ".join(f"{m} ({v / 1e6:.0f}M)" for m, v in self.universo))
        except Exception as e:
            log.error("Erro ao atualizar universo: %s (mantendo lista anterior)", e)

    def _conferir_periodo_parado(self, pos, now_ms):
        """Se o bot ficou parado (notebook dormindo etc.), confere nos candles de 5m
        se stop ou alvo foram tocados nesse intervalo. Retorna True se fechou."""
        desde = pos.get("conferido_ms", pos["entrada_ms"])
        if now_ms - desde < 3 * 60_000:
            return False
        try:
            cs = parse_candles(self.api.candles(pos["moeda"], self.cfg["intervalo_conferencia"],
                                                desde, now_ms), now_ms)
        except Exception as e:
            log.error("Erro ao conferir candles de %s apos pausa: %s (usando so o preco atual)",
                      pos["moeda"], e)
            return False
        for c in cs:
            if c["T"] < desde:
                continue
            saida = verificar_saida_candle(pos, c)
            if saida:
                log.info("CONFERENCIA %s: %s tocado as %s UTC enquanto o bot estava parado",
                         pos["moeda"], saida[0], iso_utc(c["T"]))
                self.fechar(pos, saida[1], saida[0], c["T"])
                return True
            atualizar_trailing(pos, c["h"], c["l"])
        if cs:
            pos["conferido_ms"] = cs[-1]["T"] + 1
        return False

    def _gerir_posicoes(self, now_ms):
        for pos in list(self.estado["posicoes"]):
            if self._conferir_periodo_parado(pos, now_ms):
                continue
            mid = self.mids.get(pos["moeda"])
            if mid is None:
                continue
            pos["ultimo_mid"] = mid
            saida = verificar_saida(pos, mid, now_ms, self.cfg)
            if saida:
                self.fechar(pos, saida[1], saida[0], now_ms)
            else:
                atualizar_trailing(pos, mid, mid)
                pos["conferido_ms"] = now_ms

    def _checar_limite_diario(self, now_ms):
        if self.estado["pausado"] or self.estado["equity_inicio_dia"] is None:
            return
        eq = self.equity()
        limite = self.estado["equity_inicio_dia"] * (1 - self.cfg["perda_diaria_max"])
        if eq <= limite:
            log.warning("LIMITE DIARIO atingido: equity US$ %.4f <= US$ %.4f. Fechando tudo e pausando ate o proximo dia UTC.",
                        eq, limite)
            for pos in list(self.estado["posicoes"]):
                ref = self.mids.get(pos["moeda"], pos.get("ultimo_mid", pos["entrada"]))
                self.fechar(pos, ref, "limite_diario", now_ms)
            self.estado["pausado"] = True

    def _pode_abrir(self):
        e = self.estado
        return (not e["pausado"] and len(e["posicoes"]) < self.cfg["max_posicoes"]
                and e["trades_hoje"] < self.cfg["max_trades_dia"])

    def _ultimo_fechado(self, now_ms):
        iv = self.cfg["intervalo_ms"]
        return (now_ms // iv) * iv - iv            # abertura do ultimo candle fechado

    def _buscar_candles(self, moeda, now_ms):
        """Candles fechados da moeda, reaproveitando o que ja foi baixado neste candle."""
        ult = self._ultimo_fechado(now_ms)
        cs = self.cache_candles.get(moeda)
        if cs and cs[-1]["t"] >= ult:
            return cs
        inicio = now_ms - self.cfg["candles_historico"] * self.cfg["intervalo_ms"]
        cs = parse_candles(self.api.candles(moeda, self.cfg["intervalo"], inicio, now_ms), now_ms)
        if cs:
            self.cache_candles[moeda] = cs
        return cs

    def _atualizar_radar(self, now_ms):
        """Uma vez por candle novo: nota de assimetria de todas as moedas liquidas."""
        ult = self._ultimo_fechado(now_ms)
        if not self.universo_amplo or self.estado.get("radar_candle", 0) >= ult:
            return
        moedas = [m for m, _ in self.universo_amplo]
        btc = None
        if "BTC" in moedas:
            moedas.remove("BTC")
            moedas.insert(0, "BTC")
        lista = []
        for moeda in moedas:
            try:
                cs = self._buscar_candles(moeda, now_ms)
            except Exception as e:
                log.error("Radar: erro ao buscar candles de %s: %s", moeda, e)
                continue
            if moeda == "BTC":
                btc = cs
            m = metricas_radar(cs, btc, self.ctxs.get(moeda)) if cs else None
            if m:
                m["moeda"] = moeda
                m["volume"] = self._volume(moeda)
                lista.append(m)
        if not lista:
            return
        lista.sort(key=lambda x: x["nota"], reverse=True)
        self.estado["radar"] = lista
        self.estado["radar_candle"] = ult
        self.estado["radar_ms"] = now_ms
        log.info("RADAR atualizado (%d moedas). Top 5: %s", len(lista),
                 ", ".join("%s %.0f %s" % (x["moeda"], x["nota"], x["lado"]) for x in lista[:5]))

    def _procurar_entradas(self, now_ms):
        if not self._pode_abrir():
            return
        ultimo_fechado = self._ultimo_fechado(now_ms)
        abertas = {p["moeda"] for p in self.estado["posicoes"]}
        candidatos = []
        for moeda, _ in self.universo:
            if moeda in abertas:
                continue
            if self.estado["ultimo_candle"].get(moeda, 0) >= ultimo_fechado:
                continue
            try:
                cs = self._buscar_candles(moeda, now_ms)
            except Exception as e:
                log.error("Erro ao buscar candles de %s: %s", moeda, e)
                continue
            if not cs:
                continue
            t_ult = cs[-1]["t"]
            if t_ult <= self.estado["ultimo_candle"].get(moeda, 0):
                continue
            self.estado["ultimo_candle"][moeda] = t_ult
            sinal = avaliar_sinal(cs, self.cfg)
            if sinal:
                sinal["moeda"] = moeda
                sinal["vol20"] = volume_usd_recente(cs, self.cfg["candles_volume_prioridade"])
                candidatos.append(sinal)
                if "rsi" in sinal:
                    det = "EMA20 %.6g EMA50 %.6g RSI %.1f" % (sinal["ema20"], sinal["ema50"], sinal["rsi"])
                else:
                    det = "rompeu %.6g" % sinal["nivel"]
                log.info("SINAL %s %s (%s) | close %.6g %s ATR %.6g", sinal["lado"].upper(), moeda,
                         self.cfg.get("estrategia", "tendencia"), sinal["close"], det, sinal["atr"])
        candidatos.sort(key=lambda s: s["vol20"], reverse=True)
        for s in candidatos:
            if not self._pode_abrir():
                break
            mid = self.mids.get(s["moeda"])
            if mid is None:
                log.warning("Sem preco atual para %s; sinal descartado", s["moeda"])
                continue
            self.abrir(s["moeda"], s["lado"], mid, s["atr"], now_ms)

    def ciclo(self):
        now_ms = agora_ms(self.now_fn)
        try:
            self.mids = parse_mids(self.api.all_mids())
        except Exception as e:
            log.error("Erro ao buscar precos: %s (tentando no proximo ciclo)", e)
            return False
        self._checar_novo_dia(now_ms)
        self._atualizar_universo(now_ms)
        self._gerir_posicoes(now_ms)
        self._checar_limite_diario(now_ms)
        try:
            self._atualizar_radar(now_ms)
        except Exception as e:
            log.error("Erro no radar: %s", e)
        self._procurar_entradas(now_ms)
        self.salvar_estado()
        return True

    # ---------- loop ----------
    def _kill(self):
        return os.path.exists(self.path_kill)

    def rodar(self):
        modo = getattr(self, "modo", "paper")
        rotulo = {"paper": "PAPER TRADING (simulacao, sem dinheiro real)",
                  "testnet": "TESTNET (ordens reais com dinheiro de TESTE)",
                  "real": "REAL (ORDENS COM DINHEIRO DE VERDADE)"}[modo]
        log.info("Bot iniciado em modo %s | capital US$ %.4f | estrategia %s %s", rotulo,
                 self.estado["capital"], self.cfg.get("estrategia", "tendencia"), self.cfg["intervalo"])
        while not self.parar:
            if self._kill():
                log.info("Arquivo KILL encontrado. Encerrando de forma limpa.")
                break
            self.ciclos += 1
            try:
                ok = self.ciclo()
            except Exception as e:  # nunca cair
                log.exception("Erro inesperado no ciclo %d: %s", self.ciclos, e)
                ok = False
            self.estado["ultimo_ciclo_ms"] = agora_ms(self.now_fn)
            self.estado["ultimo_ciclo_ok"] = ok
            try:
                self.salvar_estado()
            except Exception as e:
                log.error("Erro ao salvar estado: %s", e)
            if self.ciclos == 1 or self.ciclos % self.cfg["heartbeat_ciclos"] == 0:
                log.info("CICLO %d %s | capital US$ %.4f | equity US$ %.4f | posicoes %d | trades hoje %d | pausado %s",
                         self.ciclos, "concluido" if ok else "com erro", self.estado["capital"],
                         self.equity(), len(self.estado["posicoes"]),
                         self.estado["trades_hoje"], self.estado["pausado"])
            for _ in range(self.cfg["loop_s"]):
                if self.parar or self._kill():
                    break
                time.sleep(1)
        try:
            self.salvar_estado()
        except Exception as e:
            log.error("Erro ao salvar estado na saida: %s", e)
        log.info("Bot encerrado. Estado salvo em %s", self.path_estado)


# ---------------------------------------------------------------------------
# BOT COM ORDENS NA CORRETORA (testnet ou real)
# ---------------------------------------------------------------------------
class LiveBot(PaperBot):
    """Mesma estrategia, limites e radar do PaperBot, mas executando na Hyperliquid.
    - Stop e alvo ficam como ordens na corretora (valem com o notebook desligado).
    - A corretora e a fonte da verdade: saldo e posicoes sao lidos de la a cada ciclo.
    - Sinais e universo usam os dados da rede principal (testnet tem pouco volume)."""

    def __init__(self, cfg, executor, modo, api=None, base_dir=".", now_fn=time.time):
        if cfg.get("trailing_atr_mult"):
            raise ValueError("stop movel ainda nao e suportado no modo testnet/real")
        if not cfg.get("alvo_mult_stop"):
            raise ValueError("modo testnet/real exige alvo fixo (alvo_mult_stop)")
        cfg = dict(cfg, arquivo_estado="state_%s.json" % modo, arquivo_trades="trades_%s.csv" % modo)
        self.ex = executor
        self.modo = modo
        self.conta_valor = None
        self.livre = 0.0
        self.avisadas = set()
        super().__init__(cfg, api, base_dir, now_fn)
        self.path_kill_fechar = os.path.join(base_dir, cfg["arquivo_kill_fechar"])

    # ---------- contas ----------
    def equity(self):
        return self.conta_valor if self.conta_valor is not None else self.estado["capital"]

    def _capital_operacional(self):
        cap = self.cfg.get("capital_max_real")
        return min(self.conta_valor, cap) if cap else self.conta_valor

    # ---------- sincronizacao com a corretora ----------
    def _sincronizar(self, now_ms):
        self.conta_valor, self.livre = self.ex.conta_valor()
        self.estado["capital"] = self.conta_valor
        self.estado["modo"] = self.modo
        self.estado.setdefault("capital_inicial_conta", self.conta_valor)
        pos_ex = self.ex.posicoes()
        for pos in list(self.estado["posicoes"]):
            if pos["moeda"] not in pos_ex:
                self._finalizar(pos, pos.get("motivo_saida"), now_ms)
            else:
                pos["tamanho"] = abs(pos_ex[pos["moeda"]]["szi"])
                pos["pnl_corretora"] = pos_ex[pos["moeda"]].get("pnl")
        geridas = {p["moeda"] for p in self.estado["posicoes"]}
        for m in pos_ex:
            if m not in geridas and m not in self.avisadas:
                log.warning("POSICAO DESCONHECIDA em %s na corretora (nao foi aberta por este bot): nao sera gerida", m)
                self.avisadas.add(m)
        pendentes = [p for p in self.estado["posicoes"]
                     if not p.get("motivo_saida") and p["moeda"] in pos_ex]
        if pendentes:
            abertas = {int(o["oid"]) for o in self.ex.ordens_abertas()}
            for pos in pendentes:
                faltam = [t for t, k in (("sl", "oid_stop"), ("tp", "oid_alvo"))
                          if pos.get(k) is None or pos[k] not in abertas]
                if faltam:
                    log.warning("Protecao ausente em %s (%s): recolocando", pos["moeda"], "+".join(faltam))
                    try:
                        self._proteger(pos, faltam)
                    except Exception as e:
                        log.error("Nao consegui recolocar protecao em %s: %s. Fechando por seguranca.", pos["moeda"], e)
                        self.fechar(pos, None, "sem_protecao", now_ms)

    def _proteger(self, pos, tipos=("sl", "tp")):
        for tipo in tipos:
            preco = pos["stop"] if tipo == "sl" else pos["alvo"]
            oid = self.ex.colocar_protecao(pos["moeda"], pos["lado"], pos["tamanho"], preco, tipo)
            pos["oid_stop" if tipo == "sl" else "oid_alvo"] = oid

    def _finalizar(self, pos, motivo, now_ms):
        """Posicao nao existe mais na corretora: registra o resultado real (fills)."""
        m = pos["moeda"]
        try:
            self.ex.cancelar_ordens(m)                 # cancela a protecao que sobrou (stop ou alvo)
            fills = self.ex.fills_desde(m, pos["entrada_ms"] - 5000)
        except Exception as e:
            log.error("Erro ao ler execucoes de %s: %s (tento de novo no proximo ciclo)", m, e)
            return False
        fech = [f for f in fills if str(f.get("dir", "")).startswith("Close") or float(f.get("closedPnl") or 0) != 0]
        if not fech:
            pos.setdefault("sumiu_ms", now_ms)
            if now_ms - pos["sumiu_ms"] < 5 * 60_000:
                return False                           # execucao ainda nao apareceu: espera
            log.warning("Posicao de %s sumiu sem execucoes registradas; registrando sem PnL", m)
        sz = sum(float(f["sz"]) for f in fech)
        saida = (sum(float(f["px"]) * float(f["sz"]) for f in fech) / sz) if sz else pos.get("ultimo_mid", pos["entrada"])
        taxas = sum(float(f.get("fee") or 0) for f in fills)
        pnl = sum(float(f.get("closedPnl") or 0) for f in fech) - taxas
        if not motivo:
            d_stop, d_alvo = abs(saida - pos["stop"]), abs(saida - pos["alvo"])
            motivo = "stop" if d_stop <= d_alvo else "alvo"
            if min(d_stop, d_alvo) / saida > 0.01:
                motivo = "corretora"
        try:
            self.conta_valor, self.livre = self.ex.conta_valor()
            self.estado["capital"] = self.conta_valor
        except Exception:
            pass
        self.estado["posicoes"].remove(pos)
        self._registrar_trade({
            "horario_utc": iso_utc(now_ms), "moeda": m, "lado": pos["lado"],
            "entrada": f"{pos['entrada']:.8g}", "saida": f"{saida:.8g}",
            "tamanho": f"{pos['tamanho']:.8g}", "motivo": motivo,
            "pnl_usd": f"{pnl:.6f}", "capital_apos": f"{self.estado['capital']:.6f}",
            "taxas_usd": f"{taxas:.6f}", "horario_entrada_utc": iso_utc(pos["entrada_ms"]),
        })
        log.info("FECHOU [%s] %s %s | motivo %s | entrada %.6g | saida %.6g | PnL US$ %+.4f | conta US$ %.4f",
                 self.modo.upper(), pos["lado"].upper(), m, motivo, pos["entrada"], saida, pnl,
                 self.estado["capital"])
        return True

    # ---------- abrir / fechar ----------
    def abrir(self, moeda, lado, mid, atr_val, now_ms):
        cfg = self.cfg
        if moeda not in self.ex._meta:
            log.info("IGNORADA %s: moeda nao existe na %s", moeda, self.modo)
            return False
        capital = self._capital_operacional()
        dist = cfg["stop_atr_mult"] * atr_val
        tam = calcular_tamanho(capital, mid, dist, cfg)
        sz = execucao.arredondar_tamanho(tam[0], self.ex.sz_decimals(moeda)) if tam else 0.0
        nocional = sz * mid
        if nocional < cfg["nocional_minimo"]:
            log.info("IGNORADA %s %s: nocional US$ %.2f abaixo do minimo de US$ %.0f",
                     moeda, lado.upper(), nocional, cfg["nocional_minimo"])
            return False
        try:
            lev = self.ex.definir_alavancagem(moeda, cfg["alavancagem_max"])
            if nocional / lev > self.livre * 0.95:
                log.info("IGNORADA %s: margem insuficiente (precisa US$ %.2f, livre US$ %.2f)",
                         moeda, nocional / lev, self.livre)
                return False
            preco, sz_exec = self.ex.abrir_mercado(moeda, lado, sz, cfg["slippage_max_ordem"])
        except Exception as e:
            log.error("ERRO ao abrir %s %s: %s", lado.upper(), moeda, e)
            return False
        pos = montar_posicao(moeda, lado, preco, sz_exec, atr_val, now_ms, 0.0, cfg)
        pos.update(ultimo_mid=mid, conferido_ms=now_ms, oid_stop=None, oid_alvo=None, alavancagem=lev)
        self.estado["posicoes"].append(pos)
        self.estado["trades_hoje"] += 1
        self.salvar_estado()
        try:
            self._proteger(pos)
        except Exception as e:
            log.error("FALHA ao colocar stop/alvo em %s: %s. Fechando a posicao por seguranca.", moeda, e)
            self.fechar(pos, mid, "sem_protecao", now_ms)
            return False
        log.info("ABRIU [%s] %s %s | entrada %.6g | tamanho %.6g | nocional US$ %.2f | alav. %dx isolada | "
                 "stop %.6g | alvo %.6g (ordens na corretora)", self.modo.upper(), lado.upper(), moeda,
                 preco, sz_exec, sz_exec * preco, lev, pos["stop"], pos["alvo"])
        self.salvar_estado()
        return True

    def fechar(self, pos, ref, motivo, now_ms):
        """Fecha a mercado na corretora. O registro (com o preco real) acontece quando a
        sincronizacao confirmar que a posicao sumiu."""
        m = pos["moeda"]
        try:
            self.ex.cancelar_ordens(m)
            self.ex.fechar_mercado(m, self.cfg["slippage_max_ordem"])
        except Exception as e:
            log.error("ERRO ao fechar %s (%s): %s. Tento de novo no proximo ciclo.", m, motivo, e)
            return 0.0
        pos["motivo_saida"] = motivo
        log.info("FECHANDO [%s] %s %s a mercado | motivo %s", self.modo.upper(), pos["lado"].upper(), m, motivo)
        return 0.0

    # ---------- ciclo ----------
    def _gerir_posicoes(self, now_ms):
        for pos in list(self.estado["posicoes"]):
            if pos.get("motivo_saida"):
                continue
            mid = self.mids.get(pos["moeda"])
            if mid is not None:
                pos["ultimo_mid"] = mid
            tmax = self.cfg.get("tempo_max_min")
            if tmax and now_ms - pos["entrada_ms"] >= tmax * 60_000:
                self.fechar(pos, mid, "tempo", now_ms)

    def ciclo(self):
        now_ms = agora_ms(self.now_fn)
        try:
            self.mids = parse_mids(self.api.all_mids())
        except Exception as e:
            log.error("Erro ao buscar precos: %s (tentando no proximo ciclo)", e)
            return False
        try:
            self._sincronizar(now_ms)
        except Exception as e:
            log.error("Erro ao sincronizar com a corretora: %s (tentando no proximo ciclo)", e)
            return False
        if os.path.exists(self.path_kill_fechar):
            log.warning("KILL_FECHAR encontrado: fechando todas as posicoes a mercado e encerrando.")
            for pos in list(self.estado["posicoes"]):
                if not pos.get("motivo_saida"):
                    self.fechar(pos, None, "kill", now_ms)
            try:
                os.remove(self.path_kill_fechar)
            except OSError:
                pass
            self.salvar_estado()
            self.parar = True
            return True
        self._checar_novo_dia(now_ms)
        self._atualizar_universo(now_ms)
        self._gerir_posicoes(now_ms)
        self._checar_limite_diario(now_ms)
        try:
            self._atualizar_radar(now_ms)
        except Exception as e:
            log.error("Erro no radar: %s", e)
        self._procurar_entradas(now_ms)
        self.salvar_estado()
        return True


def adquirir_lock(caminho):
    """Trava de arquivo para impedir duas copias do bot na mesma pasta.
    Funciona no Windows (msvcrt) e no Linux/Mac (fcntl)."""
    f = open(caminho, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def aplicar_config_ativa(base, cfg=CFG):
    """Se existir config_ativa.json (gerado pelo ativar_config.py, so para configuracao
    aprovada), sobrepoe esses parametros ao CFG."""
    caminho = os.path.join(base, "config_ativa.json")
    if not os.path.exists(caminho):
        return False
    with open(caminho) as f:
        extra = json.load(f)
    cfg.update(extra)
    log.info("config_ativa.json aplicado: estrategia %s %s", cfg.get("estrategia"), cfg.get("intervalo"))
    return True


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s UTC | %(levelname)s | %(message)s")
    logging.Formatter.converter = time.gmtime
    base = os.path.dirname(os.path.abspath(__file__))
    aplicar_config_ativa(base)
    lock = adquirir_lock(os.path.join(base, "bot.lock"))
    if lock is None:
        log.error("O bot ja esta rodando nesta pasta. Esta copia sera encerrada.")
        sys.exit(1)
    env = execucao.ler_env(os.path.join(base, ".env"))
    try:
        modo = execucao.modo_do_env(env)
        if modo == "paper":
            bot = PaperBot(CFG, base_dir=base)
        else:
            log.warning("MODO %s: o bot vai ENVIAR ORDENS para a Hyperliquid %s.", modo.upper(),
                        "TESTNET (dinheiro de teste)" if modo == "testnet" else "REAL (DINHEIRO DE VERDADE)")
            bot = LiveBot(CFG, execucao.ExecutorHyperliquid(modo, env), modo, base_dir=base)
    except Exception as e:
        log.error("Nao foi possivel iniciar: %s", e)
        sys.exit(1)

    def _sinal(signum, frame):
        log.info("Sinal %s recebido. Encerrando...", signum)
        bot.parar = True

    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)
    bot.rodar()


if __name__ == "__main__":
    main()
