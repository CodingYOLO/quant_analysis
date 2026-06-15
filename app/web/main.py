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


def _parse_report_meta(stem: str) -> dict:
    """
    解析报告文件名，返回展示元信息 + 分类。
    选股报告：YYYYMMDD          → category=select
    快讯：    YYYYMMDD_HHMM_pre  → category=pre/mid/post
    """
    parts = stem.split("_")
    date = parts[0]
    display_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else date
    if len(parts) >= 3:  # 快讯
        hhmm, session = parts[1], parts[2]
        label = _SESSION_LABEL.get(session, session)
        time_str = f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm
        return {"name": stem, "category": session, "kind": f"{label}快讯",
                "date": display_date, "time": time_str}
    return {"name": stem, "category": "select", "kind": "完整选股报告",
            "date": display_date, "time": "盘后"}


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
    metas = [_parse_report_meta(f.stem) for f in files]
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


@app.post("/api/generate_selection")
async def api_generate_selection(_user: str = Depends(require_auth)):
    """按需运行完整选股流水线（吴川三层+量化+风控），返回报告 HTML。耗时约2-3分钟。"""
    try:
        import time as _t
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
        return {"ok": True, "title": f"完整选股报告 {trade_date}", "name": trade_date,
                "html": _render_markdown(content)}
    except Exception as e:
        logger.exception("选股流水线失败")
        return {"ok": False, "error": str(e)}


@app.post("/api/generate/{session}")
async def api_generate(session: str, _user: str = Depends(require_auth)):
    """
    按需生成三时段快讯之一（pre/mid/post），不推送，仅返回 HTML 供网页预览。
    """
    if session not in ("pre", "mid", "post"):
        return {"ok": False, "error": "session 必须是 pre/mid/post"}
    try:
        from app.nodes.quick_report import build_quick_report
        filepath, title, content = build_quick_report(session)
        return {
            "ok": True,
            "title": title,
            "name": Path(filepath).stem,
            "html": _render_markdown(content),
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


@app.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request, _user: str = Depends(require_auth)):
    """量化因子选股页面。"""
    from app.strategy.screener import FACTOR_GROUPS
    default_date = _last_trade_date()
    # 转成 input[type=date] 需要的 YYYY-MM-DD
    iso_date = f"{default_date[:4]}-{default_date[4:6]}-{default_date[6:]}"
    return templates.TemplateResponse(
        request=request, name="screener.html",
        context={"factor_groups": FACTOR_GROUPS, "default_date": iso_date},
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
            sort_by=body.get("sort_by", "rps120"),
            limit=int(body.get("limit", 100)),
        )
    except Exception as e:
        logger.exception("选股失败")
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
