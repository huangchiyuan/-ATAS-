# Norden Engine v3.1 - 机构级做市策略引擎

## 🎯 项目简介

**Norden Engine v3.1** 是一个专为 CME ES/NQ 期货设计的高频做市策略引擎，基于 Gary Norden 的交易哲学，通过捕获微小价差（1-3 ticks）实现稳定收益。

## ✨ 核心特性

- 🔬 **智能定价**：在线卡尔曼滤波 + 岭回归双模型并行
- 📊 **微观分析**：加权 OBI、冰山检测、队列博弈
- 🛡️ **风险控制**：BTC 熔断、多重过滤、自适应阈值
- ⚡ **高性能**：事件驱动架构，实时响应

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install numpy
```

### 2. 基本使用

```python
from norden_v3 import NordenMakerV3, TickEvent, DomSnapshot

# 创建策略引擎
engine = NordenMakerV3(order_sink=lambda cmd: print(f"订单: {cmd}"))

# 处理市场数据
tick = TickEvent(t_ms=123456, es=6857.25, nq=21500.50, ym=44000.00)
engine.on_tick(tick)

dom = DomSnapshot(t_ms=123456, best_bid=6857.25, best_ask=6857.50, ...)
engine.on_dom(dom)
```

### 3. 运行测试

```bash
python run_norden_v3_test.py
```

## 📦 核心模块

| 模块 | 功能 |
|------|------|
| `kalman_model.py` | 在线卡尔曼滤波定价引擎 |
| `ridge_model.py` | 在线岭回归备用模型 |
| `obi_calculator.py` | 加权订单簿失衡度计算 |
| `iceberg_detector.py` | 冰山订单检测 |
| `btc_regime.py` | BTC 风险监控（熔断保护） |
| `maker_engine.py` | 策略主引擎 |

## 📖 详细文档

- **完整文档**：`项目说明文档.md`
- **数据归一化**：`数据归一化说明.md`
- **冰山检测**：`冰山检测说明.md`
- **BTC 风险监控**：`BTC风险监控说明.md`
- **测试指南**：`TESTING.md`

## 🏗️ 系统架构

```
UDP 数据流 → 策略引擎 → 多层过滤 → 交易信号
                ↓
        定价模型 + 微观因子 + 风险控制
```

## 📊 决策流程

1. **定价模型**：计算 ES 公允价（基于 NQ/YM）
2. **Spread 触发**：判断价差是否超过阈值
3. **多重过滤**：BTC 风险、冰山、OBI 检查
4. **队列博弈**：估计排队成本
5. **执行决策**：生成订单指令

## ⚙️ 配置示例

```python
from norden_v3 import MakerConfig

config = MakerConfig(
    base_spread_threshold=0.5,  # 0.5 tick 触发
    min_obi_for_long=0.1,       # 做多最小 OBI
    obi_depth=10,               # OBI 计算深度
    max_queue_size=300,         # 最大队列长度
)

engine = NordenMakerV3(maker_cfg=config)
```

## 📈 输出示例

```
[STATUS] ES=6857.25 | Fair_KF= 6857.47 Spread_KF= +0.87tick | 
         Fair_RD= 6857.45 Spread_RD= +0.80tick | OBI=-0.034 | 
         Queue: B=74 A=77 | 🟢 BTC:1.2x | Iceberg: None | Order: None
```

## 🔧 技术特点

- ✅ **数据归一化**：Baseline 扣除，解决数量级差异
- ✅ **矩阵优化**：针对性的矩阵缩放，提高数值稳定性
- ✅ **高效计算**：向量化操作，降频采样
- ✅ **模块化设计**：易于扩展和维护

## 📚 相关文档

- 项目文件清单：`../项目文件清单.md`
- 项目文件说明：`../项目文件说明.md`

---

**版本**：v3.1  
**更新日期**：2025-01-19

