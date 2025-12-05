"""
NordenMakerV3 - 策略整体框架
===========================

本文件实现“骨架逻辑”，对应白皮书中的：
    - L1: 在线卡尔曼滤波定价 (OnlineKalman)
    - L2: 微观因子 OBI (订单簿加权失衡)
    - L3: 防御因子 冰山穿透 (Iceberg Map)
    - L4: 队列博弈 (Queue Filter)

注意：
    - 这里只实现“决策框架 + 接口”，真正的数据接入 / 下单执行由上层负责：
        * 上层负责把实盘 / 回测数据转换成 TickEvent / DomSnapshot
        * 上层负责把 OrderCommand 转换成具体 API 调用（Rithmic / C# / 其他）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, List, Callable
import time

import numpy as np

from .types import TickEvent, DomSnapshot, OrderCommand, Side, OrderType
from .kalman_model import OnlineKalman
from .obi_calculator import OBICalculator
from .iceberg_detector import IcebergDetector
from .btc_regime import BTCRegimeMonitor
from .config import (
    MakerConfig,
    KalmanConfig,
    OBIConfig,
    IcebergConfig,
    BTCRegimeConfig,
)


@dataclass
class PositionState:
    """当前持仓/挂单状态（极简版，只考虑单一仓位）."""

    active_order_id: Optional[str] = None
    entry_price: float = 0.0
    entry_time_ms: int = 0
    side: Optional[Side] = None


class NordenMakerV3:
    """
    白皮书 v3.1 的整体 Python 框架实现（不依赖任何 GUI / 特定 API）.

    使用方式（伪代码）::

        engine = NordenMakerV3(order_sink=your_callback)

        while running:
            tick = TickEvent(...)
            engine.on_tick(tick)

            dom = DomSnapshot(...)
            engine.on_dom(dom)

            # 当真实执行端有成交/撤单回报时，可以调用:
            engine.on_fill(...), engine.on_cancel(...)
    """

    def __init__(
        self,
        maker_cfg: Optional[MakerConfig] = None,
        kalman_cfg: Optional[KalmanConfig] = None,
        order_sink: Optional[Callable[[OrderCommand], None]] = None,
    ):
        self.cfg = maker_cfg or MakerConfig()
        self.kalman = OnlineKalman(kalman_cfg)
        self.position = PositionState()

        # OBI 计算器（使用策略配置中的 depth 参数）
        obi_cfg = OBIConfig(depth=self.cfg.obi_depth, decay=0.5)
        self.obi_calc = OBICalculator(obi_cfg)

        # 冰山检测器
        iceberg_cfg = IcebergConfig(min_hidden_size=10, decay_seconds=60.0)
        self.iceberg_detector = IcebergDetector(iceberg_cfg)

        # BTC 体制监控器（风控）
        btc_cfg = BTCRegimeConfig(alert_threshold=3.0)
        self.btc_monitor = BTCRegimeMonitor(btc_cfg)

        # 最近一次 DOM 快照（用于 OBI / queue 等）
        self.last_dom: Optional[DomSnapshot] = None

        # 最新计算结果（用于外部查询/显示）
        self.last_fair: Optional[float] = None
        self.last_spread: Optional[float] = None
        self.last_spread_ticks: Optional[float] = None

        # 下单/撤单的输出通道
        self.order_sink = order_sink or (lambda cmd: None)
        # ES 最小价位跳动
        self.es_tick_size: float = 0.25

    # ------------------------------------------------------------------
    # 外部事件入口
    # ------------------------------------------------------------------
    def on_tick(self, tick: TickEvent) -> None:
        """
        Tick 事件入口。

        - 更新 BTC 监控器（风控）
        - 更新 Kalman 状态
        - 根据 spread + 过滤器 生成决策
        """
        # 更新 BTC 监控器（每秒采样一次，计算成本极低）
        if tick.btc is not None:
            self.btc_monitor.on_tick(tick.btc)

        fair, spread = self.kalman.update(tick)

        # 保存最新结果（用于外部查询）
        self.last_fair = fair
        self.last_spread = spread
        self.last_spread_ticks = (spread / self.es_tick_size) if spread is not None else None

        # 先处理已有挂单/持仓（如止盈/止损/超时）
        self._manage_active_order(tick, fair, spread)

        # 当前有挂单 / 仓位则不再开新仓
        if self.position.active_order_id is not None:
            return

        # 没有 fair 或 spread，无法做决策
        if fair is None or spread is None:
            return

        # 将价差从点数转换为 tick 数，便于与 tick 级阈值比较
        spread_ticks = spread / self.es_tick_size if self.es_tick_size > 0 else spread

        # ------ Step 2: 信号生成 ------ #
        threshold = self._dynamic_threshold()
        want_long = spread_ticks > threshold
        want_short = spread_ticks < -threshold

        if not (want_long or want_short):
            return

        # ------ Step 3: 多重过滤 ------ #
        if not self._pass_filters(tick, want_long=want_long, want_short=want_short):
            return

        # ------ Step 4: 队列博弈 + 执行 ------ #
        if not self.last_dom:
            return

        if want_long:
            self._maybe_place_limit(side=Side.BUY)
        elif want_short:
            self._maybe_place_limit(side=Side.SELL)

    def on_dom(self, dom: DomSnapshot) -> None:
        """DOM 事件入口."""
        self.last_dom = dom
        # 更新冰山检测器的盘口状态
        self.iceberg_detector.on_dom(dom)

    # ------------------------------------------------------------------
    # 内部：过滤器 & 执行
    # ------------------------------------------------------------------
    def _dynamic_threshold(self) -> float:
        """
        动态阈值函数占位（单位：tick）。

        当前实现：始终返回固定的基础阈值。
        后续可以在此基础上接入：
            - 波动率自适应（根据 ES 波动率缩放阈值）
            - 根据盘口噪声动态调整
        """
        return self.cfg.base_spread_threshold

    def _pass_filters(self, tick: TickEvent, want_long: bool, want_short: bool) -> bool:
        """
        Layer 2/3/Regime 等多重过滤.

        过滤顺序：
            1. BTC 体制过滤（Layer 3 风控）
            2. 冰山过滤
            3. OBI 过滤
        """
        # --- Layer 3: BTC 体制过滤（熔断机制）---
        # 如果 BTC 市场处于极端波动状态，强制空仓
        if not self.btc_monitor.check_safety():
            # 市场不安全，拒绝所有交易信号
            return False

        if not self.last_dom:
            return False

        price = tick.es
        if price is None:
            return False

        # --- Layer 3: 冰山过滤 ---
        if self._check_iceberg_resistance(price, direction=1 if want_long else -1):
            return False

        # --- Layer 2: OBI 过滤 ---
        obi = self._calc_obi(self.last_dom)
        if want_long and obi < self.cfg.min_obi_for_long:
            return False
        if want_short and obi > -self.cfg.min_obi_for_short:
            return False

        # 其他过滤可以逐步补充
        return True

    def _calc_obi(self, dom: DomSnapshot) -> float:
        """
        订单簿加权失衡 (Weighted OBI).

        委托给独立的 OBICalculator 模块处理。
        """
        return self.obi_calc.calculate(dom)

    def _check_iceberg_resistance(self, price: float, direction: int) -> bool:
        """
        冰山过滤：检查目标方向是否有大冰山阻力.

        direction:
            +1 → 想做多，检查上方是否有大冰山阻力
            -1 → 想做空，检查下方是否有大冰山支撑

        Returns:
            True 表示有阻力，应该放弃交易
        """
        return self.iceberg_detector.check_iceberg_resistance(price, direction)

    def _estimate_queue_size(self, side: Side) -> float:
        """
        队列长度估计（弱化版本）.

        白皮书理想实现：
            - 使用 MBO 数据统计 best bid / best ask 排队张数（在你前面的）
        当前项目可用数据：
            - 只有每档聚合 volume，因此这里先用 best level volume 做“弱代替”：
                * Bid_Queue_Size ≈ best_bid_volume
                * Ask_Queue_Size ≈ best_ask_volume
        """
        if not self.last_dom:
            return 0.0

        if side == Side.BUY and self.last_dom.bids:
            return max(self.last_dom.bids[0][1], 0.0)
        if side == Side.SELL and self.last_dom.asks:
            return max(self.last_dom.asks[0][1], 0.0)
        return 0.0

    def _maybe_place_limit(self, side: Side) -> None:
        """队列过滤 + 发送挂单指令."""
        queue_size = self._estimate_queue_size(side)
        if queue_size > self.cfg.max_queue_size:
            # 排队太长，放弃本次机会
            return

        if not self.last_dom:
            return

        best_bid = self.last_dom.best_bid
        best_ask = self.last_dom.best_ask
        price = best_bid if side == Side.BUY else best_ask

        cmd = OrderCommand(
            is_cancel=False,
            side=side,
            order_type=OrderType.LIMIT,
            price=price,
            quantity=1,  # 数量由上层策略或配置决定，这里先占位为 1
            reason=f"maker_entry_{side.name.lower()}",
        )

        # 记录本地状态（这里先用时间戳生成一个临时 ID，真正 ID 由上层覆盖）
        now_ms = int(time.time() * 1000)
        self.position.active_order_id = cmd.client_order_id or f"local_{now_ms}"
        self.position.entry_price = price
        self.position.entry_time_ms = now_ms
        self.position.side = side

        self.order_sink(cmd)

    # ------------------------------------------------------------------
    # 已有挂单 / 仓位管理
    # ------------------------------------------------------------------
    def _manage_active_order(
        self,
        tick: TickEvent,
        fair: Optional[float],
        spread: Optional[float],
    ) -> None:
        """
        非空仓位/挂单的管理逻辑:
            - 价差消失 / 反向 → 撤单 / 减仓
            - 超时撤单 / 平仓

        这里仅实现“挂单等待”的简单管理:
            - 挂单超过 max_wait_seconds 未成交 → 撤单
        成交后的持仓管理（止盈/止损）留待后续与真实回报对接时补充。
        """
        if self.position.active_order_id is None:
            return

        now_ms = int(time.time() * 1000)
        elapsed = (now_ms - self.position.entry_time_ms) / 1000.0

        # 超时撤单
        if elapsed > self.cfg.max_wait_seconds:
            cancel_cmd = OrderCommand(
                is_cancel=True,
                client_order_id=self.position.active_order_id,
                reason="timeout_cancel",
            )
            self.order_sink(cancel_cmd)
            # 清空本地状态
            self.position = PositionState()
            return

        # 可以在这里增加更多管理逻辑，例如:
        # - spread 反向
        # - queue 急剧缩短 / 撤单潮 等
        _ = (tick, fair, spread)  # 占位，防 lint 用


