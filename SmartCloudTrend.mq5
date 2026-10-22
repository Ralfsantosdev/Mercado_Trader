//+------------------------------------------------------------------+
//|                                              SmartCloudTrend.mq5 |
//+------------------------------------------------------------------+
#property indicator_chart_window
#property indicator_buffers 6
#property indicator_plots   5

// EMA 20 (rápida - varejo)
#property indicator_label1  "EMA 20"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrTurquoise
#property indicator_width1  2

// EMA 50 (lenta - institucional)
#property indicator_label2  "EMA 50"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrTomato
#property indicator_width2  2

// Nuvem (Preenchimento)
#property indicator_label3  "Cloud"
#property indicator_type3   DRAW_FILLING
#property indicator_color3  clrSeaGreen, clrTomato // Verde para alta, Vermelho para baixa

// Seta Compra
#property indicator_label4  "Buy Signal"
#property indicator_type4   DRAW_ARROW
#property indicator_color4  clrLime
#property indicator_width4  2

// Seta Venda
#property indicator_label5  "Sell Signal"
#property indicator_type5   DRAW_ARROW
#property indicator_color5  clrRed
#property indicator_width5  2

double ema20[], ema50[];
double cloudUp[], cloudDown[];
double buySignal[], sellSignal[];

input int fastEMA = 20;  // EMA rápida (varejo)
input int slowEMA = 50;  // EMA lenta (institucional)

int handle_ema20;
int handle_ema50;

//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, ema20, INDICATOR_DATA);
   SetIndexBuffer(1, ema50, INDICATOR_DATA);
   SetIndexBuffer(2, cloudUp, INDICATOR_DATA);
   SetIndexBuffer(3, cloudDown, INDICATOR_DATA);
   SetIndexBuffer(4, buySignal, INDICATOR_DATA);
   SetIndexBuffer(5, sellSignal, INDICATOR_DATA);

   // Configura códigos das setas (Wingdings)
   PlotIndexSetInteger(3, PLOT_ARROW, 233); // Seta para cima
   PlotIndexSetInteger(4, PLOT_ARROW, 234); // Seta para baixo

   // Pega o handle das médias móveis no MT5
   handle_ema20 = iMA(NULL, 0, fastEMA, 0, MODE_EMA, PRICE_CLOSE);
   handle_ema50 = iMA(NULL, 0, slowEMA, 0, MODE_EMA, PRICE_CLOSE);

   if(handle_ema20 == INVALID_HANDLE || handle_ema50 == INVALID_HANDLE)
      return(INIT_FAILED);

   return(INIT_SUCCEEDED);
}

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
   if(rates_total < slowEMA) return 0;
   
   int limit = prev_calculated - 1;
   if(limit < 1) limit = 1;

   // Copia os valores reais das médias para os buffers
   if(CopyBuffer(handle_ema20, 0, 0, rates_total, ema20) <= 0) return 0;
   if(CopyBuffer(handle_ema50, 0, 0, rates_total, ema50) <= 0) return 0;

   for(int i = limit; i < rates_total; i++)
   {
      // Desenha a nuvem
      cloudUp[i]   = ema20[i];
      cloudDown[i] = ema50[i];

      // Zera as setas para não ficarem presas na tela
      buySignal[i]  = EMPTY_VALUE;
      sellSignal[i] = EMPTY_VALUE;

      // Condição BUY (cruzamento para cima)
      if(ema20[i] > ema50[i] && ema20[i-1] <= ema50[i-1])
      {
         buySignal[i] = low[i] - 15 * _Point;
      }

      // Condição SELL (cruzamento para baixo)
      if(ema20[i] < ema50[i] && ema20[i-1] >= ema50[i-1])
      {
         sellSignal[i] = high[i] + 15 * _Point;
      }
   }

   return(rates_total);
}
