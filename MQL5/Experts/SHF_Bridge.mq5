//+------------------------------------------------------------------+
//| SHF_Bridge.mq5 — SHF v5.6 Native TCP Socket Bridge              |
//| Zero external dependencies — uses MQL5 built-in SocketXXX()     |
//|                                                                    |
//| Architecture:                                                      |
//|   Python = TCP Server (localhost:5555)                             |
//|   MT5 EA = TCP Client (connects to Python)                        |
//|                                                                    |
//| Protocol: Length-prefixed JSON over TCP                            |
//|   [4 bytes big-endian length][JSON payload]                       |
//+------------------------------------------------------------------+
#property copyright "SHF Trading Systems"
#property link      ""
#property version   "5.61"
#property strict

input string InpHost        = "127.0.0.1";  // Python server host
input int    InpPort        = 5555;          // Python server port
input int    InpTimerMs     = 100;           // Timer interval (ms)
input int    InpMagic       = 12345;         // Magic number
input int    InpMaxSlippage = 20;            // Max slippage (points)
input int    InpTimeout     = 5000;          // Connection timeout (ms)

// Auto-detected symbols
string g_symbols[];
int    g_num_symbols = 0;

int    g_socket = INVALID_HANDLE;
bool   g_connected = false;
int    g_reconnect_count = 0;
datetime g_last_connect_attempt = 0;
int    g_timer_count = 0;          // For rate-limiting command checks
bool   g_has_pending_cmd = false;  // True when Python sent bytes

//+------------------------------------------------------------------+
int OnInit()
{
   // Auto-detect symbols for Holy Trio
   DetectSymbols();
   
   EventSetMillisecondTimer(InpTimerMs);
   
   PrintFormat("SHF Bridge v5.61 (Native TCP) | Port=%d | Timer=%dms | Magic=%d",
               InpPort, InpTimerMs, InpMagic);
   PrintFormat("Detected %d symbols: %s", g_num_symbols, SymbolListStr());
   
   TryConnect();
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void DetectSymbols()
{
   // Try multiple name variants for each asset
   string index_a_variants[] = {"US100","NAS100","USTEC","US100.cash","NAS100.cash","USTEC.cash"};
   string index_b_variants[] = {"DE40","DAX40","GER40","DE40.cash","DAX40.cash","GER40.cash"};
   string fx_a1[] = {"AUDUSD","AUDUSDm","AUDUSD.","AUDUSD_"};
   string fx_b1[] = {"NZDUSD","NZDUSDm","NZDUSD.","NZDUSD_"};
   string fx_a2[] = {"EURUSD","EURUSDm","EURUSD.","EURUSD_"};
   string fx_b2[] = {"GBPUSD","GBPUSDm","GBPUSD.","GBPUSD_"};
   
   ArrayResize(g_symbols, 0);
   g_num_symbols = 0;
   
   AddFirstValid(index_a_variants);
   AddFirstValid(index_b_variants);
   AddFirstValid(fx_a1);
   AddFirstValid(fx_b1);
   AddFirstValid(fx_a2);
   AddFirstValid(fx_b2);
}

//+------------------------------------------------------------------+
void AddFirstValid(string &variants[])
{
   for(int i = 0; i < ArraySize(variants); i++)
   {
      if(SymbolSelect(variants[i], true))
      {
         // Verify we can actually get a tick
         MqlTick tick;
         if(SymbolInfoTick(variants[i], tick) && tick.bid > 0)
         {
            int idx = ArraySize(g_symbols);
            ArrayResize(g_symbols, idx + 1);
            g_symbols[idx] = variants[i];
            g_num_symbols++;
            PrintFormat("  Found: %s (bid=%.5f)", variants[i], tick.bid);
            return;
         }
      }
   }
   PrintFormat("  WARNING: No valid symbol found for variant group (first=%s)",
               ArraySize(variants) > 0 ? variants[0] : "?");
}

//+------------------------------------------------------------------+
string SymbolListStr()
{
   string s = "";
   for(int i = 0; i < g_num_symbols; i++)
   {
      if(i > 0) s += ", ";
      s += g_symbols[i];
   }
   return s;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Disconnect();
   PrintFormat("SHF Bridge shutdown (reason=%d)", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_connected)
   {
      if((long)TimeCurrent() - (long)g_last_connect_attempt >= 2)
         TryConnect();
      return;
   }
   
   g_timer_count++;
   
   // Send data push every timer tick
   if(!SendDataPush())
   {
      Disconnect();
      return;
   }
   
   // Check for commands only every 5th tick (500ms) to avoid
   // SocketRead interfering with SocketSend on some MT5 builds
   if(g_timer_count % 5 == 0)
   {
      if(!CheckAndProcessCommand())
      {
         // SocketRead error — don't disconnect, just skip
      }
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   // Don't send on every tick — OnTimer handles data push at 10Hz
   // OnTick only used for urgent command checks when position is open
   if(!g_connected) return;
   if(PositionsTotal() > 0)
   {
      if(!CheckAndProcessCommand()) { /* skip */ }
   }
}

//+------------------------------------------------------------------+
//| Connection Management                                              |
//+------------------------------------------------------------------+
void TryConnect()
{
   g_last_connect_attempt = TimeCurrent();
   
   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE)
   {
      PrintFormat("SocketCreate failed: %d", GetLastError());
      return;
   }
   
   if(!SocketConnect(g_socket, InpHost, InpPort, InpTimeout))
   {
      int err = GetLastError();
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      if(g_reconnect_count % 10 == 0)
         PrintFormat("Waiting for Python server on %s:%d (attempt %d, err=%d)",
                     InpHost, InpPort, g_reconnect_count + 1, err);
      g_reconnect_count++;
      return;
   }
   
   g_connected = true;
   g_reconnect_count = 0;
   g_timer_count = 0;
   PrintFormat("Connected to Python server %s:%d", InpHost, InpPort);
}

//+------------------------------------------------------------------+
void Disconnect()
{
   if(g_socket != INVALID_HANDLE)
   {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
   }
   if(g_connected)
   {
      g_connected = false;
      Print("Disconnected from Python server");
   }
}

//+------------------------------------------------------------------+
//| Send Data Push                                                     |
//+------------------------------------------------------------------+
bool SendDataPush()
{
   string quotes = "";
   for(int i = 0; i < g_num_symbols; i++)
   {
      MqlTick tick;
      if(!SymbolInfoTick(g_symbols[i], tick)) continue;
      
      if(StringLen(quotes) > 0) quotes += ",";
      quotes += StringFormat(
         "\"%s\":{\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,\"vol\":%d,\"time\":%d,\"time_msc\":%I64d}",
         g_symbols[i], tick.bid, tick.ask, tick.last,
         (long)tick.volume, (long)tick.time, tick.time_msc
      );
   }
   
   string positions = "";
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      
      if(StringLen(positions) > 0) positions += ",";
      long pt = PositionGetInteger(POSITION_TYPE);
      string ts = (pt == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      
      positions += StringFormat(
         "{\"ticket\":%d,\"symbol\":\"%s\",\"type\":\"%s\",\"lots\":%.2f,"
         "\"open_price\":%.5f,\"current_price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,"
         "\"profit\":%.2f,\"swap\":%.2f,"
         "\"open_time\":\"%s\",\"magic\":%d,\"comment\":\"%s\"}",
         ticket, PositionGetString(POSITION_SYMBOL), ts,
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN),
         PositionGetDouble(POSITION_PRICE_CURRENT),
         PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT), PositionGetDouble(POSITION_SWAP),
         TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE|TIME_SECONDS),
         PositionGetInteger(POSITION_MAGIC), PositionGetString(POSITION_COMMENT)
      );
   }
   
   datetime st = TimeCurrent();
   datetime gt = TimeGMT();
   long gmt_off = (long)st - (long)gt;
   MqlDateTime dt;
   TimeToStruct(st, dt);
   
   string json = StringFormat(
      "{\"mt\":\"DATA\","
      "\"q\":{%s},"
      "\"a\":{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,"
      "\"free_margin\":%.2f,\"margin_level\":%.2f,\"profit\":%.2f,"
      "\"currency\":\"%s\",\"leverage\":%d,\"server\":\"%s\"},"
      "\"p\":[%s],"
      "\"t\":{\"datetime\":\"%s\",\"timestamp\":%d,\"gmt_offset_seconds\":%d,"
      "\"year\":%d,\"month\":%d,\"day\":%d,"
      "\"hour\":%d,\"minute\":%d,\"second\":%d,\"day_of_week\":%d}}",
      quotes,
      AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN), AccountInfoDouble(ACCOUNT_MARGIN_FREE),
      AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), AccountInfoDouble(ACCOUNT_PROFIT),
      AccountInfoString(ACCOUNT_CURRENCY), AccountInfoInteger(ACCOUNT_LEVERAGE),
      AccountInfoString(ACCOUNT_SERVER),
      positions,
      TimeToString(st, TIME_DATE|TIME_SECONDS), (long)st, gmt_off,
      dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec, dt.day_of_week
   );
   
   return SendMessage(json);
}

//+------------------------------------------------------------------+
//| Check for and process one command from Python                      |
//+------------------------------------------------------------------+
bool CheckAndProcessCommand()
{
   if(g_socket == INVALID_HANDLE) return false;
   
   // Use SocketIsReadable to check if there's data waiting
   // without blocking the send path
   uint avail = SocketIsReadable(g_socket);
   if(avail == 0) return true;  // No data — that's normal, not an error
   
   // Data waiting — read the command
   string msg = RecvMessage(100);  // 100ms timeout for reading command
   if(StringLen(msg) == 0) return true;  // Incomplete read, try again later
   
   string cmd_type = JsonGetString(msg, "type");
   string result = "";
   
   if(cmd_type == "PING")
      result = "{\"status\":\"PONG\"}";
   else if(cmd_type == "ORDER_SEND")
      result = HandleOrderSend(msg);
   else if(cmd_type == "ORDER_CLOSE")
      result = HandleOrderClose(msg);
   else if(cmd_type == "CLOSE_ALL")
      result = HandleCloseAll(msg);
   else if(cmd_type == "ORDER_MODIFY")
      result = HandleOrderModify(msg);
   else if(cmd_type == "GET_QUOTE")
      result = HandleGetQuote(msg);
   else if(cmd_type == "GET_ACCOUNT")
      result = HandleGetAccount();
   else if(cmd_type == "GET_POSITIONS")
      result = HandleGetPositions(msg);
   else if(cmd_type == "GET_SERVER_TIME")
      result = HandleGetServerTime();
   else
      result = StringFormat("{\"error\":\"Unknown: %s\"}", cmd_type);
   
   return SendMessage(result);
}

//+------------------------------------------------------------------+
//| TCP Send: 4-byte length prefix (big-endian) + JSON               |
//+------------------------------------------------------------------+
bool SendMessage(string json)
{
   if(g_socket == INVALID_HANDLE) return false;
   
   uchar json_bytes[];
   int json_len = StringToCharArray(json, json_bytes, 0, -1, CP_UTF8) - 1;
   if(json_len <= 0) return false;
   
   uchar header[4];
   header[0] = (uchar)((json_len >> 24) & 0xFF);
   header[1] = (uchar)((json_len >> 16) & 0xFF);
   header[2] = (uchar)((json_len >> 8) & 0xFF);
   header[3] = (uchar)(json_len & 0xFF);
   
   uchar packet[];
   ArrayResize(packet, 4 + json_len);
   ArrayCopy(packet, header, 0, 0, 4);
   ArrayCopy(packet, json_bytes, 4, 0, json_len);
   
   int sent = SocketSend(g_socket, packet, ArraySize(packet));
   if(sent != ArraySize(packet))
   {
      PrintFormat("SocketSend failed: sent=%d expected=%d err=%d", sent, ArraySize(packet), GetLastError());
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| TCP Recv: Read 4-byte length prefix then JSON payload             |
//+------------------------------------------------------------------+
string RecvMessage(int timeout_ms)
{
   if(g_socket == INVALID_HANDLE) return "";
   
   uchar header[];
   int read = SocketRead(g_socket, header, 4, timeout_ms);
   if(read != 4) return "";
   
   int msg_len = (header[0] << 24) | (header[1] << 16) | (header[2] << 8) | header[3];
   if(msg_len <= 0 || msg_len > 1000000) return "";
   
   uchar payload[];
   int remaining = msg_len;
   
   while(remaining > 0)
   {
      uchar chunk[];
      int chunk_read = SocketRead(g_socket, chunk, remaining, 1000);
      if(chunk_read <= 0) return "";
      
      int old_size = ArraySize(payload);
      ArrayResize(payload, old_size + chunk_read);
      ArrayCopy(payload, chunk, old_size, 0, chunk_read);
      remaining -= chunk_read;
   }
   
   return CharArrayToString(payload, 0, msg_len, CP_UTF8);
}

//+------------------------------------------------------------------+
//| Command Handlers                                                   |
//+------------------------------------------------------------------+
string HandleOrderSend(string data)
{
   string symbol   = JsonGetString(data, "symbol");
   string type_str = JsonGetString(data, "order_type");
   double lots     = JsonGetDouble(data, "lots");
   double price    = JsonGetDouble(data, "price");
   double sl       = JsonGetDouble(data, "sl");
   double tp       = JsonGetDouble(data, "tp");
   int deviation   = (int)JsonGetDouble(data, "deviation");
   int magic       = (int)JsonGetDouble(data, "magic");
   string comment  = JsonGetString(data, "comment");
   
   if(deviation <= 0) deviation = InpMaxSlippage;
   if(magic <= 0)     magic = InpMagic;
   if(StringLen(comment) == 0) comment = "SHF_ALGO";
   
   ENUM_ORDER_TYPE order_type;
   if(type_str == "ORDER_TYPE_BUY")            order_type = ORDER_TYPE_BUY;
   else if(type_str == "ORDER_TYPE_SELL")       order_type = ORDER_TYPE_SELL;
   else if(type_str == "ORDER_TYPE_BUY_LIMIT")  order_type = ORDER_TYPE_BUY_LIMIT;
   else if(type_str == "ORDER_TYPE_SELL_LIMIT") order_type = ORDER_TYPE_SELL_LIMIT;
   else if(type_str == "ORDER_TYPE_BUY_STOP")   order_type = ORDER_TYPE_BUY_STOP;
   else if(type_str == "ORDER_TYPE_SELL_STOP")  order_type = ORDER_TYPE_SELL_STOP;
   else
      return StringFormat("{\"success\":false,\"error_code\":-1,\"error_message\":\"Unknown order type: %s\"}", type_str);
   
   if(price <= 0.0)
   {
      if(order_type == ORDER_TYPE_BUY)
         price = SymbolInfoDouble(symbol, SYMBOL_ASK);
      else if(order_type == ORDER_TYPE_SELL)
         price = SymbolInfoDouble(symbol, SYMBOL_BID);
   }
   
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
   // Auto-detect filling mode: try IOC first, fall back to FOK then RETURN
   req.type_filling = GetSymbolFillingMode(symbol);
   
   bool result = OrderSend(req, res);
   
   // If fill type rejected, retry with alternatives
   if(!result || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL))
   {
      if(req.type_filling == ORDER_FILLING_IOC)
      {
         req.type_filling = ORDER_FILLING_FOK;
         result = OrderSend(req, res);
      }
      if(!result || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL))
      {
         if(req.type_filling != ORDER_FILLING_RETURN)
         {
            req.type_filling = ORDER_FILLING_RETURN;
            result = OrderSend(req, res);
         }
      }
   }
   
   if(result && res.retcode == TRADE_RETCODE_DONE)
   {
      // Use res.order (position ticket), NOT res.deal (deal ticket)
      // PositionSelectByTicket() needs the position ticket to close later
      ulong pos_ticket = res.order;
      PrintFormat("ORDER OK: %s %s %.2f @ %.5f ticket=%d (deal=%d)",
                  symbol, type_str, lots, res.price, pos_ticket, res.deal);
      return StringFormat(
         "{\"success\":true,\"ticket\":%d,\"order_type\":\"%s\",\"lots\":%.2f,"
         "\"price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"error_code\":0,\"error_message\":\"\"}",
         pos_ticket, type_str, lots, res.price, sl, tp);
   }
   else
   {
      PrintFormat("ORDER FAIL: %s %s %.2f retcode=%d", symbol, type_str, lots, res.retcode);
      return StringFormat(
         "{\"success\":false,\"ticket\":0,\"order_type\":\"%s\",\"lots\":%.2f,"
         "\"price\":0,\"sl\":0,\"tp\":0,\"error_code\":%d,\"error_message\":\"%s\"}",
         type_str, lots, (int)res.retcode, RetcodeDesc(res.retcode));
   }
}

//+------------------------------------------------------------------+
string HandleOrderClose(string data)
{
   ulong ticket = (ulong)JsonGetDouble(data, "ticket");
   double lots  = JsonGetDouble(data, "lots");
   
   if(!PositionSelectByTicket(ticket))
      return StringFormat("{\"success\":false,\"error_message\":\"Position %d not found\"}", ticket);
   
   string symbol  = PositionGetString(POSITION_SYMBOL);
   long pos_type  = PositionGetInteger(POSITION_TYPE);
   double pos_lots = PositionGetDouble(POSITION_VOLUME);
   if(lots <= 0 || lots >= pos_lots) lots = pos_lots;
   
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action   = TRADE_ACTION_DEAL;
   req.position = ticket;
   req.symbol   = symbol;
   req.volume   = lots;
   req.deviation = (ulong)InpMaxSlippage;
   req.type_filling = ORDER_FILLING_IOC;
   
   if(pos_type == POSITION_TYPE_BUY)
   { req.type = ORDER_TYPE_SELL; req.price = SymbolInfoDouble(symbol, SYMBOL_BID); }
   else
   { req.type = ORDER_TYPE_BUY; req.price = SymbolInfoDouble(symbol, SYMBOL_ASK); }
   
   if(OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE)
      return "{\"success\":true}";
   return StringFormat("{\"success\":false,\"error_message\":\"%s\"}", RetcodeDesc(res.retcode));
}

//+------------------------------------------------------------------+
string HandleCloseAll(string data)
{
   string filter = JsonGetString(data, "symbol");
   int closed = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(StringLen(filter) > 0 && sym != filter) continue;
      
      long pt = PositionGetInteger(POSITION_TYPE);
      MqlTradeRequest req = {};
      MqlTradeResult  res = {};
      req.action = TRADE_ACTION_DEAL;
      req.position = ticket;
      req.symbol = sym;
      req.volume = PositionGetDouble(POSITION_VOLUME);
      req.deviation = (ulong)InpMaxSlippage;
      req.type_filling = ORDER_FILLING_IOC;
      if(pt == POSITION_TYPE_BUY)
      { req.type = ORDER_TYPE_SELL; req.price = SymbolInfoDouble(sym, SYMBOL_BID); }
      else
      { req.type = ORDER_TYPE_BUY; req.price = SymbolInfoDouble(sym, SYMBOL_ASK); }
      if(OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE) closed++;
   }
   return StringFormat("{\"closed_count\":%d}", closed);
}

//+------------------------------------------------------------------+
string HandleOrderModify(string data)
{
   ulong ticket = (ulong)JsonGetDouble(data, "ticket");
   double sl = JsonGetDouble(data, "sl");
   double tp = JsonGetDouble(data, "tp");
   if(!PositionSelectByTicket(ticket))
      return StringFormat("{\"success\":false,\"error_message\":\"Position %d not found\"}", ticket);
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol = PositionGetString(POSITION_SYMBOL);
   req.sl = (sl > 0) ? sl : PositionGetDouble(POSITION_SL);
   req.tp = (tp > 0) ? tp : PositionGetDouble(POSITION_TP);
   if(OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE)
      return "{\"success\":true}";
   return StringFormat("{\"success\":false,\"error_message\":\"%s\"}", RetcodeDesc(res.retcode));
}

//+------------------------------------------------------------------+
string HandleGetQuote(string data)
{
   string symbol = JsonGetString(data, "symbol");
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return StringFormat("{\"error\":\"No quote for %s\"}", symbol);
   return StringFormat(
      "{\"quote\":{\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,"
      "\"volume\":%d,\"time\":\"%s\",\"time_msc\":%I64d}}",
      tick.bid, tick.ask, tick.last, (long)tick.volume,
      TimeToString(tick.time, TIME_DATE|TIME_SECONDS), tick.time_msc);
}

//+------------------------------------------------------------------+
string HandleGetAccount()
{
   return StringFormat(
      "{\"account\":{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,"
      "\"free_margin\":%.2f,\"margin_level\":%.2f,\"profit\":%.2f,"
      "\"currency\":\"%s\",\"leverage\":%d,\"server\":\"%s\"}}",
      AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN), AccountInfoDouble(ACCOUNT_MARGIN_FREE),
      AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), AccountInfoDouble(ACCOUNT_PROFIT),
      AccountInfoString(ACCOUNT_CURRENCY), AccountInfoInteger(ACCOUNT_LEVERAGE),
      AccountInfoString(ACCOUNT_SERVER));
}

//+------------------------------------------------------------------+
string HandleGetPositions(string data)
{
   string filter = JsonGetString(data, "symbol");
   string json = "[";
   bool first = true;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(StringLen(filter) > 0 && sym != filter) continue;
      if(!first) json += ",";
      first = false;
      long pt = PositionGetInteger(POSITION_TYPE);
      json += StringFormat(
         "{\"ticket\":%d,\"symbol\":\"%s\",\"type\":\"%s\",\"lots\":%.2f,"
         "\"open_price\":%.5f,\"current_price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,"
         "\"profit\":%.2f,\"swap\":%.2f,\"commission\":0,"
         "\"open_time\":\"%s\",\"magic\":%d,\"comment\":\"%s\"}",
         ticket, sym, (pt==POSITION_TYPE_BUY)?"BUY":"SELL",
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN), PositionGetDouble(POSITION_PRICE_CURRENT),
         PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT), PositionGetDouble(POSITION_SWAP),
         TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE|TIME_SECONDS),
         PositionGetInteger(POSITION_MAGIC), PositionGetString(POSITION_COMMENT));
   }
   return StringFormat("{\"positions\":%s]}", json);
}

//+------------------------------------------------------------------+
string HandleGetServerTime()
{
   datetime st = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(st, dt);
   long goff = (long)st - (long)TimeGMT();
   return StringFormat(
      "{\"server_time\":{\"datetime\":\"%s\",\"timestamp\":%d,"
      "\"gmt_offset_seconds\":%d,\"year\":%d,\"month\":%d,\"day\":%d,"
      "\"hour\":%d,\"minute\":%d,\"second\":%d,\"day_of_week\":%d}}",
      TimeToString(st, TIME_DATE|TIME_SECONDS), (long)st, goff,
      dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec, dt.day_of_week);
}

//+------------------------------------------------------------------+
string RetcodeDesc(uint rc)
{
   switch(rc)
   {
      case TRADE_RETCODE_REQUOTE:        return "Requote";
      case TRADE_RETCODE_REJECT:         return "Rejected";
      case TRADE_RETCODE_CANCEL:         return "Cancelled";
      case TRADE_RETCODE_DONE:           return "Done";
      case TRADE_RETCODE_DONE_PARTIAL:   return "Partial";
      case TRADE_RETCODE_ERROR:          return "Error";
      case TRADE_RETCODE_TIMEOUT:        return "Timeout";
      case TRADE_RETCODE_INVALID:        return "Invalid";
      case TRADE_RETCODE_INVALID_VOLUME: return "Invalid volume";
      case TRADE_RETCODE_INVALID_PRICE:  return "Invalid price";
      case TRADE_RETCODE_INVALID_STOPS:  return "Invalid stops";
      case TRADE_RETCODE_TRADE_DISABLED: return "Trade disabled";
      case TRADE_RETCODE_MARKET_CLOSED:  return "Market closed";
      case TRADE_RETCODE_NO_MONEY:       return "No money";
      case TRADE_RETCODE_PRICE_CHANGED:  return "Price changed";
      case TRADE_RETCODE_TOO_MANY_REQUESTS: return "Too many requests";
      default: return StringFormat("Unknown(%d)", rc);
   }
}

//+------------------------------------------------------------------+
//| Minimal JSON parser                                                |
//+------------------------------------------------------------------+
string JsonGetString(string json, string key)
{
   string search = "\"" + key + "\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   int colon = StringFind(json, ":", pos + StringLen(search));
   if(colon < 0) return "";
   int start = colon + 1;
   while(start < StringLen(json) && StringGetCharacter(json, start) == ' ') start++;
   if(start >= StringLen(json)) return "";
   if(StringGetCharacter(json, start) == '"')
   {
      start++;
      int end = StringFind(json, "\"", start);
      if(end < 0) return "";
      return StringSubstr(json, start, end - start);
   }
   int end = start;
   while(end < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == ',' || ch == '}' || ch == ']') break;
      end++;
   }
   return StringSubstr(json, start, end - start);
}

double JsonGetDouble(string json, string key)
{
   string val = JsonGetString(json, key);
   if(StringLen(val) == 0) return 0.0;
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
//| Auto-detect symbol filling mode                                    |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetSymbolFillingMode(string symbol)
{
   long filling_mode = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   
   if((filling_mode & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   if((filling_mode & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
}
//+------------------------------------------------------------------+
