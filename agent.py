# -*- coding: utf-8 -*-
"""盘口损耗研判智能体 · 命令行检定入口（四通道）。

用法（新四通道 + 原有三条命令全部兼容）
  python agent.py web [--port 8001] [--dual]            # 本地网页（原功能不变）
  python agent.py feargreed [--limit 7] [--history]     # 恐惧贪婪指数（原功能不变）
  python agent.py slippage --symbol BTCUSDT --side buy --amount 10000   # 原命令（=默认通道）
  python agent.py BTCUSDT --side buy --amount 10000     # 检定证书：官方盘口快照（自动选路）
  python agent.py BTCUSDT --live                        # 强制刷新：重新选路
  python agent.py BTCUSDT --skill                       # 官方 CLI（binance-cli request）直取
  python agent.py BTCUSDT --official                    # 官方归档：T+1 逐笔成交流重建冲击阶梯
  python agent.py "看看 BTCUSDT 买一万 U 的滑点"          # 自然语言：自动选通道

说明
  · 判定引擎与网页是同一段代码（sources.py 的 calculate_slippage / _walk_book，本文件不改引擎一行：
    四条通道各自取数后，直接把盘口数据喂给引擎的纯函数计算）。
  · 官方没有盘口快照的历史归档（盘口是瞬时的）——`--official` 用锚定日全部逐笔成交重建价格阶梯，
    口径如实标注「基于真实成交流的价格分布重建，不是当时的挂单盘口」。
  · 恐惧贪婪指数来自 alternative.me（第三方公开接口，非币安数据），报告里逐处区分来源。
  · 不构成投资建议。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import sources

# ---------------------------------------------------------------- 常量与标识
CHANNEL_LABELS = {
    "route": "官方公开行情入口盘口快照（先直连、连不上自动换本机上网工具的通道，与网页同口径）",
    "fresh": "官方公开行情入口盘口快照（强制刷新：重新选一遍线路）",
    "cli": "官方 CLI（binance-cli request）直取同一盘口端点",
    "archive": "币安官方开源数据仓库（锚定日全部逐笔成交，重建价格阶梯，T+1）",
}
DATA_API = "https://data-api.binance.vision"
ARCHIVE_BASE = "https://data.binance.vision"
UA = {"User-Agent": "huadian-sunhao-agent/1.0", "Accept": "application/json"}
VPN_PORTS = (7897, 7890, 10809, 2080, 1080, 8888)
DEFAULT_AMOUNTS = [1000, 5000, 10000, 30000]
RATINGS = {"低": "检定通过", "中": "关注", "高": "超差", "极高": "严重超差"}

KNOWN_BASES = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
               "DOT", "LTC", "TRX", "TON", "PEPE", "SHIB", "WIF", "OP", "ARB", "NEAR"]


def say(msg=""):
    """进度与说明一律走 stderr，保证 --json 模式 stdout 只有纯 JSON。"""
    sys.stderr.write(str(msg) + "\n")


# ------------------------------------------------------------ 自然语言入口
def channel_from_intent(text):
    """自然语言 → 通道。识别不出返回 None（静默走默认），绝不乱猜。"""
    t = text.lower()
    if any(k in t for k in ["官方技能", "官方 cli", "官方cli", "binance-cli", "skill"]):
        return "skill"
    if any(k in t for k in ["归档", "官方开源", "历史", "昨天", "成交流", "t+1"]):
        return "official"
    if any(k in t for k in ["强制刷新", "重新选路", "换网络", "网络不行"]):
        return "live"
    return None


def symbol_from_intent(text):
    """从一句话里找交易对：先找明写的 XXXUSDT，再认常见币种名。找不到返回 None。"""
    m = re.search(r"([A-Z0-9]{2,12}USDT)", text.upper())
    if m:
        return m.group(1)
    up = text.upper()
    for base in KNOWN_BASES:
        if re.search(rf"\b{base}\b", up):
            return base + "USDT"
    return None


def side_from_intent(text):
    t = text.lower()
    if any(k in t for k in ["卖出", "卖", "sell", "抛"]):
        return "sell"
    if any(k in t for k in ["买入", "买", "buy"]):
        return "buy"
    return None


def amount_from_intent(text):
    """从一句话里抓金额：「一万」「3万」「30000 U」→ 数字（USDT）。找不到返回 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", text)
    if m:
        return float(m.group(1)) * 10000
    m = re.search(r"([\d]{3,}(?:\.\d+)?)\s*(?:u|usdt|美元)?", text, re.I)
    if m:
        return float(m.group(1))
    return None


# ------------------------------------------------------------ 官方盘口快照（自动选路）
_ROUTE = {"opener": None, "desc": "", "proxy_url": None}


class EndpointGone(Exception):
    """线路是通的，但官方明确回了 4xx（交易对不存在 / 端点变更）—— 不该继续换线路、也不该报「网络不通」。"""


def _make_opener(route):
    """route='direct' → 绕过系统代理直连；否则走 127.0.0.1:<port> 本机上网工具端口。"""
    if route == "direct":
        return build_opener(ProxyHandler({}))
    return build_opener(ProxyHandler({"http": f"http://{route}", "https": f"http://{route}"}))


def _parse_depth(data, symbol):
    """把官方盘口响应整理成与引擎 fetch_binance_depth 相同的形状。"""
    bids = [[float(p), float(q)] for p, q in (data.get("bids") or [])
            if float(p) > 0 and float(q) > 0]
    asks = [[float(p), float(q)] for p, q in (data.get("asks") or [])
            if float(p) > 0 and float(q) > 0]
    if not bids or not asks:
        raise RuntimeError("盘口深度为空（交易对不存在或已下线）")
    return {"symbol": symbol, "last_update_id": data.get("lastUpdateId"),
            "bids": bids, "asks": asks}


def depth_get(symbol, limit=100, timeout=8.0, force_refresh=False):
    """官方公开行情入口盘口快照（自动选路：直连优先→本机上网工具端口）。

    返回 (depth, 线路描述)。连接类失败换线路；官方明确回 4xx 抛 EndpointGone。
    """
    import urllib.error
    path = f"/api/v3/depth?symbol={quote(symbol)}&limit={int(limit)}"
    if force_refresh:
        _ROUTE.update(opener=None, desc="", proxy_url=None)

    def _call(opener, desc):
        req = Request(DATA_API + path, headers=UA)
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise EndpointGone(f"官方返回 HTTP {exc.code}（{path[:70]}）："
                               f"交易对可能不存在或端点已变更")
        return _parse_depth(data, symbol), desc

    if _ROUTE["opener"] is not None:
        return _call(_ROUTE["opener"], _ROUTE["desc"])
    routes = [("direct", "直连")] + [(f"127.0.0.1:{p}", f"本机上网工具端口 {p}") for p in VPN_PORTS]
    last_err = None
    for route, desc in routes:
        try:
            depth, desc_used = _call(_make_opener(route), desc)
            _ROUTE.update(opener=_make_opener(route), desc=desc,
                          proxy_url=(None if route == "direct" else f"http://{route}"))
            return depth, desc_used
        except EndpointGone:
            raise  # 端点/参数问题：换线路没有用
        except Exception as exc:  # 连接类失败：换下一条线路
            last_err = exc
            continue
    raise RuntimeError(
        f"官方公开行情入口全线路不可达（直连与 6 个本机上网工具端口都试过；最后错误：{last_err}）")


def cli_depth_get(symbol, limit=100, timeout=60):
    """官方 CLI 通道：binance-cli request GET 同一盘口端点。

    官方 CLI 不读系统的上网工具设置 → 先借默认通道探一次线路，把可用代理经
    HTTP_PROXY / HTTPS_PROXY 环境变量告诉它。
    """
    exe = os.environ.get("BINANCE_CLI_PATH") or shutil.which("binance-cli")
    if not exe:
        raise RuntimeError("未找到官方 CLI（binance-cli）。安装见官方文档，或设置环境变量 BINANCE_CLI_PATH")
    proxy_url = None
    try:
        depth_get(symbol, limit=20, timeout=6)  # 借选路探一次
        proxy_url = _ROUTE.get("proxy_url")
    except Exception:
        pass
    env = dict(os.environ)
    if proxy_url:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy_url
    url = f"{DATA_API}/api/v3/depth?symbol={quote(symbol)}&limit={int(limit)}"
    p = subprocess.run([exe, "request", "GET", url], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"官方 CLI 退出码 {p.returncode}（{(p.stderr or '').strip()[:120]}）")
    return _parse_depth(json.loads(p.stdout), symbol)


# ------------------------------------------------------------ 官方归档通道（成交流重建阶梯）
def _head_exists(url):
    try:
        opener = build_opener(ProxyHandler({}))
        opener.open(Request(url, headers=UA, method="HEAD"), timeout=15).close()
        return True
    except Exception:
        return False


def _fetch_zip_bytes(url):
    import urllib.request
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers=UA), timeout=120) as resp:
        return resp.read()


def _iter_agg_trades(raw_bytes):
    """流式逐行解析官方 aggTrades 归档（带表头，列：agg_trade_id,price,quantity,
    first_trade_id,last_trade_id,transact_time,is_buyer_maker）。"""
    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    name = zf.namelist()[0]
    with zf.open(name) as fh:
        for raw_line in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
            cells = raw_line.rstrip("\n").split(",")
            if len(cells) < 6:
                continue
            try:
                price = float(cells[1])
                qty = float(cells[2])
            except ValueError:
                continue  # 表头行
            if price > 0 and qty > 0:
                yield price, qty


def collect_official(symbol, side, quote_amounts, max_bytes=120 * 1024 * 1024):
    """官方归档通道：官方没有盘口快照的历史归档（盘口是瞬时的），
    所以用锚定日**全部逐笔成交**重建价格阶梯（每档约 0.01%），再喂给引擎的纯函数做吃单模拟。

    口径（必须如实）：这是「基于真实成交流的价格分布重建」，不是当时的挂单盘口；
    每档的量级是**当日累计成交量**，反映成交流深度，不等同瞬时挂单深度。
    """
    notes = []
    today = datetime.now(timezone.utc).date()
    anchor = None
    for back in range(1, 8):
        d = today - timedelta(days=back)
        url = (f"{ARCHIVE_BASE}/data/spot/daily/aggTrades/{quote(symbol, safe='')}/"
               f"{quote(symbol, safe='')}-aggTrades-{d.isoformat()}.zip")
        if _head_exists(url):
            anchor = d
            break
    if not anchor:
        raise RuntimeError(f"官方归档里找不到 {symbol} 的现货逐笔成交文件（近 7 天都试过），"
                           f"请确认现货交易对名称")
    url = (f"{ARCHIVE_BASE}/data/spot/daily/aggTrades/{quote(symbol, safe='')}/"
           f"{quote(symbol, safe='')}-aggTrades-{anchor.isoformat()}.zip")
    raw = _fetch_zip_bytes(url)
    if len(raw) > max_bytes:
        raise RuntimeError(f"归档文件 {len(raw)/1024/1024:.1f}MB 超过上限，如实报错不硬扛")

    # 第一遍：成交笔数、VWAP、价格区间
    count, qty_sum, quote_sum, lo, hi = 0, 0.0, 0.0, None, None
    for price, qty in _iter_agg_trades(raw):
        count += 1
        qty_sum += qty
        quote_sum += price * qty
        lo = price if lo is None or price < lo else lo
        hi = price if hi is None or price > hi else hi
    if not count or not qty_sum:
        raise RuntimeError("归档逐笔文件解析后没有有效成交记录，如实报错")
    vwap = quote_sum / qty_sum

    # 第二遍：按相对价格档（约 0.001%，粒度对齐真实盘口）分桶，累计成交量与成交额
    tick = max(vwap * 0.00001, 1e-12)
    if (hi - lo) / tick > 20000:          # 档数上限保护：自适应放宽到约 2 万档
        tick = (hi - lo) / 20000
    buckets = {}
    for price, qty in _iter_agg_trades(raw):
        key = int(price / tick)
        row = buckets.get(key)
        if row is None:
            buckets[key] = [price * qty, qty, price]  # 累计额、累计量、首笔价
        else:
            row[0] += price * qty
            row[1] += qty

    levels = []
    for quote_v, qty_v, first_price in buckets.values():
        avg = quote_v / qty_v if qty_v else first_price
        levels.append([avg, qty_v])
    asks = sorted([lv for lv in levels if lv[0] >= vwap], key=lambda x: x[0])
    bids = sorted([lv for lv in levels if lv[0] < vwap], key=lambda x: x[0], reverse=True)
    if len(asks) < 2 or len(bids) < 2:
        raise RuntimeError("归档成交流无法重建双边阶梯（成交价分布过于集中），如实报错")

    synthetic_depth = {"symbol": symbol, "last_update_id": None, "bids": bids, "asks": asks}
    notes.append("官方没有盘口快照的历史归档：本通道用锚定日全部逐笔成交重建价格阶梯"
                 "（每档约 0.01%），是「成交分布重建」，不是当时的挂单盘口。")
    notes.append("阶梯每档的量级是当日累计成交量，反映成交流深度，不等同瞬时挂单深度 —— "
                 "跨通道比较滑点时请注意这一口径差异。")
    notes.append("成交流口径适合测算大额冲击：本通道额外给出 10 万 / 100 万 USDT 两档；"
                 "小额档在成交流阶梯上通常不跨档（偏差≈0），这是口径特性，不是算错。")
    meta = {"anchor": anchor.isoformat(), "trades": count, "vwap": vwap,
            "price_low": lo, "price_high": hi, "levels": len(levels),
            "bucket_pct": round(tick / vwap * 100, 5) if vwap else 0,
            "file_mb": round(len(raw) / 1024 / 1024, 2)}
    return synthetic_depth, meta, notes


# ------------------------------------------------------------ 引擎调用与恐贪指数
def run_engine(depth, side, quote_amounts):
    """把盘口数据直接喂给引擎的纯函数（引擎一行不改）。"""
    return sources.calculate_slippage(depth, side, quote_amounts)


def fetch_fear_greed_safe(limit=7):
    """恐惧贪婪指数（alternative.me，第三方）——失败不致命，如实说明。"""
    try:
        fg = sources.fetch_fear_greed(limit)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "source": "alternative.me（第三方公开接口）"}
    hist = fg.get("history") or []
    prev = hist[-2] if len(hist) >= 2 else None
    now = fg.get("now") or {}
    delta = (int(now.get("value", 0)) - int(prev.get("value", 0))) if prev else 0
    return {"now": now, "history": hist, "delta": delta,
            "source": "alternative.me（第三方公开接口，非币安数据）"}


def honesty_common(estimate=False):
    items = [
        "静态盘口估算不包含手续费、网络延迟、撤单、成交顺序变化和突发波动。",
        "盘口是瞬时快照：采样时间与快照编号已印在证书上，与网页同源同口径。",
    ]
    if estimate:
        items.insert(0, "本证书的盘口阶梯由锚定日真实逐笔成交重建（官方无盘口快照的历史归档），"
                        "每档量级=当日累计成交量，反映成交流深度而非瞬时挂单深度。")
    items.append("恐惧贪婪指数来自 alternative.me（第三方公开接口），非币安数据；"
                 "归档通道下它是实时值，与 T+1 归档不同源。")
    items.append("本证书不构成投资建议。")
    return items


# ------------------------------------------------------------ 检定证书输出（第 10 套模板）
def _fmt_money(n):
    n = float(n or 0)
    if abs(n) >= 1e9:
        return f"{n/1e9:,.2f}B"
    if abs(n) >= 1e6:
        return f"{n/1e6:,.2f}M"
    if abs(n) >= 1e3:
        return f"{n:,.2f}"
    return f"{n:,.4f}"


def _best_price_fmt(p):
    return f"{p:,.8g}"


def print_certificate(p):
    W = 62
    cert_no = datetime.now(timezone.utc).strftime("HS-%Y%m%d-%H%M")
    print("┏" + "━" * W + "┓")
    print("┃" + "盘口损耗检定证书".center(W - 4) + "    ┃")
    print("┃" + f"编号 {cert_no}".ljust(W - 2) + "┃")
    print("┗" + "━" * W + "┛")
    print(f" 送检样品   {p['symbol']}（现货）")
    print(f" 检定方向   {'买入（吃卖盘）' if p['side'] == 'buy' else '卖出（吃买盘）'}")
    print(f" 送检金额   {p['amount']:,.2f} USDT")
    print(f" 检定基准   {p['via_label']}")
    if p.get("anchor"):
        print(f" 锚定日期   {p['anchor']}（官方归档 T+1）")
    print(f" 出具时间   {p['generated_at']}")
    s = p["slippage"]
    env = f"买一 {_best_price_fmt(s['best_bid'])} ｜ 卖一 {_best_price_fmt(s['best_ask'])}" \
          f" ｜ 价差 {s['spread_pct']:.4f}%"
    print(f" 盘口环境   {env}")
    if p["estimate"]:
        m = p.get("depth_meta") or {}
        print(f" 阶梯来源   逐笔成交重建 · 成交 {m.get('trades', 0):,} 笔 · "
              f"区间 {_best_price_fmt(m.get('price_low', 0))}~{_best_price_fmt(m.get('price_high', 0))} · "
              f"{m.get('levels', 0)} 档")
    else:
        m = p.get("depth_meta") or {}
        print(f" 快照编号   {m.get('last_update_id')}（官方盘口快照，瞬时值）")
    print("─" * (W + 2))
    print("【检定项目】四档资金量吃单模拟")
    print("  金额(U)        理论均价          偏差%        磨损(U)      使用档位    未成交(U)")
    for row in s["results"]:
        print("  {:>9,.0f}  {:>16}  {:>11.4f}%  {:>12,.2f}  {:>9}  {:>11,.2f}".format(
            row["amount"], _best_price_fmt(row["avg_price"]), row["slippage_pct"],
            row["cost_usdt"], row["levels_used"], row["unfilled_usdt"]))
    max_row = max(s["results"], key=lambda r: r["slippage_pct"])
    rating = s["risk_rating"]
    print("【检定结论】")
    print(f"  本次金额偏差   {next((r['slippage_pct'] for r in s['results'] if r['amount'] == p['amount']), 0):.4f}%"
          f"（{p['amount']:,.0f} USDT）")
    print(f"  最大偏差       {max_row['slippage_pct']:.4f}%（档位 {max_row['amount']:,.0f} USDT）")
    print(f"  偏差等级       {rating} —— {RATINGS.get(rating, '')}")
    if s.get("critical_amount"):
        print(f"  临界金额       约 {s['critical_amount']:,.0f} USDT（滑点急剧放大）")
    else:
        print("  临界金额       当前测试中未发现明显突变")
    fg = p.get("fear_greed") or {}
    if fg.get("now"):
        now = fg["now"]
        delta = fg.get("delta", 0)
        trend = "回暖" if delta > 0 else ("进一步恐慌/转弱" if delta < 0 else "基本持平")
        print("【检定环境备注】")
        print(f"  市场情绪       {now.get('value')} / 100（{now.get('classification')}）"
              f"｜较前一日 {'+' if delta > 0 else ''}{delta}（{trend}）")
        print(f"  情绪来源       {fg.get('source')}")
    elif fg.get("error"):
        print("【检定环境备注】")
        print(f"  市场情绪       本次未取到（{fg['error'][:70]}）—— 如实留空不补数")
    print("【检定须知】")
    for tip in p["honesty"]:
        print(f"  · {tip}")
    print("┏" + "━" * W + "┓")
    print("┃" + "检定员：huadian-sunhao-agent ｜ 本证书不构成投资建议".ljust(W - 2) + "┃")
    print("┗" + "━" * W + "┛")


# ------------------------------------------------------------ 汇总
def build_payload(symbol, side, amount, calc, meta, fear_greed, notes, honesty, estimate):
    payload = {
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "source": meta["source"],
        "via": meta["via"],
        "via_label": meta["via_label"],
        "estimate": estimate,
        "depth_meta": meta.get("depth_meta") or {},
        "slippage": calc,
        "fear_greed": fear_greed,
        "notes": notes,
        "honesty": honesty,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    if meta.get("requestedSource"):
        payload["requestedSource"] = meta["requestedSource"]
    if meta.get("anchor"):
        payload["anchor"] = meta["anchor"]
    return payload


def build_amounts(amount):
    amounts = list(DEFAULT_AMOUNTS)
    if amount not in amounts:
        amounts.append(amount)
        amounts.sort()
    return amounts


# ------------------------------------------------------------ 原有三条命令（保持兼容）
def cmd_feargreed(args):
    print("正在获取恐惧贪婪指数……")
    fg = sources.fetch_fear_greed(args.limit)
    now = fg.get("now", {})
    if now:
        print("当前恐惧贪婪指数：{} / 100（{}）".format(
            now.get("value"), now.get("classification")))
    if args.history:
        hist = fg.get("history", [])
        print("\n近 {} 日走势：".format(min(len(hist), args.limit)))
        for row in hist[-args.limit:]:
            print("  {} {:>3}".format(row["date"], row["value"]))
    print("\n数据来源：alternative.me（第三方公开接口，非币安数据）。")


def cmd_web(args):
    import web
    if args.dual:
        web.run_dual(port_a=args.port_a, port_b=args.port_b)
    else:
        web.run(port=args.port)


# ------------------------------------------------------------ 四通道检定主流程
def run_inspection(symbol, side, amount, flags, args):
    amounts = build_amounts(amount)
    notes = []
    estimate = False
    meta = {"source": "realtime", "via": "depth-route", "via_label": CHANNEL_LABELS["route"],
            "requestedSource": None, "anchor": None, "depth_meta": {}}

    if "--official" in flags:
        say("检定基准：官方开源数据仓库（逐笔成交流重建阶梯，T+1）…")
        amounts = sorted(set(amounts) | {100000.0, 1000000.0})  # 成交流口径适合大额冲击
        synthetic, off_meta, off_notes = collect_official(symbol, side, amounts)
        notes.extend(off_notes)
        estimate = True
        depth = synthetic
        meta.update({"source": "official", "via": "official-archive",
                     "via_label": CHANNEL_LABELS["archive"], "anchor": off_meta["anchor"],
                     "depth_meta": off_meta})
        say(f"锚定 {off_meta['anchor']}：{off_meta['trades']:,} 笔成交 → {off_meta['levels']} 档阶梯")
    elif "--skill" in flags:
        say("检定基准：官方 CLI（binance-cli request）…")
        try:
            depth = cli_depth_get(symbol, limit=args.depth_limit)
            meta.update({"source": "skill", "via": "binance-cli", "via_label": CHANNEL_LABELS["cli"]})
        except Exception as exc:
            say(f"官方 CLI 不可用（{exc}）→ 如实回退到官方公开行情入口，并在通道备注标注")
            depth, route = depth_get(symbol, limit=args.depth_limit, timeout=args.route_timeout)
            meta.update({"source": "realtime", "via": "depth-route",
                         "via_label": CHANNEL_LABELS["route"] + "（回退）",
                         "requestedSource": "skill"})
            notes.append(f"本次请求的是官方 CLI 通道，但 CLI 不可用（{exc}），已如实回退")
    else:
        force = "--live" in flags
        say(f"检定基准：官方公开行情入口盘口快照（{'强制刷新重新选路' if force else '自动选路'}）…")
        depth, route = depth_get(symbol, limit=args.depth_limit, force_refresh=force,
                                 timeout=args.route_timeout)
        if force:
            meta["via_label"] = CHANNEL_LABELS["fresh"]
        notes.append(f"取数线路：{route}")
    if not estimate:
        meta["depth_meta"] = {"last_update_id": depth.get("last_update_id"),
                              "route": meta["via"]}

    say("盘口数据喂给引擎纯函数（引擎一行不改）…")
    calc = run_engine(depth, side, amounts)
    if all(r["levels_used"] <= 1 for r in calc["results"]) and \
            max((r["slippage_pct"] for r in calc["results"]), default=0) < 0.001:
        notes.append("本次全部测试金额都未跨出盘口第一档（该品种深度极好）——"
                     "偏差 0 是真实结果，不是程序没算。")
    fg = fetch_fear_greed_safe(7)
    honesty = honesty_common(estimate)
    return build_payload(symbol, side, amount, calc, meta, fg, notes, honesty, estimate)


def main():
    # Windows 管道下默认按系统代码页（GBK）写 stdout，统一强制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = sys.argv[1:]
    if not argv:
        build_parser().print_help()  # 原行为：无参数打印帮助，正常退出
        return

    if argv[0] in ("web", "feargreed", "slippage"):
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.cmd == "web":
            cmd_web(args)
            return
        if args.cmd == "feargreed":
            cmd_feargreed(args)
            return
        # slippage 子命令（原命令）→ 走同一套四通道检定
        flags = [f for f, given in (("--live", args.live), ("--skill", args.skill),
                                    ("--official", args.official)) if given]
        if len(flags) > 1:
            sys.stderr.write(f"✗ 通道旗标冲突：{'、'.join(flags)} 一次只能选一条\n")
            sys.exit(2)
        symbol = (args.symbol or "BTCUSDT").upper().strip()
        try:
            payload = run_inspection(symbol, args.side, args.amount, flags, args)
        except Exception as exc:
            say(f"✗ 出错：{exc}")
            sys.exit(1)
        if args.json:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print_certificate(payload)
        return

    # 四通道主入口：位置参数 = 交易对或自然语言
    ap = argparse.ArgumentParser(prog="agent.py", add_help=True,
                                 description="盘口损耗研判智能体 · 命令行检定（四通道）")
    ap.add_argument("target", nargs="*", default=[], help="交易对（如 BTCUSDT）或自然语言")
    ap.add_argument("--side", choices=["buy", "sell"], default=None, help="交易方向（默认 buy）")
    ap.add_argument("--amount", type=float, default=None, help="交易金额 USDT（默认 10000）")
    ap.add_argument("--live", action="store_true", help="强制刷新：重新选路")
    ap.add_argument("--skill", action="store_true", help="官方 CLI（binance-cli）直取")
    ap.add_argument("--official", action="store_true", help="官方归档：逐笔成交流重建阶梯（T+1）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（进度走 stderr）")
    ap.add_argument("--depth-limit", type=int, default=100, help="盘口档位数（默认 100，最大 5000）")
    ap.add_argument("--route-timeout", type=float, default=8.0, help="单线路超时秒数")
    args = ap.parse_args(argv)

    flags = [f for f, given in (("--live", args.live), ("--skill", args.skill),
                                ("--official", args.official)) if given]
    if len(flags) > 1:
        sys.stderr.write(f"✗ 通道旗标冲突：{'、'.join(flags)} 一次只能选一条\n")
        sys.exit(2)

    symbol, nl_parts, explicit_symbol = None, [], False
    for tok in args.target:
        t = tok.strip()
        up = t.upper()
        if re.fullmatch(r"[A-Z0-9]{2,12}USDT", up):
            symbol, explicit_symbol = up, True
        elif up in KNOWN_BASES:
            symbol, explicit_symbol = up + "USDT", True
        else:
            nl_parts.append(t)
    nl_text = " ".join(nl_parts)

    nl_channel = channel_from_intent(nl_text) if nl_text else None
    if not flags and nl_channel:
        flags = ["--" + nl_channel]
    elif flags and nl_channel and "--" + nl_channel not in flags:
        say(f"已显式指定数据通道，忽略自然语言里的通道意图（{'、'.join(flags)} 优先）")

    if not symbol:
        symbol = symbol_from_intent(nl_text) or "BTCUSDT"
        if not explicit_symbol:
            say(f"未指定交易对，按默认 {symbol} 检定（自然语言里也没找到）")
    side = args.side or side_from_intent(nl_text) or "buy"
    amount = args.amount if args.amount is not None else (amount_from_intent(nl_text) or 10000.0)
    if args.depth_limit:
        args.depth_limit = max(20, min(int(args.depth_limit), 5000))

    try:
        payload = run_inspection(symbol, side, float(amount), flags, args)
    except Exception as exc:
        say(f"✗ 出错：{exc}")
        if "--skill" in flags:
            say("（本次请求的是官方 CLI 通道，未能完成，也未静默回退出假数据）")
        sys.exit(1)

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print_certificate(payload)


def build_parser():
    parser = argparse.ArgumentParser(prog="agent.py", description="盘口损耗研判智能体")
    subs = parser.add_subparsers(dest="cmd")

    slip = subs.add_parser("slippage", help="测算盘口滑点磨损并联动情绪（四通道）")
    slip.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT")
    slip.add_argument("--side", choices=["buy", "sell"], default="buy")
    slip.add_argument("--amount", type=float, default=10000, help="交易金额，单位 USDT")
    slip.add_argument("--live", action="store_true", help="强制刷新：重新选路")
    slip.add_argument("--skill", action="store_true", help="官方 CLI（binance-cli）直取")
    slip.add_argument("--official", action="store_true", help="官方归档（T+1，逐笔成交流重建）")
    slip.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    slip.add_argument("--depth-limit", type=int, default=100, help="盘口档位数（默认 100）")
    slip.add_argument("--route-timeout", type=float, default=8.0, help="单线路超时秒数")
    slip.set_defaults(func="slippage")

    fg = subs.add_parser("feargreed", help="查看恐惧贪婪指数")
    fg.add_argument("--limit", type=int, default=7)
    fg.add_argument("--history", action="store_true", help="显示历史走势")
    fg.set_defaults(func="feargreed")

    web_cmd = subs.add_parser("web", help="启动本地网页")
    web_cmd.add_argument("--port", type=int, default=None)
    web_cmd.add_argument("--dual", action="store_true", help="同时启动 8001 和 8002")
    web_cmd.add_argument("--port-a", type=int, default=8001)
    web_cmd.add_argument("--port-b", type=int, default=8002)
    web_cmd.set_defaults(func="web")
    return parser


if __name__ == "__main__":
    main()
