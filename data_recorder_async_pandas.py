import socket
import duckdb
import datetime
import time
import os
import threading
import queue
from tkinter import Tk, Button, Label, W, E
from tkinter.ttk import Progressbar
import pandas as pd

# --- 核心配置 ---
UDP_IP = "127.0.0.1"
UDP_PORT = 5555
DB_PREFIX = "market_data"
QUEUE_SIZE = 1000000
DB_BATCH_SIZE = 50000
BATCH_TIMEOUT = 1.0
SOCKET_TIMEOUT = 2.0

# --- C# Ticks 工具 ---
TICKS_AT_EPOCH = 621355968000000000


def ticks_to_datetime_us(ticks_str):
    try:
        ticks = int(ticks_str)
        microseconds = (ticks - TICKS_AT_EPOCH) // 10
        return microseconds
    except:
        return 0


def ticks_to_full_datetime(ticks_str):
    try:
        microseconds = int(ticks_str) / 10
        return datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=microseconds)
    except:
        return datetime.datetime.now()


# =======================================================================
# 数据库写入线程 (消费者)
# =======================================================================
class DbWriterThread(threading.Thread):
    def __init__(self, data_queue, ui):
        super().__init__()
        self.data_queue = data_queue
        self.ui = ui
        self.running = True
        self.conn = None
        self.db_file = None
        self.total_written = 0
        self.buffer = []

    def init_db(self, first_ticks):
        dt = ticks_to_full_datetime(first_ticks)
        date_str = dt.strftime("%Y-%m-%d")
        self.db_file = f"{DB_PREFIX}_{date_str}.duckdb"

        self.conn = duckdb.connect(self.db_file)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                symbol VARCHAR, price DOUBLE, volume DOUBLE, side VARCHAR, 
                exchange_time TIMESTAMP, recv_time TIMESTAMP
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS depth (
                symbol VARCHAR, bids VARCHAR, asks VARCHAR, 
                exchange_time TIMESTAMP, recv_time TIMESTAMP
            );
        """)
        print(f"✅ [Writer] 数据库已连接: {self.db_file}")
        self.ui.set_status(f"Writing to {self.db_file}", "green")

    def run(self):
        print("🚀 [Writer] 写入线程启动")

        while self.running or not self.data_queue.empty():
            try:
                item = self.data_queue.get(timeout=1.0)

                if isinstance(item, dict) and 'init' in item:
                    if not self.conn:
                        self.init_db(item['init'])
                    continue

                self.buffer.append(item)

                if len(self.buffer) >= DB_BATCH_SIZE:
                    self.flush()

            except queue.Empty:
                if self.buffer and not self.running:
                    self.flush()
                continue
            except Exception as e:
                print(f"❌ [Writer Error] {e}")

        if self.buffer: self.flush()
        if self.conn:
            self.conn.close()
        print("✅ [Writer] 写入线程安全退出")

    def flush(self):
        if not self.conn or not self.buffer: return

        t0 = time.time()

        ticks_data = [x['data'] for x in self.buffer if x['type'] == 'T']
        doms_data = [x['data'] for x in self.buffer if x['type'] == 'D']
        self.buffer.clear()

        try:
            self.conn.execute("BEGIN TRANSACTION")

            # Tick 极速写入
            if ticks_data:
                tick_cols = ['symbol', 'price', 'volume', 'side', 'exchange_time_us']
                df_ticks = pd.DataFrame(ticks_data, columns=tick_cols)
                self.conn.register('temp_ticks', df_ticks)
                self.conn.execute(f"""
                    INSERT INTO ticks 
                    SELECT symbol, price, volume, side, 
                        to_timestamp(CAST(exchange_time_us AS DOUBLE) / 1000000) AS exchange_time,
                        now()
                    FROM temp_ticks
                """)
                self.conn.unregister('temp_ticks')

            # DOM 极速写入
            if doms_data:
                dom_cols = ['symbol', 'bids', 'asks', 'exchange_time_us']
                df_doms = pd.DataFrame(doms_data, columns=dom_cols)
                self.conn.register('temp_doms', df_doms)
                self.conn.execute(f"""
                    INSERT INTO depth 
                    SELECT symbol, bids, asks, 
                        to_timestamp(CAST(exchange_time_us AS DOUBLE) / 1000000) AS exchange_time,
                        now()
                    FROM temp_doms
                """)
                self.conn.unregister('temp_doms')

            self.conn.execute("COMMIT")

        except Exception as e:
            try:
                self.conn.execute("ROLLBACK")
                print(f"❌ [DB TRANSACTION] Rolled back batch due to error: {e}")
            except:
                print(f"❌ [DB FATAL] Could not rollback (Original Error: {e}). Data lost.")
            return

        # 4. 更新统计
        count = len(ticks_data) + len(doms_data)
        self.total_written += count
        self.ui.update_stats(self.total_written)

        duration = time.time() - t0
        if duration > 0.1:
            print(f"⚠️ [SLOW IO] Wrote {count} rows in {duration:.4f}s. Q:{self.data_queue.qsize()}.")


# =======================================================================
# 网络接收线程 (生产者)
# =======================================================================
class ReceiverThread(threading.Thread):
    def __init__(self, data_queue, ui):
        super().__init__()
        self.data_queue = data_queue
        self.ui = ui
        self.running = True
        self.sock = None
        self.db_initialized = False

    def run(self):
        print("🚀 [Receiver] 接收线程启动")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
        self.sock.bind((UDP_IP, UDP_PORT))
        self.sock.settimeout(2.0)

        self.ui.set_status(f"Listening on {UDP_PORT}...", "blue")

        while self.running:
            try:
                data, _ = self.sock.recvfrom(65535)
                text = data.decode('utf-8')
                messages = text.strip().split('\n')

                for msg in messages:
                    if not msg: continue
                    parts = msg.split(',')
                    msg_type = parts[0]

                    if not self.db_initialized and (msg_type == 'T' or msg_type == 'D'):
                        try:
                            ts = parts[5] if msg_type == 'T' else parts[4]
                            self.data_queue.put({'init': ts})
                            self.db_initialized = True
                        except:
                            continue

                    if not self.db_initialized: continue

                    if msg_type == 'T' and len(parts) >= 6:
                        row = (parts[1], float(parts[2]), float(parts[3]), parts[4], ticks_to_datetime_us(parts[5]))
                        self.data_queue.put({'type': 'T', 'data': row})

                    elif msg_type == 'D' and len(parts) >= 5:
                        row = (parts[1], parts[2], parts[3], ticks_to_datetime_us(parts[4]))
                        self.data_queue.put({'type': 'D', 'data': row})

            except socket.timeout:
                continue
            except Exception as e:
                print(f"❌ [Receiver Error] {e}")

        self.sock.close()
        print("✅ [Receiver] 接收线程退出")


# =======================================================================
# 前端 GUI
# =======================================================================
class RecorderGUI(Tk):
    def __init__(self):
        super().__init__()
        self.title("Async Data Recorder (High Performance)")
        self.geometry("450x250")

        self.data_queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.receiver = None
        self.writer = None
        self.running = False

        self.setup_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_ui(self):
        Label(self, text="Status:", fg="gray").pack(pady=(10, 0))
        self.status_label = Label(self, text="Ready", fg="blue", font=("Arial", 10, "bold"))
        self.status_label.pack(pady=5)

        self.count_label = Label(self, text="Total Saved: 0")
        self.count_label.pack(pady=5)

        self.q_label = Label(self, text="Queue Usage: 0")
        self.q_label.pack()

        self.start_button = Button(self, text="START", command=self.start, bg="#4CAF50", fg="white", height=2, width=20)
        self.start_button.pack(pady=10)

        self.stop_button = Button(self, text="STOP", command=self.stop, bg="#f44336", fg="white", state='disabled')
        self.stop_button.pack(pady=5)

        self.after(500, self.update_gui_stats)

    def start(self):
        if self.running: return

        self.running = True
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')

        self.writer = DbWriterThread(self.data_queue, self)
        self.receiver = ReceiverThread(self.data_queue, self)

        self.writer.start()
        self.receiver.start()

    def stop(self):
        if not self.running: return

        self.set_status("Stopping...", "orange")
        self.running = False

        if self.receiver: self.receiver.running = False

        self.stop_button.config(state='disabled')

        self.after(1000, self.check_shutdown)

    def check_shutdown(self):
        # 检查线程是否存活
        is_writer_alive = self.writer and self.writer.is_alive()
        is_receiver_alive = self.receiver and self.receiver.is_alive()

        if is_writer_alive or is_receiver_alive:
            # 仍然有线程在工作，继续等待
            q_size = self.data_queue.qsize()
            color = "red" if q_size > 50000 else "black"
            self.q_label.config(text=f"Queue Usage: {q_size:,} / {QUEUE_SIZE}", fg=color)
            self.set_status(f"Flushing... Q: {q_size}", "orange")
            self.after(500, self.check_shutdown)
        else:
            # 所有线程都已结束，执行最终关闭
            self.set_status("Stopped Safe.", "green")
            self.start_button.config(state='normal')
            self.destroy()  # 最终关闭程序

    def update_stats(self, count):
        self.count_label.after(0, lambda: self.count_label.config(text=f"Total Saved: {count:,}"))

    def update_gui_stats(self):
        if self.running:
            q_size = self.data_queue.qsize()
            color = "red" if q_size > 50000 else "black"
            self.q_label.config(text=f"Queue Usage: {q_size:,} / {QUEUE_SIZE}", fg=color)
        self.after(500, self.update_gui_stats)

    def set_status(self, msg, color):
        self.status_label.after(0, lambda: self.status_label.config(text=msg, fg=color))

    def thread_finished(self):
        # 线程通过 run() 方法中的 logic 退出，会直接触发 check_shutdown
        pass

    def on_close(self):
        if self.running:
            self.stop()
            self.after(1000, self.check_shutdown)
        else:
            self.destroy()


if __name__ == "__main__":
    app = RecorderGUI()
    app.mainloop()