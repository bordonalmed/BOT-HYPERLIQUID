"""Testes rapidos com dados sinteticos (sem rede)."""
import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import report  # noqa: E402

IV = bot.CFG["intervalo_ms"]
T0 = 1_760_000_400_000 - (1_760_000_400_000 % IV)  # alinhado ao candle de 15m


def gerar_candles(n=120, inicio=100.0, sobe=1.0, desce=0.6, volume=1000.0, t0=T0):
    """Serie que alterna -desce, +sobe (termina subindo). sobe>desce = tendencia de alta."""
    cs, preco = [], inicio
    for i in range(n):
        anterior = preco
        preco += sobe if i % 2 == 1 else -desce
        cs.append({"t": t0 + i * IV, "T": t0 + (i + 1) * IV - 1, "o": anterior,
                   "h": max(anterior, preco) + 0.1, "l": min(anterior, preco) - 0.1,
                   "c": preco, "v": volume})
    return cs


def raw(cs):
    return [{k: (str(v) if k not in ("t", "T") else v) for k, v in c.items()} for c in cs]


class FakeAPI:
    def __init__(self):
        self.mids = {}
        self.candles_por_moeda = {}
        self.universo = None
        self.falhar = False
        self.chamadas_candles = 0

    def all_mids(self):
        if self.falhar:
            raise RuntimeError("rede bloqueada")
        return {k: str(v) for k, v in self.mids.items()}

    def meta_and_ctxs(self):
        if self.falhar:
            raise RuntimeError("rede bloqueada")
        return self.universo

    def candles(self, coin, interval, start, end):
        self.chamadas_candles += 1
        return raw(self.candles_por_moeda.get(coin, []))


class Relogio:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000


def novo_bot(tmp_path, cfg=None, agora=None):
    api = FakeAPI()
    rel = Relogio(agora or T0 + 200 * IV)
    b = bot.PaperBot(cfg or dict(bot.CFG), api=api, base_dir=str(tmp_path), now_fn=rel)
    return b, api, rel


def ler_trades(tmp_path):
    with open(tmp_path / "trades.csv") as f:
        return list(csv.DictReader(f))


# ---------------- indicadores ----------------
def test_ema_valores_conhecidos():
    e = bot.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None]
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(3.0)
    assert e[4] == pytest.approx(4.0)


def test_ema_constante():
    assert bot.ema([7.0] * 60, 20)[-1] == pytest.approx(7.0)


def test_rsi_extremos_e_neutro():
    assert bot.rsi([float(i) for i in range(30)], 14)[-1] == pytest.approx(100.0)
    assert bot.rsi([float(30 - i) for i in range(30)], 14)[-1] == pytest.approx(0.0)
    assert bot.rsi([5.0] * 30, 14)[-1] == pytest.approx(50.0)


def test_rsi_wilder_alternado():
    closes = [c["c"] for c in gerar_candles(200)]
    # ganhos medios 0,5 e perdas medias 0,3 => RSI ~ 62,5
    assert bot.rsi(closes, 14)[-1] == pytest.approx(62.5, abs=2.5)


def test_atr_range_constante():
    n = 40
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    a = bot.atr(highs, lows, closes, 14)
    assert a[13] is None
    assert a[14] == pytest.approx(2.0)
    assert a[-1] == pytest.approx(2.0)


# ---------------- sinais ----------------
def test_sinal_long():
    s = bot.avaliar_sinal(gerar_candles(120))
    assert s is not None and s["lado"] == "long"
    assert 52 <= s["rsi"] <= 68 and s["ema20"] > s["ema50"] and s["close"] > s["ema20"]


def test_sinal_short():
    cs = gerar_candles(120, inicio=200.0, sobe=-1.0, desce=-0.6)  # espelhado: queda
    s = bot.avaliar_sinal(cs)
    assert s is not None and s["lado"] == "short"
    assert 32 <= s["rsi"] <= 48 and s["ema20"] < s["ema50"] and s["close"] < s["ema20"]


def test_lateral_nao_opera():
    cs = gerar_candles(120, sobe=0.5, desce=0.5)
    assert bot.avaliar_sinal(cs) is None


def test_rsi_fora_da_faixa_nao_opera():
    cs = gerar_candles(120, sobe=1.0, desce=0.1)  # RSI muito alto (~90)
    assert bot.avaliar_sinal(cs) is None


def test_poucos_candles():
    assert bot.avaliar_sinal(gerar_candles(30)) is None


# ---------------- tamanho de posicao ----------------
def test_tamanho_normal():
    tam, noc = bot.calcular_tamanho(50.0, 100.0, 1.0)
    assert tam == pytest.approx(0.75)        # 1,5% de 50 = 0,75 / 1,0
    assert noc == pytest.approx(75.0)


def test_tamanho_limitado_alavancagem():
    tam, noc = bot.calcular_tamanho(50.0, 100.0, 0.1)
    assert noc == pytest.approx(100.0)       # max 2x capital
    assert tam == pytest.approx(1.0)


def test_tamanho_abaixo_minimo():
    assert bot.calcular_tamanho(50.0, 100.0, 10.0) is None  # nocional 7,5 < 10


# ---------------- custos ----------------
def test_slippage_contra():
    s = 0.0003
    assert bot.preco_execucao(100, "long", "entrada", s) == pytest.approx(100.03)
    assert bot.preco_execucao(100, "long", "saida", s) == pytest.approx(99.97)
    assert bot.preco_execucao(100, "short", "entrada", s) == pytest.approx(99.97)
    assert bot.preco_execucao(100, "short", "saida", s) == pytest.approx(100.03)


def test_custos_ida_e_volta_no_mesmo_preco():
    s, f = 0.0003, 0.00045
    e = bot.preco_execucao(100, "long", "entrada", s)
    x = bot.preco_execucao(100, "long", "saida", s)
    pnl, taxas = bot.pnl_liquido("long", e, x, 1.0, f)
    assert taxas == pytest.approx((100.03 + 99.97) * f)
    assert pnl == pytest.approx(-0.06 - 0.09)  # slippage 0,06 + taxas 0,09


# ---------------- execucao stop / alvo / tempo ----------------
def abrir_long(b, api, mid=100.0, atr=1.0):
    api.mids = {"BTC": mid}
    b.mids = {"BTC": mid}
    b.estado["dia"] = bot.dia_utc(bot.agora_ms(b.now_fn))
    b.estado["equity_inicio_dia"] = b.equity()
    assert b.abrir("BTC", "long", mid, atr, bot.agora_ms(b.now_fn))
    return b.estado["posicoes"][0]


def test_stop_long(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    pos = abrir_long(b, api)
    assert pos["entrada"] == pytest.approx(100.03)
    assert pos["stop"] == pytest.approx(100.03 - 1.5)
    assert pos["alvo"] == pytest.approx(100.03 + 2.25)
    b.mids = {"BTC": 98.4}
    b._gerir_posicoes(bot.agora_ms(rel))
    assert b.estado["posicoes"] == []
    t = ler_trades(tmp_path)[0]
    assert t["motivo"] == "stop"
    assert float(t["saida"]) == pytest.approx(98.4 * 0.9997)
    assert float(t["pnl_usd"]) < 0
    assert float(t["capital_apos"]) == pytest.approx(50 + float(t["pnl_usd"]))


def test_alvo_long(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    pos = abrir_long(b, api)
    b.mids = {"BTC": 103.0}
    b._gerir_posicoes(bot.agora_ms(rel))
    t = ler_trades(tmp_path)[0]
    assert t["motivo"] == "alvo"
    assert float(t["saida"]) == pytest.approx(pos["alvo"] * 0.9997)
    assert float(t["pnl_usd"]) > 0


def test_stop_e_alvo_short():
    pos = {"lado": "short", "stop": 101.5, "alvo": 97.75, "entrada_ms": 0}
    assert bot.verificar_saida(pos, 101.6, 1000)[0] == "stop"
    assert bot.verificar_saida(pos, 97.0, 1000) == ("alvo", 97.75)
    assert bot.verificar_saida(pos, 100.0, 1000) is None


def test_saida_por_tempo(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    rel.ms += 239 * 60_000
    b._gerir_posicoes(bot.agora_ms(rel))
    assert len(b.estado["posicoes"]) == 1
    rel.ms += 60_000
    b._gerir_posicoes(bot.agora_ms(rel))
    assert ler_trades(tmp_path)[0]["motivo"] == "tempo"


# ---------------- limite diario ----------------
def test_limite_diario(tmp_path):
    cfg = dict(bot.CFG, risco_por_trade=0.5)  # posicao grande para forcar o limite
    b, api, rel = novo_bot(tmp_path, cfg)
    abrir_long(b, api, mid=100.0, atr=4.0)     # stop largo (94.03); nocional limitado a 100 (2x)
    b.mids = {"BTC": 98.0}                      # perda ~2,2 USD: abaixo de 5% de 50
    b._gerir_posicoes(bot.agora_ms(rel))
    b._checar_limite_diario(bot.agora_ms(rel))
    assert len(b.estado["posicoes"]) == 1 and b.estado["pausado"] is False
    b.mids = {"BTC": 97.0}                      # perda ~3,2 USD (>2,5 = 5%), stop nao atingido
    b._gerir_posicoes(bot.agora_ms(rel))
    assert len(b.estado["posicoes"]) == 1
    b._checar_limite_diario(bot.agora_ms(rel))
    assert b.estado["pausado"] is True
    assert b.estado["posicoes"] == []
    assert ler_trades(tmp_path)[0]["motivo"] == "limite_diario"
    assert not b._pode_abrir()
    # novo dia UTC: despausa e zera contadores
    rel.ms += 24 * 3600 * 1000
    b._checar_novo_dia(bot.agora_ms(rel))
    assert b.estado["pausado"] is False and b.estado["trades_hoje"] == 0


def test_limite_diario_nao_dispara_com_perda_pequena(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    b.mids = {"BTC": 99.0}
    b._checar_limite_diario(bot.agora_ms(rel))
    assert b.estado["pausado"] is False and len(b.estado["posicoes"]) == 1


def test_max_trades_dia_e_posicoes(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    b.estado["trades_hoje"] = 8
    assert not b._pode_abrir()
    b.estado["trades_hoje"] = 0
    b.estado["posicoes"] = [{}, {}]
    assert not b._pode_abrir()


# ---------------- ciclo completo ----------------
def universo_fake(nomes_vol):
    meta = {"universe": [{"name": n, **({"isDelisted": True} if d else {})} for n, _, d in nomes_vol]}
    ctxs = [{"dayNtlVlm": str(v)} for _, v, _ in nomes_vol]
    return [meta, ctxs]


def test_parse_universo():
    raw_u = universo_fake([("BTC", 9e9, False), ("ETH", 5e9, False), ("OLD", 8e9, True),
                           ("TINY", 1e6, False)] + [(f"C{i}", 3e7 + i, False) for i in range(12)])
    u = bot.parse_universo(raw_u)
    nomes = [n for n, _ in u]
    assert len(u) == 10 and nomes[:2] == ["BTC", "ETH"]
    assert "OLD" not in nomes and "TINY" not in nomes


def test_parse_candles_so_fechados():
    cs = gerar_candles(5)
    agora = cs[3]["T"] + 1  # candle 4 ainda aberto
    assert len(bot.parse_candles(raw(cs), agora)) == 4


def test_ciclo_prioriza_maior_volume_e_avalia_uma_vez(tmp_path):
    cfg = dict(bot.CFG, max_posicoes=1)
    agora = T0 + 120 * IV + 5000
    b, api, rel = novo_bot(tmp_path, cfg, agora)
    api.universo = universo_fake([("AAA", 5e8, False), ("BBB", 4e8, False)])
    api.candles_por_moeda = {"AAA": gerar_candles(120, volume=10),
                             "BBB": gerar_candles(120, volume=99999)}
    api.mids = {"AAA": 124.0, "BBB": 124.0}
    assert b.ciclo()
    assert [p["moeda"] for p in b.estado["posicoes"]] == ["BBB"]
    chamadas = api.chamadas_candles
    rel.ms += 30_000           # mesmo candle: nao reavalia
    b.ciclo()
    assert api.chamadas_candles == chamadas
    assert os.path.exists(tmp_path / "state.json")


def test_estado_persistido(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    b.estado["ultimo_candle"]["BTC"] = 123
    b.salvar_estado()
    b2 = bot.PaperBot(dict(bot.CFG), api=api, base_dir=str(tmp_path), now_fn=rel)
    assert len(b2.estado["posicoes"]) == 1
    assert b2.estado["ultimo_candle"]["BTC"] == 123


def test_falha_de_rede_nao_derruba(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    api.falhar = True
    assert b.ciclo() is False


def test_kill_switch(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    (tmp_path / "KILL").write_text("")
    b.rodar()  # deve retornar imediatamente
    assert os.path.exists(tmp_path / "state.json")


# ---------------- relatorio ----------------
def test_relatorio_dados_insuficientes(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    b.mids = {"BTC": 103.0}
    b._gerir_posicoes(bot.agora_ms(rel))
    txt = report.relatorio(report.carregar(str(tmp_path / "trades.csv")))
    assert "DADOS INSUFICIENTES" in txt
    assert "Taxa de acerto: 100.0%" in txt


def test_relatorio_metricas():
    assert report.maior_sequencia_perdas([1, -1, -2, 3, -1, -1, -1, 2]) == 3
    assert report.drawdown_max_pct(100, [110, 99, 120]) == pytest.approx(10.0)
    assert report.semanas_seguidas_positivas({(2026, 1): 1, (2026, 2): 2, (2026, 4): 1}) == 2
    assert report.semanas_seguidas_positivas({(2026, 1): 1, (2026, 3): 2}) == 1


# ---------------- dashboard ----------------
def test_dashboard_coletar(tmp_path):
    import dashboard
    vazio = dashboard.coletar(str(tmp_path))
    assert vazio["operacoes"] == 0 and vazio["equity"] == pytest.approx(50.0)
    assert vazio["status"]["online"] is False
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    b.mids = {"BTC": 103.0}
    b._gerir_posicoes(bot.agora_ms(rel))
    abrir_long(b, api)
    b.estado["posicoes"][0]["ultimo_mid"] = 101.0
    b.estado["ultimo_ciclo_ms"] = bot.agora_ms(rel)
    b.salvar_estado()
    d = dashboard.coletar(str(tmp_path), agora=rel())
    assert d["operacoes"] == 1 and d["acerto"] == 100.0
    assert len(d["posicoes"]) == 1 and d["posicoes"][0]["pnl"] > 0
    assert d["status"]["online"] is True
    assert d["equity"] == pytest.approx(d["capital"] + d["posicoes"][0]["pnl"])


def test_dashboard_chave_de_acesso(tmp_path):
    import dashboard
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    H = dashboard.criar_handler(str(tmp_path), "segredo123", "http://x/?k=segredo123")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    porta = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # local (127.0.0.1) sempre liberado e recebe o link do celular
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/dados" % porta).read())
        assert d["url_celular"].endswith("segredo123")
        # simula acesso de outro aparelho
        H._local = lambda self: False
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/api/dados?k=errada" % porta)
        assert e.value.code == 403
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/dados?k=segredo123" % porta).read())
        assert "url_celular" not in d
    finally:
        srv.shutdown()


# ---------------- backtest ----------------
def test_backtest_gera_trades_e_respeita_regras(tmp_path):
    import backtest
    import random
    random.seed(3)
    dados = {}
    for k, nome in enumerate(["AAA", "BBB", "CCC"]):
        cs, preco = [], 100.0 + k
        for i in range(3000):
            drift = 0.03 if (i // 400) % 2 == 0 else -0.03
            ant, preco = preco, max(1.0, preco + drift + random.gauss(0, 0.35))
            cs.append({"t": T0 + i * IV, "T": T0 + (i + 1) * IV - 1, "o": ant,
                       "h": max(ant, preco) + abs(random.gauss(0, 0.15)),
                       "l": min(ant, preco) - abs(random.gauss(0, 0.15)), "c": preco, "v": 100.0})
        dados[nome] = cs
    saida = str(tmp_path / "bt.csv")
    trades = backtest.simular(dados, bot.CFG, saida)
    assert len(trades) > 10
    linhas = report.carregar(saida)
    assert len(linhas) == len(trades)
    # nunca mais de 8 entradas por dia e nunca mais de 2 posicoes ao mesmo tempo
    from collections import Counter
    por_dia = Counter(t["horario_entrada_utc"][:10] for t in trades)
    assert max(por_dia.values()) <= 8
    eventos = sorted([(t["horario_entrada_utc"], 1) for t in trades] +
                     [(t["horario_utc"], -1) for t in trades], key=lambda x: (x[0], x[1]))
    abertas = 0
    for _, d in eventos:
        abertas += d
        assert abertas <= 2
    assert {t["motivo"] for t in trades} <= {"stop", "alvo", "tempo", "limite_diario", "fim_backtest"}


def test_backtest_candle_toca_stop_e_alvo_conta_stop(tmp_path):
    import backtest
    cs = gerar_candles(120)
    ultimo = cs[-1]
    # proximo candle: abre em 124, varre de 110 a 140 (toca stop e alvo)
    cs.append({"t": ultimo["t"] + IV, "T": ultimo["T"] + IV, "o": 124.0, "h": 140.0,
               "l": 110.0, "c": 125.0, "v": 1000.0})
    trades = backtest.simular({"AAA": cs}, bot.CFG)
    ult = trades[-1]
    assert float(ult["entrada"]) == pytest.approx(124 * 1.0003, rel=1e-6)
    assert ult["motivo"] == "stop" and ult["lado"] == "long"
