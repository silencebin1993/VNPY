"""数据源：builtin_news（快讯，直接包装 market.news.flash，保持原有格式；SCHEMAS 没有登记 news 的格式）"""
from __future__ import annotations

from .base import DataProvider


class BuiltinNewsProvider(DataProvider):
    name = "builtin_news"
    label = "内置快讯"
    description = "财联社/东方财富/新浪7x24 快讯合并去重（market.news）"
    capabilities = ("news",)

    def fetch_news(self, limit: int = 100) -> list[dict]:
        from ..market import news as news_mod

        return news_mod.flash(limit=limit)
