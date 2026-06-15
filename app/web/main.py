"""
A股Agent Web UI（FastAPI）。

路由：
  /                   报告列表首页
  /report/{date}      查看指定日期报告
  /strategy           策略验证中心（回测 + 前向追踪）
  /tracking           持仓追踪（旧版，保留兼容）
  /api/strategy       策略分析 JSON API
  /api/tracking       持仓追踪 JSON API

启动：.venv/bin/python -m app.run web
"""

import logging
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings

logger = logging.getLogger(__name__)

app = FastAPI(title="A股选股日报", docs_url=None, redoc_url=None)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ──────────────────────────────────────────────
# 页面路由
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """报告列表首页。"""
    settings = get_settings()
    reports = sorted(
        Path(settings.report_dir).glob("*.md"),
        key=lambda f: f.stem,
        reverse=True,
    )
    report_list = [
        {
            "date": f.stem,
            "display": f"{f.stem[:4]}-{f.stem[4:6]}-{f.stem[6:]}",
            "size_kb": round(f.stat().st_size / 1024, 1),
        }
        for f in reports
    ]
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"reports": report_list},
    )


@app.get("/report/{date}", response_class=HTMLResponse)
async def view_report(request: Request, date: str):
    """查看指定日期报告（Markdown → HTML）。"""
    settings = get_settings()
    md_path = Path(settings.report_dir) / f"{date}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"报告 {date} 不存在")

    html_body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "nl2br"],
    )
    return templates.TemplateResponse(
        request=request, name="report.html",
        context={"date": date, "html_body": html_body},
    )


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    """策略验证中心页面。"""
    return templates.TemplateResponse(
        request=request, name="strategy.html", context={},
    )


@app.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request):
    """前向追踪页面（展示实盘候选股实际表现）。"""
    return templates.TemplateResponse(
        request=request, name="tracking.html", context={"active": []},
    )


# ──────────────────────────────────────────────
# JSON API
# ──────────────────────────────────────────────

@app.get("/api/strategy")
async def api_strategy(
    is_backtest: str = "",
    start_date: str = "",
    end_date: str = "",
):
    """
    策略分析 JSON API。

    Query params:
      is_backtest: 1=回测 / 0=前向 / 空=全部
      start_date / end_date: YYYYMMDD 筛选区间
    """
    try:
        from app.strategy.analyzer import full_analysis

        bt = int(is_backtest) if is_backtest in ("0", "1") else None
        result = full_analysis(
            is_backtest=bt,
            start_date=start_date or None,
            end_date=end_date or None,
            min_samples=5,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        logger.exception("strategy API 出错")
        return {"ok": False, "error": str(e)}


@app.get("/api/tracking")
async def api_tracking():
    """前向追踪 JSON API — 返回实盘候选股各时间窗口真实表现。"""
    try:
        from app.strategy.db import get_all_with_performance
        records = get_all_with_performance(is_backtest=0)
        return {"ok": True, "data": records}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/win_rates")
async def api_win_rates():
    """旧接口兼容 — 按主题汇总 T+1 胜率。"""
    try:
        from app.strategy.db import get_all_with_performance
        import pandas as pd
        records = get_all_with_performance(is_backtest=0)
        if not records:
            return {"ok": True, "data": {}}
        df = pd.DataFrame(records)
        sub = df[df["t1_return"].notna() & df["theme"].notna()]
        result = {}
        for theme, grp in sub.groupby("theme"):
            if len(grp) >= 2:
                result[str(theme)] = {
                    "win_rate": round(float(grp["t1_win"].mean()), 2),
                    "samples": int(len(grp)),
                    "avg_pct": round(float(grp["t1_return"].mean()), 2),
                }
        return {"ok": True, "data": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────
# 启动入口
# ──────────────────────────────────────────────

def start_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    logger.info("Web UI 启动: http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    start_server()
