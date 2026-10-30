#!/usr/bin/env python3
"""
Execucao de ordens na Hyperliquid (TESTNET ou REAL) via SDK oficial.

Seguranca:
- A chave vem SO do arquivo .env do seu computador (nunca do codigo, nunca impressa no log).
- Use uma API WALLET (carteira de agente): ela pode operar, mas NAO pode sacar.
  Se a chave informada for da carteira principal, o bot se recusa a rodar.
- Modo REAL exige, alem de MODO=real, a linha CONFIRMO_DINHEIRO_REAL=SIM no .env.

Requer Python 3.9+ (exigencia do SDK oficial: pip install hyperliquid-python-sdk).
"""

import math
import os

URLS = {"testnet": "https://api.hyperliquid-testnet.xyz", "real": "https://api.hyperliquid.xyz"}


class ErroExecucao(Exception):
    pass


# ---------------------------------------------------------------------------
# .env e validacoes (sem dependencias externas)
# ---------------------------------------------------------------------------
def ler_env(caminho):
    """Le KEY=VALOR de um arquivo .env. Linhas vazias e com # sao ignoradas."""
    env = {}
    if not os.path.exists(caminho):
        return env
    with open(caminho, encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def modo_do_env(env):
    modo = (env.get("MODO") or "paper").strip().lower()
    if modo not in ("paper", "testnet", "real"):
        raise ErroExecucao("MODO invalido no .env: use paper, testnet ou real")
    if modo == "real" and env.get("CONFIRMO_DINHEIRO_REAL", "").strip().upper() != "SIM":
        raise ErroExecucao("MODO=real exige a linha CONFIRMO_DINHEIRO_REAL=SIM no .env")
    return modo


def validar_credenciais(env, endereco_da_chave):
    """endereco_da_chave = endereco derivado da chave privada (feito pelo eth_account)."""
    conta = (env.get("HL_ACCOUNT_ADDRESS") or "").strip()
    if not conta.startswith("0x") or len(conta) != 42:
        raise ErroExecucao("HL_ACCOUNT_ADDRESS ausente ou invalido no .env (endereco 0x... da sua carteira principal)")
    if endereco_da_chave.lower() == conta.lower():
        raise ErroExecucao("A chave no .env e da CARTEIRA PRINCIPAL. Por seguranca o bot se recusa. "
                           "Crie uma API Wallet (More > API no site da Hyperliquid) e use a chave dela.")
    return conta


# ---------------------------------------------------------------------------
# Arredondamentos exigidos pela Hyperliquid
# ---------------------------------------------------------------------------
def arredondar_tamanho(sz, sz_decimals):
    """Sempre para baixo, nas casas decimais permitidas da moeda."""
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f


def arredondar_preco(px, sz_decimals):
    """Perpetuos: no maximo 5 algarismos significativos e (6 - szDecimals) casas decimais."""
    if px >= 100_000:
        return float(round(px))
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def _status(resp):
    """Extrai o primeiro status de uma resposta de ordem ou levanta ErroExecucao."""
    if not resp or resp.get("status") != "ok":
        raise ErroExecucao("corretora recusou: %s" % (resp,))
    statuses = resp["response"]["data"]["statuses"]
    st = statuses[0] if statuses else {}
    if "error" in st:
        raise ErroExecucao("ordem recusada: %s" % st["error"])
    return st


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class ExecutorHyperliquid:
    """Camada fina sobre o SDK. Tudo que fala com a corretora passa por aqui."""

    def __init__(self, modo, env, info=None, exchange=None, endereco_agente=None):
        self.modo = modo
        self.url = URLS[modo]
        if info is None or exchange is None:      # criacao real (testes injetam falsos)
            try:
                from eth_account import Account
                from hyperliquid.exchange import Exchange
                from hyperliquid.info import Info
            except ImportError:
                raise ErroExecucao("SDK da Hyperliquid nao instalado. Use Python 3.9+ e rode 1_instalar.bat")
            chave = (env.get("HL_SECRET_KEY") or "").strip()
            if not chave:
                raise ErroExecucao("HL_SECRET_KEY ausente no .env (chave da API Wallet)")
            agente = Account.from_key(chave)
            endereco_agente = agente.address
            self.conta = validar_credenciais(env, endereco_agente)
            info = Info(self.url, skip_ws=True)
            exchange = Exchange(agente, self.url, account_address=self.conta)
        else:
            self.conta = validar_credenciais(env, endereco_agente or "0x" + "0" * 40)
        self.info, self.ex = info, exchange
        self._meta = {a["name"]: a for a in self.info.meta()["universe"]}
        self.modo_conta = self._ler_modo_conta()

    def _ler_modo_conta(self):
        """'unifiedAccount', 'portfolioMargin', 'disabled'/'default' ou None se nao der para saber."""
        try:
            r = self.info.query_user_abstraction_state(self.conta)
        except Exception:
            return None
        if isinstance(r, dict):
            r = r.get("abstraction") or r.get("type") or r.get("state")
        return str(r) if r else None

    def unificada(self, perp_valor=None, spot_total=None):
        """Conta 'Unified': o USDC fica no saldo spot e serve de margem para os perpetuos."""
        if self.modo_conta:
            return self.modo_conta in ("unifiedAccount", "portfolioMargin")
        # modo desconhecido: se os perpetuos estao zerados e ha USDC no spot, trata como unificada
        return (perp_valor or 0) < 0.01 and (spot_total or 0) > 0

    def saldo_spot_usdc(self):
        try:
            st = self.info.spot_user_state(self.conta)
        except Exception:
            return 0.0, 0.0
        for b in st.get("balances", []):
            if b.get("coin") == "USDC":
                return float(b.get("total") or 0), float(b.get("hold") or 0)
        return 0.0, 0.0

    def detalhes_saldo(self):
        """Todos os numeros de saldo, para diagnostico."""
        st = self.info.user_state(self.conta)
        total, hold = self.saldo_spot_usdc()
        upnl = sum(float(ap["position"].get("unrealizedPnl") or 0) for ap in st.get("assetPositions", []))
        return {"modo_conta": self.modo_conta or "desconhecido",
                "perps_valor": float(st["marginSummary"]["accountValue"]),
                "perps_livre": float(st.get("withdrawable", 0)),
                "spot_usdc_total": total, "spot_usdc_em_uso": hold, "pnl_aberto": upnl}

    # ---------- leitura ----------
    def sz_decimals(self, coin):
        return int(self._meta[coin]["szDecimals"])

    def alavancagem_max(self, coin):
        return int(self._meta[coin].get("maxLeverage", 1))

    def conta_valor(self):
        """(valor total da conta, valor livre para margem), nos dois modos de conta."""
        d = self.detalhes_saldo()
        if self.unificada(d["perps_valor"], d["spot_usdc_total"]):
            return (d["spot_usdc_total"] + d["pnl_aberto"],
                    max(d["spot_usdc_total"] - d["spot_usdc_em_uso"], 0.0))
        return d["perps_valor"], d["perps_livre"]

    def posicoes(self):
        st = self.info.user_state(self.conta)
        out = {}
        for ap in st.get("assetPositions", []):
            p = ap["position"]
            szi = float(p["szi"])
            if szi != 0:
                out[p["coin"]] = {"szi": szi, "entryPx": float(p["entryPx"] or 0),
                                  "pnl": float(p.get("unrealizedPnl") or 0)}
        return out

    def ordens_abertas(self, coin=None):
        return [o for o in self.info.frontend_open_orders(self.conta) if coin is None or o["coin"] == coin]

    def fills_desde(self, coin, t_ms):
        return [f for f in self.info.user_fills_by_time(self.conta, int(t_ms)) if f["coin"] == coin]

    # ---------- ordens ----------
    def definir_alavancagem(self, coin, lev):
        lev = max(1, min(int(lev), self.alavancagem_max(coin)))
        r = self.ex.update_leverage(lev, coin, False)          # margem ISOLADA por posicao
        if not r or r.get("status") != "ok":
            raise ErroExecucao("nao consegui ajustar alavancagem de %s: %s" % (coin, r))
        return lev

    def abrir_mercado(self, coin, lado, sz, slippage_max):
        st = _status(self.ex.market_open(coin, lado == "long", sz, None, slippage_max))
        if "filled" not in st:
            raise ErroExecucao("ordem de entrada nao executada: %s" % st)
        return float(st["filled"]["avgPx"]), float(st["filled"]["totalSz"])

    def colocar_protecao(self, coin, lado, sz, preco_gatilho, tipo):
        """Stop (tipo='sl') ou alvo (tipo='tp') como ordem na corretora, reduce-only.
        Fica valendo mesmo com o bot/notebook desligado."""
        dec = self.sz_decimals(coin)
        fecha_comprando = lado == "short"
        gatilho = arredondar_preco(preco_gatilho, dec)
        # limite com folga de 5% para garantir a execucao quando o gatilho disparar
        limite = arredondar_preco(gatilho * (1.05 if fecha_comprando else 0.95), dec)
        st = _status(self.ex.order(coin, fecha_comprando, sz, limite,
                                   {"trigger": {"triggerPx": gatilho, "isMarket": True, "tpsl": tipo}},
                                   True))
        if "resting" in st:
            return int(st["resting"]["oid"])
        if "filled" in st:                         # disparou na hora (preco ja passou do gatilho)
            return None
        raise ErroExecucao("protecao %s nao aceita: %s" % (tipo, st))

    def fechar_mercado(self, coin, slippage_max):
        r = self.ex.market_close(coin, None, None, slippage_max)
        if r is None:
            return None                            # nao havia posicao
        st = _status(r)
        return float(st["filled"]["avgPx"]) if "filled" in st else None

    def cancelar_ordens(self, coin):
        n = 0
        for o in self.ordens_abertas(coin):
            r = self.ex.cancel(coin, int(o["oid"]))
            if r and r.get("status") == "ok":
                n += 1
        return n
