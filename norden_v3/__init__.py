"""
Norden Engine v3.1 - Institutional Maker

本目录用于承载全新的 v3.1 策略实现，与现有 NFQE Lite GUI/策略解耦：

- 不直接依赖 PyQt，可在回测和实盘环境中复用
- 输入：标准化的 Tick / DOM / 执行回报 事件
- 输出：标准化的 下单 / 撤单 / 改单 指令

集成方式（示意）：
    - 实盘：从 UDP / Rithmic API 接收事件 → 转换为本目录定义的事件结构 → 调用 NordenMakerV3.on_event(...)
    - 回测：从 DuckDB 读取历史数据 → 逐条喂给 NordenMakerV3
"""

from .maker_engine import NordenMakerV3, MakerConfig
from .types import (
    Side,
    OrderType,
    OrderCommand,
    TickEvent,
    DomSnapshot,
)
from .kalman_model import OnlineKalman, KalmanConfig
from .ridge_model import OnlineRidge, RidgeConfig
from .obi_calculator import OBICalculator, OBIConfig, calculate_simple_obi
from .iceberg_detector import IcebergDetector, IcebergConfig
from .btc_regime import BTCRegimeMonitor, BTCRegimeConfig

__all__ = [
    "NordenMakerV3",
    "MakerConfig",
    "Side",
    "OrderType",
    "OrderCommand",
    "TickEvent",
    "DomSnapshot",
    "OnlineKalman",
    "KalmanConfig",
    "OnlineRidge",
    "RidgeConfig",
    "OBICalculator",
    "OBIConfig",
    "calculate_simple_obi",
    "IcebergDetector",
    "IcebergConfig",
    "BTCRegimeMonitor",
    "BTCRegimeConfig",
]



