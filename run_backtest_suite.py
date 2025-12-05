"""
批量回测套件启动脚本
====================

功能：
    1. 支持多个配置同时回测
    2. 模型选择（Kalman/Ridge/Both）
    3. 参数对比和统计摘要
    4. 自动生成对比报告

使用方式：
    python run_backtest_suite.py
"""

import time
import queue
import sys
from typing import Dict, Any, Optional, List

from dom_data_feed import UdpListener, InstrumentState
from norden_v3 import (
    NordenMakerV3,
    MakerConfig,
    KalmanConfig,
    RidgeConfig,
    TickEvent,
    DomSnapshot,
    OrderCommand,
    Side,
    OnlineRidge,
)
from norden_v3.backtest_analyzer import BacktestAnalyzer
from norden_v3 import BacktestConfig, BacktestResult, PricingModel, RidgeMakerEngine

# --- 配置 ---
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


class SingleBacktestRunner:
    """单个配置的回测运行器"""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        
        # 数据通道
        self.q = queue.Queue(maxsize=100000)
        self.listener = UdpListener(self.q)
        
        # 分析器
        self.analyzer = BacktestAnalyzer(
            track_duration=config.track_duration,
            tick_size=config.tick_size,
            tp_ticks=config.tp_ticks,
            sl_ticks=config.sl_ticks,
        )
        
        # 策略引擎（根据配置选择模型）
        self.ridge_model = None  # 用于 BOTH 模式
        
        if config.pricing_model == PricingModel.KALMAN:
            self.engine = NordenMakerV3(
                maker_cfg=config.maker_config,
                kalman_cfg=config.kalman_config,
                order_sink=self._on_strategy_order,
            )
        elif config.pricing_model == PricingModel.RIDGE:
            # 使用 Ridge 引擎包装器
            self.engine = RidgeMakerEngine(
                maker_cfg=config.maker_config,
                ridge_cfg=config.ridge_config,
                order_sink=self._on_strategy_order,
            )
        else:  # BOTH
            # BOTH 模式：使用 Kalman 作为主引擎，同时运行 Ridge 用于对比
            self.engine = NordenMakerV3(
                maker_cfg=config.maker_config,
                kalman_cfg=config.kalman_config,
                order_sink=self._on_strategy_order,
            )
            self.ridge_model = OnlineRidge(config.ridge_config)
        
        # 缓存
        self.prices: Dict[str, Optional[float]] = {}
        self.last_dom: Optional[DomSnapshot] = None
        self.current_tick: Optional[TickEvent] = None
        
        # 性能统计
        self.event_count = 0
        self.signal_count = 0
        
        # 统计输出控制
        self.last_stats_time = time.time()
        self.last_stats_event_count = 0
        self.last_stats_signal_count = 0
    
    def _on_strategy_order(self, cmd: OrderCommand):
        """策略下单回调（信号拦截）"""
        if self.current_tick is None:
            return
        
        # 从引擎中提取上下文数据
        fair = self.engine.last_fair or 0.0
        spread_ticks = self.engine.last_spread_ticks or 0.0
        
        # OBI 和 Queue
        obi = 0.0
        queue_len = 0.0
        if self.engine.last_dom:
            obi = self.engine._calc_obi(self.engine.last_dom)
            if cmd.side == Side.BUY and self.engine.last_dom.bids:
                queue_len = self.engine.last_dom.bids[0][1]
            elif cmd.side == Side.SELL and self.engine.last_dom.asks:
                queue_len = self.engine.last_dom.asks[0][1]
        
        # BTC 状态
        btc_ratio = self.engine.btc_monitor.get_vol_ratio()
        
        side_str = 'BUY' if cmd.side == Side.BUY else 'SELL'
        
        # 使用当前市场价格作为 entry_price
        entry_price = self.current_tick.es if self.current_tick else cmd.price
        
        # 通知分析器
        self.analyzer.on_signal(
            tick=self.current_tick,
            side=side_str,
            price=entry_price,
            fair=fair,
            spread=spread_ticks,
            obi=obi,
            queue=queue_len,
            btc=btc_ratio,
        )
        
        self.signal_count += 1
    
    def run(self, duration_seconds: Optional[float] = None):
        """运行回测"""
        self.listener.start()
        
        start_time = time.time()
        
        try:
            while True:
                # 检查是否达到运行时长限制
                if duration_seconds and (time.time() - start_time) >= duration_seconds:
                    break
                
                batch_size = 0
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
                
                if batch_size == 0:
                    time.sleep(0.001)
                
                # 统计输出（简化版）
                now = time.time()
                if now - self.last_stats_time >= 5.0:  # 每5秒输出一次
                    events_per_sec = (self.event_count - self.last_stats_event_count) / (now - self.last_stats_time)
                    self.last_stats_time = now
                    self.last_stats_event_count = self.event_count
                    print(f"[{self.config.name}] 事件: {events_per_sec:.0f}/s | 信号: {self.signal_count}", flush=True)
        
        except KeyboardInterrupt:
            pass
        finally:
            self.listener.stop()
    
    def _handle_trade(self, event: Dict[str, Any]):
        """处理成交事件"""
        sym = event.get('symbol', '').strip().upper()
        price = float(event['price'])
        self.prices[sym] = price
        
        if sym == 'ES':
            tick = TickEvent(
                t_ms=_ticks_to_ms(event['ticks']),
                es=price,
                nq=self.prices.get('NQ'),
                ym=self.prices.get('YM') or self.prices.get('MYM'),
                btc=self.prices.get('BTCUSDT'),
            )
            
            self.current_tick = tick
            
            if tick.nq:
                self.engine.on_tick(tick)
                # BOTH 模式：同时更新 Ridge 模型（用于对比，但不用于信号生成）
                if self.ridge_model:
                    self.ridge_model.update(tick)
            
            self.analyzer.on_tick_update(price, tick.t_ms)
    
    def _handle_dom(self, event: Dict[str, Any]):
        """处理 DOM 事件"""
        if event['symbol'] != 'ES':
            return
        
        bids = _parse_dom(event.get('bids', ''))
        asks = _parse_dom(event.get('asks', ''))
        
        dom = DomSnapshot(
            t_ms=_ticks_to_ms(event['ticks']),
            best_bid=bids[0][0] if bids else 0,
            best_ask=asks[0][0] if asks else 0,
            bids=bids,
            asks=asks,
        )
        self.engine.on_dom(dom)
        self.last_dom = dom
    
    def get_result(self) -> BacktestResult:
        """获取回测结果"""
        summary = self.analyzer.get_result_summary()
        
        result = BacktestResult(config=self.config)
        result.total_signals = summary['total_signals']
        result.tp_count = summary['tp_count']
        result.sl_count = summary['sl_count']
        result.timeout_count = summary['timeout_count']
        result.avg_pnl = summary['avg_pnl']
        result.avg_mfe = summary['avg_mfe']
        result.avg_mae = summary['avg_mae']
        result.mfe_positive_count = summary['mfe_positive_count']
        result.mfe_zero_count = summary['mfe_zero_count']
        result.avg_duration = summary['avg_duration']
        result.min_duration = summary['min_duration']
        result.max_duration = summary['max_duration']
        result.immediate_sl_count = summary['immediate_sl_count']
        
        return result
    
    def save_detailed_report(self, filename_prefix: str = None):
        """保存详细报告"""
        prefix = filename_prefix or f"backtest_{self.config.name}"
        self.analyzer.save_report(prefix)


def print_comparison_report(results: List[BacktestResult]):
    """打印对比报告"""
    print("\n" + "="*100)
    print("📊 批量回测对比报告 (Batch Backtest Comparison)")
    print("="*100)
    
    # 表头
    header = (
        f"{'配置名称':<20} | "
        f"{'信号数':<8} | "
        f"{'胜率%':<8} | "
        f"{'败率%':<8} | "
        f"{'超时%':<8} | "
        f"{'平均PnL':<10} | "
        f"{'平均MFE':<10} | "
        f"{'平均MAE':<10} | "
        f"{'平均时长':<10}"
    )
    print(header)
    print("-" * 100)
    
    # 数据行
    for result in results:
        row = (
            f"{result.config.name:<20} | "
            f"{result.total_signals:<8} | "
            f"{result.win_rate():>6.1f}% | "
            f"{result.loss_rate():>6.1f}% | "
            f"{result.timeout_rate():>6.1f}% | "
            f"{result.avg_pnl:>8.2f}t | "
            f"{result.avg_mfe:>8.2f}t | "
            f"{result.avg_mae:>8.2f}t | "
            f"{result.avg_duration:>8.2f}s"
        )
        print(row)
    
    print("="*100 + "\n")


def create_test_configs() -> List[BacktestConfig]:
    """创建测试配置列表"""
    configs = []
    
    # 配置1：默认 Kalman
    configs.append(BacktestConfig(
        name="Kalman_默认2T3S",
        pricing_model=PricingModel.KALMAN,
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-3.0,
    ))

    configs.append(BacktestConfig(
        name="Kalman_默认2T5S",
        pricing_model=PricingModel.KALMAN,
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-5.0,
    ))
    configs.append(BacktestConfig(
        name="Kalman_默认1T3S",
        pricing_model=PricingModel.KALMAN,
        track_duration=10.0,
        tp_ticks=1.0,
        sl_ticks=-3.0,
    ))
    # 配置2：Kalman 保守（高阈值）
    configs.append(BacktestConfig(
        name="Kalman_保守",
        pricing_model=PricingModel.KALMAN,
        maker_config=MakerConfig(
            base_spread_threshold=1.0,
            min_obi_for_long=0.15,
        ),
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-4.0,
    ))
    
    # 配置3：Kalman 激进（低阈值）
    configs.append(BacktestConfig(
        name="Kalman_激进",
        pricing_model=PricingModel.KALMAN,
        maker_config=MakerConfig(
            base_spread_threshold=0.3,
            min_obi_for_long=0.05,
        ),
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-4.0,
    ))
    
    # 配置4：Ridge 默认
    configs.append(BacktestConfig(
        name="Ridge_默认",
        pricing_model=PricingModel.RIDGE,
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-4.0,
    ))
    
    # 配置5：Ridge 保守
    configs.append(BacktestConfig(
        name="Ridge_保守",
        pricing_model=PricingModel.RIDGE,
        maker_config=MakerConfig(
            base_spread_threshold=1.0,
            min_obi_for_long=0.15,
        ),
        track_duration=10.0,
        tp_ticks=2.0,
        sl_ticks=-4.0,
    ))
    
    return configs


if __name__ == "__main__":
    print("🚀 [BacktestSuite] 批量回测系统启动")
    print("   请在 ATAS 中开始回放数据...\n")
    
    # 创建测试配置
    configs = create_test_configs()
    print(f"📋 共 {len(configs)} 个配置待测试\n")
    
    # 运行每个配置的回测
    results = []
    
    for i, config in enumerate(configs, 1):
        print(f"\n{'='*80}")
        print(f"📊 [{i}/{len(configs)}] 运行配置: {config.name}")
        print(f"{'='*80}")
        
        runner = SingleBacktestRunner(config)
        
        try:
            # 每个配置运行 60 秒（可以根据需要调整）
            runner.run(duration_seconds=60.0)
        except KeyboardInterrupt:
            print(f"\n⚠️  配置 {config.name} 被中断")
        
        # 保存详细报告
        runner.save_detailed_report()
        
        # 获取结果
        result = runner.get_result()
        results.append(result)
        
        # 打印单个配置的摘要
        print(f"\n✅ [{config.name}] 完成")
        print(f"   信号数: {result.total_signals} | "
              f"胜率: {result.win_rate():.1f}% | "
              f"平均PnL: {result.avg_pnl:.2f}t")
    
    # 打印对比报告
    print_comparison_report(results)
    
    print("✅ 批量回测完成！")

