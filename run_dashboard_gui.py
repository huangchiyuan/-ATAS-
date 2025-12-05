"""
Norden Engine v3.1 策略驾驶舱 (Strategy Dashboard)
=========================================
功能：
1. 可视化 Spread 柱状图与动态阈值（Kalman + Ridge 双模型对比）
2. 红绿灯式的多重过滤状态显示 (BTC, OBI, ICE, Kalman, Ridge)
3. 实时参数调整面板 (热更新策略参数，包括 Kalman 和 Ridge)
4. 最终交易指令的大字提示
5. 价格显示面板 (ES, NQ, YM, BTC)

使用方法：
    python run_dashboard_gui.py

依赖：
    pip install PyQt6 pyqtgraph
"""

import sys
import queue
import time
from typing import Dict, Any, Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QGroupBox, QDoubleSpinBox, QFrame, QGridLayout, QSplitter
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QColor

import pyqtgraph as pg
import numpy as np

# 引入核心模块
from dom_data_feed import UdpListener, InstrumentState, TICKS_AT_EPOCH, PRICE_TICK
from norden_v3 import (
    NordenMakerV3, MakerConfig, KalmanConfig, RidgeConfig,
    OnlineRidge,
    TickEvent, DomSnapshot
)


def ticks_to_ms(ticks_str: str) -> int:
    """将 C# ticks（.NET Ticks）转换为毫秒时间戳."""
    try:
        ticks = int(ticks_str)
        us = (ticks - TICKS_AT_EPOCH) // 10
        return int(us // 1000)
    except:
        return int(time.time() * 1000)


# ============================================================================
# UI 组件：状态指示灯
# ============================================================================

class StatusLight(QFrame):
    """红绿灯式状态指示灯."""
    
    def __init__(self, label_text: str):
        super().__init__()
        self.setFrameShape(QFrame.Shape.Box)
        self.setFixedSize(120, 90)
        self.setStyleSheet("border-radius: 5px;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # 标题
        self.lbl_title = QLabel(label_text)
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_title.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.lbl_title.setStyleSheet("color: white;")
        
        # 数值/状态
        self.lbl_val = QLabel("--")
        self.lbl_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_val.setFont(QFont("Arial", 12))
        self.lbl_val.setStyleSheet("color: white;")
        
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_val)
        
        # 初始状态：灰色
        self.set_status("GRAY", "--")

    def set_status(self, color_code: str, text: str):
        """
        设置状态颜色和文本.
        
        Args:
            color_code: 'GREEN', 'RED', 'GRAY', 'YELLOW'
            text: 显示的文本
        """
        colors = {
            'GREEN': '#2E7D32',    # 深绿
            'RED': '#C62828',      # 深红
            'YELLOW': '#F9A825',   # 黄色
            'GRAY': '#424242'      # 灰色
        }
        bg = colors.get(color_code, '#424242')
        self.setStyleSheet(
            f"background-color: {bg}; "
            f"border-radius: 5px; "
            f"border: 2px solid {colors.get(color_code, '#424242')};"
        )
        self.lbl_val.setText(str(text))


# ============================================================================
# 主窗口
# ============================================================================

class StrategyDashboard(QMainWindow):
    """策略驾驶舱主窗口."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Norden v3.1 策略驾驶舱 (Strategy Dashboard)")
        self.resize(1400, 900)
        
        # 初始化后台策略
        self.init_strategy()
        
        # 初始化 UI
        self.init_ui()
        
        # 启动定时器 (30ms 刷新一次 UI)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_loop)
        self.timer.start(30)  # ~33 FPS

    def init_strategy(self):
        """初始化策略引擎和数据接收."""
        # 数据队列
        self.q = queue.Queue(maxsize=50000)
        self.listener = UdpListener(self.q)
        self.listener.start()
        
        # 策略引擎配置
        self.maker_cfg = MakerConfig(
            base_spread_threshold=0.75,
            min_obi_for_long=0.1,
            min_obi_for_short=0.1
        )
        self.kalman_cfg = KalmanConfig(
            r_obs=1.0,      # 归一化后，误差项在 -2~+2 点范围，R 应该匹配（默认 1.0）
            q_beta=1e-8     # 归一化后，数据量级变小，Q 可以适当增大（默认 1e-8）
        )
        
        # 岭回归配置
        self.ridge_cfg = RidgeConfig(
            lambda_factor=0.995,
            ridge_alpha=1e-4
        )
        
        self.engine = NordenMakerV3(
            maker_cfg=self.maker_cfg,
            kalman_cfg=self.kalman_cfg
        )
        
        # 独立的岭回归模型（用于对比）
        self.ridge_model = OnlineRidge(self.ridge_cfg)
        
        # Ridge 模型状态
        self.ridge_fair: Optional[float] = None
        self.ridge_spread: Optional[float] = None
        self.ridge_spread_ticks: Optional[float] = None
        
        # 价格缓存
        self.prices = {
            'ES': None,
            'NQ': None,
            'YM': None,
            'MYM': None,  # 兼容 YM 的别名
            'BTCUSDT': None
        }
        
        # DOM 状态管理（用于解析 DOM 数据）
        self.instruments = {
            'ES': InstrumentState('ES'),
            'NQ': InstrumentState('NQ'),
            'YM': InstrumentState('YM'),
        }
        
        # 实时数据（不保留历史）
        self.current_spread_kalman: Optional[float] = None
        self.current_spread_ridge: Optional[float] = None

    def init_ui(self):
        """初始化用户界面."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        
        # ========== 区域 1: 顶部指令区 (The Action) ==========
        action_frame = QFrame()
        action_frame.setFixedHeight(100)
        action_layout = QVBoxLayout(action_frame)
        
        self.lbl_action = QLabel("等待数据中... (WAITING FOR DATA...)")
        self.lbl_action.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_action.setFont(QFont("Arial", 28, QFont.Weight.Bold))
        self.lbl_action.setStyleSheet(
            "background-color: #212121; "
            "color: #9E9E9E; "
            "border-radius: 10px; "
            "padding: 10px;"
        )
        action_layout.addWidget(self.lbl_action)
        main_layout.addWidget(action_frame)
        
        # ========== 区域 2: 价格显示面板 ==========
        price_group = QGroupBox("价格面板")
        price_layout = QHBoxLayout()
        
        self.lbl_es = QLabel("ES: --")
        self.lbl_nq = QLabel("NQ: --")
        self.lbl_ym = QLabel("YM: --")
        self.lbl_btc = QLabel("BTC: --")
        
        for lbl in [self.lbl_es, self.lbl_nq, self.lbl_ym, self.lbl_btc]:
            lbl.setFont(QFont("Consolas", 14))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("padding: 5px; background-color: #1E1E1E; color: #E0E0E0; border-radius: 5px;")
            price_layout.addWidget(lbl)
        
        price_group.setLayout(price_layout)
        main_layout.addWidget(price_group)
        
        # ========== 区域 3: 过滤器状态矩阵 ==========
        filter_group = QGroupBox("过滤器状态 (Filters & Logic State)")
        filter_layout = QHBoxLayout()
        
        # 核心信号灯
        self.light_model = StatusLight("Kalman Spread")
        self.light_ridge = StatusLight("Ridge Spread")
        self.light_obi = StatusLight("OBI Flow")
        self.light_ice = StatusLight("Iceberg")
        self.light_btc = StatusLight("BTC Risk")
        
        filter_layout.addWidget(self.light_model)
        filter_layout.addWidget(self.light_ridge)
        filter_layout.addWidget(self.light_obi)
        filter_layout.addWidget(self.light_ice)
        filter_layout.addWidget(self.light_btc)
        filter_layout.addStretch()
        
        filter_group.setLayout(filter_layout)
        main_layout.addWidget(filter_group)
        
        # ========== 区域 4: 实时能量柱显示 ==========
        energy_group = QGroupBox("实时 Spread 能量柱")
        energy_layout = QHBoxLayout()
        
        # Kalman 能量柱图表
        kalman_chart = QWidget()
        kalman_layout = QVBoxLayout(kalman_chart)
        kalman_label = QLabel("Kalman Spread")
        kalman_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        kalman_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        kalman_label.setStyleSheet("color: #FFD700;")
        
        self.plot_kalman = pg.PlotWidget()
        self.plot_kalman.setBackground('#1e1e1e')
        self.plot_kalman.hideAxis('bottom')  # 隐藏 X 轴（实时显示不需要时间轴）
        self.plot_kalman.setLabel('left', 'Spread (Ticks)', color='#E0E0E0')
        self.plot_kalman.setYRange(-4, 4)
        self.plot_kalman.setFixedHeight(200)
        self.plot_kalman.setMouseEnabled(x=False, y=False)  # 禁用缩放
        self.plot_kalman.showGrid(x=False, y=True, alpha=0.3)  # 只显示 Y 轴网格
        
        # 0 轴参考线
        self.plot_kalman.addItem(
            pg.InfiniteLine(
                pos=0, angle=0,
                pen=pg.mkPen('#757575', width=2, style=Qt.PenStyle.DashLine)
            )
        )
        
        # 阈值线
        self.line_upper_kalman = pg.InfiniteLine(
            pos=0.75, angle=0,
            pen=pg.mkPen('#00E676', width=2, style=Qt.PenStyle.DashLine)
        )
        self.line_lower_kalman = pg.InfiniteLine(
            pos=-0.75, angle=0,
            pen=pg.mkPen('#FF5252', width=2, style=Qt.PenStyle.DashLine)
        )
        self.plot_kalman.addItem(self.line_upper_kalman)
        self.plot_kalman.addItem(self.line_lower_kalman)
        
        # Kalman 能量柱（实时单柱）
        self.bar_kalman = pg.BarGraphItem(
            x=[0], height=[0], width=0.5,
            brush=pg.mkBrush('#FFD700', alpha=255),
            pen=pg.mkPen('#FFD700', width=2)
        )
        self.plot_kalman.addItem(self.bar_kalman)
        
        kalman_layout.addWidget(kalman_label)
        kalman_layout.addWidget(self.plot_kalman)
        energy_layout.addWidget(kalman_chart, stretch=1)
        
        # Ridge 能量柱图表
        ridge_chart = QWidget()
        ridge_layout = QVBoxLayout(ridge_chart)
        ridge_label = QLabel("Ridge Spread")
        ridge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ridge_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        ridge_label.setStyleSheet("color: #00BCD4;")
        
        self.plot_ridge = pg.PlotWidget()
        self.plot_ridge.setBackground('#1e1e1e')
        self.plot_ridge.hideAxis('bottom')  # 隐藏 X 轴（实时显示不需要时间轴）
        self.plot_ridge.setLabel('left', 'Spread (Ticks)', color='#E0E0E0')
        self.plot_ridge.setYRange(-4, 4)
        self.plot_ridge.setFixedHeight(200)
        self.plot_ridge.setMouseEnabled(x=False, y=False)  # 禁用缩放
        self.plot_ridge.showGrid(x=False, y=True, alpha=0.3)  # 只显示 Y 轴网格
        
        # 0 轴参考线
        self.plot_ridge.addItem(
            pg.InfiniteLine(
                pos=0, angle=0,
                pen=pg.mkPen('#757575', width=2, style=Qt.PenStyle.DashLine)
            )
        )
        
        # 阈值线
        self.line_upper_ridge = pg.InfiniteLine(
            pos=0.75, angle=0,
            pen=pg.mkPen('#00E676', width=2, style=Qt.PenStyle.DashLine)
        )
        self.line_lower_ridge = pg.InfiniteLine(
            pos=-0.75, angle=0,
            pen=pg.mkPen('#FF5252', width=2, style=Qt.PenStyle.DashLine)
        )
        self.plot_ridge.addItem(self.line_upper_ridge)
        self.plot_ridge.addItem(self.line_lower_ridge)
        
        # Ridge 能量柱（实时单柱）
        self.bar_ridge = pg.BarGraphItem(
            x=[0], height=[0], width=0.5,
            brush=pg.mkBrush('#00BCD4', alpha=255),
            pen=pg.mkPen('#00BCD4', width=2)
        )
        self.plot_ridge.addItem(self.bar_ridge)
        
        ridge_layout.addWidget(ridge_label)
        ridge_layout.addWidget(self.plot_ridge)
        energy_layout.addWidget(ridge_chart, stretch=1)
        
        energy_group.setLayout(energy_layout)
        main_layout.addWidget(energy_group, stretch=1)
        
        # ========== 区域 5: 参数调整面板 ==========
        param_group = QGroupBox("实时参数调整 (Live Parameter Tuning)")
        param_layout = QGridLayout()
        
        # 1. Spread Threshold
        param_layout.addWidget(QLabel("Spread 阈值 (Ticks):"), 0, 0)
        self.spin_spread = QDoubleSpinBox()
        self.spin_spread.setRange(0.25, 5.0)
        self.spin_spread.setSingleStep(0.25)
        self.spin_spread.setValue(0.75)
        self.spin_spread.setDecimals(2)
        self.spin_spread.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_spread, 0, 1)
        
        # 2. Kalman R (观测噪声)
        param_layout.addWidget(QLabel("Kalman R (观测噪声):"), 0, 2)
        self.spin_r = QDoubleSpinBox()
        self.spin_r.setRange(0.1, 10.0)  # 归一化后，R 范围应该匹配误差项量级
        self.spin_r.setSingleStep(0.1)
        self.spin_r.setValue(1.0)  # 归一化后的默认值
        self.spin_r.setDecimals(1)
        self.spin_r.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_r, 0, 3)
        
        # 3. OBI Threshold
        param_layout.addWidget(QLabel("最小 OBI:"), 1, 0)
        self.spin_obi = QDoubleSpinBox()
        self.spin_obi.setRange(0.0, 0.8)
        self.spin_obi.setSingleStep(0.05)
        self.spin_obi.setValue(0.1)
        self.spin_obi.setDecimals(2)
        self.spin_obi.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_obi, 1, 1)
        
        # 4. Kalman Q Beta
        param_layout.addWidget(QLabel("Kalman Q Beta:"), 1, 2)
        self.spin_q_beta = QDoubleSpinBox()
        # 注意：由于 QDoubleSpinBox 对极小值的显示有限制，这里使用对数形式输入
        # 实际值为 10^input，所以输入 -12 表示 1e-12
        self.spin_q_beta.setRange(-15, -6)  # 归一化后，范围可扩展
        self.spin_q_beta.setSingleStep(1)
        self.spin_q_beta.setValue(-8)  # 表示 1e-8（归一化后的默认值）
        self.spin_q_beta.setDecimals(0)
        self.spin_q_beta.setSuffix(" (10^N)")
        self.spin_q_beta.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_q_beta, 1, 3)
        
        # 5. Ridge Lambda（遗忘因子）
        param_layout.addWidget(QLabel("Ridge Lambda (遗忘因子):"), 2, 0)
        self.spin_ridge_lambda = QDoubleSpinBox()
        self.spin_ridge_lambda.setRange(0.99, 0.999)
        self.spin_ridge_lambda.setSingleStep(0.001)
        self.spin_ridge_lambda.setValue(0.995)
        self.spin_ridge_lambda.setDecimals(3)
        self.spin_ridge_lambda.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_ridge_lambda, 2, 1)
        
        # 6. Ridge Alpha（惩罚系数）
        param_layout.addWidget(QLabel("Ridge Alpha (惩罚系数):"), 2, 2)
        self.spin_ridge_alpha = QDoubleSpinBox()
        self.spin_ridge_alpha.setRange(1e-5, 1e-2)
        self.spin_ridge_alpha.setSingleStep(1e-4)
        self.spin_ridge_alpha.setValue(1e-4)
        self.spin_ridge_alpha.setDecimals(5)
        self.spin_ridge_alpha.valueChanged.connect(self.update_params)
        param_layout.addWidget(self.spin_ridge_alpha, 2, 3)
        
        param_group.setLayout(param_layout)
        main_layout.addWidget(param_group)

    def update_params(self):
        """实时更新策略参数."""
        # 1. Spread 阈值
        new_th = self.spin_spread.value()
        self.engine.cfg.base_spread_threshold = new_th
        
        # 更新图表上的阈值线（两个图表都需要更新）
        self.line_upper_kalman.setPos(new_th)
        self.line_lower_kalman.setPos(-new_th)
        self.line_upper_ridge.setPos(new_th)
        self.line_lower_ridge.setPos(-new_th)
        
        # 2. Kalman R（实时更新观测噪声）
        new_r = self.spin_r.value()
        self.kalman_cfg.r_obs = new_r
        # Kalman 模型有 R 属性，可以直接修改
        self.engine.kalman.R = new_r
        
        # 3. OBI 阈值
        self.engine.cfg.min_obi_for_long = self.spin_obi.value()
        self.engine.cfg.min_obi_for_short = self.spin_obi.value()
        
        # 4. Kalman Q Beta（使用对数形式：输入 -12 表示 1e-12）
        # 需要重启模型才能完全生效，这里先更新配置
        log_value = self.spin_q_beta.value()
        actual_value = 10.0 ** log_value
        self.kalman_cfg.q_beta = actual_value
        
        # 5. Ridge Lambda（遗忘因子）
        new_lambda = self.spin_ridge_lambda.value()
        self.ridge_cfg.lambda_factor = new_lambda
        # Ridge 模型内部通过 self.cfg.lambda_factor 访问，可以直接更新配置
        
        # 6. Ridge Alpha（惩罚系数）
        new_alpha = self.spin_ridge_alpha.value()
        self.ridge_cfg.ridge_alpha = new_alpha
        # Ridge 模型内部通过 self.cfg.ridge_alpha 访问，可以直接更新配置
        # 这两个参数会在下次 update() 调用时生效

    def update_loop(self):
        """主更新循环：处理数据并更新 UI."""
        # 批量处理数据（支持加速回放）
        processed = 0
        while not self.q.empty() and processed < 200:
            try:
                event = self.q.get_nowait()
                self.process_event(event)
                processed += 1
            except queue.Empty:
                break
        
        # 更新 UI
        self.update_charts()
        self.update_status_lights()
        self.update_action_display()
        self.update_price_display()

    def process_event(self, event: Dict[str, Any]):
        """处理单个事件."""
        event_type = event.get('type')
        symbol = event.get('symbol', '')
        
        if event_type == 'T':  # Trade 事件
            price = float(event.get('price', 0))
            volume = float(event.get('volume', 0))
            side = event.get('side', '')
            ticks = event.get('ticks', '0')
            
            # 更新价格缓存
            self.prices[symbol] = price
            
            # 更新 InstrumentState（用于解析 DOM）
            if symbol in self.instruments:
                self.instruments[symbol].add_trade(price, volume, side, ticks)
            
            # 构造 TickEvent 并传给策略引擎
            if symbol == 'ES':
                tick_ev = TickEvent(
                    t_ms=ticks_to_ms(ticks),
                    es=price,
                    nq=self.prices.get('NQ'),
                    ym=self.prices.get('YM') or self.prices.get('MYM'),
                    btc=self.prices.get('BTCUSDT')
                )
                # 确保有 NQ 数据才处理
                if tick_ev.nq is not None:
                    # 更新主策略引擎（Kalman）
                    self.engine.on_tick(tick_ev)
                    
                    # 同时更新 Ridge 模型（用于对比）
                    ridge_fair, ridge_spread = self.ridge_model.update(tick_ev)
                    if ridge_fair is not None and ridge_spread is not None:
                        self.ridge_fair = ridge_fair
                        self.ridge_spread = ridge_spread
                        self.ridge_spread_ticks = ridge_spread / 0.25  # 转换为 tick
                        # 更新实时数据
                        self.current_spread_ridge = self.ridge_spread_ticks
                
                    # 更新 Kalman 实时数据
                    if self.engine.last_spread_ticks is not None:
                        self.current_spread_kalman = self.engine.last_spread_ticks
        
        elif event_type == 'D':  # DOM 事件
            if symbol == 'ES':
                bids_str = event.get('bids', '')
                asks_str = event.get('asks', '')
                ticks = event.get('ticks', '0')
                
                # 使用 InstrumentState 解析 DOM
                inst = self.instruments.get('ES')
                if inst:
                    inst.update_dom(bids_str, asks_str)
                    
                    # 构造 DomSnapshot
                    best_bid = inst.bids[0][0] if inst.bids and inst.bids[0][0] > 0 else 0.0
                    best_ask = inst.asks[0][0] if inst.asks and inst.asks[0][0] > 0 else 0.0
                    
                    # 过滤有效的 bids/asks
                    valid_bids = [(p, int(v)) for p, v in inst.bids if p > 0 and v > 0]
                    valid_asks = [(p, int(v)) for p, v in inst.asks if p > 0 and v > 0]
                    
                    if valid_bids and valid_asks:
                        dom = DomSnapshot(
                            t_ms=ticks_to_ms(ticks),
                            best_bid=best_bid,
                            best_ask=best_ask,
                            bids=valid_bids,
                            asks=valid_asks
                        )
                        self.engine.on_dom(dom)

    def update_charts(self):
        """更新实时能量柱图表（只显示当前值，不保留历史）."""
        # 更新 Kalman 能量柱（只显示当前值）
        if self.current_spread_kalman is not None:
            # 只显示一个柱子，在 x=0 位置
            self.bar_kalman.setOpts(x=[0], height=[self.current_spread_kalman], width=0.5)
        
        # 更新 Ridge 能量柱（只显示当前值）
        if self.current_spread_ridge is not None:
            # 只显示一个柱子，在 x=0 位置
            self.bar_ridge.setOpts(x=[0], height=[self.current_spread_ridge], width=0.5)

    def update_status_lights(self):
        """更新状态指示灯."""
        spread_ticks = self.engine.last_spread_ticks
        if spread_ticks is None:
            return
        
        th = self.engine.cfg.base_spread_threshold
        
        # A. Kalman Model Spread Light
        if spread_ticks > th:
            self.light_model.set_status("GREEN", f"Long\n{spread_ticks:.2f}t")
        elif spread_ticks < -th:
            self.light_model.set_status("RED", f"Short\n{spread_ticks:.2f}t")
        else:
            self.light_model.set_status("GRAY", f"Neutral\n{spread_ticks:.2f}t")
        
        # A2. Ridge Model Spread Light
        ridge_spread_ticks = self.ridge_spread_ticks
        if ridge_spread_ticks is not None:
            if ridge_spread_ticks > th:
                self.light_ridge.set_status("GREEN", f"Long\n{ridge_spread_ticks:.2f}t")
            elif ridge_spread_ticks < -th:
                self.light_ridge.set_status("RED", f"Short\n{ridge_spread_ticks:.2f}t")
            else:
                self.light_ridge.set_status("GRAY", f"Neutral\n{ridge_spread_ticks:.2f}t")
        else:
            self.light_ridge.set_status("GRAY", "No Data")
        
        # B. OBI Light
        obi = 0.0
        if self.engine.last_dom:
            obi = self.engine._calc_obi(self.engine.last_dom)
        
        min_obi = self.engine.cfg.min_obi_for_long
        if obi > min_obi:
            self.light_obi.set_status("GREEN", f"Bullish\n{obi:.2f}")
        elif obi < -min_obi:
            self.light_obi.set_status("RED", f"Bearish\n{obi:.2f}")
        else:
            self.light_obi.set_status("GRAY", f"Flat\n{obi:.2f}")
        
        # C. BTC Risk Light
        is_safe = self.engine.btc_monitor.check_safety()
        vol_ratio = self.engine.btc_monitor.get_vol_ratio()
        if is_safe:
            self.light_btc.set_status("GREEN", f"Safe\n{vol_ratio:.1f}x")
        else:
            self.light_btc.set_status("RED", f"ALERT\n{vol_ratio:.1f}x")
        
        # D. Iceberg Light
        es_price = self.prices.get('ES')
        if es_price:
            res = self.engine.iceberg_detector.get_resistance(es_price)
            sup = self.engine.iceberg_detector.get_support(es_price)
            if res > 100:
                self.light_ice.set_status("RED", f"Resist\n{res:.0f}")
            elif sup > 100:
                self.light_ice.set_status("RED", f"Support\n{sup:.0f}")
            else:
                self.light_ice.set_status("GREEN", "Clean")
        else:
            self.light_ice.set_status("GRAY", "No Data")

    def update_action_display(self):
        """更新顶部指令显示."""
        spread_ticks = self.engine.last_spread_ticks
        es_price = self.prices.get('ES')
        
        if spread_ticks is None or es_price is None:
            self.lbl_action.setText("等待数据中... (WAITING FOR DATA...)")
            self.lbl_action.setStyleSheet(
                "background-color: #212121; "
                "color: #9E9E9E; "
                "border-radius: 10px; "
                "padding: 10px;"
            )
            return
        
        th = self.engine.cfg.base_spread_threshold
        
        # 基础信号
        want_long = spread_ticks > th
        want_short = spread_ticks < -th
        
        # 模拟过滤器检查（简化版，实际逻辑在 engine 内部）
        valid = True
        reason = ""
        
        # BTC 风险检查
        if not self.engine.btc_monitor.check_safety():
            valid = False
            reason = "BTC RISK"
        
        # OBI 检查
        obi = 0.0
        if self.engine.last_dom:
            obi = self.engine._calc_obi(self.engine.last_dom)
            if want_long and obi < self.engine.cfg.min_obi_for_long:
                valid = False
                reason = "OBI"
            if want_short and obi > -self.engine.cfg.min_obi_for_short:
                valid = False
                reason = "OBI"
        
        # 冰山检查
        if valid and es_price:
            if want_long and self.engine.iceberg_detector.check_iceberg_resistance(es_price, 1):
                valid = False
                reason = "ICEBERG"
            if want_short and self.engine.iceberg_detector.check_iceberg_resistance(es_price, -1):
                valid = False
                reason = "ICEBERG"
        
        # 显示结果
        style_base = (
            "border-radius: 10px; "
            "padding: 10px; "
            "font-weight: bold; "
        )
        
        if want_long and valid:
            self.lbl_action.setText(f"🟢 BUY LIMIT @ {es_price:.2f}")
            self.lbl_action.setStyleSheet(
                f"background-color: #2E7D32; "
                f"color: white; "
                f"{style_base}"
            )
        elif want_short and valid:
            self.lbl_action.setText(f"🔴 SELL LIMIT @ {es_price:.2f}")
            self.lbl_action.setStyleSheet(
                f"background-color: #C62828; "
                f"color: white; "
                f"{style_base}"
            )
        elif want_long or want_short:
            # 有信号但被过滤
            self.lbl_action.setText(f"🟡 信号被过滤 ({reason})")
            self.lbl_action.setStyleSheet(
                f"background-color: #F9A825; "
                f"color: black; "
                f"{style_base}"
            )
        else:
            self.lbl_action.setText("⏳ 等待信号 (WAIT)")
            self.lbl_action.setStyleSheet(
                f"background-color: #212121; "
                f"color: #9E9E9E; "
                f"{style_base}"
            )

    def update_price_display(self):
        """更新价格显示."""
        self.lbl_es.setText(f"ES: {self.prices.get('ES', '--'):.2f}" if self.prices.get('ES') else "ES: --")
        self.lbl_nq.setText(f"NQ: {self.prices.get('NQ', '--'):.2f}" if self.prices.get('NQ') else "NQ: --")
        self.lbl_ym.setText(f"YM: {self.prices.get('YM', '--') or self.prices.get('MYM', '--'):.2f}" 
                           if (self.prices.get('YM') or self.prices.get('MYM')) else "YM: --")
        self.lbl_btc.setText(f"BTC: {self.prices.get('BTCUSDT', '--'):.2f}" 
                            if self.prices.get('BTCUSDT') else "BTC: --")

    def closeEvent(self, event):
        """窗口关闭事件."""
        print("[Dashboard] 正在关闭...")
        self.listener.stop()
        self.listener.join(timeout=2.0)
        event.accept()


# ============================================================================
# 主程序入口
# ============================================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 设置应用样式
    app.setStyle('Fusion')
    
    # 创建并显示窗口
    win = StrategyDashboard()
    win.show()
    
    print("=" * 60)
    print("🚀 Norden v3.1 策略驾驶舱已启动")
    print("=" * 60)
    print("📊 功能：")
    print("  1. Spread 信号可视化")
    print("  2. 多重过滤器状态显示")
    print("  3. 实时参数调整")
    print("  4. 交易指令提示")
    print("=" * 60)
    print("⚠️  确保 ATAS 的 NFQE_Bridge_UDP 指标正在运行")
    print("=" * 60)
    
    sys.exit(app.exec())

