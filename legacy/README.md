# Legacy 目录说明

本目录存放已废弃的旧版根目录模块（`main.py`、`execution_engine.py`、`strategy_engine.py`、`config.py`）。

这些文件是另一套更早的实现（带战役管理/成交同步/自适应进化），**不应被当前运行时引用**。

## 当前唯一入口

```
python run_engine.py [--execute] [--loop] [--interval N]
```

所有活跃代码均在 `app/` 目录下。如需参考旧版逻辑，请在此目录查看，但勿在生产环境中 import。
