"""
基于 Ridge 模型的策略引擎包装器
================================

复用 NordenMakerV3 的过滤器和执行逻辑，但使用 Ridge 模型进行定价。
用于批量回测中对 Ridge 模型的支持。
"""

from __future__ import annotations
from typing import Optional, Callable

from .maker_engine import NordenMakerV3
from .types import TickEvent, DomSnapshot, OrderCommand, Side
from .ridge_model import OnlineRidge
from .config import MakerConfig, RidgeConfig


class RidgeMakerEngine:
    """
    基于 Ridge 模型的策略引擎包装器
    
    复用 NordenMakerV3 的所有逻辑，但使用 Ridge 模型进行定价。
    """
    
    def __init__(
        self,
        maker_cfg: Optional[MakerConfig] = None,
        ridge_cfg: Optional[RidgeConfig] = None,
        order_sink: Optional[Callable[[OrderCommand], None]] = None,
    ):
        # 创建一个 Kalman 引擎用于复用所有过滤器和执行逻辑
        # 但我们会覆盖它的定价结果
        self.base_engine = NordenMakerV3(
            maker_cfg=maker_cfg,
            kalman_cfg=None,  # 使用默认 Kalman 配置（不会被使用）
            order_sink=order_sink,
        )
        
        # 使用 Ridge 模型进行定价
        self.ridge_model = OnlineRidge(ridge_cfg or RidgeConfig())
        
        # 保存最新的 Ridge 结果
        self.last_fair: Optional[float] = None
        self.last_spread: Optional[float] = None
        self.last_spread_ticks: Optional[float] = None
    
    @property
    def cfg(self):
        """访问策略配置"""
        return self.base_engine.cfg
    
    @property
    def last_dom(self):
        """访问最后 DOM"""
        return self.base_engine.last_dom
    
    @property
    def es_tick_size(self):
        """访问 tick size"""
        return self.base_engine.es_tick_size
    
    def on_tick(self, tick: TickEvent) -> None:
        """
        Tick 事件入口（使用 Ridge 模型定价）
        """
        # 更新 BTC 监控器
        if tick.btc is not None:
            self.base_engine.btc_monitor.on_tick(tick.btc)
        
        # 使用 Ridge 模型更新
        fair, spread = self.ridge_model.update(tick)
        
        # 保存最新结果
        self.last_fair = fair
        self.last_spread = spread
        self.last_spread_ticks = (spread / self.es_tick_size) if spread is not None else None
        
        # 处理已有挂单/持仓（使用 Ridge 的 fair 和 spread）
        self.base_engine._manage_active_order(tick, fair, spread)
        
        # 当前有挂单/仓位则不再开新仓
        if self.base_engine.position.active_order_id is not None:
            return
        
        # 没有 fair 或 spread，无法做决策
        if fair is None or spread is None:
            return
        
        # 将价差转换为 tick 数
        spread_ticks = spread / self.es_tick_size if self.es_tick_size > 0 else spread
        
        # 信号生成
        threshold = self.base_engine._dynamic_threshold()
        want_long = spread_ticks > threshold
        want_short = spread_ticks < -threshold
        
        if not (want_long or want_short):
            return
        
        # 多重过滤（复用 base_engine 的逻辑）
        if not self.base_engine._pass_filters(tick, want_long=want_long, want_short=want_short):
            return
        
        # 队列博弈 + 执行
        if not self.last_dom:
            return
        
        if want_long:
            self.base_engine._maybe_place_limit(side=Side.BUY)
        elif want_short:
            self.base_engine._maybe_place_limit(side=Side.SELL)
    
    def on_dom(self, dom: DomSnapshot) -> None:
        """DOM 事件入口"""
        self.base_engine.on_dom(dom)
    
    def _calc_obi(self, dom: DomSnapshot) -> float:
        """计算 OBI（用于信号记录）"""
        return self.base_engine._calc_obi(dom)
    
    @property
    def btc_monitor(self):
        """访问 BTC 监控器"""
        return self.base_engine.btc_monitor

