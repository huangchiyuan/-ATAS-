import duckdb
import pandas as pd
import datetime
import os
import json

# --- 配置 (与 data_recorder.py 保持一致) ---
DB_PREFIX = "market_data"


def parse_dom_string(dom_str: str) -> list[tuple[float, float]]:
    """
    解析 C# 发送的 '|' 和 '@' 分隔的 DOM 字符串。
    格式示例: "6734.25@17|6734.00@28|..."
    返回: [(price, volume), ...]
    """
    levels = []
    if not dom_str:
        return levels

    # 检查是否为 C# 端的 '0@0' 占位符
    if dom_str == '0@0':
        return levels

    try:
        # 分割成 Level 组: "Price@Volume"
        for level_str in dom_str.split('|'):
            if not level_str: continue

            # 分割 Price 和 Volume
            parts = level_str.split('@')
            if len(parts) == 2:
                price = float(parts[0])
                volume = float(parts[1])
                # 排除 C# 填补的 0@0 占位符
                if price > 0 and volume > 0:
                    levels.append((price, volume))
    except Exception as e:
        print(f"⚠️ 解析 DOM 错误: {e}. 原始数据: {dom_str[:50]}...")
        return []

    return levels


def load_data_for_backtest(date_str: str, symbol: str):
    """
    从指定日期的 DuckDB 文件中加载 Tick 和 DOM 数据。

    Args:
        date_str (str): 交易所日期，格式 YYYY-MM-DD (e.g., '2025-11-20')
        symbol (str): 市场代码 (e.g., 'ES', 'NQ')

    Returns:
        tuple: (pd.DataFrame for Ticks, pd.DataFrame for Depth)
    """
    db_file = f"{DB_PREFIX}_{date_str}.duckdb"

    if not os.path.exists(db_file):
        print(f"❌ 错误: 数据库文件未找到: {db_file}")
        return None, None

    conn = duckdb.connect(db_file)

    print(f"⏳ 正在加载 {symbol} ({date_str}) 数据...")

    # 1. Tick 数据加载 (最关键的回测数据)
    # DuckDB 会自动将 TIMESTAMP 列加载为 Pandas datetime 格式
    tick_query = f"""
        SELECT 
            exchange_time, price, volume, side, recv_time 
        FROM ticks 
        WHERE symbol = '{symbol}'
        ORDER BY exchange_time ASC;
    """
    df_ticks = conn.execute(tick_query).df()

    # 2. DOM 数据加载
    depth_query = f"""
        SELECT 
            exchange_time, bids, asks, recv_time 
        FROM depth 
        WHERE symbol = '{symbol}'
        ORDER BY exchange_time ASC;
    """
    df_depth = conn.execute(depth_query).df()

    conn.close()

    # 3. 数据解析与清理

    # 将时间戳设置为索引 (回测框架标准)
    df_ticks = df_ticks.set_index('exchange_time')

    # 解析 DOM 字符串为可用的结构 (创建新的列)
    df_depth['parsed_bids'] = df_depth['bids'].apply(parse_dom_string)
    df_depth['parsed_asks'] = df_depth['asks'].apply(parse_dom_string)

    print(f"✅ Tick 数据加载完成: {len(df_ticks)} 条")
    print(f"✅ Depth 数据加载完成: {len(df_depth)} 条")

    return df_ticks, df_depth


# =========================================================
# 演示区：如何使用
# =========================================================

if __name__ == "__main__":
    # 请输入您的录制日期和市场代码
    RECORD_DATE = "2025-11-20"
    TARGET_SYMBOL = "ES"

    df_ticks, df_depth = load_data_for_backtest(RECORD_DATE, TARGET_SYMBOL)

    if df_ticks is not None:
        print("\n--- 1. Tick 数据预览 (已按交易所时间排序) ---")
        print(df_ticks.head(5))

        print("\n--- 2. 深度数据解析预览 (Bids/Asks 已转为列表) ---")

        # 打印 DOM 数据的第一个解析结果
        first_depth_row = df_depth.iloc[0]

        print(f"时间: {first_depth_row['exchange_time']}")
        print(f"Bid 1st Level: {first_depth_row['parsed_bids'][0]}")
        print(f"Ask 1st Level: {first_depth_row['parsed_asks'][0]}")
        print("\n您可以使用 df_ticks 和 df_depth 进行策略回测。")