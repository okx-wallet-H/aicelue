# AI策略引擎 (aicelue)

OKX Agent TradeKit 交易赛 AI 策略引擎 —— 自适应多策略融合交易系统。

## 架构概述

本系统采用 **四子策略加权融合** 的决策框架，结合多周期市场分析、动态杠杆调整、自适应参数进化，实现全自动化的合约交易。

### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 策略引擎 | `app/strategy_engine.py` | 四子策略评分与融合决策 |
| 主控引擎 | `app/main.py` | 交易循环、持仓管理、平仓逻辑 |
| 执行引擎 | `app/execution_engine.py` | OKX API 下单执行 |
| 风控管理 | `app/risk_manager.py` | 止损、熔断、仓位控制 |
| 自适应进化 | `app/evolution.py` | 参数自我迭代优化 |
| 市场数据 | `app/market_data.py` | K线、资金费率、持仓量采集 |
| 市场状态 | `app/market_state.py` | 多周期趋势判断 |
| 指标引擎 | `app/indicator_engine.py` | 技术指标计算 |
| 知识库 | `app/knowledge_base.py` | 交易经验积累 |
| 复盘模块 | `app/review.py` | 每日自动复盘 |
| OKX CLI | `app/okx_cli.py` | OKX命令行接口封装 |

### 四子策略

1. **趋势跟踪 (trend_following)** — EMA多头/空头排列 + ADX趋势强度
2. **均值回归 (mean_reversion)** — 布林带偏离 + RSI超买超卖
3. **突破策略 (breakout)** — 布林带宽度扩张 + 价格突破
4. **动量确认 (momentum_confirmation)** — EMA交叉 + RSI动量

### 多周期分析

- **4H**：判断市场状态（强势上涨/弱势上涨/区间震荡/弱势下跌/强势下跌）
- **1H**：判断节奏与趋势延续性
- **15M**：确认入场与短线反转窗口

### 风控体系

- 单笔止损：1.2%-2.0%（自适应）
- 日亏损熔断：6%
- 总回撤熔断：20%
- 连续亏损减仓/停仓
- 动态杠杆：2x-10x（根据信号强度）

## 部署

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 OKX API Key

# 启动策略引擎
python run_engine.py --loop --interval 300
```

## 比赛信息

- 比赛：OKX Agent TradeKit 交易赛
- 截止：2026/04/23 16:00
- Skill提交截止：2026/04/30 16:00
