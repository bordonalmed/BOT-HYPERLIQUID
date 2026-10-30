#!/usr/bin/env python3
"""
Dashboard local do bot de paper trading (somente leitura).

Abre em http://localhost:8050 e le state.json, trades.csv e bot.log da pasta.
Nao envia nada para fora do computador e nao mexe no bot.
Uso: python dashboard.py [--porta 8050] [--dir PASTA] [--sem-navegador] [--rede]

--rede: libera o acesso pelo celular na mesma rede Wi-Fi. Quem acessa de outro
aparelho precisa da chave de acesso (fica em dashboard_chave.txt e vai no link/QR).
"""

import argparse
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import bot
import execucao
import report

ONLINE_MAX_S = 120   # sem ciclo ha mais que isso => OFFLINE


def _ler_json(caminho):
    for _ in range(5):
        try:
            with open(caminho, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            time.sleep(0.1)  # arquivo sendo regravado pelo bot
    return None


def _tail(caminho, linhas=40, max_bytes=65536):
    try:
        with open(caminho, "rb") as f:
            f.seek(0, os.SEEK_END)
            tam = f.tell()
            f.seek(max(0, tam - max_bytes))
            dados = f.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError):
        return []
    return [l for l in dados.splitlines() if l.strip()][-linhas:]


def modo_e_cfg(base):
    """Le o MODO do .env (paper/testnet/real) e aponta para os arquivos desse modo."""
    modo = (execucao.ler_env(os.path.join(base, ".env")).get("MODO") or "paper").strip().lower()
    if modo not in ("testnet", "real"):
        return "paper", bot.CFG
    return modo, dict(bot.CFG, arquivo_estado="state_%s.json" % modo, arquivo_trades="trades_%s.csv" % modo)


def coletar(base, cfg=bot.CFG, agora=None, modo="paper"):
    agora = agora or time.time()
    agora_ms = int(agora * 1000)
    estado = _ler_json(os.path.join(base, cfg["arquivo_estado"])) or {}
    cap_ini = cfg["capital_inicial"]
    ao_vivo = estado.get("modo") in ("testnet", "real")
    if ao_vivo:
        # capital investido = saldo quando o bot comecou + depositos - saques
        cap_ini = float(estado.get("capital_inicial_conta", cap_ini)) + float(estado.get("aportes", 0.0))
    capital = float(estado.get("capital", cap_ini))

    # ---------- posicoes abertas ----------
    posicoes = []
    for p in estado.get("posicoes", []):
        mid = p.get("ultimo_mid") or p["entrada"]
        saida = bot.preco_execucao(mid, p["lado"], "saida", p.get("slip", cfg["slippage_por_lado"]))
        pnl, _ = bot.pnl_liquido(p["lado"], p["entrada"], saida, p["tamanho"], cfg["taxa_por_lado"])
        if p.get("pnl_corretora") is not None:
            pnl = p["pnl_corretora"]                      # valor real informado pela corretora
        posicoes.append({
            "moeda": p["moeda"], "lado": p["lado"], "entrada": p["entrada"], "atual": mid,
            "stop": p["stop"], "alvo": p["alvo"], "tamanho": p["tamanho"],
            "nocional": p["tamanho"] * p["entrada"], "pnl": pnl,
            "movel": bool(p.get("trailing")),
            "minutos": (agora_ms - p["entrada_ms"]) / 60000.0,
        })
    # ao vivo, o saldo da corretora ja inclui o resultado das posicoes abertas
    equity = capital if ao_vivo else capital + sum(p["pnl"] for p in posicoes)

    # ---------- operacoes fechadas ----------
    trades = []
    caminho_trades = os.path.join(base, cfg["arquivo_trades"])
    if os.path.exists(caminho_trades):
        try:
            trades = report.carregar(caminho_trades)
        except Exception:
            trades = []
    if trades:
        cap_ini = trades[0]["capital"] - trades[0]["pnl"]
    pnls = [t["pnl"] for t in trades]
    ganhos = [x for x in pnls if x > 0]
    perdas = [x for x in pnls if x < 0]
    n = len(trades)
    dd = report.drawdown_max_pct(cap_ini, [t["capital"] for t in trades] + [equity])

    por_moeda = defaultdict(lambda: {"ops": 0, "pnl": 0.0, "ganhos": 0})
    por_semana = defaultdict(float)
    for t in trades:
        m = por_moeda[t["moeda"]]
        m["ops"] += 1
        m["pnl"] += t["pnl"]
        m["ganhos"] += 1 if t["pnl"] > 0 else 0
        iso = t["dt"].isocalendar()
        por_semana[(iso[0], iso[1])] += t["pnl"]
    seq_sem = report.semanas_seguidas_positivas(por_semana) if por_semana else 0

    curva = [{"t": None, "v": cap_ini}]
    curva += [{"t": t["horario_utc"], "v": t["capital"]} for t in trades]
    curva.append({"t": "agora", "v": equity})

    ultimas = [{
        "hora": t["horario_utc"], "moeda": t["moeda"], "lado": t["lado"],
        "entrada": float(t["entrada"]), "saida": float(t["saida"]),
        "motivo": t["motivo"], "pnl": t["pnl"], "capital": t["capital"],
    } for t in reversed(trades[-25:])]

    # ---------- dia ----------
    eq_dia = estado.get("equity_inicio_dia") or equity
    perda_dia = max(0.0, (eq_dia - equity) / eq_dia * 100) if eq_dia else 0.0

    ultimo = estado.get("ultimo_ciclo_ms")
    online = bool(ultimo) and (agora_ms - ultimo) / 1000.0 <= ONLINE_MAX_S

    return {
        "agora_ms": agora_ms,
        "modo": estado.get("modo") or modo,
        "status": {
            "online": online, "ultimo_ciclo_ms": ultimo,
            "ultimo_ciclo_ok": estado.get("ultimo_ciclo_ok"),
            "pausado": bool(estado.get("pausado")),
        },
        "capital_inicial": cap_ini, "capital": capital, "equity": equity,
        "pnl_total": equity - cap_ini, "pnl_realizado": sum(pnls),
        "taxas": sum(t["taxas"] for t in trades),
        "operacoes": n, "acerto": (len(ganhos) / n * 100) if n else None,
        "ganho_medio": (sum(ganhos) / len(ganhos)) if ganhos else None,
        "perda_media": (sum(perdas) / len(perdas)) if perdas else None,
        "fator_lucro": (sum(ganhos) / abs(sum(perdas))) if perdas else None,
        "seq_perdas": report.maior_sequencia_perdas(pnls),
        "drawdown": dd,
        "dia": {
            "data": estado.get("dia"), "trades": estado.get("trades_hoje", 0),
            "max_trades": cfg["max_trades_dia"], "perda_pct": perda_dia,
            "limite_pct": cfg["perda_diaria_max"] * 100,
        },
        "max_posicoes": cfg["max_posicoes"],
        "tempo_max_min": cfg["tempo_max_min"],
        "estrategia": "%s · %s" % (cfg.get("estrategia", "tendencia").upper(), cfg["intervalo"].upper()),
        "alavancagem_max": cfg["alavancagem_max"],
        "radar": estado.get("radar", [])[:15],
        "radar_ms": estado.get("radar_ms"),
        "posicoes": posicoes,
        "universo": estado.get("universo", []),
        "curva": curva,
        "ultimas": ultimas,
        "por_moeda": sorted(
            [{"moeda": k, **v} for k, v in por_moeda.items()],
            key=lambda x: x["pnl"], reverse=True),
        "criterios": {
            "ops": n, "ops_min": report.MIN_TRADES,
            "semanas": len(por_semana), "seq_semanas": seq_sem,
            "seq_min": report.MIN_SEMANAS_SEGUIDAS,
            "dd": dd, "dd_max": report.MAX_DD_PCT,
        },
        "log": _tail(os.path.join(base, "bot.log")),
    }


HTML = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEON//DESK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#06050c; --bg2:#0d0b1a; --panel:rgba(18,14,36,.82); --line:#2a2350;
  --cyan:#00f0ff; --mag:#ff2bd6; --yel:#f5ff3b; --green:#39ff8f; --red:#ff3a68;
  --txt:#d9e4ff; --dim:#7d86b3; --mono:'JetBrains Mono',Consolas,monospace; --disp:'Orbitron','Segoe UI',sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:14px}
body{min-height:100vh;
  background:
    radial-gradient(1200px 600px at 85% -10%,rgba(255,43,214,.18),transparent 60%),
    radial-gradient(900px 500px at -10% 110%,rgba(0,240,255,.14),transparent 60%),
    linear-gradient(rgba(0,240,255,.05) 1px,transparent 1px) 0 0/40px 40px,
    linear-gradient(90deg,rgba(0,240,255,.05) 1px,transparent 1px) 0 0/40px 40px,
    var(--bg);}
body:after{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.18) 0 1px,transparent 1px 3px);mix-blend-mode:multiply}
body.noscan:after{display:none}
.wrap{max-width:1500px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
h1{font-family:var(--disp);font-weight:900;font-size:clamp(22px,4vw,38px);margin:0;letter-spacing:.08em;
  color:#fff;text-shadow:0 0 6px var(--cyan),0 0 22px rgba(0,240,255,.6),3px 0 0 rgba(255,43,214,.7),-3px 0 0 rgba(0,240,255,.5)}
h1 small{display:block;font-size:.36em;letter-spacing:.35em;color:var(--mag);text-shadow:0 0 10px var(--mag);margin-top:4px}
.badges{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.badge{font-family:var(--disp);font-size:12px;letter-spacing:.15em;padding:8px 14px;border:1px solid;clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%,0 8px)}
.on{color:var(--green);border-color:var(--green);box-shadow:inset 0 0 12px rgba(57,255,143,.3);background:rgba(57,255,143,.08)}
.off{color:var(--red);border-color:var(--red);background:rgba(255,58,104,.1);animation:blink 1.2s steps(2) infinite}
.paused{color:var(--yel);border-color:var(--yel);background:rgba(245,255,59,.08)}
.sim{color:var(--cyan);border-color:var(--cyan);background:rgba(0,240,255,.06)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 10px currentColor;margin-right:8px;vertical-align:middle}
@keyframes blink{50%{opacity:.45}}
.meta{color:var(--dim);font-size:12px}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:14px}
.main{grid-template-columns:2fr 1fr}
.main2{grid-template-columns:3fr 2fr}
@media (max-width:980px){.main,.main2{grid-template-columns:1fr}}
.panel{position:relative;background:var(--panel);border:1px solid var(--line);padding:16px;backdrop-filter:blur(6px);
  clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,16px 100%,0 calc(100% - 16px));min-width:0}
.panel:before{content:"";position:absolute;left:0;top:0;width:60px;height:2px;background:var(--cyan);box-shadow:0 0 10px var(--cyan)}
.panel.m:before{background:var(--mag);box-shadow:0 0 10px var(--mag)}
.panel.y:before{background:var(--yel);box-shadow:0 0 10px var(--yel)}
h2{font-family:var(--disp);font-size:12px;letter-spacing:.25em;color:var(--cyan);margin:0 0 12px;font-weight:700;text-transform:uppercase}
.m h2{color:var(--mag)} .y h2{color:var(--yel)}
.kpi .lbl{font-size:11px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase}
.kpi .val{font-family:var(--disp);font-size:clamp(20px,2.4vw,28px);font-weight:700;margin-top:8px;color:#fff;text-shadow:0 0 12px rgba(0,240,255,.45);white-space:nowrap}
.kpi .sub{font-size:12px;color:var(--dim);margin-top:6px}
.pos{color:var(--green)!important;text-shadow:0 0 10px rgba(57,255,143,.5)!important}
.neg{color:var(--red)!important;text-shadow:0 0 10px rgba(255,58,104,.5)!important}
.bar{height:8px;background:#151230;border:1px solid var(--line);margin-top:10px;position:relative;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--cyan),var(--mag));box-shadow:0 0 12px var(--mag)}
.bar.risk i{background:linear-gradient(90deg,var(--yel),var(--red))}
svg{display:block;width:100%;height:auto}
.empty{color:var(--dim);padding:30px 0;text-align:center;letter-spacing:.1em}
.tbl{width:100%;overflow-x:auto}
#trades .tbl{max-height:430px;overflow-y:auto}
#trades th{position:sticky;top:0;background:#120e24}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-weight:600;color:var(--dim);text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
td{padding:7px 8px;text-align:right;border-bottom:1px solid rgba(42,35,80,.5);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tr:hover td{background:rgba(0,240,255,.05)}
.tag{font-size:11px;padding:2px 8px;border:1px solid;letter-spacing:.1em}
.long{color:var(--green);border-color:rgba(57,255,143,.5)}
.short{color:var(--mag);border-color:rgba(255,43,214,.5)}
.crit{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px dashed var(--line);gap:10px}
.crit:last-of-type{border-bottom:0}
.crit b{font-family:var(--disp);font-size:12px;letter-spacing:.12em}
.chk{font-family:var(--disp);font-size:11px;padding:3px 8px;border:1px solid}
.ok{color:var(--green);border-color:var(--green)} .no{color:var(--dim);border-color:var(--line)} .bad{color:var(--red);border-color:var(--red)}
.verdict{margin-top:12px;padding:12px;border:1px solid var(--line);font-size:12.5px;line-height:1.5}
.coins{display:flex;flex-wrap:wrap;gap:8px}
.coin{border:1px solid var(--line);padding:6px 10px;font-size:12px;background:rgba(0,240,255,.04)}
.coin span{color:var(--dim);margin-left:6px}
.coin.open{border-color:var(--mag);box-shadow:0 0 10px rgba(255,43,214,.35)}
.term{background:#030208;border:1px solid var(--line);padding:12px;height:320px;overflow:auto;font-size:12px;line-height:1.55}
.term div{white-space:pre-wrap;word-break:break-word}
.l-err{color:var(--red)} .l-warn{color:var(--yel)} .l-open{color:var(--green)} .l-close{color:var(--mag)} .l-info{color:#9fb0e6}
.row{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;padding:6px 0}
.row span:first-child{color:var(--dim)}
.hbar{display:grid;grid-template-columns:70px 1fr 90px;gap:10px;align-items:center;font-size:12.5px;padding:5px 0}
.hbar .track{height:10px;background:#120f26;position:relative}
.hbar .track i{position:absolute;top:0;bottom:0}
footer{margin-top:18px;color:var(--dim);font-size:11px;text-align:center;letter-spacing:.1em}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>NEON//DESK<small id="sub">HYPERLIQUID · PAPER TRADING</small></h1>
    <div class="badges">
      <span class="badge sim" id="modo">SIMULACAO · SEM DINHEIRO REAL</span>
      <span id="st" class="badge off"><span class="dot"></span>CARREGANDO</span>
      <button id="btnCel" class="badge sim" style="display:none;cursor:pointer;background:transparent">CELULAR</button>
    </div>
  </header>
  <div class="meta" id="meta" style="margin:-8px 0 14px"></div>
  <div id="celCard" class="panel m" style="display:none;margin-bottom:14px">
    <h2>Abrir no celular</h2>
    <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center">
      <div id="qr" style="background:#fff;padding:10px;line-height:0"></div>
      <div style="min-width:0;flex:1">
        <div class="meta">Celular no MESMO Wi-Fi do notebook. Aponte a camera para o QR Code ou digite:</div>
        <div id="celUrl" style="color:var(--cyan);font-size:15px;margin:8px 0;word-break:break-all"></div>
        <div class="meta">O link contem a chave de acesso. Nao compartilhe.</div>
      </div>
    </div>
  </div>

  <section class="grid kpis" id="kpis"></section>

  <section class="grid main" style="margin-bottom:14px">
    <div class="panel"><h2>Curva de capital</h2><div id="curva"></div></div>
    <div class="panel y"><h2>Criterios p/ dinheiro real</h2><div id="crit"></div></div>
  </section>

  <section class="grid main2" style="margin-bottom:14px">
    <div class="panel m"><h2>Posicoes abertas</h2><div id="posicoes"></div></div>
    <div class="panel"><h2>Risco do dia</h2><div id="dia"></div></div>
  </section>

  <div class="panel m" style="margin-bottom:14px"><h2>Radar de assimetria</h2><div id="radar"></div></div>

  <section class="grid main2" style="margin-bottom:14px">
    <div class="panel"><h2>Ultimas operacoes</h2><div id="trades"></div></div>
    <div class="grid" style="align-content:start">
      <div class="panel m"><h2>Resultado por moeda</h2><div id="moedas"></div></div>
      <div class="panel"><h2>Universo monitorado</h2><div id="universo"></div></div>
    </div>
  </section>

  <div class="panel y"><h2>Terminal // bot.log</h2><div class="term" id="log"></div></div>
  <footer>Atualiza a cada 5 s · dados locais de state.json, trades.csv e bot.log · horarios em UTC</footer>
</div>
<script>
const $=id=>document.getElementById(id);
const usd=(v,s)=>v==null?'—':(s&&v>0?'+':'')+(v<0?'-':'')+'US$ '+Math.abs(v).toFixed(2);
const pct=(v,s)=>v==null?'—':(s&&v>0?'+':'')+v.toFixed(2)+'%';
const cls=v=>v>0?'pos':v<0?'neg':'';
const px=v=>{if(v==null)return'—';const a=Math.abs(v);return v.toFixed(a>=1000?1:a>=10?3:a>=1?4:6)};
const dur=m=>m>=1440?(m/1440).toFixed(1)+'d':m>=60?(m/60).toFixed(1)+'h':Math.floor(m)+'min';
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function kpi(lbl,val,sub,c,bar,risk){
  return `<div class="panel kpi"><div class="lbl">${lbl}</div><div class="val ${c||''}">${val}</div>`+
    (sub?`<div class="sub">${sub}</div>`:'')+
    (bar!=null?`<div class="bar ${risk?'risk':''}"><i style="width:${Math.max(0,Math.min(100,bar))}%"></i></div>`:'')+`</div>`;
}

function curva(d){
  const pts=d.curva.map(p=>p.v);
  if(pts.length<3){return '<div class="empty">AGUARDANDO A PRIMEIRA OPERACAO FECHADA...</div>'+
    `<div class="row"><span>Capital inicial</span><span>${usd(d.capital_inicial)}</span></div>`}
  const W=900,H=300,P=36;
  let mn=Math.min(...pts),mx=Math.max(...pts);if(mx-mn<1e-9){mx+=1;mn-=1}
  const pad=(mx-mn)*.12;mn-=pad;mx+=pad;
  const x=i=>P+(W-2*P)*i/(pts.length-1), y=v=>H-P-(H-2*P)*(v-mn)/(mx-mn);
  const line=pts.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const base=y(d.capital_inicial);
  let grid='';for(let k=0;k<=4;k++){const v=mn+(mx-mn)*k/4,yy=y(v);
    grid+=`<line x1="${P}" x2="${W-P}" y1="${yy}" y2="${yy}" stroke="#2a2350" stroke-dasharray="3 6"/>`+
      `<text x="${W-P+4}" y="${yy+4}" fill="#7d86b3" font-size="11" font-family="JetBrains Mono">${v.toFixed(2)}</text>`}
  const last=pts[pts.length-1],col=last>=d.capital_inicial?'#39ff8f':'#ff3a68';
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Curva de capital">
  <defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#00f0ff" stop-opacity=".35"/><stop offset="1" stop-color="#ff2bd6" stop-opacity="0"/></linearGradient>
  <filter id="gl"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
  ${grid}
  <line x1="${P}" x2="${W-P}" y1="${base}" y2="${base}" stroke="#f5ff3b" stroke-opacity=".5"/>
  <text x="${P+4}" y="${base-6}" fill="#f5ff3b" font-size="10" font-family="JetBrains Mono" opacity=".8">INICIAL ${d.capital_inicial.toFixed(2)}</text>
  <polygon points="${x(0)},${H-P} ${line} ${x(pts.length-1)},${H-P}" fill="url(#ga)"/>
  <polyline points="${line}" fill="none" stroke="#00f0ff" stroke-width="2.5" filter="url(#gl)"/>
  <circle cx="${x(pts.length-1)}" cy="${y(last)}" r="5" fill="${col}" filter="url(#gl)"/>
  </svg>`;
}

function criterios(d){
  const c=d.criterios;
  const ops=c.ops>=c.ops_min, semOk=c.seq_semanas>=c.seq_min, semDados=c.semanas>=c.seq_min, dd=c.dd<c.dd_max;
  const it=(nome,txt,st,prog)=>`<div class="crit"><div><b>${nome}</b><div class="meta">${txt}</div>`+
    (prog!=null?`<div class="bar" style="width:180px"><i style="width:${Math.min(100,prog)}%"></i></div>`:'')+
    `</div><span class="chk ${st}">${st==='ok'?'OK':st==='bad'?'FALHOU':'PENDENTE'}</span></div>`;
  let h=it('OPERACOES',`${c.ops} de ${c.ops_min}`,ops?'ok':'no',c.ops/c.ops_min*100);
  h+=it('2 SEMANAS +',semDados?`maior sequencia: ${c.seq_semanas} semana(s)`:`dados insuficientes (${c.semanas} semana(s))`,semOk?'ok':(semDados?'bad':'no'));
  h+=it('DRAWDOWN',`${c.dd.toFixed(2)}% (limite ${c.dd_max}%)`,dd?(c.ops?'ok':'no'):'bad');
  let v;
  if(!ops||!semDados) v=d.modo==='paper'?'<span style="color:var(--yel)">DADOS INSUFICIENTES.</span> Continue no paper trading. Nao use dinheiro real ainda.'
    :'<span style="color:var(--yel)">HISTORICO AINDA CURTO.</span> Acompanhe: so da para avaliar o resultado real depois de 40 operacoes e 2 semanas.';
  else if(ops&&semOk&&dd) v='<span style="color:var(--green)">CRITERIOS ATINGIDOS.</span> Isso nao garante lucro futuro.';
  else v='<span style="color:var(--red)">CRITERIOS NAO ATINGIDOS.</span> Nao use dinheiro real.';
  return h+`<div class="verdict">${v}</div>`;
}

function posicoes(d){
  if(!d.posicoes.length) return `<div class="empty">NENHUMA POSICAO ABERTA · 0/${d.max_posicoes}</div>`;
  return '<div class="tbl"><table><tr><th>Moeda</th><th>Lado</th><th>Entrada</th><th>Atual</th><th>Stop</th><th>Alvo</th><th>Nocional</th><th>PnL</th><th>Tempo</th></tr>'+
  d.posicoes.map(p=>`<tr><td><b>${esc(p.moeda)}</b></td><td><span class="tag ${p.lado}">${p.lado.toUpperCase()}</span></td>
   <td>${px(p.entrada)}</td><td>${px(p.atual)}</td><td class="neg">${px(p.stop)}${p.movel?' <span class="meta">movel</span>':''}</td><td class="pos">${p.alvo==null?'livre':px(p.alvo)}</td>
   <td>${usd(p.nocional)}</td><td class="${cls(p.pnl)}">${usd(p.pnl,1)}</td><td>${dur(p.minutos)} / ${dur(d.tempo_max_min)}</td></tr>`).join('')+'</table></div>';
}

function dia(d){
  const x=d.dia, s=d.status;
  return `<div class="row"><span>Dia (UTC)</span><span>${x.data||'—'}</span></div>
  <div class="row"><span>Operacoes hoje</span><span>${x.trades} / ${x.max_trades}</span></div>
  <div class="bar"><i style="width:${x.trades/x.max_trades*100}%"></i></div>
  <div class="row" style="margin-top:10px"><span>Perda do dia</span><span class="${x.perda_pct>0?'neg':''}">${x.perda_pct.toFixed(2)}% / ${x.limite_pct.toFixed(0)}%</span></div>
  <div class="bar risk"><i style="width:${x.perda_pct/x.limite_pct*100}%"></i></div>
  <div class="row" style="margin-top:10px"><span>Posicoes</span><span>${d.posicoes.length} / ${d.max_posicoes}</span></div>
  <div class="row"><span>Status</span><span style="color:${s.pausado?'var(--yel)':'var(--green)'}">${s.pausado?'PAUSADO (limite diario)':'OPERANDO'}</span></div>`;
}

function trades(d){
  if(!d.ultimas.length) return '<div class="empty">SEM OPERACOES FECHADAS AINDA</div>';
  return '<div class="tbl"><table><tr><th>Saida (UTC)</th><th>Moeda</th><th>Lado</th><th>Entrada</th><th>Saida</th><th>Motivo</th><th>PnL</th><th>Capital</th></tr>'+
  d.ultimas.map(t=>`<tr><td>${esc(t.hora.slice(5,16))}</td><td><b>${esc(t.moeda)}</b></td><td><span class="tag ${t.lado}">${t.lado.toUpperCase()}</span></td>
  <td>${px(t.entrada)}</td><td>${px(t.saida)}</td><td>${esc(t.motivo)}</td><td class="${cls(t.pnl)}">${usd(t.pnl,1)}</td><td>${t.capital.toFixed(2)}</td></tr>`).join('')+'</table></div>';
}

function moedas(d){
  if(!d.por_moeda.length) return '<div class="empty">—</div>';
  const mx=Math.max(...d.por_moeda.map(m=>Math.abs(m.pnl)),1e-9);
  return d.por_moeda.map(m=>{const w=Math.abs(m.pnl)/mx*50;
    const st=m.pnl>=0?`left:50%;width:${w}%;background:var(--green);box-shadow:0 0 8px var(--green)`:`right:50%;width:${w}%;background:var(--red);box-shadow:0 0 8px var(--red)`;
    return `<div class="hbar"><b>${esc(m.moeda)}</b><div class="track"><i style="${st}"></i></div><span class="${cls(m.pnl)}" style="text-align:right">${usd(m.pnl,1)}</span></div>`}).join('');
}

function radar(d){
  if(!d.radar.length) return '<div class="empty">O RADAR APARECE APOS O PRIMEIRO CANDLE FECHADO (ATE 4H)...</div>';
  const ha=d.radar_ms?Math.round((d.agora_ms-d.radar_ms)/60000):null;
  const fx=(v,suf)=>v==null?'—':(v>0?'+':'')+v+suf;
  return `<div class="meta" style="margin:-4px 0 10px">Informativo: aponta moedas com potencial de movimento grande. NAO e sinal de entrada validado. ${ha!=null?'Atualizado ha '+ha+' min.':''}</div>`+
  '<div class="tbl"><table><tr><th>Moeda</th><th>Nota</th><th>Vies</th><th>Compressao</th><th>Volume</th><th>Forca vs BTC</th><th>Ate romper</th><th>Tendencia</th><th>Funding/ano</th></tr>'+
  d.radar.map(r=>`<tr><td><b>${esc(r.moeda)}</b></td>
   <td style="min-width:120px"><div class="bar" style="margin:0;display:inline-block;width:70px;vertical-align:middle"><i style="width:${r.nota}%"></i></div> ${r.nota.toFixed(0)}</td>
   <td>${r.lado==='-'?'<span class="meta">neutro</span>':`<span class="tag ${r.lado}">${r.lado.toUpperCase()}</span>`}</td>
   <td class="${r.compressao<=1.2?'pos':''}">${r.compressao.toFixed(2)}x</td>
   <td class="${r.vol_ratio>=1.5?'pos':''}">${r.vol_ratio.toFixed(2)}x</td>
   <td class="${cls(r.rs_pct)}">${fx(r.rs_pct,'%')}</td><td>${r.dist_rompimento_pct.toFixed(2)}%</td>
   <td class="${cls(r.tendencia_pct)}">${fx(r.tendencia_pct,'%')}</td>
   <td class="${Math.abs(r.funding_apr_pct||0)>=50?'neg':''}">${fx(r.funding_apr_pct,'%')}</td></tr>`).join('')+'</table></div>'+
  '<div class="meta" style="margin-top:8px">Compressao 1.00x = bandas no ponto mais apertado de 120 candles · Volume = ultimos 6 candles vs 30 anteriores · Funding alto = muita gente de um lado (risco de squeeze)</div>';
}

function universo(d){
  if(!d.universo.length) return '<div class="empty">AGUARDANDO DADOS DA API...</div>';
  const abertas=new Set(d.posicoes.map(p=>p.moeda));
  return '<div class="coins">'+d.universo.map(([m,v])=>`<div class="coin ${abertas.has(m)?'open':''}">${esc(m)}<span>${(v/1e6).toFixed(0)}M</span></div>`).join('')+'</div>';
}

function logs(d){
  const el=$('log'), noFim=el.scrollTop+el.clientHeight>=el.scrollHeight-20;
  el.innerHTML=d.log.length?d.log.map(l=>{let c='l-info';
    if(/ERROR/.test(l))c='l-err';else if(/WARNING|LIMITE/.test(l))c='l-warn';else if(/ABRIU/.test(l))c='l-open';else if(/FECHOU/.test(l))c='l-close';
    return `<div class="${c}">${esc(l)}</div>`}).join(''):'<div class="l-info">bot.log ainda vazio. Inicie o bot com 3_iniciar_bot.bat</div>';
  if(noFim) el.scrollTop=el.scrollHeight;
}

function render(d){
  const s=d.status, st=$('st'), md=$('modo');
  if(d.modo==='testnet'){md.textContent='TESTNET · DINHEIRO DE TESTE';md.className='badge paused'}
  else if(d.modo==='real'){md.textContent='REAL · DINHEIRO DE VERDADE';md.className='badge off'}
  else{md.textContent='SIMULACAO · SEM DINHEIRO REAL';md.className='badge sim'}
  $('sub').textContent='HYPERLIQUID · '+({paper:'PAPER TRADING',testnet:'TESTNET',real:'CONTA REAL'}[d.modo]||'PAPER TRADING')+' · '+d.estrategia;
  if(s.pausado){st.className='badge paused';st.innerHTML='<span class="dot"></span>PAUSADO'}
  else if(s.online){st.className='badge on';st.innerHTML='<span class="dot"></span>BOT ONLINE'}
  else{st.className='badge off';st.innerHTML='<span class="dot"></span>BOT OFFLINE'}
  const ult=s.ultimo_ciclo_ms?Math.round((d.agora_ms-s.ultimo_ciclo_ms)/1000):null;
  $('meta').textContent=ult==null?'Nenhum ciclo registrado ainda.':`Ultimo ciclo ha ${ult}s · ${s.ultimo_ciclo_ok?'sem erro':'COM ERRO (veja o terminal abaixo)'}`;
  const pt=d.pnl_total, ptp=pt/d.capital_inicial*100;
  $('kpis').innerHTML=
    kpi('Equity',usd(d.equity),`capital realizado ${usd(d.capital)}`)+
    kpi('PnL total',usd(pt,1),pct(ptp,1)+' desde o inicio',cls(pt))+
    kpi('Operacoes',d.operacoes,`taxas pagas ${usd(d.taxas)}`)+
    kpi('Taxa de acerto',d.acerto==null?'—':d.acerto.toFixed(1)+'%',`ganho medio ${usd(d.ganho_medio,1)} · perda media ${usd(d.perda_media,1)}`,'',d.acerto)+
    kpi('Fator de lucro',d.fator_lucro==null?'—':d.fator_lucro.toFixed(2),`maior seq. de perdas: ${d.seq_perdas}`)+
    kpi('Drawdown max',pct(d.drawdown),'limite p/ real: 15% · alav. max '+d.alavancagem_max+'x',d.drawdown>=15?'neg':'',d.drawdown/15*100,1);
  $('curva').innerHTML=curva(d); $('crit').innerHTML=criterios(d); $('posicoes').innerHTML=posicoes(d);
  $('dia').innerHTML=dia(d); $('trades').innerHTML=trades(d); $('moedas').innerHTML=moedas(d);
  $('universo').innerHTML=universo(d); $('radar').innerHTML=radar(d); logs(d);
}

let qrFeito=false;
function celular(d){
  if(!d.url_celular) return;
  const b=$('btnCel'); b.style.display='';
  if(!b.onclick) b.onclick=()=>{const c=$('celCard'),abrir=c.style.display==='none';c.style.display=abrir?'':'none';document.body.classList.toggle('noscan',abrir);desenhaQR(d.url_celular)};
  $('celUrl').textContent=d.url_celular;
}
function desenhaQR(url){
  if(qrFeito) return; qrFeito=true;
  const img=new Image(); img.width=200; img.height=200; img.alt='QR Code';
  img.onerror=()=>{$('qr').outerHTML='<div class="meta" style="max-width:220px">QR indisponivel: rode 1_instalar.bat de novo. Use o link ao lado.</div>'};
  img.src='/qr.svg'; $('qr').appendChild(img);
}
async function tick(){
  try{const r=await fetch('/api/dados'+location.search,{cache:'no-store'});
    if(r.status===403){const st=$('st');st.className='badge off';st.innerHTML='<span class="dot"></span>CHAVE DE ACESSO INVALIDA';return}
    const d=await r.json();render(d);celular(d)}
  catch(e){const st=$('st');st.className='badge off';st.innerHTML='<span class="dot"></span>DASHBOARD SEM CONEXAO'}
}
tick(); setInterval(tick,5000);
</script>
</body>
</html>
"""


def criar_handler(base, chave=None, url_celular=None):
    class Handler(BaseHTTPRequestHandler):
        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _autorizado(self, query):
            if self._local():
                return True
            if not chave:
                return False
            k = parse_qs(query).get("k", [""])[0]
            return hmac.compare_digest(k.encode(), chave.encode())

        def _enviar(self, corpo, tipo):
            dados = corpo.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def do_GET(self):
            partes = urlsplit(self.path)
            if not self._autorizado(partes.query):
                self.send_error(403, "Chave de acesso invalida")
                return
            if partes.path == "/api/dados":
                try:
                    modo, cfg = modo_e_cfg(base)
                    dados = coletar(base, cfg, modo=modo)
                    if self._local() and url_celular:
                        dados["url_celular"] = url_celular
                    self._enviar(json.dumps(dados), "application/json")
                except Exception as e:
                    self.send_error(500, str(e))
            elif partes.path == "/qr.svg" and self._local() and url_celular:
                try:
                    import io
                    import segno
                    buf = io.BytesIO()
                    segno.make(url_celular, error="m").save(buf, kind="svg", scale=6, border=2)
                    self._enviar(buf.getvalue().decode("utf-8"), "image/svg+xml")
                except Exception as e:
                    self.send_error(500, "QR indisponivel: %s" % e)
            elif partes.path in ("/", "/index.html"):
                self._enviar(HTML, "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    return Handler


def ip_local():
    """IP do notebook na rede Wi-Fi (nenhum pacote e enviado)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def obter_chave(base):
    caminho = os.path.join(base, "dashboard_chave.txt")
    if os.path.exists(caminho):
        with open(caminho) as f:
            chave = f.read().strip()
        if chave:
            return chave
    chave = secrets.token_urlsafe(9)
    with open(caminho, "w") as f:
        f.write(chave)
    return chave


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8050)
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--sem-navegador", action="store_true")
    ap.add_argument("--rede", action="store_true", help="libera acesso pelo celular no mesmo Wi-Fi")
    a = ap.parse_args()
    bot.aplicar_config_ativa(a.dir)
    chave = url_celular = None
    host = "127.0.0.1"
    if a.rede:
        host = "0.0.0.0"
        chave = obter_chave(a.dir)
        url_celular = "http://%s:%d/?k=%s" % (ip_local(), a.porta, chave)
    try:
        srv = ThreadingHTTPServer((host, a.porta), criar_handler(a.dir, chave, url_celular))
    except OSError:
        print("A porta %d ja esta em uso. O dashboard provavelmente ja esta aberto: http://localhost:%d"
              % (a.porta, a.porta))
        if not a.sem_navegador:
            webbrowser.open("http://localhost:%d" % a.porta)
        sys.exit(1)
    url = "http://localhost:%d" % a.porta
    print("Dashboard rodando em %s  (feche esta janela para desligar o dashboard; o bot continua)" % url)
    if url_celular:
        print("")
        print("CELULAR (mesmo Wi-Fi): %s" % url_celular)
        print("No notebook, clique no botao CELULAR do painel para ver o QR Code.")
        print("Se o Windows perguntar sobre o Firewall, marque 'Redes privadas' e clique em Permitir.")
    if not a.sem_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
