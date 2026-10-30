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
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIGURACAO
# ---------------------------------------------------------------------------
CFG = {
    # dados
    "api_url": "https://api.hyperliquid.xyz/info",
    "http_timeout_s": 15,
    "http_tentativas": 3,
    "http_espera_base_s": 2,          # espera crescente: 2s, 4s, ...
    # universo
    "top_n": 10,
    "volume_minimo_usd": 20_000_000,
    "atualizar_universo_s": 3600,
    # estrategia
    "intervalo": "15m",
    "intervalo_ms": 15 * 60 * 1000,
    "candles_historico": 200,
    "ema_rapida": 20,
    "ema_lenta": 50,
    "rsi_periodo": 14,
    "atr_periodo": 14,
    "lateral_min_pct": 0.001,         # |EMA20-EMA50|/preco < 0,1% => lateral
    "rsi_long": (52, 68),
    "rsi_short": (32, 48),
    "candles_volume_prioridade": 20,
    # risco
    "capital_inicial": 50.0,
    "risco_por_trade": 0.015,
    "stop_atr_mult": 1.5,
    "alvo_mult_stop": 1.5,
    "tempo_max_min": 240,
    "alavancagem_max": 2.0,
    "nocional_minimo": 10.0,
    "max_posicoes": 2,
    "max_trades_dia": 8,
    "perda_diaria_max": 0.05,
    "taxa_por_lado": 0.00045,
    "slippage_por_lado": 0.0003,
    # operacao
    "loop_s": 30,
    "heartbeat_ciclos": 20,           # loga um resumo a cada N ciclos (~10 min)
    "arquivo_estado": "state.json",
    "arquivo_trades": "trades.csv",
    "arquivo_kill": "KILL",
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


# ---------------------------------------------------------------------------
# ESTRATEGIA
# ---------------------------------------------------------------------------
def avaliar_sinal(candles, cfg=CFG):
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
    info = {"ema20": e_r, "ema50": e_l, "rsi": r, "atr": a, "close": preco}
    if abs(e_r - e_l) / preco < cfg["lateral_min_pct"]:
        return None
    lo, hi = cfg["rsi_long"]
    if e_r > e_l and preco > e_r and lo <= r <= hi:
        return dict(info, lado="long")
    lo, hi = cfg["rsi_short"]
    if e_r < e_l and preco < e_r and lo <= r <= hi:
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


def verificar_saida(pos, mid, now_ms, cfg=CFG):
    """Retorna (motivo, preco_referencia) ou None."""
    if pos["lado"] == "long":
        if mid <= pos["stop"]:
            return "stop", mid          # preenche no preco atual (pior que o stop)
        if mid >= pos["alvo"]:
            return "alvo", pos["alvo"]  # preenche no alvo (conservador)
    else:
        if mid >= pos["stop"]:
            return "stop", mid
        if mid <= pos["alvo"]:
            return "alvo", pos["alvo"]
    if now_ms - pos["entrada_ms"] >= cfg["tempo_max_min"] * 60_000:
        return "tempo", mid
    return None


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
        self.universo = []           # [(moeda, volume)]
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
        saida = preco_execucao(mid, pos["lado"], "saida", self.cfg["slippage_por_lado"])
        pnl, _ = pnl_liquido(pos["lado"], pos["entrada"], saida, pos["tamanho"],
                             self.cfg["taxa_por_lado"])
        return pnl

    def equity(self):
        return self.estado["capital"] + sum(self.pnl_aberto(p) for p in self.estado["posicoes"])

    # ---------- abrir / fechar ----------
    def abrir(self, moeda, lado, mid, atr_val, now_ms):
        cfg = self.cfg
        entrada = preco_execucao(mid, lado, "entrada", cfg["slippage_por_lado"])
        dist = cfg["stop_atr_mult"] * atr_val
        tam = calcular_tamanho(self.estado["capital"], entrada, dist, cfg)
        if tam is None:
            log.info("IGNORADA %s %s: nocional abaixo de US$ %.0f", moeda, lado.upper(),
                     cfg["nocional_minimo"])
            return False
        tamanho, nocional = tam
        if lado == "long":
            stop, alvo = entrada - dist, entrada + cfg["alvo_mult_stop"] * dist
        else:
            stop, alvo = entrada + dist, entrada - cfg["alvo_mult_stop"] * dist
        pos = {"moeda": moeda, "lado": lado, "entrada": entrada, "tamanho": tamanho,
               "stop": stop, "alvo": alvo, "entrada_ms": now_ms, "ultimo_mid": mid}
        self.estado["posicoes"].append(pos)
        self.estado["trades_hoje"] += 1
        log.info("ABRIU %s %s | entrada %.6g | tamanho %.6g | nocional US$ %.2f | stop %.6g | alvo %.6g",
                 lado.upper(), moeda, entrada, tamanho, nocional, stop, alvo)
        return True

    def fechar(self, pos, ref, motivo, now_ms):
        cfg = self.cfg
        saida = preco_execucao(ref, pos["lado"], "saida", cfg["slippage_por_lado"])
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
            self.universo = parse_universo(self.api.meta_and_ctxs(), self.cfg)
            self.universo_ms = now_ms
            self.estado["universo"] = [[m, v] for m, v in self.universo]
            log.info("UNIVERSO atualizado: %s",
                     ", ".join(f"{m} ({v / 1e6:.0f}M)" for m, v in self.universo))
        except Exception as e:
            log.error("Erro ao atualizar universo: %s (mantendo lista anterior)", e)

    def _gerir_posicoes(self, now_ms):
        for pos in list(self.estado["posicoes"]):
            mid = self.mids.get(pos["moeda"])
            if mid is None:
                continue
            pos["ultimo_mid"] = mid
            saida = verificar_saida(pos, mid, now_ms, self.cfg)
            if saida:
                self.fechar(pos, saida[1], saida[0], now_ms)

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

    def _procurar_entradas(self, now_ms):
        if not self._pode_abrir():
            return
        iv = self.cfg["intervalo_ms"]
        ultimo_fechado = (now_ms // iv) * iv - iv   # abertura do ultimo candle fechado
        abertas = {p["moeda"] for p in self.estado["posicoes"]}
        candidatos = []
        for moeda, _ in self.universo:
            if moeda in abertas:
                continue
            if self.estado["ultimo_candle"].get(moeda, 0) >= ultimo_fechado:
                continue
            try:
                inicio = now_ms - self.cfg["candles_historico"] * iv
                cs = parse_candles(self.api.candles(moeda, self.cfg["intervalo"], inicio, now_ms), now_ms)
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
                log.info("SINAL %s %s | close %.6g EMA20 %.6g EMA50 %.6g RSI %.1f ATR %.6g",
                         sinal["lado"].upper(), moeda, sinal["close"], sinal["ema20"],
                         sinal["ema50"], sinal["rsi"], sinal["atr"])
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
        self._procurar_entradas(now_ms)
        self.salvar_estado()
        return True

    # ---------- loop ----------
    def _kill(self):
        return os.path.exists(self.path_kill)

    def rodar(self):
        log.info("Bot de PAPER TRADING iniciado (simulacao, sem dinheiro real). Capital US$ %.4f",
                 self.estado["capital"])
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


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s UTC | %(levelname)s | %(message)s")
    logging.Formatter.converter = time.gmtime
    base = os.path.dirname(os.path.abspath(__file__))
    lock = adquirir_lock(os.path.join(base, "bot.lock"))
    if lock is None:
        log.error("O bot ja esta rodando nesta pasta. Esta copia sera encerrada.")
        sys.exit(1)
    bot = PaperBot(CFG, base_dir=base)

    def _sinal(signum, frame):
        log.info("Sinal %s recebido. Encerrando...", signum)
        bot.parar = True

    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)
    bot.rodar()


if __name__ == "__main__":
    main()
