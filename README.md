# AI策略引擎 (aicelue)

OKX Agent TradeKit 交易赛 AI 策略引擎 —— AI 唯一决策、2 小时 UTC 整点调度、执行与风控闭环系统。

## 架构概述

本系统采用 **AI 唯一决策 + 交易所原生风控兜底** 的主链路，围绕 BTC/ETH/SOL 三标的的 4H、1H、15M 多周期数据完成统一分析，并在执行层强制校验止损、移动止盈与真实成交价风险。

### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 调度引擎 | `run_engine.py` | 严格对齐 UTC 2 小时整点，且只允许单实例运行 |
| 策略引擎 | `app/strategy_engine.py` | 承接 AI 决策并规范动作枚举 |
| 主控引擎 | `app/main.py` | 交易循环、持仓管理、平仓与预算闭环 |
| 执行引擎 | `app/execution_engine.py` | OKX 下单执行、止损强校验与强制平仓 |
| 风控管理 | `app/risk_manager.py` | 熔断、仓位约束与风险状态同步 |
| LLM分析器 | `app/llm_analyzer.py` | 通义千问主力、DeepSeek 备用的双链路 AI 决策 |
| 市场数据 | `app/market_data.py` | K线、资金费率、持仓量与订单簿采集 |
| 指标引擎 | `app/indicator_engine.py` | 技术指标计算 |
| 知识库 | `app/knowledge_base.py` | 决策记录与状态持久化 |
| OKX CLI | `app/okx_cli.py` | OKX 命令行接口封装与执行错误分级 |

### 多周期分析

- **4H**：判断市场状态（强势上涨/弱势上涨/区间震荡/弱势下跌/强势下跌）
- **1H**：判断节奏与趋势延续性
- **15M**：确认入场与短线反转窗口

### 风控体系

- 单笔风险上限：真实成交价口径不超过权益的 2%
- 止损要求：开仓成功后必须确认交易所原生止损算法单回执成功
- 止损兜底：止损挂单失败立即反向市价强平
- 调度要求：仅在 UTC 双数小时整点执行一次
- 熔断要求：日亏损、总回撤与连续亏损达到阈值即停止新开仓

## 部署

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 OKX API Key

# 启动策略引擎
python run_engine.py --loop --execute
```

## 比赛信息

- 比赛：OKX Agent TradeKit 交易赛
- 截止：2026/04/23 16:00
- Skill提交截止：2026/04/30 16:00
