//+------------------------------------------------------------------+
//| SignalBotSignals.mq5                                              |
//| Draws Signal Bot's Scanner C signals on the chart: entry arrow,   |
//| entry / SL / TP1 levels, the exit with WIN / LOSS and the result  |
//| in R. Reads <Common>\Files\signalbot\signals.csv, which the bot   |
//| rewrites every few minutes.                                       |
//|                                                                   |
//| DISPLAY ONLY: this indicator never opens, changes or closes any   |
//| order or position.                                                |
//|                                                                   |
//| Install: MetaEditor > open > Compile (F7), then drag              |
//| "SignalBotSignals" from Navigator > Indicators onto a chart of a  |
//| watched symbol (XAUUSD, EURUSD, USDJPY, USDTHB ...).              |
//+------------------------------------------------------------------+
#property copyright   "Signal Bot"
#property version     "1.00"
#property description "Draws Signal Bot signals (entry, SL, TP1, exit, WIN/LOSS). Display only, never trades."
#property indicator_chart_window
#property indicator_plots 0

input string InpFile       = "signalbot\\signals.csv"; // file in the Common Files folder
input int    InpRefreshSec = 10;             // re-read the file every N seconds
input bool   InpShowClosed = true;           // also draw finished signals (last 30 days)
input bool   InpPreview    = false;          // draw two sample signals to preview the look
input color  InpEntryColor = clrDodgerBlue;  // entry arrow and entry level
input color  InpSLColor    = clrTomato;      // stop loss
input color  InpTPColor    = clrLimeGreen;   // TP1 (half off)
input color  InpWinColor   = clrLimeGreen;   // finished: win
input color  InpLossColor  = clrTomato;      // finished: loss
input int    InpFontSize   = 8;
input bool   InpSnapshot   = true;           // save one picture of the chart after loading (MQL5\Files\signalbot_*.png)
input bool   InpPanel      = true;           // box in the lower-left corner with entry / SL / TP1 of open signals
                                             // (the lines can sit outside the visible price range)

#define PFX "SBS_"

datetime g_lastModify = 0;
bool     g_logNext    = true;                // print what was drawn (once, after loading)
bool     g_snapDone   = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(MathMax(InpRefreshSec, 2));
   Redraw(true);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PFX);
   ChartRedraw();
  }

void OnTimer()
  {
   Redraw(false);
   if(InpSnapshot && !g_snapDone)
     {
      g_snapDone = true;
      string file = "signalbot_" + _Symbol + "_" + StringSubstr(EnumToString(_Period), 7) + ".png";
      if(ChartScreenShot(0, file, 1600, 900, ALIGN_RIGHT))
         Print("SignalBotSignals: chart picture saved to MQL5\\Files\\", file);
     }
  }

int CountObjects()
  {
   int n = 0;
   for(int i = ObjectsTotal(0, -1, -1) - 1; i >= 0; i--)
      if(StringFind(ObjectName(0, i, -1, -1), PFX) == 0)
         n++;
   return(n);
  }

int OnCalculate(const int rates_total, const int prev_calculated, const int begin, const double &price[])
  {
   return(rates_total);
  }

//+------------------------------------------------------------------+
//| broker server time minus UTC, rounded to 15 minutes               |
//+------------------------------------------------------------------+
int ServerOffset()
  {
   long off = (long)TimeTradeServer() - (long)TimeGMT();
   return((int)(MathRound(off / 900.0) * 900));
  }

bool SameSymbol(string s)
  {
   string a = _Symbol, b = s;
   StringToUpper(a);
   StringToUpper(b);
   return(StringLen(b) > 0 && StringFind(a, b) == 0);     // XAUUSD matches XAUUSD, XAUUSD.m, ...
  }

string Px(double v)
  {
   return(DoubleToString(v, _Digits));
  }

//+------------------------------------------------------------------+
//| drawing helpers                                                   |
//+------------------------------------------------------------------+
void Segment(string name, datetime t1, double p1, datetime t2, double p2, color clr, int style, int width, bool ray, string tip)
  {
   ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, ray);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, tip);
  }

void Label(string name, datetime t, double p, string text, color clr, ENUM_ANCHOR_POINT anchor)
  {
   ObjectCreate(0, name, OBJ_TEXT, 0, t, p);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, InpFontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, text);
  }

void Marker(string name, ENUM_OBJECT type, datetime t, double p, color clr, string tip)
  {
   ObjectCreate(0, name, type, 0, t, p);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, tip);
  }

void PanelLine(int row, string text, color clr)
  {
   string name = PFX + "panel" + IntegerToString(row);
   ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 10 + row * (InpFontSize + 8));
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, InpFontSize + 1);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

//+------------------------------------------------------------------+
//| one signal                                                        |
//+------------------------------------------------------------------+
void DrawSignal(string no, string side, string status, datetime tEntry, double entry, double slInitial,
                double slNow, double tp1, bool tp1Hit, datetime tExit, double exitPrice, double resultR)
  {
   bool open = (status == "active" || status == "partial" || status == "weakened");
   if(!open && !InpShowClosed)
      return;
   string key = PFX + (no != "" ? no : IntegerToString((long)tEntry)) + "_";
   string tag = (no != "" ? "#" + no + " " : "") + side;
   datetime tEnd = open ? TimeCurrent() : tExit;
   if(tEnd <= tEntry)
      tEnd = tEntry + PeriodSeconds();

   // entry arrow and label
   Marker(key + "in", OBJ_ARROW_BUY, tEntry, entry, InpEntryColor, tag + " entry " + Px(entry));
   Label(key + "tag", tEntry, entry, " " + tag + " " + Px(entry), InpEntryColor, ANCHOR_LEFT_LOWER);

   // levels: entry, initial SL, TP1 (rays to the right while the signal is open)
   Segment(key + "en", tEntry, entry, tEnd, entry, InpEntryColor, STYLE_DOT, 1, open, tag + " entry " + Px(entry));
   Segment(key + "sl0", tEntry, slInitial, tEnd, slInitial, InpSLColor, STYLE_DASH, 1, open && slNow <= slInitial,
           tag + " SL " + Px(slInitial));
   Label(key + "sl0t", tEntry, slInitial, " SL " + Px(slInitial), InpSLColor, ANCHOR_LEFT_UPPER);
   Segment(key + "tp", tEntry, tp1, tEnd, tp1, InpTPColor, STYLE_DASH, 1, open && !tp1Hit,
           tag + " TP1 " + Px(tp1) + (tp1Hit ? " (hit, half closed)" : " (half off)"));
   Label(key + "tpt", tEntry, tp1, " TP1 " + Px(tp1) + (tp1Hit ? " hit" : ""), InpTPColor, ANCHOR_LEFT_LOWER);

   if(open)
     {
      // the trailing stop now (only when it has moved from the initial SL)
      if(slNow > slInitial)
        {
         Segment(key + "sl", tEntry, slNow, TimeCurrent(), slNow, InpSLColor, STYLE_SOLID, 2, true, tag + " SL now " + Px(slNow));
         Label(key + "slt", tEntry, slNow, " SL now " + Px(slNow), InpSLColor, ANCHOR_LEFT_UPPER);
        }
      return;
     }

   // finished: exit marker, path from entry to exit, WIN / LOSS text
   bool win = resultR > 0.05;
   bool loss = resultR < -0.05;
   color clr = win ? InpWinColor : (loss ? InpLossColor : clrSilver);
   string word = win ? "WIN" : (loss ? "LOSS" : "FLAT");
   string how = (status == "expired") ? "time" : "SL";
   string res = tag + " " + word + " " + (resultR >= 0 ? "+" : "") + DoubleToString(resultR, 2) + "R";
   Segment(key + "path", tEntry, entry, tExit, exitPrice, clr, STYLE_DOT, 1, false, res);
   Marker(key + "out", win ? OBJ_ARROW_CHECK : OBJ_ARROW_STOP, tExit, exitPrice, clr, res + " exit " + Px(exitPrice) + " (" + how + ")");
   Label(key + "outt", tExit, exitPrice, " " + res, clr, ANCHOR_LEFT_UPPER);
  }

//+------------------------------------------------------------------+
//| sample signals around recent bars (InpPreview)                    |
//+------------------------------------------------------------------+
void DrawPreview()
  {
   int bars = Bars(_Symbol, _Period);
   if(bars < 80)
      return;
   double range = 0;
   for(int i = 1; i <= 14; i++)
      range += iHigh(_Symbol, _Period, i) - iLow(_Symbol, _Period, i);
   double atr = 2.0 * range / 14.0;
   // a finished one: entry 60 bars ago, exit 35 bars ago
   double e1 = iClose(_Symbol, _Period, 60), x1 = iClose(_Symbol, _Period, 35);
   DrawSignal("P1", "BUY PREVIEW", "stopped", iTime(_Symbol, _Period, 60), e1, e1 - atr, e1 - atr, e1 + atr,
              x1 >= e1 + atr, iTime(_Symbol, _Period, 35), x1, (x1 - e1) / atr);
   // an open one: entry 15 bars ago
   double e2 = iClose(_Symbol, _Period, 15);
   DrawSignal("P2", "BUY PREVIEW", "active", iTime(_Symbol, _Period, 15), e2, e2 - atr, e2 - atr, e2 + atr,
              false, 0, 0, 0);
  }

//+------------------------------------------------------------------+
//| read the file and redraw everything for this chart's symbol      |
//+------------------------------------------------------------------+
void Redraw(bool force)
  {
   datetime modified = (datetime)FileGetInteger(InpFile, FILE_MODIFY_DATE, true);
   if(!force && modified == g_lastModify && !InpPreview)
      return;
   g_lastModify = modified;
   ObjectsDeleteAll(0, PFX);

   if(InpPreview)
      DrawPreview();

   int h = FileOpen(InpFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
     {
      ChartRedraw();
      return;
     }
   int offset = ServerOffset();
   string panel[];
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      string f[];
      int n = StringSplit(line, ',', f);
      if(n < 13 || f[0] == "no")
         continue;
      if(!SameSymbol(f[1]))
         continue;
      datetime tEntry = (datetime)(StringToInteger(f[4]) + offset);
      long exitUtc = StringToInteger(f[10]);
      datetime tExit = exitUtc > 0 ? (datetime)(exitUtc + offset) : 0;
      DrawSignal(f[0], f[2], f[3], tEntry, StringToDouble(f[5]), StringToDouble(f[6]), StringToDouble(f[7]),
                 StringToDouble(f[8]), f[9] == "1", tExit, StringToDouble(f[11]), StringToDouble(f[12]));
      if(exitUtc == 0)
        {
         int k = ArraySize(panel);
         ArrayResize(panel, k + 1);
         double sl0 = StringToDouble(f[6]), sl = StringToDouble(f[7]);
         panel[k] = (f[0] != "" ? "#" + f[0] + " " : "") + f[2] + " " + Px(StringToDouble(f[5]))
                    + " | SL " + Px(sl > sl0 ? sl : sl0) + (sl > sl0 ? " (moved)" : "")
                    + " | TP1 " + Px(StringToDouble(f[8])) + (f[9] == "1" ? " hit" : "");
        }
      if(g_logNext)
         PrintFormat("SignalBotSignals %s: %s%s %s entry %s @ %s | SL %s | TP1 %s%s",
                     _Symbol, f[0] != "" ? "#" + f[0] + " " : "", f[2], f[3], TimeToString(tEntry),
                     Px(StringToDouble(f[5])), Px(StringToDouble(f[7])), Px(StringToDouble(f[8])),
                     tExit > 0 ? " | exit " + TimeToString(tExit) + " @ " + Px(StringToDouble(f[11])) + " " + f[12] + "R" : "");
     }
   FileClose(h);
   if(InpPanel && ArraySize(panel) > 0)
     {
      // rows count up from the bottom: oldest first, newest just under the title
      int rows = ArraySize(panel);
      for(int i = 0; i < rows; i++)
         PanelLine(i, panel[i], InpEntryColor);
      PanelLine(rows, "Signal Bot - open signal" + (rows > 1 ? "s" : "") + " " + _Symbol, clrSilver);
     }
   ChartRedraw();
   if(g_logNext)
      PrintFormat("SignalBotSignals %s: %d chart object(s) drawn, server offset %+d h", _Symbol, CountObjects(), offset / 3600);
   g_logNext = false;
  }
//+------------------------------------------------------------------+
