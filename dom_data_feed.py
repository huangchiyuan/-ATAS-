import socket
import threading
import queue
import time
from collections import deque

# ===================== 数据连接与深度配置 =====================
UDP_IP = "0.0.0.0"
UDP_PORT = 5555

# DOM / 行情参数（供 GUI 引用）
DEPTH_LEVELS = 15             # 每边最大深度档数（从 C# 传来的深度里截取）
DOM_ROWS = DEPTH_LEVELS * 2   # DOM 总行数（bid + ask，对称在中间价上下展开）
PRICE_TICK = 0.25             # ES/NQ 最小价位间距，用于补齐中间“空价位”
MAX_TRADE_TIPS = 30
REFRESH_INTERVAL_MS = 50      # GUI 刷新间隔

TICKS_AT_EPOCH = 621355968000000000


def ticks_to_str(ticks: str) -> str:
    """将 C# ticks（.NET Ticks）转换为 HH:MM:SS 字符串，仅用于成交明细展示。"""
    try:
        total_us = (int(ticks) - TICKS_AT_EPOCH) // 10
        seconds = total_us // 1_000_000
        tm = time.gmtime(seconds)
        return time.strftime("%H:%M:%S", tm)
    except Exception:
        return "--:--:--"


class InstrumentState:
    """
    保存单个品种的 DOM 与成交信息。
    - bids / asks: 已解析、按价格排序的 (price, volume) 列表
    - trade_tips: 最近成交提示，用于 GUI 列表展示
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids = [(0.0, 0)] * DEPTH_LEVELS
        self.asks = [(0.0, 0)] * DEPTH_LEVELS
        self.trade_tips = deque(maxlen=MAX_TRADE_TIPS)
        self.last_update = 0.0
        self.last_price = 0.0  # 最新成交价，用于DOM表格以当前价格为中心显示

    def update_dom(self, bids_str: str, asks_str: str):
        # 调试：显示原始数据（仅前几次）
        if not hasattr(self, '_dom_debug_count'):
            self._dom_debug_count = 0
        self._dom_debug_count += 1
        if self._dom_debug_count <= 3:
            print(f"[DOM RAW] {self.symbol} - Bids原始: {bids_str[:200]}...")
            print(f"[DOM RAW] {self.symbol} - Asks原始: {asks_str[:200]}...")
        
        # 修复：保留最靠近当前价格的15档，而不是简单取前15档
        # C#发送的Bids是从高到低，Asks是从低到高
        # 我们需要保留最靠近当前价格（last_price）的15档
        # 如果没有当前价格，使用best_bid和best_ask的中间价作为参考
        reference_price = self.last_price
        if reference_price <= 0:
            # 临时解析一次以获取best_bid和best_ask（不使用参考价格，直接取前15档）
            temp_bids = self._parse_levels(bids_str, reverse=False, reference_price=0.0, is_bid=True)
            temp_asks = self._parse_levels(asks_str, reverse=False, reference_price=0.0, is_bid=False)
            valid_bids = [x for x in temp_bids if x[0] > 0 and x[1] > 0]
            valid_asks = [x for x in temp_asks if x[0] > 0 and x[1] > 0]
            if valid_bids and valid_asks:
                best_bid = max(x[0] for x in valid_bids)
                best_ask = min(x[0] for x in valid_asks)
                reference_price = (best_bid + best_ask) / 2.0
            elif valid_bids:
                reference_price = max(x[0] for x in valid_bids)
            elif valid_asks:
                reference_price = min(x[0] for x in valid_asks)
        
        # 调试：显示参考价格
        if self._dom_debug_count <= 3:
            print(f"[DOM REF] {self.symbol} - 参考价格: {reference_price:.2f} (last_price={self.last_price:.2f})")
        
        self.bids = self._parse_levels(bids_str, reverse=False, reference_price=reference_price, is_bid=True)
        self.asks = self._parse_levels(asks_str, reverse=False, reference_price=reference_price, is_bid=False)
        self.last_update = time.time()
        
        # 调试：显示解析后的前3个数据
        if self._dom_debug_count <= 3:
            valid_bids = [x for x in self.bids if x[0] > 0 and x[1] > 0]
            valid_asks = [x for x in self.asks if x[0] > 0 and x[1] > 0]
            print(f"[DOM PARSED] {self.symbol} - Bids解析后前3个: {valid_bids[:3]}")
            print(f"[DOM PARSED] {self.symbol} - Asks解析后前3个: {valid_asks[:3]}")

    def add_trade(self, price: float, volume: float, side: str, ticks_str: str):
        tip = f"{ticks_to_str(ticks_str)} {self.symbol:<6} {side:<4} {volume:>5.0f} @ {price:.2f}"
        self.trade_tips.appendleft(tip)
        self.last_price = price  # 更新最新成交价
        self.last_update = time.time()

    @staticmethod
    def _parse_levels(raw: str, reverse: bool, reference_price: float = 0.0, is_bid: bool = True) -> list:
        """
        将 C# 发送的 "price@vol|price@vol|..." 字符串解析为 (price, vol) 列表，
        保留最靠近参考价格的 DEPTH_LEVELS 档。
        
        Args:
            raw: 原始字符串，格式 "price@vol|price@vol|..."
            reverse: 是否反转列表顺序（保留参数以兼容旧代码，但不再使用）
            reference_price: 参考价格（当前价格），用于筛选最靠近的档位
            is_bid: 是否为买盘（Bids=True, Asks=False）
        """
        all_levels = []
        if raw:
            # 处理特殊情况：C#可能发送"0@0"作为占位符
            if raw == "0@0" or raw.strip() == "0@0":
                pass  # 不添加任何数据
            else:
                for item in raw.split('|'):
                    if '@' not in item:
                        continue
                    # 处理"0@0"占位符
                    if item == "0@0" or item.strip() == "0@0":
                        continue
                    parts = item.split('@')
                    if len(parts) != 2:
                        continue
                    price_str, vol_str = parts[0].strip(), parts[1].strip()
                    try:
                        price = float(price_str)
                        vol = int(float(vol_str))
                        # 只添加有效的价格和成交量
                        if price > 0 and vol > 0:
                            all_levels.append((price, vol))
                    except (ValueError, IndexError) as e:
                        # 调试：显示解析错误
                        if not hasattr(_parse_levels, '_error_count'):
                            _parse_levels._error_count = 0
                        _parse_levels._error_count += 1
                        if _parse_levels._error_count <= 3:
                            print(f"[DOM PARSE ERROR] 解析失败: '{item}' -> {e}")
                        continue
        
        # 如果没有参考价格，或者数据量少于等于DEPTH_LEVELS，直接返回前DEPTH_LEVELS档
        if reference_price <= 0 or len(all_levels) <= DEPTH_LEVELS:
            levels = all_levels[:DEPTH_LEVELS]
        else:
            # 根据参考价格，保留最靠近的DEPTH_LEVELS档
            # 策略：按价格距离排序，取最靠近reference_price的DEPTH_LEVELS档
            if is_bid:
                # Bids（买盘）：应该显示在当前价格下方（价格 <= 当前价格）
                # C#发送的Bids是从高到低，所以all_levels已经是按从高到低排序的
                # 我们需要找到最靠近reference_price的DEPTH_LEVELS档
                # 优先保留价格 <= reference_price 且最靠近的档位
                below = [x for x in all_levels if x[0] <= reference_price]
                above = [x for x in all_levels if x[0] > reference_price]
                
                if len(below) >= DEPTH_LEVELS:
                    # 如果below有足够档位，取最靠近reference_price的DEPTH_LEVELS档
                    # below是从高到低，所以前DEPTH_LEVELS个就是最靠近reference_price的
                    levels = below[:DEPTH_LEVELS]
                elif len(below) > 0:
                    # 如果below不足但>0，补充above中最靠近reference_price的档位
                    remaining = DEPTH_LEVELS - len(below)
                    # above是从高到低，最靠近reference_price的在前面
                    levels = below + above[:remaining]
                else:
                    # 如果所有档位都 > reference_price，说明当前价格在Bids范围之外
                    # 这种情况下，我们应该取最靠近reference_price的DEPTH_LEVELS档
                    # 由于all_levels是从高到低，我们需要找到最靠近reference_price的位置
                    # 然后取该位置附近的DEPTH_LEVELS档
                    # 计算每个档位到reference_price的距离，排序后取最近的DEPTH_LEVELS档
                    sorted_by_distance = sorted(all_levels, key=lambda x: abs(x[0] - reference_price))
                    levels = sorted_by_distance[:DEPTH_LEVELS]
                    # 但Bids应该保持从高到低的顺序，所以需要重新排序
                    levels.sort(key=lambda x: x[0], reverse=True)
            else:
                # Asks（卖盘）：应该显示在当前价格上方（价格 >= 当前价格）
                # C#发送的Asks是从低到高，所以all_levels已经是按从低到高排序的
                # 我们需要找到最靠近reference_price的DEPTH_LEVELS档
                # 优先保留价格 >= reference_price 且最靠近的档位
                above = [x for x in all_levels if x[0] >= reference_price]
                below = [x for x in all_levels if x[0] < reference_price]
                
                if len(above) >= DEPTH_LEVELS:
                    # 如果above有足够档位，取最靠近reference_price的DEPTH_LEVELS档
                    # above是从低到高，所以前DEPTH_LEVELS个就是最靠近reference_price的
                    levels = above[:DEPTH_LEVELS]
                elif len(above) > 0:
                    # 如果above不足但>0，补充below中最靠近reference_price的档位
                    remaining = DEPTH_LEVELS - len(above)
                    # below是从低到高，最靠近reference_price的在后面，需要反转
                    below.sort(key=lambda x: x[0], reverse=True)
                    levels = below[:remaining] + above
                else:
                    # 如果所有档位都 < reference_price，取最靠近的DEPTH_LEVELS档（后DEPTH_LEVELS个）
                    levels = all_levels[-DEPTH_LEVELS:] if len(all_levels) >= DEPTH_LEVELS else all_levels
        
        # 填充到DEPTH_LEVELS档
        while len(levels) < DEPTH_LEVELS:
            levels.append((0.0, 0))
        
        return levels


class UdpListener(threading.Thread):
    """
    只负责从 C# 端接收 UDP 数据，并放入线程安全队列，供 GUI / 策略消费。
    不关心如何显示，只关心数据格式：
      - T,Symbol,Price,Volume,Side,ExchangeTimeTicks
      - D,Symbol,bid1@vol1|...,ask1@vol1|...,ExchangeTimeTicks
    """

    def __init__(self, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self.running = True
        self.total_packets = 0
        self.last_log = time.time()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((UDP_IP, UDP_PORT))
        sock.settimeout(1.0)
        while self.running:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            text = data.decode(errors="ignore").strip()
            for line in text.split('\n'):
                if not line:
                    continue
                parts = line.split(',')
                msg_type = parts[0]
                if msg_type == 'T' and len(parts) >= 6:
                    event = {
                        'type': 'T',
                        'symbol': parts[1],
                        'price': float(parts[2]),
                        'volume': float(parts[3]),
                        'side': parts[4],
                        'ticks': parts[5],
                    }
                    self._safe_put(event)
                elif msg_type == 'D' and len(parts) >= 5:
                    event = {
                        'type': 'D',
                        'symbol': parts[1],
                        'bids': parts[2],
                        'asks': parts[3],
                        'ticks': parts[4],
                    }
                    self._safe_put(event)

                self.total_packets += 1

            now = time.time()
            if now - self.last_log >= 5.0:
                print(f"[Listener] Total packets: {self.total_packets:,} | Queue: {self.out_queue.qsize():,}")
                self.last_log = now

        sock.close()

    def stop(self):
        self.running = False

    def _safe_put(self, event):
        try:
            self.out_queue.put_nowait(event)
        except queue.Full:
            # 若队列爆满则丢弃最旧元素，避免阻塞
            try:
                _ = self.out_queue.get_nowait()
            except queue.Empty:
                pass
            self.out_queue.put_nowait(event)


