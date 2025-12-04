"""
OBI (Order Book Imbalance) 订单簿失衡计算模块
=============================================

核心功能：
    - 计算买卖双方挂单的"兵力对比"
    - 使用加权算法，重点关注前几档（Level 1-3）
    - 高性能实现（预计算权重，向量化计算）

在策略中的作用：
    - Layer 2 微观因子：验证 Kalman 信号是否与盘口资金流一致
    - 做多信号需要 OBI > 0.1（买方力量占优，阻力更小）
    - 做空信号需要 OBI < -0.1（卖方力量占优，支撑更弱）

数学公式：
    OBI = (Σ V_bid_i * w_i - Σ V_ask_i * w_i) / (Σ V_bid_i * w_i + Σ V_ask_i * w_i)
    w_i = exp(-decay * (i - 1))  # 指数衰减权重

结果范围：[-1, +1]
    - +1: 只有买单（涨停板）
    - -1: 只有卖单（跌停板）
    - 0: 买卖完全平衡
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple, Optional
import numpy as np

from .types import DomSnapshot


@dataclass
class OBIConfig:
    """OBI 计算配置."""

    # 计算深度：使用前几档
    # Level 1-3 最重要（真实挂单），Level 10+ 很多是假单
    depth: int = 10

    # 权重衰减系数：decay 越大，权重衰减越快
    # decay = 0.5  →  Level 1=100%, Level 2=60%, Level 3=36%
    # decay = 1.0  →  Level 1=100%, Level 2=37%, Level 3=14% (更激进)
    decay: float = 0.5

    # 是否使用自适应深度（根据实际可用档位数自动调整）
    auto_adjust_depth: bool = True


class OBICalculator:
    """
    订单簿失衡计算器（高性能版本）.

    使用方式:
        calculator = OBICalculator(OBIConfig(depth=10, decay=0.5))
        obi = calculator.calculate(dom_snapshot)
    """

    def __init__(self, cfg: Optional[OBIConfig] = None):
        self.cfg = cfg or OBIConfig()

        # 预计算权重向量（避免盘中重复计算）
        # weights[i] = exp(-decay * i), i 从 0 开始
        # 对应 Level 1, Level 2, ..., Level N
        max_depth = self.cfg.depth
        self.weights = np.exp(-self.cfg.decay * np.arange(max_depth, dtype=float))

    def calculate(self, dom: DomSnapshot) -> float:
        """
        计算加权 OBI.

        Args:
            dom: DOM 快照

        Returns:
            OBI 值，范围 [-1, +1]
                > 0: 买方占优（支撑强，阻力弱）
                < 0: 卖方占优（阻力强，支撑弱）
                ≈ 0: 双方平衡
        """
        # 确定实际计算深度
        if self.cfg.auto_adjust_depth:
            depth = min(
                self.cfg.depth,
                len(dom.bids),
                len(dom.asks),
            )
        else:
            depth = self.cfg.depth

        if depth <= 0:
            return 0.0

        # 提取前 depth 档的成交量
        # dom.bids/asks 格式: [(price, volume), ...]
        bid_vols = np.array(
            [max(v, 0.0) for _, v in dom.bids[:depth]], dtype=float
        )
        ask_vols = np.array(
            [max(v, 0.0) for _, v in dom.asks[:depth]], dtype=float
        )

        # 使用预计算的权重（只取前 depth 个）
        w = self.weights[:depth]

        # 向量化计算加权体积（极速）
        weighted_bid_vol = float(bid_vols.dot(w))
        weighted_ask_vol = float(ask_vols.dot(w))

        total_weighted_vol = weighted_bid_vol + weighted_ask_vol

        # 防止除零（极罕见，但必须保护）
        if total_weighted_vol <= 0.0:
            return 0.0

        # 计算失衡度
        obi = (weighted_bid_vol - weighted_ask_vol) / total_weighted_vol

        return float(obi)

    def calculate_detailed(
        self, dom: DomSnapshot
    ) -> Tuple[float, float, float, float]:
        """
        计算 OBI 并返回详细分解信息（用于调试/分析）.

        Returns:
            (obi, weighted_bid_vol, weighted_ask_vol, total_weighted_vol)
        """
        depth = min(
            self.cfg.depth,
            len(dom.bids),
            len(dom.asks),
        )

        if depth <= 0:
            return 0.0, 0.0, 0.0, 0.0

        bid_vols = np.array(
            [max(v, 0.0) for _, v in dom.bids[:depth]], dtype=float
        )
        ask_vols = np.array(
            [max(v, 0.0) for _, v in dom.asks[:depth]], dtype=float
        )

        w = self.weights[:depth]
        weighted_bid_vol = float(bid_vols.dot(w))
        weighted_ask_vol = float(ask_vols.dot(w))
        total_weighted_vol = weighted_bid_vol + weighted_ask_vol

        if total_weighted_vol <= 0.0:
            return 0.0, 0.0, 0.0, 0.0

        obi = (weighted_bid_vol - weighted_ask_vol) / total_weighted_vol

        return (
            float(obi),
            weighted_bid_vol,
            weighted_ask_vol,
            total_weighted_vol,
        )


def calculate_simple_obi(bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> float:
    """
    简化版 OBI 计算（无加权，所有档位权重相等）.

    适用于快速测试或数据深度不够的场景.

    Args:
        bids: [(price, volume), ...]
        asks: [(price, volume), ...]

    Returns:
        OBI 值，范围 [-1, +1]
    """
    total_bid_vol = sum(max(v, 0.0) for _, v in bids)
    total_ask_vol = sum(max(v, 0.0) for _, v in asks)

    total_vol = total_bid_vol + total_ask_vol
    if total_vol <= 0.0:
        return 0.0

    return (total_bid_vol - total_ask_vol) / total_vol


# ========== 使用示例 ==========

if __name__ == "__main__":
    # 示例：测试 OBI 计算

    from .types import DomSnapshot

    # 模拟一个 DOM 快照
    # 买方较强：Level 1 有 500 手，Level 2 有 400 手
    # 卖方较弱：Level 1 只有 100 手
    test_dom = DomSnapshot(
        t_ms=1000,
        best_bid=6800.0,
        best_ask=6800.25,
        bids=[
            (6800.0, 500),
            (6799.75, 400),
            (6799.50, 300),
            (6799.25, 200),
            (6799.0, 100),
        ],
        asks=[
            (6800.25, 100),
            (6800.50, 100),
            (6800.75, 100),
            (6801.0, 100),
            (6801.25, 100),
        ],
    )

    # 使用加权 OBI 计算器
    calculator = OBICalculator(OBIConfig(depth=10, decay=0.5))
    obi = calculator.calculate(test_dom)

    print(f"OBI: {obi:.4f}")
    print(f"解释: {'买方占优，支撑强' if obi > 0.1 else '卖方占优或平衡'}")

    # 详细分解
    obi_detail, bid_w, ask_w, total_w = calculator.calculate_detailed(test_dom)
    print(f"\n详细分解:")
    print(f"  加权买量: {bid_w:.2f}")
    print(f"  加权卖量: {ask_w:.2f}")
    print(f"  总加权量: {total_w:.2f}")
    print(f"  OBI: {obi_detail:.4f}")

    # 简化版（无加权）
    simple_obi = calculate_simple_obi(test_dom.bids, test_dom.asks)
    print(f"\n简化版 OBI (无加权): {simple_obi:.4f}")

