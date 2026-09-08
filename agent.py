# -*- coding: utf-8 -*-
"""盘口损耗研判智能体命令行入口。"""
import argparse
import sys
import sources


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


def cmd_slippage(args):
    symbol = args.symbol.upper().strip()
    side = args.side.lower()
    print("正在获取 {} {} 盘口深度……".format(symbol, "买入" if side == "buy" else "卖出"))
    depth = sources.fetch_binance_depth(symbol, 100)
    amounts = [1000, 5000, 10000, 30000]
    if args.amount not in amounts:
        amounts.append(args.amount)
        amounts.sort()
    calc = sources.calculate_slippage(depth, side, amounts)
    fg = sources.fetch_fear_greed(7)
    now = fg.get("now", {})
    hist = fg.get("history", [])
    prev = hist[-2] if len(hist) >= 2 else None
    print("\n=== 盘口损耗研判智能体 ===")
    print("币种：{} ｜ 方向：{} ｜ 本次金额：{:,.2f} U".format(
        symbol, "买入" if side == "buy" else "卖出", args.amount))
    print("买一：{:,.6f} ｜ 卖一：{:,.6f} ｜ 盘口价差：{:.4f}%".format(
        calc["best_bid"], calc["best_ask"], calc["spread_pct"]))
    print("\n分档滑点测算：")
    print("{:>10} {:>14} {:>12} {:>14} {:>8} {:>10}".format(
        "金额(U)", "理论均价", "滑点", "预估磨损(U)", "档位", "未成交(U)"))
    for row in calc["results"]:
        print("{:>10,.0f} {:>14,.6f} {:>11.4f}% {:>14,.2f} {:>8} {:>10,.2f}".format(
            row["amount"], row["avg_price"], row["slippage_pct"], row["cost_usdt"],
            row["levels_used"], row["unfilled_usdt"]))
    print("\n流动性风险：{}".format(calc["risk_rating"]))
    if calc.get("critical_amount"):
        print("滑点急剧放大临界点：约 {:,.0f} U".format(calc["critical_amount"]))
    else:
        print("滑点急剧放大临界点：当前测试中未发现明显突变")
    if now:
        delta = int(now.get("value", 0)) - int(prev.get("value", 0)) if prev else 0
        direction = "回暖" if delta > 0 else ("进一步恐慌/转弱" if delta < 0 else "基本持平")
        print("\n市场情绪：{} / 100（{}）".format(now.get("value"), now.get("classification")))
        print("较前一日：{}{}/情绪变化：{}".format("+" if delta > 0 else "", delta, direction))
    print("\n免责声明：本结果为公开盘口理论估算，不构成投资建议。")


def cmd_web(args):
    import web
    if args.dual:
        web.run_dual(port_a=args.port_a, port_b=args.port_b)
    else:
        web.run(port=args.port)


def build_parser():
    parser = argparse.ArgumentParser(prog="agent.py", description="盘口损耗研判智能体")
    subs = parser.add_subparsers(dest="cmd")

    slip = subs.add_parser("slippage", help="测算盘口滑点磨损并联动情绪")
    slip.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT")
    slip.add_argument("--side", choices=["buy", "sell"], default="buy")
    slip.add_argument("--amount", type=float, default=10000, help="交易金额，单位 USDT")
    slip.set_defaults(func=cmd_slippage)

    fg = subs.add_parser("feargreed", help="查看恐惧贪婪指数")
    fg.add_argument("--limit", type=int, default=7)
    fg.add_argument("--history", action="store_true", help="显示历史走势")
    fg.set_defaults(func=cmd_feargreed)

    web_cmd = subs.add_parser("web", help="启动本地网页")
    web_cmd.add_argument("--port", type=int, default=None)
    web_cmd.add_argument("--dual", action="store_true", help="同时启动 8001 和 8002")
    web_cmd.add_argument("--port-a", type=int, default=8001)
    web_cmd.add_argument("--port-b", type=int, default=8002)
    web_cmd.set_defaults(func=cmd_web)
    return parser


def main():
    args = build_parser().parse_args()
    if not getattr(args, "func", None):
        build_parser().print_help()
        return
    try:
        args.func(args)
    except Exception as exc:
        sys.exit("运行失败：{}".format(exc))


if __name__ == "__main__":
    main()
