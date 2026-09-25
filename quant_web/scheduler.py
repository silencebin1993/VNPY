"""
每日自动更新：交易日北京时间 15:35 以后，自动跑一次"一键更新"（更新数据 → 必要时训练 → 预测）。

- 设置里关闭 auto_update 就不跑；
- 面板还是空的（从没下载过数据）时绝不自动开始——首次全量下载要十几分钟，必须由用户在网页上点击；
- 电脑休眠/没开程序错过了几天，下次启动时自动补上（落后超过 30 个交易日就留给用户手动更新）；
- 同一个交易日失败最多重试 3 次，每次间隔 30 分钟；
- 模拟盘：一键更新任务（run_daily）预测完会自动记录当天信号；如果数据已经是最新（比如用户手动点过
  「更新数据」）而当天信号还没记录，就单独提交一个"记录模拟盘信号"任务（同样最多 3 次、间隔 30 分钟）。
- 除权日昨收核对：一键更新任务最后每周一次（jobs.calibrate_weekly，限时 5 分钟、失败不影响任务），不单独调度。
"""
import threading
import time as _time
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from . import config
from . import settings as settings_mod
from .jobs import HEAVY, JOBS, JobManager, Progress, now_text, run_daily, run_paper


RUN_AFTER: time = time(15, 35)          # 收盘数据基本稳定的时间
CHECK_SECONDS: float = 60.0
FIRST_DELAY: float = 15.0               # 启动后先等一会儿，让网页先打开
MAX_ATTEMPTS: int = 3
RETRY_SECONDS: float = 30 * 60
MAX_CATCHUP_DAYS: int = 30
CALENDAR_TTL: float = 3600.0


def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def _weekday_calendar(today: date, days: int = 60) -> list[date]:
    """没有网络时的退化日历：只排除周末"""
    return [today - timedelta(days=i) for i in range(days, -1, -1) if (today - timedelta(days=i)).weekday() < 5]


def _default_calendar() -> list[date]:
    """最近的交易日列表（上证指数K线日期，已收盘的），拿不到时按工作日估算"""
    from .market import history

    fn: Callable | None = getattr(history, "latest_closed_day", None)
    if fn is None:
        return _weekday_calendar(china_now().date())
    _, closed = fn()
    return list(closed)


def _default_last_date() -> date | None:
    from .market import history

    return history.last_date()


def _default_paper_pending(day: date) -> list[str]:
    """day 这一天还需要记录模拟盘信号的策略（现在不能记录、或都记过了时为空）"""
    from . import paper

    return paper.pending_kinds(day)


def target_day(now: datetime, calendar: list[date]) -> date | None:
    """此刻应该已经有数据的最近交易日：当天 15:35 以后算当天，否则算前一个交易日"""
    today: date = now.date()
    cutoff: date = today if now.time() >= RUN_AFTER else today - timedelta(days=1)
    days: list[date] = [d for d in calendar if d <= cutoff]
    return days[-1] if days else None


class Scheduler:
    def __init__(
        self,
        jobs: JobManager = JOBS,
        runner: Callable[[Progress], Any] = run_daily,
        now_fn: Callable[[], datetime] = china_now,
        calendar_fn: Callable[[], list[date]] = _default_calendar,
        last_date_fn: Callable[[], date | None] = _default_last_date,
        paper_fn: Callable[[Progress], Any] | None = None,
        paper_pending_fn: Callable[[date], list[str]] | None = None,
    ) -> None:
        self.jobs: JobManager = jobs
        self.runner: Callable[[Progress], Any] = runner
        self.now_fn: Callable[[], datetime] = now_fn
        self.calendar_fn: Callable[[], list[date]] = calendar_fn
        self.last_date_fn: Callable[[], date | None] = last_date_fn
        self.paper_fn: Callable[[Progress], Any] | None = paper_fn
        self.paper_pending_fn: Callable[[date], list[str]] | None = paper_pending_fn
        self._attempts: dict[Any, list[float]] = {}
        self._calendar: tuple[float, list[date]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_check: str | None = None
        self.last_job_id: str | None = None
        self.message: str = "尚未检查"
        self.target: date | None = None

    # ---------------------------------------------------------------- 判断

    def _get_calendar(self) -> list[date]:
        mono: float = _time.monotonic()
        if self._calendar and mono - self._calendar[0] < CALENDAR_TTL:
            return self._calendar[1]
        try:
            cal: list[date] = self.calendar_fn()
        except Exception:  # noqa: BLE001  网络问题：按工作日估算，稍后再试
            cal = _weekday_calendar(self.now_fn().date())
            self._calendar = (mono - CALENDAR_TTL + 600, cal)     # 10 分钟后重新获取
            return cal
        self._calendar = (mono, cal)
        return cal

    def check(self) -> str | None:
        """检查一次，需要时提交"一键更新"任务，返回 job_id"""
        now: datetime = self.now_fn()
        self.last_check = now.strftime("%Y-%m-%d %H:%M:%S")

        s = settings_mod.load()
        if not s.auto_update:
            self.message = "自动更新已关闭"
            return None
        last: date | None = self.last_date_fn()
        if last is None:
            self.message = "还没有下载过数据，请在网页上点击开始下载（首次约需15分钟）"
            return None

        # 先用工作日粗算，数据已是最新就不用联网查日历
        rough: date | None = target_day(now, _weekday_calendar(now.date(), 10))
        if rough is not None and last >= rough:
            self.target = rough
            self.message = f"数据已是最新（{last:%Y-%m-%d}）"
            return self._check_paper(last)

        calendar: list[date] = self._get_calendar()
        target: date | None = target_day(now, calendar)
        self.target = target
        if target is None or last >= target:
            self.message = f"数据已是最新（{last:%Y-%m-%d}）"
            return self._check_paper(last)

        behind: int = len([d for d in calendar if last < d <= target])
        if behind > MAX_CATCHUP_DAYS:
            self.message = f"数据落后 {behind} 个交易日，请在数据中心手动点击更新"
            return None
        if self.jobs.is_busy(HEAVY):
            self.message = "有其他任务正在运行，稍后再检查"
            return None

        attempts: list[float] = self._attempts.setdefault(target, [])
        mono: float = _time.monotonic()
        if len(attempts) >= MAX_ATTEMPTS:
            self.message = f"{target:%Y-%m-%d} 的自动更新已尝试 {MAX_ATTEMPTS} 次未成功，请手动更新"
            return None
        if attempts and mono - attempts[-1] < RETRY_SECONDS:
            self.message = "上次自动更新未完成，稍后重试"
            return None

        attempts.append(mono)
        self.last_job_id = self.jobs.submit("daily", "收盘后自动更新（数据+预测）", self.runner, group=HEAVY)
        self.message = f"{now_text()} 已自动开始更新 {target:%Y-%m-%d} 的数据"
        return self.last_job_id

    def _check_paper(self, day: date) -> str | None:
        """数据已是最新时：当天模拟盘信号还没记录就提交记录任务"""
        if self.paper_fn is None or self.paper_pending_fn is None:
            return None
        try:
            pending: list[str] = list(self.paper_pending_fn(day))
        except Exception:  # noqa: BLE001  判断失败就不记录，下次再看
            return None
        if not pending or self.jobs.is_busy(HEAVY):
            return None
        attempts: list[float] = self._attempts.setdefault(("paper", day), [])
        mono: float = _time.monotonic()
        if len(attempts) >= MAX_ATTEMPTS or (attempts and mono - attempts[-1] < RETRY_SECONDS):
            return None
        attempts.append(mono)
        self.last_job_id = self.jobs.submit("paper", "记录模拟盘信号", self.paper_fn, group=HEAVY)
        self.message = f"数据已是最新（{day:%Y-%m-%d}），{now_text()} 已自动记录模拟盘信号"
        return self.last_job_id

    # ---------------------------------------------------------------- 线程

    def _loop(self) -> None:
        if self._stop.wait(FIRST_DELAY):
            return
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as e:  # noqa: BLE001  后台检查出错不能让线程退出
                self.message = f"自动更新检查出错：{e}"
            self._stop.wait(CHECK_SECONDS)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quant-web-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def status(self) -> dict:
        running: bool = bool(self._thread and self._thread.is_alive())
        message: str = self.message
        if not running and self.last_check is None:
            message = "这次启动没有开启自动更新线程（--no-scheduler），需要时请手动点「更新数据」"
        return {
            "running": running,
            "last_check": self.last_check,
            "last_job_id": self.last_job_id,
            "target_day": self.target.isoformat() if self.target else None,
            "message": message,
            "run_after": RUN_AFTER.strftime("%H:%M"),
        }


SCHEDULER = Scheduler(paper_fn=run_paper, paper_pending_fn=_default_paper_pending)


def start() -> None:
    SCHEDULER.start()


def stop() -> None:
    SCHEDULER.stop()
