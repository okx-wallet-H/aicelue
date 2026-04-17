# AI策略引擎 (aicelue)

OKX Agent TradeKit 交易赛 AI 策略引擎 —— LLM 多标的决策 + 自适应风控执行系统。

## 唯一主入口

```
run_engine.py  →  app/main.py（LLMAnalyzer 多标的决策）
```

> ⚠️ 根目录下曾存在的 `main.py / strategy_engine.py / execution_engine.py / config.py` 已移入 `legacy/`，**不是运行主链路**，请勿直接 import 或运行。

## 快速启动

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填写 OKX API Key 与 LLM 配置

# 分析模式（只输出决策，不下单）
python run_engine.py

# 循环分析模式（每 4 小时一次，不下单）
python run_engine.py --loop

# ⚠️ 真实下单（必须同时满足两个条件）：
# 1. 环境变量 TRADING_ENABLED=true
# 2. 传入 --execute 参数
TRADING_ENABLED=true python run_engine.py --execute --loop
```

## 交易开关说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `TRADING_ENABLED` | `false` | **必须设为 true 才允许真实下单**；缺失时 `--execute` 会被自动降级为分析模式 |
| `OKX_USE_DEMO` | `false` | 设为 true 使用 OKX 模拟盘 |
| `LLM_ENABLED` | `true` | 设为 false 禁用 LLM，所有决策降级为 SKIP |

## 架构概述

本系统采用 **LLM 多标的决策 + 执行安全校验** 的框架，结合多周期市场分析、动态风控，实现全自动化的合约交易。

### 核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 策略引擎 | `app/strategy_engine.py` | LLM 决策包装、schema 校验、action 规范化 |
| 主控引擎 | `app/main.py` | 交易循环、持仓管理、平仓验证、风控 |
| 执行引擎 | `app/execution_engine.py` | OKX 下单执行、SL/TP 强校验、保护性平仓 |
| 风控管理 | `app/risk_manager.py` | 止损、熔断、仓位控制 |
| 自适应进化 | `app/evolution.py` | 参数自我迭代优化 |
| 市场数据 | `app/market_data.py` | K线、资金费率、持仓量采集 |
| 市场状态 | `app/market_state.py` | 多周期趋势判断 |
| 指标引擎 | `app/indicator_engine.py` | 技术指标计算 |
| 知识库 | `app/knowledge_base.py` | 交易经验积累 |
| 复盘模块 | `app/review.py` | 每日自动复盘 |
| OKX CLI | `app/okx_cli.py` | OKX 命令行接口封装 |

### Action 枚举

执行端支持以下 action（均来自 LLM 输出后经 `StrategyEngine._normalize_decision` 校验）：

| Action | 说明 |
|--------|------|
| `OPEN_LONG` | 做多开仓 |
| `OPEN_SHORT` | 做空开仓 |
| `CLOSE_LONG` | 平多仓 |
| `CLOSE_SHORT` | 平空仓 |
| `CLOSE` | 自动根据当前持仓方向映射为 CLOSE_LONG / CLOSE_SHORT |
| `HOLD` | 持仓不动 |
| `SKIP` | 跳过，不操作 |

非预期 action 会被降级为 `SKIP` 并记录 warning。

### 执行安全保障

- **止损强校验**：SL 挂单失败 → 立即保护性市价平仓，避免裸仓
- **平仓后验证**：`verify_position_closed()` 确认净仓位归零；验证失败则阻止本轮再次开仓
- **单笔风险上限**：1.5%（比赛后期保守控制）
- **总保证金上限**：账户权益 × 60%

### 风控体系

- 单笔止损：1.5%（风险预算约束）
- 日亏损熔断：15%
- 总回撤熔断：25%
- 连续亏损减仓/停仓
- 动态杠杆：1x-15x（LLM 建议，执行端校验范围）

## 比赛期策略建议

> 比赛剩余约 5 天，优先"控制回撤"而非"追求高收益"

1. **只做强结构信号**：4H/1H EMA 同向排列 + ADX 有效时才允许 A 级仓位；中性行情降级或不做
2. **仓位分级**（保证金占权益比）：
   - A 级（结构最强）：20%～30%，杠杆 5～10
   - B 级（次强）：10%～15%
   - C 级（试探）：5%，错了立即撤
3. **同时最多 2 个标的持仓**：避免三开导致相关性回撤
4. **单笔风险从 2% 下调至 1%～1.5%**：通过多做高质量机会提升收益，而非靠大风险博
5. **资金费率拥挤 + 极端波动同时触发时禁止新开仓**；只触发其一时降级仓位

## 比赛信息

- 比赛：OKX Agent TradeKit 交易赛
- 截止：2026/04/23 16:00
- Skill提交截止：2026/04/30 16:00

