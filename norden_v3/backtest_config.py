"""
回测配置管理模块
================

用于批量回测和参数对比。
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import MakerConfig, KalmanConfig, RidgeConfig


class PricingModel(Enum):
    """定价模型选择"""
    KALMAN = "kalman"
    RIDGE = "ridge"
    BOTH = "both"  # 同时使用两个模型，取平均或分别测试


@dataclass
class BacktestConfig:
    """
    单个回测配置
    
    包含所有可配置参数，用于批量回测和参数对比。
    """
    
    # ========== 配置名称 ==========
    name: str
    """配置名称，用于标识和报告"""
    
    # ========== 模型选择 ==========
    pricing_model: PricingModel = PricingModel.KALMAN
    """使用的定价模型"""
    
    # ========== 策略配置 ==========
    maker_config: Optional[MakerConfig] = None
    """策略主引擎配置（None 使用默认）"""
    kalman_config: Optional[KalmanConfig] = None
    """Kalman 模型配置（None 使用默认）"""
    ridge_config: Optional[RidgeConfig] = None
    """Ridge 模型配置（None 使用默认）"""
    
    # ========== 回测追踪配置 ==========
    track_duration: float = 10.0
    """追踪每个信号的最大时长（秒）"""
    
    tp_ticks: float = 2.0
    """虚拟止盈点数（单位：tick）"""
    
    sl_ticks: float = -3.0
    """虚拟止损点数（单位：tick，负数）"""
    
    # ========== 其他配置 ==========
    tick_size: float = 0.25
    """最小跳动点数（ES=0.25）"""
    
    def __post_init__(self):
        """配置验证和默认值设置"""
        if self.maker_config is None:
            self.maker_config = MakerConfig()
        if self.kalman_config is None:
            self.kalman_config = KalmanConfig()
        if self.ridge_config is None:
            self.ridge_config = RidgeConfig()
        
        # 验证止损为负数
        if self.sl_ticks > 0:
            self.sl_ticks = -abs(self.sl_ticks)


@dataclass
class BacktestResult:
    """
    单个回测的结果
    
    用于汇总和对比。
    """
    
    config: BacktestConfig
    """使用的配置"""
    
    total_signals: int = 0
    """总信号数"""
    
    tp_count: int = 0
    """止盈单数"""
    sl_count: int = 0
    """止损单数"""
    timeout_count: int = 0
    """超时平仓数"""
    
    avg_pnl: float = 0.0
    """平均每单盈亏（ticks）"""
    
    avg_mfe: float = 0.0
    """平均最大潜盈（ticks）"""
    avg_mae: float = 0.0
    """平均最大潜亏（ticks）"""
    
    mfe_positive_count: int = 0
    """MFE > 0 的信号数"""
    mfe_zero_count: int = 0
    """MFE = 0 的信号数"""
    
    avg_duration: float = 0.0
    """平均追踪时长（秒）"""
    min_duration: float = 0.0
    """最小追踪时长（秒）"""
    max_duration: float = 0.0
    """最大追踪时长（秒）"""
    
    immediate_sl_count: int = 0
    """0.1秒内触发止损的信号数"""
    
    def win_rate(self) -> float:
        """胜率（止盈 / 总信号）"""
        if self.total_signals == 0:
            return 0.0
        return (self.tp_count / self.total_signals) * 100.0
    
    def loss_rate(self) -> float:
        """败率（止损 / 总信号）"""
        if self.total_signals == 0:
            return 0.0
        return (self.sl_count / self.total_signals) * 100.0
    
    def timeout_rate(self) -> float:
        """超时率（超时 / 总信号）"""
        if self.total_signals == 0:
            return 0.0
        return (self.timeout_count / self.total_signals) * 100.0

