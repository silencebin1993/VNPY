# quant_web 架构与接口约定

本文件记录 `quant_web` 各模块之间**实际的**接口（2026-09-25 端到端联调后按代码更新；第二版 swing/模拟盘见第 7 节）。
改接口时同步改这里；新增字段可以加，已有字段不要删改。面向用户的文字一律简体中文、平实易懂。

## 0. 总体

- 本地网页应用：FastAPI 后端 + Vue3（全局构建，无打包）+ ECharts，静态资源全部在 `quant_web/static/vendor/`，不依赖外网 CDN。
- 启动：`python -m quant_web [--no-browser] [--no-scheduler] [--port N]` → `127.0.0.1:8765`（被占用自动换 8766~8775；
  该端口已有量化助手时直接打开浏览器）。根目录 `启动量化助手.bat` 调用它（参数原样传递）。`python -m quant_web etf` = 旧版 ETF 命令行。
  两个 .bat（`启动量化助手.bat`、`安装环境.bat`）：CRLF、UTF-8 无 BOM；**注释行（rem）只用 ASCII，并且都放在 `chcp 65001` 之前**——
  cmd 在 chcp 之后会读错含中文的 rem 行，打印"系统找不到指定的路径"（2026-09-25 修正，用 `--no-browser --no-scheduler --port <空闲端口>` 实测无报错）；
  echo/title 里的中文不受影响。
- 环境变量：`QUANT_WEB_WORKSPACE`（数据目录）、`QUANT_WEB_NO_SCHEDULER=1`（不启动收盘后自动更新线程；`--no-scheduler` 会设置它）。
- `etf_quant` 包（稳健ETF，基于 vnpy.alpha）保持可用，网页通过 `/api/etf/*` 调用；它的数据在 `etf_quant/workspace`（`ETF_QUANT_WORKSPACE`）。
- Python 3.13（`F:\VNPY\.venv\Scripts\python.exe`）。依赖见 `requirements.txt`；`pypinyin` 可选（没有时拼音首字母按 GB2312 编码估算）。
- 代码风格：类型注解、`python -m ruff check quant_web` 通过。时间一律北京时间 `config.CHINA_TZ`（本机时区不是北京）。
- 网络：国内站点优先直连（绕过系统代理），失败再走代理，统一用 `net.py`。
- 资源：全量面板进程内缓存约 3 GB（加载峰值约 6 GB，约 10 秒）；`train("all")`（连板→首板→波段）实测约 6 分钟、峰值约 11.8 GB（2026-09-25）。

## 1. 目录

```
quant_web/
  __main__.py  config.py  net.py  settings.py  jobs.py  scheduler.py  paper.py（模拟盘，见 7.4）
  market/  universe.py history.py fundamentals.py realtime.py pools.py info.py news.py sentiment.py
  predict/ limits.py features.py labels.py model.py scoring.py backtest.py service.py costs.py execution.py swing.py（见 7.1/7.2）
  api/     server.py（路由+静态文件） etf.py（稳健ETF封装）
  static/  index.html app.js style.css pages/*.js vendor/{vue.global.prod.js,echarts.min.js}
  workspace/（运行数据，gitignore）
tests/test_quant_web_*.py（全部离线；带 @pytest.mark.network 的联网冒烟测试断网自动跳过）
```

## 2. 公共约定

### 2.1 代码与交易所（market/universe.py）
- `code` 6 位字符串；`exchange` = `"SSE"/"SZSE"/"BSE"`；前缀 `6`→SSE，`0/3`→SZSE，`4/8/92`→BSE；B 股（`200`、`900`）不支持。
- `board`：`main` / `chinext`（300、301、302）/ `star`（688、689）/ `bj`；`BOARD_LABELS` 给中文名。
- 工具：`exchange_of, is_a_share, board_of, market_symbol("sz001216"), bs_symbol("sz.001216"), vt_symbol, clean_name, name_is_st`。

### 2.2 路径（config.py）
`WORKSPACE`（env 可改）/ `STOCK_LAB` / `PANEL_DIR = stock_lab/daily/{YYYY}.parquet` / `UNIVERSE_FILE` /
`POOLS_DIR = stock_lab/pools/{kind}/{YYYYMMDD}.parquet` / `MODEL_DIR`（`{kind}.txt, {kind}_meta.json, {kind}_oos.parquet, {kind}_oos_daily.parquet`）/
`CACHE_DIR`（`news_store.parquet`、`trade_calendar.json`、个股资料缓存）/ `SETTINGS_FILE` / `WATCHLIST_FILE` / `STATIC_DIR`；
`stock_lab` 下另有 `fundamentals.parquet, lhb.parquet, first_bars.parquet, preclose_checked.parquet`。`HISTORY_START = "2019-01-01"`，`ensure_dirs()`。

### 2.3 日线面板 `PANEL_SCHEMA`
`date, code, open, high, low, close, preclose, volume(股), amount(元), turn(%), tradestatus(Int8), is_st, source("tx"/"rt")`，主键 (date, code)。
价格**不复权**；`preclose` 为交易所口径昨收（除权日用复权因子换算，特殊除权日用 baostock 校正，记录在 `preclose_checked.parquet`）。
`is_st` 用当前名称近似全部历史（limits 模块对 2026-07-06 前做了修正，见 3.4）。

### 2.4 股票列表 `universe.SCHEMA`
`code, name, exchange, board, list_date, delist_date, is_st, industry(证监会行业), status(1 上市 / 0 退市)`。

### 2.5 数据源实测（2026-09-24）
- 腾讯：实时报价 `qt.gtimg.cn`（全市场约 5 秒）、日K `newfqkline`（不复权/前复权，每次≤2000 根，注意是 开收高低 顺序）、分时/5日分时。主力数据源。
- 东方财富：`push2/push2his` 本机不可用；`push2ex` 涨停类股票池只保留最近约 15 个交易日 → 每天存档；
  `datacenter-web`：龙虎榜（可回溯到 2004-06-25）、按报告期的全市场财报（基本面主源）。
- 同花顺：F10 概念、单只财报（备用，按 IP 限流）；新浪：个股资金流；财联社/东财/新浪 7x24 快讯。
- baostock：只用于股票上市日期/行业与特殊除权日昨收校正（慢、会限流）。

## 3. 模块接口（实际签名）

### 3.1 net.py
`get(url, params=None, headers=None, timeout=10, encoding=None, retries=3) -> Response`（直连→代理轮换，抛中文 ConnectionError）；
`get_text / get_json(url, **kw)`；`domestic_direct()` 上下文；`call_with_fallback(func, retries=3, empty_ok=True)`；`is_domestic(url)`。

### 3.2 market/universe.py
`refresh_universe(progress=None) -> DataFrame`；`load_universe()`（按文件签名缓存；不存在返回带列空表）；
`load_first_bars() / save_first_bars(records) / fill_list_dates(universe=None, save=True)`（**内容没变不重写文件**：文件时间是各处缓存的失效依据）；
`search(q, limit=20) -> [{"code","name","board","industry"}]`：代码精确/前缀 > 名称前缀 > 名称包含 > 拼音首字母，退市排后。

### 3.3 market/history.py
`update_history(progress=None, codes=None, start=HISTORY_START, workers=8, force=False) -> {"updated","failed","last_date","rows","mode"("full"/"kline"/"snapshot"/"none"...),"seconds","skipped_empty"}`
（增量；面板只缺最近一个已收盘交易日时用全市场快照；分批落盘可续传；下载成功却一根新K线都没有的股票（长期停牌等，约十几只）
记在 `STOCK_LAB/empty_fetch.json`（代码 → 当时的目标交易日，`load_empty_fetch()`），之后 `EMPTY_SKIP_DAYS=5` 个交易日内不再下载，
`force=True` 照常下载；再次下载仍从面板最后一根K线之后补，不丢数据；有了新K线就从记录里去掉）；
`append_realtime_snapshot(snapshot, day) -> int`；`latest_closed_day(calendar=None) -> (day, closed_days)`；
`load_panel(start=None, end=None, codes=None, columns=None)`（去停牌，按 code,date 排序）；`last_date()`；`trade_dates(start, end)`；
`calibrate_ex_rights(...)`、`fetch_stock(...)`、`compute_preclose(...)` 为内部/维护用。

### 3.3b market/fundamentals.py
`update_fundamentals(progress=None, codes=None, workers=6, max_age_days=7) -> {"updated","failed","skipped","rows","periods","source","blocked","seconds"}`；
`load_fundamentals()`；`asof_join(df, fund)`（按 `date >= avail_date`）。
列：`code, report_date, avail_date(法定披露截止日次日), eps, eps_ttm, bvps, roe, revenue, revenue_yoy, net_profit, net_profit_yoy, debt_ratio, gross_margin, ...(ocfps, *_ttm, net_margin), notice_date, source("em"/"ths"), fetched_at`；
金额元、比率为百分数，累计值非年化。

### 3.4 predict/limits.py
`limit_ratio(code, is_st, day)`：主板 10%，主板 ST 5%（**2026-07-06** 起 10%，由真实数据核实），创业板 20%（2020-08-24 前 10%），科创板 20%，北交所 30%；
`limit_price(preclose, ratio, board="main")`（ROUND_HALF_UP；北交所涨停向下、跌停向上取整）；`no_limit_days(board, list_date)`；
`add_limit_columns(panel, universe)` 新增：`board, list_days, no_limit, limit_up, limit_down, is_limit_up, touched_up, is_broken, is_limit_down, one_word, streak, pct`。容差 0.005 元。

### 3.5 market/sentiment.py
`daily_sentiment(panel_lim)` 每日一行：`date, n_stocks, n_up, n_down, n_limit_up, n_limit_down, n_broken, break_rate, max_streak, n_streak2, prev_lu_premium, prev_lu_promote, prev_streak_premium, median_pct, amount_total, amount_ratio20, temperature(0-100)`；
`temperature_label(t)`（<20 冰点 <40 低迷 <60 正常 <80 活跃 其余过热）；
`live_sentiment()`：快照+历史（复用 `service.context()`），字段同上 + `as_of, label, live(bool), pool_limit_up`；`recent(days=60)`；`history_frame()`。

### 3.6 predict/features.py
`GROUPS, GROUP_LABELS, FEATURE_GROUPS, FEATURE_LABELS, ALL_FEATURES, FEATURE_TO_GROUP, DISPLAY_COLUMNS, STREAK_ONLY`；
`feature_columns(kind)`（first 模型不用 `STREAK_ONLY`）；`compute_base(panel_lim, universe, lhb=None)`（全市场逐行特征，训练时复用）；
`build_features(panel_lim, universe, sentiment, kind, dates=None, neg_sample=None, seed=7, base=None, fund=None, lhb=None, zt_pool=None)`
→ `date, code` + 特征(Float32) + `DISPLAY_COLUMNS = close, pct, streak, turn, float_cap(亿), is_st, board, one_word, industry`。
候选：streak = 当日收盘涨停且 not no_limit；first = 非涨停、非停牌、not no_limit、近 5 日没涨停过。严禁未来函数（测试覆盖）。

### 3.7 predict/labels.py
`add_labels(feat, panel_lim)` → `y`（下一交易日收盘涨停；下一根 K 线距今 >10 自然日或没有时为空）+ `next_date, next_open, next_limit_up, next_one_word, next_close, next_pct`。

### 3.8 predict/model.py
`Fold(train_end, test_start, test_end)`；`make_folds(dates, first_test_year=2022, step_months=6)`；
`train_walk_forward(ds, feature_cols, folds, progress=None, test_ds=None, offset=0.0, contrib_top=None, params=None, keep_cols=None) -> (oos, fold_metrics)`；
`train_final(ds, feature_cols, num_boost_round=None, params=None)`；`predict(booster, feat, feature_cols, offset=None, n_reasons=5)` → `date, code, prob, bias, contrib_<group>..., reasons`；
`evaluate(pred)`（auc、top1/3/5/10_hit、base_rate…）；`calibration(pred)`；`importance(booster, cols) -> (组占比, top_features)`；
`paths(kind)`；`save(kind, booster, meta, oos=None, daily=None)`；`load(kind)`；`load_meta(kind)`；`load_oos(kind)`。
first 模型训练时负样本抽 5%（`logit_offset = ln 0.05` 校正回真实概率），样本外全部候选都算概率，只保存每天前 300 名的贡献分解 + 每日候选统计 `{kind}_oos_daily.parquet`。
meta 字段：`kind, label, trained_at, data_start, data_end, n_samples, n_positive, base_rate, train_base_rate, neg_sample, logit_offset, n_rounds, feature_cols, feature_labels, group_labels,
folds[{fold,train_end,test_start,test_end,n_train,best_iter,n,days,positives,top1/3/5/10_hit,auc,base_rate,seconds}], oos{...}, oos_start, oos_end, topn_hit, calibration[{bucket,pred,actual,n}], importance{group:占比}, top_features, notes, seconds, n_oos_saved`。

### 3.9 predict/scoring.py
`apply_weights(pred, weights, news_score=None, news_weight=0.0)`：`score_logit = bias + Σ w_g·contrib_g + news_weight·news_z`，`score = sigmoid`；`dim_<group> = 50+50·tanh(contrib_g)`，`dim_news`（无消息为 50）。
`filter_candidates(df, filters)`（ST、板块、流通市值、股价、连板数（仅 streak）、排除一字板、概率门槛；缺数据不因此排除）；`reasons_text(reasons)`；`describe(feature, value)`。

### 3.10 predict/backtest.py
`run_backtest(oos, panel_lim, s, t, daily=None) -> dict`，规则见模块文档（T 收盘信号 → T+1 开盘买，一字/开盘涨停/停牌买不到，开盘涨幅 > max_gap_pct(%) 放弃；
next_open / next_close / until_break（最多 10 天）；止损按 min(开盘价, 止损价)；跌停卖不出顺延；佣金最低 5 元、印花税、滑点；100 股一手）。
返回 `metrics{trades, win_rate, avg_return, median_return, profit_factor, total_return, cagr, max_drawdown, hit_rate, base_rate, unfilled, avg_hold_days, sharpe,
signals, skipped_gap, skipped_full, skipped_cash, skipped_lot, skipped_held, suspended, open_positions, final_equity, total_fees, start, end,
daily_t, nw_t, fill_rate, per_trade_uncapped{trades,avg_return,win_rate}}`（新字段见 7.3）, `notes[]`,
`equity/drawdown[{date,value}]`, `yearly[{year,return,trades,win_rate}]`, `calibration`, `topn_hit[{n,hit_rate,picks}]`,
`trades[{signal_date,code,name,score,entry_date,entry,exit_date,exit,ret,reason,hold_days,pnl}]`（最近 500 笔）。

### 3.11 predict/service.py
- `context(progress=None, force=False) -> Context(panel_lim, universe, sentiment, dates, last_date, ...)`：全量面板+涨停列+情绪，按面板/股票列表文件签名失效；`invalidate()`。
- `dataset_status()`；`train(kind, progress=None, neg_sample=None, first_test_year=2022) -> 摘要`；`models_meta(kind)`。
- `predict_latest(kind, settings=None)`（见 `/api/predict/today`）；最新一天的模型预测按 (面板, 模型) 缓存，调权重/筛选不重算模型。
- `prediction_for(code, settings=None) -> {kind,label,signal_date,prob,score,streak,dims,reasons,in_list,rank,pick,n_candidates,n_list,base_prob} | None`
  （in_list/rank 按用户权重与筛选、不含消息面）。
- `backtest(kind, settings=None)`：用缓存的样本外预测，约 1.5~2.5 秒（含两种随机基准各 20 次、分段各用全新账户，见 7.3）；消息面权重恒为 0。
  返回另加 `tuned{modified, fields[中文名]}`（`tuned_fields(kind, ps, ts)`：与 `settings.defaults_for(kind)` 不同的选股/买卖字段，不算本金/费用/消息面）。
- `refresh_evaluation(kind="swing")`：用已保存的 `{kind}_oos.parquet` 按当前口径重算 `meta.trade_oos`（含 `random_matched`），只重写 meta（`model.save_meta`），不重训。
- `update_data(progress=None)`：股票列表（>20 小时才刷新）→ 日线增量 → 当天涨停池存档 → 龙虎榜 → 财报；**面板文件没变化时保留内存面板**，只清预测缓存。
- `daily_pipeline(progress=None) -> {"trained":[...],"predictions":{kind:{signal_date,count,picks,warnings}},"warnings","update","seconds"}`：模型不存在或 >30 天未训练才重训。
`settings` 参数接受 `Settings` / 同名属性对象 / dict / None（读保存的设置）。

### 3.12 market/realtime.py（腾讯）
`quotes(codes) -> [dict]`：`code,name,symbol,price,prev_close,open,high,low,volume(股),amount(元),pct(%),change,turnover(%),pe,pb,float_cap(元),total_cap(元),limit_up,limit_down,amplitude,volume_ratio,avg_price,time,bids,asks`；
`quote(code)`；`snapshot_all(codes) -> DataFrame`；`index_quotes()`（`INDEXES` 五个指数）；
`kline(code, period="day"|"week"|"month", adjust="qfq"|"", count=600) -> [{date,open,close,high,low,volume,amount,turnover}]`；
`minute(code, days=1) -> {code,symbol,name,date,prev_close,points[{time,price,volume,amount,avg_price}], days?[{date,prev_close,points}]}`；
`trade_calendar(refresh=False)`（上证指数日K，缓存到 `CACHE_DIR/trade_calendar.json`）；`is_trading_day(day)`；`recent_trading_days(n, until=None)`；
`market_phase(now=None)` → `"盘前"/"交易中"/"午间休市"/"已收盘"/"休市"`。

### 3.13 market/pools.py
`KINDS = {zt 涨停, zb 炸板, dt 跌停, prev 昨日涨停, strong 强势, sub_new 次新}`；`fetch_pool(kind, day)` → `POOL_SCHEMA`
（`date, code, name, pct, price, amount, float_cap, total_cap, turn, seal_amount, first_time, last_time, open_times, streak, industry, stat, stat_days, stat_count, limit_price, ...`）；
`archive(day, kinds=None, force=False)`；`archived_days(kind)`；`backfill(days=15)`；`load_archive(kind, start=None, end=None)`；`get_pool(kind, day)`（存档优先）；
`update_lhb(start=None, progress=None, workers=4) -> {"rows","added","start","end","months","failed","seconds","earliest"}`（没有新记录不重写文件）；
`load_lhb()` → `date, code, name, reason, net_buy, buy, sell, amount_total, market_amount, turnover, close, pct, explain, net_ratio, amount_ratio, float_cap`；`fetch_lhb(start, end, code=None)`。
接口失败返回空表 + 警告，不让上层崩溃。

### 3.14 market/info.py 与 news.py
`profile(code, refresh=False)` → `code, name, exchange, board, board_label, industry, list_date, is_st, business, concepts[], valuation{pe_ttm,pb,total_cap,float_cap,price,pct},
finance{report_date,revenue,revenue_yoy,net_profit,net_profit_yoy,roe,eps,eps_ttm,bvps,debt_ratio,gross_margin}|null, fund_flow[{date,main_net,main_pct,super_net,big_net,mid_net,small_net,net}](新浪，近20日),
lhb[{date,reason,net_buy,buy,sell,explain,pct}], news[{time,title,url,source}], limit_history[{date,streak,one_word}](近250日), errors{part: 原因}, updated_at`；缓存 10 分钟（有失败 2 分钟）。
`flash(limit=100) -> [{time,title,content,tags,stocks[{code,name}],source,url,important}]`（三源合并去重，存 `news_store.parquet` 保留 72 小时）；
`POLICY_KEYWORDS`；`news_heat(codes, hours=24) -> [code, news_count, policy_count, news_z]`；`refresh(force=False)`。

### 3.15 settings.py（pydantic v2，`extra="ignore"`）
```python
PredictSettings: kind("streak"|"first"|"swing")="streak", weights{group:0~2}=1, news_weight=0.0(0~2), exclude_st=True,
    boards=["main","chinext","star"], min_float_cap=0, max_float_cap=500(亿), min_price=2, max_price=200,
    streak_min=1, streak_max=10, exclude_one_word=False, threshold=0.0(0~1), top_n=5(1~50)
TradeSettings: capital=100000, position_pct=0.2(小数), max_positions=5, max_gap_pct=7.0(百分数),
    exit_rule("next_open"|"next_close"|"until_break"|"until_break_close")="next_close", stop_loss_pct=0.0(小数，0.05=跌5%止损),
    fee_rate=0.00025, stamp_duty=0.0005(仅 stamp_by_date=False 时用), stamp_by_date=True, slippage=0.001
Settings: predict, trade, auto_update=True, risk_ack=False
```
`PRESETS`（稳健/均衡/激进：`description, predict, trade, threshold_by_kind{streak,first,swing}, kind_overrides{swing:{predict,trade}}`）；按 kind 的默认值见 7.3b；`load()`（文件损坏时回到默认）；`save(s)`；
`merge_update(current, patch)`（深合并后校验，失败抛 `SettingsError`(中文)）；`apply_preset(s, name)`；`error_text / field_name / format_errors`（校验错误中文化）。

### 3.16 jobs.py 与 scheduler.py
`JobManager.submit(name, title, fn, group=None) -> job_id`（同名运行中返回已有 id；同 `group`（`HEAVY`="heavy"：更新/训练/一键）排队串行）；
`get(id)` → `{id,name,title,status("running"|"done"|"failed"),queued,progress,message,logs[最近200],result,error,started_at,finished_at,elapsed}`；
`list(max_logs=20)`（新的在前）、`running()`、`latest(name)`、`is_busy(group)`、`wait(id, timeout)`。任务线程的 logging/print 自动进日志。
任务函数：`run_update`（→ `service.update_data`）、`run_train(kind|"all")`、`run_daily`（→ `service.daily_pipeline` → 模拟盘记录 →
`calibrate_weekly()`：每 `CALIBRATE_EVERY_DAYS=7` 天一次 `history.calibrate_ex_rights(timeout=CALIBRATE_TIMEOUT=300)`，核对除权除息日昨收，
没查完的下次续查；上次时间与结果记在 `STOCK_LAB/ex_rights_calibrated.json`；没有日线或不到一周返回 None（结果里没有 `calibrate`），
出错返回 `{"error"}`，都不影响任务结果；到期时结果里有 `calibrate`）、`run_paper`。
`Scheduler(jobs, runner, now_fn, calendar_fn, last_date_fn)`：交易日北京时间 15:35 后面板缺当天数据 → 提交 `daily`；
不在：没下载过数据、`auto_update=False`、落后 >30 个交易日、有大任务在跑；失败 30 分钟后重试，同一交易日最多 3 次；
`check()`、`start()/stop()`（线程先等 15 秒，之后每 60 秒检查）、`status()`。可注入假时钟测试（见 tests）。

## 4. HTTP API（全部 JSON；错误 `{"detail": "中文说明"}` + 4xx/5xx；参数校验失败 422）

| 方法 路径 | 返回 |
|---|---|
| GET `/api/status` | `{now, today, phase, panel{start,end,stocks,rows,updated_at,empty}, first_run, models{streak,first: meta 摘要+n_folds,oos,auc,top1_hit,top5_hit,age_days,stale \| null}, jobs[运行中], settings_risk_ack, auto_update, scheduler{running,last_check,last_job_id,target_day,message,run_after}, version}` |
| POST `/api/jobs/update` · `/api/jobs/daily` · `/api/jobs/etf_update` | `{job_id}`（update/daily/train 同组排队） |
| POST `/api/jobs/train` body `{"kind":"streak"\|"first"\|"swing"\|"all"}` | `{job_id}`（all = streak→first→swing） |
| GET `/api/jobs` · `/api/jobs/{id}` | 任务列表 / 单个任务（见 3.16；重启后清空，404） |
| GET `/api/market/overview` | `{as_of, phase, source("panel"\|"live"), indexes[quote], sentiment{3.5 一行 + label}, ladder[{streak,count,stocks[{code,name,pct,industry,one_word,streak,close}]}], industries[{industry,count}], history[近60日情绪行+label], warnings}`（10 秒缓存；交易时段/收盘未更新时用实时快照与涨停池） |
| GET `/api/market/pool?kind=zt&date=YYYYMMDD` | `{kind,label,date,source("archive"\|"live"),count,rows[POOL_SCHEMA],warnings}`（date 缺省=最近交易日） |
| GET `/api/stock/search?q=&limit=20` | `[{code,name,board,industry}]` |
| GET `/api/stock/{code}/quote` | quotes 单条（非 A 股代码 400，取不到 404） |
| GET `/api/stock/{code}/kline?period=day&adjust=qfq&count=600` | `{code,name,period,adjust,source("realtime"\|"panel"),bars[...],limit_days[...],warnings}`（在线失败退回本地不复权日线） |
| GET `/api/stock/{code}/minute?days=1..5` | minute() |
| GET `/api/stock/{code}/profile` | profile() + `prediction`（`service.prediction_for`，按当前设置；不在候选为 null）。服务端再用 `align_with_list` 与 `/api/predict/today` 的名单对齐：`rank/pick/score/dims` 取自名单（含消息面加分，和预测页一致），新增 `score_no_news`（不含消息面的综合概率）、`news_count, policy_count`、`rank_basis:"list"`、`beyond_rows`（在名单里但排在已返回的前 N 行之后时为 N，此时 rank 为空） |
| GET `/api/predict/today?kind=streak` | `{kind,label,signal_date,generated_at,model{trained_at,data_end,oos_topn_hit,oos_topn,base_rate,auc,oos_start,oos_end},count,rows[{code,name,industry,board,close,pct(%),streak,turn,float_cap(亿),one_word,is_st,prob,score,dims{5组+news},reasons[],news_count,policy_count,pick}],filtered_out,warnings,notes}`（按 score 降序、已过滤、≤200 行，前 top_n 行 pick；5 分钟缓存） |
| POST `/api/predict/backtest` body `{"predict":{...},"trade":{...}}`（可部分，与已保存设置合并，不保存） | 3.10/7.3 的返回 + `kind, settings_used, tuned, seconds, notes` |
| GET `/api/model/report?kind=streak` | 完整 meta + `label`（未训练 404） |
| GET / PUT `/api/settings` | Settings JSON（PUT 为部分更新：深合并→校验→保存→返回；校验失败 400 中文） |
| GET `/api/settings/presets` | PRESETS |
| GET `/api/settings/defaults?kind=` | `settings.defaults_for(kind)` = `{predict, trade}`（未知 kind 400） |
| GET `/api/paper/summary` · `/api/paper/trades?kind=&status=&limit=` · POST `/api/paper/record` | 见 7.4 |
| GET `/api/news/flash?limit=100` | flash()（60 秒缓存） |
| GET / POST / DELETE `/api/watchlist`（body `{"code","note"?}`）· DELETE `/api/watchlist/{code}` | `[quote + note, added_at]`（行情失败时带 `error`） |
| GET `/api/etf/advice?update=true` | 本周建议 `{orders, positions, allocation, cash_weight, signal_date, warnings, data, updated, update_failed, applied, ...}`；未设置资金时 `{needs_init:true, message, allocation 预览, ...}` |
| POST `/api/etf/apply` · POST `/api/etf/init` `{"capital"}` · GET/PUT `/api/etf/holdings` `{"cash","positions":{"510300":1000}}` | 持仓 `{exists,needs_init,cash,positions,market_value,total_value,price_date,price_source,nav,start_value,total_return,file}` |
| GET `/api/etf/backtest?start=2014-06-01&capital=200000` | `{start,end,capital,final_value,metrics,benchmark_metrics,benchmark_name,cash_fund_name,equity[{date,strategy,benchmark,cash_fund}],drawdown,yearly,classes,class_weights,trade_count,rebalance_count,total_commission,data_date,cached,in_sample(true),in_sample_note}`（etf_quant 参数是参考 2014-2026 年同一段历史选定的 → 样本内，页面必须标明） |
| GET `/api/etf/data` | `{has_data,updated_at,last_complete_day,fresh}` |
| GET `/api/data/status` | `{panel, models, universe/fundamentals/lhb{exists,rows,updated_at}, pools{kind:{days,first,last}}, etf, service(dataset_status)}` |
| GET `/` · `/static/*` · `/{file}` | 前端静态文件（vendor 缓存 1 天，其余 no-cache）；未知 `/api/*` 404 JSON |

服务端缓存：面板统计/情绪/天梯/涨停日期按面板文件签名；预测按 (面板, 模型, 设置) + 5 分钟；启动后后台预热面板与拼音索引（`create_app(background=False)` 时不预热，测试用）。

## 5. 前端

- `index.html` 加载 vue、echarts、`app.js`（公共组件、`QW.api/fmt/store/...`）和 `pages/*.js`（每页 `QW.page(name, component)`）。
- 路由（hash）：`#/` 市场情绪 · `#/watch/:code?` 看盘 · `#/predict?kind=swing|streak|first&code=` 短线预测（默认 swing；code 打开详情抽屉）· `#/paper` 模拟盘 ·
  `#/settings?kind=&run=1` 预测设置与历史检验（run=1 打开即检验）· `#/model?kind=` 模型中心 · `#/watchlist` · `#/news` · `#/etf?tab=advice|holdings|backtest` · `#/data`。
- 排查：地址加 `?debug=1`（如 `/?debug=1#/predict`）会把脚本错误/console.error 显示在页面底部。
- 设计：浅色为主（跟随系统可切深色），红涨绿跌，数字等宽，术语旁"?"解释；首次进入连板预测弹风险提示，确认后 `PUT /api/settings {"risk_ack":true}`。
- 新手路径：首页顶部「新手三步走」（看冷热 → 连板预测 → 看盘/加自选，可关闭，localStorage `qw-guide-hide`）。
- 预测页顶部把「挑得准吗（前N名次日涨停率）/ 照着买能赚钱吗（按用户规则回测平均每笔）/ 买得进吗（买不进占信号比例）」三个数同等分量展示；消息面把名单重新排序时提示并提供「只按模型排序」（`PUT /api/settings {"predict":{"news_weight":0}}`）；综合概率旁「含加分/含减分」标出与模型原始概率的差。
- 看盘：分时 / 五日（`/minute?days=5`）/ 日K / 周K / 月K；涨跌停封板时盘口给出封单手数与金额；一字板徽章与说明（`QW.ONE_WORD_TIP`）。

## 6. 诚实性要求（必须遵守）
- 所有命中率/收益只用**样本外**结果；预测页顶部显示前 N 名历史次日涨停率 vs 全部候选平均，以及按用户规则回测的平均每笔收益（含费用、买不进次数）。
- 实测（2026-09-24 数据，2026-09-25 重训后）：按默认规则真实交易 streak/first 都是亏的（连板 -2.33%/笔、首板 -0.87%/笔，均不如同池随机；
  留出期全新账户：连板 -0.65% vs 随机 -0.58%，首板 -0.14% vs 随机 -0.03%），页面必须如实提示；swing 见 7.0（2026-09-25 第二次重训后的数字）。
- 随机基准有两种：`baseline`（天天挑满 top_n，含策略"不操作"的日子）和 `baseline_matched`（只在策略出信号的日子挑同样多只）；
  策略减 matched = 挑股票，matched 减 baseline = 择时。文案不得把 baseline 说成"同一天、同样数量"。
- 用户改过推荐设置（`tuned.modified`）再看历史检验，成绩（含留出期）会偏乐观：设置页与预测页必须提示，并在本机记录检验过几套设置（localStorage `qw-bt-tried`）。
- 稳健ETF 的历史回测是样本内（参数就是参考同一段历史选的），页面标签与横幅必须写明。
- 消息面只有实时数据，不参与训练与回测；行业/概念用当前归属、ST 用当前名称近似，存在轻微后视偏差，在模型中心注明。
- 概念板块成分股（东方财富 push2）本机不可用，题材面用行业代替。

## 7. 第二版（2026-09-25 研究结论驱动；按实际实现更新）

### 7.0 研究结论与实测（决定产品定位）
- `streak`（连板晋级）、`first`（首板潜力）**能预测次日涨停**，但**按真实规则买入全部亏钱**且不如同池随机 → 定位 **"观察名单"**。
  2026-09-25 在真实数据上重训（已含 7.1 修正）：streak 样本外 AUC 0.726、前5名次日涨停率 63.4%（基准 24.1%），默认规则回测 -2.33%/笔（随机 -1.79%）；
  first AUC 0.809、前5名 13.0%（基准 0.8%），回测 -0.87%/笔（随机 -0.14%）。
- **强势股波段 `swing`**（当天涨≥5%未涨停，按"可实现收益"训练，T+1 开盘买，首个非涨停收盘卖，≤5 日）→ 定位 **"模拟跟踪中"**。
  **2026-09-25 第二次重训**（`trained_at 2026-09-25T07:13+08:00`；本轮改动：每折 3 个种子取平均且每个 ≥50 棵树（只为减小波动，没有按留出期结果改任何东西）、
  until_break_close 跌停顺延后锁定卖出、同一只仍持有时不重复买、NW t）。`meta.trade_oos`（模型检验：主板、每天前5、预期≥1%，按比例收费、不受资金限制）：
  全部 594 笔 +1.35%/笔（t 3.03，NW t 2.27；天天随便挑 -0.31%，同日同数量随便挑 +0.86%），选择期 340 笔 +1.67%（t 2.71，NW 2.01；随机 -0.41%，同日同数量 +1.10%），
  **留出期 254 笔 +0.93%（t 1.46，NW t 1.10；随机 -0.04%，同日同数量 +0.48%）——留出期 t 不到 2，还不够可信**；因仍持有跳过 10 次（`held_skipped`）。
  上一次（单种子早停）是 544 笔 +1.69%、留出期 252 笔 +1.70%（t 2.66）：差异来自同时做的几项改动（种子平均、至少 50 棵树、锁定卖出的标签、去重），
  无法单独归因；此前研究里换种子 7/8/9 复算留出期就在 +1.00%~+1.70% 之间，说明单种子的留出期数字波动很大，应以这次的平均结果为准。
  2023 年仍只有 10 笔（那半年模型给出的预期收益普遍低于 1%）；2026-01 那折三个种子都训练到 1442~1493 棵树、IC 只有 0.008。
  按用户账户默认回测（10 万、每只 4%、最低佣金 5 元、一手 100 股、开盘涨幅上限 30%）：606 个信号成交 513 笔，+0.96%/笔（t 2.12，NW 1.66；天天随便挑 -0.80%，
  同日同数量 +0.66%），82 次买不起一手（`skipped_lot`，13.5%，未到 20% 提示线）；**留出期全新账户 191 笔只有 +0.24%/笔（t 0.19，NW 0.20），
  不如同日同数量随机（+0.39%）**，只好过天天随便挑（-0.39%）；选择期 314 笔 +1.49%（t 2.60）。同一批信号逐笔不受资金限制（`per_trade_uncapped`）604 笔 +1.45%。
  结论：比"天天随便挑"多出来的主要是择时（模型检验里只比挑股票多 0.49 个百分点、择时 1.17 个百分点），留出期挑股票的优势在账户回测里没有体现。
  预测页/模型中心的主数字用账户回测，t 值取两个留出期 t（daily_t / nw_t）里低的。
- 参考实现（研究脚本）：`scratchpad/round3/r1_tradeable_label/`。

### 7.1 数据修正（所有 kind）
- 名称含 **"退"** 视同 ST：`universe.name_is_st / st_expr`、`load_universe()`（读取时按名称补 `is_st`，不必重下列表）、`limits.add_limit_columns`、`sentiment.snapshot_rows`。
- `predict/costs.py`：`COMMISSION=0.00025, SLIPPAGE=0.001, MIN_COMMISSION=5, STAMP_CUT_DATE=2023-08-28`；`stamp_duty(day)`（之前 0.1%，之后 0.05%）、
  `stamp_duty_expr / stamp_duty_array`、`Costs(commission, slippage, stamp=None→按日期)`、`from_trade(ts)`、`net_return(entry_raw, exit_raw, exit_day, costs)`。
  回测、swing 标签、`simulate_trades` 统一用它（`TradeSettings.stamp_by_date=True`；False 时用固定 `stamp_duty`）。
- 净化：`model.purge_mask(ds, train_end)`，在 `train_walk_forward(_reg)` 里去掉 `label_end`（无则 `next_date`）> train_end 的训练行（不是在 make_folds 里）。
- 回测：开盘跌停但盘中打开（最高>跌停价）按开盘价卖出；只有一字跌停才顺延。
- 消息面：`news_weight` 默认 **0**；`scoring` 中消息项 = `news_weight * clip(news_z, -2, 2)`。

### 7.2 `swing`（强势股波段）
- 候选池（T 日收盘，`features.candidate_mask("swing")`）：`pct ≥ 5%（SWING_MIN_PCT）`、非涨停收盘、非 no_limit、非 ST（含"退"）、非北交所、收盘≥2、有成交；
  训练用主板/创业板/科创板，评价与默认设置只看 `main`。特征 = `feature_columns("first")`；`build_features_chunked(..., n_chunks=4)` 分块省内存。
- 标签 `labels.swing_labels(feat, panel_lim, delisted, costs, calendar)` → `status, fill, entry_date, entry, exit_date, exit, net, y(net>0), label_end, hold_days, exit_reason`，
  与 `simulate_trades` 同一引擎（`predict/execution.py`：`simulate(signals, panel_lim, *, costs, exit_rule, stop_loss_pct, max_gap_pct, delisted, calendar)`，
  `SWING_FORCE_K=5, SWING_MAX_K=7`，`load_delisted()`）。与研究的差异：k=7 仍跌停按当日收盘记账（研究丢弃）；无涨跌幅限制日可成交；买入日停牌算"未成交"留在池里；
  中途退市按最后收盘价记账。训练用 `clip(net, ±0.15)`。标签不设开盘涨幅上限（`max_gap_pct=None`）。
- until_break_close 的跌停顺延（execution、backtest、labels 三处一致）：某根收盘决定卖出（没涨停，或第 5 根强制）但收在跌停 → **锁定**，
  下一根收盘不管涨停与否都卖（不重新判断是否涨停），仍跌停再顺延，第 7 根仍跌停按收盘价计（`ld_force`）；原因文字沿用决定卖出时的原因 + "（曾因跌停卖不出顺延）"。
- 买不进的原因文字（`execution`）：`SUSPENDED_TEXT`"买入日停牌，没买到"、`ONE_WORD_TEXT`"一字板，买不进"（买入日最低价 ≥ 涨停价-0.005）、
  `LIMIT_UP_TEXT`"开盘就涨停，买不进"（开盘涨停但盘中打开过）、`BAD_OPEN_TEXT`、"开盘涨幅 x% 超过上限 y%，放弃买入"；`PENDING_TEXT / HOLDING_TEXT`。
- 模型：`model.REG_PARAMS`（lr 0.03、leaves 15、min_child 200、ff 0.7、bagging 0.8/1、l2 10、≤1500 轮）、`train_walk_forward_reg(..., seeds=REG_SEEDS, min_rounds=REG_MIN_ROUNDS)`
  （早停看日内去均值 IC；**每折** `fit_regression_seeds`：种子 `REG_SEEDS=(7,8,9)` 各训练一个，早停选出的轮数 < `REG_MIN_ROUNDS=50` 时按 50 轮重训，
  `predict_reg_frame_avg` 把 pred/bias/各维贡献取平均；折指标 `best_iter`=中位数、`best_iters[3]`、`seeds`）、
  最终模型：全部数据、固定轮数 = max(50, 各折各种子树数量中位数×1.1)、**单个种子（7）**（线上预测与样本外评价的模型不是同一个平均，差异来自种子）；
  `predict_reg`（`pred`；`prob` 为空）、`evaluate_reg`（`ic, base_mean/base_win, top{1,3,5,10}_mean/_win`）。编排 `swing.train_model / build_dataset / trade_oos / gate_and_plan`。
- 保存：`MODEL_DIR/swing{.txt,_meta.json,_oos.parquet}`。meta 额外：`objective="regression", label_desc, n_pool, fill_rate, base_return, train_base_return, eval_boards,
  oos_all_boards, calibration[{bucket(预期收益区间),pred,actual,n}], seeds[7,8,9], min_rounds(50)`，`oos`/`folds[]` 为回归指标，
  `trade_oos{config{top_n,threshold,boards,exit_rule,sleeves,holdout_start}, n, signals, held_skipped, days, fill_rate, trades_per_day, mean, median, win_rate, daily_mean, daily_t, nw_t,
  cagr, max_drawdown, total_return, by_year[{year,n,mean,win_rate,return}], start, end, random{...20 次同池随机，天天挑满}, random_matched{同 random 字段，只在出信号的日子挑同样多只},
  selection{同上+random+random_matched}, holdout{同上+random+random_matched}}`
  （收益均为小数；资金分 5 份错开轮动算曲线；`nw_t` = 按日组合收益的 Newey-West t（滞后 5，`predict/stats.py`），持有最多 5 天使相邻几天收益相关，比 daily_t 保守；
  `held_skipped`：同一只股票更早那笔还没卖出（`label_end` > 信号日，或买进后到数据末尾都没卖出）时不再选它（`swing.drop_held`，与账户回测的 `skipped_held` 一致），
  `signals`/`fill_rate` 按去重后的信号算；两种随机基准同样去重，`random_matched` 按去重前每天的个数挑）。
  评价只到最后一个有已完成交易的信号日；不设开盘涨幅上限（和标签一致）。
- `predict_latest("swing")`：`rows` 为全部通过筛选的候选（不按门槛删），按预测净收益降序；行里 `score`=预测净收益（小数）、`exp_ret`（%）、`prob` 为空；
  `pick` = 前 `top_n` 且 `score ≥ threshold`（默认 0.01）。顶层 `gate{trade, reason, n_picks, threshold}`、`plan{buy, sell, position}`（中文三句话；
  `gate_and_plan(picks, ps, ts, signal_date, ok=True, meta=None)`：position 里的历史成绩由 `swing.record_text(meta)` 从 `meta.trade_oos` 计算（全部/留出期每笔、
  留出期 daily_t 与 nw_t 里低的那个，<2 时写"还不够可信"），buy 在 max_gap_pct<20 时才提开盘涨幅上限）、
  `model{trained_at,data_end,oos_topn_hit(胜率),oos_topn_return,oos_topn,base_rate,base_return,auc:null,ic,oos_start,oos_end,trade{扁平摘要}}`。
  所有 kind 另加 `settings_used{predict,trade}`（该名单实际用的设置，前端筛选标签用它）。
- `service`：`KIND_LABELS`（含 swing）、`TRAIN_ORDER=[streak,first,swing]`、`REGRESSION_KINDS={"swing"}`、`notes_for(kind, meta=None)`（swing 的成绩句子由
  `meta.trade_oos` 计算：全部/天天随便挑/同日同数量、只比挑股票多几个百分点与择时几个百分点、留出期 daily_t 与 NW t；佣金与一手的句子由 `settings.defaults_for("swing")` 计算；
  meta 为空时读已保存的 meta；没有写死的数字）；`train("all")` 返回 `{kind: 摘要}`。
  **两处模型状态字段不同**：`GET /api/status` 的 `models.swing` 是 `api/server.model_summary`：meta 的标量字段 + `oos` + **`trade_oos`（meta.trade_oos 去掉列表，
  含嵌套的 selection/holdout/random/random_matched）**；`GET /api/data/status` 的 `service.models.swing`（`service.dataset_status` → `_model_summary`）是扁平的
  **`trade{n,avg_return,median_return,win_rate,daily_t,nw_t,held_skipped,fill_rate,trades_per_day,cagr,max_drawdown,
  baseline_avg_return,baseline_win_rate,baseline_matched_avg_return,baseline_matched_win_rate,selection_{avg_return,daily_t,nw_t,n},selection_baseline(_matched)_avg_return,
  holdout_{avg_return,daily_t,nw_t,n},holdout_baseline_avg_return,holdout_baseline_matched_avg_return,config,basis("per_trade"：按比例收费、不受资金限制)}`**，
  `/api/predict/today` 的 `model.trade` 也是这个扁平摘要。`prediction_for` 按 swing→streak→first 找，swing 另给 `exp_ret, base_exp_ret`（%）；
  `models_meta(kind)` 的 `notes` 用当前版本文字。`daily_pipeline` 结果 `predictions[kind]` 带 `gate`，新增 `paper{kind: 记录条数}`；各 kind 设置用 `paper.settings_for(kind)`（与网页一致）。

### 7.3 回测（所有 kind，`backtest.run_backtest(oos, panel_lim, s, t, daily=None, *, delisted=None, baseline_seeds=20)`）
新增：`metrics.daily_t`（按日收益 t 值）、`metrics.nw_t`（按日收益的 Newey-West t，滞后 5 天）、`metrics.skipped_lot`（钱不够买一手）、
`metrics.fill_rate` = 1 − (unfilled 开盘涨停/一字 + skipped_gap 开盘涨幅超限 + suspended 停牌) / signals（因仓位满/资金不足/一手太贵/已持有跳过的不算买不进）、
`metrics.per_trade_uncapped{trades, avg_return, win_rate}`（同一批信号用 `execution.simulate` 逐笔成交：不受资金/仓位/一手限制、按比例收费，只算已卖出的；
"账户回测比模型检验低多少"里有多少来自资金限制）；顶层 `notes[]`：(skipped_lot + skipped_cash) > 20% × signals 时加一句
"资金不足/一手太贵跳过了 X% 的信号，整体结果主要代表前一段时间（…）"（`service.backtest` 把它放在模型说明前面）；
`baseline{avg_return, win_rate, total_return, cagr, trades, daily_t, nw_t, seeds}`
（同一筛选后的候选池每天随机挑 top_n、不看门槛——包括策略不操作的日子、同样规则，20 次平均）；
`baseline_matched{同 baseline 字段 + same_as_baseline}`（只在策略出信号的日子、每天随机挑和策略同样多只；门槛不起作用时与 baseline 相同，same_as_baseline=true）。
**基准的含义**：策略 − baseline_matched = 挑股票的本事；baseline_matched − baseline = 择时（哪天做、哪天不做）；两种基准都用同样的账户规则（资金、一手、最低佣金、已持有跳过）；
`by_period{selection, holdout}`（2025-07-01 分界，各用全新账户：
`trades, win_rate, avg_return, median_return, total_return, cagr, max_drawdown, daily_t, nw_t, start, end, signals, baseline{avg_return, win_rate, total_return, cagr, trades, daily_t, nw_t, seeds}, baseline_matched{同}}`，
段内两种随机基准也和策略一样**各用全新账户、只走该段的交易日**（2026-09-25 修正：之前取自整段随机账户，整段账户在选择期亏光后留出期只剩几笔，会把"不如随机"颠倒成"好过随机"））；`EXIT_REASONS` 增 `break_close, force_close, ld_force, delisted`；swing 的 `topn_hit` 行带 `avg_return`、
`calibration` 按预期收益分桶。约 1.5~2.5 秒（随机基准占大头，`baseline_seeds=0` 跳过）。
until_break_close 跌停顺延后锁定卖出（见 7.2），与标签、`simulate_trades` 一致（测试 `test_until_break_close_sale_stays_committed_after_limit_down`）。
`simulate_trades(signals, panel_lim, trade=None, *, delisted=None, calendar=None)`：逐笔、不受资金约束；输入 `signal_date, code, kind, exit_rule[, stop_loss_pct]`，
输出加 `status(pending/unfilled/holding/closed), entry_date, entry, exit_date, exit, ret, hold_days, exit_reason`（entry/exit 含滑点；holding 时 exit 为空、ret 为按最新收盘的浮动收益）；
只给少数股票的面板时请传 `calendar`（全市场交易日），否则认不出买入日停牌。

### 7.3b 设置（settings.py）
`KINDS, EXIT_RULES, KIND_DEFAULTS, KIND_FIELDS{predict:[threshold,top_n,boards,max_float_cap,max_price], trade:[position_pct,max_positions,exit_rule]}`；
`KIND_FIELDS.trade` 含 `max_gap_pct`（2026-09-25 起：切换模型时开盘涨幅上限也换成该模型的默认）。
swing 默认：`boards=[main], threshold=0.01, top_n=5, max_float_cap=100000, max_price=10000; position_pct=0.04, max_positions=25, exit_rule=until_break_close,
max_gap_pct=30`（字段上限就是 30，主板最多涨 10% → 等于不设上限：swing 的标签和 `trade_oos` 都没有开盘涨幅上限，默认设置不应悄悄放弃模型算过的买入）；
三套风格的 swing `kind_overrides.trade` 也都是 `max_gap_pct=30`。连板/首板仍为 7%（风格里 5/7/9%）。
`defaults_for(kind) -> {predict, trade}`（未知 kind 抛 SettingsError）；`for_kind(s, kind)`：kind 不同时把 KIND_FIELDS 换成该 kind 默认、其余保留。
只保存一份设置：`paper.settings_for(kind, base=None)`（API `/api/predict/today`、`/api/predict/backtest` 的基础设置、模拟盘、每日流程共用）——
与保存的 kind 同类（streak/first 门槛都是概率）只换 kind，不同类按 `for_kind` 规则换 KIND_FIELDS。

### 7.4 模拟盘 `quant_web/paper.py`（前向跟踪，只记录不下单）
- 文件 `WORKSPACE/paper/`：`signals.parquet`（`signal_date, kind, code, name, rank, score, exp_ret(%), prob, exit_rule, stop_loss_pct, created_at,
  max_gap_pct, fee_rate, slippage, stamp_by_date, stamp_duty, position_pct`，主键 (signal_date, kind, code)；后 6 列是**记录时**的交易设置，算结果一律用它们，
  改设置不改写历史；没给的项为空；早期记录（如 2026-09-24 那 10 条）没有这些列 → 读取时补空列，计算时用该策略**当前**设置补（`_fill_stored`））、
  `days.parquet`（`signal_date, kind, picks, trade, note, created_at`：同一 (日, kind) 只记第一次，含"今天不操作"的日子；note 里另记"xx 还在持有中，不重复买"）。
- `record_signals(kind, result, settings, now=None, strict=True) -> int`（只记 pick 行；swing `gate.trade=False` 时记为不操作日；**同一策略里还"持有中/待买入"
  （signal_date 早于本次的信号，按 `evaluate()`）的股票不记**，与回测的已持有跳过一致；算不出当前持仓时不做这项检查）；
  `record_today(predict_fn=None, settings_fn=None, now=None, kinds=None) -> {signal_date, kinds{kind:{status,recorded,picks,held_skipped,signal_date,message}}, recorded, message}`
  （`held_skipped` = 因仍持有没记的只数；选中的全都仍持有时 status=no_trade、message 说明）；
  （status：recorded/no_trade/already/stale/too_early/no_data/error；交易日 15:05 前或数据不是最新时抛 RuntimeError 中文）；
  `record_block_reason(now) / expected_signal_day(now) / pending_kinds(day) / recorded_kinds(day)`；`settings_for(kind, base) / with_kind(s, kind)`。
- `evaluate(trade=None) -> DataFrame`（用 `simulate_trades` + 全市场日历；按 (kind + 记录时的费用/滑点/开盘涨幅上限) 分组模拟；按信号文件/面板/补用设置缓存；
  `trade` 只用来补早期记录缺的项）；
  `summary() -> {since, kinds{kind:{label, since, last_signal_date, days, no_trade_days, signals, filled, unfilled, pending, open, closed, win_rate, avg_return, open_return,
  avg_hold_days, total_return, position_pct, equity[{date,value}]}}, updated_at, data_end, equity_method, warnings}`：
  **收益曲线按账户算**（`equity_curve(frame, closes, start, default_pct)`）：每笔持仓占它的 `position_pct`（记录时设置里的单只仓位；早期记录：swing 4%（`settings.defaults_for`），
  其他用当前设置），其余资金收益为 0；当天账户收益 = Σ 仓位 × 当天收益（每天按当日开始时的净值计仓位，近似），同时持有的仓位合计 > 100% 时按比例缩到 100%；
  `total_return` 取这条曲线；`win_rate/avg_return` 仍是每笔的；`position_pct` = 最近一条信号的仓位（没有信号时为该策略默认）；`equity_method` 为中文说明。
  `trades(kind=None, status=None, limit=500) -> [signals 列 + status, entry_date, entry, exit_date, exit, ret, hold_days, exit_reason, kind_label, status_label]`（未知 kind/status 抛 ValueError→400）；
  `exit_reason` 文字与 `execution` 一致："一字板，买不进"只在买入日最低价 ≥ 涨停价−0.005 时，否则"开盘就涨停，买不进"；停牌 = `execution.SUSPENDED_TEXT`；待买入 = `PENDING_TEXT`。
- 自动记录：`service.daily_pipeline` 预测后记三个 kind；`scheduler` 在已是最新数据但当天没记时提交 `paper` 任务（`jobs.run_paper` → `record_today`）。
- API：`GET /api/paper/summary`、`GET /api/paper/trades?kind=&status=&limit=`、`POST /api/paper/record`（409 + 中文原因）。
- 2026-09-24 信号已在真实工作区记录：streak 5 只、first 5 只、swing "今天不操作"（最高预期收益 -0.58%；第二次重训后的模型同一天最高 -0.66%，同样不操作）。
  这 10 条是旧格式（没有记录时设置的列），按当前设置（默认：开盘涨幅上限 7%、佣金 0.025%、滑点 0.1%、印花税按日期、单只 20%）计算，与记录时一致；
  2026-09-25（节假日）重新计算：10 条都是"待买入"，summary 正常。

### 7.5 前端
- `#/predict` 三个标签：**强势股波段（模拟跟踪中，默认）**、连板晋级（观察）、首板潜力（观察）。顶部"照着买能赚钱吗 / 靠得住吗（t 值；swing 用留出期 t）/ 买得进吗"：
  swing 的主数字用 `POST /api/predict/backtest`（按用户本金，最低佣金、一手）的结果，`model.trade`（模型检验，不受资金限制）放在旁边标"偏乐观"；
  t 值取两者留出期 t 里低的；swing 的可信度框永远不用 ok（绿/蓝"可信"）色调。另两个用 `POST /api/predict/backtest` 的 metrics+baseline+by_period（`QW.normTrade` 统一字段，
  另有 `match`=同日同数量随机）；`QW.BASE_HELP / MATCH_HELP / TUNED_HELP` 为统一文案；
  swing 显示闸门卡（gate/plan）；筛选标签用 `settings_used.predict`；名单行数 = `count`（超过 200 行只显示前 200）。
- `#/paper` 模拟盘：三个策略卡（含"不操作"天数）、收益曲线、逐笔明细（待买入/未成交/持有中/已卖出）、"记录今天的信号"。
- `#/settings`：切换 kind 载入 `/api/settings/defaults`；swing 门槛为"预期收益%"、`until_break_close`；检验结果含随机基准、t 值、选择期/留出期（含段内随机）、`skipped_lot`。
- `#/model`：swing 卡片主数字=按你的本金回测（另列模型检验、天天随便挑），选择期/留出期表分"按你的本金回测"与"模型检验"两组（含同日同数量列）；`#/data`：三个模型的状态与训练选项；`#/watch`：swing 候选显示"AI波段评估"（预期收益、今天候选平均）。
- `#/settings` 检验结果：对比表另有"同日同数量随便挑 / 只比挑股票多赚"（门槛起作用时）；`tuned.modified` 时显示"你改过推荐设置"横幅（含本机检验过几套），t 值不再标绿。
- `#/etf?tab=backtest`：标签"历史回测（样本内）"，顶部横幅说明参数是用同一段历史选的、成绩偏乐观（`in_sample_note`）。
