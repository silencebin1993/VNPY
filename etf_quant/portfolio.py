"""
我的持仓：保存在 workspace/持仓.csv，可以直接用 Excel 打开修改。
"""
import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import CODE_MAP, HOLDINGS_FILE, NAV_FILE, WORKSPACE

CASH_CODE: str = "现金"
HEADER: list[str] = ["代码", "名称", "数量", "说明"]


@dataclass
class Holdings:
    cash: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)     # 代码 -> 份额

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(v * prices.get(code, 0.0) for code, v in self.positions.items())

    def total_value(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


def holdings_exist(path: Path = HOLDINGS_FILE) -> bool:
    return path.exists()


def _read_rows(path: Path) -> list[dict]:
    """读取CSV。Excel 在中文系统上另存时默认是 GBK 编码，也要能读。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码：{path}，请用 Excel 另存为“CSV UTF-8”格式")


def load_holdings(path: Path = HOLDINGS_FILE) -> Holdings:
    holdings = Holdings()
    if not path.exists():
        return holdings

    for row in _read_rows(path):
        code: str = (row.get("代码") or "").strip()
        raw: str = (row.get("数量") or "0").replace(",", "").strip() or "0"
        if not code:
            continue
        try:
            amount: float = float(raw)
        except ValueError as e:
            raise ValueError(f"持仓文件中 {code} 的数量“{raw}”不是数字，请修改 {path}") from e

        if code == CASH_CODE:
            holdings.cash = amount
        else:
            code = code.zfill(6)
            if amount:
                holdings.positions[code] = holdings.positions.get(code, 0) + int(amount)
    return holdings


def save_holdings(holdings: Holdings, path: Path = HOLDINGS_FILE) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerow([CASH_CODE, "可用资金", f"{holdings.cash:.2f}", "单位：元"])
        for code, volume in sorted(holdings.positions.items()):
            if not volume:
                continue
            etf = CODE_MAP.get(code)
            writer.writerow([code, etf.name if etf else "", volume, "单位：份" if etf else "不在策略范围内，程序不会处理"])


def append_nav(total: float, cash: float, note: str, day: date, path: Path = NAV_FILE) -> None:
    """记录一次账户总资产，用于跟踪实盘/模拟盘表现"""
    new_file: bool = not path.exists()
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["日期", "总资产", "现金", "说明"])
        writer.writerow([day.isoformat(), f"{total:.2f}", f"{cash:.2f}", note])


def load_nav(path: Path = NAV_FILE) -> list[dict]:
    if not path.exists():
        return []
    return _read_rows(path)
