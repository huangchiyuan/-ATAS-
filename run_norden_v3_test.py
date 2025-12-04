"""
Norden Engine v3.1 完整系统测试脚本
===================================

测试内容：
    1. 数据接收：UDP 监听 ES/NQ/YM 的 Tick 和 DOM 数据
    2. 定价模型：Kalman + Ridge 双模型并行计算公允价
    3. OBI 计算：订单簿失衡度
    4. BTC 风险监控：检测极端市场波动，触发熔断保护
    5. 冰山检测：实时检测隐藏订单
    6. 策略引擎：完整的 NordenMakerV3 决策流程

输出格式说明：
    [STATUS] ES=6866.75 | Fair_KF= 6866.79 Spread_KF= +0.14tick | Fair_RD= 6866.75 Spread_RD= +0.00tick | OBI=-0.135 | Queue: B=  71 A=  80 | 🟢 BTC:1.2x | Iceberg: None | Order: LONG@6871.50
    
    字段说明：
    - ES: 当前 ES 价格
    - Fair_KF: Kalman 模型计算的公允价
    - Spread_KF: Kalman 模型计算的价差（tick 单位）
    - Fair_RD: Ridge 模型计算的公允价
    - Spread_RD: Ridge 模型计算的价差（tick 单位）
    - OBI: 订单簿失衡度（-1 到 +1）
    - Queue: B/A 分别表示 Best Bid/Ask 的挂单量
    - BTC: BTC 风险监控状态
        * 🟢 BTC:1.2x: 市场安全，波动率比率 1.2（正常）
        * 🔴 BTC:3.5x: 市场极端波动，已触发熔断（比率 > 3.0）
    - Iceberg: 
        * None: 未检测到冰山订单
        * 🧊 Iceberg: R=150 S=80 [6870.25(ASK,150) | 6865.00(BID,80)]
          - R: 上方阻力总量（手）
          - S: 下方支撑总量（手）
          - [] 内显示具体价位和方向：价格(ASK/BID,隐藏量)
    - Order: 当前挂单状态
        * None: 无挂单
        * LONG@6871.50: 做多挂单，价格 6871.50
        * SHORT@6861.75: 做空挂单，价格 6861.75

使用：
    python run_norden_v3_test.py
"""

from __future__ import annotations

import queue
import time
from typing import Dict, Any, Optional

from dom_data_feed import UdpListener, InstrumentState
from norden_v3 import (
    NordenMakerV3,
    MakerConfig,
    OnlineRidge,
    RidgeConfig,
    TickEvent,
    DomSnapshot,
    Side,
)


def _ticks_to_ms(ticks_str: str) -> int:
    """将 .NET Ticks 转成 Unix 毫秒时间戳."""
    try:
        ticks = int(ticks_str)
    except Exception:
        return int(time.time() * 1000)

    TICKS_AT_EPOCH = 621355968000000000
    us = (ticks - TICKS_AT_EPOCH) // 10
    return int(us // 1000)


def _parse_dom_levels(raw_str: str) -> list[tuple[float, float]]:
    """解析 C# 发送的 DOM 字符串格式: 'price@vol|price@vol|...'"""
    levels = []
    if not raw_str or raw_str == "0@0":
        return levels

    for item in raw_str.split("|"):
        if "@" not in item:
            continue
        if item == "0@0":
            continue

        try:
            parts = item.split("@")
            if len(parts) != 2:
                continue
            price = float(parts[0].strip())
            vol = float(parts[1].strip())
            if price > 0 and vol > 0:
                levels.append((price, vol))
        except (ValueError, IndexError):
            continue

    return levels


class NordenV3Tester:
    """完整的 v3.1 系统测试器."""

    def __init__(self):
        print("🚀 [NordenV3Test] 初始化系统...")

        # 数据接收
        self.q: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=50000)
        self.listener = UdpListener(self.q)

        # 策略引擎（配置一个保守的参数用于测试）
        maker_cfg = MakerConfig(
            base_spread_threshold=0.5,  # 0.5 tick
            min_obi_for_long=0.1,
            min_obi_for_short=0.1,
            obi_depth=10,
            max_queue_size=300,
        )
        
        # 自定义 order_sink：只打印，不下单
        def mock_order_sink(cmd):
            side_str = cmd.side.name if cmd.side else "N/A"
            if cmd.is_cancel:
                print(f"  [ORDER] 撤单: {cmd.client_order_id} ({cmd.reason})")
            else:
                print(
                    f"  [ORDER] 下单: {side_str} {cmd.quantity}@{cmd.price:.2f} "
                    f"({cmd.order_type.name}, reason: {cmd.reason})"
                )

        self.engine = NordenMakerV3(
            maker_cfg=maker_cfg,
            kalman_cfg=None,  # 使用默认配置
            order_sink=mock_order_sink,
        )

        # 独立的 Ridge 模型（用于对比显示）
        self.ridge = OnlineRidge(RidgeConfig())

        # 独立的价格缓存（用于构造 TickEvent）
        self.last_prices: Dict[str, float] = {}
        self.instruments: Dict[str, InstrumentState] = {}
        
        # Ridge 模型的最新结果（用于显示）
        self.ridge_fair: Optional[float] = None
        self.ridge_spread_ticks: Optional[float] = None

        # 打印控制
        self.last_print_time = 0.0
        self.print_interval = 0.5  # 每 0.5 秒打印一次

        print("✅ [NordenV3Test] 系统初始化完成")
        print("   等待 C# 端发送数据（请确保 ATAS 指标已启动）...\n")

    def run(self):
        """主循环."""
        self.listener.start()

        try:
            while True:
                self._consume_events()
                time.sleep(0.01)  # 10ms 轮询间隔

        except KeyboardInterrupt:
            print("\n\n⏹️  [NordenV3Test] 收到停止信号，正在关闭...")
            self.listener.stop()
            print("✅ [NordenV3Test] 已安全退出")

    def _consume_events(self):
        """消费 UDP 队列中的事件."""
        processed = 0
        while processed < 100:  # 每次最多处理 100 条
            try:
                event = self.q.get_nowait()
            except queue.Empty:
                break

            processed += 1

            if event.get("type") == "T":
                self._handle_trade(event)
            elif event.get("type") == "D":
                self._handle_dom(event)

        # 定期打印状态
        now = time.time()
        if now - self.last_print_time >= self.print_interval:
            # 在处理状态前，先刷新冰山检测器（处理所有缓存的成交）
            self.engine.iceberg_detector.flush_trade_buffer()
            
            self._print_status()
            self.last_print_time = now

    def _handle_trade(self, event: Dict[str, Any]):
        """处理成交事件."""
        symbol = event.get("symbol", "")
        price = float(event.get("price", 0.0))
        ticks_str = event.get("ticks", "")

        # 更新价格缓存
        self.last_prices[symbol] = price

        # 维护 InstrumentState（用于后续可能的 DOM 解析）
        if symbol not in self.instruments:
            self.instruments[symbol] = InstrumentState(symbol)
        self.instruments[symbol].add_trade(
            price, float(event.get("volume", 0.0)), event.get("side", ""), ticks_str
        )

        # 只有收到 ES tick 时才构造完整 TickEvent 并喂给策略引擎
        if symbol == "ES":
            tick = TickEvent(
                t_ms=_ticks_to_ms(ticks_str),
                es=self.last_prices.get("ES"),
                nq=self.last_prices.get("NQ"),
                ym=self.last_prices.get("YM") or self.last_prices.get("MYM"),
                btc=self.last_prices.get("BTCUSDT"),
            )

            # 检查数据完整性（至少需要 ES + NQ）
            if tick.es and tick.nq:
                # 更新策略引擎（使用 Kalman）
                self.engine.on_tick(tick)
                
                # 同时更新独立的 Ridge 模型（用于对比显示）
                fair_rd, spread_rd = self.ridge.update(tick)
                if fair_rd is not None and spread_rd is not None:
                    self.ridge_fair = fair_rd
                    self.ridge_spread_ticks = spread_rd / 0.25  # 转换为 tick
                else:
                    self.ridge_fair = None
                    self.ridge_spread_ticks = None

        # 更新冰山检测器（所有 ES 成交都需要）
        if symbol == "ES":
            t_ms = _ticks_to_ms(ticks_str)
            volume = float(event.get("volume", 0.0))
            side = event.get("side", "")
            if volume > 0:
                self.engine.iceberg_detector.on_trade(price, volume, side, t_ms)

    def _handle_dom(self, event: Dict[str, Any]):
        """处理 DOM 事件."""
        symbol = event.get("symbol", "")
        if symbol != "ES":  # 只处理 ES 的 DOM
            return

        bids_str = event.get("bids", "")
        asks_str = event.get("asks", "")
        ticks_str = event.get("ticks", "")

        bids = _parse_dom_levels(bids_str)
        asks = _parse_dom_levels(asks_str)

        if not bids or not asks:
            return

        # 构造 DomSnapshot
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0

        dom = DomSnapshot(
            t_ms=_ticks_to_ms(ticks_str),
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
        )

        self.engine.on_dom(dom)

    def _print_status(self):
        """打印当前状态（每 0.5 秒一次）."""
        # 检查必要数据是否齐全
        es_price = self.last_prices.get("ES")
        nq_price = self.last_prices.get("NQ")

        if es_price is None or nq_price is None:
            return

        if not self.engine.last_dom:
            return

        # 直接从策略引擎获取最新计算结果
        fair_kf = self.engine.last_fair
        spread_ticks = self.engine.last_spread_ticks or 0.0
        
        # 计算 OBI
        obi = self.engine._calc_obi(self.engine.last_dom) if self.engine.last_dom else 0.0

        # 队列长度估计
        queue_bid = 0.0
        queue_ask = 0.0
        if self.engine.last_dom:
            if self.engine.last_dom.bids:
                queue_bid = self.engine.last_dom.bids[0][1]
            if self.engine.last_dom.asks:
                queue_ask = self.engine.last_dom.asks[0][1]

        # 冰山检测结果
        resistance = self.engine.iceberg_detector.get_resistance(es_price) if es_price else 0.0
        support = self.engine.iceberg_detector.get_support(es_price) if es_price else 0.0
        iceberg_map = self.engine.iceberg_detector.get_iceberg_map()
        iceberg_count = len(iceberg_map)
        
        # 格式化冰山信息（显示价位）
        iceberg_info = []
        if iceberg_map:
            # 按价格排序，只显示前3个
            sorted_icebergs = sorted(iceberg_map.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
            for price, vol in sorted_icebergs:
                side_str = "ASK" if vol > 0 else "BID"
                iceberg_info.append(f"{price:.2f}({side_str},{abs(vol):.0f})")
        iceberg_str = " | ".join(iceberg_info) if iceberg_info else "None"
        
        # 订单状态信息
        pos = self.engine.position
        if pos.active_order_id:
            side_str = "LONG" if pos.side == Side.BUY else "SHORT"
            order_info = f"{side_str}@{pos.entry_price:.2f}"
        else:
            order_info = "None"

        # 格式化输出（处理可能的 None 值）
        fair_kf_str = f"{fair_kf:.2f}" if fair_kf is not None else "N/A"
        spread_kf_str = f"{spread_ticks:+.2f}" if spread_ticks is not None else "N/A"
        
        # Ridge 模型结果
        fair_rd_str = f"{self.ridge_fair:.2f}" if self.ridge_fair is not None else "N/A"
        spread_rd_str = f"{self.ridge_spread_ticks:+.2f}" if self.ridge_spread_ticks is not None else "N/A"
        
        # BTC 风险监控状态
        btc_stats = self.engine.btc_monitor.get_stats()
        btc_safe = btc_stats.get("is_safe", True)
        btc_ratio = btc_stats.get("vol_ratio", 1.0)
        btc_status = "🟢" if btc_safe else "🔴"
        btc_status_str = f"{btc_status} BTC:{btc_ratio:.2f}x"

        # 构建输出行（同时显示 Kalman 和 Ridge 结果）
        parts = [
            f"ES={es_price:.2f}",
            f"Fair_KF={fair_kf_str:>8} Spread_KF={spread_kf_str:>6}tick",
            f"Fair_RD={fair_rd_str:>8} Spread_RD={spread_rd_str:>6}tick",
            f"OBI={obi:+.3f}",
            f"Queue: B={queue_bid:>4.0f} A={queue_ask:>4.0f}",
            btc_status_str,
        ]
        
        # 如果有冰山，显示详细信息
        if iceberg_count > 0:
            parts.append(f"🧊 Iceberg: R={resistance:>4.0f} S={support:>4.0f} [{iceberg_str}]")
        else:
            parts.append(f"Iceberg: None")
        
        # 订单状态
        parts.append(f"Order: {order_info}")
        
        print(f"[STATUS] {' | '.join(parts)}")


def main():
    print("=" * 70)
    print("Norden Engine v3.1 - 完整系统测试")
    print("=" * 70)
    print()
    print("说明：")
    print("  - 本脚本会接收 UDP 数据并运行完整的策略流程")
    print("  - 所有交易信号会打印到控制台，但不会真正下单")
    print("  - 按 Ctrl+C 停止")
    print()
    print("=" * 70)
    print()

    tester = NordenV3Tester()
    tester.run()


if __name__ == "__main__":
    main()

