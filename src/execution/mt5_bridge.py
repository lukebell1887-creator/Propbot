"""
MetaTrader 5 Native TCP Socket Bridge
======================================

Provides bidirectional communication between Python trading logic and MT5.

Architecture:
    Python (TCP Server on :5555)  <--TCP-->  MT5 EA (TCP Client)

Protocol:
    Length-prefixed JSON over TCP:
    [4 bytes big-endian length][JSON payload]

    EA pushes DATA messages (quotes, account, positions, server_time)
    Python sends COMMAND messages (orders, queries)
    EA responds with RESULT messages

Zero external dependencies — uses only Python stdlib `socket` module.
"""

import socket
import struct
import json
import logging
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Callable, Any
from dataclasses import dataclass, asdict
from enum import Enum
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class BridgeTimeoutError(Exception):
    """Raised when MT5 communication times out."""
    pass


class OrderType(Enum):
    """MT5 Order types."""
    MARKET_BUY = "ORDER_TYPE_BUY"
    MARKET_SELL = "ORDER_TYPE_SELL"
    LIMIT_BUY = "ORDER_TYPE_BUY_LIMIT"
    LIMIT_SELL = "ORDER_TYPE_SELL_LIMIT"
    STOP_BUY = "ORDER_TYPE_BUY_STOP"
    STOP_SELL = "ORDER_TYPE_SELL_STOP"


class MessageType(Enum):
    """Message types."""
    PING = "PING"
    ORDER_SEND = "ORDER_SEND"
    ORDER_MODIFY = "ORDER_MODIFY"
    ORDER_CLOSE = "ORDER_CLOSE"
    CLOSE_ALL = "CLOSE_ALL"
    GET_POSITIONS = "GET_POSITIONS"
    GET_ACCOUNT = "GET_ACCOUNT"
    GET_QUOTE = "GET_QUOTE"
    GET_SERVER_TIME = "GET_SERVER_TIME"
    GET_HISTORY = "GET_HISTORY"
    SUBSCRIBE = "SUBSCRIBE"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    RESPONSE = "RESPONSE"
    ERROR = "ERROR"
    TICK = "TICK"
    BAR = "BAR"


@dataclass
class TickData:
    """Real-time tick data."""
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    time: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class BarData:
    """OHLCV bar data."""
    symbol: str
    timeframe: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass
class Position:
    """Open position information."""
    ticket: int
    symbol: str
    type: str  # "BUY" or "SELL"
    lots: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    swap: float
    commission: float
    open_time: datetime
    magic: int
    comment: str


@dataclass
class AccountInfo:
    """Account information."""
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    profit: float
    currency: str
    leverage: int
    server: str


@dataclass
class ServerTimeInfo:
    """Broker server time information for time-sync."""
    datetime_str: str
    timestamp: int
    gmt_offset_seconds: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    day_of_week: int


@dataclass
class OrderRequest:
    """Order request structure."""
    symbol: str
    order_type: OrderType
    lots: float
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 20
    magic: int = 12345
    comment: str = "SHF_ALGO"


@dataclass
class OrderResult:
    """Order execution result."""
    success: bool
    ticket: int
    order_type: str
    lots: float
    price: float
    sl: float
    tp: float
    error_code: int
    error_message: str


class MT5Bridge:
    """
    Native TCP Socket bridge for MT5 communication.

    Runs a TCP server. The MT5 EA connects as a client.
    EA continuously pushes market data; Python sends commands when needed.
    """

    def __init__(
        self,
        req_port: int = 5555,
        sub_port: int = 5556,   # Kept for API compat, not used
        host: str = "0.0.0.0",
        recv_timeout_ms: int = 5000,
        send_timeout_ms: int = 1000
    ):
        self.port = req_port
        self.host = host
        self.recv_timeout_ms = recv_timeout_ms
        self.send_timeout_ms = send_timeout_ms

        # TCP server
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
        self._client_lock = threading.Lock()

        # Cached data from EA pushes
        self._quotes: Dict[str, dict] = {}
        self._account: dict = {}
        self._positions: list = []
        self._server_time: dict = {}
        self._data_lock = threading.RLock()
        self._last_data_time: float = 0.0

        # Command response queue
        self._response_queue: Queue = Queue()

        # Background threads
        self._accept_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Connection state
        self._connected = False
        self._last_heartbeat: Optional[datetime] = None

        # Tick/bar handlers (for compatibility)
        self._tick_handlers: Dict[str, List[Callable]] = {}
        self._bar_handlers: Dict[str, List[Callable]] = {}
        self._subscribed_symbols: set = set()

    def connect(self) -> bool:
        """
        Start TCP server and wait for EA connection.

        Returns:
            True if EA connected successfully
        """
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.settimeout(1.0)
            self._server_socket.bind((self.host, self.port))
            self._server_socket.listen(1)

            logger.info(f"MT5 Bridge TCP server listening on {self.host}:{self.port}")
            logger.info("Waiting for MT5 EA to connect...")

            # Wait for EA connection (up to 60 seconds)
            self._server_socket.settimeout(60.0)
            try:
                self._client_socket, addr = self._server_socket.accept()
                self._client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._client_socket.settimeout(self.recv_timeout_ms / 1000.0)
                logger.info(f"MT5 EA connected from {addr}")
            except socket.timeout:
                logger.error("No EA connection within 60s")
                return False

            # Start receiver thread
            self._stop_event.clear()
            self._recv_thread = threading.Thread(target=self._receiver_loop, daemon=True)
            self._recv_thread.start()

            # Wait for first DATA push to confirm it works
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self._last_data_time > 0:
                    break
                time.sleep(0.05)

            if self._last_data_time > 0:
                self._connected = True
                self._last_heartbeat = datetime.utcnow()
                logger.info("MT5 Bridge connected — receiving live data")
                return True
            else:
                logger.error("Connected but no DATA received within 5s")
                return False

        except Exception as e:
            logger.error(f"MT5 Bridge connection error: {e}")
            return False

    def disconnect(self) -> None:
        """Disconnect and cleanup."""
        self._stop_event.set()
        self._connected = False

        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)

        if self._client_socket:
            try:
                self._client_socket.close()
            except:
                pass
            self._client_socket = None

        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass
            self._server_socket = None

        logger.info("MT5 Bridge disconnected")

    # === TCP Protocol ===

    def _send_msg(self, data: dict) -> bool:
        """Send length-prefixed JSON message."""
        with self._client_lock:
            if not self._client_socket:
                return False
            try:
                payload = json.dumps(data).encode('utf-8')
                header = struct.pack('>I', len(payload))
                self._client_socket.sendall(header + payload)
                return True
            except Exception as e:
                logger.error(f"Send error: {e}")
                return False

    def _recv_msg(self, sock: socket.socket, timeout: float = 5.0) -> Optional[dict]:
        """Receive length-prefixed JSON message."""
        try:
            old_timeout = sock.gettimeout()
            sock.settimeout(timeout)

            # Read 4-byte header
            header = self._recv_exact(sock, 4)
            if not header:
                sock.settimeout(old_timeout)
                return None

            msg_len = struct.unpack('>I', header)[0]
            if msg_len <= 0 or msg_len > 1_000_000:
                sock.settimeout(old_timeout)
                return None

            # Read payload
            payload = self._recv_exact(sock, msg_len)
            sock.settimeout(old_timeout)

            if not payload:
                return None

            return json.loads(payload.decode('utf-8'))

        except socket.timeout:
            return None
        except Exception as e:
            logger.debug(f"Recv error: {e}")
            return None

    def _recv_exact(self, sock: socket.socket, n: int) -> Optional[bytes]:
        """Read exactly n bytes from socket."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    return None  # Connection closed
                data += chunk
            except socket.timeout:
                return None
            except Exception:
                return None
        return data

    # === Receiver Thread ===

    def _receiver_loop(self) -> None:
        """Background thread: reads all messages from EA."""
        while not self._stop_event.is_set():
            if not self._client_socket:
                time.sleep(0.1)
                continue

            msg = self._recv_msg(self._client_socket, timeout=0.5)
            if msg is None:
                continue

            mt = msg.get('mt', '')

            if mt == 'DATA':
                # Update cached state from EA data push
                with self._data_lock:
                    if 'q' in msg:
                        self._quotes = msg['q']
                    if 'a' in msg:
                        self._account = msg['a']
                    if 'p' in msg:
                        self._positions = msg['p']
                    if 't' in msg:
                        self._server_time = msg['t']
                    self._last_data_time = time.time()
                    self._last_heartbeat = datetime.utcnow()

                # Dispatch tick handlers
                for symbol, qdata in msg.get('q', {}).items():
                    if symbol in self._tick_handlers:
                        try:
                            tick = TickData(
                                symbol=symbol,
                                bid=qdata.get('bid', 0),
                                ask=qdata.get('ask', 0),
                                last=qdata.get('last', 0),
                                volume=qdata.get('vol', 0),
                                time=datetime.utcnow()
                            )
                            for handler in self._tick_handlers[symbol]:
                                handler(tick)
                        except Exception as e:
                            logger.error(f"Tick handler error: {e}")
            else:
                # This is a command response — put in queue
                self._response_queue.put(msg)

    # === Command Interface ===

    def _send_command(self, msg_type: MessageType, data: dict) -> dict:
        """Send command and wait for response."""
        if not self._connected and msg_type != MessageType.PING:
            raise ConnectionError("Not connected to MT5")

        # Drain any stale responses
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except Empty:
                break

        # Build and send command
        cmd = {'type': msg_type.value}
        cmd.update(data)

        if not self._send_msg(cmd):
            raise ConnectionError("Failed to send command to MT5")

        # Wait for response
        try:
            response = self._response_queue.get(timeout=self.recv_timeout_ms / 1000.0)
            return response
        except Empty:
            raise BridgeTimeoutError(
                f"MT5 timeout after {self.recv_timeout_ms}ms "
                f"(command={msg_type.value}). EA may not be responding."
            )

    def _ping(self) -> bool:
        """Check EA is alive via cached data freshness."""
        with self._data_lock:
            # If we received data in the last 2 seconds, EA is alive
            if self._last_data_time > 0 and (time.time() - self._last_data_time) < 2.0:
                return True
        return False

    # === Public API (same interface as before) ===

    def subscribe_ticks(self, symbol: str, handler: Callable[[TickData], None]) -> None:
        """Subscribe to tick data for a symbol."""
        if symbol not in self._tick_handlers:
            self._tick_handlers[symbol] = []
        self._tick_handlers[symbol].append(handler)
        self._subscribed_symbols.add(symbol)
        logger.info(f"Subscribed to ticks: {symbol}")

    def subscribe_bars(self, symbol: str, timeframe: str, handler: Callable) -> None:
        """Subscribe to bar data."""
        key = f"{symbol}_{timeframe}"
        if key not in self._bar_handlers:
            self._bar_handlers[key] = []
        self._bar_handlers[key].append(handler)
        logger.info(f"Subscribed to bars: {key}")

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from symbol data."""
        self._tick_handlers.pop(symbol, None)
        self._subscribed_symbols.discard(symbol)
        keys_to_remove = [k for k in self._bar_handlers if k.startswith(symbol)]
        for key in keys_to_remove:
            del self._bar_handlers[key]
        logger.info(f"Unsubscribed: {symbol}")

    def send_order(self, request: OrderRequest) -> OrderResult:
        """Send order to MT5."""
        data = {
            'symbol': request.symbol,
            'order_type': request.order_type.value,
            'lots': request.lots,
            'price': request.price,
            'sl': request.sl,
            'tp': request.tp,
            'deviation': request.deviation,
            'magic': request.magic,
            'comment': request.comment
        }

        logger.info(f"Sending order: {request.symbol} {request.order_type.value} {request.lots} lots")

        try:
            response = self._send_command(MessageType.ORDER_SEND, data)

            result = OrderResult(
                success=response.get('success', False),
                ticket=response.get('ticket', 0),
                order_type=response.get('order_type', ''),
                lots=response.get('lots', 0),
                price=response.get('price', 0),
                sl=response.get('sl', 0),
                tp=response.get('tp', 0),
                error_code=response.get('error_code', 0),
                error_message=response.get('error_message', '')
            )

            if result.success:
                logger.info(f"Order executed: ticket={result.ticket} price={result.price}")
            else:
                logger.error(f"Order failed: {result.error_message}")

            return result

        except BridgeTimeoutError:
            raise
        except Exception as e:
            logger.error(f"Order error: {e}")
            return OrderResult(
                success=False, ticket=0, order_type='', lots=0,
                price=0, sl=0, tp=0, error_code=-1, error_message=str(e)
            )

    def execute_spread(
        self,
        request_a: OrderRequest,
        request_b: OrderRequest
    ) -> Tuple[OrderResult, OrderResult]:
        """Execute both legs of a spread trade."""
        logger.info(
            f"Spread execution: {request_a.symbol} {request_a.order_type.value} + "
            f"{request_b.symbol} {request_b.order_type.value}"
        )

        # For TCP single-connection, execute sequentially (still fast — ~5ms per leg)
        result_a = self.send_order(request_a)
        result_b = self.send_order(request_b)

        if result_a.success != result_b.success:
            failed = "A" if not result_a.success else "B"
            succeeded = "B" if not result_a.success else "A"
            logger.error(
                f"SPREAD LEG IMBALANCE: Leg {failed} failed, Leg {succeeded} succeeded. "
                f"Auto-closing orphaned leg..."
            )
            # Auto-close the orphaned leg to prevent widowmaker
            orphan_ticket = result_a.ticket if result_a.success else result_b.ticket
            if orphan_ticket:
                try:
                    closed = self.close_position(orphan_ticket)
                    if closed:
                        logger.info(f"ORPHAN CLOSED: ticket={orphan_ticket} — widowmaker prevented")
                    else:
                        logger.error(f"ORPHAN CLOSE FAILED: ticket={orphan_ticket} — MANUAL CLOSE REQUIRED")
                except Exception as e:
                    logger.error(f"ORPHAN CLOSE ERROR: ticket={orphan_ticket} — {e}")

        return result_a, result_b

    def modify_position(self, ticket: int, sl: Optional[float] = None,
                        tp: Optional[float] = None) -> bool:
        """Modify SL/TP of a position."""
        data = {'ticket': ticket}
        if sl is not None:
            data['sl'] = sl
        if tp is not None:
            data['tp'] = tp

        response = self._send_command(MessageType.ORDER_MODIFY, data)
        success = response.get('success', False)

        if success:
            logger.info(f"Position {ticket} modified: SL={sl} TP={tp}")
        else:
            logger.error(f"Position modify failed: {response.get('error_message')}")
        return success

    def close_position(self, ticket: int, lots: Optional[float] = None) -> bool:
        """Close a position."""
        data = {'ticket': ticket}
        if lots is not None:
            data['lots'] = lots

        response = self._send_command(MessageType.ORDER_CLOSE, data)
        success = response.get('success', False)

        if success:
            logger.info(f"Position {ticket} closed")
        else:
            logger.error(f"Position close failed: {response.get('error_message')}")
        return success

    def close_all_positions(self, symbol: Optional[str] = None) -> int:
        """Close all positions."""
        data = {}
        if symbol:
            data['symbol'] = symbol

        response = self._send_command(MessageType.CLOSE_ALL, data)
        closed = response.get('closed_count', 0)
        logger.warning(f"Close all positions: {closed} closed")
        return closed

    def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get open positions from cached data."""
        with self._data_lock:
            positions_data = self._positions

        positions = []
        for p in positions_data:
            if symbol and p.get('symbol') != symbol:
                continue
            try:
                positions.append(Position(
                    ticket=p['ticket'],
                    symbol=p['symbol'],
                    type=p['type'],
                    lots=p['lots'],
                    open_price=p['open_price'],
                    current_price=p['current_price'],
                    sl=p['sl'],
                    tp=p['tp'],
                    profit=p['profit'],
                    swap=p.get('swap', 0),
                    commission=p.get('commission', 0),
                    open_time=datetime.strptime(p['open_time'], "%Y.%m.%d %H:%M:%S")
                             if isinstance(p.get('open_time'), str) else datetime.utcnow(),
                    magic=p.get('magic', 0),
                    comment=p.get('comment', '')
                ))
            except Exception as e:
                logger.error(f"Position parse error: {e}")

        return positions

    def get_account_info(self) -> AccountInfo:
        """Get account info from cached data."""
        with self._data_lock:
            data = self._account

        return AccountInfo(
            balance=data.get('balance', 0),
            equity=data.get('equity', 0),
            margin=data.get('margin', 0),
            free_margin=data.get('free_margin', 0),
            margin_level=data.get('margin_level', 0),
            profit=data.get('profit', 0),
            currency=data.get('currency', 'USD'),
            leverage=data.get('leverage', 100),
            server=data.get('server', '')
        )

    def get_server_time(self) -> Optional[ServerTimeInfo]:
        """Get server time from cached data."""
        with self._data_lock:
            data = self._server_time

        if not data:
            return None

        return ServerTimeInfo(
            datetime_str=data.get('datetime', ''),
            timestamp=int(data.get('timestamp', 0)),
            gmt_offset_seconds=int(data.get('gmt_offset_seconds', 0)),
            year=int(data.get('year', 0)),
            month=int(data.get('month', 0)),
            day=int(data.get('day', 0)),
            hour=int(data.get('hour', 0)),
            minute=int(data.get('minute', 0)),
            second=int(data.get('second', 0)),
            day_of_week=int(data.get('day_of_week', 0)),
        )

    def get_history(self, symbol: str, count: int = 768, timeout_ms: int = 15000) -> List[dict]:
        """
        Fetch historical M1 bars from broker via EA's CopyRates().

        Used at startup to pre-warm the CointegrationEngine so the bot
        is immediately ready to trade (no 3+ hour wait for live bars).

        Args:
            symbol: Broker symbol name (e.g. "AUDUSD", "NAS100")
            count: Number of M1 bars to request (default 768 = fills Hurst window)
            timeout_ms: Timeout for this command (longer than normal — history can be slow)

        Returns:
            List of bar dicts: [{"t": epoch, "o": open, "h": high, "l": low, "c": close, "v": vol}, ...]
            Ordered oldest-first (chronological). Empty list on failure.
        """
        data = {'symbol': symbol, 'count': count}

        # Use longer timeout for history requests (CopyRates can be slow)
        old_timeout = self.recv_timeout_ms
        self.recv_timeout_ms = timeout_ms
        try:
            response = self._send_command(MessageType.GET_HISTORY, data)
        except BridgeTimeoutError:
            logger.error(f"GET_HISTORY timeout for {symbol} ({timeout_ms}ms)")
            self.recv_timeout_ms = old_timeout
            return []
        finally:
            self.recv_timeout_ms = old_timeout

        bars = response.get('bars', [])
        error = response.get('error', '')
        if error:
            logger.error(f"GET_HISTORY error for {symbol}: {error}")
            return []

        logger.info(f"GET_HISTORY: {symbol} — received {len(bars)} M1 bars")
        return bars

    def get_quote(self, symbol: str) -> Optional[TickData]:
        """Get current quote from cached data (updated every 100ms by EA)."""
        with self._data_lock:
            qdata = self._quotes.get(symbol)

        if not qdata:
            logger.warning(f"No cached quote for {symbol}")
            return None

        bid = qdata.get('bid', 0)
        ask = qdata.get('ask', 0)

        if bid <= 0 or ask <= 0:
            logger.warning(f"Invalid quote for {symbol}: bid={bid}, ask={ask}")
            return None

        return TickData(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=qdata.get('last', bid),
            volume=qdata.get('vol', 0),
            time=datetime.utcnow()
        )

    def get_available_symbols(self) -> list:
        """Return list of symbols the EA is currently streaming quotes for."""
        with self._data_lock:
            return list(self._quotes.keys())

    def resolve_symbol(self, wanted: str, aliases: list = None) -> Optional[str]:
        """
        Find the actual broker symbol name for a wanted symbol.
        Checks cached quotes for exact match first, then tries aliases.
        Returns the actual broker name or None if not found.
        """
        with self._data_lock:
            available = set(self._quotes.keys())

        # Exact match
        if wanted in available:
            return wanted

        # Try aliases
        if aliases:
            for alias in aliases:
                if alias in available:
                    return alias

        return None

    @property
    def is_connected(self) -> bool:
        """Check if bridge is connected and receiving data."""
        if not self._connected:
            return False
        with self._data_lock:
            # Connected if data received in last 5 seconds
            return (time.time() - self._last_data_time) < 5.0

    def heartbeat(self) -> bool:
        """Check connection health via data freshness."""
        alive = self._ping()
        if alive:
            self._last_heartbeat = datetime.utcnow()
        return alive


class MT5BridgeAsync:
    """Async wrapper for MT5Bridge."""

    def __init__(self, bridge: MT5Bridge):
        self._bridge = bridge

    async def connect(self) -> bool:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.connect)

    async def disconnect(self) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._bridge.disconnect)

    async def send_order(self, request: OrderRequest) -> OrderResult:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.send_order, request)

    async def close_position(self, ticket: int, lots: Optional[float] = None) -> bool:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.close_position, ticket, lots)

    async def close_all_positions(self, symbol: Optional[str] = None) -> int:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.close_all_positions, symbol)

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_positions, symbol)

    async def get_account_info(self) -> AccountInfo:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_account_info)

    async def get_quote(self, symbol: str) -> TickData:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_quote, symbol)
