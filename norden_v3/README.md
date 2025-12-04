## Norden Engine v3.1 (Institutional Maker) 框架说明

本目录是 **Norden Engine v3.1** 的独立实现框架，和现有 NFQE Lite GUI / 策略解耦。

### 项目概述

Norden Engine v3.1 是一个机构级混合做市策略引擎，专为 CME ES/NQ 期货设计。系统基于 Gary Norden 的"不预测，只反应"交易哲学，通过捕获高频的统计学微小优势（1-3 ticks）实现稳定收益。

### 核心特点

- ✅ **智能定价**：在线卡尔曼滤波 + 岭回归双模型并行
- ✅ **微观分析**：加权 OBI、冰山检测、队列博弈
- ✅ **风险控制**：BTC 熔断、多重过滤、自适应阈值
- ✅ **高性能**：事件驱动架构，实时响应

### 目标

- 按照白皮书实现一个 **只挂单、不追单** 的 Hybrid Maker 引擎。
- 输入是标准化的 Tick / DOM 事件，输出是标准化的下单 / 撤单指令。
- 可以用于：
  - 实盘（接 Rithmic / ATAS / 其他数据源）
  - 回测（从 DuckDB / 其他数据库重放）

### 目录结构

- `__init__.py`  
  导出核心类 `NordenMakerV3` 及常用类型。

- `types.py`  
  定义统一的数据与指令类型：
  - `TickEvent`：多品种价格事件 (ES/NQ/YM/BTC)
  - `DomSnapshot`：DOM 快照 (bids/asks 深度)
  - `OrderCommand`：策略输出的下单 / 撤单指令
  - `Side`, `OrderType`：方向与订单类型枚举

- `kalman_model.py`  
  在线卡尔曼滤波定价模块：
  - 状态向量 `theta = [beta_NQ, beta_YM, alpha]`
  - 观测方程 `ES_t = H_t · theta + v_t`
  - 提供 `OnlineKalman.update(TickEvent)` → `(fair_price, spread)`

- `ridge_model.py`  
  在线岭回归定价模块（备用模型）：
  - 带遗忘因子的岭回归
  - 与 Kalman 并行计算，对比验证

- `obi_calculator.py`  
  加权订单簿失衡度计算：
  - 使用指数衰减权重
  - 高效向量化计算

- `iceberg_detector.py`  
  冰山订单检测：
  - 通过成交量 vs 挂单量差异识别隐藏订单
  - 批量成交聚合，时间衰减机制

- `btc_regime.py`  
  BTC 风险监控（熔断保护）：
  - 相对波动率检测
  - 自动熔断机制

- `maker_engine.py`  
  v3.1 核心引擎：
  - 使用 `OnlineKalman` 计算 ES 公允价值与价差 (Spread)
  - 实现完整的多层过滤体系：
    - Layer 1: Spread 触发层
    - Layer 2: OBI 微观过滤层
    - Layer 3: 冰山过滤 + BTC 风险监控
    - Layer 4: 队列博弈过滤
  - 提供事件入口：
    - `on_tick(TickEvent)` - 处理价格事件
    - `on_dom(DomSnapshot)` - 处理 DOM 更新
  - 提供订单输出：
    - 通过构造时传入的 `order_sink(OrderCommand)` 回调

### 集成方式（示意）

```python
from norden_v3 import NordenMakerV3, TickEvent, DomSnapshot

def send_order_to_executor(cmd):
    # 这里把 OrderCommand 转成你现有的 C#/Rithmic 指令
    print("ORDER:", cmd)

engine = NordenMakerV3(order_sink=send_order_to_executor)

# 在你的事件循环中：
tick = TickEvent(t_ms=..., es=..., nq=..., ym=..., btc=...)
engine.on_tick(tick)

dom = DomSnapshot(t_ms=..., best_bid=..., best_ask=..., bids=[...], asks=[...])
engine.on_dom(dom)
```

### 快速开始

```python
from norden_v3 import NordenMakerV3, TickEvent, DomSnapshot

# 创建策略引擎
def order_handler(cmd):
    print(f"订单: {cmd}")

engine = NordenMakerV3(order_sink=order_handler)

# 处理市场数据
tick = TickEvent(t_ms=123456, es=6857.25, nq=21500.50, ym=44000.00, btc=95000.00)
engine.on_tick(tick)

dom = DomSnapshot(t_ms=123456, best_bid=6857.25, best_ask=6857.50, bids=[...], asks=[...])
engine.on_dom(dom)
```

运行完整测试：
```bash
python run_norden_v3_test.py
```

### 文档说明

- **完整文档**：`项目说明文档.md` - 详细的项目说明和架构介绍
- **技术文档**：
  - `数据归一化说明.md` - Baseline 归一化方法
  - `冰山检测说明.md` - 冰山订单检测原理
  - `BTC风险监控说明.md` - BTC 风险监控算法
- **测试指南**：`TESTING.md`
- **中文 README**：`README_CN.md`

### 后续扩展点

- **完整的 MBO 队列长度计算**  
  当前使用 best level 的聚合成交量近似，后续可接入 MBO 数据实现精确的队列统计

- **止盈止损优化**  
  当前仅实现挂单超时撤单逻辑，后续可接入：
  - 成交回报 → 更新持仓
  - 止盈 / 止损 → 限价出场 / 紧急市价平仓

- **回测框架集成**  
  当前仅支持实盘测试，后续可集成历史数据回测功能

---

**版本**：v3.1  
**最后更新**：2025-01-19


