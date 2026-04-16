
import requests
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("rootdata_client")

class RootDataClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.rootdata.com/open"
        self.headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }
        # 模拟数据兜底
        self.mock_data = {
            "BTC-USDT-SWAP": {
                "name": "Bitcoin",
                "heat_index": 95.5,
                "heat_rank": 1,
                "influence_index": 98.0,
                "growth_index": 0.5,
                "market_cap": 1200000000000,
                "funding": "N/A"
            },
            "SOL-USDT-SWAP": {
                "name": "Solana",
                "heat_index": 88.2,
                "heat_rank": 5,
                "influence_index": 85.0,
                "growth_index": 2.1,
                "market_cap": 60000000000,
                "funding": "250M"
            }
        }

    def get_project_metrics(self, symbol: str) -> Dict[str, Any]:
        """
        获取项目核心指标：热度、影响力、增长指数等。
        如果 API 不可用或没有 API Key，使用模拟数据兜底。
        """
        if not self.api_key:
            return self.mock_data.get(symbol, self._default_metrics(symbol))

        # 映射 symbol 到 RootData project_id (示例映射)
        symbol_to_id = {
            "BTC-USDT-SWAP": 12,
            "SOL-USDT-SWAP": 145
        }
        project_id = symbol_to_id.get(symbol)
        if not project_id:
            return self._default_metrics(symbol)

        url = f"{self.base_url}/get_item"
        payload = {
            "project_id": project_id,
            "include_team": False,
            "include_investors": True
        }
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=5)
            if response.status_code == 200:
                data = response.json().get("data", {})
                metrics = {
                    "name": data.get("project_name"),
                    "heat_index": data.get("heat", 0),
                    "heat_rank": data.get("heat_rank", 999),
                    "influence_index": data.get("influence", 0),
                    "growth_index": data.get("growth_24h", 0), # 假设字段
                    "market_cap": data.get("market_cap", 0),
                    "funding": data.get("total_funding", "N/A")
                }
                return metrics
            else:
                logger.warning(f"RootData API error: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching RootData for {symbol}: {e}")
            
        # 降级方案：使用模拟数据
        return self.mock_data.get(symbol, self._default_metrics(symbol))

    def _default_metrics(self, symbol: str) -> Dict[str, Any]:
        return {
            "name": symbol,
            "heat_index": 50.0,
            "heat_rank": 500,
            "influence_index": 50.0,
            "growth_index": 0.0,
            "market_cap": 0,
            "funding": "N/A"
        }
