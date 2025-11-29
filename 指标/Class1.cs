using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Collections.Generic;
using System.Linq;
// [修复] 引用正确的命名空间，解决颜色冲突
using WpfColors = System.Windows.Media.Colors;
using GdiColor = System.Drawing.Color;

using ATAS.Indicators;
using ATAS.Indicators.Technical;

namespace ATAS.Indicators.Technical
{
    [DisplayName("NFQE Visualizer V6.3 (Smart Weighting)")]
    [Category("Norden Flow")]
    public class NFQE_Visualizer_Pro : Indicator
    {
        // ================= 1. 极简参数定义 =================

        [Display(Name = "Period Low (Slow Market)")]
        public int PeriodLow { get; set; } = 500; // [优化] 从 3000 降至 1000，适应亚盘

        [Display(Name = "Period High (Fast Market)")]
        public int PeriodHigh { get; set; } = 300; // [优化] 更灵敏，紧贴美盘爆发

        [Display(Name = "Min Trade Size (Smart Filter)")]
        public int MinTradeSize { get; set; } = 2; // [核心] 忽略 < 2手的单子 (过滤噪音)

        // ================= 2. 可视化线条 =================
        private ValueDataSeries _vwapLine = new ValueDataSeries("Smart uVWAP");
        private ValueDataSeries _bidLine = new ValueDataSeries("Bid Value Line");
        private ValueDataSeries _askLine = new ValueDataSeries("Ask Value Line");

        // ================= 3. 内部变量 =================
        private LinkedList<Tuple<decimal, decimal>> _history = new LinkedList<Tuple<decimal, decimal>>();
        private LinkedList<DateTime> _flowWindow = new LinkedList<DateTime>();
        private decimal _sumPV = 0;
        private decimal _sumVol = 0;
        private decimal _currentVWAP = 0;
        private int _dynamicPeriod = 500;

        protected override void OnInitialize()
        {
            // 初始化颜色 (WPF)
            _vwapLine.Color = WpfColors.Gold;
            _vwapLine.Width = 2;
            _vwapLine.VisualType = VisualMode.Line;

            _bidLine.Color = WpfColors.Gray;
            _bidLine.Width = 2;
            _bidLine.VisualType = VisualMode.Line;

            _askLine.Color = WpfColors.Gray;
            _askLine.Width = 2;
            _askLine.VisualType = VisualMode.Line;

            DataSeries.Add(_vwapLine);
            DataSeries.Add(_bidLine);
            DataSeries.Add(_askLine);

            DenyToChangePanel = true;
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            if (bar == CurrentBar - 1)
                _vwapLine[bar] = _currentVWAP;
        }

        protected override void OnNewTrade(MarketDataArg arg)
        {
            // 1. 更新流速 (始终计算所有订单，因为这代表市场热度)
            UpdateFlowRate(arg.Time);

            // 2. 计算动态周期
            int tpm = _flowWindow.Count;
            // 映射逻辑调整：更激进的自适应
            if (tpm <= 30) _dynamicPeriod = PeriodLow; // 极慢 (亚盘午休)
            else if (tpm >= 300) _dynamicPeriod = PeriodHigh; // 极快 (美盘开盘)
            else
            {
                double ratio = (tpm - 30) / 270.0;
                _dynamicPeriod = PeriodLow + (int)((PeriodHigh - PeriodLow) * ratio);
            }

            // 3. [核心逻辑] 智能过滤 (Smart Weighting)
            // 如果当前单子小于阈值，直接忽略，不计入 VWAP 计算
            // 这意味着 VWAP 线只会因为“大单”而移动
            if (arg.Volume < MinTradeSize)
            {
                // 即使不更新数值，也要刷新线条显示
                _vwapLine[CurrentBar - 1] = _currentVWAP;
                return;
            }

            // 4. 更新 VWAP 数据 (只包含有效大单)
            _history.AddLast(new Tuple<decimal, decimal>(arg.Price, arg.Volume));
            _sumPV += arg.Price * arg.Volume;
            _sumVol += arg.Volume;

            // 5. 动态调整窗口
            while (_history.Count > _dynamicPeriod)
            {
                var old = _history.First.Value;
                _history.RemoveFirst();
                _sumPV -= old.Item1 * old.Item2;
                _sumVol -= old.Item2;
            }

            // 6. 计算 Smart uVWAP
            if (_sumVol > 0) _currentVWAP = _sumPV / _sumVol;
            else _currentVWAP = arg.Price;

            _vwapLine[CurrentBar - 1] = _currentVWAP;
        }

        private void UpdateFlowRate(DateTime now)
        {
            _flowWindow.AddLast(now);
            // 60秒流速窗口
            while (_flowWindow.Count > 0 && (now - _flowWindow.First.Value).TotalSeconds > 60)
            {
                _flowWindow.RemoveFirst();
            }
        }

        protected override void OnBestBidAskChanged(MarketDataArg arg)
        {
            try
            {
                var provider = this.MarketDepthInfo;
                if (provider == null) return;

                var snapshot = provider.GetMarketDepthSnapshot();
                if (snapshot == null) return;
                var allData = snapshot.ToList();

                var bestBidOrder = allData.Where(x => x.Direction == TradeDirection.Buy).OrderByDescending(x => x.Price).FirstOrDefault();
                var bestAskOrder = allData.Where(x => x.Direction == TradeDirection.Sell).OrderBy(x => x.Price).FirstOrDefault();

                decimal bestBid = bestBidOrder != null ? bestBidOrder.Price : 0;
                decimal bestAsk = bestAskOrder != null ? bestAskOrder.Price : 0;

                if (bestBid == 0 || bestAsk == 0) return;

                int bar = CurrentBar - 1;

                _bidLine[bar] = bestBid;
                _askLine[bar] = bestAsk;

                // 动态变色 (GDI)
                if (bestBid < _currentVWAP)
                    _bidLine.Colors[bar] = GdiColor.Lime;
                else
                    _bidLine.Colors[bar] = GdiColor.Gray;

                if (bestAsk > _currentVWAP)
                    _askLine.Colors[bar] = GdiColor.Red;
                else
                    _askLine.Colors[bar] = GdiColor.Gray;
            }
            catch { }
        }
    }
}