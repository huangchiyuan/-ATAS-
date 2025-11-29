## ⭐️ Repository Name: `NordenFlow-QuantEngine-Lite`

## 📝 项目详细描述 (README.md Content)

### 1. 执行摘要 (Executive Summary)

**Norden Flow Quant Engine (NFQE) Lite** 是一个专为 CME ES/NQ 期货设计的、基于微观结构分析的半自动化交易系统。

该系统放弃了传统的基于 K 线图的预测方法，而是严格遵循 Gary Norden 的 **“不预测，只反应”** 哲学。其核心目标是通过捕获高频的、统计学上的微小优势（1-3 ticks），利用 **频率复合边际 (Edge × Frequency)** 效应积累稳定收益。

### 2. 核心价值判断算法 (Value Determination)

本系统将“价值 (Value)”定义为由市场环境决定的动态区域，而非单一的 VWAP 均线。

* **价值定义：** 价值由 **相关性 (Correlations)** 和 **订单流 (Tape)** 共同确定，不等于价格。
* **VWAP 的角色：** **uVWAP** 仅作为 **“执行位置的过滤器”**。它量化了当前价格是折价 (Discount) 还是溢价 (Premium)，辅助算法在有利的成本区挂单。
* **价值驱动：** 通过 **NQ/YM 权重模型** 实时计算 **Value Bias Score**。Score 决定了价值的方向：
    * **Score > +0.3:** 价值上移，偏多。
    * **Score < -0.3:** 价值下移，偏空。

### 3. 微观结构与执行逻辑 (Micro Execution Layer)

系统通过 **三要素**（价值、EQP、订单流）过滤进场时机。**V3.5 版本已完全实装限价单与改单逻辑，确保微观优势的执行。**

| 模块 | 核心功能 | 算法依据 | Norden 规则引用 |
| :--- | :--- | :--- | :--- |
| **动态 EQP 过滤** | 计算队列长度，判断是否值得排队。 | 阈值根据 **Tape 流速** 动态调整，防止排长队。 | 厚市场 (ES) EQP 必须 $< 200$。 |
| **墙体检测** | 扫描 DOM 5 档深度。 | 识别超过 **200 手** (ES) 的显性大单，作为阻力或支撑墙。 | 机构大单 = 风险，需回避。 |
| **挂单原则** | 确定最佳执行价格。 | **[New] 智能挂单 (Smart Limit):** 支持 `JOIN_BID`/`JOIN_ASK` 指令，自动挂在最优买一/卖一价，并通过 `MODIFY` 指令跟随盘口移动。 | 99% 采用限价单，赚取结构性价差。 |
| **流速适应** | 调整 VWAP 周期。 | 慢速市场（亚盘）自动使用较短周期 (e.g., 1000 ticks) 来保证指标的灵敏度，克服滞后问题。 | 速度决定策略，动态调整是核心. |

### 4. 架构与闭环风控 (Closed-Loop Risk Management)

NFQE 采用混合架构，确保数据和指令的传输效率与安全。**C# 执行端 (Commander) 在 V3.5 版本进行了核心重构，以满足高频交易的健壮性要求。**

* **Commander (C# Strategy V3.5):** 部署在 ES 图表，作为系统的 **智能执行核心**。
    * **全指令集支持：** 不仅支持市价单 (`MARKET`)，现已完整支持 **限价单 (`LIMIT`)**、**排队挂单 (`JOIN`)**、**改单 (`MODIFY`)** 及 **一键撤单 (`CANCEL_ALL`)**。
    * **并发指令队列 (Command Queue):** 引入 `ConcurrentQueue` 机制，Python 发出的高频指令（如毫秒级的连续改单）会被存入队列并按 **FIFO（先进先出）** 顺序严格执行，彻底解决了指令冲突与乱序风险。
    * **防丢包机制 (Anti-Packet-Loss):** UDP 接收缓冲区扩容至 **10MB**（系统默认仅 64KB），在图表重绘或系统高负载时，确保交易指令绝不丢失。
    * **虚拟持仓预判 (Projected Position):** 实现了 `Virtual Position = Actual + Pending` 的实时计算。在风控检查时，系统不仅看当前持仓，还能“看到”在途未成交的订单，防止因成交回报延迟导致的超额开仓。

* **Scout (C# Indicator):** 部署在 NQ/YM 图表，负责 **数据发送**。
* **Brain (Python Strategy):** 负责 **计算** 和 **决策**。

#### 关键风控机制：

* **持仓状态同步 (Position Feedback):** C# 策略通过 UDP 实时发送 **真实持仓量 (`P` 消息)** 给 Python。Python 不再依赖“虚拟持仓”，确保了闭环控制。
* **原子化平仓 (Atomic Exit):** `CLOSE_ALL` 指令在 C# 内部被原子化为 **"撤销所有挂单 -> 市价平仓"** 两个步骤，杜绝了“平仓后挂单成交”导致的意外反向持仓风险。
* **主动划线 (Scratch Alert):** 策略持续监控入场后的持仓时间 (Time Stop) 和最大不利变动 (MAE)。
    * **触发条件：** 持仓 > 10 秒无利润，或遭遇反向冲击。
    * **动作：** GUI 闪烁 **"🛡️ SCRATCH NOW"**，并发送 `CLOSE_ALL` 指令。