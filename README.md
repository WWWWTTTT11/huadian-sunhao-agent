在线演示网址：https://ebeec7b3bd494aaf82160c644b8bcc5e.app.workbuddy.link/
# 盘口损耗研判智能体 · huadian-sunhao-agent

一个围绕交易前风险检查的本地数据分析工具，提供盘口滑点磨损测算与恐慌贪婪指数分析。

## 核心功能

- **滑点磨损测算**：读取 Binance 公开现货盘口深度，按 1,000 / 5,000 / 10,000 / 30,000 USDT 模拟吃单。
- **交易方向支持**：支持买入和卖出，计算理论成交均价、盘口滑点、预估磨损、使用档位和未成交金额。
- **流动性研判**：根据不同资金量的滑点变化识别潜在临界金额，并给出低、中、高、极高风险评级。
- **情绪联动**：读取 alternative.me 恐慌贪婪指数及近 7 日历史，判断情绪回暖、转弱或持平。
- **可视化报告**：网页展示盘口深度图、滑点结果、情绪曲线、历史记录和可复制 Markdown 报告。

## 本地启动

项目仅依赖 Python 标准库，不需要安装第三方 Python 包或 API Key。

### 单端口

```bash
python agent.py web --port 8001
```

浏览器打开：

```text
http://127.0.0.1:8001
```

### 双端口

```bash
python agent.py web --dual --port-a 8001 --port-b 8002
```

浏览器打开任意一个：

```text
http://127.0.0.1:8001
http://127.0.0.1:8002
```

双端口只是同一台电脑上的两个监听入口，并不等于自动获得公网访问能力。若要让其他人访问，需要部署到有公网地址的主机，并配置端口、防火墙和网络访问权限。

## 终端用法

```bash
# 买入 10,000 USDT 的 BTC
python agent.py slippage --symbol BTCUSDT --side buy --amount 10000

# 卖出 30,000 USDT 的 ETH
python agent.py slippage --symbol ETHUSDT --side sell --amount 30000

# 查看近 7 日恐慌贪婪指数
python agent.py feargreed --history
```

## 项目结构

```text
huadian-sunhao-agent/
├── agent.py                 # CLI 入口
├── sources.py               # 公开数据接口与滑点计算
├── web.py                   # 标准库本地 Web 服务与 API
├── static/
│   └── index.html           # 盘口损耗研判页面
└── data/
    └── slippage_history.json # 本地历史测算记录
```

## API

```text
GET /
GET /api/history
GET /api/analyze?symbol=BTCUSDT&side=buy&amount=10000
```

## 数据来源与边界

- 盘口深度：Binance Vision 公开接口
- 恐慌贪婪指数：alternative.me 公开接口
- 无需 API Key，不自动交易
- 静态盘口估算不包含手续费、网络延迟、撤单、成交顺序变化和突发波动
- 数据接口可能受网络环境、服务限流或临时不可用影响
- 本项目仅供学习和研究，不构成投资建议
