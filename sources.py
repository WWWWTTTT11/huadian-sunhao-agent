# -*- coding: utf-8 -*-
"""盘口损耗研判智能体数据源与计算模块。"""
import datetime
import json
import urllib.parse
import urllib.request

UA = "huadian-sunhao-agent/1.0 (educational)"
TIMEOUT = 12
BINANCE_DEPTH_URL = "https://data-api.binance.vision/api/v3/depth"
FNG_URL = "https://api.alternative.me/fng/?limit={}"


def _get(url, timeout=None):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout or TIMEOUT) as response:
        return response.read().decode("utf-8", "ignore")


def _get_json(url, timeout=None):
    return json.loads(_get(url, timeout))


def fetch_fear_greed(limit=7):
    """获取当前及近 limit 天恐慌贪婪指数。"""
    try:
        limit = max(1, min(int(limit), 365))
    except (TypeError, ValueError):
        limit = 7
    data = _get_json(FNG_URL.format(limit)).get("data", [])
    history = []
    for item in data:
        timestamp = int(item.get("timestamp") or 0)
        history.append({
            "value": int(item.get("value") or 0),
            "classification": item.get("value_classification") or "Unknown",
            "timestamp": timestamp,
            "date": datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%d"),
        })
    history.sort(key=lambda row: row["timestamp"])
    return {"now": history[-1] if history else {}, "history": history}


def fetch_binance_depth(symbol="BTCUSDT", limit=100):
    """获取 Binance 现货公开盘口深度。"""
    symbol = (symbol or "BTCUSDT").upper().strip()
    try:
        limit = max(20, min(int(limit), 5000))
    except (TypeError, ValueError):
        limit = 100
    url = BINANCE_DEPTH_URL + "?" + urllib.parse.urlencode({"symbol": symbol, "limit": limit})
    data = _get_json(url)
    bids = [[float(price), float(quantity)] for price, quantity in data.get("bids", [])
            if float(price) > 0 and float(quantity) > 0]
    asks = [[float(price), float(quantity)] for price, quantity in data.get("asks", [])
            if float(price) > 0 and float(quantity) > 0]
    if not bids or not asks:
        raise RuntimeError("盘口深度为空")
    return {"symbol": symbol, "last_update_id": data.get("lastUpdateId"), "bids": bids, "asks": asks}


def _walk_book(levels, quote_amount):
    """按价格档位模拟吃单，金额单位为 USDT。"""
    remaining = float(quote_amount)
    spent = 0.0
    base_quantity = 0.0
    filled = []
    for price, quantity in levels:
        if remaining <= 0:
            break
        level_quote = price * quantity
        take_quote = min(remaining, level_quote)
        take_quantity = take_quote / price
        spent += take_quote
        base_quantity += take_quantity
        remaining -= take_quote
        filled.append({"price": price, "quantity": take_quantity, "quote": take_quote})
    return {
        "requested_quote": quote_amount,
        "filled_quote": spent,
        "filled_base": base_quantity,
        "average_price": spent / base_quantity if base_quantity else 0,
        "unfilled_quote": max(0, remaining),
        "levels_used": len(filled),
        "fills": filled,
    }


def calculate_slippage(depth, side="buy", quote_amounts=None):
    """计算买入或卖出的多档盘口滑点和磨损。"""
    side = (side or "buy").lower()
    if side not in ("buy", "sell"):
        raise ValueError("交易方向必须是 buy 或 sell")
    quote_amounts = quote_amounts or [1000, 5000, 10000, 30000]
    bids, asks = depth["bids"], depth["asks"]
    best_bid, best_ask = bids[0][0], asks[0][0]
    midpoint = (best_bid + best_ask) / 2
    levels = asks if side == "buy" else bids
    results = []
    for amount in quote_amounts:
        fill = _walk_book(levels, amount)
        average = fill["average_price"]
        reference = best_ask if side == "buy" else best_bid
        impact = ((average - reference) / reference * 100) if reference else 0
        midpoint_impact = ((average - midpoint) / midpoint * 100) if midpoint else 0
        results.append({
            "amount": amount,
            "avg_price": average,
            "best_price": reference,
            "slippage_pct": abs(impact),
            "mid_slippage_pct": abs(midpoint_impact),
            "cost_usdt": amount * abs(impact) / 100,
            "levels_used": fill["levels_used"],
            "unfilled_usdt": fill["unfilled_quote"],
            "filled_base": fill["filled_base"],
        })
    critical_amount = None
    for previous, current in zip(results, results[1:]):
        if previous["slippage_pct"] > 0 and current["slippage_pct"] / previous["slippage_pct"] >= 1.8:
            critical_amount = current["amount"]
            break
    maximum_slippage = max((row["slippage_pct"] for row in results), default=0)
    rating = "低" if maximum_slippage < 0.1 else (
        "中" if maximum_slippage < 0.5 else (
        "高" if maximum_slippage < 1.5 else "极高"))
    return {
        "side": side,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": midpoint,
        "spread_pct": (best_ask - best_bid) / midpoint * 100 if midpoint else 0,
        "results": results,
        "critical_amount": critical_amount,
        "risk_rating": rating,
    }
