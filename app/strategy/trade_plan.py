"""交易计划：把用户最终决定的下单计划，转换成 QMT 可读的 plan.json + 配套执行脚本。

红线：本模块**只产出数据/脚本文本，绝不下单**。下单由用户在自己机器上跑 QMT 脚本完成，
研究层（网站）与执行层（QMT）严格隔离，避免任何 bug 直接动真金白银。
"""

from __future__ import annotations


def to_qmt_plan(plans: list[dict]) -> list[dict]:
    """把交易计划行（DB）转成 QMT 脚本读取的最小格式（纯函数·只取 pending）。

    QMT 脚本只认：code/name/action/buy_high/stop_loss/position_pct。
    """
    out = []
    for p in plans:
        if (p.get("status") or "pending") != "pending":
            continue
        if not p.get("ts_code"):
            continue
        out.append({
            "code": p["ts_code"],
            "name": p.get("name") or "",
            "action": p.get("side") or "buy",
            "buy_high": p.get("buy_price"),       # 限价上限·QMT 不追高于此
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "position_pct": p.get("position_pct"),
        })
    return out


# QMT(miniQMT/xtquant) 执行脚本模板——用户下载到本机、填好账号即可跑（先模拟盘！）
QMT_SCRIPT = r'''# qmt_executor.py —— 读取网站导出的 plan.json，9:15集合竞价挂单 + 盘中自动止损
# 运行前提：打开国金 QMT 客户端、开通 miniQMT；务必先用【模拟盘账号】跑通再上真钱！
import json, time, datetime
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant, xtdata

# ===== 配置（改成你自己的）=====
QMT_PATH   = r"D:\国金QMT\userdata_mini"   # QMT 安装目录下的 userdata_mini
ACCOUNT_ID = "你的资金账号"
TOTAL_CASH = 100000          # 可用资金（按仓位算手数用）
PLAN_FILE  = r"plan.json"    # 从网站「交易计划」页导出的文件
# ==============================

class Cb(XtQuantTraderCallback):
    def on_stock_trade(self, t): print(f"[成交] {t.stock_code} {t.traded_volume}股 @{t.traded_price}")
    def on_order_error(self, e): print(f"[废单] {e.error_msg}")

def connect():
    tr = XtQuantTrader(QMT_PATH, int(time.time()))
    tr.start(); assert tr.connect() == 0, "连不上QMT，确认客户端已打开"
    acc = StockAccount(ACCOUNT_ID); tr.register_callback(Cb()); tr.subscribe(acc)
    return tr, acc

def vol_of(price, pct):                       # 按仓位算手数（向下取整到100股）
    return max(int(TOTAL_CASH * (pct or 0) / price // 100 * 100), 0) if price else 0

def place_orders(tr, acc, plan):              # ① 9:15 集合竞价挂限价单
    for p in plan:
        if p["action"] != "buy" or not p.get("buy_high"): continue
        bid = float(p["buy_high"])            # 纪律：只挂到买点上限·绝不追高一字板
        vol = vol_of(bid, p.get("position_pct"))
        if vol < 100: print(f"跳过 {p['code']}: 不足一手"); continue
        tr.order_stock(acc, p["code"], xtconstant.STOCK_BUY, vol,
                       xtconstant.FIX_PRICE, bid, "plan", p.get("name",""))
        print(f"[挂单] {p.get('name')} {vol}股 @{bid}")

def monitor(tr, acc, plan):                   # ② 盘中自动止损
    sl = {p["code"]: p["stop_loss"] for p in plan if p.get("stop_loss")}
    while datetime.datetime.now().strftime("%H%M") < "1457":
        for pos in tr.query_stock_positions(acc):
            s = sl.get(pos.stock_code)
            if not s or pos.can_use_volume <= 0: continue
            px = xtdata.get_full_tick([pos.stock_code])[pos.stock_code]["lastPrice"]
            if px <= float(s):
                tr.order_stock(acc, pos.stock_code, xtconstant.STOCK_SELL, pos.can_use_volume,
                               xtconstant.MARKET_PEER_PRICE_FIRST, 0, "sl", "破位止损")
                print(f"[自动止损] {pos.stock_code} @{px} 跌破{s}")
        time.sleep(3)

if __name__ == "__main__":
    plan = json.load(open(PLAN_FILE, encoding="utf-8"))
    tr, acc = connect()
    if datetime.datetime.now().strftime("%H%M") <= "0920":
        place_orders(tr, acc, plan)
    monitor(tr, acc, plan)
    tr.run_forever()
# 注意：xtconstant 常量名/order_stock 参数顺序，不同 QMT 版本略有差异，以国金 miniQMT 文档为准。
'''
