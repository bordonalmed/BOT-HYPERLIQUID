#!/usr/bin/env python3
"""
Ativa no bot a configuracao do otimizador de rompimento, SO se ela foi APROVADA
na validacao fora da amostra. Uso:
  python ativar_config.py            -> ativa melhor_config_rompimento.json (se aprovada)
  python ativar_config.py --desativar -> volta para a estrategia padrao do bot.py (tendencia)
Reinicie o bot depois (6_parar_bot.bat e 3_iniciar_bot.bat).
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ORIGEM = os.path.join(BASE, "melhor_config_rompimento.json")
DESTINO = os.path.join(BASE, "config_ativa.json")


def ativar(origem=ORIGEM, destino=DESTINO):
    if not os.path.exists(origem):
        return False, "Rode primeiro o otimizador de rompimento (13_otimizar_rompimento.bat)."
    with open(origem) as f:
        dados = json.load(f)
    if not dados.get("aprovada"):
        return False, ("A configuracao NAO foi aprovada na validacao fora da amostra. "
                       "Nada foi ativado; o bot continua com a estrategia atual.")
    with open(destino, "w") as f:
        json.dump(dados["cfg_para_o_bot"], f, indent=2)
    return True, "Configuracao ATIVADA: %s. Reinicie o bot." % json.dumps(dados["parametros"])


def desativar(destino=DESTINO):
    if os.path.exists(destino):
        os.remove(destino)
        return True, "config_ativa.json removido. O bot volta para a estrategia padrao. Reinicie o bot."
    return True, "Nenhuma configuracao extra estava ativa."


if __name__ == "__main__":
    ok, msg = desativar() if "--desativar" in sys.argv else ativar()
    print(msg)
    sys.exit(0 if ok else 1)
