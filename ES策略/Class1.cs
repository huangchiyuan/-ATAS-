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
using System.Collections.Concurrent;
using ATAS.Strategies.Chart;
using ATAS.Indicators;
using ATAS.DataFeedsCore;

// 【核心修复1】引用正确的日志命名空间 (解决 CS0234 和 CS0176)
using Utils.Common.Logging;

// 【核心修复2】指定别名防止 TradeDirection 类型冲突 (解决 CS0104 和 CS0019)
using IndTradeDirection = ATAS.Indicators.TradeDirection;

namespace ATAS.Strategies.Technical
{
    [DisplayName("NFQE Commander V3.5 (Final Stable)")]
    public class NFQE_AutoStrategy : ChartStrategy
    {
        #region Parameters

        [Display(Name = "UDP Port (Data Out)", GroupName = "Connection", Order = 10)]
        public int PortOut { get; set; } = 5555;

        [Display(Name = "UDP Port (Cmd In)", GroupName = "Connection", Order = 20)]
        public int PortIn { get; set; } = 6666;

        [Display(Name = "Trade Volume", GroupName = "Trading", Order = 30)]
        public decimal TradeVolume { get; set; } = 1;

        [Display(Name = "Max Position", GroupName = "Trading", Order = 40)]
        public decimal MaxPosition { get; set; } = 5;

        [Display(Name = "Close on Stop", GroupName = "Risk", Order = 50)]
        public bool CloseOnStop { get; set; } = true;

        #endregion

        #region Private Fields

        private UdpClient _udpSender;
        private UdpClient _udpReceiver;
        private IPEndPoint _endPointOut;
        private bool _isRunning = true;

        // 指令队列与订单管理
        private readonly ConcurrentQueue<string> _commandQueue = new ConcurrentQueue<string>();
        private readonly List<Order> _activeOrders = new List<Order>();
        private readonly object _stateLock = new object();

        #endregion

        protected override void OnInitialize()
        {
            try
            {
                _udpSender = new UdpClient();
                _endPointOut = new IPEndPoint(IPAddress.Parse("127.0.0.1"), PortOut);

                // --- 接收端防丢包配置 ---
                _udpReceiver = new UdpClient();
                _udpReceiver.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);

                // 【关键优化】设置 10MB 接收缓冲区，防止本机高频丢包
                _udpReceiver.Client.ReceiveBufferSize = 10 * 1024 * 1024;
                _udpReceiver.Client.Bind(new IPEndPoint(IPAddress.Any, PortIn));

                // 启动后台线程
                Task.Run(() => ListenForCommands());
                Task.Run(() => ProcessCommandLoop());
                Task.Run(() => HeartbeatLoop());
            }
            catch (Exception ex)
            {
                // 【核心修复3】补全参数：Message, Title, Level (解决 CS1503)
                RaiseShowNotification($"Init Error: {ex.Message}", "NFQE Error", LoggingLevel.Error);
            }
        }

        // ================= UDP 监听 (生产者) =================
        private async Task ListenForCommands()
        {
            try
            {
                while (_isRunning && _udpReceiver != null)
                {
                    var result = await _udpReceiver.ReceiveAsync();
                    string cmd = Encoding.ASCII.GetString(result.Buffer).Trim();
                    if (!string.IsNullOrEmpty(cmd)) _commandQueue.Enqueue(cmd);
                }
            }
            catch { }
        }

        // ================= 指令处理 (消费者) =================
        private async Task ProcessCommandLoop()
        {
            while (_isRunning)
            {
                if (_commandQueue.TryDequeue(out string cmd))
                {
                    if (Application.Current != null)
                        await Application.Current.Dispatcher.InvokeAsync(() => ExecuteCommandSafe(cmd));
                }
                else
                {
                    await Task.Delay(5);
                }
            }
        }

        // ================= 核心执行逻辑 =================
        private void ExecuteCommandSafe(string cmd)
        {
            try
            {
                var parts = cmd.Split(',');
                string action = parts[0].ToUpper();
                decimal projectedPos = CalculateProjectedPosition();

                // 2. 风控拦截
                if (Math.Abs(projectedPos) >= MaxPosition)
                {
                    bool isBuy = action.Contains("BUY") || action == "JOIN_BID";
                    bool isSell = action.Contains("SELL") || action == "JOIN_ASK";
                    if ((isBuy && projectedPos > 0) || (isSell && projectedPos < 0)) return;
                }

                // 3. 执行指令
                switch (action)
                {
                    case "BUY_MARKET":
                        PlaceOrder(OrderDirections.Buy, OrderTypes.Market);
                        break;

                    case "SELL_MARKET":
                        PlaceOrder(OrderDirections.Sell, OrderTypes.Market);
                        break;

                    case "BUY_LIMIT":
                        if (parts.Length > 1 && decimal.TryParse(parts[1], out decimal blPrice))
                            PlaceOrder(OrderDirections.Buy, OrderTypes.Limit, blPrice);
                        break;

                    case "SELL_LIMIT":
                        if (parts.Length > 1 && decimal.TryParse(parts[1], out decimal slPrice))
                            PlaceOrder(OrderDirections.Sell, OrderTypes.Limit, slPrice);
                        break;

                    case "MODIFY":
                        if (parts.Length > 2 &&
                            decimal.TryParse(parts[1], out decimal oldPrice) &&
                            decimal.TryParse(parts[2], out decimal newPrice))
                        {
                            ModifyOrderByPrice(oldPrice, newPrice);
                        }
                        break;

                    case "JOIN_BID":
                        var bestBid = GetBestPrice(IndTradeDirection.Sell);
                        if (bestBid > 0) PlaceOrder(OrderDirections.Buy, OrderTypes.Limit, bestBid);
                        break;

                    case "JOIN_ASK":
                        var bestAsk = GetBestPrice(IndTradeDirection.Buy);
                        if (bestAsk > 0) PlaceOrder(OrderDirections.Sell, OrderTypes.Limit, bestAsk);
                        break;

                    case "CANCEL_ALL":
                        CancelAllOrders();
                        break;

                    case "CLOSE_ALL":
                    case "SCRATCH":
                        CancelAllOrders();
                        ClosePosition();
                        break;
                }
            }
            catch (Exception ex)
            {
                RaiseShowNotification($"Exec Error: {ex.Message}", "NFQE Error", LoggingLevel.Error);
            }
        }

        // ================= 订单管理增强 =================

        private void ModifyOrderByPrice(decimal oldPrice, decimal newPrice)
        {
            lock (_stateLock)
            {
                // 使用 OrderStates.Active 替代旧版的 Pending/Working (解决 CS0117)
                var targetOrder = _activeOrders.FirstOrDefault(o =>
                    o.State == OrderStates.Active &&
                    Math.Abs(o.Price - oldPrice) < Security.TickSize / 2);

                if (targetOrder != null)
                {
                    var newOrder = new Order
                    {
                        Portfolio = targetOrder.Portfolio,
                        Security = targetOrder.Security,
                        Direction = targetOrder.Direction,
                        Type = targetOrder.Type,
                        QuantityToFill = targetOrder.QuantityToFill,
                        Price = newPrice,
                        TriggerPrice = targetOrder.TriggerPrice
                    };
                    ModifyOrder(targetOrder, newOrder);
                }
            }
        }

        private decimal CalculateProjectedPosition()
        {
            lock (_stateLock)
            {
                decimal projected = CurrentPosition;

                foreach (var order in _activeOrders)
                {
                    if (order.State == OrderStates.Active)
                    {
                        decimal pendingVol = order.QuantityToFill;

                        if (order.Direction == OrderDirections.Buy)
                            projected += pendingVol;
                        else
                            projected -= pendingVol;
                    }
                }
                return projected;
            }
        }

        private void PlaceOrder(OrderDirections direction, OrderTypes type, decimal price = 0)
        {
            var order = new Order
            {
                Portfolio = Portfolio,
                Security = Security,
                Direction = direction,
                Type = type,
                QuantityToFill = TradeVolume,
                Price = (type == OrderTypes.Limit) ? price : 0
            };

            lock (_stateLock) { _activeOrders.Add(order); }
            OpenOrder(order);
        }

        protected override void OnOrderChanged(Order order)
        {
            lock (_stateLock)
            {
                // 只要不是 Active，就视为已终结
                if (order.State != OrderStates.Active)
                {
                    _activeOrders.RemoveAll(x => x == order);
                }
                else
                {
                    if (!_activeOrders.Contains(order)) _activeOrders.Add(order);
                }
            }
        }

        protected override void OnOrderRegisterFailed(Order order, string message)
        {
            lock (_stateLock) { _activeOrders.Remove(order); }
        }

        private void CancelAllOrders()
        {
            lock (_stateLock)
            {
                foreach (var order in _activeOrders.ToList())
                {
                    if (order.State == OrderStates.Active)
                        CancelOrder(order);
                }
            }
        }

        private void ClosePosition()
        {
            if (CurrentPosition != 0)
            {
                var order = new Order
                {
                    Portfolio = Portfolio,
                    Security = Security,
                    Type = OrderTypes.Market,
                    QuantityToFill = Math.Abs(CurrentPosition),
                    Direction = CurrentPosition > 0 ? OrderDirections.Sell : OrderDirections.Buy
                };
                OpenOrder(order);
            }
        }

        // 使用别名 IndTradeDirection
        private decimal GetBestPrice(IndTradeDirection dir)
        {
            if (MarketDepthInfo == null) return 0;
            var snapshot = MarketDepthInfo.GetMarketDepthSnapshot();
            if (snapshot == null) return 0;

            if (dir == IndTradeDirection.Sell) // Bid
                return snapshot.Where(x => x.Direction == IndTradeDirection.Sell).OrderByDescending(x => x.Price).FirstOrDefault()?.Price ?? 0;
            else // Ask
                return snapshot.Where(x => x.Direction == IndTradeDirection.Buy).OrderBy(x => x.Price).FirstOrDefault()?.Price ?? 0;
        }

        // ================= 数据发送逻辑 =================
        protected override void OnStopping()
        {
            if (CloseOnStop) { CancelAllOrders(); ClosePosition(); }
            _isRunning = false; _udpSender?.Close(); _udpReceiver?.Close();
            base.OnStopping();
        }

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
                await Task.Delay(1000);
            }
        }

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

                var bids = allData.Where(x => x.Direction == IndTradeDirection.Sell).OrderByDescending(x => x.Price).Take(5).ToList();
                var asks = allData.Where(x => x.Direction == IndTradeDirection.Buy).OrderBy(x => x.Price).Take(5).ToList();

                string bidsStr = "";
                for (int i = 0; i < 5; i++) bidsStr += (i < bids.Count) ? $"{bids[i].Price}@{bids[i].Volume}|" : "0@0|";
                string asksStr = "";
                for (int i = 0; i < 5; i++) asksStr += (i < asks.Count) ? $"{asks[i].Price}@{asks[i].Volume}|" : "0@0|";

                SendRaw($"D,{InstrumentInfo.Instrument},{bidsStr},{asksStr}");
            }
            catch { }
        }

        protected override void OnPositionChanged(Position position)
        {
            if (InstrumentInfo != null) SendRaw($"P,{InstrumentInfo.Instrument},{position.Volume}");
        }

        protected override void OnCalculate(int bar, decimal value) { }

        private void SendRaw(string message)
        {
            if (_udpSender != null) { try { byte[] bytes = Encoding.ASCII.GetBytes(message); _udpSender.Send(bytes, bytes.Length, _endPointOut); } catch { } }
        }
    }
}