
## ⭐️ Repository Name: `NordenFlow-QuantEngine-Lite`

## 📝 项目详细描述 (README.md Content)

### 1. 执行摘要 (Executive Summary)

[cite_start]**Norden Flow Quant Engine (NFQE) Lite** 是一个专为 CME ES/NQ 期货设计的、基于微观结构分析的半自动化交易系统 [cite: 3, 43]。

[cite_start]该系统放弃了传统的基于 K 线图的预测方法，而是严格遵循 Gary Norden 的 **“不预测，只反应”** 哲学 [cite: 136][cite_start]。其核心目标是通过捕获高频的、统计学上的微小优势（1-3 ticks），利用 **频率复合边际 (Edge × Frequency)** 效应积累稳定收益 [cite: 56, 62]。

### 2. 核心价值判断算法 (Value Determination)

本系统将“价值 (Value)”定义为由市场环境决定的动态区域，而非单一的 VWAP 均线。

* [cite_start]**价值定义：** 价值由 **相关性 (Correlations)** 和 **订单流 (Tape)** 共同确定，不等于价格 [cite: 17, 184]。
* [cite_start]**VWAP 的角色：** **uVWAP** 仅作为 **“执行位置的过滤器”** [cite: 73]。它量化了当前价格是折价 (Discount) 还是溢价 (Premium)，辅助算法在有利的成本区挂单。
* **价值驱动：** 通过 **NQ/YM 权重模型** 实时计算 **Value Bias Score**。Score 决定了价值的方向：
    * **Score > +0.3:** 价值上移，偏多。
    * **Score < -0.3:** 价值下移，偏空。

### 3. 微观结构与执行逻辑 (Micro Execution Layer)

[cite_start]系统通过 **三要素**（价值、EQP、订单流）过滤进场时机 [cite: 274]。

| 模块 | 核心功能 | 算法依据 | Norden 规则引用 |
| :--- | :--- | :--- | :--- |
| **动态 EQP 过滤** | 计算队列长度，判断是否值得排队。 | [cite_start]阈值根据 **Tape 流速** 动态调整，防止排长队 [cite: 163, 171]。 | [cite_start]厚市场 (ES) EQP 必须 $< 200$ [cite: 165]。 |
| **墙体检测** | 扫描 DOM 5 档深度。 | [cite_start]识别超过 **200 手** (ES) 的显性大单，作为阻力或支撑墙 [cite: 448]。 | [cite_start]机构大单 = 风险，需回避 [cite: 445]。 |
| **挂单原则** | 确定最佳执行价格。 | [cite_start]只有当 `BestBid < uVWAP` (折价) 且 EQP 良好时，才建议 **Join the Bid** [cite: 87, 89]。 | [cite_start]99% 采用限价单，赚取结构性价差 [cite: 86, 84]。 |
| **流速适应** | 调整 VWAP 周期。 | [cite_start]慢速市场（亚盘）自动使用较短周期 (e.g., 1000 ticks) 来保证指标的灵敏度，克服滞后问题 [cite: 206]。 | [cite_start]速度决定策略，动态调整是核心[cite: 210]. |

### 4. 架构与闭环风控 (Closed-Loop Risk Management)

NFQE 采用混合架构，确保数据和指令的传输效率与安全。

* **Commander (C# Strategy):** 部署在 ES 图表，负责 **指令执行** 和 **数据发送 (Port 5555)**。
* **Scout (C# Indicator):** 部署在 NQ/YM 图表，负责 **数据发送**。
* **Brain (Python Strategy):** 负责 **计算** 和 **决策**。

#### 关键风控机制：

* [cite_start]**持仓状态同步 (Position Feedback):** C# 策略通过 UDP 实时发送 **真实持仓量 (`P` 消息)** 给 Python。Python 不再依赖“虚拟持仓”，确保了闭环控制 [cite: 568]。
* **主动划线 (Scratch Alert):** 策略持续监控入场后的持仓时间 (Time Stop) 和最大不利变动 (MAE)。
    * [cite_start]**触发条件：** 持仓 > 10 秒无利润，或遭遇反向冲击 [cite: 131, 119]。
    * **动作：** GUI 闪烁 **"🛡️ SCRATCH NOW"**，并发送 `CLOSE_ALL` 指令。
* **线程安全：** 使用 UDP 协议和 Python 的 `queue` 机制，彻底隔离网络 I/O 和 UI 线程，防止平台崩溃。
