"""
在线卡尔曼滤波定价引擎 (v3.1 实盘修正版)
======================

修正点：
1. 参数去敏: 增大 R, 减小 Q，防止过度拟合导致 Spread 为 0。
2. 热启动: 给定 Beta 的先验估计，避免 YM 权重长期为 0。
3. 数据鲁棒: 使用最近一次有效价格，避免数据丢包时用 0 污染模型。
"""

from __future__ import annotations

from typing import Tuple, Optional

import numpy as np

from .types import TickEvent
from .config import KalmanConfig


class OnlineKalman:
    """
    在线卡尔曼滤波器（用于 ES 公允价值定价）.

    接口：
        - update(event: TickEvent) -> (fair_price, spread)
            fair_price: 当前 tick 的理论 ES 价格
            spread    : fair_price - ES_actual
        - reset()
    """

    def __init__(self, cfg: Optional[KalmanConfig] = None):
        self.cfg = cfg or KalmanConfig()

        # ---- 修正 2：热启动 (Warm Start) ----
        # 经验关系：ES ≈ 0.3 * NQ + 0.05 * YM + alpha
        # 给出一个数量级正确的先验，避免一开始误差极大导致参数不稳定。
        self.theta = np.array([0.30, 0.05, 0.0], dtype=float)

        # ---- 修正 4：矩阵缩放 (Matrix Scaling) ----
        # 由于 NQ/YM 价格在 2~4 万点量级，Beta 的微小波动会被放大为巨大的价格波动。
        # 若直接用 init_P 作为 Beta 的初始方差，会导致 H·P·H^T 的量级过大，
        # 进而让观测噪声 R 显得微不足道，模型会“跪舔”实际价格 → Spread → 0。
        #
        # 解决办法：给 Beta 极小的初始方差，给 Alpha 较大的方差。
        #   Var(beta_NQ) ≈ Var(beta_YM) ≈ 1e-8
        #   Var(alpha)  ≈ 100.0
        self.P = np.diag([1e-8, 1e-8, self.cfg.init_P]).astype(float)

        # 过程噪声 Q, 观测噪声 R
        self.Q = np.diag(
            [self.cfg.q_beta, self.cfg.q_beta, self.cfg.q_alpha]
        ).astype(float)
        self.R = float(self.cfg.r_obs)

        # 最近一次 fair price（绝对价格坐标）
        self.last_fair: Optional[float] = None

        # ---- 修正 3：数据缓存（防止 None → 0 污染） ----
        self.last_nq: Optional[float] = None
        self.last_ym: Optional[float] = None

        # ---- 基准价格（Baseline，用于去中心化）----
        # 我们只关心“相对涨跌多少点”，而不是绝对价位是多少。
        self.base_es: Optional[float] = None
        self.base_nq: Optional[float] = None
        self.base_ym: Optional[float] = None

    def reset(self) -> None:
        """重置状态，保留热启动先验."""
        self.theta = np.array([0.30, 0.05, 0.0], dtype=float)
        self.P[:, :] = np.eye(3, dtype=float) * self.cfg.init_P
        self.last_fair = None
        self.last_nq = None
        self.last_ym = None
        self.base_es = None
        self.base_nq = None
        self.base_ym = None

    def update(self, tick: TickEvent) -> Tuple[Optional[float], Optional[float]]:
        """
        使用最新 Tick 更新状态，返回 (ES_fair_abs, spread).

        归一化与坐标系：
            - 内部在“去中心化坐标”上做回归:
                x_NQ = NQ - NQ_base
                x_YM = YM - YM_base
                y_ES = ES - ES_base
            - fair_delta = H · theta  是“相对 ES_base 的变动量”
            - fair_abs   = fair_delta + ES_base 才是最终返回的公允价

        spread 定义（不受基准影响）：
            spread = fair_abs - ES_actual = fair_delta - y_ES
        """
        # --- 数据清洗与缓存：使用“最近一次有效价格” ---
        nq_raw = float(tick.nq) if tick.nq is not None else self.last_nq
        ym_raw = float(tick.ym) if tick.ym is not None else self.last_ym
        es_raw = float(tick.es) if tick.es is not None else None

        if tick.nq is not None:
            self.last_nq = float(tick.nq)
        if tick.ym is not None:
            self.last_ym = float(tick.ym)

        # 若还没有任何历史数据，无法计算
        if nq_raw is None or ym_raw is None or es_raw is None:
            return self.last_fair, None

        # --- 基准价格初始化：使用收到的第一笔完整 Tick 作为 baseline ---
        if self.base_es is None or self.base_nq is None or self.base_ym is None:
            self.base_es = es_raw
            self.base_nq = nq_raw
            self.base_ym = ym_raw
            # 第一笔仅用来设定基准，不产出有效 spread 信号
            self.last_fair = es_raw
            return es_raw, 0.0

        # 去中心化：只关心“相对涨跌多少点”
        x_nq = nq_raw - self.base_nq
        x_ym = ym_raw - self.base_ym
        y_es = es_raw - self.base_es

        # H_t = [x_NQ_t, x_YM_t, 1]
        H = np.array([x_nq, x_ym, 1.0], dtype=float)
        y = y_es

        # ---- Step 1: 预测（使用旧参数计算当前“相对”理论价，用于信号） ----
        fair_delta = float(H.dot(self.theta))  # 相对 ES_base 的变动
        spread_for_signal = fair_delta - y     # = fair_abs - es_raw

        # ---- Step 2: 更新（将本次误差“学习”进 theta，用于下一 tick） ----
        PHt = self.P.dot(H)
        S = float(H.dot(PHt) + self.R)

        if S > 1e-12:
            K = PHt / S

            # 误差 = 实际 - 预测（注意与 spread 符号相反）
            error = y - fair_delta
            self.theta += K * error

            # 协方差更新：P = (I - K H) P + Q
            KH = np.outer(K, H)
            I = np.eye(3, dtype=float)
            self.P = (I - KH).dot(self.P) + self.Q

        # 还原到绝对价格坐标
        fair_abs = fair_delta + self.base_es
        self.last_fair = fair_abs
        return fair_abs, spread_for_signal




