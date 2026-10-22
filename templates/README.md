# Templates — SmartCloudTrend_App

Esta pasta contém os arquivos para reproduzir, no **MetaTrader 5**, o
gráfico idêntico ao do nosso app Streamlit/Plotly (`app.py`).

> ⚠️ Esta pasta usa o sufixo `_App` (`SmartCloudTrend_App.mq5`,
> `.set`, `.tpl`) para **conviver lado a lado** com qualquer outro
> indicador "SmartCloudTrend" que você já tenha configurado no MT5.
> Nada aqui sobrescreve o original.

## Arquivos

| Arquivo                          | Tipo            | Uso                                                    |
|----------------------------------|-----------------|--------------------------------------------------------|
| `SmartCloudTrend_App.set`        | set-file (texto) | Carrega os **parâmetros** do indicador.                |
| `SmartCloudTrend_App.tpl`        | referência (XML) | Descreve o template — **não** é lido pelo MT5.         |

## Como aplicar (passo a passo)

1. **Compile** o indicador no MetaEditor:
   `File → Open → SmartCloudTrend_App.mq5 → F7`. Vai aparecer
   `SmartCloudTrend_App.ex5` em `MQL5/Indicators/`.

2. **Abra** um gráfico do seu ativo no mesmo timeframe do app
   (`M5` ou `M15` são os mais usados — veja a barra lateral do app).

3. **Aplique o indicador** arrastando `SmartCloudTrend_App` do Navigator.
   Em *Properties → Inputs → Load*, escolha `SmartCloudTrend_App.set` desta
   pasta. Clique OK — o gráfico deve ficar igual ao do app.

4. **Salve o template de verdade** (passo obrigatório):
   clique com o botão direito no gráfico →
   `Templates → Save Template…` → nomeie como `SmartCloudTrend_App` →
   salve **dentro desta pasta** (`templates/`). Isso sobrescreve o
   `.tpl` desta pasta pelo template **binário oficial** do MT5,
   que é o único que o terminal de fato lê.

5. Para reutilizar em outros ativos/timeframes: dê duplo-clique no
   `.tpl` gerado pelo MT5 e o gráfico abre com tudo aplicado.

## Por que o `.tpl` aqui é só XML?

O MT5 grava `.tpl` em formato **binário proprietário** que muda entre
builds do terminal — é impossível (e frágil) tentar fabricá-lo à mão.
A versão XML desta pasta serve apenas como **documentação** da
configuração esperada (ativo, timeframe, cores, indicador aplicado).
O template funcional sai do passo 4 acima.

## Mapeamento app.py ↔ `.set`

| `app.py` / `indicators.py`            | Parâmetro no `.set`         |
|---------------------------------------|------------------------------|
| SMA 9 / 21 / 200                      | `SMA_Fast=9`, `SMA_Mid=21`, `SMA_Slow=200` |
| Bollinger 20 / 2σ                     | `BB_Period=20`, `BB_Dev=2.0` |
| EMA 8 / 20 (Smart Cloud)              | `Cloud_Fast=8`, `Cloud_Slow=20` |
| FVG (5 cores)                         | `ShowFVG=true`, `BullFVG_Color`, `BearFVG_Color` |
| Swings H/L (window=5)                 | `ShowSwings=true`, `Swing_Window=5` |
| Fibo 33/50/66 (lookback=150)          | `ShowFibo=true`, `SMC_Lookback=150` |
| Liquidity Sweeps                      | `ShowSweeps=true` |
| Volume Profile (bins=40, VA=70%)      | `VP_Bins=40`, `VP_ValueAreaPct=0.70`, `VP_Lookback=200` |
| Histograma Volume (alta=#26A69A, baixa=#EF5350) | `Vol_Up_Color`, `Vol_Dn_Color` |

Se você mudar parâmetros no app.py (ex: trocar SMA 9 por outra janela),
**atualize também este `.set`** para manter o gráfico MT5 alinhado.

## Convivência com outro SmartCloudTrend

O seu "SmartCloudTrend" original continua intacto no MT5. Esta versão
`_App`:

- **Indicator name**: `SmartCloudTrend_App`
- **Prefixo dos objetos**: `SCTA_` (em vez de `SCT_`)
- **Pasta de origem**: `MQL5/Indicators/SmartCloudTrend_App.ex5`

Os dois indicadores podem rodar no mesmo gráfico — só não dá pra
distingui-los visualmente na legenda se você não renomear. Para
desativar o seu original temporariamente: clique na legenda → desmarca.