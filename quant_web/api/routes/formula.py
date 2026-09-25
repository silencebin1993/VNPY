"""
公式接口：公式库、语法检查、在个股上预览、今天选股（扫描）、历史验证（后台任务）、我的公式。
"""
from datetime import date
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, Body, HTTPException

from ..common import CACHE, check_code, mod, ok, panel_sig


router = APIRouter()

SCAN_LIMIT: int = 500
SCAN_CHIP_DAYS: int = 30


def _compile(text: str) -> Any:
    parser = mod("formula.parser")
    try:
        return parser.compile_formula(text)
    except parser.FormulaError as e:
        raise HTTPException(400, str(e)) from None


def _meta(prog: Any) -> dict:
    return {"outputs": prog.outputs, "condition": prog.condition, "uses_chips": prog.uses_chips,
            "lookback": prog.lookback, "indicators": sorted(prog.indicators)}


def _text_of(text: str | None, fid: str | None) -> str:
    if text and text.strip():
        return text
    if fid:
        if fid.startswith("my_"):
            item = next((x for x in mod("formula.store").list_mine() if x["id"] == fid), None)
            if item is None:
                raise HTTPException(404, "没有找到这个公式（可能已被删除）")
            return item["text"]
        return mod("formula.library").get(fid)["text"]
    raise HTTPException(400, "请提供公式内容")


@router.get("/api/formula/library")
def formula_library() -> Any:
    lib = mod("formula.library")
    parser = mod("formula.parser")
    store = mod("formula.store")
    tasks = mod("tasks")
    items: list[dict] = []
    for f in lib.LIBRARY:
        prog = parser.compile_formula(f["text"])
        key: str = store.result_key(f["text"], tasks.formula_params({}))
        res: dict | None = store.load_result(key)
        items.append({**f, **_meta(prog), "result_key": key,
                      "verdicts": {h: r["verdict"] for h, r in res["holds"].items()} if res else None,
                      "validated_at": res.get("saved_at") if res else None})
    mine: list[dict] = []
    for f in store.list_mine():
        try:
            meta = _meta(parser.compile_formula(f["text"]))
        except parser.FormulaError as e:
            meta = {"error": str(e)}
        key = store.result_key(f["text"], tasks.formula_params({}))
        res = store.load_result(key)
        mine.append({**f, **meta, "group": "我的公式", "kind": "select", "result_key": key,
                     "verdicts": {h: r["verdict"] for h, r in res["holds"].items()} if res else None,
                     "validated_at": res.get("saved_at") if res else None})
    return ok({"groups": lib.GROUPS, "items": items, "mine": mine, "reference": parser.reference()})


@router.post("/api/formula/check")
def formula_check(text: Annotated[str, Body(embed=True)]) -> Any:
    parser = mod("formula.parser")
    try:
        prog = parser.compile_formula(text)
    except parser.FormulaError as e:
        return ok({"ok": False, "error": str(e)})
    return ok({"ok": True, **_meta(prog)})


def _stock_frame(code: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """一只股票的全部历史：(原始日线, 公式用的前复权表)"""
    def build() -> tuple[pl.DataFrame, pl.DataFrame]:
        engine = mod("formula.engine")
        raw: pl.DataFrame = mod("market.history").load_panel(codes=[code], columns=engine.FRAME_COLS)
        if raw.is_empty():
            return raw, raw
        return raw, engine.prepare_frame(raw)
    return CACHE.get(("formula_frame", code), build, ttl=600, sig=panel_sig())


@router.post("/api/formula/preview")
def formula_preview(
    code: Annotated[str, Body(embed=True)],
    text: Annotated[str | None, Body(embed=True)] = None,
    fid: Annotated[str | None, Body(embed=True)] = None,
) -> Any:
    """在一只股票的历史上计算公式：输出线（按日期）和信号出现的日期"""
    code = check_code(code)
    formula: str = _text_of(text, fid)
    prog = _compile(formula)
    raw, frame = _stock_frame(code)
    if frame.is_empty():
        raise HTTPException(404, f"本地没有 {code} 的日线数据，请先更新数据")
    engine = mod("formula.engine")
    chips = engine.chips_for_frame(frame, raw) if prog.uses_chips else None
    parser = mod("formula.parser")
    try:
        res = engine.evaluate(prog, frame, chips)
    except parser.FormulaError as e:
        raise HTTPException(400, str(e)) from None
    dates: list[str] = [str(d) for d in frame["date"].to_list()]
    outputs: dict = {}
    for name, s in res.outputs.items():
        if s.dtype == pl.Boolean:
            continue
        outputs[name] = [None if v is None else round(float(v), 4) for v in s.to_list()]
    signals: list[str] = [d for d, v in zip(dates, res.condition.to_list(), strict=True) if v] if res.condition is not None else []
    return ok({**_meta(prog), "code": code, "dates": dates, "series": outputs, "signals": signals})


@router.post("/api/formula/scan")
def formula_scan(
    text: Annotated[str | None, Body(embed=True)] = None,
    fid: Annotated[str | None, Body(embed=True)] = None,
    boards: Annotated[list[str] | None, Body(embed=True)] = None,
    exclude_st: Annotated[bool, Body(embed=True)] = True,
) -> Any:
    """最近一个交易日，全市场（按板块筛选）哪些股票满足公式"""
    formula: str = _text_of(text, fid)
    prog = _compile(formula)
    history = mod("market.history")
    last: date | None = history.last_date()
    if last is None:
        raise HTTPException(409, "本地还没有日线数据，请先点“一键更新”")
    if boards is None:
        boards = list(mod("settings").load().profile.boards)
    engine = mod("formula.engine")
    validate = mod("formula.validate")
    start: date = engine.lookback_start(prog, last)
    raw: pl.DataFrame = history.load_panel(start=start, columns=engine.FRAME_COLS)
    frame: pl.DataFrame = validate.add_trade_columns(engine.prepare_frame(raw))
    chips = None
    if prog.uses_chips:
        days: list = sorted(frame["date"].unique().to_list())[-SCAN_CHIP_DAYS:]
        stats, _ = mod("indicators.chips").compute_panel(raw, want=set(days))
        chips = frame.select("code", "date").join(stats, on=["code", "date"], how="left")
    parser = mod("formula.parser")
    try:
        res = engine.evaluate(prog, frame, chips)
    except parser.FormulaError as e:
        raise HTTPException(400, str(e)) from None
    if res.condition is None:
        raise HTTPException(400, "这个公式没有选股条件（请写一句 XG:…）")
    elig = validate.eligible_expr(tuple(boards), exclude_st, 0.0)
    hits: pl.DataFrame = (frame.with_columns(res.condition.alias("_sig"), elig.alias("_ok"))
                          .filter((pl.col("date") == last) & pl.col("_sig") & pl.col("_ok")))
    uni: pl.DataFrame = mod("market.universe").load_universe()
    names: pl.DataFrame = uni.select(["code", "name", *[c for c in ("industry",) if c in uni.columns]]) if uni.height else pl.DataFrame({"code": [], "name": []})
    hits = hits.join(names, on="code", how="left").with_columns(
        ((pl.col("raw_close") / pl.col("preclose") - 1) * 100).alias("pct"))
    total: int = hits.height
    rows: list[dict] = hits.sort("amount", descending=True).head(SCAN_LIMIT).select(
        [c for c in ("code", "name", "industry", "board", "raw_close", "pct", "amount", "turn") if c in hits.columns]
    ).rename({"raw_close": "close"}).to_dicts()
    return ok({"date": str(last), "total": total, "rows": rows, "boards": boards, "truncated": total > SCAN_LIMIT,
               **_meta(prog)})


@router.post("/api/formula/validate")
def formula_validate(
    text: Annotated[str | None, Body(embed=True)] = None,
    fid: Annotated[str | None, Body(embed=True)] = None,
    holds: Annotated[list[int] | None, Body(embed=True)] = None,
    boards: Annotated[list[str] | None, Body(embed=True)] = None,
    min_amount: Annotated[float, Body(embed=True)] = 0.0,
    force: Annotated[bool, Body(embed=True)] = False,
) -> Any:
    """已有相同公式+参数的验证结果时直接返回（force=True 重新验证）；否则启动后台任务，返回 {job_id, key}"""
    formula: str = _text_of(text, fid)
    _compile(formula)
    tasks = mod("tasks")
    store = mod("formula.store")
    params: dict = tasks.formula_params({"holds": holds, "boards": boards, "min_amount": min_amount})
    key: str = store.result_key(formula, params)
    if not force:
        cached: dict | None = store.load_result(key)
        if cached:
            return ok({"key": key, "result": cached})
    job_id: str = tasks.submit("formula_validate", {"text": formula, **params})
    return ok({"key": key, "job_id": job_id})


@router.get("/api/formula/result/{key}")
def formula_result(key: str) -> Any:
    res: dict | None = mod("formula.store").load_result(key)
    if res is None:
        raise HTTPException(404, "还没有这个验证结果")
    return ok(res)


@router.get("/api/formula/mine")
def formula_mine() -> Any:
    return ok(mod("formula.store").list_mine())


@router.post("/api/formula/mine")
def formula_mine_save(
    name: Annotated[str, Body(embed=True)],
    text: Annotated[str, Body(embed=True)],
    note: Annotated[str, Body(embed=True)] = "",
    id: Annotated[str | None, Body(embed=True)] = None,           # noqa: A002
) -> Any:
    return ok(mod("formula.store").save_mine({"id": id, "name": name, "text": text, "note": note}))


@router.delete("/api/formula/mine/{fid}")
def formula_mine_delete(fid: str) -> Any:
    if not mod("formula.store").delete_mine(fid):
        raise HTTPException(404, "没有找到这个公式")
    return ok({"deleted": fid})
