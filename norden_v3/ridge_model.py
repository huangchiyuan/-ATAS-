"""
Online Ridge Regression (在线岭回归) - HFT 定价引擎
=================================================
核心优势:
1. 抗共线性: 解决 NQ 和 YM 同涨同跌导致的参数互斥爆炸问题。
2. 强制 Spread: 通过 L2 惩罚，防止模型过度拟合价格，从而保留交易信号。
3. 自动归一化: 内部处理去中心化，无需外部预处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, Optional

import numpy as np

from .types import TickEvent


@dataclass
class RidgeConfig:
    """
    岭回归参数配置
    """

    # 1. 遗忘因子 (Forgetting Factor): 决定记忆长度
    # 0.999 ~= 过去 1000 tick (稳健)
    # 0.99  ~= 过去 100 tick (灵敏)
    lambda_factor: float = 0.995

    # 2. 岭惩罚系数 (Ridge Penalty / Alpha): 决定模型有多"硬"
    # 这是产生 Spread 的核心！
    # 0.0      = 普通 RLS (Spread 容易被吃光)
    # 1e-4     = 标准惩罚 (推荐)
    # 1e-3     = 强力惩罚 (Spread 会很大，但可能滞后)
    ridge_alpha: float = 1e-4

    # 3. 初始不确定性 (Initial Covariance)
    init_P: float = 100.0


class OnlineRidge:
    """
    在线岭回归定价模型.

    用法:
        model = OnlineRidge()
        fair, spread = model.update(tick_event)
    """

    def __init__(self, cfg: Optional[RidgeConfig] = None):
        self.cfg = cfg or RidgeConfig()

        # 状态向量 Theta: [Beta_NQ, Beta_YM, Alpha(截距)]
        # 热启动: 给一个合理的初值，防止冷启动时 Spread 暴跳
        # ES ≈ 0.3 * NQ + 0.05 * YM (经验值)
        self.theta = np.array([0.30, 0.05, 0.0], dtype=float)

        # 协方差矩阵 P（这里使用 RLS 风格的“信息矩阵”）
        self.P = np.eye(3, dtype=float) * self.cfg.init_P

        # 基准价格缓存 (用于去中心化/归一化)
        # 岭回归对数值大小非常敏感，必须减去基准价才能工作！
        self.base_prices = {"nq": None, "ym": None, "es": None}

        # 数据缓存 (处理数据包不同步)
        self.last_nq: Optional[float] = None
        self.last_ym: Optional[float] = None
        self.last_fair: Optional[float] = None

    def reset(self) -> None:
        """重置模型状态."""
        self.theta = np.array([0.30, 0.05, 0.0], dtype=float)
        self.P = np.eye(3, dtype=float) * self.cfg.init_P
        self.base_prices = {"nq": None, "ym": None, "es": None}
        self.last_nq = None
        self.last_ym = None
        self.last_fair = None

    def update(self, tick: TickEvent) -> Tuple[Optional[float], Optional[float]]:
        """
        更新模型并返回 (Fair_Price, Spread).

        Spread 定义:
            Spread = Fair_Price - ES_actual
            > 0 → ES 被低估 → 倾向做多
            < 0 → ES 被高估 → 倾向做空
        """
        # --- 1. 数据清洗与对齐 ---
        nq = float(tick.nq) if tick.nq is not None else self.last_nq
        ym = float(tick.ym) if tick.ym is not None else self.last_ym
        es = float(tick.es) if tick.es is not None else None

        # 更新缓存
        if tick.nq is not None:
            self.last_nq = nq
        if tick.ym is not None:
            self.last_ym = ym

        # 数据不全，跳过
        if nq is None or ym is None or es is None:
            return self.last_fair, None

        # --- 2. 自动归一化 (Auto-Centering) ---
        # 如果是第一帧数据，将其设为基准点 (Base)
        if self.base_prices["es"] is None:
            self.base_prices["nq"] = nq
            self.base_prices["ym"] = ym
            self.base_prices["es"] = es
            # 刚启动没有 Spread
            self.last_fair = es
            return es, 0.0

        # 计算相对涨跌幅 (Delta)
        # 这样输入数据就在 0 附近波动，数量级很小，适合岭回归
        x_nq = nq - self.base_prices["nq"]  # type: ignore[operator]
        x_ym = ym - self.base_prices["ym"]  # type: ignore[operator]
        y_es = es - self.base_prices["es"]  # type: ignore[operator]

        # 构造特征向量 x = [NQ_delta, YM_delta, 1.0]
        x = np.array([x_nq, x_ym, 1.0], dtype=float)

        # --- 3. 预测 (Predict) ---
        # y_pred 是相对于基准价的预测涨跌幅
        y_pred_delta = float(x.dot(self.theta))

        # 还原为绝对价格
        fair_price = y_pred_delta + float(self.base_prices["es"])

        # 计算 Spread (信号来源)
        # Spread = 理论 - 实际
        spread = fair_price - es

        # --- 4. RLS 核心更新 (带遗忘因子) ---
        # Px = P * x
        Px = self.P.dot(x)

        # 分母 g = λ + x^T * P * x
        g = self.cfg.lambda_factor + float(x.dot(Px))

        # 增益向量 k = Px / g
        k = Px / g

        # 更新 P 矩阵:
        # P_new = (P - k * x^T * P) / λ
        self.P = (self.P - np.outer(k, Px)) / self.cfg.lambda_factor

        # --- 5. 权重更新 (带岭惩罚 Ridge Penalty) ---
        # 预测误差 (a priori error)
        error = y_es - y_pred_delta

        # A. 正常学习步骤
        self.theta += k * error

        # B. 岭回归收缩步骤 (Weight Decay):
        if self.cfg.ridge_alpha > 0.0:
            # 通常不对截距项(theta[2])做强力衰减，只衰减 Beta
            decay_vector = np.array(
                [1.0 - self.cfg.ridge_alpha, 1.0 - self.cfg.ridge_alpha, 1.0],
                dtype=float,
            )
            self.theta *= decay_vector

        self.last_fair = fair_price
        return fair_price, spread


