//+------------------------------------------------------------------+
//| Clarezas_Logica.mq5                                              |
//| Núcleo da estratégia: Fluxo + Absorção + Continuação             |
//| Plugue no seu EA - sem painel, sem execução                      |
//+------------------------------------------------------------------+
#property copyright "Clarezas"
#property version   "1.00"
#property strict

//+------------------------------------------------------------------+
//| CONFIG                                                           |
//+------------------------------------------------------------------+
input int    VolumePeriod    = 20;      // Período da média de volume
input double VolumeMultiplier = 1.5;    // Multiplicador para volume alto
input double BodyRatio       = 0.3;     // Razão corpo/range para absorção

//+------------------------------------------------------------------+
//| FUNÇÕES AUXILIARES                                                |
//+------------------------------------------------------------------+
double CandleBody(int i)
{
   return MathAbs(iClose(_Symbol, _Period, i) - iOpen(_Symbol, _Period, i));
}

double CandleRange(int i)
{
   return iHigh(_Symbol, _Period, i) - iLow(_Symbol, _Period, i);
}

double AvgVolume(int period)
{
   double sum = 0;
   for(int i = 1; i <= period; i++)
      sum += iVolume(_Symbol, _Period, i);
   return sum / period;
}

//+------------------------------------------------------------------+
//| DETECTAR FLUXO FORTE                                              |
//+------------------------------------------------------------------+
bool StrongBuyFlow()
{
   double vol = iVolume(_Symbol, _Period, 1);
   double avg = AvgVolume(VolumePeriod);

   return (vol > avg * VolumeMultiplier &&
           iClose(_Symbol, _Period, 1) > iOpen(_Symbol, _Period, 1));
}

bool StrongSellFlow()
{
   double vol = iVolume(_Symbol, _Period, 1);
   double avg = AvgVolume(VolumePeriod);

   return (vol > avg * VolumeMultiplier &&
           iClose(_Symbol, _Period, 1) < iOpen(_Symbol, _Period, 1));
}

//+------------------------------------------------------------------+
//| DETECTAR ABSORÇÃO                                                |
//+------------------------------------------------------------------+
bool Absorption()
{
   double vol = iVolume(_Symbol, _Period, 1);
   double avg = AvgVolume(VolumePeriod);

   double body  = CandleBody(1);
   double range = CandleRange(1);

   if(range == 0) return false;

   return (vol > avg * VolumeMultiplier &&
           (body / range) < BodyRatio);
}

//+------------------------------------------------------------------+
//| PULLBACK                                                         |
//+------------------------------------------------------------------+
bool PullbackBuy()
{
   return (iClose(_Symbol, _Period, 2) > iOpen(_Symbol, _Period, 2) &&
           iClose(_Symbol, _Period, 1) < iOpen(_Symbol, _Period, 1));
}

bool PullbackSell()
{
   return (iClose(_Symbol, _Period, 2) < iOpen(_Symbol, _Period, 2) &&
           iClose(_Symbol, _Period, 1) > iOpen(_Symbol, _Period, 1));
}

//+------------------------------------------------------------------+
//| CONTINUAÇÃO                                                      |
//+------------------------------------------------------------------+
bool ContinuationBuy()
{
   return (StrongBuyFlow() &&
           PullbackBuy() &&
           iClose(_Symbol, _Period, 0) > iHigh(_Symbol, _Period, 1));
}

bool ContinuationSell()
{
   return (StrongSellFlow() &&
           PullbackSell() &&
           iClose(_Symbol, _Period, 0) < iLow(_Symbol, _Period, 1));
}
//+------------------------------------------------------------------+
