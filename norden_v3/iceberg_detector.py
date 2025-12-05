"""
冰山订单检测模块 (Iceberg Detector)
===================================

核心思想：
    通过比较"成交量"和"可见挂单量"的差异，识别隐藏的冰山订单。
    
检测逻辑：
    1. 监听 Best Bid/Ask 的价格和挂单量
    2. 当发生成交时，比较：
       - 如果成交量 >= 可见挂单量，但价格未变
       - 说明有隐藏订单（冰山）在补充挂单
    3. 记录隐藏量：Hidden = Trade_Volume - Displayed_Size

应用场景：
    - Layer 3 防御因子：检测目标方向的阻力/支撑
    - 做多时：检查上方是否有卖单冰山（阻力）
    - 做空时：检查下方是否有买单冰山（支撑）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple
import time

from .types import DomSnapshot, TickEvent
from .config import IcebergConfig


@dataclass
class QuoteState:
    """当前盘口状态快照."""

    best_bid_price: float = 0.0
    best_bid_size: float = 0.0
    best_ask_price: float = 0.0
    best_ask_size: float = 0.0
    timestamp_ms: int = 0


class IcebergDetector:
    """
    实时冰山订单检测器.

    使用方式:
        detector = IcebergDetector()
        
        # 更新盘口
        detector.on_dom(dom_snapshot)
        
        # 处理成交
        detector.on_trade(price, volume, side)
        
        # 查询阻力/支撑
        resistance = detector.get_resistance(current_price)
        support = detector.get_support(current_price)
    """

    def __init__(self, cfg: Optional[IcebergConfig] = None):
        self.cfg = cfg or IcebergConfig()

        # 当前盘口状态
        self.current_quote: Optional[QuoteState] = None

        # 冰山地图：{price: hidden_volume}
        # 正数 = Ask侧冰山（阻力），负数 = Bid侧冰山（支撑）
        self.iceberg_map: Dict[float, float] = {}

        # 冰山发现时间：{price: timestamp_ms}
        self.iceberg_times: Dict[float, int] = {}

        # 用于批量聚合的临时成交缓存（同一毫秒内的成交）
        self.trade_buffer: List[Tuple[float, float, str, int]] = []  # (price, volume, side, timestamp_ms)
        self.last_trade_time_ms: Optional[int] = None

    def reset(self) -> None:
        """重置检测器状态."""
        self.current_quote = None
        self.iceberg_map.clear()
        self.iceberg_times.clear()
        self.trade_buffer.clear()
        self.last_trade_time_ms = None

    def on_dom(self, dom: DomSnapshot) -> None:
        """
        更新盘口状态.

        每次收到 DOM 更新时调用，用于记录当前的 Best Bid/Ask。
        """
        # 在处理新 DOM 前，先处理之前缓存的成交（如果有）
        if self.trade_buffer:
            self._process_trade_batch()

        if not dom.bids or not dom.asks:
            return

        self.current_quote = QuoteState(
            best_bid_price=dom.best_bid,
            best_bid_size=dom.bids[0][1] if dom.bids else 0.0,
            best_ask_price=dom.best_ask,
            best_ask_size=dom.asks[0][1] if dom.asks else 0.0,
            timestamp_ms=dom.t_ms,
        )

        # 顺便清理过期的冰山
        self._cleanup_expired(dom.t_ms)

    def on_trade(
        self,
        price: float,
        volume: float,
        side: str,
        timestamp_ms: int,
    ) -> None:
        """
        处理单笔成交事件.

        注意：Rithmic 可能会将一笔大单拆成多笔小成交推送，
        所以我们需要先聚合同一时间窗口内的成交，再进行检测。
        """
        # 如果是新的时间窗口，先处理之前的缓存
        if (
            self.last_trade_time_ms is not None
            and timestamp_ms > self.last_trade_time_ms
        ):
            self._process_trade_batch()

        # 添加到缓存
        self.trade_buffer.append((price, volume, side, timestamp_ms))
        self.last_trade_time_ms = timestamp_ms

    def _process_trade_batch(self) -> None:
        """处理缓存中的批量成交（同一时间窗口内的所有成交）."""
        if not self.trade_buffer or not self.current_quote:
            self.trade_buffer.clear()
            return

        # 按价格和方向聚合成交
        # agg_trades: {(price, side): total_volume}
        agg_trades: Dict[Tuple[float, str], float] = defaultdict(float)

        for price, volume, side, _ in self.trade_buffer:
            key = (price, side)
            agg_trades[key] += volume

        # 对每笔聚合后的成交进行检测
        for (price, side), total_vol in agg_trades.items():
            self._detect_iceberg(price, total_vol, side)

        # 清空缓存
        self.trade_buffer.clear()

    def _detect_iceberg(self, price: float, volume: float, side: str) -> None:
        """
        核心检测逻辑：判断是否存在冰山订单.

        算法：
            1. 如果是主动买入（BUY），价格应该在 Ask 侧
            2. 如果成交量 >= Ask 挂单量，但价格没变，说明有隐藏卖单
            3. 同理，主动卖出时检测隐藏买单
        """
        if not self.current_quote:
            return

        # 检测 Ask 侧冰山（阻力）
        if side.upper() in ("BUY", "B"):
            # 价格应该在 Ask 侧（或接近）
            if abs(price - self.current_quote.best_ask_price) < self.cfg.price_tolerance:
                displayed_size = self.current_quote.best_ask_size

                # 核心判定：成交量 >= 挂单量，但价格未变
                if volume >= displayed_size and displayed_size > 0:
                    hidden_vol = volume - displayed_size

                    if hidden_vol >= self.cfg.min_hidden_size:
                        # 发现冰山！
                        self._update_iceberg(
                            price, hidden_vol, "ASK", self.current_quote.timestamp_ms
                        )

        # 检测 Bid 侧冰山（支撑）
        elif side.upper() in ("SELL", "S"):
            # 价格应该在 Bid 侧（或接近）
            if abs(price - self.current_quote.best_bid_price) < self.cfg.price_tolerance:
                displayed_size = self.current_quote.best_bid_size

                if volume >= displayed_size and displayed_size > 0:
                    hidden_vol = volume - displayed_size

                    if hidden_vol >= self.cfg.min_hidden_size:
                        # 发现冰山！
                        self._update_iceberg(
                            price, -hidden_vol, "BID", self.current_quote.timestamp_ms
                        )

    def _update_iceberg(
        self,
        price: float,
        hidden_vol: float,
        side: str,
        timestamp_ms: int,
    ) -> None:
        """
        更新冰山地图.

        Args:
            price: 价格
            hidden_vol: 隐藏量（Ask侧为正，Bid侧为负）
            side: "ASK" 或 "BID"
            timestamp_ms: 时间戳
        """
        # 如果同一位置已有冰山，累加（说明是连续的大单）
        if price in self.iceberg_map:
            # 同向累加
            existing_vol = self.iceberg_map[price]
            if (existing_vol > 0 and hidden_vol > 0) or (
                existing_vol < 0 and hidden_vol < 0
            ):
                self.iceberg_map[price] += hidden_vol
            else:
                # 反向，取较大的绝对值
                if abs(hidden_vol) > abs(existing_vol):
                    self.iceberg_map[price] = hidden_vol
        else:
            self.iceberg_map[price] = hidden_vol

        self.iceberg_times[price] = timestamp_ms

    def _cleanup_expired(self, current_time_ms: int) -> None:
        """清理过期的冰山记录."""
        decay_ms = int(self.cfg.decay_seconds * 1000)
        expired_prices = [
            p
            for p, t_ms in self.iceberg_times.items()
            if current_time_ms - t_ms > decay_ms
        ]

        for price in expired_prices:
            self.iceberg_map.pop(price, None)
            self.iceberg_times.pop(price, None)

    def get_resistance(
        self, current_price: float, range_ticks: Optional[int] = None
    ) -> float:
        """
        查询上方的阻力（Ask 侧冰山总量）.

        Args:
            current_price: 当前价格
            range_ticks: 检查范围（tick 数），默认使用配置值

        Returns:
            阻力总量（手数），0 表示无阻力
        """
        if range_ticks is None:
            range_ticks = self.cfg.check_range_ticks

        tick_size = 0.25  # ES tick size
        total_resistance = 0.0

        # 检查上方 range_ticks 个 tick 的价格
        for i in range(1, range_ticks + 1):
            check_price = current_price + (i * tick_size)

            # 查找该价格附近的冰山
            for iceberg_price, hidden_vol in self.iceberg_map.items():
                if (
                    abs(iceberg_price - check_price) < self.cfg.price_tolerance
                    and hidden_vol > 0  # Ask 侧（阻力）
                ):
                    total_resistance += hidden_vol

        return total_resistance

    def get_support(
        self, current_price: float, range_ticks: Optional[int] = None
    ) -> float:
        """
        查询下方的支撑（Bid 侧冰山总量）.

        Args:
            current_price: 当前价格
            range_ticks: 检查范围（tick 数）

        Returns:
            支撑总量（手数），0 表示无支撑
        """
        if range_ticks is None:
            range_ticks = self.cfg.check_range_ticks

        tick_size = 0.25
        total_support = 0.0

        # 检查下方 range_ticks 个 tick 的价格
        for i in range(1, range_ticks + 1):
            check_price = current_price - (i * tick_size)

            for iceberg_price, hidden_vol in self.iceberg_map.items():
                if (
                    abs(iceberg_price - check_price) < self.cfg.price_tolerance
                    and hidden_vol < 0  # Bid 侧（支撑）
                ):
                    total_support += abs(hidden_vol)

        return total_support

    def check_iceberg_resistance(
        self, price: float, direction: int, range_ticks: Optional[int] = None
    ) -> bool:
        """
        检查指定方向是否有冰山阻力（策略接口）.

        Args:
            price: 当前价格
            direction: 1=想做多（检查上方阻力），-1=想做空（检查下方阻力）
            range_ticks: 检查范围

        Returns:
            True 表示有阻力，应该放弃交易
        """
        if direction > 0:
            # 想做多，检查上方阻力
            resistance = self.get_resistance(price, range_ticks)
            return resistance > 200  # 超过 200 手认为有显著阻力
        else:
            # 想做空，检查下方支撑（反向阻力）
            support = self.get_support(price, range_ticks)
            return support > 200

    def flush_trade_buffer(self) -> None:
        """强制处理缓存中的所有成交（用于确保所有数据都被处理）."""
        if self.trade_buffer:
            self._process_trade_batch()

    def get_iceberg_map(self) -> Dict[float, float]:
        """获取当前冰山地图（用于调试/可视化）."""
        return self.iceberg_map.copy()

