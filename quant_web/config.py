"""路径与全局常量"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT: Path = Path(__file__).parent
WORKSPACE: Path = Path(os.environ.get("QUANT_WEB_WORKSPACE") or ROOT.joinpath("workspace"))

STOCK_LAB: Path = WORKSPACE.joinpath("stock_lab")          # 全市场数据
PANEL_DIR: Path = STOCK_LAB.joinpath("daily")              # 日线面板，按年分文件 {YYYY}.parquet
UNIVERSE_FILE: Path = STOCK_LAB.joinpath("universe.parquet")
POOLS_DIR: Path = STOCK_LAB.joinpath("pools")              # 涨停池存档 {kind}/{YYYYMMDD}.parquet
MODEL_DIR: Path = WORKSPACE.joinpath("models")             # {kind}.txt {kind}_meta.json {kind}_oos.parquet
CACHE_DIR: Path = WORKSPACE.joinpath("cache")              # 短期缓存（个股资料、新闻等）
SETTINGS_FILE: Path = WORKSPACE.joinpath("settings.json")
WATCHLIST_FILE: Path = WORKSPACE.joinpath("watchlist.json")
STATIC_DIR: Path = ROOT.joinpath("static")

HOST: str = "127.0.0.1"
PORT: int = 8765

HISTORY_START: str = "2019-01-01"                          # 面板起点（特征从 2020 年开始有效）

CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")


def ensure_dirs() -> None:
    for path in [WORKSPACE, STOCK_LAB, PANEL_DIR, POOLS_DIR, MODEL_DIR, CACHE_DIR]:
        path.mkdir(parents=True, exist_ok=True)
