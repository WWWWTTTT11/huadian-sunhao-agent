"""合约爆仓多空观测智能体 · 命令行战报入口（四通道）。

用法
  python agent.py web                        # 本地网页（原功能不变）
  python agent.py BTCUSDT                    # 战报：官方合约实时接口（自动选路，与网页同口径）
  python agent.py BTCUSDT --live             # 强制刷新：重新选路（网络环境变化后用）
  python agent.py BTCUSDT --skill            # 官方 CLI（binance-cli request）直取
  python agent.py BTCUSDT --official         # 官方开源数据仓库（折算口径，T+1）
  python agent.py "用官方归档看看 BTCUSDT"     # 自然语言：自动选通道

说明
  · 判定引擎与网页是同一段代码（sources.py 的 liquidation_data / open_interest / analyze，
    本文件不改引擎一行：各通道把取到的真实数据函数级注入引擎后调用原 analyze）。
  · 命令行不做演示数据降级：取不到数就如实报错退出（网页版保留演示降级并明确标注）。
  · 引擎原有的「持仓趋势」示意曲线与大额爆仓「代表性补录」不进战报 —— 战报只报真实数据。
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
from web import run_server

# ---------------------------------------------------------------- 通道标识
CHANNEL_LABELS = {
    "route": "官方合约实时接口（先直连、连不上自动换本机上网工具的通道，与网页同口径）",
    "fresh": "官方合约实时接口（强制刷新：重新选一遍线路，网络环境变化后用）",
    "cli": "官方 CLI（binance-cli request）直取合约实时接口",
    "archive": "币安官方开源数据仓库（K 线 + 资金费率折算爆仓压力 + 合约指标，T+1）",
}

FAPI = "https://fapi.binance.com"
ARCHIVE_BASE = "https://data.binance.vision"
FACE_PREFIXES = ["", "1000", "10000", "100000"]  # 官方归档的面值币前缀（SHIBUSDT 实际是 1000SHIBUSDT）
UA = {"User-Agent": "baocang-duokong-agent/1.0", "Accept": "application/json"}

# 强平方向口径（与引擎一致）：强平单 side=SELL → 买方（多头仓位）被强平 → 记「多头」伤亡


def say(msg=""):
    """进度与说明一律走 stderr，保证 --json 模式 stdout 只有纯 JSON。"""
    sys.stderr.write(str(msg) + "\n")


# ------------------------------------------------------------ 自然语言入口

KNOWN_BASES = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
               "DOT", "LTC", "TRX", "TON", "PEPE", "SHIB", "WIF", "OP", "ARB", "NEAR"]


def channel_from_intent(text):
    """自然语言 → 通道。识别不出返回 None（静默走默认），绝不乱猜。"""
    t = text.lower()
    if any(k in t for k in ["官方技能", "官方 cli", "官方cli", "binance-cli", "skill"]):
        return "skill"
    if any(k in t for k in ["官方归档", "官方开源", "开源数据", "历史", "昨天", "t+1", "免代理", "折算"]):
        return "official"
    if any(k in t for k in ["强制刷新", "重新选路", "网络不行", "换网络", "实时", "最新", "现在"]):
        return "live"
    return None


def symbol_from_intent(text):
    """从一句话里找合约交易对：先找明写的 XXXUSDT，再认常见币种名。找不到返回 None。"""
    m = re.search(r"([A-Z0-9]{2,12}USDT)", text.upper())
    if m:
        return m.group(1)
    up = text.upper()
    for base in KNOWN_BASES:
        if re.search(rf"\b{base}\b", up):
            return base + "USDT"
    return None


# ------------------------------------------------------------ 官方实时接口（自动选路）

_ROUTE = {"opener": None, "desc": "", "proxy_url": None}


class EndpointGone(Exception):
    """线路是通的，但官方明确回了 4xx（端点已下线 / 参数不对）—— 不该继续换线路、也不该报「全线路不可达」。"""


def _make_opener(route):
    """route='direct' → 绕过系统代理直连；否则走 127.0.0.1:<port> 本机代理。"""
    if route == "direct":
        return build_opener(ProxyHandler({}))
    return build_opener(ProxyHandler({"http": f"http://{route}", "https": f"http://{route}"}))


def _try_once(opener, path, timeout):
    """单次请求：连接类失败抛异常；HTTP 错误返回 (None, 'HTTP <code>')。"""
    import urllib.error
    req = Request(FAPI + path, headers=UA)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"


def fapi_get(path, timeout=8.0, force_refresh=False):
    """带自动选路的合约接口 GET。成功线路会记住（同一次运行内复用）。

    返回 (json 数据, 线路描述)。
    - 连接类失败（超时 / 拒绝）→ 换下一条线路试，全试完抛 RuntimeError；
    - 官方明确回 404（端点已下线）→ 抛 EndpointGone：线路是通的，别误报成「网络不通」。
    """
    if force_refresh:
        _ROUTE.update(opener=None, desc="", proxy_url=None)

    def _call(opener, desc):
        data, status = _try_once(opener, path, timeout)
        if status == "HTTP 404":
            raise EndpointGone(f"官方返回 404（该端点已下线或不存在）：{path}")
        if status:
            raise RuntimeError(f"官方返回 {status}：{path}")
        return data, desc

    if _ROUTE["opener"] is not None:
        return _call(_ROUTE["opener"], _ROUTE["desc"])
    routes = [("direct", "直连")] + [(f"127.0.0.1:{p}", f"本机上网工具端口 {p}") for p in
                                     (7897, 7890, 10809, 2080, 1080, 8888)]
    last_err = None
    for route, desc in routes:
        opener = _make_opener(route)
        try:
            data, desc_used = _call(opener, desc)
            _ROUTE.update(opener=opener, desc=desc,
                          proxy_url=(None if route == "direct" else f"http://{route}"))
            return data, desc_used
        except EndpointGone:
            raise  # 端点问题：换线路没有用，交给上层如实处理
        except Exception as exc:  # 连接类失败：换下一条线路继续试
            last_err = exc
            continue
    raise RuntimeError(
        f"官方合约实时接口全线路不可达（直连与 6 个本机上网工具端口都试过；最后错误：{last_err}）")


def _estimate_events(ohlcv, funding_rate, symbol):
    """小时级爆仓压力折算（与网页部署版 netlify api.mjs 同一套公式）：
    压力 = K 线振幅 ×（1 + |资金费率|×4000）；名义额 = 成交量 × 收盘价 × 压力 × 0.35。
    这是估算口径，输出必须如实标注。"""
    events = []
    for ts, o, h, l, c, v in ohlcv:
        if not o:
            continue
        rng = (h - l) / o
        pressure = rng * (1 + abs(funding_rate) * 4000)
        events.append({"time": ts, "symbol": symbol,
                       "side": "多头" if c < o else "空头",
                       "amount": v * c * pressure * 0.35})
    return events


def estimate_from_fapi_klines(klines, funding_rate, symbol):
    """官方实时 1h K 线（list 形）→ 折算事件。"""
    rows = [(_parse_ts(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
            for c in klines]
    return _estimate_events(rows, funding_rate, symbol)


def collect_fapi(symbol, force_refresh=False, timeout=8.0):
    """官方合约实时通道。

    实测事实：官方公开的实时强平明细端点 /fapi/v1/allForceOrders 已下线（404）。
    所以主口径 = 真实的持仓量 / 价格 / 24h 多空账户比 / 当前资金费率 / 24 根 1h K 线，
    爆仓金额用与网页部署版同一套公式「K 线振幅 × 资金费率」折算（estimate=True）；
    若该端点将来恢复可用，自动改用真实逐笔强平（estimate=False）。
    """
    notes = []
    estimate = True
    events = []

    try:
        raw_orders, r1 = fapi_get(f"/fapi/v1/allForceOrders?symbol={quote(symbol)}&limit=1000",
                                  timeout=timeout, force_refresh=force_refresh)
    except EndpointGone:
        raw_orders, r1 = None, "强平明细端点已下线（404）"
        notes.append("官方实时强平明细端点 /fapi/v1/allForceOrders 已下线（实测 404）："
                     "爆仓金额改用「实时 K 线振幅 × 资金费率」折算（与网页部署版同一套公式），"
                     "持仓量 / 价格 / 多空账户比 / 资金费率仍是官方真实实时值。")
    if isinstance(raw_orders, list):
        events = [{
            "time": int(x.get("time", 0)),
            "symbol": x.get("symbol", symbol),
            # SELL 方向的强平单 = 多头仓位被强平
            "side": "多头" if x.get("side") == "SELL" else "空头",
            "amount": float(x.get("origQty", 0)) * float(x.get("price") or x.get("averagePrice") or 0),
        } for x in raw_orders]
        estimate = False

    oi_raw, r2 = fapi_get(f"/fapi/v1/openInterest?symbol={quote(symbol)}", timeout=timeout)
    px_raw, r3 = fapi_get(f"/fapi/v1/ticker/price?symbol={quote(symbol)}", timeout=timeout)
    ratio_raw, r4 = fapi_get(
        f"/futures/data/globalLongShortAccountRatio?symbol={quote(symbol)}&period=1h&limit=24",
        timeout=timeout)

    oi_usd = float(oi_raw.get("openInterest", 0) or 0) * float(px_raw.get("price", 0) or 0)
    if not oi_usd:
        raise RuntimeError("持仓量或价格接口返回空值，如实报错不补演示值")
    ratio_series = []
    for x in ratio_raw:
        if not x.get("timestamp"):
            continue
        ts_ms = int(x["timestamp"])
        ratio_series.append({"ratio": float(x.get("longShortRatio", 0)), "ts": ts_ms,
                             "hour": datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour})
    ratio_series.sort(key=lambda x: x["ts"])

    if estimate:
        fund_raw, r5 = fapi_get(f"/fapi/v1/premiumIndex?symbol={quote(symbol)}", timeout=timeout)
        funding_rate = float(fund_raw.get("lastFundingRate", 0) or 0)
        kl, r6 = fapi_get(f"/fapi/v1/klines?symbol={quote(symbol)}&interval=1h&limit=24",
                          timeout=timeout)
        events = estimate_from_fapi_klines(kl, funding_rate, symbol)

    oi_data = {"oi": oi_usd,
               "ratio": ratio_series[-1]["ratio"] if ratio_series else 1.0,
               "price": float(px_raw.get("price", 0) or 0)}
    route_desc = (f"自动选路成功（{_ROUTE['desc']}）· "
                  + ("折算口径：24 根实时 K 线 × 当前资金费率" if estimate else "真实逐笔强平"))
    return events, oi_data, ratio_series, route_desc, notes, estimate


def collect_cli(symbol, timeout=45):
    """官方 CLI 通道：用 binance-cli request GET 打官方端点。CLI 不在或取不到就抛错。

    两个实测要点：
    · 官方 CLI 不读系统的上网工具设置 → 先用默认通道探一次线路，把可用代理通过
      HTTP_PROXY / HTTPS_PROXY 环境变量告诉它；
    · 强平明细端点同样已下线 → 与其他实时通道一致，走折算口径（estimate=True）。
    返回 (events, oi_data, ratio_series, notes, estimate)。
    """
    exe = os.environ.get("BINANCE_CLI_PATH") or shutil.which("binance-cli")
    if not exe:
        raise RuntimeError("未找到官方 CLI（binance-cli）。安装见官方文档，或设置环境变量 BINANCE_CLI_PATH")

    proxy_url = None
    try:
        fapi_get("/fapi/v1/ping", timeout=6)  # 借默认通道的选路探出可用线路
        proxy_url = _ROUTE.get("proxy_url")
    except Exception:
        pass
    env = dict(os.environ)
    if proxy_url:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy_url

    def cli_get(path):
        p = subprocess.run([exe, "request", "GET", FAPI + path], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError(f"官方 CLI 退出码 {p.returncode}（{path[:60]}…）")
        return json.loads(p.stdout)

    notes, estimate, events = [], True, []
    try:
        orders = cli_get(f"/fapi/v1/allForceOrders?symbol={quote(symbol)}&limit=1000")
    except Exception as exc:
        orders = None
        notes.append(f"官方 CLI 未能取到强平明细（{exc}）：该端点已下线，改用 K 线×资金费率折算口径。")
    if isinstance(orders, list):
        events = [{
            "time": int(x.get("time", 0)),
            "symbol": x.get("symbol", symbol),
            "side": "多头" if x.get("side") == "SELL" else "空头",
            "amount": float(x.get("origQty", 0)) * float(x.get("price") or x.get("averagePrice") or 0),
        } for x in orders]
        estimate = False

    oi = cli_get(f"/fapi/v1/openInterest?symbol={quote(symbol)}")
    px = cli_get(f"/fapi/v1/ticker/price?symbol={quote(symbol)}")
    ratio_raw = cli_get(
        f"/futures/data/globalLongShortAccountRatio?symbol={quote(symbol)}&period=1h&limit=24")
    oi_usd = float(oi.get("openInterest", 0) or 0) * float(px.get("price", 0) or 0)
    if not oi_usd:
        raise RuntimeError("官方 CLI 返回的持仓量或价格为空，如实报错")
    ratio_series = []
    for x in ratio_raw:
        if not x.get("timestamp"):
            continue
        ts_ms = int(x["timestamp"])
        ratio_series.append({"ratio": float(x.get("longShortRatio", 0)), "ts": ts_ms,
                             "hour": datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour})
    ratio_series.sort(key=lambda x: x["ts"])
    if estimate:
        fund = cli_get(f"/fapi/v1/premiumIndex?symbol={quote(symbol)}")
        kl = cli_get(f"/fapi/v1/klines?symbol={quote(symbol)}&interval=1h&limit=24")
        events = estimate_from_fapi_klines(kl, float(fund.get("lastFundingRate", 0) or 0), symbol)
    oi_data = {"oi": oi_usd,
               "ratio": ratio_series[-1]["ratio"] if ratio_series else 1.0,
               "price": float(px.get("price", 0) or 0)}
    return events, oi_data, ratio_series, notes, estimate


# ------------------------------------------------------------ 官方开源数据仓库（折算口径）

def _head_exists(url):
    import urllib.request
    try:
        opener = build_opener(ProxyHandler({}))
        opener.open(Request(url, headers=UA, method="HEAD"), timeout=15).close()
        return True
    except Exception:
        return False


def _fetch_zip_text(url):
    import urllib.request
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers=UA), timeout=60) as resp:
        zf = zipfile.ZipFile(io.BytesIO(resp.read()))
        return zf.read(zf.namelist()[0]).decode("utf-8")


def _month_list(d, count):
    """从 d 所在月往前数 count 个月的 (年, 月) 列表。"""
    out, y, m = [], d.year, d.month
    for _ in range(count):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _parse_ts(value):
    """归档时间戳自适应：毫秒（13 位）或微秒（16 位）统一转毫秒。"""
    x = int(value)
    while x > 10 ** 14:
        x //= 1000
    return x


def collect_official(symbol):
    """官方开源数据仓库通道。

    官方不提供强平明细的历史归档（这是事实），所以本通道用与网页部署版同一套折算公式：
    每小时爆仓压力 = K 线振幅 ×（1 + |资金费率|×4000），名义额 = 成交量×收盘价×压力×0.35，
    再加上合约指标文件里的真实持仓量与全局多空账户比。输出如实标注「折算估算口径」。
    """
    notes = []
    today = datetime.now(timezone.utc).date()

    # 1) 锚定日 + 面值币前缀探测（用合约指标文件当存在性探针，T+1）
    anchor, actual, used_prefix = None, symbol, None
    for back in range(1, 8):
        d = today - timedelta(days=back)
        for prefix in FACE_PREFIXES:
            candidate = prefix + symbol
            url = (f"{ARCHIVE_BASE}/data/futures/um/daily/metrics/{candidate}/"
                   f"{candidate}-metrics-{d.isoformat()}.zip")
            if _head_exists(url):
                anchor, actual, used_prefix = d, candidate, prefix
                break
        if anchor:
            break
    if not anchor:
        raise RuntimeError(
            f"官方归档里找不到 {symbol} 的合约指标文件（面值币前缀都试过），请确认合约交易对名称")
    if used_prefix:
        notes.append(f"官方归档使用面值币代码：{symbol} 对应 {actual}，已自动对应")

    # 2) 合约指标（真实持仓量 + 全局多空账户比，5 分钟粒度，按小时取末行）
    trend_rows, latest_oi, latest_ratio = [], 0.0, 1.0
    url = (f"{ARCHIVE_BASE}/data/futures/um/daily/metrics/{actual}/"
           f"{actual}-metrics-{anchor.isoformat()}.zip")
    text = _fetch_zip_text(url)
    rows = text.strip().splitlines()
    header = rows[0].split(",")
    idx_oi = header.index("sum_open_interest_value")
    idx_ratio = header.index("count_long_short_ratio")
    hourly_last = {}
    for line in rows[1:]:
        cells = line.split(",")
        ct = cells[0]  # create_time，形如 2026-09-13 00:55:00（UTC）
        try:
            ts = datetime.strptime(ct, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        bucket = hourly_last.get(ts.hour)
        if bucket is None or ts > bucket[0]:
            hourly_last[ts.hour] = (ts, float(cells[idx_oi]), float(cells[idx_ratio]))
    for hour in sorted(hourly_last):
        _, oi_v, ratio_v = hourly_last[hour]
        trend_rows.append({"hour": hour, "oi": oi_v, "ratio": ratio_v})
    if trend_rows:
        latest_oi = trend_rows[-1]["oi"]
        latest_ratio = trend_rows[-1]["ratio"]
    if not latest_oi:
        raise RuntimeError("合约指标文件解析后没有有效持仓量，如实报错")

    # 3) 资金费率：锚定月的上月 + 当月（当月归档常未发布，缺的如实列出）
    funding_rows, missing_months = [], []
    for y, m in _month_list(anchor, 2):
        url = (f"{ARCHIVE_BASE}/data/futures/um/monthly/fundingRate/{actual}/"
               f"{actual}-fundingRate-{y:04d}-{m:02d}.zip")
        try:
            ftext = _fetch_zip_text(url)
        except Exception:
            missing_months.append(f"{y:04d}-{m:02d}")
            continue
        for line in ftext.strip().splitlines()[1:]:
            cells = line.split(",")
            funding_rows.append({"fundingTime": _parse_ts(cells[0]), "fundingRate": float(cells[2])})
    funding_rows.sort(key=lambda x: x["fundingTime"])
    if missing_months:
        notes.append(f"资金费率归档未发布的月份：{'、'.join(missing_months)}（如实列出，不补数）")
    if not funding_rows:
        notes.append("资金费率文件一个都没取到：折算只含 K 线振幅项（如实说明）")

    def funding_for(ts_ms):
        prev = 0.0
        for row in funding_rows:
            if row["fundingTime"] <= ts_ms:
                prev = row["fundingRate"]
            else:
                break
        return prev

    # 4) 1h K 线（锚定日 24 根，带表头）→ 按网页部署版同一套公式折算爆仓压力
    url = (f"{ARCHIVE_BASE}/data/futures/um/daily/klines/{actual}/"
           f"1h/{actual}-1h-{anchor.isoformat()}.zip")
    ktext = _fetch_zip_text(url)
    klines = []
    for line in ktext.strip().splitlines():
        cells = line.split(",")
        try:
            float(cells[0])
        except ValueError:
            continue  # futures 的 klines 带表头：首列不是数字的行是表头，直接跳过
        klines.append(cells)
    if len(klines) < 12:
        raise RuntimeError(f"锚定日 K 线只有 {len(klines)} 根，不足以折算，如实报错")
    events = []
    for cells in klines:
        open_time = _parse_ts(cells[0])
        o, h, l, c, v = (float(cells[1]), float(cells[2]), float(cells[3]),
                         float(cells[4]), float(cells[5]))
        rng = (h - l) / o if o else 0.0  # 振幅 = (high - low) / open，与网页部署版一致
        fr = funding_for(open_time)
        pressure = rng * (1 + abs(fr) * 4000)
        notional = v * c
        events.append({"time": open_time, "symbol": symbol,
                       "side": "多头" if c < o else "空头",
                       "amount": notional * pressure * 0.35})
    route_desc = (f"币安官方开源数据仓库 · 锚定 {anchor.isoformat()} 收盘（T+1）· "
                  f"折算估算口径（官方无强平明细归档）")
    return events, {"oi": latest_oi, "ratio": latest_ratio,
                    "price": float(klines[-1][4])}, trend_rows, route_desc, notes, anchor


# ------------------------------------------------------------ 引擎调用（函数级注入，引擎一行不改）

def run_engine(symbol, events, oi_data):
    """把真实数据注入引擎取数函数后调用原 analyze —— 判定逻辑原样不动。"""
    orig_l, orig_o = sources.liquidation_data, sources.open_interest
    sources.liquidation_data = lambda *_a, **k: events
    sources.open_interest = lambda *_a, **k: oi_data
    try:
        return sources.analyze(symbol)
    finally:
        sources.liquidation_data, sources.open_interest = orig_l, orig_o


def real_large(events):
    """真实大额爆仓（单笔 ≥ 100 万 USDT，最多 8 条）。不足就是空 —— 绝不补代表性记录。"""
    return sorted([e for e in events if e["amount"] >= 1_000_000],
                  key=lambda x: x["amount"], reverse=True)[:8]


# ------------------------------------------------------------ 战报输出（第 9 套「战况通报体」）

LEVELS = {"正常": ("平稳", "◆"), "警告": ("交火", "◆◆"), "极端": ("激战", "◆◆◆")}


def fmt_usdt(n):
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def print_report(p):
    W = 58
    print("═" * W)
    print(" ◤ 战况通报 · 合约爆仓多空观测")
    print("═" * W)
    level_name, dots = LEVELS.get(p["summary"].get("alert"), ("平稳", "◆"))
    peak = int(p["summary"].get("peak_hour", 0))
    print(f"   观测目标   {p['symbol']}（USDT 本位永续）")
    print(f"   战况等级   {level_name} {dots}（峰值小时 {peak:02d}:00 UTC 爆仓为全时均值的 "
          f"{p['summary'].get('alert_level_x', 0):.1f} 倍）")
    print(f"   侦察线路   {p['via_label']}")
    if p.get("anchor"):
        print(f"   锚定日期   {p['anchor']}（官方归档 T+1）")
    print(f"   报告时间   {p['generated_at']}")
    if p.get("source_note"):
        print(f"   通道备注   {p['source_note']}")
    if p.get("estimate"):
        print("   口径提示   爆仓金额为折算估算值（官方实时明细端点已下线），其余字段为官方真实实时值")
    print("─" * W)
    s = p["summary"]
    total = max(s.get("total", 0), 1)
    long_t, short_t = s.get("long", 0), s.get("short", 0)
    print(" ◤ 伤亡统计（近 24 小时 · UTC 分桶）")
    print(f"   多军伤亡（多头爆仓）  {fmt_usdt(long_t)} USDT · 占 {long_t / total * 100:.1f}%")
    print(f"   空军伤亡（空头爆仓）  {fmt_usdt(short_t)} USDT · 占 {short_t / total * 100:.1f}%")
    print(f"   火力峰值   {peak:02d}:00 UTC（{fmt_usdt(s.get('peak_total', 0))} USDT）")
    print(f"   全天合计   {fmt_usdt(s.get('total', 0))} USDT")
    print(" ◤ 重大伤亡（单笔 ≥ 100 万 USDT · 真实逐笔，不足不补）" if not p.get("estimate")
          else " ◤ 重大伤亡（单笔真实明细）")
    large = p.get("large") or []
    if large:
        for e in large[:8]:
            t = datetime.fromtimestamp(e["time"] / 1000, timezone.utc)
            print(f"   {t:%H:%M} UTC  {e['symbol']}  {e['side']}  {fmt_usdt(e['amount'])} USDT")
    elif p.get("estimate"):
        print("   （折算估算口径没有单笔明细 —— 本区如实留空）")
    else:
        print("   （近 24 小时无单笔 ≥ 100 万 USDT 的真实爆仓记录 —— 不做代表性补录）")
    print(" ◤ 兵力与士气")
    print(f"   未平仓名义价值   {fmt_usdt(s.get('oi', 0))} USDT")
    ratio = s.get("ratio", 1.0)
    side = "多军士气占优" if ratio > 1 else "空军士气占优"
    print(f"   多空账户比       {ratio:.2f} : 1（{side}）")
    trend = p.get("trend") or []
    if trend:
        has_oi = "oi" in trend[0]
        sample = trend[::max(1, len(trend) // 6)][:6]
        if has_oi:
            print("   兵力走势（真实持仓量 · 每小时末值，USDT）")
            print("     " + "  ".join(
                f"{int(r.get('hour', 0)):02d}h {fmt_usdt(r.get('oi', 0))}" for r in sample))
            print("   士气走势（真实多空账户比 · 每小时末值）")
        else:
            print("   士气走势（真实多空账户比 · 每小时，官方实时接口 24h 序列）")
        print("     " + "  ".join(
            f"{int(r.get('hour', 0)):02d}h {float(r.get('ratio') or 0):.2f}" for r in sample))
    print(" ◤ 参谋提示（别过度解读）")
    for tip in p["honesty"]:
        print(f"   · {tip}")
    print("═" * W)


# ------------------------------------------------------------ 汇总输出

def build_payload(symbol, result, meta, large_real, trend_real, trend_note, notes, honesty):
    payload = {
        "symbol": symbol,
        "source": meta["source"],
        "via": meta["via"],
        "via_label": meta["via_label"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "summary": result.get("summary", {}),
        "hourly": result.get("hourly", []),
        "large": large_real,
        "large_note": ("真实逐笔大额爆仓（单笔 ≥ 100 万 USDT）" if large_real
                       else "近 24 小时无单笔 ≥ 100 万 USDT 的真实爆仓记录，不做代表性补录"),
        "trend": trend_real,
        "trend_note": trend_note,
        "notes": notes,
        "honesty": honesty,
    }
    if meta.get("requestedSource"):
        payload["requestedSource"] = meta["requestedSource"]
    if meta.get("anchor"):
        payload["anchor"] = meta["anchor"]
    # 战况等级依据（引擎口径的显式化）：峰值小时爆仓 ÷ 全时均值
    hourly = result.get("hourly", [])
    peak_total = max((h.get("total", 0) for h in hourly), default=0)
    avg = (sum(h.get("total", 0) for h in hourly) / len(hourly)) if hourly else 1
    payload["summary"]["peak_total"] = peak_total
    payload["summary"]["alert_level_x"] = (peak_total / avg) if avg else 0
    return payload


def honesty_common(estimate=False):
    items = ["账户比是「账户数」之比，不等于资金量之比；持仓量口径以输出标注为准。"]
    if not estimate:
        items.append("强平订单实时接口只返回最近约 1000 笔，深夜清淡时段样本天然偏少。")
    items.append("本战报不构成投资建议；爆仓数据只反映已发生的强平，不预测下一步。")
    return items


def main():
    # Windows 管道下 Python 默认按系统代码页（GBK）写 stdout/stderr，统一强制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write(
            "用法：python agent.py <币种|自然语言> [--live|--skill|--official] [--json]\n"
            "      python agent.py web [--port 8001]\n"
            "示例：python agent.py BTCUSDT --official --json\n")
        sys.exit(2)

    if argv[0] == "web":
        wp = argparse.ArgumentParser(prog="agent.py web")
        wp.add_argument("--port", type=int, default=None)
        wargs = wp.parse_args(argv[1:])
        port = int(os.environ.get("PORT") or wargs.port or 8001)
        run_server(port)
        return

    ap = argparse.ArgumentParser(prog="agent.py", add_help=True,
                                 description="合约爆仓多空观测智能体 · 命令行战报（四通道）")
    ap.add_argument("target", nargs="*", default=[], help="币种（如 BTCUSDT）或自然语言")
    ap.add_argument("--live", action="store_true", help="强制刷新：重新选路")
    ap.add_argument("--skill", action="store_true", help="官方 CLI（binance-cli）直取")
    ap.add_argument("--official", action="store_true", help="官方开源数据仓库（折算口径，T+1）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（进度走 stderr）")
    ap.add_argument("--route-timeout", type=float, default=8.0, help="实时接口单线路超时秒数")
    args = ap.parse_args(argv)

    flags = [f for f, given in (("--live", args.live), ("--skill", args.skill),
                                ("--official", args.official)) if given]
    if len(flags) > 1:
        sys.stderr.write(f"✗ 通道旗标冲突：{'、'.join(flags)} 一次只能选一条\n")
        sys.exit(2)

    # 目标解析：USDT 形态 / 常见币种单词 → 币种；其余整体进自然语言
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

    # 通道：显式旗标优先；无旗标时自然语言保守映射；都不给 → 默认实时
    nl_channel = channel_from_intent(nl_text) if nl_text else None
    if not flags and nl_channel:
        flags = ["--" + nl_channel]
    elif flags and nl_channel and "--" + nl_channel not in flags:
        say(f"已显式指定数据通道，忽略自然语言里的通道意图（{'、'.join(flags)} 优先）")

    if not symbol:
        symbol = symbol_from_intent(nl_text) or "BTCUSDT"
        if not explicit_symbol:
            say(f"未指定币种，按默认 {symbol} 观测（自然语言里也没找到）")

    notes = []
    meta = {"source": "realtime", "via": "fapi-route", "via_label": CHANNEL_LABELS["route"],
            "anchor": None, "requestedSource": None}
    trend_real, trend_note = [], ""
    requested = "--skill" in flags

    estimate = False
    try:
        if "--official" in flags:
            say("侦察线路：官方开源数据仓库（折算口径）…")
            events, oi_data, trend_real, route_desc, off_notes, anchor = collect_official(symbol)
            notes.extend(off_notes)
            estimate = True
            meta.update({"source": "official", "via": "official-archive",
                         "via_label": CHANNEL_LABELS["archive"], "anchor": anchor.isoformat()})
            trend_note = "真实持仓量与多空账户比序列（官方合约指标文件，每小时末值）"
            say(f"锚定 {anchor.isoformat()}，折算出 {len(events)} 条小时级爆仓压力事件")
        elif "--skill" in flags:
            say("侦察线路：官方 CLI（binance-cli request）…")
            try:
                events, oi_data, ratio_series, cli_notes, estimate = collect_cli(symbol)
                notes.extend(cli_notes)
                route_desc = ("官方 CLI（binance-cli request）直取成功"
                              + ("（折算口径）" if estimate else "（真实逐笔）"))
                meta.update({"source": "skill", "via": "binance-cli",
                             "via_label": CHANNEL_LABELS["cli"]})
                trend_real = ratio_series
                trend_note = "真实多空账户比 24h 序列（官方合约实时接口经官方 CLI 直取）"
            except Exception as exc:
                say(f"官方 CLI 不可用（{exc}）→ 如实回退到官方合约实时接口，并在通道备注标注")
                events, oi_data, ratio_series, route_desc, fapi_notes, estimate = collect_fapi(
                    symbol, timeout=args.route_timeout)
                notes.extend(fapi_notes)
                meta.update({"source": "realtime", "via": "fapi-route",
                             "via_label": CHANNEL_LABELS["route"] + "（回退）",
                             "requestedSource": "skill"})
                notes.append(f"本次请求的是官方 CLI 通道，但 CLI 不可用（{exc}），已如实回退")
                trend_real = ratio_series
                trend_note = "真实多空账户比 24h 序列（官方实时接口回退通道）"
        else:
            force = "--live" in flags
            say(f"侦察线路：官方合约实时接口（{'强制刷新重新选路' if force else '自动选路'}）…")
            events, oi_data, ratio_series, route_desc, fapi_notes, estimate = collect_fapi(
                symbol, force_refresh=force, timeout=args.route_timeout)
            notes.extend(fapi_notes)
            if force:
                meta["via_label"] = CHANNEL_LABELS["fresh"]
            trend_real = ratio_series
            trend_note = "真实多空账户比 24h 序列（官方实时接口 period=1h limit=24）"

        say(f"事件 {len(events)} 条，注入引擎判定（引擎一行不改）…")
        result = run_engine(symbol, events, oi_data)

        # 输出层两处真实化（引擎的示意 trend 与凑数 large 不进战报）
        large = real_large(events)
        if estimate:
            large = []
            notes.append("折算估算口径没有「单笔真实强平」可言 —— 大额伤亡区如实留空"
                         "（官方实时明细端点已下线、历史明细官方无归档）。")

        honesty = honesty_common(estimate)
        if meta["via"] == "official-archive":
            honesty.insert(0, "本通道爆仓金额为「K 线振幅 × 资金费率」折算的估算值（与网页部署版同一套公式），"
                               "不是逐笔真实强平 —— 官方不提供强平明细的历史归档。")
        elif estimate:
            honesty.insert(0, "官方公开的实时强平明细端点已下线（实测 404）：本通道爆仓金额为"
                               "「实时 K 线振幅 × 当前资金费率」折算的估算值（与网页部署版同一套公式）；"
                               "持仓量、价格、多空账户比、资金费率为官方真实实时值。")
        else:
            honesty.insert(0, "引擎原有的「多空持仓趋势」是示意曲线，本战报已替换为上方真实序列（口径见标注）。")
        if estimate:
            honesty.append("折算口径没有单笔真实强平明细，重大伤亡区如实留空。")
        elif not large:
            honesty.append("近 24 小时无单笔 ≥ 100 万 USDT 的真实爆仓；引擎内的「代表性补录」已被剔除，不进战报。")
        honesty.append("网页版在接口不可用时会展示演示数据（原作品设计如此）；命令行版绝不使用演示数据，取不到就报错。")

        payload = build_payload(symbol, result, meta, large, trend_real, trend_note, notes, honesty)
        payload["estimate"] = estimate
        payload["source_note"] = route_desc

        if args.json:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print_report(payload)
    except Exception as exc:
        say(f"✗ 出错：{exc}")
        if requested:
            say("（本次请求的是官方 CLI 通道，未能完成，也未静默回退出假数据）")
        sys.exit(1)


if __name__ == "__main__":
    main()
