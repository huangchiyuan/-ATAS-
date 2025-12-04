# Norden Engine v3.1 测试指南

## 完整系统测试

### 测试脚本：`run_norden_v3_test.py`

这是一个综合测试脚本，验证整个 v3.1 系统的所有组件是否正常工作。

#### 测试内容

1. **数据接收层**
   - UDP 监听（端口 5555）
   - 解析 T（成交）和 D（DOM）事件
   - 维护 ES/NQ/YM/BTC 价格缓存

2. **定价模型层**
   - Kalman 滤波计算 ES 公允价
   - 实时更新 Beta 参数

3. **微观结构层**
   - OBI（订单簿失衡）计算
   - 队列长度估计

4. **策略引擎层**
   - 完整的 NordenMakerV3 决策流程
   - 多层过滤器（L1/L2/L3/L4）
   - 交易信号生成（但不下单，仅打印）

#### 使用方法

1. **启动 ATAS 回放**
   - 加载 `NFQE_Bridge_UDP` 指标
   - 配置指向 `127.0.0.1:5555`
   - 开启回放（1x ~ 500x 均可）

2. **运行测试脚本**
   ```bash
   python run_norden_v3_test.py
   ```

3. **观察输出**
   - 每 0.5 秒打印一行状态
   - 如果触发交易信号，会打印 `[ORDER]` 日志
   - 按 `Ctrl+C` 停止

#### 输出示例

```
[STATUS] ES=6862.25 | Fair_KF=6862.30 | Spread=+0.20tick | OBI=+0.150 | Queue: Bid=  45 Ask=  32 | Pos=None
[STATUS] ES=6862.50 | Fair_KF=6862.45 | Spread=-0.20tick | OBI=-0.080 | Queue: Bid=  50 Ask=  40 | Pos=None
  [ORDER] 下单: BUY 1@6862.50 (LIMIT, reason: maker_entry_buy)
[STATUS] ES=6862.50 | Fair_KF=6862.48 | Spread=-0.08tick | OBI=+0.120 | Queue: Bid=  52 Ask=  38 | Pos=local_1234567890
  [ORDER] 撤单: local_1234567890 (timeout_cancel)
```

#### 输出字段说明

| 字段 | 说明 |
|------|------|
| `ES` | ES 当前实际价格 |
| `Fair_KF` | Kalman 计算的公允价 |
| `Spread` | 价差（tick 单位），正值=做多信号，负值=做空信号 |
| `OBI` | 订单簿失衡度 [-1, +1]，>0.1 支持做多，<-0.1 支持做空 |
| `Queue: Bid/Ask` | 最优档的聚合成交量（队列长度近似值） |
| `Pos` | 当前挂单/持仓状态（None=空仓） |

---

## 单元测试

### 1. Kalman 模型测试：`run_kalman_live.py`

测试 Kalman 滤波器的实时更新和参数收敛。

**输出**：每 0.5 秒打印 Beta 系数和 Spread

---

### 2. 可视化测试：`run_kalman_qt.py`

PyQtGraph 高性能图表，对比 ES / Fair / NQ / YM 的轨迹。

**输出**：实时图表窗口（支持缩放、平移）

---

### 3. OBI 计算测试

可以直接在 Python 交互环境中测试：

```python
from norden_v3 import OBICalculator, OBIConfig, DomSnapshot

# 创建计算器
calc = OBICalculator(OBIConfig(depth=10, decay=0.5))

# 构造测试 DOM
dom = DomSnapshot(
    t_ms=1000,
    best_bid=6800.0,
    best_ask=6800.25,
    bids=[(6800.0, 500), (6799.75, 400), ...],
    asks=[(6800.25, 100), (6800.50, 100), ...],
)

# 计算 OBI
obi = calc.calculate(dom)
print(f"OBI: {obi:.4f}")
```

---

## 故障排查

### 问题 1：没有收到数据

**症状**：控制台一直显示 "等待 C# 端发送数据"

**检查**：
1. ATAS 指标是否已加载？
2. 指标配置的 IP/Port 是否正确（127.0.0.1:5555）？
3. 防火墙是否阻止了 UDP 端口 5555？

### 问题 2：Spread 一直是 0

**症状**：Fair_KF 和 ES 几乎完全一致，Spread ≈ 0

**可能原因**：
- Kalman 模型还在预热期（前 10 秒）
- 市场本身波动很小
- 模型参数需要调整（增大 R，减小 Q）

### 问题 3：OBI 总是 0

**症状**：OBI 值始终为 0.000

**检查**：
- DOM 数据是否正常接收？（检查 Queue 字段是否有值）
- 是否收到了 ES 的 DOM 事件（type='D'）？

### 问题 4：没有交易信号

**症状**：Spread 和 OBI 都有值，但从不打印 `[ORDER]`

**可能原因**：
- Spread 未超过阈值（默认 0.5 tick）
- OBI 未满足条件（做多需 >0.1，做空需 <-0.1）
- 队列太长（>300 手）
- BTC 波动率过滤触发

---

## 性能指标

### 预期延迟

- **数据接收延迟**：< 1ms（UDP 本地回环）
- **Kalman 更新延迟**：< 0.1ms（纯矩阵运算）
- **OBI 计算延迟**：< 0.01ms（向量化）
- **总决策延迟**：< 2ms（端到端）

### 吞吐量

- **Tick 处理速度**：> 10,000 条/秒
- **DOM 更新速度**：> 100 次/秒

---

## 下一步

测试通过后，可以：

1. **接入真实执行端**
   - 修改 `order_sink` 回调，连接到 Rithmic / C# API
   - 添加订单回报处理（`on_fill`, `on_cancel`）

2. **参数调优**
   - 根据回测结果调整 Kalman 参数（R, Q）
   - 调整 OBI 阈值和队列长度限制

3. **风险控制**
   - 添加持仓限制
   - 添加每日亏损上限
   - 添加时间段过滤

