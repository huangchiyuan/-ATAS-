"""
Norden Engine v3.1 配置使用示例
================================

本文件展示如何使用集中配置文件自定义策略参数。
"""

from norden_v3 import (
    NordenMakerV3,
    MakerConfig,
    KalmanConfig,
    RidgeConfig,
    OBIConfig,
    IcebergConfig,
    BTCRegimeConfig,
    PresetConfigs,
)


def example_1_default_config():
    """示例 1：使用默认配置."""
    # 所有参数使用默认值
    engine = NordenMakerV3()
    return engine


def example_2_custom_maker_config():
    """示例 2：自定义策略配置."""
    # 创建自定义策略配置
    maker_cfg = MakerConfig(
        base_spread_threshold=0.8,    # 0.8 tick 触发
        min_obi_for_long=0.15,        # 更严格的做多条件
        min_obi_for_short=0.15,       # 更严格的做空条件
        obi_depth=12,                 # 看更多档位
        max_queue_size=250,           # 中等队列限制
        max_wait_seconds=2.5,         # 稍短的等待时间
        take_profit_ticks=5.0,        # 更大止盈
        hard_stop_ticks=8.0,          # 更大止损
    )

    engine = NordenMakerV3(maker_cfg=maker_cfg)
    return engine


def example_3_custom_kalman_config():
    """示例 3：自定义 Kalman 模型配置."""
    # 自定义 Kalman 配置
    kalman_cfg = KalmanConfig(
        init_P=150.0,      # 更大的初始不确定性
        q_beta=1e-13,      # Beta 更稳定
        q_alpha=1e-7,      # Alpha 稍微保守
        r_obs=150.0,       # 更强的 Spread（更不信任实际价格）
    )

    engine = NordenMakerV3(kalman_cfg=kalman_cfg)
    return engine


def example_4_preset_configs():
    """示例 4：使用预设配置模板."""
    # 使用保守配置
    conservative_cfg = PresetConfigs.conservative()
    engine_conservative = NordenMakerV3(maker_cfg=conservative_cfg)

    # 使用激进配置
    aggressive_cfg = PresetConfigs.aggressive()
    engine_aggressive = NordenMakerV3(maker_cfg=aggressive_cfg)

    # 使用平衡配置（默认）
    balanced_cfg = PresetConfigs.balanced()
    engine_balanced = NordenMakerV3(maker_cfg=balanced_cfg)

    return engine_conservative, engine_aggressive, engine_balanced


def example_5_full_custom():
    """示例 5：完整自定义配置（所有参数）."""
    # 策略配置
    maker_cfg = MakerConfig(
        base_spread_threshold=0.6,
        min_obi_for_long=0.12,
        min_obi_for_short=0.12,
        max_queue_size=280,
        take_profit_ticks=4.5,
        hard_stop_ticks=7.0,
    )

    # Kalman 配置
    kalman_cfg = KalmanConfig(
        r_obs=120.0,
        q_beta=1e-12,
    )

    engine = NordenMakerV3(
        maker_cfg=maker_cfg,
        kalman_cfg=kalman_cfg,
    )

    return engine


def example_6_config_from_dict():
    """示例 6：从字典创建配置（方便从文件加载）."""
    # 假设从配置文件或数据库加载
    config_dict = {
        "base_spread_threshold": 0.7,
        "min_obi_for_long": 0.1,
        "min_obi_for_short": 0.1,
        "max_queue_size": 300,
        "take_profit_ticks": 4.0,
        "hard_stop_ticks": 6.0,
    }

    # 使用字典更新配置
    maker_cfg = MakerConfig(**config_dict)
    engine = NordenMakerV3(maker_cfg=maker_cfg)

    return engine


if __name__ == "__main__":
    print("Norden Engine v3.1 配置使用示例")
    print("=" * 50)
    print("\n所有配置类已集中到 norden_v3/config.py")
    print("详细参数说明请参考：norden_v3/配置参数说明.md")
    print("\n运行示例：")
    print("  engine = example_1_default_config()")

