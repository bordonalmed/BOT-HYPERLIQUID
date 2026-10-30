# Bot de Paper Trading – Hyperliquid (perpétuos)

**Simulação apenas.** O bot lê dados públicos da Hyperliquid e simula as operações com US$ 50 fictícios.
Não envia ordens, não assina nada, não usa carteira, seed, chave privada nem API key.

## Arquivos
| Arquivo | O que é |
|---|---|
| `bot.py` | O bot. Toda a configuração fica no dicionário `CFG` no topo do arquivo. |
| `report.py` | Relatório a partir do `trades.csv`. |
| `dashboard.py` | Painel visual local (somente leitura): capital, curva, posições, operações, critérios e log. Só escuta em 127.0.0.1. |
| `backtest.py` | Backtest com o histórico de candles 15m (~52 dias disponíveis na API). Entrada na abertura do candle seguinte ao sinal; stop/alvo pela máxima/mínima; se o candle tocar os dois, conta stop. |
| `otimizar_rompimento.py` | Otimizador da estratégia de rompimento (usa a mesma função de sinal do bot). |
| `ativar_config.py` | Ativa/desativa a configuração aprovada do otimizador de rompimento. |
| `check_api.py` | Teste real de conexão: busca universo, preços e candles e mostra o parse. |
| `tests/test_bot.py` | Testes com dados sintéticos (indicadores, sinais, tamanho, stop/alvo/tempo, limite diário, custos, kill switch). |
| `state.json` | (gerado) Estado salvo: capital, posições abertas, contadores do dia, último candle avaliado por moeda. Permite retomar depois de reiniciar. |
| `trades.csv` | (gerado) Uma linha por operação fechada. |
| `bot.log` | (gerado) Log em português, uma linha por evento. |

## Windows (notebook) – jeito fácil
1. Instale o Python em python.org/downloads e **marque "Add Python to PATH"** na instalação.
2. Baixe o ZIP do projeto e descompacte numa pasta.
3. Clique duas vezes, na ordem:

| Arquivo | Para quê |
|---|---|
| `1_instalar.bat` | Instala as dependências (só na primeira vez). |
| `2_testar_conexao.bat` | Confirma que a API da Hyperliquid responde e que o parse funciona. |
| `3_iniciar_bot.bat` | Liga o bot numa janela minimizada chamada BOT-HYPERLIQUID. |
| `4_ver_log.bat` | Mostra o log ao vivo (fechar essa janela NÃO para o bot). |
| `5_relatorio.bat` | Mostra o relatório. |
| `6_parar_bot.bat` | Cria o arquivo KILL: o bot salva o estado e encerra. |
| `7_rodar_testes.bat` | Roda os testes automáticos (precisa do passo 1). |
| `8_dashboard.bat` | Abre o painel visual no navegador (http://localhost:8050). Atualiza a cada 5 s. |
| `10_backtest.bat` | Roda a mesma estratégia sobre ~50 dias de histórico real em poucos minutos. Salva `backtest_resultado.txt` e `backtest_trades.csv`. Não mexe no bot ao vivo. |
| `11_otimizar.bat` | Testa centenas de variações (15m/1h/4h, stop, alvo, filtro EMA200, RSI, limites) nos primeiros 60% do histórico e valida a melhor nos últimos 40%. Salva `otimizacao_resultado.txt` e `melhor_config.json`. |
| `12_zerar_paper.bat` | Arquiva `state.json`, `trades.csv` e `bot.log` numa pasta `arquivo_DATA` e recomeça o paper com US$ 50 (pare o bot antes). |
| `13_otimizar_rompimento.bat` | Otimizador da estratégia de **rompimento com stop móvel** (busca de assimetria): 1h/4h, 10 ou 30 moedas, 384 variações, treino 60% / teste 40%. |
| `14_ativar_rompimento.bat` | Ativa no bot a configuração de rompimento **só se o otimizador aprovou** (gera `config_ativa.json`). Reinicie o bot depois. |
| `15_voltar_estrategia_padrao.bat` | Remove o `config_ativa.json` e volta para a estratégia padrão (tendência 4h). |
| `9_dashboard_celular.bat` | Igual ao 8, mas libera o acesso pelo celular no mesmo Wi-Fi. Clique em CELULAR no painel e leia o QR Code. O link tem uma chave de acesso (arquivo `dashboard_chave.txt`); sem ela, outro aparelho recebe acesso negado. Apague esse arquivo para gerar uma chave nova. |

- Configure o Windows para **não suspender** quando estiver na tomada: Configurações → Sistema → Energia → Suspensão = Nunca.
- Se o notebook dormir ou desligar, o bot para e, ao ligar de novo pelo `3_iniciar_bot.bat`, ele continua de onde parou (`state.json`).
- O bot não deixa rodar duas cópias na mesma pasta: a segunda se encerra sozinha com aviso no log.

## Instalar (Linux/Mac)
```bash
pip install -r requirements.txt
```

## Testar
```bash
python3 -m pytest -q tests
```
```bash
python3 check_api.py
```

## Rodar em segundo plano
```bash
nohup python3 -u bot.py > bot.log 2>&1 &
```
Ver o log ao vivo:
```bash
tail -f bot.log
```

## Parar
Jeito recomendado (kill switch): cria um arquivo `KILL` na pasta. Em até ~1 s o bot salva o estado e encerra.
```bash
touch KILL
```
Antes de rodar de novo, apague o arquivo:
```bash
rm KILL
```
Alternativa: `pkill -f bot.py` (o bot captura o sinal e também salva o estado).
As posições abertas continuam salvas no `state.json` e são retomadas quando você liga de novo.

Para começar do zero (zerar capital e histórico): pare o bot e apague `state.json` e `trades.csv`.

## Relatório
```bash
python3 report.py
```
Mostra: nº de operações, taxa de acerto, PnL antes e depois das taxas, ganho e perda médios, fator de lucro,
maior sequência de perdas, drawdown máximo, resultado por moeda, por dia e por semana. No final, diz se os
critérios para dinheiro real foram atingidos (≥ 40 operações, 2 semanas seguidas positivas e drawdown < 15%) ou
se **ainda não há dados suficientes**.

## Como o bot funciona
- **Universo:** as 10 moedas de maior volume em 24 h, com volume mínimo de US$ 20 M e excluindo as deslistadas. Atualizado a cada 1 h.
- **Loop de 30 s:** atualiza preços (`allMids`) → checa stop/alvo/tempo → checa o limite diário → procura entradas.
- **Estratégia atual (aprovada pelo `otimizar.py` em validação fora da amostra):** gráfico de **4h**, só candles fechados, uma avaliação por candle novo.
  - Lateral (|EMA20 − EMA50| / preço < 0,1%): não opera.
  - LONG: EMA20 > EMA50, fechamento > EMA20, **fechamento > EMA200** e RSI14 entre 50 e 72.
  - SHORT: EMA20 < EMA50, fechamento < EMA20, **fechamento < EMA200** e RSI14 entre 28 e 50.
  - Vários sinais ao mesmo tempo: entra primeiro na moeda com maior volume em USD nos últimos 20 candles.
  - Todos os parâmetros ficam no `CFG` do `bot.py`.
- **Risco:** 1,5% do capital por trade; stop = 3 × ATR14 (Wilder); alvo = 3 × a distância do stop; saída por tempo após 48 candles (8 dias);
  nocional máximo de 2× o capital; ordens abaixo de US$ 10 de nocional são ignoradas; máximo de 2 posições e 3 operações por dia.
- **Bot parado (notebook dormindo):** ao voltar, o bot confere nos candles de 5m se o stop ou o alvo foram tocados no período e fecha a posição no preço correspondente.
- **Limite diário:** se o capital + o PnL das posições abertas (já com custos) cair 5% em relação ao início do dia UTC, o bot fecha tudo e pausa até o próximo dia UTC.
- **Custos:** taxa de 0,045% por lado + slippage de 0,03% por lado, sempre contra você.

### Simplificações da simulação (importante)
- As entradas usam o preço médio atual (mid) com slippage, não o book real.
- Stop e alvo são checados pelo mid a cada 30 s. Stop executa no preço atual (pode ser pior que o stop); alvo executa no preço do alvo (conservador).
  Um pico rápido que encosta no alvo/stop e volta entre duas checagens pode não ser capturado.
- Funding não é considerado.

## Estratégia de rompimento e radar de assimetria
- **Rompimento** (`"estrategia": "rompimento"` ou via `14_ativar_rompimento.bat`): entra quando o fechamento rompe a máxima/mínima
  dos últimos N candles, opcionalmente depois de uma **compressão** (Bandas de Bollinger perto da mínima de 120 candles) e com
  **volume** acima da média. Sem alvo fixo: o **stop móvel** acompanha o melhor preço a N × ATR e só aperta, nunca afrouxa.
  A ideia é perder pouco nas falhas e deixar os acertos correrem (ganhos assimétricos).
- **Alavancagem máxima 4x**: é só o teto do tamanho da posição. A perda planejada por operação continua 1,5% do capital.
- **Slippage maior nas moedas pequenas** (volume 24h < US$ 100 M): 0,10% por lado, contra 0,03% nas grandes.
- **Radar de assimetria** (painel no dashboard): a cada candle novo, dá uma nota 0–100 às 30 moedas mais líquidas
  (volume ≥ US$ 10 M) combinando compressão, volume crescente, força relativa contra o BTC, proximidade do rompimento
  e funding extremo. **É informativo**: aponta onde olhar, não é sinal de entrada validado.
- Fluxo recomendado: `13_otimizar_rompimento.bat` → se **APROVADA**, `14_ativar_rompimento.bat` → reiniciar o bot → paper trading.

## trades.csv – colunas
`horario_utc` (saída), `moeda`, `lado`, `entrada`, `saida` (preços já com slippage), `tamanho` (em moedas),
`motivo` (stop, alvo, tempo, limite_diario), `pnl_usd` (líquido de taxas e slippage), `capital_apos`,
`taxas_usd` (só taxas), `horario_entrada_utc`.

## Mensagens do log
`ABRIU`, `FECHOU`, `SINAL`, `IGNORADA`, `LIMITE DIARIO`, `NOVO DIA`, `UNIVERSO`, `CICLO n concluido` (no 1º ciclo e depois a cada ~10 min)
e `ERROR` para falhas de rede (o bot segue no próximo ciclo, sem cair).
