"""
BTC 体制过滤器 (BTC Regime Filter)
==================================

核心目的：检测市场是否处于"地震"时刻（极端风险），如数据发布、重大新闻、币圈崩盘等。
当检测到极端波动时，强制空仓，避免相关性模型失效导致的损失。

核心逻辑：
    1. 计算相对波动率：当前 1分钟波动率 / 过去 10分钟平均波动率
    2. 如果比率 > 3.0（当前波动是平时的 3 倍），判定为"不安全"
    3. 使用对数收益率计算波动率，更符合金融理论
    4. 每秒采样一次，降低计算成本

应用场景：
    - Layer 3 风控：在生成交易信号前检查市场状态
    - 熔断机制：极端波动时自动停止交易
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional
import time
import numpy as np


@dataclass
class BTCRegimeConfig:
    """BTC 体制监控配置."""

    # 短期窗口：60秒（1分钟）
    short_window_seconds: int = 60

    # 长期窗口：600秒（10分钟）
    long_window_seconds: int = 600

    # 报警阈值：当前波动是平时的倍数
    alert_threshold: float = 3.0

    # 采样频率：每秒采样一次（1Hz）
    sample_interval_seconds: float = 1.0


class BTCRegimeMonitor:
    """
    BTC 市场体制监控器.

    使用相对波动率检测极端市场状态：
        - 绿灯 (Safe): 正常波动，可以交易
        - 红灯 (Unsafe): 极端波动，强制空仓

    使用方式:
        monitor = BTCRegimeMonitor()
        
        # 每次收到 BTC tick 时调用
        monitor.on_tick(btc_price)
        
        # 在策略中检查市场状态
        if monitor.check_safety():
            # 可以交易
        else:
            # 强制空仓
    """

    def __init__(self, cfg: Optional[BTCRegimeConfig] = None):
        self.cfg = cfg or BTCRegimeConfig()

        # 数据容器：存储最近 long_window_seconds 个秒级价格快照
        self.price_history: deque[float] = deque(
            maxlen=self.cfg.long_window_seconds
        )

        # 状态
        self.last_sample_time: float = 0.0
        self.is_market_safe: bool = True  # 默认安全
        self.current_vol_ratio: float = 1.0

        # 统计信息
        self.last_short_vol: float = 0.0
        self.last_baseline_vol: float = 0.0

    def reset(self) -> None:
        """重置监控器状态."""
        self.price_history.clear()
        self.last_sample_time = 0.0
        self.is_market_safe = True
        self.current_vol_ratio = 1.0
        self.last_short_vol = 0.0
        self.last_baseline_vol = 0.0

    def on_tick(self, btc_price: float) -> None:
        """
        在接收到 BTC Tick 时调用.

        注意：内部会降频采样，每秒只记录一次价格，避免计算过载。

        Args:
            btc_price: BTC 当前价格
        """
        if btc_price is None or btc_price <= 0:
            return

        now = time.time()

        # 降频采样：每秒只记录一次价格（1Hz）
        # 避免 Tick 太多导致计算过载，且秒级波动率更稳定
        if now - self.last_sample_time >= self.cfg.sample_interval_seconds:
            self._update_sample(btc_price)
            self.last_sample_time = now

    def _update_sample(self, price: float) -> None:
        """
        执行每秒一次的核心计算：更新波动率比率。

        Args:
            price: 当前 BTC 价格
        """
        self.price_history.append(price)

        # 数据还不够填满短期窗口时，暂时认为安全
        if len(self.price_history) < self.cfg.short_window_seconds:
            self.is_market_safe = True
            self.current_vol_ratio = 1.0
            return

        # 转换成 numpy 数组进行计算
        prices = np.array(self.price_history)

        # 计算对数收益率 (Log Returns)
        # 波动率通常是对收益率求标准差，而不是对价格求标准差
        # returns = ln(P_t / P_{t-1})
        if len(prices) < 2:
            return

        returns = np.diff(np.log(prices))

        if len(returns) == 0:
            return

        # 计算波动率
        # A. 短期波动率（最近 short_window_seconds 个样本）
        short_returns = returns[-self.cfg.short_window_seconds :]
        current_vol = np.std(short_returns, ddof=1)  # 使用样本标准差

        # B. 长期基准波动率（所有 long_window_seconds 个样本）
        baseline_vol = np.std(returns, ddof=1)

        # 防止除零
        if baseline_vol == 0 or np.isnan(baseline_vol):
            baseline_vol = 1e-9

        if current_vol == 0 or np.isnan(current_vol):
            current_vol = 0.0

        # 保存统计信息
        self.last_short_vol = current_vol
        self.last_baseline_vol = baseline_vol

        # 计算比率
        self.current_vol_ratio = current_vol / baseline_vol

        # 判定体制
        if self.current_vol_ratio > self.cfg.alert_threshold:
            self.is_market_safe = False  # 🔴 危险！波动率爆表
        else:
            self.is_market_safe = True  # 🟢 安全

    def check_safety(self) -> bool:
        """
        检查市场是否安全（给主策略调用的接口）.

        Returns:
            True: 市场安全，可以交易
            False: 市场极端波动，应该强制空仓
        """
        return self.is_market_safe

    def get_vol_ratio(self) -> float:
        """获取当前波动率比率（用于调试/监控）."""
        return self.current_vol_ratio

    def get_stats(self) -> dict:
        """
        获取统计信息（用于调试/可视化）.

        Returns:
            {
                'is_safe': bool,
                'vol_ratio': float,
                'short_vol': float,
                'baseline_vol': float,
                'samples_count': int,
            }
        """
        return {
            "is_safe": self.is_market_safe,
            "vol_ratio": self.current_vol_ratio,
            "short_vol": self.last_short_vol,
            "baseline_vol": self.last_baseline_vol,
            "samples_count": len(self.price_history),
        }

