"""
并行批量回测套件启动脚本
========================

功能：
    1. 所有配置同时运行，使用相同的数据流
    2. 实时对比不同配置的表现
    3. 模型选择（Kalman/Ridge/Both）
    4. 参数对比和统计摘要
    5. 自动生成对比报告

关键改进：
    - 所有配置共享同一个数据源
    - 每次收到数据，同时传递给所有配置
    - 实现真正的并行对比
"""

import time
import queue
import sys
import socket
from typing import Dict, Any, Optional, List

from dom_data_feed import UdpListener, InstrumentState, UDP_PORT
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
    RidgeMakerEngine,
)
from norden_v3.backtest_analyzer import BacktestAnalyzer
from norden_v3 import BacktestConfig, BacktestResult, PricingModel

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


class ParallelBacktestSuite:
    """并行批量回测套件 - 所有配置同时运行"""
    
    def __init__(self, configs: List[BacktestConfig]):
        self.configs = configs
        
        # 统一的数据通道（所有配置共享）
        self.q = queue.Queue(maxsize=100000)
        self.listener = UdpListener(self.q)
        
        # 为每个配置创建独立的运行器
        self.runners: List[Dict[str, Any]] = []
        for config in configs:
            runner = self._create_runner(config)
            self.runners.append(runner)
        
        # 价格缓存（所有配置共享）
        self.prices: Dict[str, Optional[float]] = {}
        
        # 性能统计
        self.event_count = 0
        self.start_time = None
    
    def _check_port_available(self) -> bool:
        """检查 UDP 端口是否可用"""
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.bind(("0.0.0.0", UDP_PORT))
            test_sock.close()
            return True
        except OSError:
            return False
    
    def _create_runner(self, config: BacktestConfig) -> Dict[str, Any]:
        """为单个配置创建运行器"""
        # 分析器
        analyzer = BacktestAnalyzer(
            track_duration=config.track_duration,
            tick_size=config.tick_size,
            tp_ticks=config.tp_ticks,
            sl_ticks=config.sl_ticks,
        )
        
        # 策略引擎（根据配置选择模型）
        if config.pricing_model == PricingModel.KALMAN:
            engine = NordenMakerV3(
                maker_cfg=config.maker_config,
                kalman_cfg=config.kalman_config,
                order_sink=lambda cmd: self._on_strategy_order(config, analyzer, cmd),
            )
        elif config.pricing_model == PricingModel.RIDGE:
            engine = RidgeMakerEngine(
                maker_cfg=config.maker_config,
                ridge_cfg=config.ridge_config,
                order_sink=lambda cmd: self._on_strategy_order(config, analyzer, cmd),
            )
        else:  # BOTH
            engine = NordenMakerV3(
                maker_cfg=config.maker_config,
                kalman_cfg=config.kalman_config,
                order_sink=lambda cmd: self._on_strategy_order(config, analyzer, cmd),
            )
        
        return {
            'config': config,
            'engine': engine,
            'analyzer': analyzer,
            'current_tick': None,
            'signal_count': 0,
        }
    
    def _on_strategy_order(self, config: BacktestConfig, analyzer: BacktestAnalyzer, cmd: OrderCommand):
        """策略下单回调（信号拦截）"""
        # 找到对应的 runner
        runner = None
        for r in self.runners:
            if r['config'] == config:
                runner = r
                break
        
        if runner is None or runner['current_tick'] is None:
            return
        
        tick = runner['current_tick']
        engine = runner['engine']
        
        # 从引擎中提取上下文数据
        fair = engine.last_fair or 0.0
        spread_ticks = engine.last_spread_ticks or 0.0
        
        # OBI 和 Queue
        obi = 0.0
        queue_len = 0.0
        if engine.last_dom:
            obi = engine._calc_obi(engine.last_dom)
            if cmd.side == Side.BUY and engine.last_dom.bids:
                queue_len = engine.last_dom.bids[0][1]
            elif cmd.side == Side.SELL and engine.last_dom.asks:
                queue_len = engine.last_dom.asks[0][1]
        
        # BTC 状态
        btc_ratio = engine.btc_monitor.get_vol_ratio()
        
        side_str = 'BUY' if cmd.side == Side.BUY else 'SELL'
        
        # 使用当前市场价格作为 entry_price
        entry_price = tick.es if tick else cmd.price
        
        # 通知分析器
        analyzer.on_signal(
            tick=tick,
            side=side_str,
            price=entry_price,
            fair=fair,
            spread=spread_ticks,
            obi=obi,
            queue=queue_len,
            btc=btc_ratio,
        )
        
        runner['signal_count'] += 1
    
    def run(self, duration_seconds: Optional[float] = None):
        """并行运行所有配置的回测"""
        print("🚀 [ParallelBacktest] 启动并行回测...")
        print(f"   共 {len(self.configs)} 个配置将同时运行")
        print("   所有配置使用相同的数据流\n")
        
        # 检查端口是否可用（延迟检查，给之前的程序时间关闭）
        print("   检查 UDP 端口...", end=" ", flush=True)
        time.sleep(1.0)  # 等待 1 秒，让之前的程序有时间关闭
        
        if not self._check_port_available():
            print(f"❌ 端口 {UDP_PORT} 已被占用！")
            print(f"\n💡 解决方案：")
            print(f"   1. 检查是否有其他程序正在运行（回测程序、仪表盘、数据记录等）")
            print(f"   2. 关闭所有使用端口 {UDP_PORT} 的程序")
            print(f"   3. 等待几秒后重试")
            print(f"\n   如果问题持续，可以尝试：")
            print(f"   - 重启 Python 环境")
            print(f"   - 检查是否有残留进程：netstat -ano | findstr {UDP_PORT}")
            return
        else:
            print("✅ 可用")
        
        self.listener.start()
        
        # 等待监听器启动
        time.sleep(0.5)
        if not self.listener.is_alive():
            print(f"❌ UDP 监听器启动失败！", flush=True)
            return
        
        print("✅ UDP 监听器已启动，开始接收数据...\n", flush=True)
        self.start_time = time.time()
        
        last_stats_time = time.time()
        last_stats_event_count = 0
        last_listener_packets = 0
        
        try:
            while True:
                # 检查是否达到运行时长限制
                if duration_seconds and (time.time() - self.start_time) >= duration_seconds:
                    print(f"\n⏰ 已达到运行时长限制 ({duration_seconds}秒)，停止回测", flush=True)
                    break
                
                # 批量消费数据
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
                    except Exception as e:
                        # 捕获处理事件时的异常，避免程序崩溃
                        print(f"⚠️ [ERROR] 处理事件时出错: {e}", flush=True)
                        import traceback
                        traceback.print_exc()
                
                if batch_size == 0:
                    time.sleep(0.001)
                
                # 统计输出（每5秒一次）
                now = time.time()
                if now - last_stats_time >= 5.0:
                    events_per_sec = (self.event_count - last_stats_event_count) / (now - last_stats_time)
                    last_stats_time = now
                    last_stats_event_count = self.event_count
                    
                    signal_counts = [r['signal_count'] for r in self.runners]
                    total_signals = sum(signal_counts)
                    
                    # 检查 UDP 监听器状态
                    listener_alive = self.listener.is_alive() if hasattr(self.listener, 'is_alive') else True
                    listener_status = "✅" if listener_alive else "❌"
                    
                    elapsed = now - self.start_time
                    print(f"[STATS] 运行时长: {elapsed:.0f}s | {listener_status} 监听器 | "
                          f"事件: {events_per_sec:.0f}/s | 总信号: {total_signals} | "
                          f"队列: {self.q.qsize()}", flush=True)
                    
                    # 打印每个配置的信号数
                    for i, runner in enumerate(self.runners):
                        if runner['signal_count'] > 0:
                            print(f"  [{runner['config'].name}]: {runner['signal_count']} 信号", flush=True)
                    
                    # 如果监听器已停止，发出警告
                    if not listener_alive:
                        print(f"⚠️ [WARNING] UDP 监听器线程已停止！数据可能无法接收", flush=True)
        
        except KeyboardInterrupt:
            print("\n⚠️  回测被中断")
        except Exception as e:
            print(f"\n❌ [FATAL] 发生未预期的错误: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            print("\n🛑 正在停止 UDP 监听器...", flush=True)
            self.listener.stop()
            print("✅ UDP 监听器已停止", flush=True)
    
    def _handle_trade(self, event: Dict[str, Any]):
        """处理成交事件 - 同时传递给所有配置"""
        try:
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
                
                if tick.nq:
                    # 同时传递给所有配置的引擎
                    for runner in self.runners:
                        try:
                            runner['current_tick'] = tick
                            runner['engine'].on_tick(tick)
                        except Exception as e:
                            print(f"⚠️ [ERROR] 配置 {runner['config'].name} 处理 tick 时出错: {e}", flush=True)
                    
                    # 更新所有分析器
                    for runner in self.runners:
                        try:
                            if runner['current_tick']:
                                es_price = runner['current_tick'].es
                                if es_price:
                                    runner['analyzer'].on_tick_update(es_price, tick.t_ms)
                        except Exception as e:
                            print(f"⚠️ [ERROR] 配置 {runner['config'].name} 更新分析器时出错: {e}", flush=True)
        except Exception as e:
            print(f"⚠️ [ERROR] 处理成交事件时出错: {e}", flush=True)
    
    def _handle_dom(self, event: Dict[str, Any]):
        """处理 DOM 事件 - 同时传递给所有配置"""
        try:
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
            
            # 同时传递给所有配置的引擎
            for runner in self.runners:
                try:
                    runner['engine'].on_dom(dom)
                except Exception as e:
                    print(f"⚠️ [ERROR] 配置 {runner['config'].name} 处理 DOM 时出错: {e}", flush=True)
        except Exception as e:
            print(f"⚠️ [ERROR] 处理 DOM 事件时出错: {e}", flush=True)
    
    def get_results(self) -> List[BacktestResult]:
        """获取所有配置的回测结果"""
        results = []
        for runner in self.runners:
            summary = runner['analyzer'].get_result_summary()
            config = runner['config']
            
            result = BacktestResult(config=config)
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
            
            results.append(result)
        
        return results
    
    def save_all_reports(self, filename_prefix: str = "parallel_backtest"):
        """保存所有配置的详细报告"""
        for runner in self.runners:
            config_name = runner['config'].name.replace(' ', '_')
            prefix = f"{filename_prefix}_{config_name}"
            runner['analyzer'].save_report(prefix)


def print_comparison_report(results: List[BacktestResult]):
    """打印对比报告"""
    print("\n" + "="*100)
    print("📊 并行回测对比报告 (Parallel Backtest Comparison)")
    print("="*100)
    
    # 表头
    header = (
        f"{'配置名称':<25} | "
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
            f"{result.config.name:<25} | "
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
    """创建测试配置列表（从 run_backtest_suite.py 复制）"""
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
    print("🚀 [ParallelBacktestSuite] 并行批量回测系统启动")
    print("   请在 ATAS 中开始回放数据...\n")
    
    # 创建测试配置
    configs = create_test_configs()
    print(f"📋 共 {len(configs)} 个配置将同时运行\n")
    
    # 创建并行回测套件
    suite = ParallelBacktestSuite(configs)
    
    try:
        # 运行并行回测
        # duration_seconds=None 表示持续运行直到手动中断
        # 也可以设置具体时长，例如: duration_seconds=300.0 (5分钟)
        print("💡 提示：按 Ctrl+C 手动停止回测\n")
        suite.run(duration_seconds=None)  # 持续运行
    except KeyboardInterrupt:
        print("\n⚠️  并行回测被中断")
    
    # 保存所有详细报告
    print("\n📝 正在生成详细报告...")
    suite.save_all_reports()
    
    # 获取结果并打印对比报告
    results = suite.get_results()
    print_comparison_report(results)
    
    print("✅ 并行批量回测完成！")

