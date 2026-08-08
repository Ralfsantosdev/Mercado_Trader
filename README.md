# Forex Dashboard

## Aviso importante antes de operar com dinheiro real

Este dashboard **não garante lucro**. O backtest com custo real (spread) faz
parte do app e mostra, para os parâmetros atuais e dados recentes, resultados
marginais ou negativos para boa parte das configurações — a estratégia muitas
vezes perde para o custo da operação. Qualquer sistema que ignore
spread/slippage superestima resultados.

Leia o expander "⚠️ Leia antes de operar com dinheiro real" dentro do app.

## Mudanças importantes (v4)

### ✋ Inteligência Artificial (IA narrativa) DESLIGADA
A chamada à API OpenAI (parecer narrativo) foi **removida por decisão do
usuário** para não gerar custo de tokens:
- `llm_parecer()` em `ai_analysis.py` agora é um no-op que **nunca** chama a API.
- As métricas **locais** (Monte Carlo, VaR, P(TP/SL)) continuam rodando 100%
  offline, sem custo.
- A sidebar do app inicia com a análise de risco IA **desligada**.

## Fonte de dados: AwesomeAPI (tempo real)

O app agora usa como padrão a **AwesomeAPI** (`https://awesomeapi.com.br`) para
cotação e análise em tempo real:

- **Cotação ao vivo** (bid/ask, máx/mín do dia) no topo do dashboard.
- **Candles reais** montados a partir de ticks de ~1 min nos timeframes
  `1m/5m/15m/30m/1h` e fechamento diário no `1d` — sem o problema do Yahoo,
  que não entrega forex intradiário confiável.
- **Sem cache** enquanto a API key estiver configurada (sem chave, a API usa
  cache de 1 minuto — o app segue funcionando).

Para usar sua chave, coloque em `.streamlit/secrets.toml`:

```toml
awesomeapi_key = "sk_..."
```

A chave fica em `https://awesomeapi.com.br/account#keys`. O arquivo
`secrets.toml` está no `.gitignore` — **nunca suba a chave para o GitHub**.

### Como rodar

```bash
pip install -r requirements.txt

# configurar senha e chave (obrigatório)
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edite .streamlit/secrets.toml: TROQUE a senha padrão e adicione awesomeapi_key

# rode local (recomendado para segurança)
streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

### ☁️ Deploy no Render (gratuito)

O `Dockerfile` + `entrypoint.sh` + `render.yaml` já estão no repo (repo **privado** é
suportado no Render). O dashboard roda em container Linux **sem MetaTrader** — os
dados em tempo real vêm da AwesomeAPI e a execução manual usa o modo demo; a
ponte MT5 fica desligada no container (é lazy e cai em try/except silenciosamente).

1. No [render.com](https://render.com) → **New +** → **Web Service** → conecte o
   GitHub → selecione `Mercado_Trader`.
2. O Render detecta o `Dockerfile` (runtime `docker`). O `render.yaml` já define
   o plano `free`, healthcheck em `/` e as variáveis.
3. Vá em **Environment** → **Environment Variables** e defina (vazias = desligadas):
   ```bash
   APP_PASSWORD="sua_senha_forte"      # OBRIGATÓRIO se quiser autenticação no app
   AWESOMEAPI_API_KEY="sua_chave"      # opcional; sem ela usa cache de 1 min
   TELEGRAM_TOKEN="123:ABC"            # opcional — alertas
   TELEGRAM_CHAT="123456789"           # opcional — alertas
   ```
   O `entrypoint.sh` gera `.streamlit/secrets.toml` a partir dessas variáveis no
   boot. **Nunca** coloque `secrets.toml` real dentro do repo.
4. Sobe sozinho na porta `$PORT` (Render injeta). Healthcheck em `/` e reinício
   em falha automático.

> ⚠️ Plano **free** do Render pausa o serviço após ~15 min sem acesso (acorda no
> próximo clique, levando ~1 min para subir). Se quiser sempre-ativo, use o plano
> Starter (~US$7/mês) ou um VPS.

> ⚠️ O robô MT5 (`run_trader.sh`) e a execução real continuam rodando **só na sua
> máquina Windows** — o cloud é somente o painel de análise + sinais por regras.

Na sidebar você pode alternar entre **awesomeapi** (padrão, tempo real) e
**yfinance** (Yahoo Finance, cache de 1h).

## Novas funcionalidades (v3)

### 🌍 Filtro de condição de mercado (sessão + ADX)
- **Sessão**: só opera nos horários de alta liquidez (Tóquio, Londres, NY,
  Londres+NY etc., em UTC). Fora deles o spread é maior e o movimento não
  tem continuidade.
- **ADX mínimo**: ignora sinais quando o mercado está lateral (range).
  ADX > 20-25 indica tendência aproveitável.

### 🛡️ Gestão de risco corrigida (foi a causa do grande prejuízo)
Um único trade em EUR-USD destruiu ~2,3% da conta porque o SL por ATR ficou
minúsculo num candle quieto e a posição explodiu para **~50× de alavancagem**
(`size = risco ÷ distância_SL`). Corrigido em `executor.py`:
- **Piso de 10 pips** no SL/TP (`MIN_SL_PIPS`) — impossível stop minúsculo.
- **Cap de 10×** de alavancagem (`MAX_LEVERAGE`) no paper e no MT5.
- **Risco padrão reduzido** de 1% para **0,25%** por trade (executor, backtest,
  alertas e script do robô).

### Somente estratégias com tendência (ADX)
- Filtro **ADX ≥ 20** agora é o padrão (sidebar, alertas e `run_trader.sh`).
  Pesquisas com backtests (200-day MA, EMA 9/21, breakouts) mostram que o
  filtro de tendência é o que transforma estratégia perdedora em lucrativa.

### 🤖 ML com validação walk-forward
- O modelo agora é validado em **janelas móveis** (re-treina a cada passo e
  testa sempre em dados nunca vistos), com 15 features (momentum, ADX,
  deslocamentos de média etc.).
- O número honesto é o **edge** (acurácia − baseline). Edge ~0 ou negativo
  = **não há previsibilidade aproveitável** — resultado esperado para direção
  de 1 candle. Desconfie de qualquer ferramenta que prometa mais.

### 📣 Alertas no Telegram
Notifica quando o sinal **muda** (neutro→compra/venda), sem spam a cada refresh.

```toml
telegram_token = "123456:AAH..."
telegram_chat  = "987654321"
```
Como criar o bot e descobrir o chat_id: veja o topo de `alerts.py`. Você pode
rodar `python alerts.py --test` para validar, e o módulo roda sozinho fora do
app (para um cron agendar a verificação mesmo com o app fechado).

## 🤖 Robô automático no MetaTrader 5
Além dos alertas, o cron pode **executar os sinais automaticamente no MT5**
(abre/fecha posições sem precisar do dashboard aberto):

- `run_trader.sh` — verifica o sinal a cada 5 min e executa via `Mt5Broker`.
- Agendado no crontab (`*/5 * * * *`), log em `/tmp/trader.log`.
- Parâmetros (par/timeframe) editáveis no topo do script.
- Teste manual: `bash run_trader.sh` (ou `--force` para notificar sempre).

Avisos:
- Só executa quando há **sinal + MT5 disponível**; sinal neutro não faz nada.
- Acompanhe o log e o terminal MT5. Remova a linha do cron para desligar o robô.

### 💸 Execução ao vivo em modo demo (paper trading)
Executa os sinais no **preço real da AwesomeAPI**, com **slippage simulado**,
sem arriscar dinheiro. Posições e histórico persistem em `execution_state.json`.
A interface `Broker` já está separada: plugar uma corretora demo real
(MetaTrader5/OANDA) é criar um `LiveBroker` com a mesma interface e trocar
uma linha. Papel é papel — não garante execução real.

### 🐍 Execução no MetaTrader 5 (demo/real)
O dashboard pode executar os sinais **direto na conta logada do MT5** (sua
conta atual é demo Pepperstone). No Linux o MT5 roda sob Wine, então usamos um
bridge RPyC:

1. Instale o cliente bridge e baixe o binário do servidor:
   ```bash
   pip install mt5linux          # só para o binário; o client é substituído
   # baixe o mt5server.exe da última release e salve em ~/.local/share/mt5linux/
   # https://github.com/lucas-campagna/mt5linux/releases
   ```
2. Suba terminal + bridge (o terminal precisa estar logado na conta):
   ```bash
   ./start_mt5.sh                # para parar: ./start_mt5.sh stop
   ```
3. No dashboard, sidebar → **Execução** → marque "Acompanhar sinais" e escolha
   **MetaTrader 5** como broker.

Arquitetura: `mt5server.exe` expõe o terminal como servidor RPyC clássico na
porta 18812; `mt5_bridge.py` é um client mínimo (sem depender do pacote
`mt5linux`, que exige Python 3.12). `Mt5Broker` em `executor.py` implementa a
mesma interface do `PaperBroker` (troca-se a instância no app).

⚠️ Avisos:
- Símbolos do app (`EUR-USD`) são convertidos para o MT5 (`EURUSD`) removendo o
  hífen — se sua corretora usa sufixo (ex.: `EURUSD.a`), ajuste `mt5_symbol()`.
- Dimensionamento é aproximado (1 lote = 100k unidades); o risco real pode
  diferir para pares cruzados/JPY.
- O pacote `MetaTrader5` do PyPI é **só Windows**; por isso o bridge RPyC.

## O que foi corrigido em relação aos arquivos originais

| Problema | Arquivo original | Correção |
|---|---|---|
| Erro de sintaxe / indentação quebrada | `Mercado_atualizado_ao_vivo.py` | Reescrito do zero, testado com `py_compile` |
| `while True` travando o Streamlit | Ambos os arquivos | Removido — agora usa botão "Atualizar dados" + cache com TTL |
| Senha hardcoded no código (`"1234"`) | Ambos os arquivos | Lida de `.streamlit/secrets.toml` (fora do controle de versão) |
| Vazamento de dados no ML (`train_test_split` aleatório em série temporal) | Ambos os arquivos | Split temporal: treina no passado, testa no período mais recente |
| Import de Flask/Redis sem app Flask (`NameError`) | `app.py` original | Removido — não fazia sentido dentro de um app Streamlit |
| RSI com média simples em vez de suavização de Wilder | Ambos os arquivos | RSI recalculado com fórmula de Wilder (igual à IQ Option/MetaTrader) |
| Sem separação entre fonte de dados e lógica | Ambos os arquivos | `data_provider.py` isola a fonte — trocar yfinance por CSV é 1 linha |
| Sinal nunca disparava (Volume=0 no Yahoo forex) | `model.py` | Volume inútil é detectado e a confirmação passa a usar ATR em alta |
| RSI fixo em 10/90 (quase nunca gatilho) | `model.py` | Limites configuráveis na sidebar (padrão 30/70) |
| "Gatilho de cruzamento" era só nível | `model.py` | Cruzamento real MM9×MM21 (opcional, use_cross) |
| "Confiança" do ML era sempre ≥ 50% | `model.py` | Agora é a margem \|p − 0.5\| com aviso "moeda lançada" se < 5% |
| Não havia backtest/P&L | — | `backtest.py` novo: spread, SL/TP por ATR, risco %/trade, expectativa, drawdown, Sharpe |
| Sintaxe `str \| None` (Python 3.8) | `data_provider.py` | `Optional[str]` — roda em Python 3.8+ |

## Estrutura

```
forex_dashboard/
├── app.py              # Interface Streamlit (parâmetros, backtest, ML, demo, alertas)
├── data_provider.py    # Fonte de dados (AwesomeAPI tempo real / yfinance / CSV)
├── indicators.py       # RSI, MACD, médias, ATR, Bollinger, ADX (fórmulas próprias)
├── model.py            # Sinais por regra + filtros de sessão/ADX + ML walk-forward
├── backtest.py         # Backtest com spread, risco %, SL/TP, métricas
├── executor.py         # Paper trading (Broker) com preço real + slippage
├── alerts.py           # Alertas Telegram com estado persistente
├── test_pipeline.py    # Teste de sanidade com dados sintéticos
└── requirements.txt
```

## Limitações conhecidas

- **AwesomeAPI intraday**: a API entrega no máximo ~1500 pontos por chamada
  (≈ 1 dia de ticks de 1 min), então o histórico intradiário é montado com
  paginação — limites do slider já refletem isso (~30 dias intraday, 360 no 1d).
- **Volume**: o AwesomeAPI não tem volume real — o app usa contagem de ticks
  por candle como proxy. No Yahoo o volume de forex é zerado (o app detecta e
  usa ATR na confirmação).
- **yfinance e timeframe M5**: o `yfinance` **não garante dados intradiários
  de forex em M5 de forma confiável e gratuita** — prefira a AwesomeAPI para
  intraday. Para replicar exatamente sua estratégia de M5 (que você operava
  manualmente), exporte o histórico do MetaTrader/Dukascopy em CSV e use
  `get_provider(source="csv", csv_path="seu_arquivo.csv")` em `app.py`.

## O que ainda NÃO existe (próxima etapa)

- **Corretora real** — o app executa no papel (modo demo). A interface `Broker`
  em `executor.py` já está pronta; falta plugar MetaTrader5/OANDA practice.
- **Dados M5 confiáveis** — o Yahoo não entrega; a AwesomeAPI resolve em tempo
  real para os pares e timeframes que ela suporta. Para M5 com histórico mais
  longo que ~30 dias, use CSV do MetaTrader/Dukascopy (a camada `data_provider`
  já suporta).
- **Walk-forward em múltiplos pares** — o ML já usa walk-forward, mas rode o
  mesmo backtest em vários pares/períodos antes de confiar em qualquer edge.
- **Slippage real, requotes e downtime** — o modo demo simula slippage, mas o
  resto depende da corretora real.
