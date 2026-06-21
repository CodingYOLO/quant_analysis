"""
M6：主题 LLM 解读层（盘后批量生成，落库缓存，前端只读）。

为热度达阈值的主题生成接地式解读：理由 / 新闻证据 / 分层与策略结论
（🟢介入条件 + 🔴证伪条件 + 因子翻人话）。另生成每日市场环境解读一条。

准确性优先（复用 detail_common 的博查联网 + 反编造接地）：
  - LLM 只「解释」已算好的因子事实，严禁改写数字、编造公司/事件；
  - 新闻只用财联社精筛 + 博查权威源（附原文链接核对）；
  - 不输出胜率/成功率；低置信显式说明；强制 JSON。
"""

from __future__ import annotations

import json
import logging
import re

from app.data.composite_provider import CompositeProvider
from app.data.theme_heat_db import (
    get_themes,
    latest_trade_date,
    upsert_market_env,
    upsert_theme_llm,
)
from app.llm.client import LLMClient
from app.strategy import detail_common as DC

logger = logging.getLogger(__name__)

_DEFAULT_TOP_N = 15        # 仅为热度前 N 的主题生成（控成本）
_HEAT_FLOOR = 40.0         # 热度下限，低于此不生成


def generate_for_date(trade_date: str = "", theme_type: str = "industry",
                      top_n: int = _DEFAULT_TOP_N) -> dict:
    """
    为某交易日的热门主题批量生成 LLM 解读 + 市场环境，落库。

    Returns: {date, generated:int, env:bool}
    """
    d = (trade_date or "").replace("-", "") or (latest_trade_date(theme_type) or "")
    if not d:
        return {"date": "", "generated": 0, "env": False, "msg": "宽表未计算"}

    rows = get_themes(d, theme_type)
    if not rows:
        return {"date": d, "generated": 0, "env": False, "msg": "无宽表数据"}

    provider = CompositeProvider()
    headlines = DC.macro_headlines(provider, d)

    # 市场环境（一日一条）
    env_ok = _gen_market_env(d, rows, headlines)

    # 热门主题逐个生成
    hot = [r for r in rows if (r.get("heat_score") or 0) >= _HEAT_FLOOR][:top_n]
    n = 0
    for r in hot:
        try:
            if _gen_theme(d, theme_type, r, headlines):
                n += 1
            else:
                logger.warning("[主题LLM] %s JSON 解析失败，跳过", r.get("theme_name"))
        except Exception as e:
            logger.warning("[主题LLM] %s 生成失败: %s", r.get("theme_name"), e)
    logger.info("[主题LLM] %s %s 生成 %d 条 + 市场环境=%s", d, theme_type, n, env_ok)
    return {"date": d, "generated": n, "env": env_ok}


# ──────────────────────────────────────────────
# 单主题解读
# ──────────────────────────────────────────────

def _gen_theme(date: str, theme_type: str, r: dict, headlines: list[str]) -> bool:
    """生成并落库单主题解读。返回是否成功落库（JSON 解析失败返回 False）。"""
    name = r["theme_name"]
    subject = f"{name}{'行业' if theme_type == 'industry' else '概念'}"
    rel_news = DC.relevant_news(headlines, {name})
    web = DC.web_search(subject, "")

    facts = _facts_text(r)
    news_text = "\n".join(f"- {h}" for h in rel_news) or "（财联社电报无本主题直接相关条目）"
    web_text = "\n".join(
        f"- [{w.get('date','')} {w.get('site','')}] {w.get('title','')}：{(w.get('summary') or w.get('snippet') or '')[:120]}"
        for w in web
    ) or "（未启用联网检索或无结果）"

    prompt = (
        f"你是严谨的A股主题研究员。下面是【{date} {subject}】已算好的客观因子事实与真实新闻。\n"
        f"**要给鲜明判断**：这主题强不强、是机会还是退潮风险、分层 tier 敢选 buy/watch/avoid 并说依据，别只复述数据、别和稀泥。"
        f"但严禁改写数字、编造未出现的公司/事件/政策；不编造胜率%、不打包票「必涨」。\n"
        f"请输出严格 JSON（不要代码块标记），字段：\n"
        f'{{"reason":"1-2句上涨/驱动逻辑(结合新闻,注明来源如[财联社]/[博查·证券时报])",'
        f'"news_evidence":["真实新闻标题(仅取下方出现的)"],'
        f'"tier":"watch|buy|avoid(参考量化分层,可微调并在reason体现)",'
        f'"score":0-100整数,'
        f'"enter_conditions":["🟢可观测的介入条件(如:次日资金维持净流入且不破MA5)"],'
        f'"falsify_conditions":["🔴可证伪的退出条件(如:龙头走弱或资金转净流出则逻辑破坏)"],'
        f'"factor_explain":["把关键因子翻成人话(如:次日风险70→追高隔夜回撤风险高)"]}}\n\n'
        f"【因子事实】\n{facts}\n"
        f"【相关新闻(财联社精筛)】\n{news_text}\n"
        f"【联网检索(博查,真实网页)】\n{web_text}\n"
    )
    prompt += "\n严格只输出一个 JSON 对象，不要任何额外文字或代码块标记。"
    llm = LLMClient()
    obj = None
    for _ in range(2):   # 解析失败重试一次
        raw = llm.chat([{"role": "user", "content": prompt}], task_type="pro",
                       temperature=0.3, max_tokens=1600)
        obj = _parse_json(raw)
        if obj:
            break
    if not obj:
        return False

    upsert_theme_llm({
        "theme_name": name, "trade_date": date, "theme_type": theme_type,
        "reason": str(obj.get("reason", "")),
        "news_evidence": json.dumps(obj.get("news_evidence", []), ensure_ascii=False),
        "enter_conditions": json.dumps(obj.get("enter_conditions", []), ensure_ascii=False),
        "falsify_conditions": json.dumps(obj.get("falsify_conditions", []), ensure_ascii=False),
        "factor_explain": json.dumps(obj.get("factor_explain", []), ensure_ascii=False),
        "tier_llm": str(obj.get("tier", "")),
        "score_llm": _safe_num(obj.get("score")),
        "web_sources": json.dumps([{"title": w.get("title"), "url": w.get("url"),
                                    "site": w.get("site"), "date": w.get("date")} for w in web],
                                   ensure_ascii=False),
    })
    return True


def _facts_text(r: dict) -> str:
    def g(k):
        v = r.get(k)
        return "数据缺失" if v is None else v
    return (
        f"热度{g('heat_score')}(3日Δ{g('heat_score_delta_3d')}) 阶段{g('phase')} 量化分层{g('tier')} 次日风险{g('nextday_risk_penalty')}\n"
        f"资金(亿): 1日{g('money_flow_1d')} 3日{g('money_flow_3d')} 5日{g('money_flow_5d')} 7日{g('money_flow_7d')}\n"
        f"涨跌(%): 1日{g('pct_chg_1d')} 3日{g('pct_chg_3d')} 5日{g('pct_chg_5d')} 7日{g('pct_chg_7d')}\n"
        f"均线广度(%): MA20 {g('breadth_ma20')} / MA60 {g('breadth_ma60')} / MA144 {g('breadth_ma144')}\n"
        f"拥挤度: Top100 {g('top100_ratio')}% Top300 {g('top300_ratio')}% 人气集中HHI {g('pop_concentration_hhi')}\n"
        f"样本{g('sample_count')}只 可靠性{g('sample_reliability')}%"
    )


# ──────────────────────────────────────────────
# 市场环境
# ──────────────────────────────────────────────

def _gen_market_env(date: str, rows: list[dict], headlines: list[str]) -> bool:
    up = [r for r in rows if (r.get("pct_chg_1d") or 0) > 0]
    total_money = round(sum((r.get("money_flow_1d") or 0) for r in rows), 1)
    avg_breadth = round(sum((r.get("breadth_ma20") or 0) for r in rows) / max(len(rows), 1), 1)
    top = sorted(rows, key=lambda r: (r.get("heat_score") or 0), reverse=True)[:6]
    top_str = "、".join(f"{r['theme_name']}({int(r.get('heat_score') or 0)})" for r in top)
    news_text = "\n".join(f"- {h}" for h in headlines[:15]) or "（无）"

    prompt = (
        f"你是A股策略分析师。基于 {date} 全市场板块客观数据，判断市场环境。"
        f"只依据数据与新闻、不编造数字事件、不编胜率%；但 phase/trend/summary 要给**鲜明判断**"
        f"（现在该进攻还是防守、什么风格占优、风险在哪），别和稀泥。输出严格 JSON："
        f'{{"phase":"主升初期|加速|震荡|退潮|衰退","trend":"up|flat|down","confidence":0-1小数,'
        f'"summary":"80-140字市场解读(资金/广度/赚钱效应/风格,结合新闻)"}}\n\n'
        f"【数据】板块{len(rows)}个,上涨{len(up)}个,当日资金合计{total_money}亿,平均MA20广度{avg_breadth}%,"
        f"热度居前: {top_str}\n【新闻头条】\n{news_text}\n"
    )
    raw = LLMClient().chat([{"role": "user", "content": prompt}], task_type="pro",
                           temperature=0.3, max_tokens=1300)
    obj = _parse_json(raw)
    if not obj:
        return False
    upsert_market_env(date, str(obj.get("phase", "")), str(obj.get("trend", "")),
                      _safe_num(obj.get("confidence")) or 0.0, str(obj.get("summary", "")))
    return True


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────

def _parse_json(raw: str) -> dict | None:
    """从 LLM 输出中稳健提取 JSON 对象。"""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _safe_num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
