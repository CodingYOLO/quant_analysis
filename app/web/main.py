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

import json as _json_std
import logging
import math
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import get_settings

logger = logging.getLogger(__name__)


def _json_sanitize(obj):
    """递归把 NaN/Inf → None，保证任何响应都能 JSON 序列化。

    根治反复出现的 `ValueError: Out of range float values are not JSON compliant`：
    个别端点某个浮点算出 NaN/Inf（低量除零、`NaN or 0` 因 NaN 为 truthy 仍得 NaN 等），
    一个坏值就打崩整份响应→前端 500/白屏。此处在序列化前统一兜底，杜绝打地鼠。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


class SafeJSONResponse(JSONResponse):
    """全局 JSON 响应：序列化前清洗 NaN/Inf→null（只影响 dict/list 返回·不动 HTML/重定向/文件响应）。"""

    def render(self, content) -> bytes:
        return _json_std.dumps(_json_sanitize(content), ensure_ascii=False,
                               allow_nan=False, separators=(",", ":")).encode("utf-8")


app = FastAPI(title="A股Agent", docs_url=None, redoc_url=None,
              default_response_class=SafeJSONResponse)

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


@app.on_event("startup")
async def _startup_realtime() -> None:
    """启动全推实时枢纽 + 盯盘扫描线程。休市连不上不报错，开盘自动接入。"""
    try:
        from app.strategy.realtime_hub import ensure_started
        from app.strategy.realtime_scan import start_scanner
        ensure_started()
        start_scanner()
        logger.info("实时枢纽与盯盘扫描线程已启动")
    except Exception:
        logger.exception("实时枢纽启动失败（不影响其余功能）")


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
    ("digest", "📰 消息面 / 前瞻"),   # 非交易日(周末/节假日)消息面复盘 + 下周前瞻
    ("poolcheck", "📋 选股池盘前体检"),   # 交易日开盘前·选股池消息面体检(利好/利空/舆情)
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
    if len(parts) >= 3:  # 快讯 / 非交易日消息面报告
        session = parts[2]
        if session == "digest":   # YYYYMMDD_HHMM_digest_{daily|preview}：非交易日消息面报告
            mode = parts[3] if len(parts) >= 4 else "daily"
            kind = "下周前瞻" if mode == "preview" else "消息面复盘"
            return {"name": stem, "category": "digest", "kind": kind,
                    "date": display_date, "time": gen_time}
        if session == "poolcheck":   # YYYYMMDD_HHMM_poolcheck：选股池盘前消息面体检
            return {"name": stem, "category": "poolcheck", "kind": "选股池盘前体检",
                    "date": display_date, "time": gen_time}
        label = _SESSION_LABEL.get(session, session)
        return {"name": stem, "category": session, "kind": f"{label}快讯",
                "date": display_date, "time": gen_time}
    return {"name": stem, "category": "select", "kind": "完整选股报告",
            "date": display_date, "time": gen_time}


def _render_markdown(md_text: str) -> str:
    """Markdown→HTML 正文片段（网页深色主题用）。

    用 fragment 版而非邮件版：邮件版自带白底内联 <style>（td/p 深色字、
    偶数行白底），嵌进深色页面会让大半文字隐形。样式交给页面 .md-body/.report-body。
    """
    from app.notify.notifier import _md_to_html_fragment
    return _md_to_html_fragment(md_text)


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
            groups.append({"cat": cat, "label": label, "reports": items})
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"groups": groups, "total": len(metas), "page": "index"},
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


@app.get("/generate")
async def generate_page(_user: str = Depends(require_auth)):
    """一键生成已并入报告中心首页 → 重定向到 /（兼容旧链接/书签）。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=307)


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


def _trade_day_info(td: str) -> tuple[bool, str]:
    """判断 td（YYYYMMDD）是否交易日，并返回 (is_trading, 最近≤td 的交易日)。

    用于日期选择器选到周末/节假日时给出明确提示。取数失败时返回 (True, td) 不拦截。
    """
    import datetime
    try:
        from app.data.composite_provider import CompositeProvider
        base = datetime.datetime.strptime(td, "%Y%m%d")
        start = (base - datetime.timedelta(days=20)).strftime("%Y%m%d")
        cal = CompositeProvider().get_trade_cal(start, td)
        open_days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
        return (td in open_days), (open_days[-1] if open_days else td)
    except Exception:
        return True, td


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


@app.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request, _user: str = Depends(require_auth)):
    """大盘体检：多维同轴 + 板块轮动 + 信号复盘，一张大局图。"""
    resp = templates.TemplateResponse(request=request, name="overview.html", context={"page": "overview"})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"   # 迭代页·避免浏览器缓存旧版
    return resp


@app.get("/perception", response_class=HTMLResponse)
async def perception_page(request: Request, _user: str = Depends(require_auth)):
    """盘感训练：盲测复盘，看截至 T0 的图判断未来 N 日走势，揭晓评分。"""
    resp = templates.TemplateResponse(request=request, name="perception.html", context={"page": "perception"})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# 出题暂存（quiz_id → answer）：答案不下发前端，提交时服务端核对，避免 devtools 偷看
_QUIZ_STASH: dict[str, dict] = {}


@app.get("/api/train/new")
async def api_train_new(code: str = "", _user: str = Depends(require_auth)):
    """出一道盘感题：返回题面(截至T0·零泄漏) + quiz_id；答案暂存服务端。"""
    try:
        import uuid

        from fastapi.concurrency import run_in_threadpool

        from app.strategy.perception_trainer import build_quiz
        ts = _resolve_ts_code(code) if code else None
        q = await run_in_threadpool(lambda: build_quiz(code=ts))
        if not q.get("ok"):
            return {"ok": False, "msg": q.get("msg", "出题失败")}
        qid = uuid.uuid4().hex
        if len(_QUIZ_STASH) > 200:                  # 防内存膨胀·简单上限
            _QUIZ_STASH.clear()
        _QUIZ_STASH[qid] = q                        # 整道(含题面)·供揭晓时生成 AI 盲读
        return {"ok": True, "quiz_id": qid, "question": q["question"]}
    except Exception as e:
        logger.exception("盘感出题失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/train/answer")
async def api_train_answer(request: Request, _user: str = Depends(require_auth)):
    """提交预测：评分 + 落库 + 揭晓答案(含未来K线/股名/日期) + 最新统计。Body {quiz_id, bucket}。"""
    try:
        from app.strategy import db
        from app.strategy.perception_trainer import DEFAULT_FWD, score
        body = await request.json()
        quiz = _QUIZ_STASH.get(body.get("quiz_id", ""))    # 不弹出·留给可选 AI 盲读
        if not quiz:
            return {"ok": False, "msg": "题目已过期，请重新出题"}
        ans = quiz["answer"]
        pred = str(body.get("bucket", ""))
        sc = score(pred, ans["bucket"])
        db.log_perception(ts_code=ans["ts_code"], name=ans["name"], t0=ans["t0"],
                          setup_tag=ans["setup_tag"], market_state=ans["market_state"],
                          pred=pred, actual=ans["bucket"],
                          ret_fwd=ans["rets"].get(DEFAULT_FWD, ans["rets"].get(5, 0.0)),
                          points=sc["points"], direction_right=sc["direction_right"])
        return {"ok": True, "score": sc, "answer": ans, "stats": db.perception_stats()}
    except Exception as e:
        logger.exception("盘感评分失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/train/ai")
async def api_train_ai(quiz_id: str = "", _user: str = Depends(require_auth)):
    """按需 AI 盲读：只喂截至 T0 的题面(零未来)→ 结构化多空研判。用户点按钮才调·不拖慢练习。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.perception_trainer import ai_blind_read
        quiz = _QUIZ_STASH.get(quiz_id)
        if not quiz:
            return {"ok": False, "msg": "题目已过期，重新出题后再试"}
        ai = await run_in_threadpool(ai_blind_read, quiz["question"])
        return {"ok": True, "ai_read": ai}
    except Exception as e:
        logger.exception("AI盲读失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/train/stats")
async def api_train_stats(_user: str = Depends(require_auth)):
    """盘感训练累计统计（胜率/方向/分状态/分形态·对比随机基准）。"""
    try:
        from app.strategy import db
        return {"ok": True, "stats": db.perception_stats()}
    except Exception as e:
        logger.exception("盘感统计失败")
        return {"ok": False, "msg": str(e)}


# ── 做T训练（盘中 T+0 波段·分时回放） ──────────────────────────────────
@app.get("/tplus", response_class=HTMLResponse)
async def tplus_page(request: Request, _user: str = Depends(require_auth)):
    """做T训练：选股+近期某交易日，分时逐步播放，高抛低吸，收盘结算。"""
    resp = templates.TemplateResponse(request=request, name="tplus.html", context={"page": "tplus"})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


_TPLUS_STASH: dict[str, dict] = {}     # session_id → 当日分时(结算按下标取价·防客户端伪造)


@app.get("/api/tplus/new")
async def api_tplus_new(code: str = "", day: str = "", _user: str = Depends(require_auth)):
    """开一局做T：返回某交易日真实分时(逐根·含均价) + 昨收 + 可选日期；分时暂存服务端供结算。"""
    try:
        import uuid

        from fastapi.concurrency import run_in_threadpool

        from app.strategy.tplus_trainer import build_session
        ts = _resolve_ts_code(code) if code else None
        s = await run_in_threadpool(lambda: build_session(code=ts, day=(day or None)))
        if not s.get("ok"):
            return {"ok": False, "msg": s.get("msg", "开局失败")}
        sid = uuid.uuid4().hex
        if len(_TPLUS_STASH) > 100:
            _TPLUS_STASH.clear()
        _TPLUS_STASH[sid] = s
        return {"ok": True, "session_id": sid, "name": s["name"], "ts_code": s["ts_code"],
                "day": s["day"], "prev_close": s["prev_close"], "bars": s["bars"],
                "avail_days": s["avail_days"]}
    except Exception as e:
        logger.exception("做T开局失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/tplus/settle")
async def api_tplus_settle(request: Request, _user: str = Depends(require_auth)):
    """做T结算：Body {session_id, base, trades:[{i,side,qty}]}。结算按服务端暂存的分时取价。"""
    try:
        from app.strategy.tplus_trainer import settle
        body = await request.json()
        s = _TPLUS_STASH.get(body.get("session_id", ""))
        if not s:
            return {"ok": False, "msg": "本局已过期，请重新开局"}
        prices = [b["c"] for b in s["bars"]]
        base = int(body.get("base") or 1000)
        r = settle(prices, body.get("trades") or [], base=base, close=prices[-1],
                   prev_close=(s.get("prev_close") or s["bars"][0]["o"]))
        return {"ok": True, "settle": r}
    except Exception as e:
        logger.exception("做T结算失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/overview")
async def api_market_overview(days: int = 120, _user: str = Depends(require_auth)):
    """大盘体检数据：多维同轴序列 + 板块轮动矩阵 + 地量冰点信号事件研究。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.market_overview import build_overview
        end_date = _last_trade_date()
        data = await run_in_threadpool(build_overview, end_date, days)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.exception("大盘体检数据失败")
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
    """板块雷达：下拉列表 + 四栏诊断（资金暗流/低吸/轮动/高位风险，读宽表已过滤）+ 持仓对照。"""
    try:
        from app.strategy.sector_radar import build_sector_radar
        watch = _watch_industries() if type == "industry" else set()
        return build_sector_radar(date, theme_type=type, watch_names=watch)
    except Exception as e:
        logger.exception("板块雷达失败")
        return {"ok": False, "error": str(e)}


def _watch_industries() -> set:
    """用户自选/持仓覆盖的申万行业集合（供板块雷达持仓对照·失败返回空集不影响主流程）。"""
    try:
        from app.data.composite_provider import CompositeProvider
        from app.strategy import db
        codes = {w["ts_code"] for w in db.get_watchlist()}
        if not codes:
            return set()
        sb = CompositeProvider().get_stock_basic()
        return set(sb[sb["ts_code"].isin(codes)]["industry"].dropna().astype(str))
    except Exception:
        return set()


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


@app.get("/bull", response_class=HTMLResponse)
async def bull_page(request: Request, _user: str = Depends(require_auth)):
    """🐂 牛股发掘：左侧埋伏引擎（政策/新闻催化 → 板块 → 埋伏票）。"""
    return templates.TemplateResponse(request=request, name="bull.html", context={"page": "bull"})


@app.get("/api/bull/catalysts")
async def api_bull_catalysts(date: str = "", refresh: bool = False, tech: bool = False,
                             _user: str = Depends(require_auth)):
    """催化层：真实新闻→LLM抽取→映射到库内真实概念（按日缓存；refresh=1 强制重算；tech=1 只看科技赛道）。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.bull_hunter import discover_catalysts
        d = (date or "").replace("-", "") or _last_trade_date()
        return await run_in_threadpool(discover_catalysts, d, None, None, bool(refresh), bool(tech))
    except Exception as e:
        logger.exception("牛股发掘·催化层失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/bull/ambush")
async def api_bull_ambush(concept: str = "", date: str = "",
                          in_catalyst: bool = True, refresh: bool = False,
                          _user: str = Depends(require_auth)):
    """埋伏层：概念成分批量预筛→逐只真业绩+避雷→埋伏分排名（按日+概念缓存）。首次较重→线程池。"""
    if not concept:
        return {"ok": False, "msg": "缺少 concept 参数"}
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.bull_hunter import find_ambush_stocks
        d = (date or "").replace("-", "") or _last_trade_date()
        return await run_in_threadpool(
            find_ambush_stocks, concept, d, None, bool(in_catalyst), bool(refresh))
    except Exception as e:
        logger.exception("牛股发掘·埋伏层失败")
        return {"ok": False, "msg": str(e)}


@app.get("/research", response_class=HTMLResponse)
async def research_page(request: Request, _user: str = Depends(require_auth)):
    """📑 研报中心：博查抓媒体券商研报观点 → LLM 接地总结 → 映射板块·联动牛股发掘。"""
    return templates.TemplateResponse(request=request, name="research.html", context={"page": "research"})


@app.get("/api/research")
async def api_research(date: str = "", refresh: bool = False, tech: bool = False,
                      _user: str = Depends(require_auth)):
    """研报中心：博查媒体研报观点→LLM总结。按日缓存·线程池；tech=1 只看科技赛道(半导体/CPO/算力…)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.bull_hunter import discover_research
        d = (date or "").replace("-", "") or _last_trade_date()
        return await run_in_threadpool(discover_research, d, None, None, bool(refresh), bool(tech))
    except Exception as e:
        logger.exception("研报中心失败")
        return {"ok": False, "msg": str(e)}


@app.get("/analysts", response_class=HTMLResponse)
async def analysts_page(request: Request, _user: str = Depends(require_auth)):
    """🏅 金牌分析师榜：东财按历史荐股收益率排名 + 最新荐股 + 跟踪记录下钻。"""
    return templates.TemplateResponse(request=request, name="analysts.html", context={"page": "analysts"})


@app.get("/api/analysts")
async def api_analysts(year: str = "", tech: bool = False, top: int = 60,
                       _user: str = Depends(require_auth)):
    """金牌分析师榜（东财·按收益率）：tech=1 只看科技覆盖行业(电子/通信/计算机/半导体…)。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.analysts import get_analyst_board
        return await run_in_threadpool(get_analyst_board, year, bool(tech), int(top))
    except Exception as e:
        logger.exception("金牌分析师榜失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/analyst/picks")
async def api_analyst_picks(id: str = "", _user: str = Depends(require_auth)):
    """某分析师的跟踪记录（历史跟踪成分股·当前持有优先）。"""
    if not id:
        return {"ok": False, "msg": "缺少 id"}
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.analysts import get_analyst_picks
        return await run_in_threadpool(get_analyst_picks, id)
    except Exception as e:
        logger.exception("分析师明细失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/lhb/inst")
async def api_lhb_inst(date: str = "", tech: bool = False, top: int = 30,
                      _user: str = Depends(require_auth)):
    """龙虎榜机构净买/净卖榜（真机构钱·日度）。date 默认最近交易日；tech=1 只看科技赛道。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.lhb_inst import build_inst_board
        d = (date or "").replace("-", "")
        if d:                                          # 显式选了日期 → 校验是否交易日
            is_trading, nearest = _trade_day_info(d)
            if not is_trading:
                return {"ok": True, "trading": False, "date": d, "nearest": nearest,
                        "tech_only": bool(tech), "n_total": 0, "buys": [], "sells": []}
        else:
            d = _last_trade_date()
        board = await run_in_threadpool(build_inst_board, CompositeProvider(), d, int(top), bool(tech))
        return {"ok": True, "trading": True, **board}
    except Exception as e:
        logger.exception("龙虎榜机构榜失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/lhb/seats")
async def api_lhb_seats(code: str = "", date: str = "", _user: str = Depends(require_auth)):
    """某只票当日龙虎榜全部席位明细（机构/北向/游资/外资分类）+ 资金风格。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.lhb_seats import infer_style, interpret_next_day, seat_rows
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        d = (date or "").replace("-", "") or _last_trade_date()

        def _gather() -> dict:
            df = CompositeProvider().get_lhb_inst(d)
            sub = df[df["ts_code"] == ts_code] if df is not None and not df.empty else None
            seats = seat_rows(sub) if sub is not None else []
            reason = seats[0]["reason"] if seats else ""
            return {"ok": True, "ts_code": ts_code, "date": d, "seats": seats,
                    "style": infer_style(seats), "next_day": interpret_next_day(seats, reason)}

        return await run_in_threadpool(_gather)
    except Exception as e:
        logger.exception("龙虎榜席位明细失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/lhb/review")
async def api_lhb_review(code: str = "", months: int = 3, _user: str = Depends(require_auth)):
    """个股龙虎榜复盘：区间内全部上榜 + 席位/资金风格 + 之后T+N走势 + 规律。线程池。"""
    try:
        import datetime

        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.lhb_review import review_stock
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        end = datetime.datetime.now().strftime("%Y%m%d")
        start = (datetime.datetime.now() - datetime.timedelta(days=int(months) * 31)).strftime("%Y%m%d")
        out = await run_in_threadpool(review_stock, CompositeProvider(), ts_code, start, end)
        out["name"] = _stock_name(ts_code)
        return out
    except Exception as e:
        logger.exception("龙虎榜复盘失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/lhb/review/note")
async def api_lhb_review_note(request: Request, _user: str = Depends(require_auth)):
    """对已算好的复盘结果生成 LLM 规律解读。Body: {review, name}。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.lhb_review import build_review_note
        body = await request.json()
        review = body.get("review") or {}
        if not review.get("occurrences"):
            return {"ok": False, "msg": "请先运行复盘"}
        return await run_in_threadpool(build_review_note, review, body.get("name") or "")
    except Exception as e:
        logger.exception("复盘解读失败")
        return {"ok": False, "msg": str(e)}


# ── AI 投研「后台不中断」生成：在途注册表单例 + SSE 观察者 ─────────────────────
_CHAT_REGISTRY = None


def _chat_registry():
    """惰性构建在途生成注册表（注入 run_chat 与 db.add_chat_message，便于解耦/单测）。"""
    global _CHAT_REGISTRY
    if _CHAT_REGISTRY is None:
        from app.strategy import db
        from app.strategy.chat_agent import run_chat
        from app.strategy.chat_inflight import InflightRegistry
        _CHAT_REGISTRY = InflightRegistry(run_chat, db.add_chat_message)
    return _CHAT_REGISTRY


async def _sse_tail(job):
    """把在途任务的事件序列以 SSE 吐给客户端（从头回放，断开/重连都行）；
    客户端断开只是停止本观察者，后台生成不受影响、照常跑完并落库。"""
    import asyncio
    import json as _json
    i = 0
    while True:
        n = len(job.events)
        while i < n:
            yield "data: " + _json.dumps(job.events[i], ensure_ascii=False) + "\n\n"
            i += 1
        if job.done and i >= len(job.events):
            break
        await asyncio.sleep(0.05)


@app.get("/api/chat/sessions")
async def api_chat_sessions(_user: str = Depends(require_auth)):
    """AI 问答会话列表。"""
    from app.strategy import db
    return {"ok": True, "sessions": db.list_chat_sessions()}


@app.post("/api/chat/session")
async def api_chat_session_new(_user: str = Depends(require_auth)):
    """新建会话。"""
    from app.strategy import db
    return {"ok": True, "id": db.new_chat_session()}


@app.get("/api/chat/session/{sid}")
async def api_chat_session_get(sid: int, _user: str = Depends(require_auth)):
    """某会话的消息历史 + 是否仍在后台生成（前端切回时据此重新接上流）。"""
    from app.strategy import db
    return {"ok": True, "messages": db.get_chat_messages(sid),
            "generating": _chat_registry().is_active(sid)}


@app.post("/api/chat/session/{sid}/delete")
async def api_chat_session_delete(sid: int, _user: str = Depends(require_auth)):
    from app.strategy import db
    return {"ok": db.delete_chat_session(sid)}


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request, _user: str = Depends(require_auth)):
    """AI 投研问答·SSE：生成放后台线程跑(切页/断网不中断·答案必落库)，本响应仅作观察者。"""
    from fastapi.responses import StreamingResponse

    from app.strategy import db
    body = await request.json()
    sid = int(body.get("session_id") or 0)
    message = str(body.get("message") or "").strip()
    task = body.get("task") if body.get("task") in ("pro", "flash") else "pro"   # 模型档位·默认强模型
    if not sid or not message:
        return {"ok": False, "msg": "缺少 session_id 或 message"}

    reg = _chat_registry()
    job = reg.get(sid)
    if job is None or job.done:                                   # 无在途任务 → 落库用户消息并启动后台生成
        db.add_chat_message(sid, "user", message)
        msgs = db.get_chat_messages(sid)
        if sum(1 for m in msgs if m["role"] == "user") == 1:      # 首条→用它做标题
            db.rename_chat_session(sid, message[:24])
        hist = [{"role": m["role"], "content": m["content"]} for m in msgs][-12:]
        job = reg.start(sid, hist, task)                          # 已在途则复用，避免重复生成/扣费

    return StreamingResponse(_sse_tail(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/session/{sid}/stream")
async def api_chat_reattach(sid: int, _user: str = Depends(require_auth)):
    """切回页面时重新接上仍在进行(或刚完成)的生成流；无在途任务则返回 inflight:false。"""
    from fastapi.responses import StreamingResponse
    job = _chat_registry().get(sid)
    if job is None:
        return {"ok": False, "inflight": False}
    return StreamingResponse(_sse_tail(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request, _user: str = Depends(require_auth)):
    """💼 我的持仓：自选盯盘 + 持仓盈亏 + 持仓体检 + 事件预警。"""
    return templates.TemplateResponse(request=request, name="portfolio.html", context={"page": "portfolio"})


@app.get("/hold", response_class=HTMLResponse)
async def hold_page(request: Request, _user: str = Depends(require_auth)):
    """🤚 拿得住：对每只持仓按《持有手册》4问做数据接地的'卖不卖'判定。"""
    return templates.TemplateResponse(request=request, name="hold.html", context={"page": "hold"})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, _user: str = Depends(require_auth)):
    """🤖 AI 投研助手（全页）：与右下角悬浮窗共用同一套 /api/chat/* 会话与历史，仅入口不同。"""
    return templates.TemplateResponse(request=request, name="chat.html", context={"page": "chat"})


@app.get("/lhb", response_class=HTMLResponse)
async def lhb_page(request: Request, _user: str = Depends(require_auth)):
    """🏛️ 机构动向：龙虎榜机构席位每日真实净买/净卖榜（真金白银·可按科技赛道过滤）。"""
    return templates.TemplateResponse(request=request, name="lhb_inst.html", context={"page": "lhb"})


@app.get("/stock", response_class=HTMLResponse)
async def stock360_page(request: Request, _user: str = Depends(require_auth)):
    """🎯 个股360：一个股票，一页出 K线/资金/板块/财务/研报/策略/新闻 + AI 综合买入判断。"""
    return templates.TemplateResponse(request=request, name="stock360.html", context={"page": "stock360"})


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request, _user: str = Depends(require_auth)):
    """📝 交易计划：录入你最终决定的下单计划 → 导出 plan.json + QMT 执行脚本（不下单）。"""
    return templates.TemplateResponse(request=request, name="plan.html", context={"page": "plan"})


@app.get("/market", response_class=HTMLResponse)
async def market_page(request: Request, _user: str = Depends(require_auth)):
    """📡 行情中枢：东财热榜 + 7×24快讯 + 财经日历（盘面速览·主动来查·不推送）。"""
    return templates.TemplateResponse(request=request, name="market.html", context={"page": "market"})


@app.get("/chain", response_class=HTMLResponse)
async def chain_page(request: Request, _user: str = Depends(require_auth)):
    """🔗 产业链：资源材料→制造→应用 三层·每环挂龙头·按今日强度上色+领头羊+风格切换。"""
    from app.strategy.tech_chain import chain_names
    return templates.TemplateResponse(request=request, name="chain.html",
                                      context={"page": "chain", "chains": chain_names()})


@app.get("/api/chain")
async def api_chain(name: str = "", refresh: bool = False, _user: str = Depends(require_auth)):
    """某条产业链的实时地图（龙头实时表现 + 上色 + 今日风格）。refresh=1 强制重拉报价。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy import tech_chain
        if refresh:                       # 清60秒报价缓存 → 真正重拉最新
            tech_chain._SPOT_CACHE.clear()
        nm = name or tech_chain.chain_names()[0]
        return await run_in_threadpool(tech_chain.build_chain, CompositeProvider(), nm)
    except Exception as e:
        logger.exception("产业链地图失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/chain/levels")
async def api_chain_levels(name: str = "", _user: str = Depends(require_auth)):
    """某条链龙头的关键位·入局区间叠加（地图渲染后异步单拉·按交易日缓存·线程池）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy import tech_chain
        nm = name or tech_chain.chain_names()[0]
        return await run_in_threadpool(tech_chain.build_chain_levels, CompositeProvider(), nm)
    except Exception as e:
        logger.exception("产业链关键位叠加失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/hot")
async def api_market_hot(top: int = 40, kind: str = "rank", _user: str = Depends(require_auth)):
    """东财热榜（kind=rank 人气榜 / up 飙升榜）Top N（线程池·缓存）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.market_hub import hot_board
        board = await run_in_threadpool(hot_board, CompositeProvider(), kind, int(top))
        return {"ok": True, "kind": kind, **board}
    except Exception as e:
        logger.exception("东财热榜失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/concept")
async def api_market_concept(top: int = 30, _user: str = Depends(require_auth)):
    """概念热度榜（自家宽表·按 heat_score 降序·线程池·30分钟缓存）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.market_hub import concept_heat
        return {"ok": True, "rows": await run_in_threadpool(concept_heat, int(top))}
    except Exception as e:
        logger.exception("概念热度失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/style-radar")
async def api_market_style_radar(_user: str = Depends(require_auth)):
    """风格切换雷达：6大风格资金动量 + 轮动检测（行业宽表·盘后·线程池）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.style_radar import build_style_radar
        return await run_in_threadpool(build_style_radar)
    except Exception as e:
        logger.exception("风格雷达失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/market/hot/ingest")
async def api_market_hot_ingest(request: Request, _user: str = Depends(require_auth)):
    """接收本地电脑同步的东财热榜（住宅IP直连·绕开云IP限流）。Body: {kind, rows}。"""
    try:
        from app.strategy.market_hub import save_hot_disk
        b = await request.json()
        kind = b.get("kind") or "rank"
        rows = b.get("rows") or []
        if len(rows) < 10:        # 防呆：真榜单80+条·少于10视为误推/测试·拒绝落盘
            return {"ok": False, "msg": f"榜单条数过少({len(rows)})·疑似误推·拒绝", "saved": 0}
        n = save_hot_disk(kind, rows, source="本地同步")
        return {"ok": bool(n), "saved": n}
    except Exception as e:
        logger.exception("热榜同步接收失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/hot/sync-script")
async def api_market_sync_script(_user: str = Depends(require_auth)):
    """下载本地同步脚本（在家电脑跑·拉东财热榜推送到服务器·备用方案）。"""
    from fastapi.responses import Response

    from app.strategy.market_hub import LOCAL_SYNC_SCRIPT
    return Response(content=LOCAL_SYNC_SCRIPT, media_type="text/x-python",
                    headers={"Content-Disposition": "attachment; filename=local_hotrank_sync.py"})


@app.post("/api/hotrank/history/ingest")
async def api_hotrank_history_ingest(request: Request, _user: str = Depends(require_auth)):
    """接收家用详情API脚本推的【个股人气排名历史】(每行自带 trade_date)→落 hot_rank_log。"""
    try:
        from app.strategy import db
        b = await request.json()
        rows = b.get("rows") or []
        kind = b.get("kind") or "rank"
        if not rows:
            return {"ok": False, "msg": "空 rows", "saved": 0}
        n = db.log_hot_rank(kind, rows)
        return {"ok": bool(n), "saved": n}
    except Exception as e:
        logger.exception("人气历史同步接收失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/hotrank/universe")
async def api_hotrank_universe(_user: str = Depends(require_auth)):
    """给家用脚本的扫描清单：自选 + 产业链龙头（我们真正在盯的票·6位代码）。"""
    try:
        from app.strategy import db, tech_chain
        codes = set(tech_chain._all_codes())
        for w in db.get_watchlist():
            c = str(w.get("ts_code") or "").split(".")[0]
            if c:
                codes.add(c)
        return {"ok": True, "codes": sorted(codes), "n": len(codes)}
    except Exception as e:
        logger.exception("人气扫描清单失败")
        return {"ok": False, "msg": str(e), "codes": []}


@app.get("/api/hot-reversal")
async def api_hot_reversal(require_tech: bool = True, days: int = 14, kind: str = "activity",
                           _user: str = Depends(require_auth)):
    """人气反转选股（曾活跃→洗盘→拐头回升·叠加关键位双确认）。kind=activity(服务器自算·默认)/rank(东财家用)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.hot_reversal import run_screen
        return await run_in_threadpool(run_screen, CompositeProvider(), kind, int(days),
                                       {"require_tech": bool(require_tech)})
    except Exception as e:
        logger.exception("人气反转选股失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/hotrank/activity/refresh")
async def api_activity_refresh(days: int = 1, _user: str = Depends(require_auth)):
    """算/回填「活跃度排名」到 hot_rank_log(kind='activity')。days=1 只记最新日·>1 回填(供cron/首次)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.activity_rank import backfill_activity
        return await run_in_threadpool(backfill_activity, CompositeProvider(), int(days))
    except Exception as e:
        logger.exception("活跃度回填失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/hotrank/detail-script")
async def api_hotrank_detail_script(_user: str = Depends(require_auth)):
    """下载家用【详情API同步脚本】：在家电脑跑·拉每票人气历史名次→推服务器(即时算峰值/谷值/回升)。"""
    from fastapi.responses import Response

    from app.strategy.hot_reversal import DETAIL_SYNC_SCRIPT
    return Response(content=DETAIL_SYNC_SCRIPT, media_type="text/x-python",
                    headers={"Content-Disposition": "attachment; filename=hotrank_detail_sync.py"})


@app.get("/api/resonance")
async def api_resonance(market: str = "震荡", _user: str = Depends(require_auth)):
    """共振确定性选股：选股池 × 4正交维度(板块/真钱/入局区间/基本面) → 共振分+位置分级(线程池)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.resonance import run_resonance
        return await run_in_threadpool(run_resonance, CompositeProvider(), None, market)
    except Exception as e:
        logger.exception("共振选股失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/cognition/snapshot")
async def api_cognition_snapshot(_user: str = Depends(require_auth)):
    """认知脚手架：5问框架 + 今日结构速览 + 今日已存推演(供续填)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy import cognition, db
        snap = await run_in_threadpool(cognition.daily_snapshot, CompositeProvider())
        saved = db.get_cognition(snap.get("as_of") or "")
        return {"ok": True, "five_q": cognition.FIVE_Q, "stances": cognition.STANCES,
                "snapshot": snap, "saved": saved}
    except Exception as e:
        logger.exception("认知速览失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/cognition/save")
async def api_cognition_save(request: Request, _user: str = Depends(require_auth)):
    """存/更一天的「5问」推演日志。Body: {trade_date, q1..q5, stance, main_line, confidence, sh_close}。"""
    try:
        from app.strategy import db
        b = await request.json()
        td = str(b.get("trade_date") or "").strip()
        if not td:
            return {"ok": False, "msg": "缺 trade_date"}
        entry = {k: b.get(k) for k in ("q1_regime", "q2_mainline", "q3_tempo", "q4_catalyst",
                                       "q5_path", "stance", "main_line", "confidence", "sh_close")}
        db.save_cognition(td, entry)
        return {"ok": True, "trade_date": td}
    except Exception as e:
        logger.exception("认知推演保存失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/cognition/history")
async def api_cognition_history(limit: int = 60, _user: str = Depends(require_auth)):
    """历史推演 + 客观校准（上证自记录日起涨跌·供你自评命中）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy import cognition, db
        rows = db.list_cognition(int(limit))
        rows = await run_in_threadpool(cognition.review_calibrate, CompositeProvider(), rows)
        return {"ok": True, "rows": rows}
    except Exception as e:
        logger.exception("认知历史失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/cognition/review")
async def api_cognition_review(request: Request, _user: str = Depends(require_auth)):
    """回看时补自评（哪问看对/看错）。Body: {trade_date, note}。"""
    try:
        from app.strategy import db
        b = await request.json()
        ok = db.update_cognition_review(str(b.get("trade_date") or ""), str(b.get("note") or ""))
        return {"ok": ok}
    except Exception as e:
        logger.exception("认知自评失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/news")
async def api_market_news(n: int = 50, _user: str = Depends(require_auth)):
    """7×24 快讯（财联社电报·降级东财·线程池·3分钟缓存）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.market_hub import news_flash
        return {"ok": True, "rows": await run_in_threadpool(news_flash, CompositeProvider(), int(n))}
    except Exception as e:
        logger.exception("7x24快讯失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/calendar")
async def api_market_calendar(_user: str = Depends(require_auth)):
    """财经日历（经济数据/事件·线程池·30分钟缓存）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.market_hub import econ_calendar
        return {"ok": True, "rows": await run_in_threadpool(econ_calendar, CompositeProvider())}
    except Exception as e:
        logger.exception("财经日历失败")
        return {"ok": False, "msg": str(e)}


def _to_float(v):
    """安全转 float：空/无效返回 None（交易计划价格/仓位用）。"""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@app.get("/api/plan/list")
async def api_plan_list(_user: str = Depends(require_auth)):
    """交易计划列表（新→旧）。"""
    try:
        from app.strategy.db import list_plans
        return {"ok": True, "rows": list_plans()}
    except Exception as e:
        logger.exception("交易计划列表失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/plan/add")
async def api_plan_add(request: Request, _user: str = Depends(require_auth)):
    """新增交易计划（用户最终决定）。Body: {code, side?, buy_price, stop_loss, take_profit?, position_pct?, note?}。"""
    try:
        from app.strategy.db import add_plan
        b = await request.json()
        ts_code = _resolve_ts_code(b.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入代码/名称）"}
        hid = add_plan(
            ts_code, name=_stock_name(ts_code), side=(b.get("side") or "buy"),
            buy_price=_to_float(b.get("buy_price")), stop_loss=_to_float(b.get("stop_loss")),
            take_profit=_to_float(b.get("take_profit")), position_pct=_to_float(b.get("position_pct")),
            note=(b.get("note") or ""))
        return {"ok": True, "id": hid}
    except Exception as e:
        logger.exception("新增交易计划失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/plan/update")
async def api_plan_update(request: Request, _user: str = Depends(require_auth)):
    """更新交易计划字段。Body: {id, ...fields}。"""
    try:
        from app.strategy.db import update_plan
        b = await request.json()
        pid = int(b.get("id"))
        fields = {k: b[k] for k in ("name", "side", "buy_price", "stop_loss", "take_profit", "position_pct", "note", "status") if k in b}
        for k in ("buy_price", "stop_loss", "take_profit", "position_pct"):
            if k in fields:
                fields[k] = _to_float(fields[k])
        return {"ok": update_plan(pid, **fields)}
    except Exception as e:
        logger.exception("更新交易计划失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/plan/remove")
async def api_plan_remove(request: Request, _user: str = Depends(require_auth)):
    """删除交易计划。Body: {id}。"""
    try:
        from app.strategy.db import remove_plan
        b = await request.json()
        return {"ok": remove_plan(int(b.get("id")))}
    except Exception as e:
        logger.exception("删除交易计划失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/plan/export")
async def api_plan_export(_user: str = Depends(require_auth)):
    """导出 pending 计划为 QMT 可读的 plan.json（下载）。"""
    import json as _json

    from fastapi.responses import Response

    from app.strategy.db import list_plans
    from app.strategy.trade_plan import to_qmt_plan
    body = _json.dumps(to_qmt_plan(list_plans()), ensure_ascii=False, indent=2)
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=plan.json"})


@app.get("/api/plan/qmt-script")
async def api_plan_qmt_script(_user: str = Depends(require_auth)):
    """下载配套 QMT 执行脚本（读 plan.json·集合竞价挂单+自动止损·不内置下单到网站）。"""
    from fastapi.responses import Response

    from app.strategy.trade_plan import QMT_SCRIPT
    return Response(content=QMT_SCRIPT, media_type="text/x-python",
                    headers={"Content-Disposition": "attachment; filename=qmt_executor.py"})


def _watch_owner(raw) -> str:
    """归属人入参校验：仅允许 me(用户1)/dad(用户2)，非法/缺省回落 'me'。"""
    return raw if raw in ("me", "dad") else "me"


@app.get("/api/portfolio/list")
async def api_portfolio_list(owner: str = "", _user: str = Depends(require_auth)):
    """秒级返回自选名单（纯 DB 读·不算现价/技术/资金）。供页面先渲染骨架，体检数据再异步填。

    owner 传 me/dad 只返回该人；留空返回全部(各行带 owner·供页面分「我的/爸爸的」两区)。
    """
    try:
        from app.strategy import db
        rows = [{
            "ts_code": w["ts_code"],
            "name": w.get("name") or w["ts_code"][:6],
            "owner": w.get("owner", "me"),
            "is_holding": bool(w.get("is_holding")),
            "cost": w.get("cost"),
            "stop_loss": w.get("stop_loss"),
            "target_price": w.get("target_price"),
            "note": w.get("note") or "",
        } for w in db.get_watchlist(owner=owner or None)]
        return {"ok": True, "rows": rows, "n": len(rows),
                "n_holding": sum(1 for r in rows if r["is_holding"])}
    except Exception as e:
        logger.exception("自选名单读取失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/portfolio")
async def api_portfolio(_user: str = Depends(require_auth)):
    """持仓体检 + 事件预警（现价/盈亏/技术/资金/事件/健康灯）。较重→线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.portfolio import build_portfolio
        return await run_in_threadpool(build_portfolio)
    except Exception as e:
        logger.exception("持仓体检失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/hold/decide_one")
async def api_hold_decide_one(code: str = "", _user: str = Depends(require_auth)):
    """对任意股票(不必持仓)现取数据走4问——处理"临时冲动想动手"时的冷静判断。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.hold_decision import decide_for_code
        ts = _resolve_ts_code(code)
        if not ts:
            return {"ok": False, "msg": "无法识别股票（输入6位代码/名称）"}
        name = _stock_name(ts) or ""
        return await run_in_threadpool(decide_for_code, ts, name)
    except Exception as e:
        logger.exception("临时决策失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/hold/decisions")
async def api_hold_decisions(_user: str = Depends(require_auth)):
    """拿得住·卖出决策器：对每只持仓按《持有手册》4问做数据接地的'卖不卖'判定。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.hold_decision import LEVEL_ORDER, decide
        from app.strategy.portfolio import build_portfolio
        data = await run_in_threadpool(build_portfolio)
        out = []
        for r in data.get("rows", []):
            if not r.get("is_holding"):
                continue
            out.append({"ts_code": r["ts_code"], "name": r["name"], "price": r.get("price"),
                        "pct_chg": r.get("pct_chg"), "industry": r.get("industry"),
                        "sector_phase": r.get("sector_phase"), **decide(r)})
        out.sort(key=lambda x: LEVEL_ORDER.get(x["level"], 9))   # 最该处理的(止损/警惕)排最前
        return {"ok": True, "date": data.get("date"), "decisions": out, "n_holding": len(out),
                "disclaimer": "纪律框架辅助提高决策质量，非涨跌预测、不构成投资建议；卖出判定刻意不看成本价。"}
    except Exception as e:
        logger.exception("卖出决策失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/watch/quotes")
async def api_watch_quotes(_user: str = Depends(require_auth)):
    """轻量实时报价：只取自选/持仓的现价+涨跌（1次新浪批量）。供盯盘高频刷新，不重算技术/板块。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy import db
        codes = [w["ts_code"] for w in db.get_watchlist()]
        if not codes:
            return {"ok": True, "quotes": {}}

        def _q() -> dict:
            out: dict[str, dict] = {}
            try:
                qdf = CompositeProvider().get_realtime_quote(codes)
            except Exception:
                return out
            if qdf is None or qdf.empty:
                return out
            for r in qdf.to_dict("records"):
                try:
                    out[str(r.get("ts_code"))] = {"price": round(float(r.get("price")), 2),
                                                  "pct_chg": round(float(r.get("pct_chg")), 2)}
                except (TypeError, ValueError):
                    continue
            return out

        return {"ok": True, "quotes": await run_in_threadpool(_q)}
    except Exception as e:
        logger.exception("盯盘报价失败")
        return {"ok": False, "msg": str(e)}


@app.get("/realtime", response_class=HTMLResponse)
async def realtime_page(request: Request, _user: str = Depends(require_auth)):
    """实时盯盘看板（全推L1·秒级刷新·资金流向/板块/急拉/持仓体检）。"""
    resp = templates.TemplateResponse(request=request, name="realtime.html",
                                      context={"page": "realtime"})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"   # 实时看板不缓存(改版即生效·免硬刷)
    return resp


@app.get("/api/realtime/board")
async def api_realtime_board(_user: str = Depends(require_auth)):
    """实时看板数据（读进程内全推快照·线程池避免阻塞事件循环）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.realtime_hub import build_board_cached, status
        board = await run_in_threadpool(build_board_cached)   # TTL缓存·支撑前端2s轮询不加压
        board["status"] = status()
        return board
    except Exception as e:
        logger.exception("实时看板失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/realtime/raw")
async def api_realtime_raw(codes: str = "", _user: str = Depends(require_auth)):
    """调试：返回指定代码的原始全推快照字段（last/open/high/low/bid_px/ask_px/vol/amount…）。

    用于核验生产端各时段(尤其集合竞价)到底填了哪些字段。codes=逗号分隔的6位代码。
    """
    from app.strategy.realtime_hub import snapshot
    snap = snapshot()
    out = {}
    for c in (codes or "").split(","):
        c = c.strip()
        if not c:
            continue
        ts = _resolve_ts_code(c) or c
        out[ts] = snap.get(ts)
    return {"ok": True, "data": out}


@app.post("/api/realtime/scan")
async def api_realtime_scan(force: bool = False, _user: str = Depends(require_auth)):
    """手动触发一次盯盘扫描推送（force=true 可休市用测试端点演示）。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.realtime_scan import scan_once
        new = await run_in_threadpool(scan_once, force, True)
        return {"ok": True, "pushed": new, "n": len(new)}
    except Exception as e:
        logger.exception("盯盘扫描失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/portfolio/signals")
async def api_portfolio_signals(refresh: bool = False, _user: str = Depends(require_auth)):
    """🔔 自选股今日信号：它"最吃的信号"今天是否触发→买/卖点提醒(确定性历史统计)。较重→线程池·按日缓存。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.signal_watch import scan_signals
        return await run_in_threadpool(scan_signals, None, bool(refresh))
    except Exception as e:
        logger.exception("自选信号扫描失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/portfolio/add")
async def api_portfolio_add(request: Request, _user: str = Depends(require_auth)):
    """加入自选/持仓。Body: {code, is_holding?, cost?, shares?, stop_loss?, note?}。"""
    try:
        from app.strategy import db
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票（请输入6位代码/完整代码/名称）"}
        f = lambda k: (float(body[k]) if body.get(k) not in (None, "") else None)
        db.add_watch(ts_code, _stock_name(ts_code), is_holding=bool(body.get("is_holding")),
                     cost=f("cost"), shares=f("shares"), stop_loss=f("stop_loss"),
                     target_price=f("target_price"), note=str(body.get("note") or "")[:120],
                     owner=_watch_owner(body.get("owner")))
        return {"ok": True, "ts_code": ts_code, "name": _stock_name(ts_code)}
    except Exception as e:
        logger.exception("加入自选失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/portfolio/update")
async def api_portfolio_update(request: Request, _user: str = Depends(require_auth)):
    """更新自选/持仓字段。Body: {code, is_holding?/cost?/shares?/stop_loss?/note?}。"""
    try:
        from app.strategy import db
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        fields = {k: body[k] for k in ("is_holding", "cost", "shares", "stop_loss", "target_price", "note") if k in body}
        for k in ("cost", "shares", "stop_loss", "target_price"):
            if k in fields:
                fields[k] = float(fields[k]) if fields[k] not in (None, "") else None
        return {"ok": db.update_watch(ts_code, owner=_watch_owner(body.get("owner")), **fields)}
    except Exception as e:
        logger.exception("更新自选失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/portfolio/remove")
async def api_portfolio_remove(request: Request, _user: str = Depends(require_auth)):
    """移除自选/持仓。Body: {code}。"""
    try:
        from app.strategy import db
        body = await request.json()
        ts_code = _resolve_ts_code(body.get("code", ""))
        return {"ok": db.remove_watch(ts_code, owner=_watch_owner(body.get("owner"))) if ts_code else False}
    except Exception as e:
        logger.exception("移除自选失败")
        return {"ok": False, "msg": str(e)}


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
        # 资金三角增强：复用行内已算 main_flow_3d，按日批量算（top_inst/north 按日缓存·线程池避免阻塞）
        try:
            from fastapi.concurrency import run_in_threadpool
            from app.strategy.fund_triangle import build_fund_triangle
            from app.data.composite_provider import CompositeProvider
            main_flow_map = {r["ts_code"]: (r.get("main_flow_3d") or 0.0) for r in rows}
            tri = await run_in_threadpool(build_fund_triangle, CompositeProvider(), d, main_flow_map)
            for r in rows:
                t = tri.get(r["ts_code"])
                if t:
                    r["fund_triangle"] = t.to_dict()
        except Exception:
            logger.exception("资金三角增强失败（降级·不阻断选股池）")
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


@app.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request, _user: str = Depends(require_auth)):
    """产业认知教练：数据接地的认知卡片 + 练习反馈 + 自由探讨。"""
    return templates.TemplateResponse(request=request, name="industry_insight.html", context={"page": "insight"})


@app.get("/hotpicks")
@app.get("/resonance")
async def _legacy_select_redirect(_user: str = Depends(require_auth)):
    """人气反转/共振已深度并入因子选股(因子组+确定性列)·旧链接重定向到因子选股。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/screener")


@app.get("/cognition", response_class=HTMLResponse)
async def cognition_page(request: Request, _user: str = Depends(require_auth)):
    """🧠 认知脚手架：每日「5问框架」结构复盘 + 推演记录 + 事后校准（练框架·非抄结论）。"""
    return templates.TemplateResponse(request=request, name="cognition.html",
                                      context={"page": "cognition"})


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


@app.get("/api/insight/card")
async def api_insight_card(theme: str = "", force: bool = False, _user: str = Depends(require_auth)):
    """产业认知卡片(数据接地·按周缓存)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.industry_insight import build_insight_card
        if not theme.strip():
            return {"ok": False, "msg": "请输入行业/产业主题"}
        return await run_in_threadpool(build_insight_card, theme.strip(), bool(force), None)
    except Exception as e:
        logger.exception("产业认知卡片失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/insight/quiz")
async def api_insight_quiz(theme: str = "", _user: str = Depends(require_auth)):
    """据卡片出思考题(主动回忆)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.industry_insight import build_insight_card, gen_quiz
        card = (await run_in_threadpool(build_insight_card, theme.strip(), False, None)).get("card", "")
        if not card:
            return {"ok": False, "questions": []}
        return await run_in_threadpool(gen_quiz, theme.strip(), card, None)
    except Exception as e:
        logger.exception("出题失败")
        return {"ok": False, "msg": str(e), "questions": []}


@app.post("/api/insight/grade")
async def api_insight_grade(request: Request, _user: str = Depends(require_auth)):
    """批改学习者答案 + 反馈。Body: {theme, question, answer}。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.industry_insight import build_insight_card, grade_answer
        b = await request.json()
        card = (await run_in_threadpool(build_insight_card, b.get("theme", "").strip(), False, None)).get("card", "")
        return await run_in_threadpool(grade_answer, b.get("theme", ""), card,
                                       b.get("question", ""), b.get("answer", ""), None)
    except Exception as e:
        logger.exception("批改失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/insight/discuss")
async def api_insight_discuss(request: Request, _user: str = Depends(require_auth)):
    """围绕产业自由探讨。Body: {theme, history:[{role,content}], msg}。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.industry_insight import build_insight_card, discuss
        b = await request.json()
        card = (await run_in_threadpool(build_insight_card, b.get("theme", "").strip(), False, None)).get("card", "")
        return await run_in_threadpool(discuss, b.get("theme", ""), card,
                                       b.get("history", []), b.get("msg", ""), None)
    except Exception as e:
        logger.exception("探讨失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/market/radar")
async def api_market_radar(_user: str = Depends(require_auth)):
    """全市场盘中异动雷达：热点板块/涨跌幅榜/涨停/涨跌家数(秒读缓存·后台扫描)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.market_radar import get_market_radar
        return await run_in_threadpool(get_market_radar, None)
    except Exception as e:
        logger.exception("市场雷达失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/sector/strength")
async def api_sector_strength(date: str = "", _user: str = Depends(require_auth)):
    """板块强弱总览：各行业 RPS/近5日/站MA60占比/资金/龙头 + 形态判定。读因子表(缓存)·线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.sector_strength import build_sector_strength
        d = (date or "").replace("-", "") or _last_trade_date()
        return await run_in_threadpool(build_sector_strength, d, None)
    except Exception as e:
        logger.exception("板块强弱总览失败")
        return {"ok": False, "msg": str(e)}


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


@app.get("/api/stock/company")
async def api_stock_company(code: str = "", _user: str = Depends(require_auth)):
    """公司画像：主营业务/主营构成(Tushare硬数据) + 行业地位/全球排名/护城河(LLM归纳·带来源)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.company_profile import build_company_profile
        ts = _resolve_ts_code(code)
        if not ts:
            return {"ok": False, "msg": "无法识别股票"}
        name = _stock_name(ts) or ""
        return await run_in_threadpool(build_company_profile, ts, name)
    except Exception as e:
        logger.exception("公司画像失败")
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


@app.get("/api/stock/regulatory")
async def api_stock_regulatory(code: str = "", _user: str = Depends(require_auth)):
    """监管/停牌风险：停牌状态(事实) + 连板异动核查风险(派生) + 监管函/问询函新闻(博查·带来源)。"""
    try:
        from fastapi.concurrency import run_in_threadpool
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        return await run_in_threadpool(_build_regulatory, ts_code, _stock_name(ts_code) or "")
    except Exception as e:
        logger.exception("监管风险查询失败")
        return {"ok": False, "msg": str(e)}


def _build_regulatory(ts_code: str, name: str) -> dict:
    from app.data.composite_provider import CompositeProvider
    from app.strategy.realtime_hub import tech_map
    from app.strategy.reg_risk import anomaly_risk, reg_news, suspended_codes
    provider = CompositeProvider()
    consec = (tech_map().get(ts_code) or {}).get("consec_limit_now")
    return {"ok": True, "name": name,
            "suspended": ts_code in suspended_codes(provider),
            "anomaly": anomaly_risk(consec, is_st="ST" in name.upper()),
            "news": reg_news(ts_code, name)}


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


@app.get("/api/sector/heat")
async def api_sector_heat(name: str = "", type: str = "industry", date: str = "",
                         _user: str = Depends(require_auth)):
    """单个板块(行业/概念)的热度+资金概览（读宽表 theme_heat_all_in_one），供展开行显示板块温度。"""
    if not name:
        return {"ok": False, "msg": "缺少 name 参数"}
    try:
        from app.data.theme_heat_db import get_theme, latest_trade_date
        d = (date or "").replace("-", "")
        row = get_theme(d, name, type) if d else None
        if not row:                                   # 该日无→退到最近有宽表的交易日
            ld = latest_trade_date(type)
            row = get_theme(ld, name, type) if ld else None
        if not row:
            return {"ok": False, "msg": f"板块「{name}」无宽表数据"}
        delta = row.get("heat_score_delta_3d") or 0.0
        return {
            "ok": True, "name": name, "type": type,
            "heat": round(row.get("heat_score") or 0.0, 1),
            "delta": round(delta, 1), "rising": delta > 0,
            "money_flow_3d": row.get("money_flow_3d"),
            "pct_chg_3d": row.get("pct_chg_3d"),
            "breadth_ma20": row.get("breadth_ma20"),
            "phase": row.get("phase"), "tier": row.get("tier"),
            "trade_date": row.get("trade_date"),
        }
    except Exception as e:
        logger.exception("板块热度查询失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/research")
async def api_stock_research(code: str = "", _user: str = Depends(require_auth)):
    """个股研报(免费)：东财(评级/盈预/PDF) + 同花顺一致预期(机构数/EPS区间/行业平均)。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.fundamentals import get_em_research, get_ths_forecast
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}
        res = await run_in_threadpool(get_em_research, ts_code)
        res["ths"] = await run_in_threadpool(get_ths_forecast, ts_code)   # 同花顺一致预期(best-effort)
        return res
    except Exception as e:
        logger.exception("个股研报失败")
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


@app.get("/api/stock/fund")
async def api_stock_fund(code: str = "", _user: str = Depends(require_auth)):
    """个股资金三角：主力估算 + 龙虎榜机构真钱 + 大盘北向背景；附行业(供板块热度查询)。线程池。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.data.composite_provider import CompositeProvider
        from app.strategy.fund_triangle import build_fund_triangle
        from app.strategy.signals import _main_flow_3d
        ts_code = _resolve_ts_code(code)
        if not ts_code:
            return {"ok": False, "msg": "无法识别股票"}

        def _gather() -> dict:
            provider = CompositeProvider()
            d = _last_trade_date()
            mf = _main_flow_3d(provider, d)                       # 全市场主力近3日(缓存)
            tri = build_fund_triangle(provider, d, mf, ts_codes=[ts_code])
            t = tri.get(ts_code)
            name, industry, industry_l1 = "", "", ""
            try:
                sb = provider.get_stock_basic()
                hit = sb[sb["ts_code"] == ts_code]
                if not hit.empty:
                    r = hit.iloc[0]
                    name = str(r.get("name", ""))
                    industry = str(r.get("industry", ""))
                    industry_l1 = str(r.get("industry_l1", "")) if "industry_l1" in sb.columns else ""
            except Exception:
                pass
            return {"ok": True, "ts_code": ts_code, "name": name, "industry": industry,
                    "industry_l1": industry_l1, "date": d,
                    "fund_triangle": t.to_dict() if t else None}

        return await run_in_threadpool(_gather)
    except Exception as e:
        logger.exception("个股资金三角失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/stock/verdict")
async def api_stock_verdict(request: Request, _user: str = Depends(require_auth)):
    """个股360 综合买入判断：吃各区已查真数据 → LLM 给结论/评分/三档。Body: {name, code, sections}。"""
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.strategy.stock_verdict import build_verdict
        body = await request.json()
        sections = body.get("sections") or {}
        if not sections:
            return {"ok": False, "msg": "无可用数据"}
        name = body.get("name") or ""
        code = body.get("code") or ""
        return await run_in_threadpool(build_verdict, name, code, sections)
    except Exception as e:
        logger.exception("个股综合判断失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/stock/360/save")
async def api_stock360_save(request: Request, _user: str = Depends(require_auth)):
    """保存一次个股360 快照（各区数据 + AI 判断）到历史，可回看。Body: {snapshot}。"""
    try:
        from app.backtest.history import record_stock360
        body = await request.json()
        snap = body.get("snapshot") or {}
        if not snap.get("code"):
            return {"ok": False, "msg": "快照缺少股票代码"}
        hid = record_stock360(_user, snap, name=snap.get("name") or "")
        return {"ok": bool(hid), "id": hid}
    except Exception as e:
        logger.exception("个股360 保存失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/360/history")
async def api_stock360_history(_user: str = Depends(require_auth)):
    """个股360 历史列表（仅本人·时间倒序·仅 stock360 类型）。"""
    try:
        from app.backtest.history import list_records
        rows = list_records(creator=_user, limit=100, kinds=("stock360",))
        return {"ok": True, "rows": rows}
    except Exception as e:
        logger.exception("个股360 历史列表失败")
        return {"ok": False, "msg": str(e)}


@app.get("/api/stock/360/get")
async def api_stock360_get(id: int, _user: str = Depends(require_auth)):
    """取单条个股360 快照完整数据（供回看还原）。"""
    try:
        from app.backtest.history import get_record
        rec = get_record(int(id), creator=_user)
        if not rec:
            return {"ok": False, "msg": "记录不存在"}
        return {"ok": True, "snapshot": rec.get("result") or {}, "created_at": rec.get("created_at")}
    except Exception as e:
        logger.exception("个股360 取回失败")
        return {"ok": False, "msg": str(e)}


@app.post("/api/stock/360/delete")
async def api_stock360_delete(request: Request, _user: str = Depends(require_auth)):
    """删除一条个股360 历史（仅本人）。Body: {id}。"""
    try:
        from app.backtest.history import delete
        body = await request.json()
        ok = delete(int(body.get("id")), creator=_user)
        return {"ok": ok}
    except Exception as e:
        logger.exception("个股360 删除失败")
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
        name = _stock_name(ts_code)
        result = await run_in_threadpool(
            scout_strategies, ts_code, start, end,
            name=name, min_sample=int(body.get("min_sample") or 4))
        result.setdefault("ts_code", ts_code)
        # 自动落历史（与单信号回测同等待遇，可在回测历史里回看/导出）
        if result.get("ok") and result.get("ranked"):
            try:
                from app.backtest.history import record_scout
                hid = record_scout(_user, result, name=name)
                if hid:
                    result["history_id"] = hid
            except Exception:
                logger.exception("scout 历史记录写入失败（忽略）")
        return result
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
    """个股回测历史列表（仅本人，时间倒序）。q 模糊搜索票/信号。仅回测/scout，不含个股360。"""
    try:
        from app.backtest.history import list_records
        rows = list_records(creator=_user, q=q.strip(), limit=limit, kinds=("backtest", "scout"))
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
