"""
MetaTrader 5 ZeroMQ Bridge
==========================

Provides bidirectional communication between Python trading logic and MT5.

Architecture:
    Python (Strategy/Risk) <--ZeroMQ--> MT5 Expert Advisor

Message Protocol:
    - REQ/REP for commands (orders, account info)
    - PUB/SUB for market data streaming
    - JSON message format

This bridge enables:
    1. Receiving real-time tick/bar data from MT5
    2. Sending trade orders (market, limit, stop)
    3. Managing positions (modify SL/TP, close)
    4. Querying account state
"""

import zmq
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Callable, Any
from dataclasses import dataclass, asdict
from enum import Enum
from threading import Thread, Event
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
import asyncio

logger = logging.getLogger(__name__)


class BridgeTimeoutError(Exception):
    """Raised when MT5 ZMQ communication times out (MT5 likely frozen)."""
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
    """ZeroMQ message types."""
    # Commands
    PING = "PING"
    ORDER_SEND = "ORDER_SEND"
    ORDER_MODIFY = "ORDER_MODIFY"
    ORDER_CLOSE = "ORDER_CLOSE"
    CLOSE_ALL = "CLOSE_ALL"
    GET_POSITIONS = "GET_POSITIONS"
    GET_ACCOUNT = "GET_ACCOUNT"
    GET_QUOTE = "GET_QUOTE"
    GET_SERVER_TIME = "GET_SERVER_TIME"
    SUBSCRIBE = "SUBSCRIBE"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    
    # Responses
    RESPONSE = "RESPONSE"
    ERROR = "ERROR"
    
    # Data
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
    datetime_str: str       # "YYYY.MM.DD HH:MM:SS" (broker local)
    timestamp: int          # Unix epoch seconds (broker local)
    gmt_offset_seconds: int # Broker offset from GMT (e.g. +7200 = UTC+2)
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    day_of_week: int        # 0=Sunday .. 6=Saturday


@dataclass 
class OrderRequest:
    """Order request structure."""
    symbol: str
    order_type: OrderType
    lots: float
    price: float = 0.0  # For market orders, use 0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 20  # Max slippage in points
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
    ZeroMQ bridge for MT5 communication.
    
    Manages connection lifecycle, message serialization, and async data handling.
    """
    
    def __init__(
        self,
        req_port: int = 5555,
        sub_port: int = 5556,
        host: str = "localhost",
        recv_timeout_ms: int = 5000,
        send_timeout_ms: int = 1000
    ):
        self.req_port = req_port
        self.sub_port = sub_port
        self.host = host
        self.recv_timeout_ms = recv_timeout_ms
        self.send_timeout_ms = send_timeout_ms
        
        # ZeroMQ context and sockets
        self._context: Optional[zmq.Context] = None
        self._req_socket: Optional[zmq.Socket] = None
        self._sub_socket: Optional[zmq.Socket] = None
        
        # Data handlers
        self._tick_handlers: Dict[str, List[Callable]] = {}
        self._bar_handlers: Dict[str, List[Callable]] = {}
        
        # Subscription management
        self._subscribed_symbols: set = set()
        
        # Background thread for data streaming
        self._data_thread: Optional[Thread] = None
        self._stop_event = Event()
        self._data_queue: Queue = Queue()
        
        # Connection state
        self._connected = False
        self._last_heartbeat: Optional[datetime] = None
    
    def connect(self) -> bool:
        """
        Establish ZeroMQ connections to MT5.
        
        Returns:
            True if connection successful
        """
        try:
            self._context = zmq.Context()
            
            # REQ socket for commands
            self._req_socket = self._context.socket(zmq.REQ)
            self._req_socket.setsockopt(zmq.RCVTIMEO, self.recv_timeout_ms)
            self._req_socket.setsockopt(zmq.SNDTIMEO, self.send_timeout_ms)
            self._req_socket.setsockopt(zmq.LINGER, 0)
            self._req_socket.connect(f"tcp://{self.host}:{self.req_port}")
            
            # SUB socket for market data
            self._sub_socket = self._context.socket(zmq.SUB)
            self._sub_socket.setsockopt(zmq.RCVTIMEO, 100)  # Short timeout for polling
            self._sub_socket.connect(f"tcp://{self.host}:{self.sub_port}")
            
            # Test connection with ping
            if self._ping():
                self._connected = True
                self._start_data_thread()
                logger.info(f"MT5 Bridge connected | REQ:{self.req_port} SUB:{self.sub_port}")
                return True
            else:
                logger.error("MT5 Bridge connection failed - no response to ping")
                return False
                
        except Exception as e:
            logger.error(f"MT5 Bridge connection error: {e}")
            return False
    
    def disconnect(self) -> None:
        """Disconnect from MT5 and cleanup resources."""
        self._stop_event.set()
        
        if self._data_thread and self._data_thread.is_alive():
            self._data_thread.join(timeout=2.0)
        
        if self._req_socket:
            self._req_socket.close()
        if self._sub_socket:
            self._sub_socket.close()
        if self._context:
            self._context.term()
        
        self._connected = False
        logger.info("MT5 Bridge disconnected")
    
    def _ping(self) -> bool:
        """Send ping to verify connection."""
        try:
            response = self._send_command(MessageType.PING, {})
            return response.get('status') == 'PONG'
        except:
            return False
    
    def _send_command(self, msg_type: MessageType, data: dict) -> dict:
        """
        Send command to MT5 and wait for response.
        
        Args:
            msg_type: Type of message
            data: Message payload
            
        Returns:
            Response dictionary
        """
        if not self._req_socket:
            raise ConnectionError("Not connected to MT5")
        
        message = {
            'type': msg_type.value,
            'data': data,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            self._req_socket.send_json(message)
            response = self._req_socket.recv_json()
            return response
        except zmq.Again:
            raise BridgeTimeoutError(
                f"MT5 ZMQ timeout after {self.recv_timeout_ms}ms "
                f"(command={msg_type.value}). MT5 terminal may be frozen."
            )
        except Exception as e:
            raise ConnectionError(f"MT5 communication error: {e}")
    
    def _start_data_thread(self) -> None:
        """Start background thread for receiving market data."""
        self._stop_event.clear()
        self._data_thread = Thread(target=self._data_receiver, daemon=True)
        self._data_thread.start()
    
    def _data_receiver(self) -> None:
        """Background thread for receiving SUB messages."""
        while not self._stop_event.is_set():
            try:
                if self._sub_socket.poll(100):
                    message = self._sub_socket.recv_json()
                    self._process_data_message(message)
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Data receiver error: {e}")
                time.sleep(0.1)
    
    def _process_data_message(self, message: dict) -> None:
        """Process incoming market data message."""
        msg_type = message.get('type')
        data = message.get('data', {})
        
        if msg_type == MessageType.TICK.value:
            tick = TickData(
                symbol=data['symbol'],
                bid=data['bid'],
                ask=data['ask'],
                last=data.get('last', data['bid']),
                volume=data.get('volume', 0),
                time=datetime.fromisoformat(data['time'])
            )
            self._dispatch_tick(tick)
            
        elif msg_type == MessageType.BAR.value:
            bar = BarData(
                symbol=data['symbol'],
                timeframe=data['timeframe'],
                time=datetime.fromisoformat(data['time']),
                open=data['open'],
                high=data['high'],
                low=data['low'],
                close=data['close'],
                volume=data['volume']
            )
            self._dispatch_bar(bar)
    
    def _dispatch_tick(self, tick: TickData) -> None:
        """Dispatch tick to registered handlers."""
        handlers = self._tick_handlers.get(tick.symbol, [])
        for handler in handlers:
            try:
                handler(tick)
            except Exception as e:
                logger.error(f"Tick handler error for {tick.symbol}: {e}")
    
    def _dispatch_bar(self, bar: BarData) -> None:
        """Dispatch bar to registered handlers."""
        key = f"{bar.symbol}_{bar.timeframe}"
        handlers = self._bar_handlers.get(key, [])
        for handler in handlers:
            try:
                handler(bar)
            except Exception as e:
                logger.error(f"Bar handler error for {key}: {e}")
    
    # === Public API ===
    
    def subscribe_ticks(self, symbol: str, handler: Callable[[TickData], None]) -> None:
        """
        Subscribe to tick data for a symbol.
        
        Args:
            symbol: Instrument symbol (e.g., "DE40")
            handler: Callback function for tick data
        """
        if symbol not in self._tick_handlers:
            self._tick_handlers[symbol] = []
            # Subscribe on ZMQ socket
            if self._sub_socket:
                self._sub_socket.setsockopt_string(zmq.SUBSCRIBE, f"TICK_{symbol}")
        
        self._tick_handlers[symbol].append(handler)
        self._subscribed_symbols.add(symbol)
        
        # Notify MT5 to start streaming
        self._send_command(MessageType.SUBSCRIBE, {'symbol': symbol, 'type': 'tick'})
        logger.info(f"Subscribed to ticks: {symbol}")
    
    def subscribe_bars(
        self, 
        symbol: str, 
        timeframe: str, 
        handler: Callable[[BarData], None]
    ) -> None:
        """
        Subscribe to bar data for a symbol/timeframe.
        
        Args:
            symbol: Instrument symbol
            timeframe: Timeframe (e.g., "M5", "H1")
            handler: Callback function for bar data
        """
        key = f"{symbol}_{timeframe}"
        if key not in self._bar_handlers:
            self._bar_handlers[key] = []
            if self._sub_socket:
                self._sub_socket.setsockopt_string(zmq.SUBSCRIBE, f"BAR_{key}")
        
        self._bar_handlers[key].append(handler)
        
        self._send_command(MessageType.SUBSCRIBE, {
            'symbol': symbol, 
            'type': 'bar',
            'timeframe': timeframe
        })
        logger.info(f"Subscribed to bars: {key}")
    
    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from all data for a symbol."""
        self._tick_handlers.pop(symbol, None)
        self._subscribed_symbols.discard(symbol)
        
        # Remove bar handlers for this symbol
        keys_to_remove = [k for k in self._bar_handlers if k.startswith(symbol)]
        for key in keys_to_remove:
            del self._bar_handlers[key]
        
        self._send_command(MessageType.UNSUBSCRIBE, {'symbol': symbol})
        logger.info(f"Unsubscribed: {symbol}")
    
    def send_order(self, request: OrderRequest) -> OrderResult:
        """
        Send order to MT5.
        
        Args:
            request: Order parameters
            
        Returns:
            OrderResult with execution details
        """
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
            # Let timeout propagate — engine needs this for reconciliation
            raise
        except Exception as e:
            logger.error(f"Order error: {e}")
            return OrderResult(
                success=False,
                ticket=0,
                order_type='',
                lots=0,
                price=0,
                sl=0,
                tp=0,
                error_code=-1,
                error_message=str(e)
            )
    
    def execute_spread(
        self,
        request_a: OrderRequest,
        request_b: OrderRequest
    ) -> Tuple[OrderResult, OrderResult]:
        """
        Execute both legs of a spread trade concurrently.
        
        Uses ThreadPoolExecutor to fire both orders simultaneously,
        reducing inter-leg gap from ~100-400ms (sequential) to ~5-20ms.
        
        Args:
            request_a: First leg order
            request_b: Second leg order
            
        Returns:
            Tuple of (result_a, result_b)
        """
        logger.info(
            f"Spread execution: {request_a.symbol} {request_a.order_type.value} + "
            f"{request_b.symbol} {request_b.order_type.value}"
        )
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(self.send_order, request_a)
            future_b = executor.submit(self.send_order, request_b)
            
            result_a = future_a.result(timeout=10)
            result_b = future_b.result(timeout=10)
        
        # Check for leg imbalance
        if result_a.success != result_b.success:
            failed = "A" if not result_a.success else "B"
            succeeded = "B" if not result_a.success else "A"
            logger.error(
                f"SPREAD LEG IMBALANCE: Leg {failed} failed, Leg {succeeded} succeeded. "
                f"Manual intervention may be required."
            )
        
        return result_a, result_b
    
    def modify_position(
        self,
        ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None
    ) -> bool:
        """
        Modify stop loss and/or take profit of a position.
        
        Args:
            ticket: Position ticket number
            sl: New stop loss (None to keep current)
            tp: New take profit (None to keep current)
            
        Returns:
            True if modification successful
        """
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
        """
        Close a position (full or partial).
        
        Args:
            ticket: Position ticket number
            lots: Lots to close (None for full close)
            
        Returns:
            True if close successful
        """
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
        """
        Close all positions (optionally filtered by symbol).
        
        Args:
            symbol: Symbol to filter (None for all)
            
        Returns:
            Number of positions closed
        """
        data = {}
        if symbol:
            data['symbol'] = symbol
        
        response = self._send_command(MessageType.CLOSE_ALL, data)
        closed = response.get('closed_count', 0)
        
        logger.warning(f"Close all positions: {closed} closed")
        return closed
    
    def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Get open positions.
        
        Args:
            symbol: Filter by symbol (None for all)
            
        Returns:
            List of Position objects
        """
        data = {}
        if symbol:
            data['symbol'] = symbol
        
        response = self._send_command(MessageType.GET_POSITIONS, data)
        positions_data = response.get('positions', [])
        
        positions = []
        for p in positions_data:
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
                open_time=datetime.fromisoformat(p['open_time']),
                magic=p.get('magic', 0),
                comment=p.get('comment', '')
            ))
        
        return positions
    
    def get_account_info(self) -> AccountInfo:
        """Get current account information."""
        response = self._send_command(MessageType.GET_ACCOUNT, {})
        data = response.get('account', {})
        
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
        """
        Get broker server time, GMT offset, and day-of-week.

        Returns ServerTimeInfo or None if the EA doesn't support GET_SERVER_TIME
        (e.g. older EA version without the handler).
        """
        try:
            response = self._send_command(MessageType.GET_SERVER_TIME, {})
        except (BridgeTimeoutError, ConnectionError) as e:
            logger.warning(f"get_server_time failed: {e}")
            return None

        if 'error' in response:
            logger.warning(f"Server time error: {response['error']}")
            return None

        data = response.get('server_time', {})
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

    def get_quote(self, symbol: str) -> Optional[TickData]:
        """Get current quote for a symbol. Returns None on error or invalid data."""
        response = self._send_command(MessageType.GET_QUOTE, {'symbol': symbol})

        # Check for error response from MT5
        if 'error' in response:
            logger.warning(f"Quote error for {symbol}: {response['error']}")
            return None

        data = response.get('quote', {})
        bid = data.get('bid', 0)
        ask = data.get('ask', 0)

        # Guard against zero/invalid prices (error responses return bid=0, ask=0)
        if bid <= 0 or ask <= 0:
            logger.warning(f"Invalid quote for {symbol}: bid={bid}, ask={ask}")
            return None

        return TickData(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=data.get('last', bid),
            volume=data.get('volume', 0),
            time=datetime.fromisoformat(data.get('time', datetime.utcnow().isoformat()))
        )
    
    @property
    def is_connected(self) -> bool:
        """Check if bridge is connected."""
        return self._connected
    
    def heartbeat(self) -> bool:
        """Send heartbeat to verify connection is alive."""
        try:
            if self._ping():
                self._last_heartbeat = datetime.utcnow()
                return True
            return False
        except:
            return False


class MT5BridgeAsync:
    """
    Async wrapper for MT5Bridge.
    
    Provides asyncio-compatible interface for use with async trading loops.
    """
    
    def __init__(self, bridge: MT5Bridge):
        self._bridge = bridge
        self._loop: Optional[asyncio.AbstractEventLoop] = None
    
    async def connect(self) -> bool:
        """Async connect."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.connect)
    
    async def disconnect(self) -> None:
        """Async disconnect."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._bridge.disconnect)
    
    async def send_order(self, request: OrderRequest) -> OrderResult:
        """Async order send."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.send_order, request)
    
    async def close_position(self, ticket: int, lots: Optional[float] = None) -> bool:
        """Async position close."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.close_position, ticket, lots)
    
    async def close_all_positions(self, symbol: Optional[str] = None) -> int:
        """Async close all."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.close_all_positions, symbol)
    
    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Async get positions."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_positions, symbol)
    
    async def get_account_info(self) -> AccountInfo:
        """Async get account info."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_account_info)
    
    async def get_quote(self, symbol: str) -> TickData:
        """Async get quote."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._bridge.get_quote, symbol)
