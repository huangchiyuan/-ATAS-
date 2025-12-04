"""
在线卡尔曼定价引擎 - 实盘干跑测试
===============================

作用：
    - 复用现有 UDP 数据流
    - 从 ES / NQ / YM 的 Tick 中构造 TickEvent
    - 喂给 norden_v3.OnlineKalman
    - 在终端持续打印：ES 实际价、Kalman 公允价、Spread

使用：
    1. 确保 C# 端 NFQE_Bridge_UDP 正在向 127.0.0.1:5555 发送 ES/NQ/YM T 消息
    2. 在项目根目录运行：

        python run_kalman_live.py
"""

import queue
import threading
import time
from typing import Dict, Any, Optional

from dom_data_feed import UdpListener
from norden_v3.kalman_model import OnlineKalman, KalmanConfig
from norden_v3.types import TickEvent


def _ticks_to_ms(ticks_str: str) -> int:
    """
    将 .NET Ticks 转成 Unix 毫秒时间戳近似值.
    这里只做粗略换算，主要用于排序/调试，不做严格时区对齐。
    """
    try:
        ticks = int(ticks_str)
    except Exception:
        return int(time.time() * 1000)

    # .NET ticks: 100ns 单位，从 0001-01-01 开始
    # Epoch 偏移（同 dom_data_feed.TICKS_AT_EPOCH）
    TICKS_AT_EPOCH = 621355968000000000
    us = (ticks - TICKS_AT_EPOCH) // 10
    return int(us // 1000)


class KalmanLiveRunner:
    def __init__(self):
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=50000)
        self.listener = UdpListener(self.q)

        # Kalman 引擎
        self.kalman = OnlineKalman(KalmanConfig())

        # 当前最新价格缓存
        self.last_es: Optional[float] = None
        self.last_nq: Optional[float] = None
        self.last_ym: Optional[float] = None
        self.last_btc: Optional[float] = None

        self.running = True
        self.worker = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        print("🚀 [KalmanLive] 启动 UdpListener 与 OnlineKalman (仅打印，不下单)...")
        self.listener.start()
        self.worker.start()

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 [KalmanLive] 收到键盘中断，准备退出...")
            self.running = False
            self.listener.stop()
            self.listener.join(timeout=2.0)
            print("✅ [KalmanLive] 已安全退出。")

    def _loop(self) -> None:
        last_print = time.time()
        while self.running:
            try:
                event = self.q.get(timeout=0.5)
            except queue.Empty:
                continue

            if event.get("type") != "T":
                continue

            symbol = event.get("symbol")
            price = float(event.get("price", 0.0))
            ticks_str = event.get("ticks", "")
            t_ms = _ticks_to_ms(ticks_str)

            if symbol == "ES":
                self.last_es = price
            elif symbol == "NQ":
                self.last_nq = price
            elif symbol in ("YM", "MYM"):
                self.last_ym = price
            elif symbol.upper().startswith("BTC"):
                self.last_btc = price

            # 只有当 ES / NQ 至少都有价格时才更新 Kalman
            if self.last_es is None or self.last_nq is None:
                continue

            tick = TickEvent(
                t_ms=t_ms,
                es=self.last_es,
                nq=self.last_nq,
                ym=self.last_ym,
                btc=self.last_btc,
            )

            fair, spread = self.kalman.update(tick)

            now = time.time()
            if fair is not None and spread is not None and now - last_print >= 0.5:
                beta = self.kalman.theta  # [beta_NQ, beta_YM, alpha]
                print(
                    f"[KF] t={tick.t_ms}  "
                    f"ES={self.last_es:.2f}  "
                    f"Fair={fair:.5f}  "
                    f"Spread={spread:+.5f}  "
                    f"beta_NQ={beta[0]:+.4f}  "
                    f"beta_YM={beta[1]:+.4f}  "
                    f"alpha={beta[2]:+.2f}"
                )
                last_print = now


if __name__ == "__main__":
    runner = KalmanLiveRunner()
    runner.start()


