# OKX交易赛 TODO（比赛截止：2026/04/23 16:00）

## 已完成
- [x] execute_orders开关打开（从False改True）
- [x] 接入大模型LLM分析模块（通义千问 qwen-plus-latest）
- [x] 服务器配置OPENAI_API_KEY和BASE_URL
- [x] 修复JSON解析问题（通义千问返回markdown代码块）
- [x] RootData API Key配置到服务器.env
- [x] OKX API凭据存入GitHub Repository Secrets
- [x] 监控面板上线：http://47.239.244.186/monitor/

## 进行中
- [ ] 制定6天冲刺计划（4/17-4/23）
- [ ] 把比赛倒计时和冲刺规划写进策略prompt
- [ ] 凌晨2点复盘任务：调取10天数据喂给大模型分析

## 待办（策略优化）
- [ ] 降低信号门槛或优化自适应门槛算法，避免一直HOLD不交易
- [ ] 震荡市优化：ADX低于20时增加布林带高抛低吸策略
- [ ] 增加备选数据源（Coinglass/Binance资金费率）替代RootData情绪指标
- [ ] 回测分析过去8天数据，看错过了多少B级/A级信号
- [ ] 已有同向持仓时的加仓逻辑优化
- [ ] 精简喂给大模型的数据量，降低响应延迟（目前21秒）

## 待办（运维）
- [ ] 把SSH登录服务器做成Skill，提高子任务效率
- [ ] 每日定时检查策略运行状态（早9点、下午3点、晚9点）
- [ ] 每日复盘简报：当日收益、排名变化、策略调整、明日计划
- [ ] 每日推文文案+配图发给老板发推特

## 待办（Skill提交，截止：2026/04/30 16:00）
- [ ] 编写完整Skill文档，展示AI决策、自我进化、复盘机制
- [ ] 整理策略代码和文档，准备提交

## 凭证信息（子任务参考）
- OKX交易API：存在GitHub仓库 okx-wallet-H/aicelue 的 Repository Secrets（OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE）
- 通义千问API：存在服务器 /root/okx_agent_tradekit/.env（OPENAI_API_KEY / OPENAI_BASE_URL）
- RootData API：存在服务器 /root/okx_agent_tradekit/.env（ROOTDATA_API_KEY）
- 阿里云服务器：47.239.244.186，用户root
- 监控面板：http://47.239.244.186/monitor/
- PM2进程名：okx-agent-tradekit
