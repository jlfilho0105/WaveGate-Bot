# WaveGate Bot

**OKX USDT-SWAP | Markov + H1 Trend + WaveTrend M5 | LONG/SHORT**

Robo direcional para contratos perpetuos da OKX. O regime Markov diario define a direcao macro, o H1 confirma tendencia e o WaveTrend no M5 dispara entradas.

## Estrategia

| Camada | Regra |
|--------|-------|
| Markov diario | Bull = LONG, Bear = SHORT, Sideways = sem entrada |
| Filtro H1 | EMA21/EMA55 alinhadas + MACD hist na direcao |
| Entrada M5 LONG | WT1 cruza WT2 para cima apos sobrevenda |
| Entrada M5 SHORT | WT1 cruza WT2 para baixo apos sobrecompra |
| Confirmacoes | EMA M5, volume spike, MACD M5, corpo do candle |
| Saida | TP 0.6%, SL 0.3%, timeout 120 min |
| Risco | 0.5% alvo por trade, maximo 3 posicoes abertas |

## Universo Inicial

Backtest OKX 180d com custo de 0.12% por trade:

| Ativo | Trades | WR | PF | Retorno | Meses positivos |
|-------|-------:|---:|---:|--------:|----------------:|
| BNB-USDT-SWAP | 27 | 77.8% | 13.83 | +8.84 | 5/5 |
| SAHARA-USDT-SWAP | 33 | 84.8% | 6.40 | +11.34 | 6/6 |
| ETH-USDT-SWAP | 21 | 81.0% | 5.40 | +5.79 | 6/6 |
| ADA-USDT-SWAP | 15 | 73.3% | 3.83 | +3.90 | 6/6 |
| DOGE-USDT-SWAP | 21 | 71.4% | 3.38 | +4.89 | 3/5 |
| AVAX-USDT-SWAP | 26 | 61.5% | 2.21 | +4.20 | 6/6 |
| SUI-USDT-SWAP | 21 | 66.7% | 2.66 | +4.20 | 5/7 |

Consolidado dos 14 candidatos testados: 400 trades, WR 62.5%, PF 2.01, retorno liquido +59.25, max DD 3.78%, 7/7 meses positivos.

## Arquitetura

```text
main.py
├── DataAgent          # REST + WebSocket OKX public (candle5m)
├── MarkovAgent        # Regime diario Bull/Bear/Sideways
├── WaveAgent          # WaveTrend WT1/WT2
├── IndicatorAgent     # EMA, MACD, RSI, Bollinger, ATR
├── SignalAgent        # LONG/SHORT com Markov + H1 + M5
├── RiskAgent          # Sizing, limite de posicoes, stop diario
├── PortfolioAgent     # Equity e PnL long/short
├── ExecutionAgent     # OKX v5 market + TP/SL anexado
├── MonitorAgent       # WIN/LOSS/TIMEOUT por candle fechado
└── TelegramAgent      # Alertas e comandos
```

## Instalacao

```bash
git clone https://github.com/jlfilho0105/WaveGate-Bot.git
cd WaveGate-Bot
setup.bat
cp .env.example .env
.venv\Scripts\python main.py
```

## Configuracao

```env
PAPER_TRADE=true
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

`PAPER_TRADE=true` e o padrao seguro. Troque para `false` somente depois de validar credenciais OKX, modo de conta e tamanho minimo dos contratos.

## Comandos Telegram

| Comando | Funcao |
|---------|--------|
| `/status` | Posicoes abertas e metricas |
| `/equity` | Equity atual e PnL |
| `/regime` | Regra direcional Markov |
| `/help` | Lista de comandos |

## Testes

```bash
.venv\Scripts\python tests\test_monitor.py
```

## Notas

- O bot usa candles fechados; em live, TP/SL devem ficar anexados na OKX para protecao intrabar.
- Funding de perpetuos ainda nao entra no backtest consolidado.
- O universo inicial deve ser revisado a cada 30 dias.
