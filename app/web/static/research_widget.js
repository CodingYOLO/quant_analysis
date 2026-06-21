/*
 * 共享「个股研报」渲染（东财 + 同花顺一致预期），多页复用：牛股发掘 / 因子选股 / 选股池 / 研报中心。
 * 数据来自 /api/stock/research（{东财字段... , ths:{同花顺一致预期}}）。
 * 用法：AResearch.html(d) → 返回 HTML 串（东财块 + 同花顺块·任一有数据即显示·自带内联配色不依赖各页CSS）。
 */
window.AResearch = (function () {
  function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function col(v) { return v >= 0 ? '#f6465d' : '#2ebd85'; }   // 红涨绿跌

  function html(d) {
    if (!d) return '';
    const lines = [];
    // 东财：评级分布 + 买入占比 + 盈预增速 + 最新研报 PDF
    if (d.ok) {
      const rts = Object.entries(d.ratings || {}).map(([k, v]) => esc(k) + v).join(' ') || '—';
      const g = d.eps_growth, gS = (g == null) ? '' : ' ｜ 盈预增速 <span style="color:' + col(g) + '">' + (g >= 0 ? '+' : '') + g + '%</span>';
      const ev = (d.recent || [])[0];
      const pdf = (ev && ev.pdf) ? ' <a href="' + esc(ev.pdf) + '" target="_blank" rel="noopener" style="color:#7c9ef8;text-decoration:none">📄原文↗</a>' : '';
      let s = '<b style="color:#e5c07b">东财</b>·近半年 <b>' + (d.n_org || 0) + '</b>家/' + (d.n_reports || 0) + '篇 ｜ 评级 ' + rts + '(买入' + (d.buy_ratio || 0) + '%)' + gS + ' ｜ 近1月' + (d.last_month || 0) + '篇';
      if (ev) s += '<br><span style="color:#9ca3af;font-size:11px">最新：' + esc(ev.org) + ' ' + esc(ev.rating) + ' 《' + esc(ev.title) + '》' + esc(ev.date) + '</span>' + pdf;
      lines.push(s);
    }
    // 同花顺一致预期：覆盖机构数 + 分年 EPS 均值 + 隐含增速 + 行业平均
    const t = d.ths;
    if (t && t.ok) {
      const yrs = (t.by_year || []).map(y => esc(y.year) + ' ' + y.eps_avg + (y.n_org ? '<span style="color:#9ca3af;font-size:10px">(' + y.n_org + '家)</span>' : '')).join(' / ');
      const tg = t.eps_growth, tgS = (tg == null) ? '' : ' ｜ 增速 <span style="color:' + col(tg) + '">' + (tg >= 0 ? '+' : '') + tg + '%</span>';
      lines.push('<b style="color:#7c9ef8">同花顺</b>·一致预期 EPS ' + yrs + tgS + (t.ind_avg != null ? ' <span style="color:#9ca3af;font-size:10px">(行业均' + t.ind_avg + ')</span>' : ''));
    }
    if (!lines.length) return '<span style="color:#9ca3af">ℹ️ ' + esc(d.msg || '近期无券商研报覆盖') + '</span>';
    return '<div style="color:#cdd6f4;line-height:1.7">📑 <b style="color:#b794f6">券商研报</b><br>'
      + lines.join('<br>') + ' <span style="color:#9ca3af;font-size:10px">(研报观点≠事实)</span></div>';
  }

  return { html: html };
})();
