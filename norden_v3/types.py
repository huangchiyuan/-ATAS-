"""
统一的基础类型定义
====================

v3.1 引擎希望做到：
- 与具体的数据源（UDP / Rithmic / 其他撮合）解耦
- 与具体的执行端（C# / Python / FIX）解耦

因此在这里定义一组“最小可用”的数据与指令结构：
- 输入：Tick / DOM / 订单成交回报
- 输出：挂单 / 撤单 等指令（不关心如何真正下到交易所）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Tuple, Optional


class Side(Enum):
    """买卖方向."""

    BUY = auto()
    SELL = auto()


class OrderType(Enum):
    """订单类型（在 v3.1 中我们只允许 LIMIT，Market 仅用于紧急平仓）."""

    LIMIT = auto()
    MARKET = auto()


@dataclass
class TickEvent:
    """
    标准化的多品种 tick 事件.

    - t_ms: 事件时间（毫秒级时间戳，来源可为本地 / 交易所，可以统一做转换）
    - es: ES 最新价
    - nq: NQ 最新价
    - ym: YM 最新价（如暂时没有，可设为 None）
    - btc: BTC 指数价（可选，用于 regime filter）
    """

    t_ms: int
    es: float
    nq: float
    ym: Optional[float] = None
    btc: Optional[float] = None


@dataclass
class DomSnapshot:
    """
    标准化 DOM 快照.

    注意：
    - 当前项目的 UDP 流提供的是“聚合档位”（price, volume），不是 MBO。
    - v3.1 的排队长度（Queue Length）理想情况应依赖 MBO，但在没有 MBO 时，
      可以先用聚合成交量做一个“弱替代指标”，后续再接入真正的 MBO 流。
    """

    t_ms: int
    best_bid: float
    best_ask: float
    bids: List[Tuple[float, float]]  # (price, volume)
    asks: List[Tuple[float, float]]  # (price, volume)

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class OrderCommand:
    """
    引擎输出的标准指令.

    引擎不关心：
        - 账户
        - 具体 API（Rithmic / C# / 其他）
    只关心：
        - 我要以什么价格、什么方向、多少数量、用什么类型下单 / 撤单。
    """

    # 行为：新下单 或 撤单
    is_cancel: bool

    # 订单ID 由上层执行模块分配 / 维护
    client_order_id: Optional[str] = None

    # 下新单时必填
    side: Optional[Side] = None
    order_type: Optional[OrderType] = None
    price: Optional[float] = None
    quantity: Optional[int] = None

    # 方便调试的附加字段（可选）
    reason: str = ""



