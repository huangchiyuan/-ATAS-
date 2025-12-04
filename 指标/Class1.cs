using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Windows.Media; // WPF Colors (用于 DataSeries)

// 1. 解决 System.Drawing 引用
// 务必确保项目已安装 NuGet 包: System.Drawing.Common
using DrawingColor = System.Drawing.Color;
using DrawingRectangle = System.Drawing.Rectangle;
using DrawingPoint = System.Drawing.Point;

using ATAS.Indicators;
using ATAS.Indicators.Technical;

// 2. 关键引用: ATAS 绘图核心库
// 务必在 Visual Studio 项目引用中添加: OFT.Rendering.dll (位于 ATAS 安装目录)
using OFT.Rendering.Context;
using OFT.Rendering.Settings;
using OFT.Rendering.Tools;

namespace ATAS.Indicators.Technical
{
    [DisplayName("QSF Visual - Smart Flow & Structure")]
    [Description("QSF引擎可视化：VWAP带宽 + 动态VP + 闪烁警报")]
    [Category("Norden Flow")]
    public class QSF_Visual : Indicator
    {
        #region 参数设置
        [Display(Name = "VP 侧边栏宽度 (像素)", GroupName = "可视化设置", Order = 10)]
        public int HistogramWidth { get; set; } = 150;

        [Display(Name = "VP 透明度 (0-255)", GroupName = "可视化设置", Order = 20)]
        public int HistogramOpacity { get; set; } = 80;

        [Display(Name = "是否开启声音警报", GroupName = "警报设置", Order = 30)]
        public bool EnableSoundAlert { get; set; } = true;

        [Display(Name = "警报声音文件", GroupName = "警报设置", Order = 40)]
        public string AlertFile { get; set; } = "alert.wav";
        #endregion

        #region 数据序列 (DataSeries)
        private readonly ValueDataSeries _vwapLine = new("Smart VWAP")
        {
            Color = Colors.Gold,
            Width = 3,
            VisualType = VisualMode.Line
        };

        private readonly ValueDataSeries _top1 = new("SD +1")
        {
            Color = Colors.Goldenrod,
            LineDashStyle = LineDashStyle.Dash,
            Width = 1
        };
        private readonly ValueDataSeries _bot1 = new("SD -1")
        {
            Color = Colors.Goldenrod,
            LineDashStyle = LineDashStyle.Dash,
            Width = 1
        };
        private readonly ValueDataSeries _top2 = new("SD +2")
        {
            Color = Colors.DarkGoldenrod,
            LineDashStyle = LineDashStyle.Dot,
            Width = 1
        };
        private readonly ValueDataSeries _bot2 = new("SD -2")
        {
            Color = Colors.DarkGoldenrod,
            LineDashStyle = LineDashStyle.Dot,
            Width = 1
        };

        private readonly ValueDataSeries _signalDot = new("迁移信号点")
        {
            Color = Colors.Red,
            VisualType = VisualMode.Square,
            Width = 10
        };
        #endregion

        #region 内部对象
        private readonly QSF_Engine _engine = new QSF_Engine();
        private readonly object _renderLock = new object();
        private Dictionary<decimal, decimal> _renderVP = new Dictionary<decimal, decimal>();
        private decimal _cachedVAH, _cachedVAL, _cachedPOC;
        private bool _isAlerting;
        #endregion

        protected override void OnInitialize()
        {
            DataSeries.Add(_vwapLine);
            DataSeries.Add(_top1);
            DataSeries.Add(_bot1);
            DataSeries.Add(_top2);
            DataSeries.Add(_bot2);
            DataSeries.Add(_signalDot);

            // 开启自定义绘图层，且不允许用户更改面板(固定在主图)
            DenyToChangePanel = true;
        }

        protected override void OnCalculate(int bar, decimal value) { }

        protected override void OnNewTrade(MarketDataArg t)
        {
            _engine.OnNewTrade(t.Price, t.Volume, t.Time);

            int barIndex = CurrentBar - 1;
            if (barIndex < 0) return;

            decimal vwap = _engine.SmartVWAP;
            decimal sd = _engine.StdDev;

            _vwapLine[barIndex] = vwap;
            _top1[barIndex] = vwap + sd;
            _bot1[barIndex] = vwap - sd;
            _top2[barIndex] = vwap + 2 * sd;
            _bot2[barIndex] = vwap - 2 * sd;

            if (_engine.IsMigrationSignal)
            {
                if (ChartInfo != null)
                {
                    _signalDot[barIndex] = t.Price;
                }

                if (EnableSoundAlert)
                {
                    AddAlert(AlertFile, "QSF Alert", _engine.SignalMessage, Colors.Red, Colors.White);
                }
            }

            lock (_renderLock)
            {
                _renderVP = _engine.GetVPSnapshot();
                _cachedVAH = _engine.VAH;
                _cachedVAL = _engine.VAL;
                _cachedPOC = _engine.POC;
                _isAlerting = _engine.IsMigrationSignal;
            }
        }

        // =======================================================================
        // 关键修复区域：OnRender 重写
        // =======================================================================
        protected override void OnRender(RenderContext context, DrawingLayouts layout)
        {
            // 获取绘图区域
            // containerRegion.X 通常为 0，Width 为图表宽度
            var containerRegion = this.Container.Region;
            var chartInfo = this.ChartInfo;

            // 颜色定义 (GDI+)
            var colorVA = DrawingColor.FromArgb(HistogramOpacity, 0, 200, 0);       // 绿色
            var colorOut = DrawingColor.FromArgb(HistogramOpacity, 128, 128, 128);  // 灰色
            var colorPOC = DrawingColor.FromArgb(HistogramOpacity + 50, 255, 255, 255); // 白色

            bool blinkState = (DateTime.Now.Millisecond / 500) % 2 == 0;
            var alertColor = blinkState ? DrawingColor.Red : DrawingColor.Yellow;

            lock (_renderLock)
            {
                if (_renderVP == null || _renderVP.Count == 0) return;

                // --- 1. 计算缩放比例 (修复满屏色块 Bug) ---
                decimal maxVolInView = 1;
                decimal globalMaxVol = 1; // 全局最大量，作为保底

                // 获取屏幕显示的 Y 轴范围
                int minY = 0;
                int maxY = containerRegion.Height;

                foreach (var kvp in _renderVP)
                {
                    // 记录全局最大值
                    if (kvp.Value > globalMaxVol) globalMaxVol = kvp.Value;

                    // 获取 Y 坐标
                    int y = chartInfo.GetYByPrice(kvp.Key);

                    // 只统计屏幕内的最大量，以便正确缩放
                    if (y >= minY && y <= maxY)
                    {
                        if (kvp.Value > maxVolInView) maxVolInView = kvp.Value;
                    }
                }

                // 安全校验：如果屏幕内没找到任何量(可能计算误差)，但全局有量，使用全局量
                // 这能防止 maxVolInView 保持为 1 导致的宽度爆炸
                if (maxVolInView == 1 && globalMaxVol > 1)
                    maxVolInView = globalMaxVol;

                // --- 2. 绘制循环 ---
                int barHeight = (int)Math.Max(1, chartInfo.GetYByPrice(0) - chartInfo.GetYByPrice(InstrumentInfo.TickSize));

                foreach (var kvp in _renderVP)
                {
                    decimal price = kvp.Key;
                    decimal vol = kvp.Value;
                    int y = chartInfo.GetYByPrice(price);

                    // 剔除屏幕外的柱子
                    if (y < -barHeight || y > containerRegion.Height + barHeight) continue;

                    int drawY = y - barHeight / 2;

                    // 计算宽度：(当前量 / 最大量) * 设定宽度
                    // 使用 long 防止乘法溢出，再转 int
                    long widthCalc = (long)(vol / maxVolInView * HistogramWidth);

                    // 宽度限制：不能超过容器宽度
                    int width = (int)Math.Min(widthCalc, containerRegion.Width);
                    if (width <= 0) continue;

                    // 颜色选择
                    DrawingColor brushColor;
                    if (_isAlerting && price == _cachedPOC) brushColor = alertColor;
                    else if (price == _cachedPOC) brushColor = colorPOC;
                    else if (price <= _cachedVAH && price >= _cachedVAL) brushColor = colorVA;
                    else brushColor = colorOut;

                    // --- 3. 关键修复：右对齐 (Right Alignment) ---
                    // X = 容器右边界 - 柱子宽度
                    // containerRegion.Right 可能不可用，使用 X + Width
                    int x = containerRegion.X + containerRegion.Width - width;

                    // 绘制矩形
                    context.FillRectangle(brushColor, new DrawingRectangle(x, drawY, width, barHeight));
                }
            }
        }
    }

    // ==========================================
    // QSF_Engine (保持原样，无需修改)
    // ==========================================
    public class QSF_Engine
    {
        private const int VWAP_BUFFER_SIZE = 5000;
        private const int MAX_VOL_HORIZON = 15000;
        private const int MIN_VOL_HORIZON = 2000;
        private const double Z_SCORE_THRESHOLD = 3.0;
        private const int STATS_WINDOW_SIZE = 100;
        private const int VP_RECALC_MS = 500;

        public struct TradeTick { public decimal Price; public decimal Volume; }

        public decimal SmartVWAP { get; private set; }
        public decimal StdDev { get; private set; }
        public decimal POC { get; private set; }
        public decimal VAH { get; private set; }
        public decimal VAL { get; private set; }
        public bool IsMigrationSignal { get; private set; }
        public string SignalMessage { get; private set; }

        private Dictionary<decimal, decimal> _vpData = new Dictionary<decimal, decimal>();
        private DateTime _lastVPUpdateTime = DateTime.MinValue;
        private TradeTick[] _vwapRingBuffer = new TradeTick[VWAP_BUFFER_SIZE];
        private int _vwapHead = 0, _vwapTail = 0, _vwapCount = 0;
        private decimal _vwapSumPV = 0, _vwapSumVol = 0, _vwapSumP2V = 0;
        private decimal[] _volStatsBuffer = new decimal[STATS_WINDOW_SIZE];
        private int _statsIndex = 0, _statsCount = 0;
        private decimal _statsSum = 0, _statsSumSq = 0;
        private LinkedList<DateTime> _tpmWindow = new LinkedList<DateTime>();
        private bool _migrationLock = false;
        private DateTime _lockReleaseTime = DateTime.MinValue;

        public void OnNewTrade(decimal price, decimal volume, DateTime time)
        {
            IsMigrationSignal = false;
            UpdateTPM(time);
            int tpm = _tpmWindow.Count;
            decimal targetHorizon = CalculateLogHorizon(tpm);
            bool isSpike = UpdateStatsAndCheckSpike(volume);
            UpdateVP(price, volume, time);

            if (!_migrationLock && isSpike && VAH > 0)
            {
                bool aboveVAH = price > VAH;
                bool belowVAL = price < VAL;
                if (aboveVAH || belowVAL) TriggerMigration(aboveVAH ? "多头" : "空头", price);
            }

            if (_migrationLock && time > _lockReleaseTime) _migrationLock = false;
            if (_migrationLock) IsMigrationSignal = true;

            UpdateSmartVWAP(price, volume, targetHorizon);
        }

        private void UpdateTPM(DateTime time) { _tpmWindow.AddLast(time); while (_tpmWindow.Count > 0 && (time - _tpmWindow.First.Value).TotalSeconds > 60) _tpmWindow.RemoveFirst(); }
        private decimal CalculateLogHorizon(int tpm) { if (tpm <= 50) return MAX_VOL_HORIZON; double logTPM = Math.Log10(tpm); double ratio = (logTPM - 1.698) / (3.477 - 1.698); ratio = Math.Max(0, Math.Min(1, ratio)); return (decimal)((double)MAX_VOL_HORIZON - ratio * (MAX_VOL_HORIZON - MIN_VOL_HORIZON)); }
        private bool UpdateStatsAndCheckSpike(decimal vol) { if (_statsCount >= STATS_WINDOW_SIZE) { decimal old = _volStatsBuffer[_statsIndex]; _statsSum -= old; _statsSumSq -= old * old; } _volStatsBuffer[_statsIndex] = vol; _statsSum += vol; _statsSumSq += vol * vol; _statsIndex = (_statsIndex + 1) % STATS_WINDOW_SIZE; if (_statsCount < STATS_WINDOW_SIZE) _statsCount++; if (_statsCount < 30) return false; decimal mean = _statsSum / _statsCount; decimal variance = (_statsSumSq / _statsCount) - (mean * mean); if (variance <= 0) return false; return ((double)(vol - mean) / Math.Sqrt((double)variance)) > Z_SCORE_THRESHOLD; }
        private void UpdateVP(decimal p, decimal v, DateTime time) { if (!_vpData.ContainsKey(p)) _vpData[p] = 0; _vpData[p] += v; if ((time - _lastVPUpdateTime).TotalMilliseconds < VP_RECALC_MS) return; _lastVPUpdateTime = time; if (_vpData.Count == 0) return; var sorted = _vpData.OrderByDescending(x => x.Value).ToList(); POC = sorted[0].Key; decimal target = _vpData.Values.Sum() * 0.68m; decimal current = 0, hi = POC, lo = POC; foreach (var kvp in sorted) { current += kvp.Value; if (kvp.Key > hi) hi = kvp.Key; if (kvp.Key < lo) lo = kvp.Key; if (current >= target) break; } VAH = hi; VAL = lo; }
        private void UpdateSmartVWAP(decimal p, decimal v, decimal horizon) { _vwapRingBuffer[_vwapHead] = new TradeTick { Price = p, Volume = v }; _vwapSumPV += p * v; _vwapSumVol += v; _vwapSumP2V += p * p * v; _vwapHead = (_vwapHead + 1) % VWAP_BUFFER_SIZE; if (_vwapCount < VWAP_BUFFER_SIZE) _vwapCount++; else { if (_vwapHead == _vwapTail) { var old = _vwapRingBuffer[_vwapTail]; _vwapSumPV -= old.Price * old.Volume; _vwapSumVol -= old.Volume; _vwapSumP2V -= old.Price * old.Price * old.Volume; _vwapTail = (_vwapTail + 1) % VWAP_BUFFER_SIZE; _vwapCount--; } } while (_vwapSumVol > horizon && _vwapCount > 1) { var old = _vwapRingBuffer[_vwapTail]; _vwapSumPV -= old.Price * old.Volume; _vwapSumVol -= old.Volume; _vwapSumP2V -= old.Price * old.Price * old.Volume; _vwapTail = (_vwapTail + 1) % VWAP_BUFFER_SIZE; _vwapCount--; } if (_vwapSumVol > 0) { SmartVWAP = _vwapSumPV / _vwapSumVol; decimal var = (_vwapSumP2V / _vwapSumVol) - (SmartVWAP * SmartVWAP); StdDev = var > 0 ? (decimal)Math.Sqrt((double)var) : 0; } else { SmartVWAP = p; StdDev = 0; } }
        private void TriggerMigration(string dir, decimal p) { _migrationLock = true; _lockReleaseTime = DateTime.Now.AddMinutes(5); IsMigrationSignal = true; SignalMessage = $"{dir} 价值迁移 -> VWAP重置"; _vwapHead = 0; _vwapTail = 0; _vwapCount = 0; _vwapSumPV = 0; _vwapSumVol = 0; _vwapSumP2V = 0; decimal anchor = MIN_VOL_HORIZON; _vwapRingBuffer[0] = new TradeTick { Price = POC, Volume = anchor }; _vwapSumPV = POC * anchor; _vwapSumVol = anchor; _vwapSumP2V = POC * POC * anchor; _vwapHead = 1; _vwapCount = 1; SmartVWAP = POC; StdDev = 0; }
        public Dictionary<decimal, decimal> GetVPSnapshot() => new Dictionary<decimal, decimal>(_vpData);
    }
}