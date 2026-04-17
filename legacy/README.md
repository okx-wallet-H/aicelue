# Legacy 目录

此目录存放的是旧版/备用引擎实现，**不是当前运行主链路**。

当前唯一主入口为：`run_engine.py -> app/main.py`（LLM 多标的决策）。

以下文件均为历史遗留，仅供参考，请勿直接运行或 import：
- `main.py` — 旧版自适应/战役管理主引擎
- `strategy_engine.py` — 旧版四子策略引擎（非 LLM）
- `execution_engine.py` — 旧版执行引擎
- `config.py` — 旧版配置（参数与 `app/config.py` 不同）
