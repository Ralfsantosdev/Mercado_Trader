//+------------------------------------------------------------------+
//|                                          SmartCloudTrend_App.mq5 |
//|                 Replica visual do app Streamlit/Plotly            |
//+------------------------------------------------------------------+
#property copyright   "Mercado Trader"
#property description "Replicação MQL5 do dashboard Python:"
#property description "  • SMAs 9 / 21 / 200"
#property description "  • Bollinger Bands (20, 2σ)"
#property description "  • Smart Cloud (EMA20 × EMA50 com nuvem colorida)"
#property description "  • Sinais do modelo (▲/▼) + cruzamentos EMA20/50"
#property description "  • SMC: FVG (rect), Swings BSL/SSL, Fibo 33/50/66,"
#property description "    Liquidity Sweeps"
#property description "  • Volume Profile lateral (POC / VAH / VAL + bins)"
#property description "  • Histograma de Volume colorido (substitui subpainel)"

#property indicator_chart_window
#property indicator_buffers 17
#property indicator_plots   11

// --- Plot 1: SMA 9 -----------------------------------------------------
#property indicator_label1  "MM 9"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDodgerBlue
#property indicator_style1  STYLE_SOLID
#property indicator_width1  1

// --- Plot 2: SMA 21 ----------------------------------------------------
#property indicator_label2  "MM 21"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrOrange
#property indicator_style2  STYLE_SOLID
#property indicator_width2  1

// --- Plot 3: SMA 200 ---------------------------------------------------
#property indicator_label3  "MM 200"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrMediumPurple
#property indicator_style3  STYLE_SOLID
#property indicator_width3  2

// --- Plot 4: BB Superior -----------------------------------------------
#property indicator_label4  "BB Sup"
#property indicator_type4   DRAW_LINE
#property indicator_color4  C'171,71,188'
#property indicator_style4  STYLE_DOT
#property indicator_width4  1

// --- Plot 5: BB Inferior -----------------------------------------------
#property indicator_label5  "BB Inf"
#property indicator_type5   DRAW_LINE
#property indicator_color5  C'171,71,188'
#property indicator_style5  STYLE_DOT
#property indicator_width5  1

// --- Plot 6: EMA 20 (Smart Cloud) --------------------------------------
#property indicator_label6  "EMA 20"
#property indicator_type6   DRAW_LINE
#property indicator_color6  C'38,166,154'
#property indicator_style6  STYLE_SOLID
#property indicator_width6  2

// --- Plot 7: EMA 50 (Smart Cloud) -------------------------------------
#property indicator_label7  "EMA 50"
#property indicator_type7   DRAW_LINE
#property indicator_color7  C'239,83,80'
#property indicator_style7  STYLE_SOLID
#property indicator_width7  2

// --- Plot 8: Setas do MODELO (compra/venta = DRAW_COLOR_ARROW) ---------
#property indicator_label8  "Sinal COMPRA/VENDA"
#property indicator_type8   DRAW_COLOR_ARROW
#property indicator_color8  clrLime,clrRed
#property indicator_width8  4

// --- Plot 9: Histograma de Volume (substitui subpainel) ---------------
// (pintado na própria janela, em barras finas no rodapé do gráfico)
#property indicator_label9  "Volume"
#property indicator_type9   DRAW_COLOR_HISTOGRAM
#property indicator_color9  C'38,166,154',C'239,83,80'  // alta, baixa
#property indicator_style9  STYLE_SOLID
#property indicator_width9  1

// --- Plot 10: Clarezas Continuação (estrela) --------------------------
#property indicator_label10 "Clarezas Cont."
#property indicator_type10  DRAW_COLOR_ARROW
#property indicator_color10 clrLime,clrRed
#property indicator_width10 3

// --- Plot 11: Clarezas Absorção (diamante) ----------------------------
#property indicator_label11 "Clarezas Absorção"
#property indicator_type11  DRAW_ARROW
#property indicator_color11 clrDodgerBlue
#property indicator_width11 2

// Buffers principais (com plot) -----------------------------------------
double _smaFast[], _smaMid[], _smaSlow[];
double _bbUp[],    _bbDn[];
double _emaFast[], _emaSlow[];

// Plot 8 (DRAW_COLOR_ARROW): seta de sinal COMPRA/VENDA — valor + cor
double _sigVal[], _sigClr[];

// Buffers extras (sem plot) ---------------------------------------------
double BB_MidArr[], CrossUp[], CrossDown[];

// Histograma de volume (DRAW_COLOR_HISTOGRAM = valor + cor)
double _volVal[], _volClr[];

// Clarezas: continuação (DRAW_COLOR_ARROW) + absorção (DRAW_ARROW)
double _clarContVal[], _clarContClr[];
double _clarAbsVal[];

// --- inputs principais -----------------------------------------------
input string  _s0_                = "===== MÉDIAS (alinhadas ao app.py) ====="; // ---
input int    SMA_Fast             = 9;        // Período SMA rápida
input int    SMA_Mid              = 21;       // Período SMA média
input int    SMA_Slow             = 200;      // Período SMA lenta
input bool   ShowSMA              = true;     // Mostrar SMAs

input string  _s1_                = "===== BOLLINGER BANDS ====="; // ---
input bool   ShowBB              = true;      // Mostrar Bollinger
input int    BB_Period           = 20;        // Período BB
input double BB_Dev              = 2.0;       // Desvio BB (σ)

input string  _s2_                = "===== SMART CLOUD (EMA 20/50) ====="; // ---
input bool   ShowCloud           = true;      // Mostrar Smart Cloud
input int    Cloud_Fast          = 20;        // Período EMA rápida (varejo)
input int    Cloud_Slow          = 50;        // Período EMA lenta (institucional)
input bool   ShowCrossArrows     = true;      // Setas nos cruzamentos da cloud

input string  _s3_                = "===== SINAIS DO MODELO ====="; // ---
input bool   ShowModelSignals    = true;      // Mostrar setas do modelo ML

input string  _s4_                = "===== SMC / ICT ====="; // ---
input bool   ShowFVG             = true;      // FVG (Fair Value Gaps)
input bool   ShowSwings          = true;      // Swing H/L (BSL/SSL)
input bool   ShowFibo            = true;      // Retrações 33/50/66
input bool   ShowSweeps          = true;      // Liquidity Sweeps
input int    SMC_Lookback        = 150;       // Lookback SMC (candles)
input int    Swing_Window        = 5;         // Janela de fractal p/ swing
input color  BullFVG_Color       = C'38,166,154';   // Cor FVG de alta
input color  BearFVG_Color       = C'239,83,80';    // Cor FVG de baixa
input color  BSL_Color           = clrOrange;       // Cor BSL
input color  SSL_Color           = clrMagenta;      // Cor SSL
input color  Fib33_Color         = clrLightSkyBlue; // Fibo 33%
input color  Fib50_Color         = clrSilver;       // Fibo 50%
input color  Fib66_Color         = clrGold;         // Fibo 66%

input string  _s5_                = "===== VOLUME PROFILE ====="; // ---
input bool   ShowVolumeProfile   = true;      // Mostrar Volume Profile lateral
input int    VP_Bins             = 40;        // Quantidade de bins (faixas)
input int    VP_Lookback         = 200;       // Lookback do volume profile
input double VP_ValueAreaPct     = 0.70;      // % do volume p/ Value Area (Padrão 70%)
input color  VP_Up_Color         = C'38,166,154'; // Cor faixas acima do preço
input color  VP_Dn_Color         = C'239,83,80';  // Cor faixas abaixo do preço
input color  VP_POC_Color        = clrYellow;     // Cor da linha POC
input color  VP_VAH_Color        = clrDodgerBlue; // Cor da linha VAH
input color  VP_VAL_Color        = clrDodgerBlue; // Cor da linha VAL

input string  _s6_                = "===== PAINEL DE VOLUME ====="; // ---
input bool   ShowVolume          = true;      // Mostrar histograma de volume
input color  Vol_Up_Color        = C'38,166,154'; // Cor volume alta
input color  Vol_Dn_Color        = C'239,83,80';  // Cor volume baixa

input string  _s7_               = "===== CLAREZAS (FLUXO+ABSORÇÃO) ====="; // ---
input bool   ShowClarezas        = true;      // Mostrar sinais Clarezas
input int    Clar_VolumePeriod   = 20;        // Período média volume
input double Clar_VolumeMulti    = 1.5;       // Multiplicador volume alto
input double Clar_BodyRatio      = 0.3;       // Razão corpo/range absorção

//+------------------------------------------------------------------+
//| Handles dos indicadores nativos                                     |
//+------------------------------------------------------------------+
int hSMA9=INVALID_HANDLE, hSMA21=INVALID_HANDLE, hSMA200=INVALID_HANDLE;
int hBB  =INVALID_HANDLE;
int hEMA8=INVALID_HANDLE, hEMA20=INVALID_HANDLE;

// iBands: 3 buffers — [0]=Superior, [1]=Médio, [2]=Inferior (índice estável)
#define BB_BAND_UP    0
#define BB_BAND_MID   1
#define BB_BAND_LOW   2

//+------------------------------------------------------------------+
//| Inicialização                                                       |
//+------------------------------------------------------------------+
int OnInit()
{
   // --- registro de buffers (9 plots = 10 buffers + 4 helpers = 14) -
   SetIndexBuffer(0, _smaFast,  INDICATOR_DATA);
   SetIndexBuffer(1, _smaMid,   INDICATOR_DATA);
   SetIndexBuffer(2, _smaSlow,  INDICATOR_DATA);
   SetIndexBuffer(3, _bbUp,     INDICATOR_DATA);
   SetIndexBuffer(4, _bbDn,     INDICATOR_DATA);
   SetIndexBuffer(5, _emaFast,  INDICATOR_DATA);
   SetIndexBuffer(6, _emaSlow,  INDICATOR_DATA);
   SetIndexBuffer(7, _sigVal,   INDICATOR_DATA);
   SetIndexBuffer(8, _sigClr,   INDICATOR_DATA);
   SetIndexBuffer(9, _volVal,   INDICATOR_DATA);
   SetIndexBuffer(10,_volClr,   INDICATOR_DATA);

   // Buffers "sem plot" (apenas arrays de trabalho)
   SetIndexBuffer(11, BB_MidArr, INDICATOR_CALCULATIONS);
   SetIndexBuffer(12, CrossUp,   INDICATOR_CALCULATIONS);
   SetIndexBuffer(13, CrossDown, INDICATOR_CALCULATIONS);

   // Clarezas buffers
   SetIndexBuffer(14, _clarContVal, INDICATOR_DATA);
   SetIndexBuffer(15, _clarContClr, INDICATOR_DATA);
   SetIndexBuffer(16, _clarAbsVal,  INDICATOR_DATA);

   // --- estilo das setas (modelo) ----------------------------------
   PlotIndexSetInteger(7, PLOT_ARROW, 233);             // ▲ Wingdings
   PlotIndexSetInteger(7, PLOT_ARROW_SHIFT, -20);       // posiciona abaixo da vela
   PlotIndexSetInteger(7, PLOT_LINE_WIDTH, 4);

   // --- estilo Clarezas -----------------------------------------------
   PlotIndexSetInteger(9, PLOT_ARROW, 72);              // ★ estrela
   PlotIndexSetInteger(9, PLOT_ARROW_SHIFT, -30);
   PlotIndexSetInteger(9, PLOT_LINE_WIDTH, 3);
   PlotIndexSetInteger(10, PLOT_ARROW, 115);            // ◆ diamante
   PlotIndexSetInteger(10, PLOT_ARROW_SHIFT, -25);
   PlotIndexSetInteger(10, PLOT_LINE_WIDTH, 2);

   // --- handles ------------------------------------------------------
   hSMA9   = iMA(NULL, 0, SMA_Fast, 0, MODE_SMA, PRICE_CLOSE);
   hSMA21  = iMA(NULL, 0, SMA_Mid,  0, MODE_SMA, PRICE_CLOSE);
   hSMA200 = iMA(NULL, 0, SMA_Slow, 0, MODE_SMA, PRICE_CLOSE);
   hBB     = iBands(NULL, 0, BB_Period, 0, BB_Dev, PRICE_CLOSE);
   hEMA8   = iMA(NULL, 0, Cloud_Fast, 0, MODE_EMA, PRICE_CLOSE);
   hEMA20  = iMA(NULL, 0, Cloud_Slow, 0, MODE_EMA, PRICE_CLOSE);

   if(hSMA9==INVALID_HANDLE || hSMA21==INVALID_HANDLE || hSMA200==INVALID_HANDLE ||
      hBB==INVALID_HANDLE || hEMA8==INVALID_HANDLE || hEMA20==INVALID_HANDLE)
   {
      Print("SmartCloudTrend_App: falha ao criar handles nativos.");
      return INIT_FAILED;
   }

   // Esconde as SMAs se o usuário desabilitar
   if(!ShowSMA)
   {
      PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(1, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(2, PLOT_DRAW_TYPE, DRAW_NONE);
   }
   if(!ShowBB)
   {
      PlotIndexSetInteger(3, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(4, PLOT_DRAW_TYPE, DRAW_NONE);
   }
   if(!ShowCloud)
   {
      PlotIndexSetInteger(5, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(6, PLOT_DRAW_TYPE, DRAW_NONE);
   }
   if(!ShowModelSignals)
      PlotIndexSetInteger(7, PLOT_DRAW_TYPE, DRAW_NONE);
   if(!ShowVolume)
      PlotIndexSetInteger(8, PLOT_DRAW_TYPE, DRAW_NONE);
   if(!ShowClarezas)
   {
      PlotIndexSetInteger(9, PLOT_DRAW_TYPE, DRAW_NONE);
      PlotIndexSetInteger(10, PLOT_DRAW_TYPE, DRAW_NONE);
   }

   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   IndicatorSetString(INDICATOR_SHORTNAME, "SmartCloudTrend_App");

   // Cores do histograma de volume (índice 0 = alta, 1 = baixa)
   PlotIndexSetInteger(8, PLOT_LINE_COLOR, 0, Vol_Up_Color);
   PlotIndexSetInteger(8, PLOT_LINE_COLOR, 1, Vol_Dn_Color);

   // limpa objetos SMC antigos ao (re)carregar o indicador
   ObjectsDeleteAll(0, "SCTA_");
   ChartRedraw(0);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Deinit: limpa todos os objetos gráficos criados                   |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, "SCTA_");
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Cálculo principal                                                   |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   if(rates_total < MathMax(SMA_Slow, BB_Period))
      return 0;

   // --- 1. copia os indicadores nativos -----------------------------
   int need = rates_total;
   if(CopyBuffer(hSMA9,   0, 0, need, _smaFast)<=0) return 0;
   if(CopyBuffer(hSMA21,  0, 0, need, _smaMid) <=0) return 0;
   if(CopyBuffer(hSMA200, 0, 0, need, _smaSlow)<=0) return 0;
   if(CopyBuffer(hBB, BB_BAND_UP,  0, need, _bbUp)    <=0) return 0;
   if(CopyBuffer(hBB, BB_BAND_LOW, 0, need, _bbDn)    <=0) return 0;
   if(CopyBuffer(hBB, BB_BAND_MID, 0, need, BB_MidArr)<=0) return 0;
   if(CopyBuffer(hEMA8,   0, 0, need, _emaFast)<=0) return 0;
   if(CopyBuffer(hEMA20,  0, 0, need, _emaSlow)<=0) return 0;

// --- 2. preenche setas (cruzamentos + sinais do modelo) ----------
   int start = prev_calculated > 1 ? prev_calculated - 1 : 1;
   for(int i=start; i<rates_total; i++)
   {
      _sigVal[i] = EMPTY_VALUE;
      _sigClr[i] = 0.0;

      // Smart Cloud: cruzamento EMA8/EMA20
      CrossUp[i]  = EMPTY_VALUE;
      CrossDown[i]= EMPTY_VALUE;

      bool cloudUp   = (_emaFast[i] > _emaSlow[i]) && (_emaFast[i-1] <= _emaSlow[i-1]);
      bool cloudDown = (_emaFast[i] < _emaSlow[i]) && (_emaFast[i-1] >= _emaSlow[i-1]);

      if(ShowCrossArrows && cloudUp)
         CrossUp[i] = low[i] - 15 * _Point;
      if(ShowCrossArrows && cloudDown)
         CrossDown[i] = high[i] + 15 * _Point;

      // "Sinais do modelo" — replicamos a lógica do pullback_liquidity_strategy.py
      //   BUY:  close > SMA21  E  SMA9 > SMA21  E  (EMA8 cruza acima da EMA20)
      //   SELL: close < SMA21  E  SMA9 < SMA21  E  (EMA8 cruza abaixo da EMA20)
      bool buyCond  = (close[i] > _smaMid[i]) &&
                      (_smaFast[i] > _smaMid[i]) &&
                      cloudUp;
      bool sellCond = (close[i] < _smaMid[i]) &&
                      (_smaFast[i] < _smaMid[i]) &&
                      cloudDown;

      if(buyCond)
      {
         _sigVal[i] = low[i]  - 25 * _Point;
         _sigClr[i] = 0.0;            // clrLime
      }
      else if(sellCond)
      {
         _sigVal[i] = high[i] + 25 * _Point;
         _sigClr[i] = 1.0;            // clrRed
      }

      // Histograma de volume (substitui o subpainel)
      _volVal[i] = (double)tick_volume[i];
      _volClr[i] = (close[i] >= open[i]) ? 0.0 : 1.0;  // 0=alta, 1=baixa

      // Clarezas: Continuação + Absorção
      _clarContVal[i] = EMPTY_VALUE;
      _clarContClr[i] = 0.0;
      _clarAbsVal[i]  = EMPTY_VALUE;

      if(ShowClarezas && i >= Clar_VolumePeriod)
      {
         // Média de volume
         double volSum = 0;
         for(int v=1; v<=Clar_VolumePeriod; v++)
            volSum += (double)tick_volume[i-v];
         double volAvg = volSum / Clar_VolumePeriod;
         double volNow = (double)tick_volume[i];
         bool highVol = volNow > volAvg * Clar_VolumeMulti;

         // Corpo e range
         double body = MathAbs(close[i] - open[i]);
         double rng  = high[i] - low[i];
         bool smallBody = (rng > 0) && ((body / rng) < Clar_BodyRatio);

         // Fluxo
         bool buyFlow  = highVol && (close[i] > open[i]);
         bool sellFlow = highVol && (close[i] < open[i]);

         // Pullback (candle anterior oposto)
         bool prevBear = (close[i-1] < open[i-1]);
         bool prevBull = (close[i-1] > open[i-1]);

         // Rompimento
         bool breakHigh = (close[i] > high[i-1]);
         bool breakLow  = (close[i] < low[i-1]);

         // Continuação: fluxo + pullback + rompimento
         bool contBuy  = buyFlow  && prevBear && breakHigh;
         bool contSell = sellFlow && prevBull && breakLow;

         // Absorção: volume alto + candle pequeno
         bool absorption = highVol && smallBody;

         if(contBuy)
         {
            _clarContVal[i] = low[i] - 40 * _Point;
            _clarContClr[i] = 0.0;  // clrLime
         }
         else if(contSell)
         {
            _clarContVal[i] = high[i] + 40 * _Point;
            _clarContClr[i] = 1.0;  // clrRed
         }

         if(absorption)
         {
            _clarAbsVal[i] = (close[i] > open[i]) ?
               (low[i] - 35 * _Point) : (high[i] + 35 * _Point);
         }
      }
   }

   // --- 3. SMAs com 1 candle de atraso visual no início -------------
   for(int i=0; i<MathMax(SMA_Fast, MathMax(SMA_Mid, SMA_Slow)); i++)
   {
      if(i<rates_total)
      {
         if(i>=SMA_Fast)  {} else _smaFast[i] = EMPTY_VALUE;
         if(i>=SMA_Mid)   {} else _smaMid[i]  = EMPTY_VALUE;
         if(i>=SMA_Slow)  {} else _smaSlow[i] = EMPTY_VALUE;
      }
   }

   // --- 4. objetos gráficos (FVG, Swings, Fibo, Sweeps, VolProfile) -
   // Recriamos a cada cálculo para garantir alinhamento com zoom/scroll.
   BuildSMC(rates_total, time, open, high, low, close, tick_volume);

   return rates_total;
}

//+------------------------------------------------------------------+
//| Constrói objetos SMC + Volume Profile                              |
//+------------------------------------------------------------------+
void BuildSMC(const int rates_total,
              const datetime &time[],
              const double &open[],
              const double &high[],
              const double &low[],
              const double &close[],
              const long &tick_volume[])
{
   // --- limpeza de objetos do tick anterior -----------------------
   ObjectsDeleteAll(0, "SCTA_");

   int first = MathMax(0, rates_total - SMC_Lookback);
   int last  = rates_total - 1;
   if(last < 1) return;

   // ----- A) FVG (Fair Value Gaps) --------------------------------
   if(ShowFVG)
   {
      // i e i-2 precisam estar dentro do lookback
      for(int i=first+2; i<=last; i++)
      {
         // Bullish FVG: low[i] > high[i-2]
         if(low[i] > high[i-2])
         {
            string name = "SCTA_FVGB_" + IntegerToString(i);
            ObjectCreate(0, name, OBJ_RECTANGLE, 0,
                         time[i],  high[i-2],
                         time[last]+PeriodSeconds(), low[i]);
            ObjectSetInteger(0, name, OBJPROP_COLOR, BullFVG_Color);
            ObjectSetInteger(0, name, OBJPROP_FILL, true);
            ObjectSetInteger(0, name, OBJPROP_BACK, true);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
         }
         // Bearish FVG: high[i] < low[i-2]
         if(high[i] < low[i-2])
         {
            string name = "SCTA_FVGR_" + IntegerToString(i);
            ObjectCreate(0, name, OBJ_RECTANGLE, 0,
                         time[i],   low[i-2],
                         time[last]+PeriodSeconds(), high[i]);
            ObjectSetInteger(0, name, OBJPROP_COLOR, BearFVG_Color);
            ObjectSetInteger(0, name, OBJPROP_FILL, true);
            ObjectSetInteger(0, name, OBJPROP_BACK, true);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
         }
      }
   }

   // ----- B) Swings H/L (BSL/SSL) ---------------------------------
   if(ShowSwings)
   {
      int W = Swing_Window;
      for(int i=first+W; i<=last-W; i++)
      {
         bool isSwingH = true, isSwingL = true;
         for(int k=1; k<=W; k++)
         {
            if(high[i-k] >= high[i] || high[i+k] >= high[i]) isSwingH = false;
            if(low [i-k] <= low [i] || low [i+k] <= low [i]) isSwingL = false;
         }
         if(isSwingH)
         {
            string name = "SCTA_BSL_" + IntegerToString(i);
            ObjectCreate(0, name, OBJ_TREND, 0,
                         time[i],   high[i],
                         time[last]+PeriodSeconds(), high[i]);
            ObjectSetInteger(0, name, OBJPROP_COLOR, BSL_Color);
            ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DASHDOT);
            ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
            ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
            ObjectSetInteger(0, name, OBJPROP_BACK, true);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
         }
         if(isSwingL)
         {
            string name = "SCTA_SSL_" + IntegerToString(i);
            ObjectCreate(0, name, OBJ_TREND, 0,
                         time[i],   low[i],
                         time[last]+PeriodSeconds(), low[i]);
            ObjectSetInteger(0, name, OBJPROP_COLOR, SSL_Color);
            ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DASHDOT);
            ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
            ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
            ObjectSetInteger(0, name, OBJPROP_BACK, true);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
         }
      }
   }

   // ----- C) Fibonacci da onda (33 / 50 / 66) ---------------------
   if(ShowFibo)
   {
      // Mesmo algoritmo do get_current_wave() em indicators.py
      int    hiIdx=first, loIdx=first;
      double hiVal=high[first], loVal=low[first];
      for(int i=first; i<=last; i++)
      {
         if(high[i] > hiVal) { hiVal=high[i]; hiIdx=i; }
         if(low [i] < loVal) { loVal=low [i]; loIdx=i; }
      }
      if(hiVal == loVal) return;

      double origin=0, extreme=0;
      int    startBar=0;
      if(loIdx < hiIdx)
      {
         origin  = loVal; extreme = hiVal; startBar = loIdx;
      }
      else
      {
         origin  = hiVal; extreme = loVal; startBar = hiIdx;
      }
      double l33 = extreme - (extreme - origin) * 0.333;
      double l50 = extreme - (extreme - origin) * 0.500;
      double l66 = extreme - (extreme - origin) * 0.666;
      if(loIdx > hiIdx)
      {
         l33 = extreme + (origin - extreme) * 0.333;
         l50 = extreme + (origin - extreme) * 0.500;
         l66 = extreme + (origin - extreme) * 0.666;
      }

      datetime t0 = time[startBar];
      datetime t1 = time[last] + PeriodSeconds();
      DrawHLine("SCTA_FIB33", t0, t1, l33, Fib33_Color);
      DrawHLine("SCTA_FIB50", t0, t1, l50, Fib50_Color);
      DrawHLine("SCTA_FIB66", t0, t1, l66, Fib66_Color);
   }

   // ----- D) Liquidity Sweeps (wick + reversão) --------------------
   if(ShowSweeps)
   {
      // Sweep = high[i] > max(high[i-lookback..i-1]) e close[i] < max(...)
      int L = 20;
      for(int i=MathMax(first+L, 1); i<=last; i++)
      {
         double prevHi = high[i-1], prevLo = low[i-1];
         for(int k=2; k<=L && i-k>=first; k++)
         {
            if(high[i-k] > prevHi) prevHi = high[i-k];
            if(low [i-k] < prevLo) prevLo = low [i-k];
         }
         // Sweep BSL → cor laranja, marcador LOSANGO
         if(high[i] > prevHi && close[i] < prevHi)
            DrawSweepMarker("SCTA_SW_BSL_" + IntegerToString(i),
                            time[i], prevHi, BSL_Color);
         // Sweep SSL → cor roxa
         if(low[i] < prevLo && close[i] > prevLo)
            DrawSweepMarker("SCTA_SW_SSL_" + IntegerToString(i),
                            time[i], prevLo, SSL_Color);
      }
   }

   // ----- E) Volume Profile (lateral, histograma + POC/VAH/VAL) ---
   if(ShowVolumeProfile)
   {
      BuildVolumeProfile(time, open, high, low, close, tick_volume);
   }

   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Desenha linha horizontal de Fibo                                    |
//+------------------------------------------------------------------+
void DrawHLine(const string base, const datetime t0, const datetime t1,
               const double price, const color clr)
{
   ObjectCreate(0, base, OBJ_TREND, 0, t0, price, t1, price);
   ObjectSetInteger(0, base, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, base, OBJPROP_STYLE, STYLE_DOT);
   ObjectSetInteger(0, base, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, base, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, base, OBJPROP_BACK, true);
   ObjectSetInteger(0, base, OBJPROP_SELECTABLE, false);
}

//+------------------------------------------------------------------+
//| Marcador de Sweep (losango colorido)                               |
//+------------------------------------------------------------------+
void DrawSweepMarker(const string name, const datetime t, const double price, color clr)
{
   ObjectCreate(0, name, OBJ_ARROW, 0, t, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR,     clr);
   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, 177); // losango
   ObjectSetInteger(0, name, OBJPROP_WIDTH,     3);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

//+------------------------------------------------------------------+
//| Volume Profile (POC + Value Area + histograma lateral)              |
//| Mesmo algoritmo de build_volume_profile() do indicators.py         |
//+------------------------------------------------------------------+
void BuildVolumeProfile(const datetime &time[],
                        const double &open[],
                        const double &high[],
                        const double &low[],
                        const double &close[],
                        const long &tick_volume[])
{
   int rates_total = ArraySize(time);
   int first = MathMax(0, rates_total - VP_Lookback);
   int last  = rates_total - 1;
   if(last < first) return;

   // 1. range de preço e bins ---------------------------------------
   double lo = low[first], hi = high[first];
   for(int i=first; i<=last; i++)
   {
      if(low[i]  < lo) lo = low[i];
      if(high[i] > hi) hi = high[i];
   }
   if(hi <= lo) return;

   int    bins  = MathMax(5, VP_Bins);
   double step  = (hi - lo) / bins;
   if(step <= 0) return;
   double profile[];
   ArrayResize(profile, bins);
   ArrayInitialize(profile, 0.0);

   // 2. distribuição uniforme pelo range Low→High + peso extra no close
   for(int i=first; i<=last; i++)
   {
      double v = (double)tick_volume[i];
      if(v <= 0) continue;
      int iLo = (int)MathFloor((low[i]  - lo) / step);
      int iHi = (int)MathFloor((high[i] - lo) / step);
      int iCl = (int)MathFloor((close[i]- lo) / step);
      if(iLo < 0) iLo = 0;
      if(iHi >= bins) iHi = bins-1;
      if(iCl < 0) iCl = 0;
      if(iCl >= bins) iCl = bins-1;
      int span = MathMax(iHi - iLo + 1, 1);
      double base = v / span;
      for(int k=iLo; k<=iHi; k++) profile[k] += base;
      profile[iCl] += base;   // peso dobrado no fechamento
   }

   // 3. POC = bin de maior volume -----------------------------------
   int    pocIdx = 0;
   double pocVol = profile[0];
   for(int k=1; k<bins; k++)
      if(profile[k] > pocVol) { pocVol = profile[k]; pocIdx = k; }
   double pocPrice = lo + (pocIdx + 0.5) * step;

   // 4. Value Area (default 70% do volume) --------------------------
   double totalVol = 0;
   for(int k=0; k<bins; k++) totalVol += profile[k];
   if(totalVol <= 0) return;

   double target = totalVol * VP_ValueAreaPct;
   double cur    = profile[pocIdx];
   int    loIdx  = pocIdx, hiIdx = pocIdx;
   while(cur < target)
   {
      double nextLo = (loIdx > 0)     ? profile[loIdx-1] : 0;
      double nextHi = (hiIdx < bins-1)? profile[hiIdx+1] : 0;
      if(nextLo == 0 && nextHi == 0) break;
      if(nextLo >= nextHi)
      { loIdx--; cur += profile[loIdx]; }
      else
      { hiIdx++; cur += profile[hiIdx]; }
   }
   double vahPrice = lo + (hiIdx + 1.0) * step;
   double valPrice = lo + (loIdx) * step;

   // 5. desenha histograma lateral (barras horizontais a partir do fim do VP)
   datetime tEnd = time[last] + PeriodSeconds();
   double   maxVol = pocVol;
   if(maxVol <= 0) maxVol = 1;
   // largura visual de cada barra (em pixels) → traduzimos em preço
   double px2price = (hi - lo) * 0.012;  // ~1.2% do range
   for(int k=0; k<bins; k++)
   {
      if(profile[k] <= 0) continue;
      double mid = lo + (k + 0.5) * step;
      double w   = (profile[k] / maxVol) * px2price;
      bool   isUp = (mid >= close[last]);

      string name = "SCTA_VP_" + IntegerToString(k);
      ObjectCreate(0, name, OBJ_TREND, 0, tEnd, mid, tEnd + (datetime)MathRound(w/_Point), mid);
      ObjectSetInteger(0, name, OBJPROP_COLOR, isUp ? VP_Up_Color : VP_Dn_Color);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 3);
      ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   }

   // 6. linhas POC / VAH / VAL (na janela toda)
   datetime t0 = time[first];
   DrawHLineVP("SCTA_POC", t0, tEnd, pocPrice, VP_POC_Color, STYLE_SOLID, 2);
   DrawHLineVP("SCTA_VAH", t0, tEnd, vahPrice, VP_VAH_Color, STYLE_DASH,  1);
   DrawHLineVP("SCTA_VAL", t0, tEnd, valPrice, VP_VAL_Color, STYLE_DASH,  1);
}

//+------------------------------------------------------------------+
void DrawHLineVP(const string base, const datetime t0, const datetime t1,
                 const double price, const color clr,
                 const ENUM_LINE_STYLE style, const int width)
{
   ObjectCreate(0, base, OBJ_TREND, 0, t0, price, t1, price);
   ObjectSetInteger(0, base, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, base, OBJPROP_STYLE, style);
   ObjectSetInteger(0, base, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, base, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, base, OBJPROP_BACK, true);
   ObjectSetInteger(0, base, OBJPROP_SELECTABLE, false);
}

//+------------------------------------------------------------------+
//| INSTALAÇÃO                                                         |
//| 1) MetaEditor → File → Open → SmartCloudTrend_App.mq5             |
//| 2) Compile (F7). Sem erros = OK.                                  |
//| 3) MT5 → Navigator → Indicators → SmartCloudTrend_App → duplo-clique |
//|    no gráfico do ativo (mesmo timeframe do app.py).                |
//|                                                                  |
//| ALINHAMENTO COM O APP (Streamlit/Plotly)                          |
//|  • app.py usa SMA(9), SMA(21), SMA(200) → inputs SMA_Fast/SMA_Mid/SMA_Slow
//|  • app.py usa BB(20, 2σ)                → inputs BB_Period/BB_Dev
//|  • app.py usa EMA(8) e EMA(20) com nuvem → inputs Cloud_Fast/Cloud_Slow
//|  • app.py plota FVGs, BSL/SSL, Fibo 33/50/66, Sweeps, Volume Profile
//|    lateral e histograma de Volume — tudo espelhado aqui.          |
//|                                                                  |
//| PERFORMANCE                                                        |
//|  • Em M1 com 5000 velas, o Volume Profile e SMC recalculam muitos |
//|    objetos a cada tick — esperado. Em M5/M15 fica leve.            |
//|  • Se travar, reduza VP_Lookback (200 → 100) e SMC_Lookback (150 → 80).
//|                                                                  |
//| LIMITAÇÕES HONESTAS                                                |
//|  • Os "sinais do modelo" aqui usam a regra clássica do cruzamento |
//|    EMA8/EMA20 alinhado às SMAs — é uma simplificação do ML real   |
//|    do app.py (RandomForest). Para 100% de paridade com o modelo, |
//|    exporte o .pkl (joblib) e integre via DLL Python ou retreine.  |
//|  • O "subpainel" de volume do app virou histograma fino na mesma |
//|    janela (MQL5 não permite misturar chart_window + subwindow no  |
//|    mesmo .mq5). Visual equivalente.                               |
//+------------------------------------------------------------------+
