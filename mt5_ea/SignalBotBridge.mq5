//+------------------------------------------------------------------+
//| SignalBotBridge.mq5                                               |
//| Reads order requests written by Signal Bot (Telegram confirm      |
//| button) from  <Common>\Files\signalbot\orders.csv, validates them |
//| and writes the outcome to results.csv. A heartbeat.txt timestamp  |
//| tells the bot the EA is alive.                                    |
//|                                                                   |
//| THE ORDER-SENDING LINE IS INTENTIONALLY NOT INCLUDED. Find the    |
//| block marked  >>> ADD ORDER CODE HERE <<<  in HandleRequest() and |
//| add it yourself (see CTrade::Buy in the MQL5 reference). Until    |
//| then, and while InpDryRun = true, the EA only validates requests  |
//| and reports what it WOULD do.                                     |
//|                                                                   |
//| Install: MetaEditor > File > Open this file > Compile (F7), then  |
//| drag "SignalBotBridge" from Navigator > Expert Advisors onto any   |
//| chart, tick "Allow Algo Trading" in its settings and press the    |
//| AutoTrading button. Use a DEMO account first.                     |
//+------------------------------------------------------------------+
#property copyright "Signal Bot"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

input int    InpMaxPositions  = 3;      // max open positions in total
input int    InpMaxAgeMinutes = 15;     // ignore requests older than this
input int    InpPollSeconds   = 5;      // how often to read orders.csv
input ulong  InpMagic         = 20261022; // magic number for orders of this EA
input bool   InpDryRun        = true;   // true = validate and report only, never trade
input double InpMaxLots       = 1.0;    // hard cap on lot size per request

string g_dir       = "signalbot";
string g_orders    = "signalbot\\orders.csv";
string g_results   = "signalbot\\results.csv";
string g_processed = "signalbot\\processed.txt";
string g_heartbeat = "signalbot\\heartbeat.txt";

//+------------------------------------------------------------------+
int OnInit()
  {
   FolderCreate(g_dir, FILE_COMMON);
   EventSetTimer(InpPollSeconds);
   WriteHeartbeat();
   Print("SignalBotBridge started. DryRun=", InpDryRun, " folder=", TerminalInfoString(TERMINAL_COMMONDATA_PATH), "\\Files\\", g_dir);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
  {
   WriteHeartbeat();
   ProcessOrders();
  }

//+------------------------------------------------------------------+
//| Heartbeat: the bot warns if this file stops updating               |
//+------------------------------------------------------------------+
void WriteHeartbeat()
  {
   int h = FileOpen(g_heartbeat, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   FileWriteString(h, TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS) + "\n");
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| processed.txt keeps the ids already handled (one per line)        |
//+------------------------------------------------------------------+
bool IsProcessed(string id)
  {
   int h = FileOpen(g_processed, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return(false);
   bool found = false;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      StringTrimRight(line); StringTrimLeft(line);
      if(line == id) { found = true; break; }
     }
   FileClose(h);
   return(found);
  }

void MarkProcessed(string id)
  {
   int h = FileOpen(g_processed, FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return;
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, id + "\n");
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| results.csv: id,processed_utc,status,ticket,price,lots,message    |
//+------------------------------------------------------------------+
void WriteResult(string id, string status, ulong ticket, double price, double lots, string msg)
  {
   bool newFile = !FileIsExist(g_results, FILE_COMMON);
   int h = FileOpen(g_results, FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) { Print("cannot open results file: ", GetLastError()); return; }
   FileSeek(h, 0, SEEK_END);
   if(newFile) FileWriteString(h, "id,processed_utc,status,ticket,price,lots,message\n");
   StringReplace(msg, ",", ";");
   FileWriteString(h, id + "," + TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS) + "," + status + "," +
                   IntegerToString((long)ticket) + "," + DoubleToString(price, 5) + "," +
                   DoubleToString(lots, 2) + "," + msg + "\n");
   FileClose(h);
   Print("result ", id, " ", status, " ", msg);
  }

//+------------------------------------------------------------------+
//| orders.csv: id,created_utc,symbol,side,lots,sl,tp,entry_ref,comment|
//+------------------------------------------------------------------+
void ProcessOrders()
  {
   if(!FileIsExist(g_orders, FILE_COMMON)) return;
   int h = FileOpen(g_orders, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      if(StringLen(line) < 10) continue;
      string parts[];
      int n = StringSplit(line, ',', parts);
      if(n < 8 || parts[0] == "id") continue;
      string id = parts[0];
      if(IsProcessed(id)) continue;
      HandleRequest(id, parts[1], parts[2], parts[3], StringToDouble(parts[4]),
                    StringToDouble(parts[5]), StringToDouble(parts[6]), StringToDouble(parts[7]));
      MarkProcessed(id);
     }
   FileClose(h);
  }

bool HasPosition(string symbol)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == symbol) return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
void HandleRequest(string id, string createdUtc, string symbol, string side,
                   double lots, double sl, double tp, double entryRef)
  {
   datetime created = StringToTime(createdUtc);          // "YYYY.MM.DD HH:MM:SS" written as UTC
   if(created > 0 && TimeGMT() - created > InpMaxAgeMinutes * 60)
     { WriteResult(id, "EXPIRED", 0, 0, lots, "request older than " + IntegerToString(InpMaxAgeMinutes) + " min"); return; }
   if(side != "BUY")
     { WriteResult(id, "REJECTED", 0, 0, lots, "only BUY is supported"); return; }
   if(!SymbolSelect(symbol, true))
     { WriteResult(id, "REJECTED", 0, 0, lots, "symbol not found at this broker"); return; }
   if(PositionsTotal() >= InpMaxPositions)
     { WriteResult(id, "REJECTED", 0, 0, lots, "max open positions reached"); return; }
   if(HasPosition(symbol))
     { WriteResult(id, "REJECTED", 0, 0, lots, "position already open in " + symbol); return; }

   // normalise the lot size to the broker's rules and the hard cap
   double vmin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax  = MathMin(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX), InpMaxLots);
   double vstep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / vstep + 0.0000001) * vstep;
   if(lots < vmin) lots = vmin;
   if(lots > vmax) lots = vmax;

   // stops: at least the broker's minimum distance from the current price
   int    digits  = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point   = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double ask     = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double minDist = (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   if(ask <= 0) { WriteResult(id, "REJECTED", 0, 0, lots, "no price (market closed?)"); return; }
   if(sl > 0 && ask - sl < minDist) sl = ask - minDist;
   if(tp > 0 && tp - ask < minDist) tp = ask + minDist;
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);
   if(sl <= 0) { WriteResult(id, "REJECTED", 0, ask, lots, "a stop loss is required"); return; }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
     { WriteResult(id, "REJECTED", 0, ask, lots, "AutoTrading or Allow Algo Trading is off"); return; }

   string plan = "BUY " + symbol + " " + DoubleToString(lots, 2) + " lots SL " + DoubleToString(sl, digits) + " TP " + DoubleToString(tp, digits);
   if(InpDryRun)
     { WriteResult(id, "DRYRUN", 0, ask, lots, "validated: " + plan + " (set InpDryRun=false after adding the order code)"); return; }

   // >>> ADD ORDER CODE HERE <<<
   // Send a market BUY for `symbol` with volume `lots`, stop loss `sl`, take profit `tp`
   // and magic number InpMagic (the MQL5 reference documents CTrade::Buy for this).
   // Set `sent` to the call's result, `ticket` to the resulting order/position ticket and
   // `price` to the fill price, or put the error text into `err`.
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   bool   sent   = trade.Buy(lots, symbol, 0.0, sl, tp, "signalbot " + id);
   uint   rc     = trade.ResultRetcode();
   if(sent && rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED && rc != TRADE_RETCODE_DONE_PARTIAL)
      sent = false;
   ulong  ticket = sent ? trade.ResultOrder() : 0;
   double price  = sent ? trade.ResultPrice() : ask;
   string err    = "retcode " + IntegerToString(rc) + " " + trade.ResultRetcodeDescription();
   // >>> END <<<

   if(sent) WriteResult(id, "FILLED", ticket, price, lots, plan);
   else     WriteResult(id, "FAILED", 0, price, lots, err);
  }
//+------------------------------------------------------------------+
