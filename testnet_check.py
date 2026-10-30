#!/usr/bin/env python3
"""
Verificador da TESTNET (dinheiro de teste). Usa o .env da pasta, mas SEMPRE na testnet.
  python testnet_check.py               -> confere chave, saldo, posicoes e ordens (nao opera)
  python testnet_check.py --ordem-teste -> abre ~US$ 12 em BTC, coloca stop e alvo na corretora,
                                           confere, cancela e fecha. Pede confirmacao antes.
A chave privada nunca e impressa.
"""
import os
import sys
import time

import execucao

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    env = execucao.ler_env(os.path.join(BASE, ".env"))
    if not env:
        print("Arquivo .env nao encontrado. Rode 16_configurar_env.bat primeiro.")
        sys.exit(1)
    try:
        ex = execucao.ExecutorHyperliquid("testnet", env)
    except Exception as e:
        print("FALHA: %s" % e)
        sys.exit(1)
    print("Conectado na TESTNET. Conta: %s...%s" % (ex.conta[:6], ex.conta[-4:]))
    valor, livre = ex.conta_valor()
    print("Saldo da conta: US$ %.2f | livre para margem: US$ %.2f" % (valor, livre))
    pos = ex.posicoes()
    print("Posicoes abertas: %s" % (", ".join("%s %+g" % (m, p["szi"]) for m, p in pos.items()) or "nenhuma"))
    ordens = ex.ordens_abertas()
    print("Ordens abertas: %d" % len(ordens))
    if valor < 15:
        print("ATENCAO: saldo baixo. Pegue USDC de teste no faucet da testnet (app.hyperliquid-testnet.xyz).")
    if "--ordem-teste" not in sys.argv:
        print("OK: chave e conexao funcionando. Para testar ordens, rode 18_ordem_teste_testnet.bat")
        return

    if input("Abrir uma ordem de TESTE de ~US$ 12 em BTC na TESTNET? Digite SIM: ").strip().upper() != "SIM":
        print("Cancelado.")
        return
    coin = "BTC"
    mid = float(ex.info.all_mids()[coin])
    sz = execucao.arredondar_tamanho(12.0 / mid, ex.sz_decimals(coin))
    if sz * mid < 10:
        sz = execucao.arredondar_tamanho(15.0 / mid, ex.sz_decimals(coin))
    print("1) Alavancagem: %dx isolada" % ex.definir_alavancagem(coin, 4))
    px, sz_exec = ex.abrir_mercado(coin, "long", sz, 0.01)
    print("2) ABRIU LONG %g BTC a %.1f (nocional US$ %.2f)" % (sz_exec, px, sz_exec * px))
    try:
        oid_sl = ex.colocar_protecao(coin, "long", sz_exec, px * 0.97, "sl")
        oid_tp = ex.colocar_protecao(coin, "long", sz_exec, px * 1.03, "tp")
        print("3) Stop (-3%%) oid %s e alvo (+3%%) oid %s colocados na corretora" % (oid_sl, oid_tp))
        time.sleep(3)
        abertas = [o for o in ex.ordens_abertas(coin)]
        print("4) Ordens de protecao visiveis na corretora: %d (esperado: 2)" % len(abertas))
    finally:
        n = ex.cancelar_ordens(coin)
        saida = ex.fechar_mercado(coin, 0.01)
        print("5) Canceladas %d ordens e posicao fechada a %s" % (n, "%.1f" % saida if saida else "?"))
    time.sleep(2)
    fills = ex.fills_desde(coin, int(time.time() * 1000) - 10 * 60_000)
    taxas = sum(float(f.get("fee") or 0) for f in fills)
    pnl = sum(float(f.get("closedPnl") or 0) for f in fills)
    print("6) Execucoes: %d | PnL bruto US$ %+.4f | taxas US$ %.4f" % (len(fills), pnl, taxas))
    restantes = ex.posicoes().get(coin)
    print("RESULTADO: %s" % ("OK, ciclo completo funcionou na testnet." if not restantes
                             else "ATENCAO: ainda ha posicao em BTC, feche pelo site da testnet."))


if __name__ == "__main__":
    main()
