/* 模拟盘 #/paper：前向跟踪（只记录、不下单）——说明、各名单汇总、收益曲线、每笔明细（状态/名单筛选）、手动记录今天的信号 */
(function () {
  "use strict";
  const { ref, computed, watch, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, toast, isNum, colors, tooltipBase, axisBase, KINDS, kindInfo, kindLabel } = QW;

  const STATUS = [
    { value: "pending", label: "待买入", cls: "blue", help: "收盘后记下的信号，等下一个交易日开盘按开盘价买入。" },
    { value: "unfilled", label: "未成交", cls: "", help: "第二天一开盘就涨停（或停牌、高开太多），按规则买不进，这笔不算盈亏。" },
    { value: "holding", label: "持有中", cls: "warn", help: "已经按开盘价买入，还没到卖出条件；收益是按最新收盘价算的浮动盈亏。" },
    { value: "closed", label: "已卖出", cls: "green", help: "已经按规则卖出，收益已扣除手续费、印花税和滑点。" },
  ];
  const STATUS_MAP = Object.fromEntries(STATUS.map((s) => [s.value, s]));
  const HELP = {
    intro: "模拟盘只记录、不下单：每天收盘后把名单里带 ★ 的股票记下来，之后每天用真实行情按该名单的买卖规则算盈亏。它和“历史回测”不同——记录时谁也不知道后面的行情，所以更能说明真实水平。",
    signals: "一共记下了多少条信号（每只股票每天算一条）。",
    filled: "第二天开盘真的能按规则买进的笔数。一开盘就涨停买不进的算“未成交”。",
    win: "已经卖出的交易里，赚钱的占多少（已扣费用）。",
    avg: "已经卖出的交易，平均每笔赚或亏多少（已扣手续费、印花税和滑点）。",
    // 累计收益的算法以后端 summary.equity_method 为准（页面直接显示那句话）；老版本后台没给时用这句不含具体算法的说明
    total: "从开始记录那天算起的累计收益（按收益曲线最后一天算，含持有中的浮动盈亏），和真实账户可能有差别。",
    curve: "每条线是一个名单的累计收益（从开始记录那天算起）。只记录真实发生之后的行情，没有用历史数据“事后”补。",
  };

  // 模拟盘收益可能是净值（从 1 开始）、累计收益（从 0 开始）或金额，统一成累计收益百分数
  function toCumPct(eq) {
    const pts = (eq || []).filter((p) => p && p.date && isNum(p.value));
    if (!pts.length) return [];
    const first = pts[0].value;
    const vals = pts.map((p) => p.value);
    let mode = "money";
    if (Math.abs(first - 1) < 0.25 && vals.every((v) => v > 0 && v < 20)) mode = "nav";
    else if (Math.abs(first) < 0.25 && vals.every((v) => Math.abs(v) < 5)) mode = "cum";
    return pts.map((p) => {
      const v = mode === "nav" ? p.value - 1 : mode === "cum" ? p.value : first ? p.value / first - 1 : 0;
      return [String(p.date).slice(0, 10), +(v * 100).toFixed(2)];
    });
  }

  function curveOption(series) {
    const c = colors();
    const ax = axisBase(c);
    const palette = { swing: c.series[0], streak: c.series[1], first: c.series[4] };
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 14, itemHeight: 3, icon: "rect", textStyle: { color: c.text2, fontSize: 12 } },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true, axisPointer: { type: "line", lineStyle: { color: c.text3 } },
        formatter: (ps) => {
          if (!ps.length) return "";
          const d = ps[0].value && ps[0].value[0];
          return `<div style="font-weight:700;margin-bottom:4px">${d}</div>` + ps.map((p) => {
            const v = p.value[1];
            const col = v > 0 ? c.up : v < 0 ? c.down : c.text;
            return `<div style="display:flex;justify-content:space-between;gap:16px"><span>${p.marker}${p.seriesName}</span><b style="color:${col}">${v > 0 ? "+" : ""}${v.toFixed(2)}%</b></div>`;
          }).join("");
        },
      },
      grid: { left: 52, right: 16, top: 34, bottom: 28 },
      xAxis: { type: "time", ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true, formatter: (v) => { const d = new Date(v); return `${d.getMonth() + 1}/${d.getDate()}`; } } },
      yAxis: { type: "value", scale: true, splitNumber: 4, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: series.map((s) => ({
        name: s.label, type: "line", data: s.data, showSymbol: s.data.length < 30, symbolSize: 5, lineStyle: { width: 2, color: palette[s.kind] || c.series[2] },
        itemStyle: { color: palette[s.kind] || c.series[2] }, emphasis: { focus: "series" },
        markLine: s.first ? { symbol: "none", silent: true, label: { show: false }, lineStyle: { type: "dashed", color: c.text3, width: 1 }, data: [{ yAxis: 0 }] } : undefined,
      })),
    };
  }

  // 字段宽松兼容：entry/entry_price、exit/exit_price、ret/return
  const numOf = (o, keys) => { for (const k of keys) if (o && isNum(o[k])) return o[k]; return null; };
  function normRow(t, i) {
    const status = String(t.status || "").toLowerCase();
    return {
      ...t, _k: `${t.signal_date || ""}-${t.kind || ""}-${t.code || i}`,
      _status: STATUS_MAP[status] ? status : status || "pending", _statusLabel: t.status_label || status || "—",
      _entry: numOf(t, ["entry", "entry_price", "buy_price"]), _exit: numOf(t, ["exit", "exit_price", "sell_price"]),
      _ret: fmt.frac(numOf(t, ["ret", "return", "pnl_pct"]), 1.5), _hold: numOf(t, ["hold_days", "days"]),
      _exp: numOf(t, ["exp_ret"]), _prob: numOf(t, ["prob", "score"]),
    };
  }
  // POST /api/paper/record 的返回：{"signal_date","kinds":{kind:{status,recorded,picks,message}},"recorded":N,"message"}
  // （也兼容纯数字、{count}、{recorded:{kind:n}}）
  function recordText(r) {
    if (isNum(r)) return { n: r, text: `新记下 ${r} 条信号` };
    if (!r || typeof r !== "object") return { n: null, text: "已记录今天的信号" };
    const cnt = (v) => (isNum(v) ? v : v && typeof v === "object" ? numOf(v, ["recorded", "count", "added", "n"]) : null);
    const per = [r.kinds, r.by_kind, r.recorded, r.added].find((x) => x && typeof x === "object" && !Array.isArray(x));
    const parts = per ? Object.keys(per).map((k) => ({ k, n: cnt(per[k]) })).filter((x) => isNum(x.n)) : [];
    let n = numOf(r, ["recorded", "count", "added", "n", "total"]);
    if (!isNum(n) && parts.length) n = parts.reduce((a, x) => a + x.n, 0);
    const hit = parts.filter((x) => x.n > 0);
    // held_skipped：这只股票还在模拟盘里持有（之前的信号还没卖），这次不重复记
    const held = per ? Object.keys(per).map((k) => ({ k, n: per[k] && typeof per[k] === "object" ? numOf(per[k], ["held_skipped"]) : null })).filter((x) => isNum(x.n) && x.n > 0) : [];
    const heldN = held.reduce((a, x) => a + x.n, 0) || numOf(r, ["held_skipped"]) || 0;
    const heldText = heldN ? `；另有 ${heldN} 只还在模拟盘里持有，这次没重复记` + (held.length ? "（" + held.map((x) => `${kindLabel(x.k)} ${x.n} 只`).join("、") + "）" : "") : "";
    const text = isNum(n) ? `新记下 ${n} 条信号` + (hit.length ? "（" + hit.map((x) => `${kindLabel(x.k)} ${x.n} 条`).join("、") + "）" : "") + heldText : (r.message || "已记录今天的信号") + heldText;
    return { n, text, heldN, heldText, msg: isNum(n) ? r.message || "" : "" };
  }

  QW.page("paper", {
    props: ["params", "query"],
    setup(props) {
      const summary = ref(null);
      const trades = ref([]);
      const loading = ref(true);
      const tradesLoading = ref(false);
      const err = ref("");
      const unavailable = ref(false);
      const recording = ref(false);
      const q = props.query || {};
      const kindF = ref(KINDS.some((k) => k.value === q.kind) ? q.kind : "");
      const statusF = ref(STATUS_MAP[q.status] ? q.status : "");

      const loadSummary = async () => {
        try {
          summary.value = await api.get("/api/paper/summary", null, { silent: true });
          err.value = "";
          unavailable.value = false;
        } catch (e) {
          summary.value = null;
          unavailable.value = e.status === 404 || e.status === 405;
          err.value = unavailable.value ? "" : e.detail || e.message;
        }
      };
      let tseq = 0;
      const loadTrades = async () => {
        const my = ++tseq;
        tradesLoading.value = true;
        try {
          const r = await api.get("/api/paper/trades", { kind: kindF.value, status: statusF.value, limit: 500 }, { silent: true });
          if (my !== tseq) return;
          const list = Array.isArray(r) ? r : (r && (r.trades || r.rows)) || [];
          trades.value = list.map(normRow);
        } catch (e) {
          if (my === tseq) trades.value = [];
        } finally {
          if (my === tseq) tradesLoading.value = false;
        }
      };
      const load = async () => {
        loading.value = true;
        await Promise.all([loadSummary(), loadTrades()]);
        loading.value = false;
      };
      onMounted(load);
      const off = bus.on("job-done", (j) => { if (j && (j.name === "daily" || j.name === "update")) load(); });
      onBeforeUnmount(off);
      watch([kindF, statusF], () => {
        const qq = {};
        if (kindF.value) qq.kind = kindF.value;
        if (statusF.value) qq.status = statusF.value;
        QW.go("/paper", qq);
        loadTrades();
      });

      const record = async () => {
        recording.value = true;
        try {
          const r = await api.post("/api/paper/record", {});
          const t = recordText(r);
          if (t.n === 0) toast.info((t.msg || "今天的信号之前已经记过了（或者今天没有信号），不会重复记录。") + (t.heldText || ""), { duration: 8000 });
          else toast.success(t.text, { duration: 6000 });
          await load();
        } catch (e) { /* 已提示 */ } finally { recording.value = false; }
      };

      const kindsData = computed(() => (summary.value && summary.value.kinds) || {});
      const tiles = computed(() => KINDS.map((k) => {
        const s = kindsData.value[k.value] || null;
        const g = (keys) => numOf(s, keys);
        return {
          ...k, has: !!s,
          signals: g(["signals", "n_signals"]), filled: g(["filled"]), unfilled: g(["unfilled"]), open: g(["open", "holding"]),
          closed: g(["closed"]), pending: g(["pending"]), days: g(["days"]), noTrade: g(["no_trade_days"]), last: s && s.last_signal_date,
          win: fmt.frac(g(["win_rate", "win"]), 1.5), avg: fmt.frac(g(["avg_return", "mean"]), 0.5), total: fmt.frac(g(["total_return", "cum_return"]), 50),
          posText: isNum(g(["position_pct"])) ? +(fmt.frac(g(["position_pct"]), 1.5) * 100).toFixed(1) + "%" : "",
        };
      }));
      const totalSignals = computed(() => tiles.value.reduce((a, t) => a + (t.signals || 0), 0));
      const isEmpty = computed(() => !loading.value && !unavailable.value && !err.value && !totalSignals.value && !trades.value.length && !kindF.value && !statusF.value
        && !tiles.value.some((t) => t.days));
      const curve = computed(() => {
        const out = [];
        KINDS.forEach((k) => {
          const s = kindsData.value[k.value];
          const data = toCumPct(s && s.equity);
          if (data.length) out.push({ kind: k.value, label: k.label, data, first: !out.length });
        });
        return out;
      });
      const curveOpt = computed(() => (curve.value.length ? curveOption(curve.value) : null));
      const closedCount = computed(() => tiles.value.reduce((a, t) => a + (t.closed || 0), 0));
      // 累计收益怎么算：直接用后端的那句话（equity_method），保证和曲线的真实算法一致
      const method = computed(() => (summary.value && typeof summary.value.equity_method === "string" && summary.value.equity_method) || "");
      const totalHelp = computed(() => (method.value ? "累计收益：" + method.value : HELP.total));
      const curveHelp = computed(() => HELP.curve + (method.value ? "\n" + method.value : ""));

      // 各状态的笔数取自汇总（明细是按筛选取回来的，不能拿来数）
      const statusCounts = computed(() => {
        const c = { pending: 0, unfilled: 0, holding: 0, closed: 0 };
        tiles.value.filter((t) => t.has && (!kindF.value || t.value === kindF.value)).forEach((t) => {
          const un = t.unfilled || 0;
          const op = t.open || 0;
          const cl = t.closed || 0;
          c.unfilled += un;
          c.holding += op;
          c.closed += cl;
          c.pending += isNum(t.pending) ? t.pending : Math.max(0, (t.signals || 0) - un - op - cl);
        });
        return c;
      });
      // 后端没按筛选返回时，前端再筛一次
      const shown = computed(() => trades.value.filter((t) => (!kindF.value || t.kind === kindF.value) && (!statusF.value || t._status === statusF.value)));
      const columns = computed(() => [
        { key: "signal_date", label: "信号日", sortable: true, help: "程序记下这条信号的那天（收盘后）。" },
        kindF.value ? null : { key: "kind", label: "名单", sortable: true, sortBy: (r) => kindLabel(r.kind) },
        { key: "name", label: "股票", minWidth: "96px" },
        { key: "_status", label: "状态", sortable: true, sortBy: (r) => STATUS.findIndex((s) => s.value === r._status) },
        { key: "_entry", label: "买入", align: "right", sortable: true, sortBy: (r) => r.entry_date || "" },
        { key: "_exit", label: "卖出", align: "right", sortable: true, sortBy: (r) => r.exit_date || "" },
        { key: "_ret", label: "收益", align: "right", sortable: true, help: "已扣手续费、印花税和滑点。持有中的是按最新收盘价算的浮动盈亏。" },
        { key: "_hold", label: "持有", align: "right", sortable: true, help: "买入后持有了几个交易日。" },
        { key: "_score", label: "记录时模型的估计", align: "right", help: "强势股波段：记录时模型估算的预期收益（扣费后，平均值）；连板晋级、首板潜力：模型估算的第二天涨停概率——它不是收益，照着买历史上是亏的。", sortBy: (r) => (isNum(r._exp) ? r._exp : r._prob) },
      ].filter(Boolean));
      const statusOpts = computed(() => [{ value: "", label: "全部状态" }].concat(STATUS.map((s) => ({ ...s, n: statusCounts.value[s.value] }))));
      const kindOpts = computed(() => [{ value: "", label: "全部名单" }].concat(KINDS.map((k) => ({ value: k.value, label: k.label }))));

      return {
        store, fmt, HELP, STATUS, STATUS_MAP, KINDS, kindInfo, kindLabel, summary, trades, loading, tradesLoading, err, unavailable, recording, record, load,
        kindF, statusF, tiles, totalSignals, isEmpty, curve, curveOpt, closedCount, shown, columns, statusOpts, kindOpts, method, totalHelp, curveHelp,
      };
    },
    template: `<div class="stack paper-page">
      <div class="paper-intro">
        <span class="pi-ico"><qw-icon name="clipboard" :size="24"/></span>
        <div class="pi-body">
          <h2>模拟盘：用真实行情检验，不用真钱 <qw-help :text="HELP.intro"/></h2>
          <p>程序每天收盘后自动记下三个名单里带 ★ 的股票，之后用真实行情自动算盈亏，不用真钱；<b>至少积累几十笔再看结果</b>，几笔的输赢说明不了什么。</p>
          <p class="muted pi-meta" v-if="summary && (summary.since || summary.updated_at)">
            <span v-if="summary.since">从 {{ $fmt.cnDate(summary.since) }} 开始记录</span><span v-if="summary.updated_at">盈亏算到 {{ $fmt.datetime(summary.updated_at) }}</span>
          </p>
        </div>
        <div class="pi-act">
          <button class="btn primary" :disabled="recording || unavailable" @click="record"><qw-icon name="plus" :size="15"/>{{ recording ? '正在记录…' : '记录今天的信号' }}</button>
          <small class="muted">一般不用点：开着量化助手时每天收盘后会自动记</small>
        </div>
      </div>

      <template v-if="loading && !summary">
        <qw-skeleton type="tiles" :count="3"/>
        <qw-card><qw-skeleton height="260px"/></qw-card>
      </template>

      <qw-card v-else-if="unavailable">
        <qw-empty icon="clipboard" title="模拟盘功能还没准备好" desc="后台程序还不支持模拟盘（可能是版本较旧）。更新量化助手后再来看。" action-text="重新加载" @action="load"/>
      </qw-card>
      <qw-card v-else-if="err && !summary">
        <qw-empty icon="alert" title="模拟盘数据暂时读取不到" :desc="err" action-text="重新加载" @action="load"/>
      </qw-card>

      <qw-card v-else-if="isEmpty">
        <qw-empty icon="clipboard" title="模拟盘里还没有记录" desc="每天收盘、数据更新完后，程序会自动把三个名单里带 ★ 的股票记下来（需要开着量化助手）；也可以现在手动记一次。记下之后的每个交易日，会用真实行情自动算盈亏。">
          <button class="btn primary" :disabled="recording" @click="record"><qw-icon name="plus" :size="15"/>记录今天的信号</button>
          <a class="btn" href="#/predict?kind=swing">先看看今天的名单</a>
        </qw-empty>
      </qw-card>

      <template v-else>
        <div v-if="summary && summary.warnings && summary.warnings.length" class="qw-banner warn">
          <qw-icon name="alert" :size="18"/>
          <div class="qw-banner-body"><div v-for="(w, i) in summary.warnings" :key="i">{{ w }}</div></div>
        </div>
        <div class="paper-kinds">
          <div v-for="t in tiles" :key="t.value" class="pk-card" :class="{main: t.value === 'swing', none: !t.has}">
            <div class="pk-hd">
              <qw-icon :name="t.icon" :size="16"/><b>{{ t.label }}</b>
              <span class="qw-tag" :class="t.tone === 'sim' ? 'blue' : ''">{{ t.badge }}</span>
              <a class="pk-link" :href="'#/predict?kind=' + t.value" v-tip="'看今天的' + t.label + '名单'"><qw-icon name="chevronRight" :size="15"/></a>
            </div>
            <div class="pk-stats">
              <div><div class="k">信号数<qw-help :text="HELP.signals"/></div><div class="v num">{{ $fmt.int(t.signals ?? 0) }}</div></div>
              <div><div class="k">已成交<qw-help :text="HELP.filled"/></div><div class="v num">{{ $fmt.int(t.filled ?? 0) }}</div><div class="s" v-if="t.unfilled || t.open">{{ t.unfilled ? '未成交 ' + t.unfilled : '' }}{{ t.unfilled && t.open ? ' · ' : '' }}{{ t.open ? '持有 ' + t.open : '' }}</div></div>
              <div><div class="k">胜率<qw-help :text="HELP.win"/></div><div class="v num">{{ $fmt.ratio(t.win, 0) }}</div></div>
              <div class="key"><div class="k">平均每笔<qw-help :text="HELP.avg"/></div><div class="v num" :class="$fmt.dir(t.avg)">{{ $fmt.ratio(t.avg, 2, true) }}</div></div>
              <div class="key"><div class="k">累计<qw-help :text="totalHelp"/></div><div class="v num" :class="$fmt.dir(t.total)">{{ $fmt.ratio(t.total, 1, true) }}</div><div class="s" v-if="t.posText">每笔占资金 {{ t.posText }}</div></div>
            </div>
            <p class="pk-note" v-if="(!t.has || !t.signals) && t.days">已记录 {{ t.days }} 天<template v-if="t.noTrade">，其中 {{ t.noTrade }} 天“今天不操作”（没有达标的股票）</template><template v-if="t.last">，最近一次 {{ $fmt.cnDate(t.last) }}</template>。</p>
            <p class="pk-note" v-else-if="!t.has || !t.signals">还没有记录。</p>
            <p class="pk-note" v-else-if="(t.closed || 0) < 30">已卖出 {{ t.closed || 0 }} 笔，<b>样本还太少</b>，先别急着下结论。<template v-if="t.tone === 'watch'">这是观察名单，记下来只为看看“照着买会怎样”（历史上是亏的）。</template></p>
            <p class="pk-note" v-else>已卖出 {{ t.closed }} 笔，可以开始和历史回测对比着看了。</p>
          </div>
        </div>

        <qw-card title="累计收益曲线" icon="trend" :help="curveHelp" :sub="curve.length ? '从开始记录那天算起' : ''">
          <qw-chart v-if="curveOpt" :option="curveOpt" height="280px"/>
          <qw-empty v-else compact icon="trend" title="还没有能画曲线的数据" desc="信号记下后，要等第二天开盘买入、之后卖出，才会有盈亏。过几个交易日再来看。"/>
          <p v-if="method" class="paper-method"><qw-icon name="info" :size="13"/><span><b>怎么算的：</b>{{ method }}</span></p>
        </qw-card>

        <qw-card :pad="false" title="每笔明细" icon="clipboard" :sub="shown.length ? '共 ' + shown.length + ' 条，点击一行看盘' : ''">
          <div class="paper-filters">
            <div class="chips-wrap">
              <button v-for="o in kindOpts" :key="'k' + o.value" type="button" class="chip" :class="{active: kindF === o.value}" @click="kindF = o.value">{{ o.label }}</button>
            </div>
            <div class="chips-wrap">
              <button v-for="o in statusOpts" :key="'s' + o.value" type="button" class="chip" :class="{active: statusF === o.value}" @click="statusF = o.value" v-tip="o.help || ''">{{ o.label }}<small v-if="o.n" class="muted">{{ o.n }}</small></button>
            </div>
          </div>
          <div v-if="store.isPhone" class="paper-cards">
            <qw-skeleton v-if="tradesLoading && !shown.length" :rows="4"/>
            <div v-for="t in shown.slice(0, 80)" :key="t._k" class="paper-card" @click="$go('/watch/' + t.code)">
              <div class="r1"><qw-stock :code="t.code" :name="t.name"/><span class="qw-tag" :class="STATUS_MAP[t._status] ? STATUS_MAP[t._status].cls : ''">{{ STATUS_MAP[t._status] ? STATUS_MAP[t._status].label : t._statusLabel }}</span><span class="grow"></span><qw-price v-if="t._ret != null" :value="t._ret" ratio :digits="2"/></div>
              <div v-if="t.exit_reason && (t._status === 'unfilled' || t._status === 'closed')" class="r3">{{ t.exit_reason }}</div>
              <div class="r2 muted"><span>{{ kindLabel(t.kind) }}</span><span>信号 {{ $fmt.md(t.signal_date) }}</span><span v-if="t._exp != null">预期收益 {{ $fmt.pct(t._exp, 2) }}</span><span v-else-if="t._prob != null">涨停概率 {{ $fmt.ratio($fmt.frac(t._prob, 1.5), 0) }}</span><span v-if="t._entry != null">买 {{ $fmt.price(t._entry) }}</span><span v-if="t._exit != null">卖 {{ $fmt.price(t._exit) }}</span><span v-if="t._hold != null">{{ t._hold }} 天</span></div>
            </div>
            <qw-empty v-if="!tradesLoading && !shown.length" compact icon="filter" title="没有符合条件的记录" desc="换个名单或状态看看。"/>
          </div>
          <qw-table v-else :columns="columns" :rows="shown" row-key="_k" :page-size="15" :default-sort="{key:'signal_date',order:'desc'}" :loading="tradesLoading && !shown.length"
            clickable dense empty-text="没有符合条件的记录" @row-click="(r) => $go('/watch/' + r.code)">
            <template #cell-signal_date="{row}"><span class="num nowrap">{{ $fmt.date(row.signal_date) }}</span></template>
            <template #cell-kind="{row}"><span class="qw-tag" :class="row.kind === 'swing' ? 'blue' : ''">{{ kindLabel(row.kind) }}</span></template>
            <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
            <template #cell-_status="{row}"><span class="qw-tag" :class="STATUS_MAP[row._status] ? STATUS_MAP[row._status].cls : ''" v-tip="row.exit_reason || (STATUS_MAP[row._status] ? STATUS_MAP[row._status].help : '')">{{ STATUS_MAP[row._status] ? STATUS_MAP[row._status].label : row._statusLabel }}</span><small v-if="row.exit_reason && (row._status === 'unfilled' || row._status === 'closed')" class="muted cell-date">{{ row.exit_reason }}</small></template>
            <template #cell-_entry="{row}"><template v-if="row._entry != null"><span class="num">{{ $fmt.price(row._entry) }}</span><small class="muted cell-date">{{ $fmt.md(row.entry_date) }}</small></template><span v-else class="muted">—</span></template>
            <template #cell-_exit="{row}"><template v-if="row._exit != null"><span class="num">{{ $fmt.price(row._exit) }}</span><small class="muted cell-date">{{ $fmt.md(row.exit_date) }}</small></template><span v-else class="muted">—</span></template>
            <template #cell-_ret="{row}"><qw-price v-if="row._ret != null" :value="row._ret" ratio :digits="2"/><span v-else class="muted">—</span><small v-if="row._status === 'holding' && row._ret != null" class="muted cell-date">浮动</small></template>
            <template #cell-_hold="{row}"><span class="num">{{ row._hold != null ? row._hold + ' 天' : '—' }}</span></template>
            <template #cell-_score="{row}"><span class="score-est"><small class="muted">{{ row._exp != null ? '预期收益' : row._prob != null ? '涨停概率' : '' }}</small><span class="num muted">{{ row._exp != null ? $fmt.pct(row._exp, 2) : row._prob != null ? $fmt.ratio($fmt.frac(row._prob, 1.5), 1) : '—' }}</span></span></template>
          </qw-table>
        </qw-card>

        <div class="qw-banner info">
          <qw-icon name="info" :size="18"/>
          <div class="qw-banner-body">怎么看模拟盘：先看<b>强势股波段</b>的“平均每笔”能不能长期保持为正，并和<a href="#/predict?kind=swing">预测页</a>上方的历史成绩（尤其是最近一段“留出期”、以及同一天随便挑的对比）对照着看。积累 50 笔以上还满意，再考虑用很小的仓位试真钱；
            如果一直是负的，就说明这套方法现在不管用了。连板晋级、首板潜力只是观察名单，它们的模拟结果主要用来提醒“照着买会怎样”。</div>
        </div>
      </template>
    </div>`,
  });
})();
