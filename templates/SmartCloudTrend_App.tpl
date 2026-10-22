<?xml version="1.0" encoding="utf-8"?>
<!--
  SmartCloudTrend_App.tpl
  ----------------------------------------------------------
  Template do gráfico do projeto "Mercado Trader" com o
  indicador SmartCloudTrend_App aplicado (mesmos parâmetros do
  app.py / indicadores.py). Esta versão NÃO sobrescreve o
  "SmartCloudTrend" original — convive lado a lado no MT5.

  ⚠️ O MT5 NÃO importa templates no formato XML — este arquivo
  serve apenas como DOCUMENTAÇÃO de referência. O template
  oficial deve ser gerado DENTRO do MT5, no gráfico onde
  o indicador já está aplicado:

     1. Abra um gráfico do ativo (ex: WIN$N, WDO$N, EURUSD…)
        no MESMO timeframe usado no app.py (M5 / M15).
     2. Aplique o indicador com o arquivo:
        templates/SmartCloudTrend_App.set
        (Navigator → Indicators → SmartCloudTrend_App → Load)
     3. Botão direito no gráfico → Templates → Save Template…
     4. Salve como "SmartCloudTrend_App" dentro desta pasta
        (templates/) para sobrescrever este arquivo .tpl.

  A partir daí, basta dar duplo-clique no .tpl gerado pelo
  MT5 para reproduzir o gráfico idêntico em qualquer ativo.

  Este XML abaixo descreve a configuração ESPERADA do
  template (não é lido pelo terminal — é referência):
-->
<chart name="SmartCloudTrend_App">

  <!-- ======= ATIVO / TIMEFRAME ======= -->
  <symbol>EURUSD</symbol>
  <period>M15</period>
  <!-- Mude para o ativo/timeframe que você opera
       (WIN$N / WDO$N / PETR4 / etc.) ao salvar pelo MT5 -->

  <!-- ======= INDICADORES APLICADOS ======= -->
  <indicators>

    <!-- Replicação completa do dashboard Python -->
    <indicator
      name="SmartCloudTrend_App"
      path="MQL5/Indicators/SmartCloudTrend_App.ex5"
      shift="0"
      apply_to="1"
      inputs="templates/SmartCloudTrend_App.set" />

  </indicators>

  <!-- ======= ESTILO DO GRÁFICO ======= -->
  <style>
    <background>     Black           </background>
    <foreground>     White           </foreground>
    <grid>           clrNONE         </grid>
    <chart_up>       C'38,166,154'   </chart_up>   <!-- #26A69A -->
    <chart_down>     C'239,83,80'    </chart_down> <!-- #EF5350 -->
    <chart_line>     clrWhiteSmoke   </chart_line>
    <volumes>        clrNONE         </volumes>
    <auto_scroll>    true            </auto_scroll>
    <shift_size>     50              </shift_size> <!-- % de espaço à direita -->
  </style>

  <!-- ======= OBJETOS GRÁFICOS PADRÃO ======= -->
  <objects>
    <!-- (FVG, BSL/SSL, Fibo, Sweeps, Volume Profile e
          histograma de Volume são todos desenhados pelo
          próprio indicador; nenhum objeto extra é necessário) -->
  </objects>

</chart>