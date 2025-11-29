using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Linq;
using System.Collections.Generic;
using System.Timers; // [新增] 用于心跳
using ATAS.Indicators;
using ATAS.Indicators.Technical;

// 解决 Timer 冲突
using Timer = System.Timers.Timer;

namespace ATAS.Indicators.Technical
{
    [DisplayName("NFQE Bridge V18.0 (Heartbeat + Sync)")]
    [Category("Norden Flow")]
    public class NFQE_Bridge_UDP : Indicator
    {
        [Display(Name = "UDP Port", GroupName = "Connection")]
        public int Port { get; set; } = 5555;

        private UdpClient _udpClient;
        private IPEndPoint _endPoint;
        private Timer _heartbeatTimer; // [新增] 心跳定时器
        private string _lastDepthHash = "";

        protected override void OnInitialize()
        {
            try
            {
                // 初始化 UDP
                _udpClient = new UdpClient();
                _endPoint = new IPEndPoint(IPAddress.Parse("127.0.0.1"), Port);

                // [新增] 启动心跳定时器 (1秒一次)
                // 确保 Python 端的 check_connections 不会因为 NQ/YM 暂时没成交而误报断开
                _heartbeatTimer = new Timer(1000);
                _heartbeatTimer.Elapsed += OnHeartbeat;
                _heartbeatTimer.AutoReset = true;
                _heartbeatTimer.Enabled = true;
            }
            catch { }
        }

        private void EnsureConnection()
        {
            if (_udpClient == null)
            {
                try
                {
                    _udpClient = new UdpClient();
                    _endPoint = new IPEndPoint(IPAddress.Parse("127.0.0.1"), Port);
                }
                catch { }
            }
        }

        // [新增] 心跳发送逻辑
        private void OnHeartbeat(object sender, ElapsedEventArgs e)
        {
            try
            {
                if (this.InstrumentInfo != null)
                {
                    EnsureConnection();
                    // 格式: H,Symbol,Timestamp
                    SendRaw($"H,{this.InstrumentInfo.Instrument},{DateTime.Now.Ticks}");
                }
            }
            catch { }
        }

        protected override void OnNewTrade(MarketDataArg arg)
        {
            try
            {
                if (this.Container == null || this.InstrumentInfo == null) return;
                EnsureConnection();

                string side = "NONE";
                // [优化] 使用 ToString() 比较，与策略端保持一致，避免 CS0019 错误
                string dirStr = arg.Direction.ToString();
                if (dirStr == "Buy") side = "BUY";
                else if (dirStr == "Sell") side = "SELL";

                // 发送成交: T,Symbol,Price,Volume,Side
                SendRaw($"T,{this.InstrumentInfo.Instrument},{arg.Price},{arg.Volume},{side}");
            }
            catch { }
        }

        protected override void OnBestBidAskChanged(MarketDataArg arg)
        {
            try
            {
                if (this.Container == null || this.InstrumentInfo == null) return;

                var provider = this.MarketDepthInfo;
                if (provider == null) return;

                var snapshot = provider.GetMarketDepthSnapshot();
                if (snapshot == null) return;

                var allData = snapshot.ToList();

                // [逻辑保持] Buy=Asks(升序), Sell=Bids(降序)
                var bids = allData
                    .Where(x => x.Direction.ToString() == "Sell")
                    .OrderByDescending(x => x.Price)
                    .Take(5)
                    .ToList();

                var asks = allData
                    .Where(x => x.Direction.ToString() == "Buy")
                    .OrderBy(x => x.Price)
                    .Take(5)
                    .ToList();

                // 拼接 Price@Volume
                string bidsStr = "";
                for (int i = 0; i < 5; i++)
                {
                    if (i < bids.Count) bidsStr += $"{bids[i].Price}@{bids[i].Volume}|";
                    else bidsStr += "0@0|";
                }

                string asksStr = "";
                for (int i = 0; i < 5; i++)
                {
                    if (i < asks.Count) asksStr += $"{asks[i].Price}@{asks[i].Volume}|";
                    else asksStr += "0@0|";
                }

                // 发送深度: D,Symbol,Bids,Asks
                string msg = $"D,{this.InstrumentInfo.Instrument},{bidsStr},{asksStr}";

                if (msg != _lastDepthHash)
                {
                    EnsureConnection();
                    SendRaw(msg);
                    _lastDepthHash = msg;
                }
            }
            catch { }
        }

        protected override void OnCalculate(int bar, decimal value) { }

        private void SendRaw(string message)
        {
            if (_udpClient != null && _endPoint != null)
            {
                try
                {
                    byte[] bytes = Encoding.ASCII.GetBytes(message);
                    _udpClient.Send(bytes, bytes.Length, _endPoint);
                }
                catch { }
            }
        }

        public override void Dispose()
        {
            _heartbeatTimer?.Stop(); // 停止心跳
            _udpClient?.Close();
            base.Dispose();
        }
    }
}