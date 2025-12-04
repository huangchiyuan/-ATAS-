"""
Kalman 定价引擎高性能可视化（PyQtGraph）
======================================

特点：
    - 使用 PyQtGraph 实时绘图，支持平移、缩放、十字光标等交互
    - 不在控制台输出任何 Tick 日志
    - 适合 ATAS 1x~500x 回放，直接观察 ES / NQ / YM / ES_fair 轨迹

使用：
    1. 启动 ATAS 回放 (加载 NFQE_Bridge_UDP，指向 127.0.0.1:5555)
    2. 在项目根目录运行：

        python run_kalman_qt.py

    3. 在图表中：
        - 鼠标滚轮：缩放
        - 左键拖动：平移
        - 右键菜单：重置视图等
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Dict, Any, Optional, List

from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtCore import QTimer
import pyqtgraph as pg

from dom_data_feed import UdpListener
from norden_v3 import (
    OnlineKalman,
    KalmanConfig,
    OnlineRidge,
    RidgeConfig,
    TickEvent,
)


def _ticks_to_ms(ticks_str: str) -> int:
    """将 .NET Ticks 转成 Unix 毫秒时间戳近似值."""
    try:
        ticks = int(ticks_str)
    except Exception:
        return int(time.time() * 1000)

    TICKS_AT_EPOCH = 621355968000000000
    us = (ticks - TICKS_AT_EPOCH) // 10
    return int(us // 1000)


class KalmanQtViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kalman ES Fair Price Viewer (PyQtGraph)")
        self.resize(1200, 800)

        # 数据接收线程
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200000)
        self.listener = UdpListener(self.q)

        # Kalman & Ridge 引擎（双模型对比）
        self.kalman = OnlineKalman(KalmanConfig())
        self.ridge = OnlineRidge(RidgeConfig())

        # 价格缓存
        self.last_es: Optional[float] = None
        self.last_nq: Optional[float] = None
        self.last_ym: Optional[float] = None
        self.last_btc: Optional[float] = None

        # 时间 / 价格序列
        self.start_ms: Optional[int] = None
        self.times: List[float] = []

        # 为解决 ES/NQ/YM 数量级不同的问题，这里统一使用“基准索引法”来绘图：
        #   Value_norm = Price_t / Price_start * 100
        # 这样所有品种在 t=0 时都为 100，后续的偏移可直观比较强弱与相关性。
        self.base_es: Optional[float] = None
        self.base_nq: Optional[float] = None
        self.base_ym: Optional[float] = None

        self.es_list: List[float] = []
        self.nq_list: List[float] = []
        self.ym_list: List[float] = []
        self.fair_list: List[float] = []        # Kalman 公允价（归一化后）
        self.fair_ridge_list: List[float] = []  # Ridge 公允价（归一化后）

        # 采样控制：为避免 500x 下点太多，每 N 个 tick 采样一次
        self.sample_every = 5
        self._tick_counter = 0

        # 为了避免模型尚未收敛时的噪音，前若干秒的数据不做可视化
        # 单位：秒（相对本次回放起点）
        self.warmup_seconds = 10.0

        self._init_plot()

        # GUI 定时器：从队列取数据 + 更新曲线
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._consume_events)
        self.timer.start(10)  # 每 10ms 轮询一次

        # 启动 UDP 监听线程
        self.listener.start()

    # ----------------- 图表初始化 -----------------
    def _init_plot(self) -> None:
        pg.setConfigOptions(antialias=True, foreground="w", background="k")
        self.plot_widget = pg.PlotWidget(self)
        self.setCentralWidget(self.plot_widget)

        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.addLegend()

        # 不同价格序列的曲线
        self.curve_es = self.plot_widget.plot(
            pen=pg.mkPen(color=(0, 170, 255), width=1.5), name="ES"
        )
        self.curve_fair = self.plot_widget.plot(
            pen=pg.mkPen(color=(255, 170, 0), width=1.5), name="ES_fair_KF"
        )
        self.curve_fair_ridge = self.plot_widget.plot(
            pen=pg.mkPen(color=(200, 0, 255), width=1.2, style=pg.QtCore.Qt.PenStyle.DashLine),
            name="ES_fair_Ridge",
        )
        self.curve_nq = self.plot_widget.plot(
            pen=pg.mkPen(color=(0, 255, 0), width=1.0, style=pg.QtCore.Qt.PenStyle.DashLine),
            name="NQ",
        )
        self.curve_ym = self.plot_widget.plot(
            pen=pg.mkPen(color=(255, 0, 0), width=1.0, style=pg.QtCore.Qt.PenStyle.DashLine),
            name="YM",
        )

    # ----------------- 数据消费与更新 -----------------
    def _consume_events(self) -> None:
        # 一次性消费尽可能多的队列数据，避免堆积
        processed = 0
        while processed < 500:
            try:
                event = self.q.get_nowait()
            except queue.Empty:
                break

            processed += 1
            if event.get("type") != "T":
                continue

            symbol = event.get("symbol")
            price = float(event.get("price", 0.0))
            ticks_str = event.get("ticks", "")
            t_ms = _ticks_to_ms(ticks_str)

            if self.start_ms is None:
                self.start_ms = t_ms

            if symbol == "ES":
                self.last_es = price
                if self.base_es is None:
                    self.base_es = price
            elif symbol == "NQ":
                self.last_nq = price
                if self.base_nq is None:
                    self.base_nq = price
            elif symbol in ("YM", "MYM"):
                self.last_ym = price
                if self.base_ym is None:
                    self.base_ym = price
            elif symbol.upper().startswith("BTC"):
                self.last_btc = price

            if self.last_es is None or self.last_nq is None:
                continue

            tick = TickEvent(
                t_ms=t_ms,
                es=self.last_es,
                nq=self.last_nq,
                ym=self.last_ym,
                btc=self.last_btc,
            )
            fair_kf, _ = self.kalman.update(tick)
            fair_rd, _ = self.ridge.update(tick)
            self._tick_counter += 1

            if (
                fair_kf is not None
                and fair_rd is not None
                and self._tick_counter % self.sample_every == 0
            ):
                rel_t = (t_ms - (self.start_ms or t_ms)) / 1000.0

                # 跳过模型尚在“预热期”的数据，不进入可视化
                if rel_t < self.warmup_seconds:
                    continue

                # ---- 基准索引归一化（Baseline Indexing）----
                # 公式: Value_norm = Price_t / Price_start * 100
                # 注意：Fair 使用 ES 起始价进行归一化，以便直观看出 ES 与 Fair 的相对偏移。
                if self.base_es is None or self.base_nq is None or self.base_ym is None:
                    # 仍在基准价格收集阶段，跳过
                    continue

                es_rel = (self.last_es / self.base_es * 100.0) if self.last_es is not None else 100.0
                fair_kf_rel = fair_kf / self.base_es * 100.0
                fair_rd_rel = fair_rd / self.base_es * 100.0
                nq_rel = (self.last_nq / self.base_nq * 100.0) if self.last_nq is not None else 100.0
                ym_rel = (self.last_ym / self.base_ym * 100.0) if self.last_ym is not None else 100.0

                self.times.append(rel_t)
                self.es_list.append(es_rel)
                self.fair_list.append(fair_kf_rel)
                self.fair_ridge_list.append(fair_rd_rel)
                self.nq_list.append(nq_rel)
                self.ym_list.append(ym_rel)

        if self.times:
            self._update_curves()

    def _update_curves(self) -> None:
        x = self.times
        self.curve_es.setData(x, self.es_list)
        self.curve_fair.setData(x, self.fair_list)
        self.curve_fair_ridge.setData(x, self.fair_ridge_list)
        self.curve_nq.setData(x, self.nq_list)
        self.curve_ym.setData(x, self.ym_list)

    # ----------------- 关闭处理 -----------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.listener.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    import sys

    app = QApplication(sys.argv)
    win = KalmanQtViewer()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


