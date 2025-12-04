using System;
using System.Linq;
using ATAS.Indicators;
using ATAS.DataFeedsCore;
using Utils.Common.Logging;
using WebSocketSharp;
using Newtonsoft.Json;

namespace ATASDataGateway
{
    public class Level2ToPythonGateway : Indicator
    {
        private const string WsUrl = "ws://127.0.0.1:8765";   // ← 改成你 Python 监听的端口
        private const int MaxDepth = 10;                    // 每边发几档

        private static WebSocket _ws;

        protected override void OnInitialize()
        {
            try
            {
                _ws = new WebSocket(WsUrl);
                _ws.OnError += (s, e) => this.LogInfo($"[WS 错误] {e.Message}");
                _ws.Connect();
                this.LogInfo($"Level2 → Python 网关启动成功 → {WsUrl}");
            }
            catch (Exception ex)
            {
                this.LogInfo($"[WS 连接失败] {ex.Message}");
            }
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            if (_ws == null || !_ws.IsAlive) return;

            var snapshot = MarketDepthInfo.GetMarketDepthSnapshot();
            if (snapshot == null || !snapshot.Any()) return;

            // ========== 关键修复：彻底避开所有可能出错的 Symbol 获取方式 ==========
            // 直接用 Instrument.ToString()，在所有 ATAS 版本里都返回合约代码（如 ESZ4、BTCUSD）
            string symbol = Instrument?.ToString() ?? "UnknownSymbol";
            // =====================================================================

            var bids = snapshot.Where(x => x.IsBid)
                               .OrderByDescending(x => x.Price)
                               .Take(MaxDepth)
                               .Select(x => new[] { (double)x.Price, (double)x.Volume });

            var asks = snapshot.Where(x => !x.IsBid)
                               .OrderBy(x => x.Price)
                               .Take(MaxDepth)
                               .Select(x => new[] { (double)x.Price, (double)x.Volume });

            var payload = new
            {
                timestamp = DateTime.Now.ToString("HH:mm:ss.fff"),
                symbol = symbol,                     // 完美兼容所有版本
                bids = bids.ToArray(),
                asks = asks.ToArray(),
                total_bid_vol = MarketDepthInfo.CumulativeDomBids,
                total_ask_vol = MarketDepthInfo.CumulativeDomAsks
            };

            try
            {
                _ws.Send(JsonConvert.SerializeObject(payload, Formatting.None));
            }
            catch (Exception ex)
            {
                this.LogInfo($"[发送失败] {ex.Message}");
            }
        }

        // 官方文档推荐的真正可 override 的清理方法
        protected override void OnDispose()
        {
            try { _ws?.Close(); } catch { }
            this.LogInfo("Level2 → Python 网关已关闭");
        }
    }
}