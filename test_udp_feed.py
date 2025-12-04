"""
简单的 UDP 数据接收自检脚本
==========================

用途：
    - 在不启动 GUI 的情况下，单独验证 C# → Python 的 UDP 数据是否正常：
        * 是否能持续收到 T / D 消息
        * 字段解析是否正常
        * 每秒大致流量是否在预期范围内

使用方法：
    1. 确保 ATAS 端的指标（NFQE_Bridge_UDP）已加载，并指向本机 5555 端口
    2. 在项目根目录运行：

        python test_udp_feed.py

    3. 观察终端输出：
        - 前若干条原始事件内容
        - 每秒统计：T 条数、D 条数、队列长度
"""

import queue
import threading
import time
from typing import Dict, Any

from dom_data_feed import UdpListener


class FeedTester:
    def __init__(self):
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=50000)
        self.listener = UdpListener(self.q)

        self.running = True
        self.print_thread = threading.Thread(target=self._print_loop, daemon=True)

        self.t_count = 0
        self.d_count = 0
        self.first_samples_shown = False

    def start(self) -> None:
        print("🚀 [FeedTester] 启动 UdpListener，等待 C# 端发数据...")
        self.listener.start()
        self.print_thread.start()

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 [FeedTester] 收到键盘中断，准备退出...")
            self.running = False
            self.listener.stop()
            self.listener.join(timeout=2.0)
            print("✅ [FeedTester] 已安全退出。")

    def _print_loop(self) -> None:
        """从队列中读取事件，做简单解析与统计."""
        last_report = time.time()
        samples_shown = 0

        while self.running:
            try:
                event = self.q.get(timeout=0.5)
            except queue.Empty:
                continue

            etype = event.get("type")
            if etype == "T":
                self.t_count += 1
            elif etype == "D":
                self.d_count += 1

            # 前 10 条事件详细打印，便于人工检查字段
            if samples_shown < 10:
                print(f"[SAMPLE] {event}")
                samples_shown += 1

            # 每秒打印一次统计信息
            now = time.time()
            if now - last_report >= 1.0:
                print(
                    f"[STATS] T={self.t_count:,} 条, D={self.d_count:,} 条, "
                    f"QueueSize={self.q.qsize():,}"
                )
                last_report = now


if __name__ == "__main__":
    tester = FeedTester()
    tester.start()


