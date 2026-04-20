# Broker Workbench

AI 辅助美股自动交易系统。基于 Interactive Brokers（IBKR）API，用 MiniMax 大模型分析 K 线形态、新闻情绪和技术指标，自动选股下单。

支持**沙盘模拟**（不影响真实账户）和**实盘交易**，提供 Web 仪表盘可视化所有状态。

---

## 功能特性

- **AI 选股** — MiniMax 分析 19 只股票的技术面 + 新闻面，输出置信度和推荐理由
- **均线策略** — SMA 快慢线金叉/死叉作为基础信号
- **风控管理** — 止损/止盈/最大回撤/单日亏损限额
- **沙盘模拟** — 虚拟撮合，不消耗真实资金
- **历史回测** — 用过去 N 天数据验证策略效果
- **参数训练** — 网格搜索最优 SMA 窗口、止损/止盈组合
- **实时日志** — 轮次进度、AI 分析结果、下单动作全记录
- **明/暗主题** — 盯盘舒适度优化

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python | 3.10+ |
| IB Gateway / TWS | 必须运行在 `127.0.0.1:4002`（纸盆）或 `4001`（实盘） |
| MiniMax API Key | 用于 AI 选股分析（可申请 https://www.minimax.io） |
| 网络 | 本地 IBKR 端口可达 + MiniMax API 可访问 |

---

## 安装

```bash
# 克隆仓库
git clone https://github.com/risehnhew/broker.git
cd broker

# 安装依赖
pip install -r requirements.txt

# 配置（复制模板，填入真实参数）
cp .env.example .env
```

编辑 `.env` 文件，填入以下关键参数：

```env
# IBKR 连接
IB_HOST=127.0.0.1
IB_PORT=4002                    # 纸盆用 4002，实盘用 4001
IB_CLIENT_ID=7777

# 股票池
SYMBOLS=AAPL,MSFT               # 自动交易时跟踪的标的
STOCK_UNIVERSE=AAPL,MSFT,NVDA,AMZN,META,TSLA,JPM,JNJ,XOM,HD   # AI 选股的候选池

# AI（MiniMax）
ENABLE_AI_ANALYSIS=true
AI_BASE_URL=https://api.minimaxi.com/v1
AI_API_KEY=your_minimax_key_here
AI_MODEL=MiniMax-M2.7-highspeed
AI_MIN_CONFIDENCE=30            # AI 置信度阈值（低于此值不采纳 AI 信号）
AI_SELECTION_MIN_CONFIDENCE=25  # AI 选股置信度阈值

# 策略参数
FAST_SMA=5
SLOW_SMA=20
STOP_LOSS_PCT=0.03              # 3% 止损
TAKE_PROFIT_PCT=0.06           # 6% 止盈

# 交易时段（美东时间）
TRADE_START_TIME=09:30
TRADE_END_TIME=16:00
```

---

## IB Gateway / TWS 设置

交易前，IB Gateway 或 TWS 必须：

1. **开启 Socket API**：
   - IB Gateway: `Configure → Settings → API → Enable ActiveX and Socket Clients`
   - 记录端口号（纸盆默认 `4002`，实盘 `4001`）
   - 允许本地连接：`127.0.0.1`

2. **开启市场数据权限**：
   - `Account Management → Market Data Subscriptions`
   - 至少订阅你交易的标的（股票、期权等）

3. **关闭其他会话**：
   - 手机 IBKR App、Client Portal 等同时只能有一个会话
   - 同一账户多设备登录会导致 API 连接断开

---

## 启动仪表盘

```bash
python -m broker.dashboard
```

终端会输出：
```
Dashboard URL: http://127.0.0.1:8765
```

浏览器打开 http://127.0.0.1:8765 即可。

> 如果端口 8765 被占用，会自动切换到下一个可用端口。

---

## 使用流程

### 1. 连接 IBKR

启动 IB Gateway / TWS 后，在仪表盘点**刷新状态**，确认"已连接 127.0.0.1:4002"。

### 2. 沙盘观察（新手推荐第一步）

进入 **🎮 沙盘** 标签：

1. 填写初始本金（默认 $10,000）和股票池
2. 点**启动沙盘**
3. 等 60 秒查看第一轮结果

沙盘会每 60 秒自动运行一轮，包含：
- AI 选股（MiniMax 分析所有候选股）
- 信号生成（SMA 交叉）
- 决策（技术面 + AI → BUY / SELL / HOLD）
- 虚拟撮合（以实时价格执行买卖）

**看什么**：
- 净值曲线（橙线）是否在基准线（$10,000）之上
- "本轮分析结论"表格里 BUY / SELL 信号有多少
- 决策原因是 `signal_confirmed`（技术面确认）还是 `low_ai_confidence`（AI 置信度不够）

### 3. 策略实验

进入 **🧪 实验** 标签：

| 按钮 | 作用 |
|------|------|
| AI 选股 | 跑一轮 MiniMax 分析，查看各股置信度和推荐 |
| 预览 | 显示选股结果（哪些股入选、理由） |
| 模拟 | 在历史数据上跑一遍策略，打印逐笔交易 |
| 回测 | 对单只股票跑完整回测，输出胜率、收益、最大回撤 |
| 训练 | 网格搜索最优 SMA 窗口 + 止损/止盈参数组合 |

### 4. 实盘（沙盘验证后再来）

进入 **💰 实盘** 标签，点**AI 全自动交易**。

> 警告：真实下单，亏损真实资金。务必在沙盘跑出稳定正收益后再考虑实盘。

---

## 仪表盘快捷键

| 按键 | 功能 |
|------|------|
| `1` - `6` | 切换标签页（首页/沙盘/实验/实盘/参数/日志） |
| `T` | 切换明/暗主题 |
| `Ctrl+F5` | 强制刷新（清除缓存） |

---

## 模块说明

```
broker/
├── dashboard.py      # Web 仪表盘（FastAPI + uvicorn）
├── trader.py        # 实盘交易引擎
├── paper_trader.py  # 沙盘模拟引擎
├── simulator.py     # 命令行回测工具
├── backtest.py      # 历史数据回测
├── train.py         # 参数网格训练
├── selector.py      # AI 选股逻辑
├── decision.py      # 交易决策引擎
├── strategy.py      # SMA 均线策略
├── analysis.py       # K 线形态分析（RSI、成交量、支撑阻力）
├── news.py          # 新闻抓取与情绪分析
├── ai_analysis.py   # MiniMax API 调用
├── risk.py          # 风控评估
├── ib_client.py     # IBKR API 封装
├── config.py        # 环境变量解析
├── live_log.py      # 内存日志缓冲区
└── runtime.py       # 后台线程管理器
```

---

## 运行测试

```bash
python -m pytest tests/ -q
```

---

## 文件说明

- `.env` — 私钥和配置（不提交到 Git）
- `.env.example` — 配置模板
- `trade_history.csv` — 交易历史记录（含已实现盈亏）
- `broker/.dashboard.lock` — 仪表盘进程锁（自动管理）

---

## 注意事项

**风险警示**：本项目仅供技术验证和沙盘学习使用。实盘交易存在真实亏损风险，策略未经充分验证前不应使用真实资金。

**建议验证顺序**：
1. 沙盘跑 1-3 个交易日，观察收益曲线
2. 用历史数据回测，验证胜率和最大回撤可接受
3. 训练参数，找到当前市场环境下的最优配置
4. 小资金实盘试跑，确认一切正常后再逐步加大仓位

---

## 许可证

MIT
