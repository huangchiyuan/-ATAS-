import tkinter as tk
from tkinter import ttk
import time
import threading
import socket
from collections import deque
import math
import queue

# ================= 0. 核心配置区 =================
UDP_DATA_IP = "0.0.0.0"  # 接收
UDP_DATA_PORT = 5555

UDP_CMD_IP = "127.0.0.1"  # 发送
UDP_CMD_PORT = 6666

SYMBOL_MAP = {'ES': 'ES', 'NQ': 'NQ', 'YM': 'YM'}

WINDOW_SIZE = 1000
VWAP_WINDOW = 2000
SPEED_WINDOW = 5.0
HEARTBEAT_TIMEOUT = 5.0  # [优化] 延长至5秒，防止误报
LOG_FREQUENCY = 20

WALL_THRESHOLD_ES = 200
WALL_THRESHOLD_NQ = 80
WEIGHT_NQ = 0.5
WEIGHT_YM = 0.3
WEIGHT_VWAP = 0.2

AUTO_TRADE_ENABLED = True
TRADE_COOLDOWN = 2.0


# ================= 1. 执行引擎 =================
class ExecutionEngine:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (UDP_CMD_IP, UDP_CMD_PORT)
        self.lock = threading.Lock()
        self.last_trade_time = 0

    def send_command(self, cmd):
        if not AUTO_TRADE_ENABLED: return
        if time.time() - self.last_trade_time < TRADE_COOLDOWN: return

        with self.lock:
            try:
                self.sock.sendto(cmd.encode(), self.target)
                self.last_trade_time = time.time()
                print(f"🚀 [EXEC] >>> {cmd}")
            except:
                pass

    def buy_market(self):
        self.send_command("BUY_MARKET")

    def sell_market(self):
        self.send_command("SELL_MARKET")

    def close_all(self):
        self.send_command("CLOSE_ALL")


# ================= 2. 数据模型 =================
class InstrumentData:
    TICK_SIZE = 0.25

    def __init__(self, name):
        self.name = name
        self.prices = deque(maxlen=WINDOW_SIZE)
        self.vwap_window = deque(maxlen=VWAP_WINDOW)
        self.tape_window = deque()
        self.current_price = 0.0;
        self.current_vwap = 0.0;
        self.current_speed = 0.0
        self.bids = [(0.0, 0)] * 5;
        self.asks = [(0.0, 0)] * 5
        self.wall_msg = "";
        self.last_trade_msg = "等待成交..."
        self.last_packet_time = time.time();
        self.is_connected = False

    def update_heartbeat(self):
        self.last_packet_time = time.time();
        self.is_connected = True

    def update_trade(self, price, volume, timestamp, side):
        self.update_heartbeat()
        self.current_price = price;
        self.prices.append(price)
        self.last_trade_msg = f"Last: {price:.2f} x {int(volume)} ({side})"

        self.tape_window.append((timestamp, volume))
        while self.tape_window and (timestamp - self.tape_window[0][0]) > SPEED_WINDOW: self.tape_window.popleft()
        duration = min(SPEED_WINDOW, max(0.1, timestamp - self.tape_window[0][0] if self.tape_window else 0.1))
        self.current_speed = sum(v for t, v in self.tape_window) / duration

        self.vwap_window.append((price, volume))
        total_vol = sum(v for p, v in self.vwap_window)
        if total_vol > 0:
            self.current_vwap = sum(p * v for p, v in self.vwap_window) / total_vol
        else:
            self.current_vwap = price

    def update_depth_v2(self, bids_raw, asks_raw):
        self.update_heartbeat()
        try:
            new_bids = []
            for item in bids_raw.split('|'):
                if '@' in item: p, v = item.split('@'); new_bids.append((float(p), int(float(v))))
            new_asks = []
            for item in asks_raw.split('|'):
                if '@' in item: p, v = item.split('@'); new_asks.append((float(p), int(float(v))))
            while len(new_bids) < 5: new_bids.append((0.0, 0))
            while len(new_asks) < 5: new_asks.append((0.0, 0))
            self.bids = new_bids[:5];
            self.asks = new_asks[:5]
            self.check_walls()
        except:
            pass

    def check_walls(self):
        thresh = WALL_THRESHOLD_ES if self.name == 'ES' else WALL_THRESHOLD_NQ
        self.wall_msg = ""
        for i, item in enumerate(self.asks):
            if item[1] >= thresh: self.wall_msg = f"🔴 阻力墙 @ {item[0]:.2f} ({int(item[1])})"; return
        for i, item in enumerate(self.bids):
            if item[1] >= thresh: self.wall_msg = f"🟢 支撑墙 @ {item[0]:.2f} ({int(item[1])})"; return

    def get_change(self):
        if not self.prices: return 0.0
        return (self.current_price - self.prices[0]) / self.prices[0] * 100 if self.prices[0] else 0.0

    def get_dynamic_limit(self):
        speed = self.current_speed
        if self.name == 'ES':
            if speed < 20:
                return min(max(speed * 30 * 0.4, 80), 200)
            else:
                return min(speed * 8 * 0.5, 350)
        return 9999

    def get_support_strength(self):
        return sum(x[1] for x in self.bids[:3]), sum(x[1] for x in self.asks[:3])


# ================= 3. 策略引擎 =================
class NordenEngine:
    def __init__(self):
        self.instruments = {'ES': InstrumentData("ES"), 'NQ': InstrumentData("NQ"), 'YM': InstrumentData("YM")}
        self.exec_engine = ExecutionEngine()
        self.ui_queue = queue.Queue()
        self.main_signal = "WAIT...";
        self.signal_bg = "#222";
        self.signal_fg = "white";
        self.sub_msg = ""
        self.speed_info = "";
        self.limit_info = "";
        self.depth_info = ""
        self.vwap_info = "";
        self.vwap_color = "gray";
        self.scratch_alert = "";
        self.conn_status = ""
        self.virtual_position = 0;
        self.last_signal = "NEUTRAL";
        self.last_signal_time = 0;
        self.entry_price = 0

    def process_packet(self, data_str):
        try:
            # print(f"[RX]: {data_str.strip()}")
            parts = data_str.split(',')
            msg_type = parts[0];
            symbol_raw = parts[1]

            if msg_type == 'P':  # 仓位反馈
                if 'ES' in symbol_raw and len(parts) >= 3:
                    new_pos = float(parts[2])
                    if self.virtual_position != int(new_pos):
                        print(f"[SYNC] Position: {self.virtual_position} -> {int(new_pos)}")
                        self.virtual_position = int(new_pos)
                        if self.virtual_position == 0: self.last_signal = "NEUTRAL"
                return

            target_key = None
            for key, keyword in SYMBOL_MAP.items():
                if keyword in symbol_raw: target_key = key; break
            if not target_key: return

            target = self.instruments[target_key];
            now = time.time()
            if msg_type == 'T':
                if len(parts) < 5: return
                target.update_trade(float(parts[2]), float(parts[3]), now, parts[4])
            elif msg_type == 'D':
                if len(parts) < 4: return
                target.update_depth_v2(parts[2], parts[3])
            elif msg_type == 'H':
                target.update_heartbeat()

            if target_key in ['ES', 'NQ']: self.ui_queue.put("CALC")
        except:
            pass

    def check_connections(self):
        now = time.time();
        status_parts = []

        # [修复] 分别检查状态，而不是一刀切
        es_ok = now - self.instruments['ES'].last_packet_time < HEARTBEAT_TIMEOUT
        nq_ok = now - self.instruments['NQ'].last_packet_time < HEARTBEAT_TIMEOUT
        ym_ok = now - self.instruments['YM'].last_packet_time < HEARTBEAT_TIMEOUT

        if es_ok:
            status_parts.append("ES:OK")
        else:
            status_parts.append("ES:--")
        if nq_ok:
            status_parts.append("NQ:OK")
        else:
            status_parts.append("NQ:--")
        if ym_ok:
            status_parts.append("YM:OK")
        else:
            status_parts.append("YM:--")

        self.conn_status = " | ".join(status_parts)

        # 只有当 ES (交易标的) 掉线时才彻底阻断
        if not es_ok:
            self.main_signal = "ES 断开"
            self.signal_bg = "#333";
            self.signal_fg = "#F44"
        elif not nq_ok:
            self.main_signal = "等待 NQ..."  # 提示而不报错
            self.signal_bg = "#333";
            self.signal_fg = "#FA0"

    def update_logic(self):
        es = self.instruments['ES'];
        nq = self.instruments['NQ'];
        ym = self.instruments['YM']

        # 如果 NQ 没数据，暂时用 0 替代，防止报错
        score = 0.0
        if nq.is_connected:
            if nq.get_change() > 0.02:
                score += WEIGHT_NQ
            elif nq.get_change() < -0.02:
                score -= WEIGHT_NQ
        if ym.is_connected:
            if ym.get_change() > 0.01:
                score += WEIGHT_YM
            elif ym.get_change() < -0.01:
                score -= WEIGHT_YM

        dist = es.current_price - es.current_vwap
        if dist < -1.0:
            score += WEIGHT_VWAP
        elif dist > 1.0:
            score -= WEIGHT_VWAP

        limit = es.get_dynamic_limit();
        signal = "NEUTRAL"
        if score > 0.3:
            signal = "BUY"
        elif score < -0.3:
            signal = "SELL"

        # 冲突检测：只有当两者都连接时才检测
        if nq.is_connected and ym.is_connected:
            if abs(score) < 0.1 and nq.get_change() * ym.get_change() < 0: signal = "CONFLICT"

        self.speed_info = f"Spd: {int(es.current_speed)}";
        self.limit_info = f"Lim: {int(limit)}"
        b3, a3 = es.get_support_strength();
        self.depth_info = f"B(3):{int(b3)} | A(3):{int(a3)}"
        if dist > 0:
            self.vwap_info, self.vwap_color = f"P > VWAP (+{dist:.2f})", "#F88"
        else:
            self.vwap_info, self.vwap_color = f"P < VWAP ({dist:.2f})", "#8F8"

        queue = es.bids[0][1] if signal == "BUY" else es.asks[0][1]
        is_good = queue < limit

        final_action = "WAIT"
        if signal == "CONFLICT":
            self.main_signal, self.signal_bg = "⚠️ 震荡", "#DD0"
        elif signal in ["BUY", "SELL"]:
            action_txt = "做多" if signal == "BUY" else "做空";
            color = "#0A0" if signal == "BUY" else "#C00"
            if "阻力" in es.wall_msg and signal == "BUY":
                self.main_signal, self.signal_bg = "🚫 有墙", "#F44"
            elif "支撑" in es.wall_msg and signal == "SELL":
                self.main_signal, self.signal_bg = "🚫 有墙", "#0A0"
            elif is_good:
                self.main_signal, self.signal_bg = f"🟢 {action_txt}", color
                final_action = signal
            else:
                self.main_signal, self.signal_bg = f"⚪ 队满", "#444"
        else:
            self.main_signal, self.signal_bg = "观望", "#111"

        if final_action != self.last_signal:
            if final_action == "BUY":
                self.exec_engine.buy_market();
                self.entry_price = es.current_price;
                self.last_signal_time = time.time()
            elif final_action == "SELL":
                self.exec_engine.sell_market();
                self.entry_price = es.current_price;
                self.last_signal_time = time.time()
            self.last_signal = final_action

        self.scratch_alert = ""
        if self.virtual_position != 0:
            elapsed = time.time() - self.last_signal_time
            move = es.current_price - self.entry_price
            do_s = False
            if elapsed > 10 and abs(move) <= 0.25: do_s = True
            if self.virtual_position == 1 and move <= -0.75: do_s = True
            if self.virtual_position == -1 and move >= 0.75: do_s = True

            if do_s:
                self.scratch_alert = "SCRATCH!"
                self.exec_engine.close_all()
                self.virtual_position = 0

        self.sub_msg = f"Bias:{score:.1f} | Pos:{self.virtual_position}"


# ================= 4. 网络通信 =================
def udp_server_thread(engine):
    print(f"UDP Server Listening on {UDP_DATA_PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_DATA_IP, UDP_DATA_PORT))
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            engine.process_packet(data.decode('utf-8'))
        except:
            pass


# ================= 5. GUI =================
class FinalHUD:
    def __init__(self, root, engine):
        self.root = root;
        self.engine = engine
        self.l_dom_labels = [];
        self.dom_levels = 5
        root.overrideredirect(True);
        root.attributes('-topmost', True)
        root.geometry("400x420+100+100");
        root.configure(bg='#111')
        root.bind('<Button-1>', lambda e: setattr(self, 'x', e.x) or setattr(self, 'y', e.y))
        root.bind('<B1-Motion>',
                  lambda e: root.geometry(f"+{root.winfo_x() + (e.x - self.x)}+{root.winfo_y() + (e.y - self.y)}"))

        self.l_scratch = tk.Label(root, text="", font=("Arial", 12, "bold"), fg="#F44", bg="#111");
        self.l_scratch.pack(fill="x", pady=2)
        self.l_sig = tk.Label(root, text="INIT", font=("微软雅黑", 18, "bold"), fg="white", bg="#333", height=2);
        self.l_sig.pack(fill="x")
        self.l_sub = tk.Label(root, text="", font=("Arial", 10), fg="#CCC", bg="#333");
        self.l_sub.pack(fill="x", pady=(0, 5))

        f_ctx = tk.Frame(root, bg="#111");
        f_ctx.pack(fill="x", pady=2, padx=10)
        self.l_vwap = tk.Label(f_ctx, text="VWAP", font=("微软雅黑", 10), bg="#111", fg="gray");
        self.l_vwap.pack(side="left", expand=True)
        self.l_nq = tk.Label(f_ctx, text="NQ", bg="#111", fg="gray", font=("Consolas", 10));
        self.l_nq.pack(side="left", expand=True)
        self.l_ym = tk.Label(f_ctx, text="YM", bg="#111", fg="gray", font=("Consolas", 10));
        self.l_ym.pack(side="left", expand=True)

        f_dom = tk.Frame(root, bg="#111", pady=5);
        f_dom.pack(fill="x", padx=10)
        self.setup_dom_display(f_dom)

        f_micro = tk.Frame(root, bg="#111");
        f_micro.pack(fill="x", pady=5, padx=10)
        self.l_spd = tk.Label(f_micro, text="Spd", bg="#111", fg="cyan", font=("Arial", 9));
        self.l_spd.pack(side="left", expand=True)
        self.l_lim = tk.Label(f_micro, text="Lim", bg="#111", fg="#FA0", font=("Arial", 9));
        self.l_lim.pack(side="left", expand=True)
        self.l_tape = tk.Label(f_micro, text="---", bg="#111", fg="#DDD", font=("Consolas", 9));
        self.l_tape.pack(side="left", expand=True)
        self.l_conn = tk.Label(root, text="READY", font=("Arial", 7), fg="#444", bg="#111");
        self.l_conn.pack(side="bottom")
        self.ui_loop()

    def setup_dom_display(self, parent):
        tk.Label(parent, text="ASK", fg="#DDD", bg="#333").grid(row=0, column=0, sticky="ew")
        tk.Label(parent, text="PRICE", fg="#DDD", bg="#333").grid(row=0, column=1, sticky="ew")
        tk.Label(parent, text="BID", fg="#DDD", bg="#333").grid(row=0, column=2, sticky="ew")
        parent.grid_columnconfigure(0, weight=1);
        parent.grid_columnconfigure(1, weight=1);
        parent.grid_columnconfigure(2, weight=1)
        for i in range(self.dom_levels):
            l_ask = tk.Label(parent, text="--", fg="#F44", bg="#111", font=("Consolas", 10, "bold"))
            l_prc = tk.Label(parent, text="--.--", fg="yellow", bg="#111", font=("Consolas", 10))
            l_bid = tk.Label(parent, text="--", fg="#0A0", bg="#111", font=("Consolas", 10, "bold"))
            l_ask.grid(row=i + 1, column=0);
            l_prc.grid(row=i + 1, column=1);
            l_bid.grid(row=i + 1, column=2)
            self.l_dom_labels.append((l_ask, l_prc, l_bid))

    def update_dom(self, es):
        if not es.prices: return
        for i in range(5):
            la, lp, lb = self.l_dom_labels[i]
            ask_item = es.asks[i];
            bid_item = es.bids[i]
            la.config(text=str(int(ask_item[1])) if ask_item[1] > 0 else "")
            lb.config(text=str(int(bid_item[1])) if bid_item[1] > 0 else "")
            p_ask = f"{ask_item[0]:.2f}" if ask_item[0] > 0 else "--"
            p_bid = f"{bid_item[0]:.2f}" if bid_item[0] > 0 else "--"
            if ask_item[0] == 0: p_ask = "--"
            if bid_item[0] == 0: p_bid = "--"
            lp.config(text=f"{p_ask} | {p_bid}")

    def ui_loop(self):
        while not self.engine.ui_queue.empty(): self.engine.ui_queue.get(); self.engine.update_logic()
        eng = self.engine;
        eng.check_connections()
        self.l_sig.config(text=eng.main_signal, bg=eng.signal_bg, fg=eng.signal_fg)
        self.l_sub.config(text=eng.sub_msg, bg=eng.signal_bg)
        if eng.scratch_alert:
            self.l_scratch.config(text=eng.scratch_alert, bg="#300")
        else:
            self.l_scratch.config(text="", bg="#111")
        self.l_nq.config(text=f"NQ:{eng.instruments['NQ'].get_change():+.2f}%",
                         fg="#0F0" if eng.instruments['NQ'].get_change() > 0 else "#F44")
        self.l_ym.config(text=f"YM:{eng.instruments['YM'].get_change():+.2f}%",
                         fg="#0F0" if eng.instruments['YM'].get_change() > 0 else "#F44")
        self.l_vwap.config(text=eng.vwap_info, fg=eng.vwap_color)
        self.l_spd.config(text=eng.speed_info);
        self.l_lim.config(text=eng.limit_info)
        self.l_tape.config(text=eng.instruments['ES'].last_trade_msg)
        self.l_conn.config(text=f"CONN: {eng.conn_status}")
        self.update_dom(eng.instruments['ES'])
        self.root.after(50, self.ui_loop)


if __name__ == "__main__":
    eng = NordenEngine()
    t = threading.Thread(target=udp_server_thread, args=(eng,), daemon=True)
    t.start()
    root = tk.Tk()
    FinalHUD(root, eng)
    root.mainloop()