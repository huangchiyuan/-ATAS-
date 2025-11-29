using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;
using System.Linq;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows;
using System.Collections.Generic;
using ATAS.Strategies.Chart;
using ATAS.Indicators;
using ATAS.DataFeedsCore;

namespace ATAS.Strategies.Technical
{
    [DisplayName("NFQE Commander V2.0 (Final Auto)")]
    public class NFQE_AutoStrategy : ChartStrategy
    {
        [Display(Name = "UDP Port (Data Out)", GroupName = "Connection", Order = 10)]
        public int PortOut { get; set; } = 5555;

        [Display(Name = "UDP Port (Cmd In)", GroupName = "Connection", Order = 20)]
        public int PortIn { get; set; } = 6666;

        [Display(Name = "Trade Volume", GroupName = "Trading", Order = 30)]
        public decimal TradeVolume { get; set; } = 1;

        [Display(Name = "Max Position", GroupName = "Trading", Order = 40)]
        public decimal MaxPosition { get; set; } = 5;

        private UdpClient _udpSender;
        private UdpClient _udpReceiver;
        private IPEndPoint _endPointOut;
        private bool _isRunning = true;

        protected override void OnInitialize()
        {
            try
            {
                _udpSender = new UdpClient();
                _endPointOut = new IPEndPoint(IPAddress.Parse("127.0.0.1"), PortOut);

                // 启动指令监听
                Task.Run(() => ListenForCommands());

                // [核心修复] 启动心跳循环 (每1秒发送一次)
                // 这能防止 Python 误判为“断开”
                Task.Run(() => HeartbeatLoop());
            }
            catch { }
        }

        // ================= 心跳逻辑 =================
        private async Task HeartbeatLoop()
        {
            while (_isRunning)
            {
                try
                {
                    if (InstrumentInfo != null)
                        SendRaw($"H,{InstrumentInfo.Instrument},{DateTime.Now.Ticks}");
                }
                catch { }
                await Task.Delay(1000); // 1秒一次
            }
        }

        // ================= 数据发送 =================
        protected override void OnNewTrade(MarketDataArg arg)
        {
            if (InstrumentInfo == null) return;
            string side = arg.Direction.ToString() == "Buy" ? "BUY" : (arg.Direction.ToString() == "Sell" ? "SELL" : "NONE");
            SendRaw($"T,{InstrumentInfo.Instrument},{arg.Price},{arg.Volume},{side}");
        }

        protected override void OnBestBidAskChanged(MarketDataArg arg)
        {
            if (InstrumentInfo == null || MarketDepthInfo == null) return;
            try
            {
                var snapshot = MarketDepthInfo.GetMarketDepthSnapshot();
                if (snapshot == null) return;
                var allData = snapshot.ToList();

                // 获取 5 档深度
                var bids = allData.Where(x => x.Direction.ToString() == "Sell").OrderByDescending(x => x.Price).Take(5).ToList();
                var asks = allData.Where(x => x.Direction.ToString() == "Buy").OrderBy(x => x.Price).Take(5).ToList();

                string bidsStr = "";
                for (int i = 0; i < 5; i++) bidsStr += (i < bids.Count) ? $"{bids[i].Price}@{bids[i].Volume}|" : "0@0|";

                string asksStr = "";
                for (int i = 0; i < 5; i++) asksStr += (i < asks.Count) ? $"{asks[i].Price}@{asks[i].Volume}|" : "0@0|";

                SendRaw($"D,{InstrumentInfo.Instrument},{bidsStr},{asksStr}");
            }
            catch { }
        }

        // ================= 仓位反馈 =================
        protected override void OnPositionChanged(Position position)
        {
            // 发送净持仓给 Python
            if (InstrumentInfo != null)
                SendRaw($"P,{InstrumentInfo.Instrument},{position.Volume}");
        }

        // ================= 交易执行 =================
        private async Task ListenForCommands()
        {
            try
            {
                _udpReceiver = new UdpClient(PortIn);
                while (_isRunning)
                {
                    var result = await _udpReceiver.ReceiveAsync();
                    string cmd = Encoding.ASCII.GetString(result.Buffer).Trim();
                    if (Application.Current != null)
                        await Application.Current.Dispatcher.InvokeAsync(() => ProcessCommand(cmd));
                }
            }
            catch { }
        }

        private void ProcessCommand(string cmd)
        {
            try
            {
                var parts = cmd.Split(',');
                string action = parts[0].ToUpper();
                var netPos = CurrentPosition;

                // 风控检查
                if (Math.Abs(netPos) >= MaxPosition && action.Contains("MARKET"))
                {
                    // 如果已达最大持仓，禁止同向开仓
                    if ((action.Contains("BUY") && netPos > 0) || (action.Contains("SELL") && netPos < 0)) return;
                }

                if (action == "BUY_MARKET")
                {
                    var order = new Order { Portfolio = Portfolio, Security = Security, Direction = OrderDirections.Buy, Type = OrderTypes.Market, QuantityToFill = TradeVolume };
                    OpenOrder(order);
                }
                else if (action == "SELL_MARKET")
                {
                    var order = new Order { Portfolio = Portfolio, Security = Security, Direction = OrderDirections.Sell, Type = OrderTypes.Market, QuantityToFill = TradeVolume };
                    OpenOrder(order);
                }
                else if (action == "CLOSE_ALL" || action == "SCRATCH")
                {
                    if (netPos != 0)
                    {
                        var order = new Order { Portfolio = Portfolio, Security = Security, Type = OrderTypes.Market, QuantityToFill = Math.Abs(netPos) };
                        order.Direction = netPos > 0 ? OrderDirections.Sell : OrderDirections.Buy;
                        OpenOrder(order);
                    }
                }
            }
            catch { }
        }

        protected override void OnCalculate(int bar, decimal value) { }

        private void SendRaw(string message)
        {
            if (_udpSender != null)
            {
                byte[] bytes = Encoding.ASCII.GetBytes(message);
                _udpSender.Send(bytes, bytes.Length, _endPointOut);
            }
        }

        public override void Dispose()
        {
            _isRunning = false;
            _udpSender?.Close();
            _udpReceiver?.Close();
            base.Dispose();
        }
    }
}