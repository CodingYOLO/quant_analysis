"""
A股Agent Web UI（FastAPI）。

路由：
  /                      报告中心（选股报告 + 三时段快讯列表）
  /report/{name}         查看任意报告（Markdown → HTML）
  /generate              一键生成页（盘前/盘中/盘后）
  /api/generate/{sess}   触发生成快讯，返回结果
  /strategy /tracking    策略验证 / 持仓追踪（原有）

安全：HTTP Basic 认证，账号密码取自 .env 的 WEB_USERNAME / WEB_PASSWORD。
启动：.venv/bin/python -m app.run web
"""

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import get_settings

logger = logging.getLogger(__name__)

app = FastAPI(title="A股Agent", docs_url=None, redoc_url=None)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 静态资源（本地托管 ECharts 等，免 CDN 依赖）
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_security = HTTPBasic()

# 三时段中文名
_SESSION_LABEL = {"pre": "盘前", "mid": "盘中", "post": "盘后"}


# ──────────────────────────────────────────────
# 认证
# ──────────────────────────────────────────────

def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    """HTTP Basic 认证。WEB_PASSWORD 为空时跳过鉴权（仅限内网调试）。"""
    settings = get_settings()
    if not settings.web_password:
        return "anonymous"
    ok_user = secrets.compare_digest(credentials.username, settings.web_username)
    ok_pwd = secrets.compare_digest(credentials.password, settings.web_password)
    if not (ok_user and ok_pwd):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────

# 报告分类（用于报告中心分组展示），顺序即展示顺序
_CATEGORIES = [
    ("select", "📈 完整选股报告"),
    ("pre", "🌅 盘前快讯"),
    ("mid", "☀️ 盘中快讯"),
    ("post", "🌙 盘后复盘"),
]


def _parse_report_meta(path: Path) -> dict:
    """
    解析报告文件，返回展示元信息 + 分类。
    选股报告：YYYYMMDD          → category=select
    快讯：    YYYYMMDD_HHMM_pre  → category=pre/mid/post
    时间统一用文件实际生成时间(mtime)的 HH:MM，准确反映出具时间。
    """
    import datetime
    stem = path.stem
    parts = stem.split("_")
    date = parts[0]
    display_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else date
    # 生成时间：用文件 mtime（实际出具时间），统一 HH:MM
    try:
        gen_time = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M")
    except Exception:
        gen_time = ""
    if len(parts) >= 3:  # 快讯
        session = parts[2]
        label = _SESSION_LABEL.get(session, session)
        return {"name": stem, "category": session, "kind": f"{label}快讯",
                "date": display_date, "time": gen_time}
    return {"name": stem, "category": "select", "kind": "完整选股报告",
            "date": display_date, "time": gen_time}


def _render_markdown(md_text: str) -> str:
    """复用通知模块的 Markdown→HTML（含表格/列表归一化，渲染稳定）。"""
    from app.notify.notifier import _md_to_html
    return _md_to_html(md_text)


# ──────────────────────────────────────────────
# 页面
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _user: str = Depends(require_auth)):
    """报告中心：按类型分组展示（选股/盘前/盘中/盘后），各组内按时间倒序。"""
    settings = get_settings()
    files = sorted(
        Path(settings.report_dir).glob("*.md"),
        key=lambda f: f.stem, reverse=True,
    )
    metas = [_parse_report_meta(f) for f in files]
    # 按分类分组
    groups = []
    for cat, label in _CATEGORIES:
        items = [m for m in metas if m["category"] == cat]
        if items:
            groups.append({"label": label, "reports": items})
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"groups": groups, "total": len(metas)},
    )


@app.get("/report/{name}", response_class=HTMLResponse)
async def view_report(request: Request, name: str, _user: str = Depends(require_auth)):
    """查看任意报告（兼容选股报告与快讯两种命名）。"""
    settings = get_settings()
    # 防目录穿越
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法报告名")
    md_path = Path(settings.report_dir) / f"{name}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"报告 {name} 不存在")
    html_body = _render_markdown(md_path.read_text(encoding="utf-8"))
    return templates.TemplateResponse(
        request=request, name="report.html",
        context={"date": name, "html_body": html_body},
    )


@app.get("/generate", response_class=HTMLResponse)
async def generate_page(request: Request, _user: str = Depends(require_auth)):
    """一键生成页。"""
    return templates.TemplateResponse(request=request, name="generate.html", context={})


def _push_report(notify: bool, title: str, content: str) -> bool:
    """
    将网页手动生成的报告推送到邮箱/微信（与定时任务一致）。

    Args:
        notify:  是否推送（前端勾选框控制，默认开）
        title:   推送标题（Server酱标题上限约32字，统一截断）
        content: 报告 Markdown 全文

    Returns:
        是否推送成功；notify=False 或失败均返回 False（不影响生成结果）。
    """
    if not notify:
        return False
    try:
        from app.notify.notifier import get_notifier
        return bool(get_notifier().send(title[:32], content))
    except Exception as e:
        logger.warning("网页生成推送失败: %s", e)
        return False


@app.post("/api/generate_selection")
async def api_generate_selection(notify: bool = True, _user: str = Depends(require_auth)):
    """按需运行完整选股流水线（吴川三层+量化+风控），返回报告 HTML，默认推送邮箱/微信。"""
    try:
        from app.graph import build_graph
        from app.state import PipelineState
        from app.run import _resolve_date
        settings = get_settings()
        trade_date = _resolve_date("")
        initial = PipelineState(trade_date=trade_date)
        final_dict = build_graph().invoke(initial.model_dump())
        final = PipelineState(**final_dict)
        md_path = Path(settings.report_dir) / f"{trade_date}.md"
        content = md_path.read_text(encoding="utf-8") if md_path.exists() else (final.report_md or "")
        if not content:
            return {"ok": False, "error": "选股流水线未产出报告（可能当日数据未就绪或非交易日）"}
        # 推送标题与 CLI 定时任务保持一致
        n_cand = len(final.candidates)
        regime_label = getattr(final.market_regime, "label", "") or ""
        title = f"【盘后选股】{trade_date[4:6]}/{trade_date[6:]} {regime_label} | 候选{n_cand}只"
        pushed = _push_report(notify, title, content)
        return {"ok": True, "title": f"完整选股报告 {trade_date}", "name": trade_date,
                "html": _render_markdown(content), "pushed": pushed}
    except Exception as e:
        logger.exception("选股流水线失败")
        return {"ok": False, "error": str(e)}


@app.post("/api/generate/{session}")
async def api_generate(session: str, notify: bool = True, _user: str = Depends(require_auth)):
    """
    按需生成三时段快讯之一（pre/mid/post），返回 HTML 供网页预览，默认推送邮箱/微信。
    """
    if session not in ("pre", "mid", "post"):
        return {"ok": False, "error": "session 必须是 pre/mid/post"}
    try:
        from app.nodes.quick_report import build_quick_report
        filepath, title, content = build_quick_report(session)
        pushed = _push_report(notify, title, content)
        return {
            "ok": True,
            "title": title,
            "name": Path(filepath).stem,
            "html": _render_markdown(content),
            "pushed": pushed,
        }
    except Exception as e:
        logger.exception("按需生成失败")
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────
# 原有页面/API（加认证）
# ──────────────────────────────────────────────

def _last_trade_date() -> str:
    """
    选股页默认日期：取数据可靠可用的最近交易日。
    - 当日 18:00 后（资金已入库）→ 用当日（若为交易日）
    - 否则 → 用上一交易日（保证全量数据已入库，不会报错）
    失败回退今天。
    """
    import datetime
    now = datetime.datetime.now()
    today = now.strftime("%Y%m%d")
    try:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()
        start = (now - datetime.timedelta(days=20)).strftime("%Y%m%d")
        cal = provider.get_trade_cal(start, today)
        days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
        if not days:
            return today
        # 最近交易日是今天且已过18点 → 用今天；否则用上一交易日
        if days[-1] == today and now.hour >= 18:
            return today
        return days[-2] if days[-1] == today and len(days) >= 2 else days[-1]
    except Exception:
        return today


def _next_trade_date(td: str) -> str:
    """
    给定交易日 td（YYYYMMDD），返回其之后最近的一个交易日（用于「推荐买入日」提示）。

    用交易日历向后查 15 天足够覆盖节假日连休；查不到时回退空串。
    """
    import datetime
    try:
        from app.data.composite_provider import CompositeProvider
        base = datetime.datetime.strptime(td, "%Y%m%d")
        start = (base + datetime.timedelta(days=1)).strftime("%Y%m%d")
        end = (base + datetime.timedelta(days=15)).strftime("%Y%m%d")
        cal = CompositeProvider().get_trade_cal(start, end)
        days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
        return days[0] if days else ""
    except Exception:
        return ""


@app.get("/sentiment", response_class=HTMLResponse)
async def sentiment_page(request: Request, _user: str = Depends(require_auth)):
    """大盘情绪仪表盘页面。"""
    return templates.TemplateResponse(request=request, name="sentiment.html", context={})


@app.get("/api/sentiment")
async def api_sentiment(days: int = 22, start: str = "", end: str = "",
                        _user: str = Depends(require_auth)):
    """大盘情绪仪表盘数据。支持自定义区间 start/end（YYYYMMDD）。"""
    try:
        from app.strategy.market_sentiment import build_dashboard
        end_date = end or _last_trade_date()
        data = build_dashboard(end_date, days=days, start_date=start)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.exception("大盘情绪数据失败")
        return {"ok": False, "error": str(e)}


@app.get("/industry", response_class=HTMLResponse)
async def industry_page(request: Request, _user: str = Depends(require_auth)):
    """行业资金流仪表盘页面。"""
    return templates.TemplateResponse(request=request, name="industry.html", context={})


@app.get("/api/industry")
async def api_industry(date: str = "", _user: str = Depends(require_auth)):
    """行业资金流仪表盘数据。"""
    try:
        from app.strategy.industry_flow import build_industry_dashboard
        d = date or _last_trade_date()
        return {"ok": True, "data": build_industry_dashboard(d)}
    except Exception as e:
        logger.exception("行业数据失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/industry/detail")
async def api_industry_detail(date: str = "", industry: str = "", _user: str = Depends(require_auth)):
    """单个行业的环境/宏观/微观详情（资金定性+公告+题材+LLM驱动点评，按需+缓存）。"""
    if not industry:
        return {"ok": False, "error": "缺少 industry 参数"}
    try:
        from app.strategy.industry_detail import build_industry_detail
        d = date or _last_trade_date()
        return {"ok": True, "data": build_industry_detail(d, industry)}
    except Exception as e:
        logger.exception("行业详情失败")
        return {"ok": False, "error": str(e)}


@app.get("/llm", response_class=HTMLResponse)
async def llm_page(request: Request, _user: str = Depends(require_auth)):
    """LLM 分析模块（Tab1 主题热点等）。"""
    return templates.TemplateResponse(request=request, name="llm_theme.html", context={"page": "llm"})


@app.get("/api/theme/list")
async def api_theme_list(date: str = "", type: str = "industry", _user: str = Depends(require_auth)):
    """主题列表（读宽表 theme_heat_all_in_one）。date 为空取宽表最近已计算日。"""
    try:
        from app.data.theme_heat_db import get_themes, latest_trade_date
        from app.data.theme_heat_db import get_market_env
        d = (date or "").replace("-", "") or (latest_trade_date(type) or "")
        if not d:
            return {"ok": True, "available": False, "date": "", "rows": [],
                    "msg": "宽表尚未计算任何交易日，请先运行 python -m app.run wide"}
        rows = get_themes(d, type)
        if not rows:
            return {"ok": True, "available": False, "date": d, "rows": [],
                    "msg": f"{d} 宽表未计算（数据缺失，不展示旧/假数据）。可运行 python -m app.run wide --date {d}"}
        return {"ok": True, "available": True, "date": d, "rows": rows, "env": get_market_env(d)}
    except Exception as e:
        logger.exception("主题列表失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/theme/detail")
async def api_theme_detail(date: str = "", name: str = "", type: str = "industry",
                           _user: str = Depends(require_auth)):
    """单个主题宽表全字段。"""
    if not name:
        return {"ok": False, "error": "缺少 name 参数"}
    try:
        from app.data.theme_heat_db import get_theme, get_theme_llm
        import json as _json
        d = (date or "").replace("-", "")
        row = get_theme(d, name, type)
        if not row:
            return {"ok": False, "error": f"{d} 无主题「{name}」宽表数据"}
        # 合并 LLM 解读（盘后落库；未生成则前端显示"待生成"）
        llm = get_theme_llm(d, name, type)
        if llm:
            for k in ("news_evidence", "enter_conditions", "falsify_conditions", "factor_explain", "web_sources"):
                try:
                    llm[k] = _json.loads(llm[k]) if llm.get(k) else []
                except Exception:
                    llm[k] = []
        return {"ok": True, "data": row, "llm": llm}
    except Exception as e:
        logger.exception("主题详情失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/sector/radar")
async def api_sector_radar(date: str = "", type: str = "concept",
                           _user: str = Depends(require_auth)):
    """板块雷达：下拉列表 + 三栏诊断（低吸/轮动/高位风险，读宽表已过滤）。"""
    try:
        from app.strategy.sector_radar import build_sector_radar
        return build_sector_radar(date, theme_type=type)
    except Exception as e:
        logger.exception("板块雷达失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/sector/breadth")
async def api_sector_breadth(name: str = "", type: str = "concept", days: int = 45,
                             _user: str = Depends(require_auth)):
    """单板块广度时序。优先读盘后预算缓存(秒开)；缺则实时算(线程池·首次稍慢)。"""
    if not name:
        return {"ok": False, "msg": "缺少 name 参数"}
    try:
        from app.factors.board_breadth import load_cached_breadth
        cached = load_cached_breadth(type, name, int(days))
        if cached:                                  # 命中预算缓存 → 秒回
            return cached
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.sector_radar import compute_board_breadth
        return await run_in_threadpool(compute_board_breadth, name, type, int(days))
    except Exception as e:
        logger.exception("板块广度时序失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/pool/eval")
async def api_pool_eval(_user: str = Depends(require_auth)):
    """评分回测：A历史(价格结构·读pool_eval) + B前向(真实池完整评分·按需聚合) + 总览。"""
    try:
        from app.backtest.pool_eval import aggregate, eval_pool_date
        from app.strategy.db import get_pool_with_perf, load_evals, pool_dates
        bt = load_evals("backtest")
        fwd = []
        for d in pool_dates():
            e = eval_pool_date(get_pool_with_perf(d))
            if e:
                e["run_date"] = d
                fwd.append(e)
        fwd.sort(key=lambda x: x["run_date"])
        return {
            "ok": True,
            "backtest": bt, "backtest_agg": aggregate(bt, "强", "弱"),
            "forward": fwd, "forward_agg": aggregate(fwd, "高分(≥75)", "其余(<75)"),
        }
    except Exception as e:
        logger.exception("评分回测失败")
        return {"ok": False, "error": str(e)}


@app.get("/stockpool", response_class=HTMLResponse)
async def stockpool_page(request: Request, _user: str = Depends(require_auth)):
    """Tab2 选股池（内置策略每日盘后自动选股）。"""
    return templates.TemplateResponse(request=request, name="stockpool.html", context={"page": "llm"})


@app.get("/api/stockpool")
async def api_stockpool(date: str = "", _user: str = Depends(require_auth)):
    """选股池数据（读 stock_pool + 前向追踪 T+1/3/5）。"""
    try:
        from app.strategy.db import get_pool_with_perf, pool_dates, pool_gen_time
        all_dates = pool_dates()
        newest_pool = all_dates[0] if all_dates else ""
        d = (date or "").replace("-", "")
        if not d:
            d = newest_pool
        if not d:
            return {"ok": True, "available": False, "rows": [],
                    "msg": "选股池尚未生成，请先运行 python -m app.run stock-pool"}
        rows = get_pool_with_perf(d)
        if not rows:
            return {"ok": True, "available": False, "date": d, "rows": [],
                    "msg": f"{d} 选股池未生成（数据缺失，不展示旧/假数据）"}
        focus = sum(1 for r in rows if r.get("is_focus"))
        # 日期语义：data_date=分析所用收盘数据日；next_date=推荐观察/买入日（下一交易日）
        latest = _last_trade_date()                       # 最新「应有数据」的交易日
        try:
            from app.strategy.stock_pool import infer_market_label
            market_label = infer_market_label(d)          # 大盘状态(轻量·缓存)，供前端横幅
        except Exception:
            market_label = ""
        return {
            "ok": True, "available": True, "date": d, "market_label": market_label,
            "total": len(rows), "focus": focus, "rows": rows,
            "next_date": _next_trade_date(d),             # 推荐买入日
            "gen_time": pool_gen_time(d),                 # 选股池生成时间（北京时间）
            "latest_trade_date": latest,                 # 最新交易日
            "is_viewing_newest": d == newest_pool,       # 是否在看「最新的那个池」
            "pool_behind": bool(newest_pool and newest_pool < latest),  # 最新池是否落后于最新交易日
        }
    except Exception as e:
        logger.exception("选股池失败")
        return {"ok": False, "error": str(e)}


@app.get("/sectorscope", response_class=HTMLResponse)
async def sectorscope_page(request: Request, _user: str = Depends(require_auth)):
    """Tab3 板块全景看板（纯因子，读宽表）。"""
    return templates.TemplateResponse(request=request, name="sectorscope.html", context={"page": "llm"})


@app.get("/api/sectorscope")
async def api_sectorscope(date: str = "", _user: str = Depends(require_auth)):
    """板块全景：三栏诊断 + 全板块明细（读宽表 theme_heat_all_in_one）。"""
    try:
        from app.strategy.sector_scope import build_sectorscope
        data = build_sectorscope(date)
        return {"ok": True, **data}
    except Exception as e:
        logger.exception("板块全景失败")
        return {"ok": False, "error": str(e)}


@app.get("/concept", response_class=HTMLResponse)
async def concept_page(request: Request, _user: str = Depends(require_auth)):
    """概念资金流仪表盘页面（同花顺概念口径）。"""
    return templates.TemplateResponse(request=request, name="concept.html", context={"page": "concept"})


@app.get("/api/concept")
async def api_concept(date: str = "", _user: str = Depends(require_auth)):
    """概念资金流仪表盘数据（同花顺概念，Tushare moneyflow_cnt_ths）。"""
    try:
        from app.strategy.concept_flow import build_concept_dashboard
        d = date or _last_trade_date()
        return {"ok": True, "data": build_concept_dashboard(d)}
    except Exception as e:
        logger.exception("概念数据失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/concept/detail")
async def api_concept_detail(date: str = "", code: str = "", _user: str = Depends(require_auth)):
    """单个概念的环境/微观详情（资金定性+领涨成分+公告+联网+LLM点评，按需+缓存）。"""
    if not code:
        return {"ok": False, "error": "缺少 code 参数"}
    try:
        from app.strategy.concept_detail import build_concept_detail
        d = date or _last_trade_date()
        return {"ok": True, "data": build_concept_detail(d, code)}
    except Exception as e:
        logger.exception("概念详情失败")
        return {"ok": False, "error": str(e)}


@app.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request, _user: str = Depends(require_auth)):
    """量化因子选股页面。"""
    from app.strategy.screener import FACTOR_GROUPS, CUSTOM_FIELDS
    default_date = _last_trade_date()
    # 转成 input[type=date] 需要的 YYYY-MM-DD
    iso_date = f"{default_date[:4]}-{default_date[4:6]}-{default_date[6:]}"
    return templates.TemplateResponse(
        request=request, name="screener.html",
        context={"factor_groups": FACTOR_GROUPS, "custom_fields": CUSTOM_FIELDS,
                 "default_date": iso_date},
    )


@app.post("/api/screen")
async def api_screen(request: Request, _user: str = Depends(require_auth)):
    """按选中因子筛选股票。Body: {date, factors:[key], custom:{n,op,val}, sort_by, limit}"""
    try:
        from app.strategy.screener import screen
        body = await request.json()
        date = body.get("date") or ""
        if not date:
            # 默认最近交易日（用 stock_basic 拿不到，简单用今天/上一交易日）
            import datetime
            date = datetime.datetime.now().strftime("%Y%m%d")
        return screen(
            date=date,
            selected_keys=body.get("factors", []),
            custom=body.get("custom"),
            customs=body.get("customs"),
            sort_by=body.get("sort_by", "rps120"),
            limit=int(body.get("limit", 100)),
        )
    except Exception as e:
        logger.exception("选股失败")
        return {"ok": False, "error": str(e)}


def _resolve_ts_code(raw: str) -> str:
    """归一化股票输入：6位代码补后缀 / 已带后缀直接用 / 名称查 stock_basic。"""
    import re
    s = (raw or "").strip().upper()
    if not s:
        return ""
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        if s[0] in "69":
            return s + ".SH"
        if s[0] == "8" or s[0] == "4":
            return s + ".BJ"
        return s + ".SZ"
    # 名称模糊匹配
    try:
        from app.data.composite_provider import CompositeProvider
        sb = CompositeProvider().get_stock_basic()
        hit = sb[sb["name"].astype(str).str.contains(raw.strip(), na=False)]
        if not hit.empty:
            return str(hit.iloc[0]["ts_code"])
    except Exception:
        pass
    return ""


def _stock_name(ts_code: str) -> str:
    """由 ts_code 反查股票名称（用缓存的 stock_basic，失败返回空串）。"""
    try:
        from app.data.composite_provider import CompositeProvider
        sb = CompositeProvider().get_stock_basic()
        hit = sb[sb["ts_code"].astype(str) == ts_code]
        if not hit.empty:
            return str(hit.iloc[0]["name"])
    except Exception:
        pass
    return ""


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, _user: str = Depends(require_auth)):
    """个股回测页：选票+选信号+区间 → 历史胜率/收益/资金曲线。"""
    from app.backtest.signal_backtest import list_signals
    from app.backtest.market_regime import INDEX_PRESETS
    return templates.TemplateResponse(
        request=request, name="backtest.html",
        context={"page": "backtest", "signals": list_signals(), "indices": INDEX_PRESETS},
    )


@app.get("/api/stock/profile")
async def api_stock_profile(code: str = "", _user: str = Depends(require_auth)):
    """个股股性画像：波动/妖性/趋势性/追高友好度 + 当前形态提示 + K线。"""
    try:
        from app.strategy.stock_profile import build_stock_profile
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入6位代码/完整代码/名称）"}
        name = ""
        try:
            from app.data.composite_provider import CompositeProvider
            sb = CompositeProvider().get_stock_basic()
            hit = sb[sb["ts_code"] == ts_code]
            if not hit.empty:
                name = str(hit.iloc[0]["name"])
        except Exception:
            pass
        return build_stock_profile(ts_code, name)
    except Exception as e:
        logger.exception("股性画像失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/financials")
async def api_stock_financials(code: str = "", _user: str = Depends(require_auth)):
    """财报跟踪：ROE/营收净利同比/负债率/毛利率 近几期趋势（Tushare）。"""
    try:
        from app.strategy.fundamentals import get_financials
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        return get_financials(ts_code)
    except Exception as e:
        logger.exception("财报跟踪失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/analyst")
async def api_stock_analyst(code: str = "", _user: str = Depends(require_auth)):
    """券商盈利预测/目标价（report_rc）。按需调用：5100档限频1次/小时，日缓存兜底。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.fundamentals import get_analyst_rc
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        return await run_in_threadpool(get_analyst_rc, ts_code)
    except Exception as e:
        logger.exception("盈利预测获取失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/alert")
async def api_stock_alert(code: str = "", _user: str = Depends(require_auth)):
    """LLM 近期提示：博查真实新闻 → v4-flash 接地总结（按日缓存）。"""
    try:
        from app.strategy.fundamentals import get_recent_alert
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        name = ""
        try:
            from app.data.composite_provider import CompositeProvider
            sb = CompositeProvider().get_stock_basic()
            hit = sb[sb["ts_code"] == ts_code]
            if not hit.empty:
                name = str(hit.iloc[0]["name"])
        except Exception:
            pass
        return get_recent_alert(ts_code, name)
    except Exception as e:
        logger.exception("近期提示失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/backtest/stock")
async def api_backtest_stock(request: Request, _user: str = Depends(require_auth)):
    """单股单信号回测。Body: {code, signal, start, end}。"""
    try:
        from app.backtest.signal_backtest import backtest_stock_signal
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入6位代码/完整代码/名称）"}
        start = (body.get("start") or "").replace("-", "")
        end = (body.get("end") or "").replace("-", "")
        if not start or not end:
            return {"ok": False, "msg": "请选择回测起止日期"}
        from app.backtest.market_regime import DEFAULT_INDEX
        custom = body.get("custom")
        index_code = body.get("index_code") or DEFAULT_INDEX
        regime_filter = body.get("regime_filter") or None
        result = backtest_stock_signal(ts_code, body.get("signal", ""), start, end,
                                       custom=custom, index_code=index_code,
                                       regime_filter=regime_filter)
        # 自动落历史（仅完整未过滤回测；过滤视图不入库，避免覆盖原记录）。
        if result.get("ok") and result.get("n_signals") and not regime_filter:
            try:
                from app.backtest.history import record
                hid = record(_user, result, name=_stock_name(ts_code), custom=custom)
                if hid:
                    result["history_id"] = hid   # 前端据此把研判/同类回填到本条历史
            except Exception:
                logger.exception("回测历史记录写入失败（忽略）")
        return result
    except Exception as e:
        logger.exception("个股回测失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/backtest/sector")
async def api_backtest_sector(request: Request, _user: str = Depends(require_auth)):
    """同类/板块分析：同类基准胜率(③) + 板块广度曲线 + 信号×广度分桶(④)。较慢→跑线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.backtest.sector_backtest import analyze_sector
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入6位代码/完整代码/名称）"}
        start = (body.get("start") or "").replace("-", "")
        end = (body.get("end") or "").replace("-", "")
        if not start or not end:
            return {"ok": False, "msg": "请选择回测起止日期"}
        result = await run_in_threadpool(
            analyze_sector, ts_code, body.get("signal", ""), start, end,
            custom=body.get("custom"))
        hid = body.get("history_id")          # 回填同类到对应历史记录，点开历史可还原
        if hid and result.get("ok"):
            try:
                from app.backtest.history import save_analysis
                save_analysis(int(hid), _user, sector=result)
            except Exception:
                logger.exception("同类回填历史失败（忽略）")
        return result
    except Exception as e:
        logger.exception("同类/板块分析失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/backtest/brief")
async def api_backtest_brief(request: Request, _user: str = Depends(require_auth)):
    """AI 综合研判：把已算好的回测/大盘/同类/板块/股性/基本面喂给 v4-pro 做接地解读。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.backtest.llm_brief import generate_brief
        payload = await request.json()
        # 注入博查实时新闻，供研判主动核查消息面/解禁/政策（best-effort，复用每日缓存）
        try:
            r = payload.get("result") or {}
            code = r.get("ts_code") or _resolve_ts_code(payload.get("code", ""))
            name = payload.get("name") or _stock_name(code)
            if code:
                from app.strategy.fundamentals import get_recent_alert
                alert = await run_in_threadpool(get_recent_alert, code, name)
                if alert.get("ok"):
                    payload["news"] = {"summary": alert.get("summary", ""),
                                       "sources": alert.get("sources", [])}
        except Exception:
            logger.exception("研判注入新闻失败（忽略）")
        out = await run_in_threadpool(generate_brief, payload)
        hid = payload.get("history_id")       # 回填研判(+同类)到对应历史记录，永不重算重花钱
        if hid and out.get("ok"):
            try:
                from app.backtest.history import save_analysis
                save_analysis(int(hid), _user, brief=out, sector=payload.get("sector"))
            except Exception:
                logger.exception("研判回填历史失败（忽略）")
        return out
    except Exception as e:
        logger.exception("AI 综合研判失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/backtest/scout")
async def api_backtest_scout(request: Request, _user: str = Depends(require_auth)):
    """反向策略推荐：选票→全信号回测→按样本/T+5期望/盈亏比打分→推荐最贴股性的打法。
    Body: {code, start, end, min_sample?}。较重(16信号+股性)→跑线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.backtest.strategy_scout import scout_strategies
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入6位代码/完整代码/名称）"}
        start = (body.get("start") or "").replace("-", "")
        end = (body.get("end") or "").replace("-", "")
        if not start or not end:
            return {"ok": False, "msg": "请选择扫描起止日期"}
        return await run_in_threadpool(
            scout_strategies, ts_code, start, end,
            name=_stock_name(ts_code), min_sample=int(body.get("min_sample") or 4))
    except Exception as e:
        logger.exception("反向策略推荐失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/backtest/scout/note")
async def api_backtest_scout_note(request: Request, _user: str = Depends(require_auth)):
    """对已算好的 scout 结果生成 LLM 一句话点评（解读层，不参与排名）。Body: {result}。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.backtest.strategy_scout import generate_scout_note
        body = await request.json()
        result = body.get("result") or {}
        if not result.get("ok"):
            return {"ok": False, "msg": "请先运行策略扫描"}
        return await run_in_threadpool(generate_scout_note, result)
    except Exception as e:
        logger.exception("策略点评失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/backtest/history")
async def api_backtest_history(q: str = "", limit: int = 100,
                               _user: str = Depends(require_auth)):
    """个股回测历史列表（仅本人，时间倒序）。q 模糊搜索票/信号。"""
    try:
        from app.backtest.history import list_records
        rows = list_records(creator=_user, q=q.strip(), limit=limit)
        return {"ok": True, "rows": rows}
    except Exception as e:
        logger.exception("回测历史列表失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/backtest/history/get")
async def api_backtest_history_get(id: int, _user: str = Depends(require_auth)):
    """取单条回测历史的完整结果（用于点开还原整页）。"""
    try:
        from app.backtest.history import get_record
        rec = get_record(int(id), creator=_user)
        if not rec:
            return {"ok": False, "error": "记录不存在或无权查看"}
        return {"ok": True, "rec": rec}
    except Exception as e:
        logger.exception("回测历史取详情失败")
        return {"ok": False, "error": str(e)}


@app.post("/api/backtest/history/delete")
async def api_backtest_history_delete(request: Request, _user: str = Depends(require_auth)):
    """删除一条回测历史（仅本人可删）。"""
    try:
        from app.backtest.history import delete
        body = await request.json()
        ok = delete(int(body.get("id", 0)), _user)
        return {"ok": ok, "error": "" if ok else "无权删除或记录不存在"}
    except Exception as e:
        logger.exception("删除回测历史失败")
        return {"ok": False, "error": str(e)}


@app.post("/api/strategy/save")
async def api_strategy_save(request: Request, _user: str = Depends(require_auth)):
    """保存选股策略（名称+条件载荷）。同创建者同名覆盖。"""
    try:
        from app.strategy.saved_strategies import save
        body = await request.json()
        name = (body.get("name") or "").strip()
        payload = body.get("payload") or {}
        if not name:
            return {"ok": False, "error": "策略名称不能为空"}
        sid = save(name, _user, payload)
        return {"ok": True, "id": sid}
    except Exception as e:
        logger.exception("保存策略失败")
        return {"ok": False, "error": str(e)}


@app.get("/api/strategy/list")
async def api_strategy_list(mine: int = 0, q: str = "", _user: str = Depends(require_auth)):
    """策略库列表。mine=1 仅看本人；q 模糊搜索名称/创建者。"""
    try:
        from app.strategy.saved_strategies import list_strategies
        rows = list_strategies(creator=_user if mine else None, q=q.strip())
        return {"ok": True, "me": _user, "rows": rows}
    except Exception as e:
        logger.exception("策略库列表失败")
        return {"ok": False, "error": str(e)}


@app.post("/api/strategy/delete")
async def api_strategy_delete(request: Request, _user: str = Depends(require_auth)):
    """删除策略（仅创建者本人可删）。"""
    try:
        from app.strategy.saved_strategies import delete
        body = await request.json()
        ok = delete(int(body.get("id", 0)), _user)
        return {"ok": ok, "error": "" if ok else "无权删除或策略不存在"}
    except Exception as e:
        logger.exception("删除策略失败")
        return {"ok": False, "error": str(e)}


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(request=request, name="strategy.html", context={})


@app.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(request=request, name="tracking.html", context={"active": []})


@app.get("/api/strategy")
async def api_strategy(is_backtest: str = "", start_date: str = "", end_date: str = "",
                       _user: str = Depends(require_auth)):
    try:
        from app.strategy.analyzer import full_analysis
        bt = int(is_backtest) if is_backtest in ("0", "1") else None
        result = full_analysis(is_backtest=bt, start_date=start_date or None,
                               end_date=end_date or None, min_samples=5)
        return {"ok": True, "data": result}
    except Exception as e:
        logger.exception("strategy API 出错")
        return {"ok": False, "error": str(e)}


@app.get("/api/tracking")
async def api_tracking(_user: str = Depends(require_auth)):
    try:
        from app.strategy.db import get_all_with_performance
        return {"ok": True, "data": get_all_with_performance(is_backtest=0)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────

def start_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    logger.info("Web UI 启动: http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    start_server()
