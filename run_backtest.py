"""
Norden v3.1 回测启动脚本
========================
功能：
    1. 连接 ATAS UDP 数据流 (支持 1000x 倍速回放)
    2. 运行策略引擎生成信号 (不发送真实订单)
    3. 使用 BacktestAnalyzer 追踪并记录信号结果
    4. 程序结束时自动生成统计报告 CSV
"""

import time
import queue
import sys
from typing import Dict, Any, Optional

from dom_data_feed import UdpListener, InstrumentState
from norden_v3 import (
    NordenMakerV3,
    MakerConfig,
    KalmanConfig,
    TickEvent,
    DomSnapshot,
    OrderCommand,
    Side,
)
from norden_v3.backtest_analyzer import BacktestAnalyzer

# --- 配置 ---
TRACK_DURATION = 10.0  # 追踪每个信号 30 秒
REPORT_FILE_PREFIX = "Sim_Backtest"
TICKS_AT_EPOCH = 621355968000000000


def _ticks_to_ms(ticks_str: str) -> int:
    """将 C# ticks 转换为毫秒时间戳"""
    try:
        ticks = int(ticks_str)
        us = (ticks - TICKS_AT_EPOCH) // 10
        return int(us // 1000)
    except:
        return int(time.time() * 1000)


def _parse_dom(raw_str: str):
    """简易DOM解析"""
    levels = []
    if not raw_str or raw_str == "0@0":
        return levels
    for item in raw_str.split("|"):
        if "@" not in item:
            continue
        try:
            p, v = item.split("@")
            levels.append((float(p), float(v)))
        except:
            continue
    return levels


class BacktestRunner:
    def __init__(self):
        print("🚀 [Backtest] 初始化回测环境...", flush=True)
        
        # 1. 数据通道
        self.q = queue.Queue(maxsize=100000)  # 大队列适应 1000x 回放
        self.listener = UdpListener(self.q)
        
        # 2. 分析器 (核心新增)
        self.analyzer = BacktestAnalyzer(track_duration=TRACK_DURATION)
        
        # 3. 策略引擎
        # 通过 order_sink 钩子来捕获信号
        self.engine = NordenMakerV3(
            maker_cfg=MakerConfig(
                base_spread_threshold=0.5,  # 0.5 tick 触发（与测试脚本一致）
                min_obi_for_long=0.1,
                max_queue_size=300,
            ),
            kalman_cfg=None,  # 使用默认配置（与测试脚本一致，便于对比）
            order_sink=self._on_strategy_order,  # 挂钩回调
        )
        
        # 缓存
        self.prices: Dict[str, Optional[float]] = {}
        self.last_dom: Optional[DomSnapshot] = None
        self.current_tick: Optional[TickEvent] = None  # 保存当前 tick，用于信号记录
        
        # 性能统计
        self.event_count = 0
        self.signal_count = 0
        self.last_stats_time = time.time()
        self.last_stats_event_count = 0
        self.last_stats_signal_count = 0

    def _on_strategy_order(self, cmd: OrderCommand):
        """
        [HOOK] 拦截策略的下单指令
        """
        # 只关心开仓指令 (is_cancel=False)
        if cmd.is_cancel or not cmd.price or not cmd.side:
            return
        
        # 必须有当前的 tick 事件才能记录
        if self.current_tick is None:
            return
        
        # 从引擎中提取当时的上下文数据
        fair = self.engine.last_fair or 0.0
        spread_ticks = self.engine.last_spread_ticks or 0.0
        
        # OBI 和 Queue
        obi = 0.0
        queue_len = 0.0
        if self.engine.last_dom:
            obi = self.engine._calc_obi(self.engine.last_dom)
            # 简单估算队列
            if cmd.side == Side.BUY and self.engine.last_dom.bids:
                queue_len = self.engine.last_dom.bids[0][1]
            elif cmd.side == Side.SELL and self.engine.last_dom.asks:
                queue_len = self.engine.last_dom.asks[0][1]
        
        # BTC 状态
        btc_ratio = self.engine.btc_monitor.get_vol_ratio()
        
        side_str = 'BUY' if cmd.side == Side.BUY else 'SELL'
        
        # ★ 通知分析器开始追踪
        # 重要：使用当前市场价格作为 entry_price，而不是挂单价格
        # 因为追踪时使用的是市场价格，必须保持一致
        entry_price = self.current_tick.es if self.current_tick else cmd.price
        
        self.analyzer.on_signal(
            tick=self.current_tick,  # 使用当前的 tick 事件
            side=side_str,
            price=entry_price,  # 使用当前市场价格，而不是挂单价格
            fair=fair,
            spread=spread_ticks,
            obi=obi,
            queue=queue_len,
            btc=btc_ratio,
        )
        
        self.signal_count += 1
        print(f"  [SIGNAL] {side_str} @ {cmd.price:.2f} | Spread: {spread_ticks:+.2f} | OBI: {obi:+.2f}", flush=True)

    def run(self):
        self.listener.start()
        print("✅ [Backtest] 系统就绪，请在 ATAS 中开始回放 (建议 100x - 1000x)...", flush=True)
        print("   按 Ctrl+C 结束并生成报告。\n", flush=True)
        
        try:
            while True:
                batch_size = 0
                # 批量消费 (加速模式) - 移除数量限制，尽量快速处理
                while not self.q.empty():
                    try:
                        event = self.q.get_nowait()
                        batch_size += 1
                        self.event_count += 1
                        
                        if event['type'] == 'T':
                            self._handle_trade(event)
                        elif event['type'] == 'D':
                            self._handle_dom(event)
                    except queue.Empty:
                        break
                
                # 如果没有数据，短暂休眠；如果有数据，立即继续处理
                if batch_size == 0:
                    time.sleep(0.001)
                
                # 每秒打印一次统计信息
                self._print_stats_if_needed()
                
                # 调试信息：检查数据接收和策略状态
                if self.event_count > 0 and self.event_count % 5000 == 0:
                    has_es = 'ES' in self.prices and self.prices['ES'] is not None
                    has_nq = 'NQ' in self.prices and self.prices.get('NQ') is not None
                    has_dom = self.last_dom is not None
                    # 检查所有可能的 ES 变体
                    es_price = (self.prices.get('ES') or 
                               self.prices.get('ES ') or 
                               self.prices.get('es') or 
                               'N/A')
                    nq_price = self.prices.get('NQ', 'N/A')
                    
                    # 显示所有已接收的品种
                    all_symbols = list(self.prices.keys())
                    
                    fair_str = f"{self.engine.last_fair:.2f}" if self.engine.last_fair else "None"
                    spread_str = f"{self.engine.last_spread_ticks:+.2f}" if self.engine.last_spread_ticks else "None"
                    
                    print(f"[DEBUG] 事件={self.event_count:,} | "
                          f"ES={es_price} NQ={nq_price} DOM={'✓' if has_dom else '✗'} | "
                          f"Fair={fair_str} Spread={spread_str}t | "
                          f"已接收品种: {all_symbols[:5]}... | "
                          f"信号数={self.signal_count}",
                          flush=True)
                
        except KeyboardInterrupt:
            print("\n🛑 回测结束，正在生成统计报告...", flush=True)
            self.analyzer.save_report(REPORT_FILE_PREFIX)
            self.listener.stop()
            print("✅ 完成。", flush=True)
    
    def _print_stats_if_needed(self):
        """每 1 秒打印一次处理速度统计"""
        now = time.time()
        elapsed = now - self.last_stats_time
        
        if elapsed >= 1.0:
            events_per_sec = (self.event_count - self.last_stats_event_count) / elapsed
            signals_per_sec = (self.signal_count - self.last_stats_signal_count) / elapsed
            queue_size = self.q.qsize()
            
            print(
                f"[STATS] 事件: {events_per_sec:.0f}/s | "
                f"信号: {signals_per_sec:.2f}/s | "
                f"队列: {queue_size} | "
                f"总事件: {self.event_count:,} | "
                f"总信号: {self.signal_count}",
                flush=True
            )
            
            self.last_stats_time = now
            self.last_stats_event_count = self.event_count
            self.last_stats_signal_count = self.signal_count

    def _handle_trade(self, event: Dict[str, Any]):
        """处理成交事件"""
        sym = event.get('symbol', '').strip()  # 移除可能的空格
        price = float(event['price'])
        self.prices[sym] = price
        
        # 调试：记录收到的所有品种数据
        if self.event_count % 1000 == 0:  # 每1000个事件打印一次
            all_symbols = list(self.prices.keys())
            print(f"[TRADE DEBUG] 收到: {sym} @ {price:.2f} | 已缓存品种: {all_symbols}", flush=True)
        
        # 喂给策略和分析器
        # 注意：ES 可能是 'ES' 或 'ES ' 或其他变体，需要统一处理
        if sym.upper().strip() == 'ES':
            tick = TickEvent(
                t_ms=_ticks_to_ms(event['ticks']),
                es=price,
                nq=self.prices.get('NQ'),
                ym=self.prices.get('YM') or self.prices.get('MYM'),
                btc=self.prices.get('BTCUSDT'),
            )
            
            # 保存当前 tick，供信号记录时使用
            self.current_tick = tick
            
            # 1. 驱动策略 (策略可能会触发 _on_strategy_order)
            # 注意：策略需要 NQ 数据才能工作
            if tick.nq:
                self.engine.on_tick(tick)
            else:
                # 如果没有 NQ 数据，策略无法工作，但分析器仍可更新价格
                pass
            
            # 2. 驱动分析器 (更新价格轨迹，传入历史时间戳)
            # 重要：分析器只需要 ES 价格，不需要等待 NQ
            # 这样可以确保所有追踪器都能及时更新价格
            self.analyzer.on_tick_update(price, tick.t_ms)

    def _handle_dom(self, event: Dict[str, Any]):
        """处理 DOM 事件"""
        if event['symbol'] != 'ES':
            return
        
        bids = _parse_dom(event['bids'])
        asks = _parse_dom(event['asks'])
        if not bids or not asks:
            return
        
        dom = DomSnapshot(
            t_ms=_ticks_to_ms(event['ticks']),
            best_bid=bids[0][0],
            best_ask=asks[0][0],
            bids=bids,
            asks=asks,
        )
        self.engine.on_dom(dom)
        self.last_dom = dom
    
    def _print_stats_if_needed(self):
        """每 1 秒打印一次处理速度统计"""
        now = time.time()
        elapsed = now - self.last_stats_time
        
        if elapsed >= 1.0:
            events_per_sec = (self.event_count - self.last_stats_event_count) / elapsed if elapsed > 0 else 0
            signals_per_sec = (self.signal_count - self.last_stats_signal_count) / elapsed if elapsed > 0 else 0
            queue_size = self.q.qsize()
            
            print(
                f"[STATS] 事件: {events_per_sec:.0f}/s | "
                f"信号: {signals_per_sec:.2f}/s | "
                f"队列: {queue_size} | "
                f"总事件: {self.event_count:,} | "
                f"总信号: {self.signal_count}",
                flush=True
            )
            
            self.last_stats_time = now
            self.last_stats_event_count = self.event_count
            self.last_stats_signal_count = self.signal_count


if __name__ == "__main__":
    runner = BacktestRunner()
    runner.run()

