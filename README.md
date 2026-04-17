# AI策略引擎 (aicelue)

OKX Agent TradeKit 交易赛 AI 策略引擎 —— 自适应多策略融合交易系统。

## 唯一入口

```
python run_engine.py [--execute] [--loop] [--interval N]
```

> **所有活跃代码均在 `app/` 目录下。**  
> 根目录曾有同名旧版文件（`main.py / execution_engine.py / strategy_engine.py / config.py`），已移入 `legacy/` 目录存档，**请勿在生产环境中引用 legacy/ 下的模块**。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制并编辑环境变量
cp .env.example .env
# 在 .env 中填入 OKX API Key 和 LLM API Key

# 3. Demo 模式试运行（推荐先跑 Demo）
OKX_USE_DEMO=true python run_engine.py

# 4. 真实交易（需要显式启用）
# 在 .env 中设置：
#   TRADING_ENABLED=true
#   OKX_USE_DEMO=false（或删除此行）
python run_engine.py --execute --loop --interval 14400
```

## 交易开关（重要！）

策略引擎默认**不会下单**，即使传入 `--execute`。必须同时满足：

1. 命令行传入 `--execute`
2. 环境变量 `TRADING_ENABLED=true`

如果 `TRADING_ENABLED` 未设置或为 `false`，引擎会记录警告并跳过所有下单操作。

**强烈建议**先在 Demo 模式（`OKX_USE_DEMO=true`）下完整验证策略运行，再切换到真实交易。

## Demo vs 真实 模式

| 配置 | 说明 |
|------|------|
| `OKX_USE_DEMO=true` | 使用 OKX 模拟盘（Testnet），资金与订单互不影响真实账户 |
| `OKX_USE_DEMO=false` | 真实交易，需要配合 `TRADING_ENABLED=true` |

## 架构概述

本系统采用 **LLM 多标的 AI 决策** 的框架，结合多周期市场分析、动态杠杆调整、风控护栏，实现全自动化的合约交易。

### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 策略引擎 | `app/strategy_engine.py` | LLM AI 决策封装 |
| 主控引擎 | `app/main.py` | 交易循环、持仓管理、平仓逻辑、风控护栏 |
| 执行引擎 | `app/execution_engine.py` | OKX CLI 下单执行、止损/止盈校验、保护性平仓 |
| 风控管理 | `app/risk_manager.py` | 止损、熔断、仓位控制 |
| 市场数据 | `app/market_data.py` | K线、资金费率、持仓量采集 |
| 指标引擎 | `app/indicator_engine.py` | 技术指标计算 |
| LLM 分析 | `app/llm_analyzer.py` | 大模型决策分析 |
| 知识库 | `app/knowledge_base.py` | 交易经验积累 |
| OKX CLI | `app/okx_cli.py` | OKX 命令行接口封装 |

### 支持的 Action 枚举

| Action | 说明 |
|--------|------|
| `OPEN_LONG` | 开多仓 |
| `OPEN_SHORT` | 开空仓 |
| `CLOSE_LONG` | 平多仓 |
| `CLOSE_SHORT` | 平空仓 |
| `CLOSE` | 根据当前持仓方向平仓（无持仓则无操作） |
| `HOLD` | 持仓不动 |
| `SKIP` | 跳过本轮，不做操作 |

LLM 输出的未知 action 值会自动规范化为 `SKIP`。

### 执行安全保障

- **止损单强制校验**：开仓后必须验证止损单挂单成功（`sCode == "0"`）；失败时立即保护性平仓，避免裸仓。
- **平仓验证**：平仓后自动查询持仓，若仍有余仓则重试一次；仍不归零则记录高危告警。
- **杠杆上限**：LLM 输出的杠杆超过 `MAX_LEVERAGE_CAP`（默认 15x）时自动截断。
- **单笔风险保护**：单笔交易风险不超过 `MAX_TRADE_RISK_PCT`（默认 1.5%）。
- **总仓位保护**：所有标的 `position_pct` 之和超出 `TOTAL_MARGIN_CAP_RATIO`（默认 60%）时自动按比例缩减。

### 风控体系

- 单笔止损：≤1.5%（默认，可通过 `MAX_TRADE_RISK_PCT` 调整）
- 总保证金上限：≤60% 账户权益
- 杠杆上限：15x
- 止损挂单失败 → 保护性平仓

## 环境变量

详见 `.env.example`，关键变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TRADING_ENABLED` | `false` | 交易总开关，必须为 `true` 才允许真实下单 |
| `OKX_USE_DEMO` | `false` | Demo 模式开关 |
| `OKX_CLI_PATH` | `/usr/bin/okx` | OKX CLI 可执行文件路径 |
| `MAX_TRADE_RISK_PCT` | `0.015` | 单笔最大风险比例 |
| `TOTAL_MARGIN_CAP_RATIO` | `0.60` | 总保证金上限比例 |
| `MAX_LEVERAGE_CAP` | `15` | 杠杆上限 |

## 比赛信息

- 比赛：OKX Agent TradeKit 交易赛
- 截止：2026/04/23 16:00
- Skill提交截止：2026/04/30 16:00
