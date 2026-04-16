from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(slots=True)
class ReasoningChain:
    market_state: str
    symbol_selection: str
    rhythm_1h: str
    entry_15m: str
    crowding: str
    orderbook: str
    final_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        return (
            f"1. 市场状态：{self.market_state}\n"
            f"2. 标的选择：{self.symbol_selection}\n"
            f"3. 1H节奏：{self.rhythm_1h}\n"
            f"4. 15M入场：{self.entry_15m}\n"
            f"5. 拥挤度：{self.crowding}\n"
            f"6. 盘口质量：{self.orderbook}\n"
            f"7. 最终行动：{self.final_action}"
        )
