# WaveGate Bot

**WaveTrend M5 + Markov Gate | Binance Spot Margin | LONG-ONLY**

Robô de trading que combina o WaveTrend Oscillator (LazyBear) no M5 com filtro de regime Markov diário. Opera exclusivamente comprado quando o mercado está em regime Bull.

## Estratégia

| Componente | Descrição |
|------------|-----------|
| **WaveSignal** | WT1 cruza WT2 para cima saindo de sobrevenda (<-40) nos últimos 3 candles |
| **Markov Gate** | Transition matrix diária — só opera quando P[Bull] - P[Bear] > 10% |
| **Confirmações** | EMA alinhada, volume spike, MACD subindo, corpo bullish (mín. 4/7) |
| **Saída** | Alvo +0.6% / Stop -0.3% / R/R 2.0x / Timeout 120 min |
| **Sizing** | 1% de risco por trade / máx. 3 posições simultâneas |

## Resultados (backtest v5 — 180 dias)

| Par | WR | PF | Retorno | Status |
|-----|----|----|---------|--------|
| **ETHUSDT** | **32.6%** | **1.32** | **+9.4%** | ✅ Ativo |
| SOLUSDT | 27.5% | 0.97 | +2.8% | Reserva |
| BNBUSDT | 21.3% | 1.02 | +4.1% | Reserva |
| XRPUSDT | 22.1% | 0.74 | -3.1% | Excluído |
| AVAXUSDT | 25.6% | 0.78 | -5.2% | Excluído |

*Break-even: 33.3% WR com R/R 2:1. ETH único par viável.*

## Arquitetura

```
main.py
├── DataAgent          # WebSocket Binance Spot (stream.binance.com)
├── MarkovAgent        # Gate diário via cadeia de Markov
├── WaveAgent          # WaveTrend Oscillator (LazyBear n1=10, n2=21)
├── IndicatorAgent     # EMA 9/21/55, MACD, RSI, Bollinger Bands
├── SignalAgent        # Avalia 7 condições, exige mínimo 4
├── RiskAgent          # Position sizing + limites de exposição
├── PortfolioAgent     # Rastreia equity e P&L (persiste em JSON)
├── ExecutionAgent     # Ordens reais via Spot Margin (MARKET + OCO)
├── MonitorAgent       # Detecta WIN/LOSS/TIMEOUT por candle
└── TelegramAgent      # Notificações e comandos
```

## Instalação

```bash
# 1. Clonar
git clone https://github.com/jlfilho0105/WaveGate-Bot.git
cd WaveGate-Bot

# 2. Criar venv e instalar dependências
setup.bat

# 3. Configurar credenciais
cp .env.example .env
# editar .env com API keys Binance + token Telegram

# 4. Rodar
.venv\Scripts\python main.py
```

## Configuração (.env)

```env
PAPER_TRADE=false              # true = sem ordens reais
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Comandos Telegram

| Comando | Função |
|---------|--------|
| `/status` | Posições abertas e métricas |
| `/equity` | Equity atual e P&L |
| `/regime` | Regime Markov atual |
| `/help` | Lista de comandos |

## Testes

```bash
.venv\Scripts\python tests\test_monitor.py
# 6/6 testes OK — WIN, LOSS, TIMEOUT, progresso, sem posição, 2 pares
```

## Notas importantes

- **Futuros bloqueado** para usuários no Brasil → usa Spot Margin Cross 3x
- OCO (`/sapi/v1/margin/order/oco`) com `AUTO_REPAY` — repaga empréstimo automaticamente
- Timeout: cancela OCO + MARKET sell com AUTO_REPAY
- Reconexão WebSocket automática em 5s após queda
- Expansão para SOL e BNB após WR live > 30% em 30 dias

## Requisitos

- Python 3.14+
- Conta Binance com Cross Margin habilitado
- API Key com permissão de Spot + Margin Trading
- Bot Telegram criado via @BotFather
