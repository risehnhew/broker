# Broker Workbench / Broker 工作台

> **English** — AI-assisted US stock trading system based on Interactive Brokers API and MiniMax LLM.
>
> **中文** — 基于 Interactive Brokers API 和 MiniMax 大模型的 AI 辅助美股自动交易系统。

---

## Features / 功能特性

| English | 中文 |
|---------|------|
| AI symbol selection via MiniMax analyzing technical patterns + news sentiment | MiniMax 分析技术形态与新闻情绪，AI 智能选股 |
| SMA crossover strategy (golden cross / death cross) | SMA 快慢线金叉/死叉作为基础交易信号 |
| Risk management: stop-loss, take-profit, max drawdown, daily loss limit | 风控：止损/止盈/最大回撤/单日亏损限额 |
| Sandbox paper trading (virtual fills, no real money) | 沙盘模拟（虚拟撮合，不影响真实账户） |
| Historical backtesting | 历史数据回测 |
| Parameter training (grid search optimal SMA windows, stop/take levels) | 参数训练（网格搜索最优 SMA 窗口、止损止盈组合） |
| Real-time logs (cycle progress, AI analysis, order actions) | 实时日志（轮次进度、AI 分析、下单动作全记录） |
| Light / dark theme | 明/暗主题切换 |

---

## Prerequisites / 环境要求

| | English | 中文 |
|--|---------|------|
| Python | 3.10+ | Python | 3.10+ |
| IB Gateway / TWS | Running on `127.0.0.1:4002` (paper) or `4001` (live) | IB Gateway / TWS | 必须运行在 `127.0.0.1:4002`（纸盆）或 `4001`（实盘）|
| MiniMax API Key | Required for AI symbol selection | MiniMax API Key | 用于 AI 选股分析（可申请 https://www.minimax.io）|
| Network | IBKR port reachable + MiniMax API accessible | 网络 | 本地 IBKR 端口可达 + MiniMax API 可访问 |

---

## Installation / 安装

```bash
# Clone / 克隆仓库
git clone https://github.com/risehnhew/broker.git
cd broker

# Install dependencies / 安装依赖
pip install -r requirements.txt

# Configure / 配置
cp .env.example .env
```

Edit `.env` and fill in the following key parameters: / 编辑 `.env`，填入以下关键参数：

```env
# IBKR Connection / IBKR 连接
IB_HOST=127.0.0.1
IB_PORT=4002                    # Paper: 4002, Live: 4001
IB_CLIENT_ID=7777

# Stock Universe / 股票池
SYMBOLS=AAPL,MSFT               # Symbols to track during auto-trading
STOCK_UNIVERSE=AAPL,MSFT,NVDA,AMZN,META,TSLA,JPM,JNJ,XOM,HD

# AI — MiniMax / AI（MiniMax）
ENABLE_AI_ANALYSIS=true
AI_BASE_URL=https://api.minimaxi.com/v1
AI_API_KEY=your_minimax_key_here
AI_MODEL=MiniMax-M2.7-highspeed
AI_MIN_CONFIDENCE=30            # AI confidence threshold (below = ignore AI signal)
AI_SELECTION_MIN_CONFIDENCE=25  # AI stock selection confidence threshold

# Strategy / 策略参数
FAST_SMA=5
SLOW_SMA=20
STOP_LOSS_PCT=0.03             # 3% stop-loss
TAKE_PROFIT_PCT=0.06          # 6% take-profit

# Trading Hours (ET) / 交易时段（美东时间）
TRADE_START_TIME=09:30
TRADE_END_TIME=16:00
```

---

## IB Gateway / TWS Setup / IB Gateway / TWS 设置

Before trading, IB Gateway or TWS must be configured: / 交易前，IB Gateway 或 TWS 必须完成以下设置：

### 1. Enable Socket API / 开启 Socket API

- **IB Gateway**: `Configure → Settings → API → Enable ActiveX and Socket Clients`
- **TWS**: `Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients`
- Note the port number (paper default: `4002`, live: `4001`)
- Allow local connections: `127.0.0.1`

### 2. Enable Market Data Permissions / 开启市场数据权限

- `Account Management → Market Data Subscriptions`
- Subscribe to at least the symbols you intend to trade

### 3. Close Other Sessions / 关闭其他会话

- Only **one** active session per account at a time
- IBKR mobile app, Client Portal, or other TWS instances will cause API disconnection

---

## Start the Dashboard / 启动仪表盘

```bash
python -m broker.dashboard
```

Terminal will output: / 终端会输出：

```
Dashboard URL: http://127.0.0.1:8765
```

Open in browser: / 浏览器打开：

```
http://127.0.0.1:8765
```

> If port 8765 is occupied, it auto-switches to the next available port.
> / 如果端口 8765 被占用，会自动切换到下一个可用端口。

---

## Usage Workflow / 使用流程

### Step 1: Connect IBKR / 第一步：连接 IBKR

Start IB Gateway / TWS, then click **刷新状态** in the dashboard. Confirm "已连接 127.0.0.1:4002".
/ 启动 IB Gateway / TWS 后，在仪表盘点**刷新状态**，确认"已连接 127.0.0.1:4002"。

---

### Step 2: Sandbox Paper Trading (Recommended First Step) / 第二步：沙盘观察（新手推荐第一步）

Navigate to **🎮 沙盘** tab: / 进入 **🎮 沙盘** 标签：

1. Fill in initial capital (default $10,000) and stock universe / 填写初始本金（默认 $10,000）和股票池
2. Click **启动沙盘** / 点击**启动沙盘**
3. Wait 60 seconds for the first cycle result / 等 60 秒查看第一轮结果

The sandbox runs one cycle every 60 seconds, including: / 沙盘每 60 秒自动运行一轮，包含：

- AI stock selection (MiniMax analyzes all candidates) / AI 选股（MiniMax 分析所有候选股）
- Signal generation (SMA crossover) / 信号生成（SMA 交叉）
- Decision (technical + AI → BUY / SELL / HOLD) / 决策（技术面 + AI → BUY / SELL / HOLD）
- Virtual fills (execute at real-time prices) / 虚拟撮合（以实时价格执行买卖）

**What to watch:** / **看什么：**

- Is the equity curve (colored line) above the baseline ($10,000)? / 净值曲线是否在基准线（$10,000）之上？
- How many BUY / SELL signals are in "本轮分析结论" table? / "本轮分析结论"表格里 BUY / SELL 信号有多少？
- Is the decision reason `signal_confirmed` (technical confirmed) or `low_ai_confidence` (AI confidence too low)? / 决策原因是 `signal_confirmed`（技术面确认）还是 `low_ai_confidence`（AI 置信度不够）？

---

### Step 3: Strategy Experiments / 第三步：策略实验

Navigate to **🧪 实验** tab: / 进入 **🧪 实验** 标签：

| Button / 按钮 | English / 作用 | 中文 / 作用 |
|---------------|----------------|-------------|
| AI 选股 / AI Select | Run one round of MiniMax analysis, view per-stock confidence and recommendations | 跑一轮 MiniMax 分析，查看各股置信度和推荐 |
| 预览 / Preview | Display stock selection results (which stocks selected, reasons) | 显示选股结果（哪些股入选、理由） |
| 模拟 / Simulate | Run strategy on historical data, print per-trade log | 在历史数据上跑一遍策略，打印逐笔交易 |
| 回测 / Backtest | Run full backtest on a single stock, output win rate, return, max drawdown | 对单只股票跑完整回测，输出胜率、收益、最大回撤 |
| 训练 / Train | Grid search for optimal SMA windows + stop/take parameter combinations | 网格搜索最优 SMA 窗口 + 止损/止盈参数组合 |

---

### Step 4: Live Trading (After Sandbox Validation) / 第四步：实盘（沙盘验证后再来）

Navigate to **💰 实盘** tab, click **AI 全自动交易**. / 进入 **💰 实盘** 标签，点**AI 全自动交易**。

> **Warning / 警告：** Real orders, real money, real losses. Only consider live trading after the sandbox shows stable positive returns. / 真实下单，亏损真实资金。务必在沙盘跑出稳定正收益后再考虑实盘。

---

## Keyboard Shortcuts / 仪表盘快捷键

| Key / 按键 | English Action / 功能 | 中文 / 功能 |
|-----------|----------------------|-------------|
| `1` - `6` | Switch tab (Home / Sandbox / Lab / Live / Settings / Logs) | 切换标签页（首页/沙盘/实验/实盘/参数/日志）|
| `T` | Toggle light / dark theme | 切换明/暗主题 |
| `Ctrl+F5` | Force refresh (clear cache) | 强制刷新（清除缓存）|

---

## Module Overview / 模块说明

```
broker/
├── dashboard.py      # Web dashboard (FastAPI + uvicorn) / Web 仪表盘
├── trader.py       # Live trading engine / 实盘交易引擎
├── paper_trader.py # Sandbox paper trading engine / 沙盘模拟引擎
├── simulator.py     # CLI backtest tool / 命令行回测工具
├── backtest.py     # Historical data backtesting / 历史数据回测
├── train.py        # Parameter grid training / 参数网格训练
├── selector.py     # AI stock selection logic / AI 选股逻辑
├── decision.py      # Trading decision engine / 交易决策引擎
├── strategy.py     # SMA crossover strategy / SMA 均线策略
├── analysis.py     # K-line pattern analysis (RSI, volume, support/resistance) / K线形态分析
├── news.py         # News fetching and sentiment analysis / 新闻抓取与情绪分析
├── ai_analysis.py  # MiniMax API client / MiniMax API 调用
├── risk.py         # Risk evaluation / 风控评估
├── ib_client.py    # IBKR API wrapper / IBKR API 封装
├── config.py       # Environment variable parsing / 环境变量解析
├── live_log.py     # In-memory log buffer / 内存日志缓冲区
└── runtime.py      # Background thread manager / 后台线程管理器
```

---

## Run Tests / 运行测试

```bash
python -m pytest tests/ -q
```

---

## File Reference / 文件说明

| File / 文件 | English / 说明 | 中文 / 说明 |
|-------------|--------------|------------|
| `.env` | Private keys and config (never commit) | 私钥和配置（不提交到 Git）|
| `.env.example` | Config template | 配置模板 |
| `trade_history.csv` | Trade history including realized P&L | 交易历史记录（含已实现盈亏）|
| `broker/.dashboard.lock` | Dashboard process lock (auto-managed) | 仪表盘进程锁（自动管理）|
| `downloads/` | IB Gateway installer (not in git, download separately) | IB Gateway 安装包（不提交到 Git，需单独下载）|

---

## Risk Disclaimer / 风险警示

**This project is for technical experimentation and sandbox learning only.** Real trading involves genuine financial risk. Do not use real money before thoroughly validating strategy behavior, risk controls, and market data permissions.

**建议验证顺序 / Recommended validation workflow:**

1. Run sandbox for 1-3 trading days, observe equity curve / 沙盘跑 1-3 个交易日，观察收益曲线
2. Backtest on historical data, confirm win rate and max drawdown are acceptable / 用历史数据回测，验证胜率和最大回撤可接受
3. Train parameters to find the optimal configuration for the current market environment / 训练参数，找到当前市场环境下的最优配置
4. Start with small capital in live mode, scale up only after confirming everything works correctly / 小资金实盘试跑，确认一切正常后再逐步加大仓位

---

## License / 许可证

MIT
