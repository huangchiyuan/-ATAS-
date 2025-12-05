# Norden Flow Quant Engine (NFQE) Lite

> 一个专为 CME ES/NQ 期货设计的机构级高频做市策略系统  
> 采用 C# + Python 混合架构，结合 ATAS 平台实时数据优势和 Python 算法灵活性

---

## 📋 目录

- [项目概述](#-项目概述)
- [系统架构](#-系统架构)
- [C# 组件说明](#-c-组件说明)
- [Python 组件说明](#-python-组件说明)
- [数据流详解](#-数据流详解)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [输出示例](#-输出示例)
- [文档索引](#-文档索引)
- [技术特点](#-技术特点)
- [系统优势](#-系统优势)
- [开发与测试](#-开发与测试)
- [技术支持](#-技术支持)
- [版本历史](#-版本历史)

---

## 📋 项目概述

**Norden Flow Quant Engine (NFQE) Lite** 是一个专为 CME ES/NQ 期货设计的、基于微观结构分析的半自动化交易系统。该系统采用 **C# + Python 混合架构**，结合 ATAS 平台的实时数据优势和 Python 的算法灵活性，实现高频做市策略。

### 核心哲学

系统严格遵循 Gary Norden 的 **"不预测，只反应"** 交易哲学，放弃传统的基于 K 线图的预测方法。核心目标是通过捕获高频的、统计学上的微小优势（1-3 ticks），利用 **频率复合边际 (Edge × Frequency)** 效应积累稳定收益。

---

## 🏗️ 系统架构

### 整体架构图

```
┌──────────────────────────────────────────────────────────────┐
│                  ATAS 交易平台 (C#)                           │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌───────────────┐              ┌───────────────┐           │
│  │ NQ/YM 图表    │              │   ES 图表     │           │
│  │               │              │               │           │
│  │ NFQE_Bridge   │              │ NFQE_Commander│           │
│  │  (数据发送)    │              │  (策略执行)    │           │
│  └───────┬───────┘              └───────┬───────┘           │
│          │                               │                   │
│          │ UDP:5555 (数据)              │ UDP:6666 (指令)   │
└──────────┼───────────────────────────────┼───────────────────┘
           │                               │
           ↓                               ↑
┌──────────┴───────────────────────────────┴───────────────────┐
│             Python 策略层 (Brain)                             │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  UDP 数据接收 (dom_data_feed.py)                    │     │
│  │    • 接收 Tick / DOM 数据                           │     │
│  │    • 事件队列管理                                   │     │
│  └──────────────────┬──────────────────────────────────┘     │
│                     │                                        │
│                     ↓                                        │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  Norden Engine v3.1 策略引擎                        │     │
│  │    • 定价模型（Kalman / Ridge）                      │     │
│  │    • 微观因子（OBI / 冰山检测）                      │     │
│  │    • 风险控制（BTC 熔断）                            │     │
│  └──────────────────┬──────────────────────────────────┘     │
│                     │                                        │
│                     ↓                                        │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  交易信号生成 → UDP 指令发送                        │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  可选：GUI 可视化 (pyqt_dom_viewer.py)                        │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### 核心组件

| 组件 | 语言 | 位置 | 功能 |
|------|------|------|------|
| **NFQE_Bridge_UDP** | C# | `NQ-YM指标（发送数据）/Class1.cs` | 数据发送端，从 ATAS 获取实时数据 |
| **NFQE_Commander** | C# | `ES策略/Class1.cs` | 策略执行端，接收指令并下单 |
| **UdpListener** | Python | `dom_data_feed.py` | 数据接收端，接收 UDP 数据流 |
| **NordenMakerV3** | Python | `norden_v3/maker_engine.py` | 策略主引擎，执行交易决策 |

---

## 📡 C# 组件说明

### 1. NFQE_Bridge_UDP (数据发送端)

**位置**：`NQ-YM指标（发送数据）/Class1.cs`

**功能**：
- 从 ATAS 平台订阅实时市场数据（ES/NQ/YM/BTC）
- 通过 UDP 协议发送数据到 Python 端
- 智能缓冲：Tick 数据立即发送，DOM 数据批量发送

**配置参数**：

```csharp
PythonIP = "127.0.0.1"      // Python 监听地址
PythonPort = 5555            // UDP 端口
DepthLevels = 15             // DOM 深度（实际发送全部可见档位）
```

**发送的数据类型**：

1. **成交数据 (Trade/Tick)**
   - **格式**：`T,Symbol,Price,Volume,Side,ExchangeTimeTicks`
   - **发送策略**：立即发送，无缓冲
   - **示例**：`T,ES,6849.25,5,BUY,638456789012345678`

2. **深度数据 (DOM)**
   - **格式**：`D,Symbol,bids,asks,ExchangeTimeTicks`
   - **发送策略**：批量缓冲发送（50ms 刷新或 8KB 阈值）
   - **示例**：`D,ES,6849.50@17|6849.25@28,6849.75@12|6850.00@20,638456789012345678`

3. **心跳数据 (Heartbeat)**
   - **格式**：`H,Symbol,LocalTimeTicks`
   - **发送频率**：每 50ms 发送一次

### 2. NFQE_Commander (策略执行端)

**位置**：`ES策略/Class1.cs`

**功能**：
- 接收 Python 端发送的交易指令
- 执行下单、撤单、改单操作
- 管理持仓状态并同步给 Python 端

**核心特性**：

1. **全指令集支持**
   - 市价单 (`MARKET`)
   - 限价单 (`LIMIT`)
   - 排队挂单 (`JOIN_BID`/`JOIN_ASK`)
   - 改单 (`MODIFY`)
   - 一键撤单 (`CANCEL_ALL`)

2. **并发指令队列**
   - 使用 `ConcurrentQueue` 机制
   - FIFO 顺序严格执行
   - 解决指令冲突与乱序风险

3. **防丢包机制**
   - UDP 接收缓冲区扩容至 **10MB**
   - 确保高频指令绝不丢失

4. **虚拟持仓预判**
   - `Virtual Position = Actual + Pending`
   - 防止因成交回报延迟导致的超额开仓

**配置参数**：

```csharp
PortOut = 5555              // 数据发送端口
PortIn = 6666               // 指令接收端口
TradeVolume = 1             // 交易手数
MaxPosition = 5             // 最大持仓
CloseOnStop = true          // 停止时自动平仓
```

**接收的指令格式**：

```
LIMIT,BUY,1,6857.25,client_order_id_123
MODIFY,client_order_id_123,6857.50
CANCEL,client_order_id_123
CLOSE_ALL
```

**发送的状态反馈**：

```
P,ES,1                          // 持仓状态
M,ES,6857.25                    // 当前挂单价格
```

---

## 🐍 Python 组件说明

### 1. 数据接收层

#### dom_data_feed.py

**核心类**：

- `UdpListener`：UDP 服务器，接收并解析数据
- `InstrumentState`：维护单个合约的状态（价格、DOM、成交记录）

**功能**：

- 监听 UDP 端口 5555
- 解析 Trade/DOM/Heartbeat 消息
- 维护事件队列（最大 50000 条）
- 提供线程安全的数据访问

### 2. Norden Engine v3.1 策略引擎

**位置**：`norden_v3/` 目录

#### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **策略引擎** | `maker_engine.py` | 主引擎，整合所有模块 |
| **定价模型** | `kalman_model.py` | 在线卡尔曼滤波，估计 ES 公允价 |
| **备用模型** | `ridge_model.py` | 在线岭回归，对比验证 |
| **微观因子** | `obi_calculator.py` | 加权订单簿失衡度计算 |
| **防御因子** | `iceberg_detector.py` | 冰山订单检测 |
| **风险控制** | `btc_regime.py` | BTC 风险监控，熔断保护 |

#### 决策流程

```
Tick 事件
  ↓
Layer 1: 定价模型
  ├─ Kalman Filter：在线估计 ES 公允价（基于 NQ/YM）
  └─ Ridge Regression：备用定价模型
  ↓
Layer 2: Spread 触发
  ├─ 计算价差 (Spread = Fair - Actual)
  └─ 判断是否超过阈值（默认 0.5 tick）
  ↓
Layer 3: 多重过滤
  ├─ BTC 风险监控（波动率比率 > 3.0 则熔断）
  ├─ 冰山订单检测（隐藏量 > 200 手则拒绝）
  └─ OBI 过滤（订单簿失衡度检查）
  ↓
Layer 4: 队列博弈
  ├─ 估计队列长度（Best Bid/Ask 挂单量）
  └─ 判断是否值得排队（阈值 300 手）
  ↓
执行决策
  ├─ 生成订单指令 (OrderCommand)
  └─ 通过 UDP 发送给 C# 端
```

### 3. 测试与运行脚本

| 脚本 | 功能 |
|------|------|
| `run_norden_v3_test.py` | **完整系统测试**：数据接收 + 策略引擎 + 所有模块 |
| `run_kalman_live.py` | Kalman 模型实时运行（控制台输出） |
| `run_kalman_qt.py` | PyQtGraph 可视化（实时图表） |
| `test_udp_feed.py` | UDP 数据接收测试（最简版） |

### 4. 回测系统

| 脚本 | 功能 |
|------|------|
| `run_backtest.py` | **单次回测**：测试单个配置，生成详细报告 |
| `run_backtest_suite_parallel.py` | **并行批量回测**：同时测试多个配置，公平对比 |
| `run_backtest_suite.py` | **顺序批量回测**：依次测试多个配置（已废弃，推荐使用并行版） |

**回测功能**：
- ✅ 信号生命周期跟踪（MFE/MAE 计算）
- ✅ 可配置止盈/止损/超时
- ✅ 多配置并行对比
- ✅ 自动生成 CSV 报告
- ✅ 支持 ATAS 历史回放（任意倍速）

### 5. 策略仪表盘（Dashboard）

| 程序 | 功能 |
|------|------|
| `run_dashboard_gui.py` | **策略驾驶舱**：实时监控策略状态，支持参数热调整 |

**仪表盘功能**：
- ✅ 实时 Spread 可视化（Kalman + Ridge 双模型）
- ✅ 多重过滤状态显示（红绿灯式）
- ✅ 参数实时调整（Spread 阈值、Kalman R、OBI 阈值等）
- ✅ 实时价格显示（ES/NQ/YM/BTC）
- ✅ 交易信号大字提示

### 6. GUI 可视化

| 程序 | 功能 |
|------|------|
| `pyqt_dom_viewer.py` | PyQt6 DOM 查看器，集成 P0 分析模块 |
| `live_dom_viewer.py` | Tkinter 轻量级 DOM 查看器 |

### 5. 分析模块 (modules/)

| 模块 | 功能 |
|------|------|
| `value_line.py` | 价值线计算（POC, VAH, VAL） |
| `sweep_radar.py` | 扫单雷达（大单检测） |
| `trinity.py` | 三要素评分 |
| `smart_exec.py` | 智能执行算法 |

---

## 🔄 数据流详解

### 1. 数据流向（C# → Python）

```
ATAS 平台
  ↓
NFQE_Bridge_UDP (C# Indicator)
  ↓ (UDP:5555)
UdpListener (Python)
  ↓ (事件队列)
NordenMakerV3 策略引擎
  ↓ (计算 Spread / 过滤)
交易信号生成
```

### 2. 指令流向（Python → C#）

```
NordenMakerV3 策略引擎
  ↓ (生成 OrderCommand)
UDP 指令发送
  ↓ (UDP:6666)
NFQE_Commander (C# Strategy)
  ↓ (执行下单)
ATAS 平台
  ↓ (成交回报)
持仓状态同步 (UDP:5555)
  ↓
Python 端接收
```

### 3. 消息格式

#### 数据消息（C# → Python）

| 类型 | 格式 | 示例 |
|------|------|------|
| **Tick** | `T,Symbol,Price,Volume,Side,ExchangeTimeTicks` | `T,ES,6857.25,5,BUY,638456789012345678` |
| **DOM** | `D,Symbol,bids,asks,ExchangeTimeTicks` | `D,ES,6857.50@17\|6857.25@28,6857.75@12\|6858.00@20,638456789012345678` |
| **Heartbeat** | `H,Symbol,LocalTimeTicks` | `H,ES,638456789012345678` |

#### 指令消息（Python → C#）

| 类型 | 格式 | 示例 |
|------|------|------|
| **限价单** | `LIMIT,Side,Quantity,Price,ClientOrderId` | `LIMIT,BUY,1,6857.25,client_order_id_123` |
| **改单** | `MODIFY,ClientOrderId,NewPrice` | `MODIFY,client_order_id_123,6857.50` |
| **撤单** | `CANCEL,ClientOrderId` | `CANCEL,client_order_id_123` |
| **平仓** | `CLOSE_ALL` | `CLOSE_ALL` |

---

## 🚀 快速开始

### 1. 环境准备

#### C# 端（ATAS 平台）

1. **安装 ATAS Platform**
2. **编译 C# 项目**：
   - `NQ-YM指标（发送数据）/Class1.cs` → 编译为 **Indicator**
   - `ES策略/Class1.cs` → 编译为 **Strategy**

#### Python 端

```bash
# 安装依赖
pip install numpy pyqtgraph PyQt6

# 或使用 conda
conda create -n nfqe39 python=3.9
conda activate nfqe39
pip install numpy pyqtgraph PyQt6
```

### 2. 启动系统

#### 步骤 1：启动 Python 端

```bash
# 方式 1：运行完整测试（推荐）
python run_norden_v3_test.py

# 方式 2：运行可视化
python run_kalman_qt.py

# 方式 3：运行 GUI
python pyqt_dom_viewer.py
```

#### 步骤 2：加载 ATAS 指标

1. 在 ATAS 中打开 **NQ/YM 图表**（可同时打开多个图表）
2. 添加指标：`NFQE_Bridge_UDP`
3. 配置参数：
   - **Python IP**: `127.0.0.1`
   - **Python Port**: `5555`
   - **Depth Levels**: `15`

#### 步骤 3：加载 ATAS 策略

1. 在 ATAS 中打开 **ES 图表**
2. 添加策略：`NFQE_Commander`
3. 配置参数：
   - **UDP Port (Data Out)**: `5555`
   - **UDP Port (Cmd In)**: `6666`
   - **Trade Volume**: `1`
   - **Max Position**: `5`

#### 步骤 4：验证连接

Python 控制台应显示：
```
✅ [NordenV3Test] 系统初始化完成
   等待 C# 端发送数据（请确保 ATAS 指标已启动）...

[STATUS] ES=6857.25 | Fair_KF= 6857.47 Spread_KF= +0.87tick | ...
```

### 3. 基本使用示例

```python
from norden_v3 import NordenMakerV3, TickEvent, DomSnapshot, Side
import socket

# 创建 UDP 客户端（发送指令给 C#）
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
cmd_endpoint = ("127.0.0.1", 6666)

def send_order(cmd):
    """订单回调：发送指令给 C#"""
    if cmd.is_cancel:
        msg = f"CANCEL,{cmd.client_order_id}"
    else:
        side = "BUY" if cmd.side == Side.BUY else "SELL"
        msg = f"{cmd.order_type.name},{side},{cmd.quantity},{cmd.price:.2f},{cmd.client_order_id}"
    
    udp_sock.sendto(msg.encode(), cmd_endpoint)
    print(f"发送指令: {msg}")

# 创建策略引擎
engine = NordenMakerV3(order_sink=send_order)

# 处理市场数据（在实际使用中，这些数据来自 UdpListener）
tick = TickEvent(
    t_ms=1234567890,
    es=6857.25,
    nq=21500.50,
    ym=44000.00,
    btc=95000.00
)
engine.on_tick(tick)

dom = DomSnapshot(
    t_ms=1234567890,
    best_bid=6857.25,
    best_ask=6857.50,
    bids=[(6857.25, 74), (6857.00, 50), ...],
    asks=[(6857.50, 77), (6857.75, 80), ...]
)
engine.on_dom(dom)
```

---

## ⚙️ 配置说明

### Python 端配置

#### Norden Engine v3.1 参数

```python
from norden_v3 import MakerConfig, KalmanConfig

# 策略配置
maker_cfg = MakerConfig(
    base_spread_threshold=0.5,    # Spread 触发阈值（tick）
    min_obi_for_long=0.1,         # 做多最小 OBI
    min_obi_for_short=0.1,        # 做空最小 OBI
    obi_depth=10,                 # OBI 计算深度
    max_queue_size=300,           # 最大队列长度
)

# 卡尔曼配置
kalman_cfg = KalmanConfig(
    init_P=100.0,                 # 初始协方差
    q_beta=1e-12,                 # Beta 过程噪声
    r_obs=100.0,                  # 观测噪声
)

engine = NordenMakerV3(
    maker_cfg=maker_cfg,
    kalman_cfg=kalman_cfg
)
```

### C# 端配置

#### NFQE_Bridge_UDP 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PythonIP` | `127.0.0.1` | Python 监听地址 |
| `PythonPort` | `5555` | UDP 数据发送端口 |
| `DepthLevels` | `15` | DOM 深度（实际发送全部可见档位） |

#### NFQE_Commander 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PortOut` | `5555` | 数据发送端口（发送持仓状态） |
| `PortIn` | `6666` | 指令接收端口 |
| `TradeVolume` | `1` | 每次交易手数 |
| `MaxPosition` | `5` | 最大持仓手数 |
| `CloseOnStop` | `true` | 停止时自动平仓 |

---

## 📊 输出示例

### Python 控制台输出

```
[STATUS] ES=6857.25 | Fair_KF= 6857.47 Spread_KF= +0.87tick | 
         Fair_RD= 6857.45 Spread_RD= +0.80tick | OBI=-0.034 | 
         Queue: B=74 A=77 | 🟢 BTC:1.2x | Iceberg: None | Order: None

[ORDER] 下单: BUY 1@6857.00 (LIMIT, reason: maker_entry_buy)
[ORDER] 撤单: local_1234567890 (timeout_cancel)
```

#### 字段说明

| 字段 | 说明 |
|------|------|
| `Fair_KF` / `Spread_KF` | Kalman 模型公允价/价差 |
| `Fair_RD` / `Spread_RD` | Ridge 模型公允价/价差 |
| `OBI` | 订单簿失衡度（-1 到 +1） |
| `Queue` | 队列长度（Bid/Ask） |
| `BTC` | BTC 风险状态（🟢安全 / 🔴熔断） |
| `Iceberg` | 冰山检测结果 |
| `Order` | 当前挂单状态 |

---

## 📚 文档索引

### 核心文档

| 文档 | 说明 |
|------|------|
| **`NORDEN_V3_项目总览.md`** | 项目总览（⭐推荐从这里开始） |
| **`norden_v3/项目说明文档.md`** | Norden Engine v3.1 完整文档 |
| **`norden_v3/README.md`** | 快速开始指南 |
| **`norden_v3/README_CN.md`** | 中文快速开始指南 |

### 技术文档

| 文档 | 说明 |
|------|------|
| **`norden_v3/数据归一化说明.md`** | Baseline 归一化方法详解 |
| **`norden_v3/参数归一化调整说明.md`** | 归一化后参数调整指南 |
| **`norden_v3/岭回归参数归一化说明.md`** | Ridge 模型参数调整指南 |
| **`norden_v3/冰山检测说明.md`** | 冰山订单检测原理 |
| **`norden_v3/BTC风险监控说明.md`** | BTC 风险监控算法 |
| **`数据获取系统说明.md`** | 数据获取系统详细说明 |
| **`Ridge模型数值溢出修复说明.md`** | Ridge 模型数值稳定性修复 |

### 回测系统文档

| 文档 | 说明 |
|------|------|
| **`回测系统使用说明.md`** | 单次回测系统使用指南 |
| **`并行回测系统说明.md`** | 并行批量回测系统说明 |
| **`批量回测系统使用说明.md`** | 批量回测详细使用指南 |
| **`批量回测系统快速指南.md`** | 快速开始指南 |
| **`回测系统修复说明.md`** | 回测系统问题修复记录 |

### 仪表盘文档

| 文档 | 说明 |
|------|------|
| **`仪表盘使用说明.md`** | 策略仪表盘使用指南 |
| **`仪表盘功能说明.md`** | 仪表盘功能详解 |
| **`实时能量柱说明.md`** | Spread 可视化说明 |

### 故障排查文档

| 文档 | 说明 |
|------|------|
| **`端口占用问题排查.md`** | UDP 端口占用问题解决方案 |
| **`策略自动停止问题排查.md`** | 策略自动停止问题诊断 |
| **`ES数据接收问题排查.md`** | ES 数据接收问题排查 |
| **`回测无信号问题排查.md`** | 回测无信号问题排查 |

### 项目文档

| 文档 | 说明 |
|------|------|
| **`项目文件清单.md`** | 所有文件清单 |
| **`项目文件说明.md`** | 详细文件说明 |

---

## 🔧 技术特点

### 1. 高性能数据传输

| 特性 | 说明 |
|------|------|
| **UDP 协议** | 低延迟，适合高频场景 |
| **智能缓冲** | Tick 立即发送，DOM 批量发送 |
| **防丢包机制** | 10MB 接收缓冲区，确保指令不丢失 |

### 2. 智能定价模型

| 特性 | 说明 |
|------|------|
| **在线卡尔曼滤波** | 实时估计 ES 公允价（基于 NQ/YM 相关性） |
| **在线岭回归** | 备用定价模型，抗共线性，强制保留 Spread |
| **Baseline 归一化** | 解决数量级差异，提高模型稳定性 |
| **矩阵优化** | 针对性矩阵缩放，提高数值稳定性 |
| **数值稳定性保护** | Ridge 模型溢出保护，参数范围限制 |

### 3. 多层风险控制

| 特性 | 说明 |
|------|------|
| **BTC 熔断** | 极端波动自动停止交易（波动率比率 > 3.0） |
| **冰山检测** | 识别隐藏大单，避免在阻力位挂单 |
| **队列博弈** | 估计排队成本，避免排长队 |

### 4. 闭环风控体系

| 特性 | 说明 |
|------|------|
| **持仓状态同步** | C# 实时反馈真实持仓，确保闭环控制 |
| **虚拟持仓预判** | 防止因成交回报延迟导致的超额开仓 |
| **原子化平仓** | 杜绝"平仓后挂单成交"导致的反向持仓风险 |

### 5. 回测与分析系统

| 特性 | 说明 |
|------|------|
| **信号生命周期跟踪** | 自动跟踪每个信号的 MFE/MAE，计算真实盈亏 |
| **并行回测对比** | 同时运行多个配置，使用相同数据流，公平对比 |
| **可配置止盈止损** | 支持自定义 TP/SL 和超时时间 |
| **详细报告生成** | 自动生成 CSV 报告，包含每个信号的完整轨迹 |

### 6. 实时监控与调优

| 特性 | 说明 |
|------|------|
| **策略仪表盘** | PyQt6 实时监控界面，可视化策略状态 |
| **参数热调整** | 盘中实时调整参数，立即生效 |
| **集中配置管理** | 所有参数集中在 `norden_v3/config.py`，便于管理 |

---

## 🎯 系统优势

| 优势 | 说明 |
|------|------|
| **混合架构** | C# 执行 + Python 算法，兼顾性能与灵活性 |
| **实时响应** | UDP 传输，延迟 < 1ms |
| **智能定价** | 双模型并行（Kalman + Ridge），自适应参数调整 |
| **多重过滤** | 四层过滤体系，降低假信号 |
| **风险可控** | 闭环风控，自动熔断保护 |
| **完整回测** | 信号生命周期跟踪，MFE/MAE 分析，多配置对比 |
| **实时监控** | 策略仪表盘，参数热调整，状态可视化 |

---

## 🛠️ 开发与测试

### 测试脚本

```bash
# 完整系统测试（推荐）
python run_norden_v3_test.py

# Kalman 模型测试
python run_kalman_live.py

# 可视化测试
python run_kalman_qt.py

# UDP 数据接收测试
python test_udp_feed.py

# 策略仪表盘（实时监控 + 参数调整）
python run_dashboard_gui.py

# 单次回测
python run_backtest.py

# 并行批量回测（推荐）
python run_backtest_suite_parallel.py
```

### 调试建议

| 问题类型 | 解决方案 |
|---------|---------|
| **数据接收问题** | 先运行 `test_udp_feed.py` 确认 UDP 连接 |
| **策略无信号** | 检查 Spread 阈值和过滤条件 |
| **指令丢失** | 检查 C# 端 UDP 缓冲区配置（10MB） |
| **模型不稳定** | 检查数据归一化和 Kalman 参数 |

---

## 📞 技术支持

### 联系方式

📧 **邮箱**：huangchiyuan@foxmail.com

### 常见问题

#### 1. UDP 连接失败

- 检查防火墙设置
- 确认端口 5555/6666 未被占用
- 验证 C# 端和 Python 端 IP 配置

#### 2. Spread 一直为 0

- 检查数据归一化配置
- 确认 Kalman 参数设置
- 验证 NQ/YM 数据是否正常接收

#### 3. 无交易信号

- 检查 Spread 阈值设置（`base_spread_threshold`）
- 查看过滤条件是否过于严格
- 确认 BTC 风险监控是否触发熔断

### 参考文档

- **详细文档**：`norden_v3/项目说明文档.md`
- **测试脚本**：`run_norden_v3_test.py`
- **代码注释**：各模块均有详细注释

---

## 🔄 版本历史

### v3.1 (当前版本) - 2025-01-19

| 功能 | 状态 |
|------|------|
| Norden Engine v3.1 完整实现 | ✅ |
| 在线卡尔曼滤波 + 岭回归双模型 | ✅ |
| 冰山订单检测 | ✅ |
| BTC 风险监控 | ✅ |
| 完整的多层过滤体系 | ✅ |
| **集中配置系统** (`norden_v3/config.py`) | ✅ |
| **策略仪表盘** (实时监控 + 参数热调整) | ✅ |
| **回测系统** (单次 + 并行批量回测) | ✅ |
| **信号生命周期跟踪** (MFE/MAE 分析) | ✅ |
| **数值稳定性修复** (Ridge 模型溢出保护) | ✅ |
| **并行回测对比** (多配置同时运行) | ✅ |

### v3.0 (旧版)

| 功能 | 状态 |
|------|------|
| NFQE Lite GUI 系统 | ✅ |
| 基础策略执行框架 | ✅ |
| C# 执行端优化 | ✅ |

---

## 📄 许可证

详见 `LICENSE` 文件。

---

---

**项目名称**：Norden Flow Quant Engine (NFQE) Lite  
**版本**：v3.1  
**最后更新**：2025-01-19  
**维护状态**：✅ 活跃开发中  
**联系方式**：huangchiyuan@foxmail.com

---

## 🆕 最新更新 (2025-01-19)

### ✨ 新增功能

1. **并行批量回测系统**
   - 支持同时运行多个配置，使用相同数据流
   - 自动生成对比报告
   - 支持 Kalman 和 Ridge 模型

2. **策略仪表盘 (Dashboard)**
   - PyQt6 实时监控界面
   - 参数热调整功能
   - 实时 Spread 可视化（能量柱）

3. **集中配置系统**
   - 所有参数集中在 `norden_v3/config.py`
   - 详细的参数说明和调优建议
   - 预设配置（保守/激进/平衡）

4. **数值稳定性修复**
   - Ridge 模型溢出保护
   - 参数范围限制
   - 优雅降级机制

### 🔧 改进

- ✅ 回测系统支持历史时间戳同步（任意倍速回放）
- ✅ 信号生命周期完整跟踪（MFE/MAE 计算）
- ✅ 可配置止盈/止损/超时
- ✅ 完善的错误处理和诊断信息
- ✅ 持续运行模式（不再默认 60 秒退出）

### 📚 新增文档

- `并行回测系统说明.md`
- `批量回测系统使用说明.md`
- `仪表盘使用说明.md`
- `Ridge模型数值溢出修复说明.md`
- `策略自动停止问题排查.md`
- `端口占用问题排查.md`
