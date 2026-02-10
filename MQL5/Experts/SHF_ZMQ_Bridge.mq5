//+------------------------------------------------------------------+
//| SHF_ZMQ_Bridge.mq5 — SHF v5.6 ZeroMQ Bridge Expert Advisor      |
//| Provides bidirectional ZMQ communication between MT5 and Python  |
//|                                                                    |
//| Protocol:                                                          |
//|   REP socket on port 5555 — command/response (JSON)               |
//|   PUB socket on port 5556 — market data streaming (JSON)          |
//|                                                                    |
//| Commands: PING, ORDER_SEND, ORDER_MODIFY, ORDER_CLOSE, CLOSE_ALL |
//|           GET_POSITIONS, GET_ACCOUNT, GET_QUOTE, GET_SERVER_TIME, |
//|           SUBSCRIBE, UNSUBSCRIBE                                   |
//|                                                                    |
//| Data streams: TICK_{symbol}, BAR_{symbol}_{timeframe}             |
//+------------------------------------------------------------------+
#property copyright "SHF Trading Systems"
#property link      ""
#property version   "5.60"
#property strict

// --- ZMQ includes (mql-zmq library) ---
#include <Zmq/Zmq.mqh>

// --- Input parameters ---
input int      InpRepPort      = 5555;       // REP port (commands)
input int      InpPubPort      = 5556;       // PUB port (data streaming)
input string   InpHost         = "*";        // Bind host
input int      InpMagic        = 12345;      // Magic number for orders
input int      InpTimerMs      = 100;        // Timer interval (ms)
input int      InpMaxSlippage  = 20;         // Maximum slippage (points)

// --- ZMQ Context & Sockets ---
Context  g_context("SHF_v56");
Socket   g_rep(g_context, ZMQ_REP);
Socket   g_pub(g_context, ZMQ_PUB);

// --- State ---
bool     g_connected = false;
string   g_subscribed_ticks[];    // Symbols subscribed for tick data
string   g_subscribed_bars[];     // "SYMBOL_TF" subscribed for bar data
datetime g_last_bar_time[];       // Track last bar time per subscription

//+------------------------------------------------------------------+
//| Expert initialization                                              |
//+------------------------------------------------------------------+
int OnInit()
{
   // Bind REP socket
   string rep_addr = StringFormat("tcp://%s:%d", InpHost, InpRepPort);
   if(!g_rep.bind(rep_addr))
   {
      PrintFormat("ERROR: Failed to bind REP socket to %s", rep_addr);
      return INIT_FAILED;
   }
   g_rep.setReceiveTimeout(1);  // 1ms non-blocking poll
   g_rep.setLinger(0);
   
   // Bind PUB socket
   string pub_addr = StringFormat("tcp://%s:%d", InpHost, InpPubPort);
   if(!g_pub.bind(pub_addr))
   {
      PrintFormat("ERROR: Failed to bind PUB socket to %s", pub_addr);
      return INIT_FAILED;
   }
   g_pub.setLinger(0);
   
   g_connected = true;
   
   // Set millisecond timer
   EventSetMillisecondTimer(InpTimerMs);
   
   PrintFormat("SHF ZMQ Bridge v5.6 initialized");
   PrintFormat("  REP: %s | PUB: %s", rep_addr, pub_addr);
   PrintFormat("  Magic: %d | Timer: %dms", InpMagic, InpTimerMs);
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                            |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   
   g_rep.unbind(StringFormat("tcp://%s:%d", InpHost, InpRepPort));
   g_pub.unbind(StringFormat("tcp://%s:%d", InpHost, InpPubPort));
   
   g_connected = false;
   PrintFormat("SHF ZMQ Bridge shutdown (reason=%d)", reason);
}

//+------------------------------------------------------------------+
//| Timer handler — poll REP + stream PUB data                        |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_connected) return;
   
   // 1. Poll for incoming commands on REP socket
   PollCommands();
   
   // 2. Stream subscribed market data on PUB socket
   StreamTickData();
   StreamBarData();
}

//+------------------------------------------------------------------+
//| Also handle ticks for lowest latency streaming                    |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!g_connected) return;
   PollCommands();
   StreamTickData();
}

//+------------------------------------------------------------------+
//| Poll REP socket for incoming JSON commands                        |
//+------------------------------------------------------------------+
void PollCommands()
{
   ZmqMsg request;
   
   // Non-blocking receive
   if(!g_rep.recv(request, true))
      return;
   
   string msg_str = request.getData();
   if(StringLen(msg_str) == 0) return;
   
   // Parse command type
   string cmd_type = JsonGetString(msg_str, "type");
   string data_str = JsonGetObject(msg_str, "data");
   
   string response = "";
   
   if(cmd_type == "PING")
      response = HandlePing();
   else if(cmd_type == "ORDER_SEND")
      response = HandleOrderSend(data_str);
   else if(cmd_type == "ORDER_MODIFY")
      response = HandleOrderModify(data_str);
   else if(cmd_type == "ORDER_CLOSE")
      response = HandleOrderClose(data_str);
   else if(cmd_type == "CLOSE_ALL")
      response = HandleCloseAll(data_str);
   else if(cmd_type == "GET_POSITIONS")
      response = HandleGetPositions(data_str);
   else if(cmd_type == "GET_ACCOUNT")
      response = HandleGetAccount();
   else if(cmd_type == "GET_QUOTE")
      response = HandleGetQuote(data_str);
   else if(cmd_type == "GET_SERVER_TIME")
      response = HandleGetServerTime();
   else if(cmd_type == "SUBSCRIBE")
      response = HandleSubscribe(data_str);
   else if(cmd_type == "UNSUBSCRIBE")
      response = HandleUnsubscribe(data_str);
   else
      response = StringFormat("{\"error\":\"Unknown command: %s\"}", cmd_type);
   
   // Send response
   ZmqMsg reply(response);
   g_rep.send(reply);
}

//+------------------------------------------------------------------+
//| PING handler                                                       |
//+------------------------------------------------------------------+
string HandlePing()
{
   return "{\"status\":\"PONG\"}";
}

//+------------------------------------------------------------------+
//| ORDER_SEND handler                                                 |
//+------------------------------------------------------------------+
string HandleOrderSend(string data)
{
   string symbol    = JsonGetString(data, "symbol");
   string type_str  = JsonGetString(data, "order_type");
   double lots      = JsonGetDouble(data, "lots");
   double price     = JsonGetDouble(data, "price");
   double sl        = JsonGetDouble(data, "sl");
   double tp        = JsonGetDouble(data, "tp");
   int    deviation = (int)JsonGetDouble(data, "deviation");
   int    magic     = (int)JsonGetDouble(data, "magic");
   string comment   = JsonGetString(data, "comment");
   
   if(deviation <= 0) deviation = InpMaxSlippage;
   if(magic <= 0)     magic = InpMagic;
   if(StringLen(comment) == 0) comment = "SHF_ALGO";
   
   // Determine order type
   ENUM_ORDER_TYPE order_type;
   if(type_str == "ORDER_TYPE_BUY")           order_type = ORDER_TYPE_BUY;
   else if(type_str == "ORDER_TYPE_SELL")      order_type = ORDER_TYPE_SELL;
   else if(type_str == "ORDER_TYPE_BUY_LIMIT") order_type = ORDER_TYPE_BUY_LIMIT;
   else if(type_str == "ORDER_TYPE_SELL_LIMIT")order_type = ORDER_TYPE_SELL_LIMIT;
   else if(type_str == "ORDER_TYPE_BUY_STOP")  order_type = ORDER_TYPE_BUY_STOP;
   else if(type_str == "ORDER_TYPE_SELL_STOP") order_type = ORDER_TYPE_SELL_STOP;
   else
      return StringFormat("{\"success\":false,\"error_code\":-1,\"error_message\":\"Unknown order type: %s\"}", type_str);
   
   // Get price for market orders
   if(price <= 0.0)
   {
      if(order_type == ORDER_TYPE_BUY)
         price = SymbolInfoDouble(symbol, SYMBOL_ASK);
      else if(order_type == ORDER_TYPE_SELL)
         price = SymbolInfoDouble(symbol, SYMBOL_BID);
   }
   
   // Build trade request
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   
   req.action    = (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_SELL) 
                   ? TRADE_ACTION_DEAL : TRADE_ACTION_PENDING;
   req.symbol    = symbol;
   req.volume    = lots;
   req.type      = order_type;
   req.price     = price;
   req.sl        = sl;
   req.tp        = tp;
   req.deviation = (ulong)deviation;
   req.magic     = magic;
   req.comment   = comment;
   req.type_filling = ORDER_FILLING_IOC;
   
   bool result = OrderSend(req, res);
   
   if(result && res.retcode == TRADE_RETCODE_DONE)
   {
      return StringFormat(
         "{\"success\":true,\"ticket\":%d,\"order_type\":\"%s\",\"lots\":%.2f,"
         "\"price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"error_code\":0,\"error_message\":\"\"}",
         res.deal, type_str, lots, res.price, sl, tp
      );
   }
   else
   {
      return StringFormat(
         "{\"success\":false,\"ticket\":0,\"order_type\":\"%s\",\"lots\":%.2f,"
         "\"price\":0,\"sl\":0,\"tp\":0,\"error_code\":%d,\"error_message\":\"%s (retcode=%d)\"}",
         type_str, lots, (int)res.retcode,
         ResultRetcodeDescription(res.retcode), (int)res.retcode
      );
   }
}

//+------------------------------------------------------------------+
//| ORDER_MODIFY handler                                               |
//+------------------------------------------------------------------+
string HandleOrderModify(string data)
{
   ulong  ticket = (ulong)JsonGetDouble(data, "ticket");
   double sl     = JsonGetDouble(data, "sl");
   double tp     = JsonGetDouble(data, "tp");
   
   if(!PositionSelectByTicket(ticket))
      return StringFormat("{\"success\":false,\"error_message\":\"Position %d not found\"}", ticket);
   
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   
   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = PositionGetString(POSITION_SYMBOL);
   req.sl       = (sl > 0) ? sl : PositionGetDouble(POSITION_SL);
   req.tp       = (tp > 0) ? tp : PositionGetDouble(POSITION_TP);
   
   bool result = OrderSend(req, res);
   
   if(result && res.retcode == TRADE_RETCODE_DONE)
      return "{\"success\":true}";
   else
      return StringFormat("{\"success\":false,\"error_message\":\"%s\"}", ResultRetcodeDescription(res.retcode));
}

//+------------------------------------------------------------------+
//| ORDER_CLOSE handler                                                |
//+------------------------------------------------------------------+
string HandleOrderClose(string data)
{
   ulong  ticket = (ulong)JsonGetDouble(data, "ticket");
   double lots   = JsonGetDouble(data, "lots");
   
   if(!PositionSelectByTicket(ticket))
      return StringFormat("{\"success\":false,\"error_message\":\"Position %d not found\"}", ticket);
   
   string symbol = PositionGetString(POSITION_SYMBOL);
   long   pos_type = PositionGetInteger(POSITION_TYPE);
   double pos_lots = PositionGetDouble(POSITION_VOLUME);
   
   if(lots <= 0 || lots >= pos_lots)
      lots = pos_lots;
   
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   
   req.action    = TRADE_ACTION_DEAL;
   req.position  = ticket;
   req.symbol    = symbol;
   req.volume    = lots;
   req.deviation = (ulong)InpMaxSlippage;
   req.type_filling = ORDER_FILLING_IOC;
   
   // Reverse the position direction to close
   if(pos_type == POSITION_TYPE_BUY)
   {
      req.type  = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
   }
   else
   {
      req.type  = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   }
   
   bool result = OrderSend(req, res);
   
   if(result && res.retcode == TRADE_RETCODE_DONE)
      return "{\"success\":true}";
   else
      return StringFormat("{\"success\":false,\"error_message\":\"%s\"}", ResultRetcodeDescription(res.retcode));
}

//+------------------------------------------------------------------+
//| CLOSE_ALL handler                                                  |
//+------------------------------------------------------------------+
string HandleCloseAll(string data)
{
   string filter_symbol = JsonGetString(data, "symbol");
   int closed_count = 0;
   
   // Iterate all positions in reverse (closing changes indices)
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      
      string symbol = PositionGetString(POSITION_SYMBOL);
      
      // Apply symbol filter if specified
      if(StringLen(filter_symbol) > 0 && symbol != filter_symbol)
         continue;
      
      long   pos_type = PositionGetInteger(POSITION_TYPE);
      double lots     = PositionGetDouble(POSITION_VOLUME);
      
      MqlTradeRequest req = {};
      MqlTradeResult  res = {};
      
      req.action    = TRADE_ACTION_DEAL;
      req.position  = ticket;
      req.symbol    = symbol;
      req.volume    = lots;
      req.deviation = (ulong)InpMaxSlippage;
      req.type_filling = ORDER_FILLING_IOC;
      
      if(pos_type == POSITION_TYPE_BUY)
      {
         req.type  = ORDER_TYPE_SELL;
         req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
      }
      else
      {
         req.type  = ORDER_TYPE_BUY;
         req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
      }
      
      if(OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE)
         closed_count++;
   }
   
   return StringFormat("{\"closed_count\":%d}", closed_count);
}

//+------------------------------------------------------------------+
//| GET_POSITIONS handler                                              |
//+------------------------------------------------------------------+
string HandleGetPositions(string data)
{
   string filter_symbol = JsonGetString(data, "symbol");
   string positions_json = "[";
   bool first = true;
   
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      
      string symbol = PositionGetString(POSITION_SYMBOL);
      if(StringLen(filter_symbol) > 0 && symbol != filter_symbol)
         continue;
      
      long   pos_type    = PositionGetInteger(POSITION_TYPE);
      double lots        = PositionGetDouble(POSITION_VOLUME);
      double open_price  = PositionGetDouble(POSITION_PRICE_OPEN);
      double curr_price  = PositionGetDouble(POSITION_PRICE_CURRENT);
      double sl_val      = PositionGetDouble(POSITION_SL);
      double tp_val      = PositionGetDouble(POSITION_TP);
      double profit      = PositionGetDouble(POSITION_PROFIT);
      double swap        = PositionGetDouble(POSITION_SWAP);
      double commission  = 0; // Commission not directly available per position in MT5
      datetime open_time = (datetime)PositionGetInteger(POSITION_TIME);
      long   magic       = PositionGetInteger(POSITION_MAGIC);
      string comment_val = PositionGetString(POSITION_COMMENT);
      
      string type_name = (pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      
      if(!first) positions_json += ",";
      first = false;
      
      positions_json += StringFormat(
         "{\"ticket\":%d,\"symbol\":\"%s\",\"type\":\"%s\",\"lots\":%.2f,"
         "\"open_price\":%.5f,\"current_price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,"
         "\"profit\":%.2f,\"swap\":%.2f,\"commission\":%.2f,"
         "\"open_time\":\"%s\",\"magic\":%d,\"comment\":\"%s\"}",
         ticket, symbol, type_name, lots,
         open_price, curr_price, sl_val, tp_val,
         profit, swap, commission,
         TimeToString(open_time, TIME_DATE|TIME_SECONDS), magic, comment_val
      );
   }
   
   positions_json += "]";
   return StringFormat("{\"positions\":%s}", positions_json);
}

//+------------------------------------------------------------------+
//| GET_ACCOUNT handler                                                |
//+------------------------------------------------------------------+
string HandleGetAccount()
{
   double balance      = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity       = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin       = AccountInfoDouble(ACCOUNT_MARGIN);
   double free_margin  = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double margin_level = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   double profit       = AccountInfoDouble(ACCOUNT_PROFIT);
   string currency     = AccountInfoString(ACCOUNT_CURRENCY);
   long   leverage     = AccountInfoInteger(ACCOUNT_LEVERAGE);
   string server       = AccountInfoString(ACCOUNT_SERVER);
   
   return StringFormat(
      "{\"account\":{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,"
      "\"free_margin\":%.2f,\"margin_level\":%.2f,\"profit\":%.2f,"
      "\"currency\":\"%s\",\"leverage\":%d,\"server\":\"%s\"}}",
      balance, equity, margin, free_margin, margin_level, profit,
      currency, leverage, server
   );
}

//+------------------------------------------------------------------+
//| GET_QUOTE handler                                                  |
//+------------------------------------------------------------------+
string HandleGetQuote(string data)
{
   string symbol = JsonGetString(data, "symbol");
   
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return StringFormat("{\"error\":\"Failed to get quote for %s\"}", symbol);
   
   return StringFormat(
      "{\"quote\":{\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,"
      "\"volume\":%d,\"time\":\"%s\"}}",
      tick.bid, tick.ask, tick.last,
      (long)tick.volume,
      TimeToString(tick.time, TIME_DATE|TIME_SECONDS)
   );
}

//+------------------------------------------------------------------+
//| GET_SERVER_TIME handler                                            |
//| Returns broker server time, GMT offset, and day-of-week           |
//+------------------------------------------------------------------+
string HandleGetServerTime()
{
   datetime server_time = TimeCurrent();
   datetime gmt_time    = TimeGMT();
   
   // GMT offset in seconds: server_time - gmt_time
   // Positive = server is ahead of GMT (e.g., UTC+2 = +7200)
   long gmt_offset_sec = (long)server_time - (long)gmt_time;
   
   // Break server time into components
   MqlDateTime dt;
   TimeToStruct(server_time, dt);
   
   // Day of week: 0=Sunday, 1=Monday, ..., 6=Saturday
   int dow = dt.day_of_week;
   
   return StringFormat(
      "{\"server_time\":{\"datetime\":\"%s\","
      "\"timestamp\":%d,"
      "\"gmt_offset_seconds\":%d,"
      "\"year\":%d,\"month\":%d,\"day\":%d,"
      "\"hour\":%d,\"minute\":%d,\"second\":%d,"
      "\"day_of_week\":%d}}",
      TimeToString(server_time, TIME_DATE|TIME_SECONDS),
      (long)server_time,
      gmt_offset_sec,
      dt.year, dt.mon, dt.day,
      dt.hour, dt.min, dt.sec,
      dow
   );
}

//+------------------------------------------------------------------+
//| SUBSCRIBE handler                                                  |
//+------------------------------------------------------------------+
string HandleSubscribe(string data)
{
   string symbol = JsonGetString(data, "symbol");
   string type   = JsonGetString(data, "type");
   
   if(type == "tick")
   {
      // Add to tick subscriptions if not already present
      bool found = false;
      for(int i = 0; i < ArraySize(g_subscribed_ticks); i++)
      {
         if(g_subscribed_ticks[i] == symbol) { found = true; break; }
      }
      if(!found)
      {
         int sz = ArraySize(g_subscribed_ticks);
         ArrayResize(g_subscribed_ticks, sz + 1);
         g_subscribed_ticks[sz] = symbol;
         // Ensure symbol is in Market Watch
         SymbolSelect(symbol, true);
         PrintFormat("Subscribed to ticks: %s", symbol);
      }
   }
   else if(type == "bar")
   {
      string tf = JsonGetString(data, "timeframe");
      string key = symbol + "_" + tf;
      bool found = false;
      for(int i = 0; i < ArraySize(g_subscribed_bars); i++)
      {
         if(g_subscribed_bars[i] == key) { found = true; break; }
      }
      if(!found)
      {
         int sz = ArraySize(g_subscribed_bars);
         ArrayResize(g_subscribed_bars, sz + 1);
         ArrayResize(g_last_bar_time, sz + 1);
         g_subscribed_bars[sz] = key;
         g_last_bar_time[sz] = 0;
         SymbolSelect(symbol, true);
         PrintFormat("Subscribed to bars: %s", key);
      }
   }
   
   return "{\"success\":true}";
}

//+------------------------------------------------------------------+
//| UNSUBSCRIBE handler                                                |
//+------------------------------------------------------------------+
string HandleUnsubscribe(string data)
{
   string symbol = JsonGetString(data, "symbol");
   
   // Remove from tick subscriptions
   for(int i = ArraySize(g_subscribed_ticks) - 1; i >= 0; i--)
   {
      if(g_subscribed_ticks[i] == symbol)
      {
         // Shift array
         for(int j = i; j < ArraySize(g_subscribed_ticks) - 1; j++)
            g_subscribed_ticks[j] = g_subscribed_ticks[j+1];
         ArrayResize(g_subscribed_ticks, ArraySize(g_subscribed_ticks) - 1);
      }
   }
   
   // Remove from bar subscriptions (any timeframe for this symbol)
   for(int i = ArraySize(g_subscribed_bars) - 1; i >= 0; i--)
   {
      if(StringFind(g_subscribed_bars[i], symbol) == 0)
      {
         for(int j = i; j < ArraySize(g_subscribed_bars) - 1; j++)
         {
            g_subscribed_bars[j] = g_subscribed_bars[j+1];
            g_last_bar_time[j] = g_last_bar_time[j+1];
         }
         ArrayResize(g_subscribed_bars, ArraySize(g_subscribed_bars) - 1);
         ArrayResize(g_last_bar_time, ArraySize(g_last_bar_time) - 1);
      }
   }
   
   PrintFormat("Unsubscribed: %s", symbol);
   return "{\"success\":true}";
}

//+------------------------------------------------------------------+
//| Stream tick data on PUB socket                                     |
//+------------------------------------------------------------------+
void StreamTickData()
{
   for(int i = 0; i < ArraySize(g_subscribed_ticks); i++)
   {
      string symbol = g_subscribed_ticks[i];
      MqlTick tick;
      
      if(!SymbolInfoTick(symbol, tick))
         continue;
      
      string topic = "TICK_" + symbol;
      string json = StringFormat(
         "{\"type\":\"TICK\",\"data\":{\"symbol\":\"%s\",\"bid\":%.5f,"
         "\"ask\":%.5f,\"last\":%.5f,\"volume\":%d,"
         "\"time\":\"%s\"}}",
         symbol, tick.bid, tick.ask, tick.last,
         (long)tick.volume,
         TimeToString(tick.time, TIME_DATE|TIME_SECONDS)
      );
      
      // Send as multi-part: topic + payload
      ZmqMsg topic_msg(topic);
      ZmqMsg data_msg(json);
      g_pub.sendMore(topic_msg);
      g_pub.send(data_msg);
   }
}

//+------------------------------------------------------------------+
//| Stream bar data on PUB socket (only on new bars)                  |
//+------------------------------------------------------------------+
void StreamBarData()
{
   for(int i = 0; i < ArraySize(g_subscribed_bars); i++)
   {
      string key = g_subscribed_bars[i];
      
      // Parse "SYMBOL_TF"
      int sep = StringFind(key, "_");
      if(sep < 0) continue;
      
      string symbol = StringSubstr(key, 0, sep);
      string tf_str = StringSubstr(key, sep + 1);
      ENUM_TIMEFRAMES tf = StringToTimeframe(tf_str);
      
      // Check for new bar
      datetime bar_time = iTime(symbol, tf, 0);
      if(bar_time == 0 || bar_time == g_last_bar_time[i])
         continue;
      
      g_last_bar_time[i] = bar_time;
      
      // Get bar data (previous completed bar = index 1)
      double o = iOpen(symbol, tf, 1);
      double h = iHigh(symbol, tf, 1);
      double l = iLow(symbol, tf, 1);
      double c = iClose(symbol, tf, 1);
      long   v = iVolume(symbol, tf, 1);
      datetime t = iTime(symbol, tf, 1);
      
      string topic = "BAR_" + key;
      string json = StringFormat(
         "{\"type\":\"BAR\",\"data\":{\"symbol\":\"%s\",\"timeframe\":\"%s\","
         "\"time\":\"%s\",\"open\":%.5f,\"high\":%.5f,"
         "\"low\":%.5f,\"close\":%.5f,\"volume\":%d}}",
         symbol, tf_str,
         TimeToString(t, TIME_DATE|TIME_SECONDS),
         o, h, l, c, v
      );
      
      ZmqMsg topic_msg(topic);
      ZmqMsg data_msg(json);
      g_pub.sendMore(topic_msg);
      g_pub.send(data_msg);
   }
}

//+------------------------------------------------------------------+
//| Convert timeframe string to ENUM_TIMEFRAMES                       |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(string tf)
{
   if(tf == "M1")  return PERIOD_M1;
   if(tf == "M5")  return PERIOD_M5;
   if(tf == "M15") return PERIOD_M15;
   if(tf == "M30") return PERIOD_M30;
   if(tf == "H1")  return PERIOD_H1;
   if(tf == "H4")  return PERIOD_H4;
   if(tf == "D1")  return PERIOD_D1;
   if(tf == "W1")  return PERIOD_W1;
   if(tf == "MN1") return PERIOD_MN1;
   return PERIOD_M1;  // Default
}

//+------------------------------------------------------------------+
//| Get human-readable retcode description                            |
//+------------------------------------------------------------------+
string ResultRetcodeDescription(uint retcode)
{
   switch(retcode)
   {
      case TRADE_RETCODE_REQUOTE:        return "Requote";
      case TRADE_RETCODE_REJECT:         return "Rejected";
      case TRADE_RETCODE_CANCEL:         return "Cancelled";
      case TRADE_RETCODE_PLACED:         return "Placed";
      case TRADE_RETCODE_DONE:           return "Done";
      case TRADE_RETCODE_DONE_PARTIAL:   return "Partial fill";
      case TRADE_RETCODE_ERROR:          return "Error";
      case TRADE_RETCODE_TIMEOUT:        return "Timeout";
      case TRADE_RETCODE_INVALID:        return "Invalid request";
      case TRADE_RETCODE_INVALID_VOLUME: return "Invalid volume";
      case TRADE_RETCODE_INVALID_PRICE:  return "Invalid price";
      case TRADE_RETCODE_INVALID_STOPS:  return "Invalid stops";
      case TRADE_RETCODE_TRADE_DISABLED: return "Trade disabled";
      case TRADE_RETCODE_MARKET_CLOSED:  return "Market closed";
      case TRADE_RETCODE_NO_MONEY:       return "Insufficient funds";
      case TRADE_RETCODE_PRICE_CHANGED:  return "Price changed";
      case TRADE_RETCODE_PRICE_OFF:      return "Price off";
      case TRADE_RETCODE_INVALID_EXPIRATION: return "Invalid expiration";
      case TRADE_RETCODE_ORDER_CHANGED:  return "Order changed";
      case TRADE_RETCODE_TOO_MANY_REQUESTS: return "Too many requests";
      case TRADE_RETCODE_FROZEN:         return "Frozen";
      default:                           return StringFormat("Unknown (%d)", retcode);
   }
}

//+------------------------------------------------------------------+
//| Minimal JSON parser — extract string value by key                 |
//+------------------------------------------------------------------+
string JsonGetString(string json, string key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   
   // Find the colon after the key
   int colon = StringFind(json, ":", pos + StringLen(search));
   if(colon < 0) return "";
   
   // Skip whitespace
   int start = colon + 1;
   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;
   
   if(start >= StringLen(json)) return "";
   
   // Check if value is a string (starts with ")
   if(StringGetCharacter(json, start) == '"')
   {
      start++;
      int end = StringFind(json, "\"", start);
      if(end < 0) return "";
      return StringSubstr(json, start, end - start);
   }
   
   // Not a string — return raw value up to comma/brace
   int end = start;
   while(end < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == ',' || ch == '}' || ch == ']') break;
      end++;
   }
   return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
//| Minimal JSON parser — extract double value by key                 |
//+------------------------------------------------------------------+
double JsonGetDouble(string json, string key)
{
   string val = JsonGetString(json, key);
   if(StringLen(val) == 0) return 0.0;
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
//| Minimal JSON parser — extract nested JSON object by key           |
//+------------------------------------------------------------------+
string JsonGetObject(string json, string key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "{}";
   
   int colon = StringFind(json, ":", pos + StringLen(search));
   if(colon < 0) return "{}";
   
   // Skip whitespace to find opening brace
   int start = colon + 1;
   while(start < StringLen(json) && StringGetCharacter(json, start) == ' ')
      start++;
   
   if(start >= StringLen(json)) return "{}";
   
   ushort open_char = StringGetCharacter(json, start);
   ushort close_char;
   if(open_char == '{') close_char = '}';
   else if(open_char == '[') close_char = ']';
   else return "{}";
   
   int depth = 1;
   int end = start + 1;
   while(end < StringLen(json) && depth > 0)
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == open_char) depth++;
      else if(ch == close_char) depth--;
      end++;
   }
   
   return StringSubstr(json, start, end - start);
}
//+------------------------------------------------------------------+
