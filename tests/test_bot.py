"""Testes rapidos com dados sinteticos (sem rede)."""
import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import report  # noqa: E402

# Os testes de logica usam os parametros originais de 15m (numeros faceis de conferir).
# A configuracao aprovada (4h) tem testes proprios no final do arquivo.
LEGADO = {"intervalo": "15m", "intervalo_ms": 15 * 60 * 1000, "candles_historico": 200,
          "filtro_ema": None, "rsi_long": (52, 68), "rsi_short": (32, 48),
          "stop_atr_mult": 1.5, "alvo_mult_stop": 1.5, "tempo_max_min": 240,
          "max_trades_dia": 8, "alavancagem_max": 2.0, "estrategia": "tendencia",
          "trailing_atr_mult": None}
CFG_APROVADA = dict(bot.CFG)


@pytest.fixture(autouse=True)
def cfg_legado():
    original = dict(bot.CFG)
    bot.CFG.update(LEGADO)
    yield
    bot.CFG.clear()
    bot.CFG.update(original)


IV = LEGADO["intervalo_ms"]
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


# ---------------- otimizador ----------------
def test_otimizador_roda_e_separa_treino_teste():
    import otimizar
    import random
    random.seed(5)
    todos = {}
    for iv, ms in otimizar.INTERVALOS.items():
        d = {}
        for k in range(3):
            cs, p = [], 100.0
            for i in range(1500):
                drift = 0.05 if (i // 300) % 2 == 0 else -0.05
                a, p = p, max(1, p + drift + random.gauss(0, 0.4))
                cs.append({"t": i * ms, "T": (i + 1) * ms - 1, "o": a, "h": max(a, p) + .1,
                           "l": min(a, p) - .1, "c": p, "v": 10})
            d["C%d" % k] = cs
        todos[iv] = d
    ini, corte, fim = otimizar.periodo(todos["1h"], 0.6)
    assert ini < corte < fim
    linhas = []
    escolhida, m_teste, aprovada = otimizar.otimizar(todos, 0.6, linhas.append)
    assert escolhida is not None and isinstance(aprovada, bool)
    assert any("VEREDITO" in l for l in linhas)


def test_simular_respeita_periodo():
    import backtest
    cs = gerar_candles(400)
    corte = cs[300]["t"]
    trades = backtest.simular({"AAA": cs}, bot.CFG, t_fim=corte)
    assert all(t["horario_utc"] <= bot.iso_utc(corte + IV) for t in trades)


# ---------------- configuracao aprovada (4h) ----------------
def test_config_aprovada_valores():
    c = CFG_APROVADA
    assert c["intervalo"] == "4h" and c["stop_atr_mult"] == 3.0 and c["alvo_mult_stop"] == 3.0
    assert c["filtro_ema"] == 200 and c["max_trades_dia"] == 3 and c["tempo_max_min"] == 48 * 240
    assert c["candles_historico"] >= 260


def test_filtro_ema200_bloqueia_contra_tendencia():
    cfg = dict(CFG_APROVADA, rsi_long=(52, 68))
    # longa queda seguida de repique: EMA20 > EMA50 no curto prazo, mas preco abaixo da EMA200
    queda = gerar_candles(300, inicio=400.0, sobe=-1.0, desce=-0.6)
    repique = gerar_candles(60, inicio=queda[-1]["c"], t0=queda[-1]["t"] + IV)
    cs = queda + repique
    sem = bot.avaliar_sinal(cs, dict(cfg, filtro_ema=None))
    com = bot.avaliar_sinal(cs, cfg)
    assert sem is not None and sem["lado"] == "long"
    assert com is None
    # tendencia de alta longa: filtro deixa passar
    alta = bot.avaliar_sinal(gerar_candles(320), cfg)
    assert alta is not None and alta["lado"] == "long"


def test_conferencia_apos_pausa_fecha_no_stop(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    pos = abrir_long(b, api)                       # stop 98.53, alvo 102.28
    t_ini = bot.agora_ms(rel)
    rel.ms += 60 * 60_000                          # notebook "dormiu" 1 hora
    # nesse periodo o preco caiu ate 98 (stop) e voltou para 101
    cs = [{"t": t_ini + i * 300_000, "T": t_ini + (i + 1) * 300_000 - 1, "o": 100.0,
           "h": 100.5, "l": 99.5, "c": 100.0, "v": 1.0} for i in range(12)]
    cs[5].update(l=98.0)
    api.candles_por_moeda = {"BTC": cs}
    b.mids = {"BTC": 101.0}
    b._gerir_posicoes(bot.agora_ms(rel))
    assert b.estado["posicoes"] == []
    t = ler_trades(tmp_path)[0]
    assert t["motivo"] == "stop"
    assert float(t["saida"]) == pytest.approx(pos["stop"] * 0.9997)


def test_conferencia_sem_toque_mantem_posicao(tmp_path):
    b, api, rel = novo_bot(tmp_path)
    abrir_long(b, api)
    t_ini = bot.agora_ms(rel)
    rel.ms += 30 * 60_000
    api.candles_por_moeda = {"BTC": [{"t": t_ini + i * 300_000, "T": t_ini + (i + 1) * 300_000 - 1,
                                      "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}
                                     for i in range(6)]}
    b.mids = {"BTC": 100.5}
    b._gerir_posicoes(bot.agora_ms(rel))
    assert len(b.estado["posicoes"]) == 1
    assert b.estado["posicoes"][0]["conferido_ms"] == bot.agora_ms(rel)


# ---------------- rompimento / stop movel / radar ----------------
def candles_compressao_rompimento(n_lateral=200, lado="long", volume_final=5000.0):
    """Tendencia, depois lateral bem apertada (compressao) e um candle final que rompe."""
    cs, preco = [], 100.0
    for i in range(160):                                   # tendencia previa
        ant, preco = preco, preco + (0.5 if lado == "long" else -0.5) + (0.8 if i % 2 else -0.8)
        cs.append({"o": ant, "h": max(ant, preco) + 0.3, "l": min(ant, preco) - 0.3, "c": preco, "v": 1000.0})
    base = preco
    for i in range(n_lateral):                             # compressao: amplitude caindo
        amp = 2.0 * (1 - i / n_lateral) + 0.05
        ant, preco = preco, base + (amp if i % 2 else -amp) * 0.5
        cs.append({"o": ant, "h": max(ant, preco) + 0.05, "l": min(ant, preco) - 0.05, "c": preco, "v": 1000.0})
    ant = preco
    preco = base + (4.0 if lado == "long" else -4.0)
    cs.append({"o": ant, "h": max(ant, preco) + 0.1, "l": min(ant, preco) - 0.1, "c": preco, "v": volume_final})
    for i, c in enumerate(cs):
        c["t"], c["T"] = T0 + i * IV, T0 + (i + 1) * IV - 1
    return cs


P_ROMP = {"lookback": 20, "squeeze_fator": 1.2, "squeeze_janela": 120, "squeeze_recente": 6, "vol_mult": 1.5}


def test_largura_bollinger():
    assert bot.largura_bollinger([5.0] * 30)[-1] == pytest.approx(0.0)
    import statistics
    xs = [float(x % 7) + 10 for x in range(40)]
    esperado = 4 * statistics.pstdev(xs[-20:]) / (sum(xs[-20:]) / 20)
    assert bot.largura_bollinger(xs)[-1] == pytest.approx(esperado)


def test_rompimento_long_e_short():
    cfg = dict(bot.CFG, estrategia="rompimento", rompimento=P_ROMP, filtro_ema=None)
    s = bot.avaliar_sinal(candles_compressao_rompimento(lado="long"), cfg)
    assert s is not None and s["lado"] == "long"
    s = bot.avaliar_sinal(candles_compressao_rompimento(lado="short"), cfg)
    assert s is not None and s["lado"] == "short"


def test_rompimento_exige_volume_e_compressao():
    cfg = dict(bot.CFG, estrategia="rompimento", rompimento=P_ROMP, filtro_ema=None)
    assert bot.avaliar_sinal(candles_compressao_rompimento(volume_final=1000.0), cfg) is None
    sem_vol = dict(P_ROMP, vol_mult=None)
    assert bot.avaliar_sinal(candles_compressao_rompimento(volume_final=1000.0),
                             dict(cfg, rompimento=sem_vol)) is not None
    # sem compressao (amplitude constante e alta): o filtro de compressao barra o rompimento
    cs = candles_compressao_rompimento()
    for c in cs[160:-1]:
        c["h"], c["l"] = c["o"] + 3.0, c["o"] - 3.0
        c["c"] = c["o"] + (2.5 if c["t"] // IV % 2 else -2.5)
    cs[-1]["c"] = max(c["h"] for c in cs[-21:-1]) + 1.0
    cs[-1]["h"] = cs[-1]["c"] + 0.1
    ind = bot.indicadores_rompimento(cs)
    assert bot.sinal_rompimento_em(ind, len(cs) - 1, dict(P_ROMP, vol_mult=None)) is None
    assert bot.sinal_rompimento_em(ind, len(cs) - 1, dict(P_ROMP, vol_mult=None, squeeze_fator=None))["lado"] == "long"


def test_stop_movel_long_e_short():
    pos = bot.montar_posicao("X", "long", 100.0, 1.0, 2.0, 0, 0.0,
                             dict(bot.CFG, stop_atr_mult=2.0, alvo_mult_stop=None, trailing_atr_mult=3.0))
    assert pos["stop"] == pytest.approx(96.0) and pos["alvo"] is None
    bot.atualizar_trailing(pos, 110.0, 105.0)
    assert pos["stop"] == pytest.approx(104.0)          # 110 - 3*2
    bot.atualizar_trailing(pos, 107.0, 101.0)            # recuo: stop nao afrouxa
    assert pos["stop"] == pytest.approx(104.0)
    assert bot.verificar_saida_candle(pos, {"o": 106, "h": 106, "l": 103, "c": 104}) == ("stop", 104.0)
    sh = bot.montar_posicao("X", "short", 100.0, 1.0, 2.0, 0, 0.0,
                            dict(bot.CFG, stop_atr_mult=2.0, alvo_mult_stop=None, trailing_atr_mult=3.0))
    bot.atualizar_trailing(sh, 95.0, 90.0)
    assert sh["stop"] == pytest.approx(96.0)            # 90 + 3*2
    assert bot.verificar_saida(sh, 96.5, 0, dict(bot.CFG, tempo_max_min=None)) == ("stop", 96.5)


def test_stop_movel_no_bot_ao_vivo(tmp_path):
    cfg = dict(bot.CFG, alvo_mult_stop=None, trailing_atr_mult=2.0, tempo_max_min=None)
    b, api, rel = novo_bot(tmp_path, cfg)
    pos = abrir_long(b, api, mid=100.0, atr=1.0)        # stop 98.53
    for preco in (101.0, 104.0, 108.0):
        b.mids = {"BTC": preco}
        b._gerir_posicoes(bot.agora_ms(rel))
    assert pos["stop"] == pytest.approx(106.0)          # 108 - 2*1
    b.mids = {"BTC": 105.5}
    b._gerir_posicoes(bot.agora_ms(rel))
    t = ler_trades(tmp_path)[0]
    assert t["motivo"] == "stop" and float(t["pnl_usd"]) > 0


def test_alavancagem_4x():
    cfg = dict(bot.CFG, alavancagem_max=4.0)
    tam, noc = bot.calcular_tamanho(50.0, 100.0, 0.1, cfg)
    assert noc == pytest.approx(200.0)                  # teto 4x
    tam, noc = bot.calcular_tamanho(50.0, 100.0, 2.0, cfg)
    assert noc == pytest.approx(37.5)                   # risco 1,5% manda, nao a alavancagem


def test_slippage_moedas_pequenas():
    cfg = dict(bot.CFG, volume_grande_usd=100e6, slippage_pequenas=0.001, slippage_por_lado=0.0003)
    assert bot.slippage_da_moeda(50e6, cfg) == 0.001
    assert bot.slippage_da_moeda(500e6, cfg) == 0.0003
    assert bot.slippage_da_moeda(None, cfg) == 0.0003


def test_metricas_radar():
    cs = candles_compressao_rompimento()
    btc = gerar_candles(len(cs), sobe=0.6, desce=0.6)
    m = bot.metricas_radar(cs, btc, {"funding": 0.0001})
    assert m is not None and 0 <= m["nota"] <= 100
    assert m["funding_apr_pct"] == pytest.approx(87.6)
    assert m["compressao"] >= 1.0
    assert bot.metricas_radar(cs[:100]) is None


def test_radar_no_ciclo(tmp_path):
    agora = T0 + 400 * IV + 5000
    b, api, rel = novo_bot(tmp_path, agora=agora)
    api.universo = universo_fake([("BTC", 9e9, False), ("AAA", 5e8, False), ("PEQ", 1.5e7, False)])
    for c in api.universo[1]:
        c["funding"] = "0.00002"
    cs = candles_compressao_rompimento(n_lateral=239)
    api.candles_por_moeda = {"BTC": gerar_candles(400), "AAA": cs, "PEQ": cs}
    api.mids = {"BTC": 200.0, "AAA": cs[-1]["c"], "PEQ": cs[-1]["c"]}
    assert b.ciclo()
    radar = b.estado["radar"]
    assert {r["moeda"] for r in radar} == {"BTC", "AAA", "PEQ"}
    assert [m for m, _ in b.universo] == ["BTC", "AAA"]  # PEQ < 20M: so no radar
    chamadas = api.chamadas_candles
    rel.ms += 30_000
    b.ciclo()
    assert api.chamadas_candles == chamadas              # mesmo candle: nada de novo


def test_backtest_rompimento_com_stop_movel():
    import backtest
    cfg = dict(bot.CFG, estrategia="rompimento", rompimento=dict(P_ROMP, vol_mult=None, squeeze_fator=None),
               filtro_ema=None, alvo_mult_stop=None, trailing_atr_mult=2.0, tempo_max_min=None,
               stop_atr_mult=1.5)
    trades = backtest.simular({"AAA": gerar_candles(600)}, cfg)
    assert trades
    assert all(t["motivo"] in ("stop", "fim_backtest", "limite_diario") for t in trades)


def test_otimizador_rompimento_e_ativacao(tmp_path):
    import otimizar_rompimento as orr
    import ativar_config
    import random
    random.seed(11)
    todos = {}
    for iv, ms in orr.INTERVALOS.items():
        dados, uni = {}, []
        for k in range(4):
            cs, p = [], 100.0
            for i in range(1200):
                sig = 0.15 if (i // 150) % 2 == 0 else 0.6
                drift = 0.08 if (i // 300) % 2 == 0 else -0.08
                a, p = p, max(1, p + drift + random.gauss(0, sig))
                cs.append({"t": i * ms, "T": (i + 1) * ms - 1, "o": a, "h": max(a, p) + .1,
                           "l": min(a, p) - .1, "c": p, "v": random.uniform(50, 150)})
            dados["C%d" % k] = cs
            uni.append(["C%d" % k, 5e8 / (k + 1)])
        todos[iv] = {"universo": uni, "dados": dados}
    linhas = []
    escolhida, m_teste, aprovada = orr.otimizar(todos, 0.6, linhas.append)
    assert escolhida is not None and any("VEREDITO" in l for l in linhas)
    # ativacao: recusa se nao aprovada, ativa se aprovada
    origem, destino = str(tmp_path / "m.json"), str(tmp_path / "config_ativa.json")
    cfg = orr.cfg_de(escolhida, {})
    pacote = {"parametros": escolhida, "teste": m_teste, "aprovada": False,
              "cfg_para_o_bot": {"estrategia": "rompimento", "trailing_atr_mult": cfg["trailing_atr_mult"],
                                 "alvo_mult_stop": None, "rompimento": cfg["rompimento"]}}
    json.dump(pacote, open(origem, "w"))
    ok, msg = ativar_config.ativar(origem, destino)
    assert not ok and not os.path.exists(destino)
    pacote["aprovada"] = True
    json.dump(pacote, open(origem, "w"))
    ok, msg = ativar_config.ativar(origem, destino)
    assert ok and os.path.exists(destino)
    c = dict(bot.CFG)
    assert bot.aplicar_config_ativa(str(tmp_path), c)
    assert c["estrategia"] == "rompimento" and c["alvo_mult_stop"] is None
    ok, _ = ativar_config.desativar(destino)
    assert ok and not os.path.exists(destino)


# ---------------- execucao na corretora (testnet/real) com corretora FALSA ----------------
import execucao  # noqa: E402

CONTA = "0x" + "a" * 40
AGENTE = "0x" + "b" * 40


def test_arredondamentos_hyperliquid():
    assert execucao.arredondar_tamanho(0.123456, 3) == 0.123
    assert execucao.arredondar_tamanho(1.9999, 0) == 1.0
    assert execucao.arredondar_preco(77481.37, 5) == 77481.0
    assert execucao.arredondar_preco(1.234567, 0) == 1.2346
    assert execucao.arredondar_preco(0.0123456, 0) == 0.012346
    assert execucao.arredondar_preco(123456.7, 5) == 123457.0


def test_env_e_modos(tmp_path):
    (tmp_path / ".env").write_text("# comentario\nMODO=testnet\nHL_ACCOUNT_ADDRESS=%s\nHL_SECRET_KEY='x'\n" % CONTA)
    env = execucao.ler_env(str(tmp_path / ".env"))
    assert env["MODO"] == "testnet" and env["HL_SECRET_KEY"] == "x"
    assert execucao.modo_do_env(env) == "testnet"
    assert execucao.modo_do_env({}) == "paper"
    with pytest.raises(execucao.ErroExecucao):
        execucao.modo_do_env({"MODO": "real"})               # falta CONFIRMO_DINHEIRO_REAL=SIM
    assert execucao.modo_do_env({"MODO": "real", "CONFIRMO_DINHEIRO_REAL": "SIM"}) == "real"


def test_recusa_chave_da_carteira_principal():
    with pytest.raises(execucao.ErroExecucao):
        execucao.validar_credenciais({"HL_ACCOUNT_ADDRESS": CONTA}, CONTA)
    with pytest.raises(execucao.ErroExecucao):
        execucao.validar_credenciais({"HL_ACCOUNT_ADDRESS": "abc"}, AGENTE)
    assert execucao.validar_credenciais({"HL_ACCOUNT_ADDRESS": CONTA}, AGENTE) == CONTA


class FakeInfo:
    def __init__(self):
        self.estado = {"marginSummary": {"accountValue": "50.0"}, "withdrawable": "50.0", "assetPositions": []}
        self.ordens, self.fills = [], []

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                             {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]}

    def user_state(self, a):
        return self.estado

    def frontend_open_orders(self, a):
        return self.ordens

    def user_fills_by_time(self, a, t):
        return [f for f in self.fills if f["time"] >= t]

    def all_mids(self):
        return {"BTC": "100.0"}


class FakeExchange:
    """Simula a corretora: executa a mercado no mid, guarda triggers como ordens abertas."""
    def __init__(self, info, mid=100.0):
        self.info, self.mid, self.oid, self.chamadas = info, mid, 100, []
        self.recusar_trigger = False
        self.agora = lambda: 0

    def _ok(self, st):
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [st]}}}

    def update_leverage(self, lev, coin, is_cross):
        self.chamadas.append(("lev", coin, lev, is_cross))
        return {"status": "ok"}

    def market_open(self, coin, is_buy, sz, px, slippage):
        self.chamadas.append(("open", coin, is_buy, sz))
        szi = sz if is_buy else -sz
        self.info.estado["assetPositions"] = [{"position": {"coin": coin, "szi": str(szi), "entryPx": str(self.mid),
                                                           "unrealizedPnl": "0"}}]
        self.info.fills.append({"coin": coin, "px": str(self.mid), "sz": str(sz), "dir": "Open Long" if is_buy else "Open Short",
                                "closedPnl": "0", "fee": "0.005", "time": self.agora()})
        return self._ok({"filled": {"avgPx": str(self.mid), "totalSz": str(sz), "oid": 1}})

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only):
        self.chamadas.append(("trigger", coin, is_buy, sz, limit_px, order_type, reduce_only))
        if self.recusar_trigger:
            return self._ok({"error": "trigger recusado"})
        self.oid += 1
        self.info.ordens.append({"coin": coin, "oid": self.oid})
        return self._ok({"resting": {"oid": self.oid}})

    def market_close(self, coin, sz, px, slippage):
        self.chamadas.append(("close", coin))
        if not self.info.estado["assetPositions"]:
            return None
        self.info.estado["assetPositions"] = []
        return self._ok({"filled": {"avgPx": str(self.mid), "totalSz": "1", "oid": 9}})

    def cancel(self, coin, oid):
        self.info.ordens = [o for o in self.info.ordens if o["oid"] != oid]
        return {"status": "ok"}

    def simular_stop(self, coin, px, sz, closed_pnl, t):
        """A corretora executou o stop sozinha (bot nem precisou estar ligado)."""
        self.info.estado["assetPositions"] = []
        self.info.fills.append({"coin": coin, "px": str(px), "sz": str(sz), "dir": "Close Long",
                                "closedPnl": str(closed_pnl), "fee": "0.004", "time": t})


def novo_executor():
    info = FakeInfo()
    exch = FakeExchange(info)
    ex = execucao.ExecutorHyperliquid("testnet", {"HL_ACCOUNT_ADDRESS": CONTA}, info=info, exchange=exch,
                                      endereco_agente=AGENTE)
    return ex, info, exch


def test_executor_protecoes_reduce_only_e_arredondadas():
    ex, info, exch = novo_executor()
    oid = ex.colocar_protecao("BTC", "long", 0.001, 97.123456, "sl")
    _, coin, is_buy, sz, limite, otype, reduce_only = exch.chamadas[-1]
    assert oid == 101 and reduce_only is True and is_buy is False          # stop de long = venda
    assert otype["trigger"]["tpsl"] == "sl" and otype["trigger"]["isMarket"] is True
    assert otype["trigger"]["triggerPx"] == 97.1                             # BTC: 6 - 5 = 1 casa decimal
    exch.recusar_trigger = True
    with pytest.raises(execucao.ErroExecucao):
        ex.colocar_protecao("BTC", "long", 0.001, 97.0, "tp")


def novo_livebot(tmp_path, cfg=None):
    ex, info, exch = novo_executor()
    api = FakeAPI()
    rel = Relogio(T0 + 200 * IV)
    c = cfg or dict(bot.CFG, alavancagem_max=4.0)
    lb = bot.LiveBot(c, ex, "testnet", api=api, base_dir=str(tmp_path), now_fn=rel)
    exch.agora = lambda: bot.agora_ms(rel)
    api.mids = {"BTC": 100.0}
    lb.mids = {"BTC": 100.0}
    lb.conta_valor, lb.livre = ex.conta_valor()
    lb.estado["dia"] = bot.dia_utc(bot.agora_ms(rel))
    lb.estado["equity_inicio_dia"] = 50.0
    return lb, ex, info, exch, api, rel


def test_livebot_abre_com_stop_e_alvo_na_corretora(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    assert lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    pos = lb.estado["posicoes"][0]
    tipos = [c for c in exch.chamadas if c[0] == "trigger"]
    assert [t[5]["trigger"]["tpsl"] for t in tipos] == ["sl", "tp"]
    assert pos["oid_stop"] and pos["oid_alvo"]
    assert ("lev", "BTC", 4, False) in exch.chamadas                       # 4x isolada
    assert os.path.exists(tmp_path / "state_testnet.json")                  # arquivo separado do paper
    assert not os.path.exists(tmp_path / "state.json")


def test_livebot_fecha_se_protecao_falhar(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    exch.recusar_trigger = True
    assert not lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    assert ("close", "BTC") in exch.chamadas                                # nunca fica sem protecao
    assert lb.estado["posicoes"][0]["motivo_saida"] == "sem_protecao"


def test_livebot_registra_stop_executado_pela_corretora(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    pos = lb.estado["posicoes"][0]
    rel.ms += 3600_000
    exch.simular_stop("BTC", pos["stop"], pos["tamanho"], -0.74, bot.agora_ms(rel))
    info.estado["marginSummary"]["accountValue"] = "49.25"
    assert lb.ciclo()
    assert lb.estado["posicoes"] == []
    assert info.ordens == []                                                 # alvo que sobrou foi cancelado
    t = list(csv.DictReader(open(tmp_path / "trades_testnet.csv")))[0]
    assert t["motivo"] == "stop"
    assert float(t["pnl_usd"]) == pytest.approx(-0.74 - 0.005 - 0.004)       # pnl real - taxas reais
    assert float(t["capital_apos"]) == pytest.approx(49.25)


def test_livebot_saida_por_tempo_e_kill_fechar(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    rel.ms += lb.cfg["tempo_max_min"] * 60_000
    lb._gerir_posicoes(bot.agora_ms(rel))
    assert ("close", "BTC") in exch.chamadas
    info.fills.append({"coin": "BTC", "px": "100.5", "sz": "1", "dir": "Close Long", "closedPnl": "0.1",
                       "fee": "0.004", "time": bot.agora_ms(rel)})
    lb._sincronizar(bot.agora_ms(rel))
    assert lb.estado["posicoes"] == []
    assert list(csv.DictReader(open(tmp_path / "trades_testnet.csv")))[0]["motivo"] == "tempo"
    # KILL_FECHAR: fecha tudo e para
    lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    (tmp_path / "KILL_FECHAR").write_text("")
    lb.ciclo()
    assert lb.parar and not os.path.exists(tmp_path / "KILL_FECHAR")
    assert exch.chamadas.count(("close", "BTC")) == 2


def test_livebot_recoloca_protecao_e_avisa_posicao_desconhecida(tmp_path, caplog):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    lb.abrir("BTC", "long", 100.0, 1.0, bot.agora_ms(rel))
    info.ordens = []                                                         # alguem cancelou no site
    info.estado["assetPositions"].append({"position": {"coin": "ETH", "szi": "0.5", "entryPx": "10"}})
    lb._sincronizar(bot.agora_ms(rel))
    assert len(info.ordens) == 2
    assert "DESCONHECIDA" in caplog.text


def test_livebot_respeita_teto_de_capital(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path, dict(bot.CFG, capital_max_real=60.0))
    lb.conta_valor = 1000.0
    assert lb._capital_operacional() == 60.0


def test_livebot_recusa_stop_movel():
    ex, info, exch = novo_executor()
    with pytest.raises(ValueError):
        bot.LiveBot(dict(bot.CFG, trailing_atr_mult=3.0), ex, "testnet", api=FakeAPI(), base_dir=".")


def test_conta_unificada_usa_saldo_spot():
    ex, info, exch = novo_executor()
    assert ex.modo_conta is None and ex.conta_valor() == (50.0, 50.0)      # conta classica
    info.estado = {"marginSummary": {"accountValue": "0.0"}, "withdrawable": "0.0",
                   "assetPositions": [{"position": {"coin": "BTC", "szi": "0.0002", "entryPx": "77000",
                                                    "unrealizedPnl": "-0.12"}}]}
    info.spot_user_state = lambda a: {"balances": [{"coin": "USDC", "total": "19.80", "hold": "3.10"}]}
    info.query_user_abstraction_state = lambda a: "unifiedAccount"
    ex.modo_conta = ex._ler_modo_conta()
    assert ex.modo_conta == "unifiedAccount"
    valor, livre = ex.conta_valor()
    assert valor == pytest.approx(19.80 - 0.12) and livre == pytest.approx(16.70)
    # modo desconhecido + perps zerados + USDC no spot => trata como unificada
    ex.modo_conta = None
    info.estado["marginSummary"]["accountValue"] = "0.0"
    assert ex.conta_valor()[0] == pytest.approx(19.68)


def test_deposito_nao_conta_como_lucro(tmp_path):
    import dashboard
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    info.user_non_funding_ledger_updates = lambda a, t: []
    lb._sincronizar(bot.agora_ms(rel))
    assert lb.estado["capital_inicial_conta"] == 50.0 and lb.estado["aportes"] == 0.0
    # deposito de 30 e saque de 5 (taxa 1) depois do inicio
    info.estado["marginSummary"]["accountValue"] = "74.0"
    info.user_non_funding_ledger_updates = lambda a, t: [
        {"delta": {"type": "deposit", "usdc": "30.0"}},
        {"delta": {"type": "withdraw", "usdc": "5.0", "fee": "1.0"}},
        {"delta": {"type": "accountClassTransfer", "usdc": "10", "toPerp": True}}]
    rel.ms += 6 * 60_000
    lb._sincronizar(bot.agora_ms(rel))
    assert lb.estado["aportes"] == pytest.approx(24.0)
    lb.salvar_estado()
    d = dashboard.coletar(str(tmp_path), dict(bot.CFG, arquivo_estado="state_testnet.json",
                                               arquivo_trades="trades_testnet.csv"), agora=rel(), modo="testnet")
    assert d["capital_inicial"] == pytest.approx(74.0) and d["pnl_total"] == pytest.approx(0.0)


def test_estado_antigo_passa_a_descontar_depositos_ja_feitos(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    # estado gravado pela versao antiga: capital inicial 19.78, sem inicio_conta_ms
    lb.estado["capital_inicial_conta"] = 19.78
    info.estado["marginSummary"]["accountValue"] = "58.79"
    pedidos = []
    def ledger(a, t):
        pedidos.append(t)
        return [{"delta": {"type": "deposit", "usdc": "19.80"}}, {"delta": {"type": "deposit", "usdc": "39.50"}}]
    info.user_non_funding_ledger_updates = ledger
    lb._sincronizar(bot.agora_ms(rel))
    assert pedidos == [0]                                         # leu desde a criacao da conta
    investido = lb.estado["capital_inicial_conta"] + lb.estado["aportes"]
    assert investido == pytest.approx(59.30)
    assert 58.79 - investido == pytest.approx(-0.51)              # resultado real, nao +39


def test_adota_posicoes_da_corretora_sem_registro_local(tmp_path):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    agora = bot.agora_ms(rel)
    # duas posicoes reais com stop e alvo na corretora, e o state local foi perdido
    info.estado["assetPositions"] = [
        {"position": {"coin": "BTC", "szi": "-0.00026", "entryPx": "76895", "unrealizedPnl": "-0.1"}},
        {"position": {"coin": "ETH", "szi": "-0.0069", "entryPx": "2238.7", "unrealizedPnl": "-0.3"}}]
    info.ordens = [{"coin": "BTC", "oid": 1, "orderType": "Stop Market", "triggerPx": "80309"},
                   {"coin": "BTC", "oid": 2, "orderType": "Take Profit Market", "triggerPx": "66653.1"},
                   {"coin": "ETH", "oid": 3, "orderType": "Stop Market", "triggerPx": "2366.6"},
                   {"coin": "ETH", "oid": 4, "orderType": "Take Profit Market", "triggerPx": "1854.9"}]
    t_entrada = agora - int(3.8 * 86_400_000)
    info.fills = [{"coin": "BTC", "px": "76895", "sz": "0.00026", "dir": "Open Short", "closedPnl": "0",
                   "fee": "0.009", "time": t_entrada}]
    lb._sincronizar(agora)
    pos = {p["moeda"]: p for p in lb.estado["posicoes"]}
    assert set(pos) == {"BTC", "ETH"}
    assert pos["BTC"]["lado"] == "short" and pos["BTC"]["stop"] == 80309 and pos["BTC"]["alvo"] == 66653.1
    assert pos["BTC"]["entrada_ms"] == t_entrada                          # tempo real preservado (8 dias)
    assert len(info.ordens) == 4                                          # nada recolocado em duplicidade
    # nunca abre outra posicao numa moeda que ja tem posicao na corretora
    assert not lb._pode_abrir()                                           # 2 de 2 posicoes ocupadas
    lb.estado["posicoes"] = [p for p in lb.estado["posicoes"] if p["moeda"] != "ETH"]
    assert not lb.abrir("ETH", "long", 2200.0, 30.0, agora)


def test_posicao_sem_protecao_bloqueia_moeda(tmp_path, caplog):
    lb, ex, info, exch, api, rel = novo_livebot(tmp_path)
    info.estado["assetPositions"] = [{"position": {"coin": "BTC", "szi": "0.0002", "entryPx": "100"}}]
    info.ordens = []
    lb._sincronizar(bot.agora_ms(rel))
    assert lb.estado["posicoes"] == [] and "BTC" in lb.bloqueadas
    assert "DESCONHECIDA" in caplog.text
    assert not lb.abrir("BTC", "short", 100.0, 1.0, bot.agora_ms(rel))


def test_otimizador_frequencia_e_ativacao_somada(tmp_path):
    import otimizar_frequencia as of
    import ativar_config
    import random
    random.seed(4)
    ms, dados, uni = of.IV_MS, {}, []
    for k in range(12):
        cs, p = [], 100.0
        for i in range(1500):
            drift = 0.06 if ((i + 37 * k) // 300) % 2 == 0 else -0.06
            a, p = p, max(1, p + drift + random.gauss(0, 0.5))
            cs.append({"t": i * ms, "T": (i + 1) * ms - 1, "o": a, "h": max(a, p) + .2, "l": min(a, p) - .2,
                       "c": p, "v": 100})
        dados["C%02d" % k] = cs
        uni.append(["C%02d" % k, 9e8 / (k + 1)])
    linhas = []
    escolhida, m_teste, aprovada = of.otimizar({"universo": uni, "dados": dados}, 0.6, linhas.append)
    assert escolhida is not None and any("COMPARACAO" in l for l in linhas)
    # ativacao soma com a configuracao ja ativa (ex.: estrategia) em vez de apagar
    destino = str(tmp_path / "config_ativa.json")
    json.dump({"estrategia": "tendencia"}, open(destino, "w"))
    origem = str(tmp_path / "freq.json")
    json.dump({"parametros": escolhida, "aprovada": True, "cfg_para_o_bot": {"max_posicoes": 4}}, open(origem, "w"))
    ok, _ = ativar_config.ativar(origem, destino)
    assert ok and json.load(open(destino)) == {"estrategia": "tendencia", "max_posicoes": 4}
