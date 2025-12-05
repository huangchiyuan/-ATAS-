"""
Norden v3.1 回测分析器 (Signal Analyzer)
========================================

功能：
    1. 监听策略发出的下单信号 (Signal Snapshot)
    2. 追踪信号发出后 N 秒内的价格走势 (Outcome Tracking)
    3. 计算 MFE (最大潜在利润) 和 MAE (最大潜在亏损)
    4. 导出 CSV 供 Excel/Python 进一步统计

核心指标：
    - MFE (Max Favorable Excursion): 信号发出后，价格往有利方向跑了多少 tick (代表最大获利潜力)
    - MAE (Max Adverse Excursion): 信号发出后，价格往不利方向跑了多少 tick (代表最大回撤风险)
    - Win Rate (理论胜率): 检查是否触及 +4 ticks 止盈
"""

import time
import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from .types import TickEvent, Side


@dataclass
class SignalRecord:
    """单次交易信号的完整记录（包含因与果）"""
    
    # --- 1. 信号时刻快照 (The "Cause") ---
    signal_id: int
    timestamp: int          # 毫秒时间戳
    time_str: str           # 可读时间 (HH:MM:SS)
    side: str               # 'BUY' / 'SELL'
    entry_price: float      # 信号触发时的市场价 (或挂单价)
    
    # 环境参数
    fair_price: float       # 当时的理论价
    spread_ticks: float     # 当时的价差
    obi: float              # 当时的 OBI
    queue_len: float        # 当时的队列长度
    btc_ratio: float        # 当时的 BTC 波动率比率
    
    # --- 2. 结果追踪 (The "Effect") ---
    # 追踪窗口：30秒
    duration_s: float = 0.0 # 实际追踪时长
    
    # 价格变动 (Ticks)
    pnl_1s: float = 0.0     # 1秒后的浮盈亏 (tick)
    pnl_5s: float = 0.0
    pnl_10s: float = 0.0
    pnl_30s: float = 0.0
    
    # 极值统计
    mfe_ticks: float = -99.0 # Max Favorable (最大浮盈)
    mae_ticks: float = 99.0  # Max Adverse (最大浮亏)
    
    # 结果判定
    hit_tp: bool = False    # 是否触及止盈 (+4 ticks)
    hit_sl: bool = False    # 是否触及止损 (-6 ticks)
    
    # 内部状态 (不导出)
    _start_timestamp_ms: int = 0  # 历史时间戳（毫秒），用于计算经过时间
    _is_closed: bool = False


class BacktestAnalyzer:
    def __init__(
        self,
        track_duration: float = 30.0,
        tick_size: float = 0.25,
        tp_ticks: float = 2.0,
        sl_ticks: float = -3.0,
    ):
        """
        :param track_duration: 每个信号追踪多少秒 (默认 30秒)
        :param tick_size: 最小跳动点数 (ES=0.25)
        :param tp_ticks: 虚拟止盈点数（单位：tick）
        :param sl_ticks: 虚拟止损点数（单位：tick，负数）
        """
        self.track_duration = track_duration
        self.tick_size = tick_size
        
        self.records: List[SignalRecord] = []
        self.active_trackers: List[SignalRecord] = []
        self.signal_counter = 0
        
        # 止盈止损设置 (仅用于统计胜率，不影响策略)
        self.tp_ticks = tp_ticks
        self.sl_ticks = sl_ticks if sl_ticks < 0 else -abs(sl_ticks)

    def on_signal(
        self,
        tick: TickEvent,
        side: str,
        price: float,
        fair: float,
        spread: float,
        obi: float,
        queue: float,
        btc: float,
    ):
        """
        当策略发出下单指令时调用，创建一个新的追踪器
        """
        self.signal_counter += 1
        
        # 转换为可读时间字符串
        try:
            time_str = time.strftime("%H:%M:%S", time.localtime(tick.t_ms / 1000))
        except:
            time_str = "--:--:--"
        
        rec = SignalRecord(
            signal_id=self.signal_counter,
            timestamp=tick.t_ms,
            time_str=time_str,
            side=side,
            entry_price=price,
            fair_price=fair,
            spread_ticks=spread,
            obi=obi,
            queue_len=queue,
            btc_ratio=btc,
            _start_timestamp_ms=tick.t_ms,  # 使用历史时间戳作为基准
        )
        
        # 初始状态：MFE/MAE 从 0 开始（因为刚入场时盈亏为 0）
        rec.mae_ticks = 0.0
        rec.mfe_ticks = 0.0
        
        self.active_trackers.append(rec)
        self.records.append(rec)
        
        # print(f"  [ANALYZER] 开始追踪信号 #{rec.signal_id} ({side} @ {price})")

    def on_tick_update(self, current_price: float, current_timestamp_ms: int):
        """
        收到新的行情时，更新所有活跃的追踪器
        
        :param current_price: 当前价格
        :param current_timestamp_ms: 当前tick的历史时间戳（毫秒）
        """
        # 倒序遍历，方便安全移除已结束的追踪器
        for i in range(len(self.active_trackers) - 1, -1, -1):
            rec = self.active_trackers[i]
            
            # 1. 计算时间经过（使用历史时间戳，支持倍速回放）
            elapsed_ms = current_timestamp_ms - rec._start_timestamp_ms
            elapsed = elapsed_ms / 1000.0  # 转换为秒
            
            # 防止时间计算错误（如果时间戳有问题，可能导致负数或异常大的值）
            if elapsed < 0:
                # 时间戳可能有问题，跳过本次更新（可能是同一时刻的信号和价格更新）
                continue
            if elapsed > self.track_duration * 2:
                # 时间异常大，可能是时间戳错误，关闭追踪器
                rec._is_closed = True
                self.active_trackers.pop(i)
                continue
            
            # 如果时间戳相同或非常接近（< 1ms），设置为最小时间间隔
            if elapsed < 0.001:
                elapsed = 0.001  # 至少 1 毫秒
                
            rec.duration_s = elapsed
            
            # 2. 计算当前浮动盈亏 (Ticks)
            if rec.side == 'BUY':
                diff = current_price - rec.entry_price
            else:
                diff = rec.entry_price - current_price
                
            pnl_ticks = diff / self.tick_size
            
            # 3. 更新极值 (MFE / MAE)
            if pnl_ticks > rec.mfe_ticks:
                rec.mfe_ticks = pnl_ticks
            if pnl_ticks < rec.mae_ticks:
                rec.mae_ticks = pnl_ticks
                
            # 4. 检查虚拟止盈止损
            if pnl_ticks >= self.tp_ticks:
                rec.hit_tp = True
            if pnl_ticks <= self.sl_ticks:
                rec.hit_sl = True
            
            # 5. 记录关键时间点快照（只在第一次达到时记录）
            # 注意：使用 <= 0.0 判断是否未记录，避免精度问题
            if elapsed >= 1.0 and abs(rec.pnl_1s) < 1e-6:
                rec.pnl_1s = pnl_ticks
            if elapsed >= 5.0 and abs(rec.pnl_5s) < 1e-6:
                rec.pnl_5s = pnl_ticks
            if elapsed >= 10.0 and abs(rec.pnl_10s) < 1e-6:
                rec.pnl_10s = pnl_ticks
            if elapsed >= 30.0 and abs(rec.pnl_30s) < 1e-6:
                rec.pnl_30s = pnl_ticks
            
            # 6. 结束追踪条件
            # 时间到 OR 止盈 OR 止损 (模拟真实交易结束)
            if elapsed >= self.track_duration or rec.hit_tp or rec.hit_sl:
                rec._is_closed = True
                self.active_trackers.pop(i)

    def save_report(self, filename_prefix: str = "backtest"):
        """导出分析报告"""
        if not self.records:
            print("⚠️ 未记录到任何信号，无法生成报告。")
            return
            
        # 转为 DataFrame
        data = [asdict(r) for r in self.records]
        df = pd.DataFrame(data)
        
        # 移除内部字段
        cols_to_drop = [c for c in df.columns if c.startswith('_')]
        df = df.drop(columns=cols_to_drop)
        
        # 生成文件名 (带时间戳)
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{filename_prefix}_{ts_str}.csv"
        
        df.to_csv(filename, index=False, encoding='utf-8-sig')  # 使用 utf-8-sig 以支持 Excel 正确显示中文
        print(f"\n✅ [ANALYZER] 详细报告已保存至: {filename}")
        
        # --- 打印统计摘要 ---
        print("\n" + "="*40)
        print("📊 回测统计摘要 (Simulation Summary)")
        print("="*40)
        print(f"总信号数: {len(df)}")
        
        # 胜率 (触及止盈 vs 触及止损)
        tp_count = len(df[df['hit_tp'] == True])
        sl_count = len(df[df['hit_sl'] == True])
        timeout_count = len(df) - tp_count - sl_count
        
        if len(df) > 0:
            print(f"止盈单数 (+{self.tp_ticks}t): {tp_count} ({tp_count/len(df)*100:.1f}%)")
            print(f"止损单数 ({self.sl_ticks}t): {sl_count} ({sl_count/len(df)*100:.1f}%)")
            print(f"超时平仓: {timeout_count} ({timeout_count/len(df)*100:.1f}%)")
        
        # 盈亏期望
        # 超时单以追踪结束时的价格平仓
        # 根据追踪时长选择对应的 pnl，如果没有则使用最接近的时间点
        timeout_pnl_col = None
        if self.track_duration >= 30:
            timeout_pnl_col = 'pnl_30s'
        elif self.track_duration >= 10:
            timeout_pnl_col = 'pnl_10s'
        elif self.track_duration >= 5:
            timeout_pnl_col = 'pnl_5s'
        else:
            timeout_pnl_col = 'pnl_1s'
        
        df['final_pnl'] = np.where(
            df['hit_tp'], self.tp_ticks,
            np.where(df['hit_sl'], self.sl_ticks, df[timeout_pnl_col].fillna(0.0))
        )
        
        avg_pnl = df['final_pnl'].mean()
        print(f"平均每单盈亏: {avg_pnl:.2f} ticks")
        
        # MFE/MAE 分析
        if len(df) > 0:
            print(f"平均 MFE (最大潜盈): {df['mfe_ticks'].mean():.2f} ticks")
            print(f"平均 MAE (最大潜亏): {df['mae_ticks'].mean():.2f} ticks")
            
            # 诊断信息
            mfe_positive = len(df[df['mfe_ticks'] > 0])
            mfe_zero = len(df[df['mfe_ticks'] == 0])
            print(f"MFE > 0 的信号数: {mfe_positive} ({mfe_positive/len(df)*100:.1f}%)")
            print(f"MFE = 0 的信号数: {mfe_zero} ({mfe_zero/len(df)*100:.1f}%)")
            
            # 检查是否有追踪时长异常的信号
            avg_duration = df['duration_s'].mean()
            min_duration = df['duration_s'].min()
            max_duration = df['duration_s'].max()
            print(f"平均追踪时长: {avg_duration:.2f} 秒 (范围: {min_duration:.2f} - {max_duration:.2f})")
            
            # 检查是否有立即止损的信号
            immediate_sl = len(df[df['duration_s'] < 0.1])
            if immediate_sl > 0:
                print(f"⚠️ 警告: {immediate_sl} 个信号在 0.1 秒内触发止损（可能是价格立即下跌）")
        
        print("="*40 + "\n")
    
    def get_result_summary(self) -> Dict[str, Any]:
        """
        提取回测结果摘要（用于批量对比）
        
        Returns:
            包含所有统计指标的字典
        """
        if not self.records:
            return {
                'total_signals': 0,
                'tp_count': 0,
                'sl_count': 0,
                'timeout_count': 0,
                'avg_pnl': 0.0,
                'avg_mfe': 0.0,
                'avg_mae': 0.0,
                'mfe_positive_count': 0,
                'mfe_zero_count': 0,
                'avg_duration': 0.0,
                'min_duration': 0.0,
                'max_duration': 0.0,
                'immediate_sl_count': 0,
            }
        
        data = [asdict(r) for r in self.records]
        df = pd.DataFrame(data)
        
        # 移除内部字段
        cols_to_drop = [c for c in df.columns if c.startswith('_')]
        df = df.drop(columns=cols_to_drop)
        
        tp_count = len(df[df['hit_tp'] == True])
        sl_count = len(df[df['hit_sl'] == True])
        timeout_count = len(df) - tp_count - sl_count
        
        # 计算最终盈亏
        timeout_pnl_col = None
        if self.track_duration >= 30:
            timeout_pnl_col = 'pnl_30s'
        elif self.track_duration >= 10:
            timeout_pnl_col = 'pnl_10s'
        elif self.track_duration >= 5:
            timeout_pnl_col = 'pnl_5s'
        else:
            timeout_pnl_col = 'pnl_1s'
        
        df['final_pnl'] = np.where(
            df['hit_tp'], self.tp_ticks,
            np.where(df['hit_sl'], self.sl_ticks, df[timeout_pnl_col].fillna(0.0))
        )
        
        return {
            'total_signals': len(df),
            'tp_count': tp_count,
            'sl_count': sl_count,
            'timeout_count': timeout_count,
            'avg_pnl': float(df['final_pnl'].mean()),
            'avg_mfe': float(df['mfe_ticks'].mean()),
            'avg_mae': float(df['mae_ticks'].mean()),
            'mfe_positive_count': len(df[df['mfe_ticks'] > 0]),
            'mfe_zero_count': len(df[df['mfe_ticks'] == 0]),
            'avg_duration': float(df['duration_s'].mean()),
            'min_duration': float(df['duration_s'].min()),
            'max_duration': float(df['duration_s'].max()),
            'immediate_sl_count': len(df[df['duration_s'] < 0.1]),
        }
    
    def get_result_summary(self) -> Dict[str, Any]:
        """
        提取回测结果摘要（用于批量对比）
        
        Returns:
            包含所有统计指标的字典
        """
        if not self.records:
            return {
                'total_signals': 0,
                'tp_count': 0,
                'sl_count': 0,
                'timeout_count': 0,
                'avg_pnl': 0.0,
                'avg_mfe': 0.0,
                'avg_mae': 0.0,
                'mfe_positive_count': 0,
                'mfe_zero_count': 0,
                'avg_duration': 0.0,
                'min_duration': 0.0,
                'max_duration': 0.0,
                'immediate_sl_count': 0,
            }
        
        import pandas as pd
        from dataclasses import asdict
        
        data = [asdict(r) for r in self.records]
        df = pd.DataFrame(data)
        
        # 移除内部字段
        cols_to_drop = [c for c in df.columns if c.startswith('_')]
        df = df.drop(columns=cols_to_drop)
        
        tp_count = len(df[df['hit_tp'] == True])
        sl_count = len(df[df['hit_sl'] == True])
        timeout_count = len(df) - tp_count - sl_count
        
        # 计算最终盈亏
        timeout_pnl_col = None
        if self.track_duration >= 30:
            timeout_pnl_col = 'pnl_30s'
        elif self.track_duration >= 10:
            timeout_pnl_col = 'pnl_10s'
        elif self.track_duration >= 5:
            timeout_pnl_col = 'pnl_5s'
        else:
            timeout_pnl_col = 'pnl_1s'
        
        df['final_pnl'] = pd.Series(
            np.where(
                df['hit_tp'], self.tp_ticks,
                np.where(df['hit_sl'], self.sl_ticks, df[timeout_pnl_col].fillna(0.0))
            )
        )
        
        return {
            'total_signals': len(df),
            'tp_count': tp_count,
            'sl_count': sl_count,
            'timeout_count': timeout_count,
            'avg_pnl': float(df['final_pnl'].mean()),
            'avg_mfe': float(df['mfe_ticks'].mean()),
            'avg_mae': float(df['mae_ticks'].mean()),
            'mfe_positive_count': len(df[df['mfe_ticks'] > 0]),
            'mfe_zero_count': len(df[df['mfe_ticks'] == 0]),
            'avg_duration': float(df['duration_s'].mean()),
            'min_duration': float(df['duration_s'].min()),
            'max_duration': float(df['duration_s'].max()),
            'immediate_sl_count': len(df[df['duration_s'] < 0.1]),
        }

