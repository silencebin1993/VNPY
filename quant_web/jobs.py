"""
后台任务：耗时操作（更新数据、训练模型）放在线程里跑，网页轮询进度和日志。

- 同名任务正在运行时再提交，直接返回已有任务（单实例）；
- group 相同的任务排队依次执行（"heavy"：数据更新/训练/一键更新，避免同时读写面板）；
- 日志收集：进度消息、任务线程里的 logging 记录和 print 输出、出错时的调用栈末尾。
- 一键更新（run_daily）最后每周一次核对除权除息日的昨收（calibrate_weekly → history.calibrate_ex_rights，
  限时 CALIBRATE_TIMEOUT 秒，没查完的下次接着查；失败不影响任务结果）。
"""
import json
import logging
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from . import config


Progress = Callable[[float, str], None]

MAX_LOGS: int = 200
CALIBRATE_EVERY_DAYS: int = 7           # 除权日昨收核对：每周一次
CALIBRATE_TIMEOUT: float = 300.0        # 每次最多查这么多秒（没查完的下次续查）
CALIBRATE_STAMP: str = "ex_rights_calibrated.json"      # STOCK_LAB 下，记录上次核对的时间与结果
MAX_HISTORY: int = 50
TRACE_LINES: int = 15
HEAVY: str = "heavy"

_current = threading.local()           # 当前线程正在执行的任务


def now_text() -> str:
    return datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def friendly_error(e: BaseException) -> str:
    """异常 → 给普通用户看的一句话"""
    text: str = str(e).strip() or type(e).__name__
    if isinstance(e, ModuleNotFoundError):
        return f"缺少功能模块 {e.name or text}，程序可能还没有更新完整"
    if isinstance(e, ConnectionError | TimeoutError):
        return f"网络连接失败：{text}"
    if isinstance(e, MemoryError):
        return "内存不足，请关闭其他程序后重试"
    return text


class Job:
    def __init__(self, name: str, title: str, group: str | None) -> None:
        self.id: str = uuid.uuid4().hex[:12]
        self.name: str = name
        self.title: str = title
        self.group: str | None = group
        self.status: str = "running"
        self.queued: bool = False
        self.progress: float = 0.0
        self.message: str = "准备开始……"
        self.logs: deque[str] = deque(maxlen=MAX_LOGS)
        self.result: Any = None
        self.error: str | None = None
        self.started_at: str = now_text()
        self.finished_at: str | None = None
        self._t0: float = time.monotonic()
        self._t1: float | None = None
        self._line: str = ""                # print 输出中尚未换行的部分

    def log(self, text: str) -> None:
        stamp: str = datetime.now(config.CHINA_TZ).strftime("%H:%M:%S")
        for line in str(text).replace("\r", "\n").splitlines():
            if line.strip():
                self.logs.append(f"[{stamp}] {line.rstrip()}")

    def report(self, fraction: float, message: str = "") -> None:
        """传给任务函数的 progress(fraction, message)"""
        try:
            value: float = float(fraction)
        except (TypeError, ValueError):
            value = self.progress
        if value == value:                   # 排除 NaN
            self.progress = min(max(value, 0.0), 1.0)
        if message and message != self.message:
            self.message = message
            self.log(message)

    def to_dict(self, max_logs: int = MAX_LOGS) -> dict:
        end: float = self._t1 if self._t1 is not None else time.monotonic()
        logs: list[str] = list(self.logs)
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "queued": self.queued,
            "progress": round(self.progress, 4),
            "message": self.message,
            "logs": logs[-max_logs:] if max_logs else [],
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(end - self._t0, 1),
        }


class _JobLogHandler(logging.Handler):
    """把任务线程里的 logging 记录写进该任务的日志"""

    def emit(self, record: logging.LogRecord) -> None:
        job: Job | None = getattr(_current, "job", None)
        if job is None:
            return
        try:
            prefix: str = "" if record.levelno < logging.WARNING else ("[警告] " if record.levelno < logging.ERROR else "[错误] ")
            job.log(prefix + record.getMessage())
        except Exception:  # noqa: BLE001  日志不能影响任务
            pass


class _StreamRouter:
    """替换 sys.stdout/stderr：任务线程里的输出进任务日志，其他线程照常输出"""

    def __init__(self, stream: Any) -> None:
        self._stream: Any = stream

    def write(self, text: str) -> int:
        job: Job | None = getattr(_current, "job", None)
        if job is None:
            try:
                return self._stream.write(text)
            except (ValueError, OSError):
                return len(text)
        data: str = job._line + str(text).replace("\r", "\n")
        *lines, job._line = data.split("\n")
        for line in lines:
            job.log(line)
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (ValueError, OSError, AttributeError):
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _install_capture() -> None:
    root: logging.Logger = logging.getLogger()
    if not any(isinstance(h, _JobLogHandler) for h in root.handlers):
        handler = _JobLogHandler(level=logging.INFO)
        root.addHandler(handler)
        logging.getLogger("quant_web").setLevel(logging.INFO)
    for name in ("stdout", "stderr"):
        stream: Any = getattr(sys, name)
        if stream is not None and not isinstance(stream, _StreamRouter):
            setattr(sys, name, _StreamRouter(stream))


class JobManager:
    def __init__(self, capture_output: bool = True) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._groups: dict[str, threading.Lock] = {}
        self._capture: bool = capture_output

    def submit(
        self,
        name: str,
        title: str,
        fn: Callable[[Progress], Any],
        group: str | None = None,
    ) -> str:
        """启动后台任务，返回 job_id；同名任务正在运行时返回已有任务的 id"""
        with self._lock:
            for job in self._jobs.values():
                if job.name == name and job.status == "running":
                    return job.id
            job = Job(name, title, group)
            self._jobs[job.id] = job
            self._trim()
        if self._capture:
            _install_capture()
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{name}", daemon=True)
        thread.start()
        return job.id

    def _group_lock(self, group: str) -> threading.Lock:
        with self._lock:
            return self._groups.setdefault(group, threading.Lock())

    def _run(self, job: Job, fn: Callable[[Progress], Any]) -> None:
        _current.job = job
        lock: threading.Lock | None = self._group_lock(job.group) if job.group else None
        acquired: bool = False
        status: str = "failed"          # 最后才写回 job.status，保证轮询到"结束"时结果和日志都已齐全
        try:
            if lock is not None:
                acquired = lock.acquire(blocking=False)
                if not acquired:
                    job.queued = True
                    job.report(0.0, "排队中：等前一个任务完成后自动开始")
                    acquired = lock.acquire()
                    job.queued = False
            job.log(f"开始：{job.title}")
            job.result = fn(job.report)
            job.progress = 1.0
            if job.message in ("", "准备开始……") or job.message.startswith("排队中"):
                job.message = "已完成"
            job.log(f"完成（用时 {time.monotonic() - job._t0:.0f} 秒）")
            status = "done"
        except Exception as e:  # noqa: BLE001  任务出错要记录下来展示给用户，不能让线程静默退出
            job.error = friendly_error(e)
            job.message = f"失败：{job.error}"
            job.log(f"[错误] {type(e).__name__}: {e}")
            tail: list[str] = traceback.format_exc().rstrip().splitlines()[-TRACE_LINES:]
            for line in tail:
                job.logs.append("    " + line)
        finally:
            if job._line.strip():
                job.log(job._line)
                job._line = ""
            if lock is not None and acquired:
                lock.release()
            if status == "failed" and job.error is None:      # BaseException 之类的意外退出
                job.error = "任务意外中止"
            job._t1 = time.monotonic()
            job.finished_at = now_text()
            _current.job = None
            job.status = status

    def _trim(self) -> None:
        finished: list[str] = [k for k, j in self._jobs.items() if j.status != "running"]
        for key in finished[: max(len(self._jobs) - MAX_HISTORY, 0)]:
            del self._jobs[key]

    def get(self, job_id: str) -> dict | None:
        job: Job | None = self._jobs.get(job_id)
        return job.to_dict() if job else None

    def running(self) -> list[dict]:
        with self._lock:
            jobs: list[Job] = [j for j in self._jobs.values() if j.status == "running"]
        return [j.to_dict(max_logs=5) for j in jobs]

    def latest(self, name: str) -> dict | None:
        with self._lock:
            jobs: list[Job] = [j for j in self._jobs.values() if j.name == name]
        return jobs[-1].to_dict() if jobs else None

    def is_busy(self, group: str = HEAVY) -> bool:
        with self._lock:
            return any(j.status == "running" and j.group == group for j in self._jobs.values())

    def wait(self, job_id: str, timeout: float = 30.0) -> dict | None:
        """等待任务结束（测试和命令行用）"""
        deadline: float = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job: Job | None = self._jobs.get(job_id)
            if job is None or job.status != "running":
                break
            time.sleep(0.05)
        return self.get(job_id)

    def list(self, max_logs: int = 20) -> list[dict]:
        """最近的任务，新的在前（放在类末尾：方法名 list 会遮住内置 list）"""
        with self._lock:
            jobs: list[Job] = list(self._jobs.values())
        return [j.to_dict(max_logs) for j in reversed(jobs)]


JOBS = JobManager()


# ---------------------------------------------------------------- 常用任务

def scaled(progress: Progress | None, lo: float, hi: float) -> Progress:
    """把子步骤的 0~1 进度映射到总进度的 [lo, hi] 区间"""
    def report(fraction: float, message: str = "") -> None:
        if progress is not None:
            try:
                f: float = min(max(float(fraction), 0.0), 1.0)
            except (TypeError, ValueError):
                f = 0.0
            progress(lo + (hi - lo) * f, message)
    return report


def _china_today() -> date:
    return datetime.now(config.CHINA_TZ).date()


def update_data(progress: Progress | None = None) -> dict:
    """更新数据：股票列表 → 日线（增量，收盘后自动用快照补当天）→ 涨停池存档 → 龙虎榜 → 基本面（超过7天才更新）"""
    from .market import history, universe

    report: Progress = progress or (lambda f, m="": None)
    config.ensure_dirs()
    out: dict = {"warnings": []}

    report(0.0, "第1步：更新股票列表……")
    try:
        uni = universe.refresh_universe(progress=scaled(report, 0.0, 0.05))
        out["stocks"] = uni.height
    except Exception as e:  # noqa: BLE001  列表更新失败时用旧列表继续
        if universe.load_universe().is_empty():
            raise
        out["warnings"].append(f"股票列表更新失败，继续使用旧列表：{friendly_error(e)}")
        report(0.05, out["warnings"][-1])

    report(0.05, "第2步：下载/补齐日线行情……")
    result: dict = history.update_history(progress=scaled(report, 0.05, 0.72))
    out["history"] = result
    last: str | None = result.get("last_date")

    if last and last == _china_today().isoformat():
        report(0.72, "第3步：存档今天的涨停/炸板/跌停池……")
        try:
            from .market import pools

            out["pools"] = pools.archive(_china_today())
        except Exception as e:  # noqa: BLE001
            out["warnings"].append(f"涨停池存档失败：{friendly_error(e)}")
            report(0.75, out["warnings"][-1])

    report(0.76, "第4步：更新龙虎榜……")
    try:
        from .market import pools

        out["lhb"] = pools.update_lhb(progress=scaled(report, 0.76, 0.86))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"龙虎榜更新失败：{friendly_error(e)}")
        report(0.86, out["warnings"][-1])

    try:
        from .market import fundamentals

        path = config.STOCK_LAB.joinpath("fundamentals.parquet")
        stale: bool = not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400
        if stale:
            report(0.86, "第5步：更新财报数据（每周一次，约5分钟）……")
            out["fundamentals"] = fundamentals.update_fundamentals(progress=scaled(report, 0.86, 0.99))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"财报数据更新失败：{friendly_error(e)}")
        report(0.99, out["warnings"][-1])

    failed: int = len(result.get("failed") or [])
    report(1.0, f"数据已更新到 {last or '未知'}" + (f"（{failed} 只股票下载失败，下次会重试）" if failed else ""))
    return out


def run_update(progress: Progress | None = None) -> dict:
    """更新数据：优先用 predict.service.update_data（更新后会清掉预测缓存），不可用时用本模块的 update_data"""
    try:
        from .predict import service
    except ImportError:
        return update_data(progress)
    fn: Callable | None = getattr(service, "update_data", None)
    return fn(progress) if fn else update_data(progress)


def record_paper(progress: Progress | None = None) -> dict:
    """把今天三个策略的正式信号记入模拟盘（paper.record_today）。

    不能记录（交易日收盘前、数据不是最新）或出错时返回 {"error": 原因}，不让整个任务失败；
    同一 (信号日, 策略) 只记第一次，重复调用是安全的。
    """
    try:
        from . import paper

        out: dict = paper.record_today()
    except Exception as e:  # noqa: BLE001  模拟盘记录失败不影响数据更新/预测
        return {"error": friendly_error(e)}
    if progress is not None:
        progress(1.0, f"模拟盘：{out.get('message') or '已记录'}")
    return out


def calibrate_weekly(progress: Progress | None = None, now: datetime | None = None,
                     timeout: float = CALIBRATE_TIMEOUT) -> dict | None:
    """每周一次用 baostock 核对除权除息日的昨收（history.calibrate_ex_rights，限时 timeout 秒，没查完的下次续查）。

    距上次不到 CALIBRATE_EVERY_DAYS 天、或还没有日线数据时跳过，返回 None；
    出错时返回 {"error": 原因}（不抛异常，不影响每日流程）；否则返回 calibrate_ex_rights 的结果。
    """
    stamp_path = config.STOCK_LAB.joinpath(CALIBRATE_STAMP)
    now = now or datetime.now(config.CHINA_TZ)
    try:
        last: datetime = datetime.fromisoformat(json.loads(stamp_path.read_text(encoding="utf-8"))["last_run"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=config.CHINA_TZ)
        if (now - last).total_seconds() < CALIBRATE_EVERY_DAYS * 86400:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        pass                                # 从没核对过（或记录损坏）：现在核对
    try:
        from .market import history

        if history.last_date() is None:
            return None
        if progress is not None:
            progress(1.0, "每周一次：正在核对除权除息日的昨收（最多几分钟，没查完下次继续）……")
        result: dict = history.calibrate_ex_rights(
            progress=(lambda f, m="": progress(1.0, m)) if progress is not None else None, timeout=timeout)
    except Exception as e:  # noqa: BLE001  核对失败不影响数据更新和预测
        return {"error": friendly_error(e)}
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps({"last_run": now.isoformat(timespec="seconds"), "result": result},
                                         ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass
    return result


def run_daily(progress: Progress | None = None) -> Any:
    """一键更新：更新数据 → 必要时重新训练 → 生成今天的预测（由 predict.service 编排）→ 记录模拟盘信号
    → 每周一次核对除权日昨收（calibrate_weekly，结果在 "calibrate"；不到一周时不出现这个字段）。

    service.daily_pipeline 结果里已有 "paper"（它自己记录过）时不再记录；即使重复记录也会按
    (信号日, 策略) 去重，不会记两遍。
    """
    from .predict import service

    result: Any = service.daily_pipeline(progress=progress)
    if isinstance(result, dict) and "paper" not in result:
        result["paper"] = record_paper(progress)
    cal: dict | None = calibrate_weekly(progress)
    if cal is not None and isinstance(result, dict):
        result["calibrate"] = cal
    return result


def run_paper(progress: Progress | None = None) -> dict:
    """只记录模拟盘信号（数据已是最新、但当天信号还没记录时由调度器提交）"""
    if progress is not None:
        progress(0.05, "正在生成今天的信号并记入模拟盘……")
    out: dict = record_paper(progress)
    if "error" in out:
        raise RuntimeError(out["error"])
    return out


ALL_KINDS: list[str] = ["streak", "first", "swing"]


def run_train(kind: str, progress: Progress | None = None) -> dict:
    """训练模型；kind="all" 时依次训练全部模型（以 service.KIND_LABELS 为准）"""
    from .predict import service

    if kind == "all":
        labels: Any = getattr(service, "KIND_LABELS", None)
        kinds: list[str] = list(labels) if isinstance(labels, dict) and labels else list(ALL_KINDS)
    else:
        kinds = [kind]
    results: dict = {}
    for i, k in enumerate(kinds):
        sub: Progress = scaled(progress, i / len(kinds), (i + 1) / len(kinds))
        results[k] = service.train(k, progress=sub)
    return results if kind == "all" else results[kind]
