/*
 * 共享 K线渲染（选股池 / 牛股发掘 / 因子选股 复用，消除三处重复）。
 * 依赖：echarts（各页已引入 /static/echarts.min.js）。
 * 用法：AKline.render(elId, profile.kline) → 渲染 蜡烛图 + 量柱 + MA5/10/20/60（图例圆点与线同色）。
 *      AKline.sma(candle, m) → 由收盘价(candle[i][1])算 m 日均线（profile 无 MA10 时前端补）。
 */
window.AKline = (function () {
  function sma(candle, m) {
    const out = [];
    for (let i = 0; i < candle.length; i++) {
      if (i < m - 1) { out.push(null); continue; }
      let s = 0;
      for (let j = i - m + 1; j <= i; j++) s += candle[j][1];
      out.push(+(s / m).toFixed(2));
    }
    return out;
  }

  function render(elId, kl) {
    const el = document.getElementById(elId);
    if (!el || typeof echarts === 'undefined' || !kl) return null;
    // 关键：换股/快照还原时上层会 innerHTML='' 清画布但不释放 ECharts 实例，
    // 旧实例仍登记在该 DOM 上，echarts.init 会复用这个坏实例 → 画到空白。先释放，确保拿到全新实例。
    const prev = echarts.getInstanceByDom && echarts.getInstanceByDom(el);
    if (prev) prev.dispose();
    const c = echarts.init(el);
    const ma10 = sma(kl.candle, 10);
    c.setOption({
      backgroundColor: 'transparent', tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: ['MA5', 'MA10', 'MA20', 'MA60'], textStyle: { color: '#9ca3af' }, right: 10, top: 0 },
      grid: [{ left: 50, right: 20, top: 30, height: '60%' }, { left: 50, right: 20, top: '76%', height: '15%' }],
      xAxis: [{ type: 'category', data: kl.dates, axisLabel: { color: '#9ca3af' } },
              { type: 'category', data: kl.dates, gridIndex: 1, axisLabel: { show: false } }],
      yAxis: [{ scale: true, axisLabel: { color: '#9ca3af' }, splitLine: { lineStyle: { color: '#1a1d2e' } } },
              { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }],
      series: [
        { type: 'candlestick', data: kl.candle, itemStyle: { color: '#f6465d', color0: '#2ebd85', borderColor: '#f6465d', borderColor0: '#2ebd85' } },
        { name: 'MA5', type: 'line', data: kl.ma5, smooth: true, symbol: 'none', itemStyle: { color: '#e5c07b' }, lineStyle: { width: 1, color: '#e5c07b' } },
        { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', itemStyle: { color: '#4ade80' }, lineStyle: { width: 1, color: '#4ade80' } },
        { name: 'MA20', type: 'line', data: kl.ma20, smooth: true, symbol: 'none', itemStyle: { color: '#7c9ef8' }, lineStyle: { width: 1, color: '#7c9ef8' } },
        { name: 'MA60', type: 'line', data: kl.ma60, smooth: true, symbol: 'none', itemStyle: { color: '#c084fc' }, lineStyle: { width: 1, color: '#c084fc' } },
        { type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: kl.vol, itemStyle: { color: '#3a3f5c' } }
      ]
    });
    requestAnimationFrame(() => c.resize());
    setTimeout(() => c.resize(), 120);
    return c;
  }

  return { render: render, sma: sma };
})();
