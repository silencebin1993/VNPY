/* 稳健ETF #/etf：本周建议（初始化/订单/目标持仓/理由/按建议下单）、我的持仓（可编辑）、历史回测（收益/回撤/年度/大类仓位） */
(function () {
  "use strict";
  const { ref, computed, watch, onMounted } = Vue;
  const { api, fmt, store, toast, go, jobs, isNum, colors, tooltipBase, axisBase } = QW;

  const TABS = [{ value: "advice", label: "本周建议" }, { value: "holdings", label: "我的持仓" }, { value: "backtest", label: "历史回测（样本内）" }];
  const CLASS_ORDER = ["A股大盘", "A股成长", "美股", "黄金", "债券", "商品", "避险仓", "现金管理", "货币基金/现金"];
  const CLASS_SLOT = { A股大盘: 0, A股成长: 4, 美股: 5, 黄金: 3, 债券: 2, 商品: 1, 避险仓: 2 };  // 相邻类别的颜色已用色盲校验脚本检查
  const CLASS_NOTES = {
    A股大盘: "中国大公司股票", A股成长: "中国中小盘、科技成长股票", 美股: "美国股票（QDII基金）", 黄金: "避险资产",
    债券: "国债，收益稳定", 商品: "大宗商品，和股票走势关系不大", 避险仓: "股票走弱时的避风港（国债）",
    现金管理: "货币基金，相当于活期理财", "货币基金/现金": "货币基金或现金",
  };
  const STARTS = [
    { value: "2014-06-01", label: "2014年6月起（最长）" }, { value: "2016-01-01", label: "2016年起" },
    { value: "2018-01-01", label: "2018年起" }, { value: "2020-01-01", label: "2020年起" }, { value: "2022-01-01", label: "2022年起" },
  ];
  const STEPS = [
    ["分散", "把钱分到 A股大盘、A股成长、美股、黄金、债券、商品 6 类资产，不把鸡蛋放在一个篮子里。"],
    ["看趋势", "只买价格在 200 日均线之上、最近在上涨的 ETF；已经持有的跌破均线 5% 才卖，躲开长期下跌。"],
    ["挑最强", "每一类只买走势最强的一只，新的明显更强才换，减少来回买卖。"],
    ["控风险", "波动大的少买、波动小的多买，单只最多 30%；整体波动超过 10% 时自动降低仓位。"],
    ["每周一次", "每周最后一个交易日收盘后计算，下周一开盘按建议下单。平时不用盯盘。"],
  ];
  const HELP = {
    vol: "组合波动：按历史估算这套配置一年里大概会上下波动多少。越小越稳。策略把它控制在 10% 以内。",
    cashAfter: "按建议买卖完成后，账户里大约还剩多少现金（按参考价估算，已扣手续费）。",
    order: "请按顺序操作：先卖出，腾出资金后再买入。数量按 100 份一手取整，参考价是最近的收盘价，实际成交价会有小差别。",
    target: "本周建议的目标持仓：每只 ETF 应该持有多少份，以及占总资产的比例。",
    reason: "每一类资产本周为什么买、换或者不持有。",
    momentum: "动量：最近 1、3、6 个月涨幅的平均值，越高说明走势越强。",
    trend: "趋势：价格是否在 200 日均线之上。在均线之上说明处于长期上涨趋势。",
    cagr: "年化收益：把整段时间的总收益折算成“平均每年赚多少”。",
    dd: "最大回撤：资产从某个最高点最多往下跌了多少，代表中途可能要忍受的最大亏损。",
    sharpe: "夏普比率：每承担一份波动换来多少收益，越高越好，大于1算不错。",
    under: "最长回本时间：从一个高点跌下来，最久花了多少天才重新回到那个高点。",
    posYears: "有多少比例的年份是赚钱的。这是样本内结果（参数就是参考这段历史选的），以后遇到亏钱的年份很正常。",
    vol2: "年化波动：资产每年上下波动的幅度，越小越稳。",
  };
  // 历史回测是样本内的：etf_quant 的参数就是参考 2014~2026 年这段历史选定的（后端 in_sample_note 同义）
  const IN_SAMPLE_NOTE = "这是“样本内”回测：策略的参数本来就是参考 2014~2026 年这段历史检验、选定的，再拿同一段历史来检验，成绩自然好看（相当于看过答案再考试），以后实际的效果通常会更差。";
  const shortMoney = (v) => {
    if (!isNum(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (a >= 1e4) return (v / 1e4).toFixed(a >= 1e6 ? 0 : 1).replace(/\.0$/, "") + "万";
    return String(Math.round(v));
  };
  const mixHex = (a, b, t) => {
    const pa = String(a).replace("#", "");
    const pb = String(b).replace("#", "");
    if (pa.length !== 6 || pb.length !== 6) return a;
    const ch = (s, i) => parseInt(s.slice(i, i + 2), 16);
    return "#" + [0, 2, 4].map((i) => Math.round(ch(pa, i) * (1 - t) + ch(pb, i) * t).toString(16).padStart(2, "0")).join("");
  };
  // 颜色跟着资产大类走（不随排序变化）；避险仓是国债，用债券色的浅一档；货币基金/现金为中性灰
  const classColor = (c, name) => {
    if (name === "避险仓") return mixHex(c.series[CLASS_SLOT["债券"]], c.surface, 0.45);
    return name in CLASS_SLOT ? c.series[CLASS_SLOT[name]] : c.bands[2];
  };
  const yearTicks = (dates) => (i) => i === 0 || String(dates[i]).slice(0, 4) !== String(dates[i - 1]).slice(0, 4);
  const sliderZoom = (c) => ({
    type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 18, borderColor: c.border, backgroundColor: "transparent",
    fillerColor: c.dark ? "rgba(91,143,240,0.16)" : "rgba(42,111,219,0.10)",
    dataBackground: { lineStyle: { color: c.axis }, areaStyle: { color: c.surface3 } },
    selectedDataBackground: { lineStyle: { color: c.primary }, areaStyle: { color: c.primaryWeak } },
    handleStyle: { color: c.surface, borderColor: c.text3 }, moveHandleStyle: { color: c.axis }, textStyle: { color: c.text3, fontSize: 10 },
  });

  // ------------------------------------------------------------------ 图表
  function equityOption(bt, c) {
    const ax = axisBase(c);
    const eq = bt.equity || [];
    const dates = eq.map((p) => p.date);
    const names = { strategy: "稳健策略", benchmark: "沪深300（一直持有）", cash_fund: "货币基金" };
    const hasCash = eq.some((p) => isNum(p.cash_fund));
    const ddRows = bt.drawdown && bt.drawdown.length === eq.length ? bt.drawdown : null;
    const ddOf = (key) => {
      if (ddRows) return ddRows.map((p) => (isNum(p[key]) ? +(p[key] * 100).toFixed(2) : null));
      let peak = -Infinity;
      return eq.map((p) => { if (!isNum(p[key])) return null; peak = Math.max(peak, p[key]); return +((p[key] / peak - 1) * 100).toFixed(2); });
    };
    const colorOf = { strategy: c.series[0], benchmark: c.series[1], cash_fund: c.text3 };
    const lines = ["strategy", "benchmark"].concat(hasCash ? ["cash_fund"] : []);
    const titleStyle = { fontSize: 12, color: c.text2, fontWeight: 500, fontFamily: c.font };
    return {
      animation: false, textStyle: { fontFamily: c.font },
      title: [{ text: "资产变化（元）", left: 0, top: 26, textStyle: titleStyle }, { text: "回撤：离最高点跌了多少", left: 0, top: 272, textStyle: titleStyle }],
      legend: { top: 0, left: 0, itemWidth: 14, itemHeight: 3, icon: "rect", textStyle: { color: c.text2, fontSize: 12 }, data: lines.map((k) => names[k]) },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true, axisPointer: { type: "line", lineStyle: { color: c.text3 } },
        formatter: (ps) => {
          const i = ps[0] ? ps[0].dataIndex : 0;
          const p = eq[i] || {};
          let h = `<div style="font-weight:700;margin-bottom:4px">${dates[i]}</div>`;
          lines.forEach((k) => {
            if (!isNum(p[k])) return;
            const r = bt.capital ? p[k] / bt.capital - 1 : null;
            h += `<div style="display:flex;gap:14px;justify-content:space-between"><span><span style="color:${colorOf[k]}">●</span> ${names[k]}</span><b>${shortMoney(p[k])}${r != null ? `（${fmt.ratio(r, 1, true)}）` : ""}</b></div>`;
          });
          return h;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left: 60, right: 16, top: 52, height: 206 }, { left: 60, right: 16, top: 296, height: 70 }],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, boundaryGap: false, ...ax, axisLabel: { show: false }, splitLine: { show: false } },
        { type: "category", data: dates, gridIndex: 1, boundaryGap: false, ...ax, splitLine: { show: false }, axisTick: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true, interval: yearTicks(dates), formatter: (v) => String(v).slice(0, 4) } },
      ],
      yAxis: [
        { type: "value", scale: true, gridIndex: 0, splitNumber: 4, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: shortMoney } },
        { type: "value", gridIndex: 1, max: 0, splitNumber: 2, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => Math.round(v) + "%" } },
      ],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }, sliderZoom(c)],
      series: [
        ...lines.map((k) => ({
          name: names[k], type: "line", data: eq.map((p) => (isNum(p[k]) ? p[k] : null)), showSymbol: false, sampling: "lttb", connectNulls: true,
          lineStyle: { width: k === "strategy" ? 2 : 1.5, color: colorOf[k], type: k === "cash_fund" ? "dashed" : "solid" }, itemStyle: { color: colorOf[k] },
          z: k === "strategy" ? 3 : 2,
        })),
        ...["strategy", "benchmark"].map((k) => ({
          name: names[k], type: "line", xAxisIndex: 1, yAxisIndex: 1, data: ddOf(k), showSymbol: false, sampling: "lttb", connectNulls: true,
          lineStyle: { width: 1.5, color: colorOf[k] }, itemStyle: { color: colorOf[k] },
          areaStyle: k === "strategy" ? { color: colorOf[k], opacity: 0.12 } : undefined, z: k === "strategy" ? 3 : 2,
        })),
      ],
    };
  }

  function yearlyOption(rows, c) {
    const ax = axisBase(c);
    const keys = [["strategy", "稳健策略", c.series[0]], ["benchmark", "沪深300", c.series[1]]];
    if (rows.some((r) => isNum(r.cash_fund))) keys.push(["cash_fund", "货币基金", c.bands[2]]);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: c.text2, fontSize: 12 } },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", axisPointer: { type: "shadow" }, confine: true,
        formatter: (ps) => {
          const r = rows[ps[0].dataIndex];
          let h = `<div style="font-weight:700;margin-bottom:4px">${r.year} 年</div>`;
          keys.forEach(([k, n, col]) => {
            const v = r[k];
            h += `<div style="display:flex;gap:14px;justify-content:space-between"><span><span style="color:${col}">●</span> ${n}</span><b style="color:${v > 0 ? c.up : v < 0 ? c.down : c.text}">${fmt.ratio(v, 1, true)}</b></div>`;
          });
          return h;
        },
      },
      grid: { left: 44, right: 8, top: 34, bottom: 24 },
      xAxis: { type: "category", data: rows.map((r) => String(r.year)), ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true } },
      yAxis: { type: "value", ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: keys.map(([k, n, col]) => ({
        name: n, type: "bar", barMaxWidth: 12, barGap: "15%", data: rows.map((r) => (isNum(r[k]) ? +(r[k] * 100).toFixed(1) : null)),
        itemStyle: { color: col, borderRadius: 3 },
      })),
    };
  }

  function classOption(bt, c) {
    const ax = axisBase(c);
    const rows = bt.class_weights || [];
    const classes = (bt.classes && bt.classes.length ? bt.classes : Object.keys(rows[0] || {}).filter((k) => k !== "date"))
      .slice().sort((a, b) => CLASS_ORDER.indexOf(a) - CLASS_ORDER.indexOf(b));
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 8, icon: "roundRect", textStyle: { color: c.text2, fontSize: 12 }, data: classes },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true, axisPointer: { type: "line", lineStyle: { color: c.text3 } },
        formatter: (ps) => {
          const r = rows[ps[0].dataIndex] || {};
          let h = `<div style="font-weight:700;margin-bottom:4px">${r.date} 这一周</div>`;
          classes.slice().reverse().forEach((k) => {
            if (!(r[k] > 0.0005)) return;
            h += `<div style="display:flex;gap:14px;justify-content:space-between"><span><span style="color:${classColor(c, k)}">■</span> ${k}</span><b>${fmt.ratio(r[k], 1)}</b></div>`;
          });
          return h;
        },
      },
      grid: { left: 44, right: 12, top: 56, bottom: 30 },
      xAxis: { type: "category", data: rows.map((r) => r.date), boundaryGap: false, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true, interval: yearTicks(rows.map((r) => r.date)), formatter: (v) => String(v).slice(0, 4) } },
      yAxis: { type: "value", max: 100, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: classes.map((k) => ({
        name: k, type: "line", stack: "w", step: "end", data: rows.map((r) => (isNum(r[k]) ? +(r[k] * 100).toFixed(2) : 0)), showSymbol: false,
        lineStyle: { width: 1, color: c.surface }, itemStyle: { color: classColor(c, k) }, areaStyle: { color: classColor(c, k), opacity: 0.88 },
        emphasis: { focus: "series" },
      })),
    };
  }

  function navOption(nav, c) {
    const ax = axisBase(c);
    const vals = nav.map((r) => r.total);
    const span = Math.max(...vals) - Math.min(...vals);
    const yFmt = span < Math.max(...vals) * 0.02 ? (v) => fmt.int(v) : shortMoney;
    return {
      animation: false, textStyle: { fontFamily: c.font },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true,
        formatter: (ps) => { const r = nav[ps[0].dataIndex]; return `<b>${r.date}</b><br>总资产 <b>${fmt.num(r.total, 0)}</b> 元<br>现金 ${fmt.num(r.cash, 0)} 元${r.note ? "<br>" + r.note : ""}`; },
      },
      grid: { left: 64, right: 16, top: 14, bottom: 26 },
      xAxis: { type: "category", data: nav.map((r) => r.date), boundaryGap: false, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true, alignMinLabel: "left", alignMaxLabel: "right" } },
      yAxis: { type: "value", scale: true, splitNumber: 3, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: yFmt } },
      series: [{ type: "line", data: nav.map((r) => r.total), symbolSize: 8, lineStyle: { width: 2, color: c.series[0] }, itemStyle: { color: c.series[0], borderColor: c.surface, borderWidth: 2 }, areaStyle: { color: c.series[0], opacity: 0.06 } }],
    };
  }

  // ------------------------------------------------------------------ 页面
  QW.page("etf", {
    props: ["params", "query"],
    setup(props) {
      const q = props.query && props.query.tab;
      const tab = ref(TABS.some((t) => t.value === q) ? q : "advice");
      const introOpen = ref(false);

      // 建议
      const adv = ref(null);
      const advLoading = ref(true);
      const advUpdating = ref(false);
      const advErr = ref("");
      const capital = ref(100000);
      const initBusy = ref(false);
      const applyOpen = ref(false);
      const applyBusy = ref(false);
      const dataJob = ref(null);

      const loadAdvice = async (update) => {
        if (update) advUpdating.value = true;
        else if (!adv.value) advLoading.value = true;
        try {
          adv.value = await api.get("/api/etf/advice", { update: update ? "true" : "false" }, { silent: !update });
          advErr.value = "";
          if (update) toast.success(adv.value && adv.value.updated ? "已下载最新行情并重新计算建议" : "建议已刷新");
        } catch (e) {
          advErr.value = e.detail || e.message;
        } finally {
          advLoading.value = false;
          advUpdating.value = false;
        }
      };
      const init = async () => {
        const v = Number(capital.value);
        if (!(v > 0)) { toast.warn("请先填写准备投入的金额"); return; }
        initBusy.value = true;
        try {
          const r = await api.post("/api/etf/init", { capital: v });
          toast.success((r && r.message) || "已设置好资金");
          hold.value = r;
          await loadAdvice(false);
        } catch (e) { /* 已提示 */ } finally { initBusy.value = false; }
      };
      const doApply = async () => {
        applyBusy.value = true;
        try {
          const r = await api.post("/api/etf/apply", {});
          toast.success((r && r.message) || "持仓已更新");
          applyOpen.value = false;
          hold.value = r;
          await loadAdvice(false);
          loadHoldings();
        } catch (e) { /* 已提示 */ } finally { applyBusy.value = false; }
      };
      const startDataJob = async () => {
        try { dataJob.value = await jobs.start("/api/jobs/etf_update", {}, "更新ETF行情"); } catch (e) { /* 已提示 */ }
      };
      const onDataDone = () => { dataJob.value = null; loadAdvice(false); if (tab.value === "backtest") runBt(); };

      const needsInit = computed(() => !!(adv.value && adv.value.needs_init));
      const orders = computed(() => {
        const list = ((adv.value && adv.value.orders) || []).map((o, i) => ({ ...o, _i: i }));
        return list.filter((o) => o.side === "卖出").concat(list.filter((o) => o.side !== "卖出")).map((o, i) => ({ ...o, _n: i + 1 }));
      });
      const sells = computed(() => orders.value.filter((o) => o.side === "卖出"));
      const buys = computed(() => orders.value.filter((o) => o.side !== "卖出"));
      const sumOf = (list) => list.reduce((a, o) => a + (o.amount || 0), 0);
      const positions = computed(() => ((adv.value && adv.value.positions) || []).slice().sort((a, b) => (b.target_weight || 0) - (a.target_weight || 0)));
      const allocation = computed(() => ((adv.value && adv.value.allocation) || []).slice().sort((a, b) => CLASS_ORDER.indexOf(a.asset_class) - CLASS_ORDER.indexOf(b.asset_class)));
      const cashWeight = computed(() => (adv.value && isNum(adv.value.cash_weight) ? adv.value.cash_weight : null));
      const mix = computed(() => {
        const c = colors();
        const list = allocation.value.filter((a) => a.weight > 0.0005).map((a) => ({ name: a.asset_class, w: a.weight, color: classColor(c, a.asset_class) }));
        const used = list.reduce((a, x) => a + x.w, 0);
        const rest = isNum(cashWeight.value) ? cashWeight.value : Math.max(0, 1 - used);
        if (rest > 0.0005) list.push({ name: "货币基金/现金", w: rest, color: c.bands[2] });
        return list;
      });
      const dataInfo = computed(() => (adv.value && adv.value.data) || null);
      const clsColor = (name) => classColor(colors(), name);
      const candTip = (a) => (a.candidates || []).map((x) => `${x.name}（${x.code}）：动量 ${fmt.ratio(x.momentum, 1, true)}，${x.trend ? "趋势向上" : "趋势向下"}`).join("\n");

      // 持仓
      const hold = ref(null);
      const holdLoading = ref(false);
      const holdErr = ref("");
      const editCash = ref(0);
      const editRows = ref([]);
      const newCode = ref("");
      const newVol = ref(null);
      const saveBusy = ref(false);
      const resetEdit = () => {
        const h = hold.value;
        editCash.value = h ? +(+h.cash || 0).toFixed(2) : 0;
        editRows.value = h ? (h.positions || []).map((p) => ({ ...p, volume: p.volume })) : [];
      };
      watch(hold, resetEdit);
      const loadHoldings = async () => {
        holdLoading.value = !hold.value;
        try {
          hold.value = await api.get("/api/etf/holdings", null, { silent: true });
          holdErr.value = "";
        } catch (e) {
          holdErr.value = e.detail || e.message;
        } finally { holdLoading.value = false; }
      };
      const posKey = (cash, rows) => JSON.stringify({ c: +(+cash || 0).toFixed(2), p: rows.filter((r) => +r.volume > 0).map((r) => [r.code, +r.volume]).sort() });
      const holdDirty = computed(() => !!hold.value && posKey(editCash.value, editRows.value) !== posKey(hold.value.cash, hold.value.positions || []));
      const nameBook = computed(() => {
        const book = {};
        ((adv.value && adv.value.allocation) || []).forEach((a) => (a.candidates || []).forEach((x) => { book[x.code] = x.name; }));
        ((adv.value && adv.value.positions) || []).forEach((p) => { book[p.code] = p.name; });
        ((hold.value && hold.value.positions) || []).forEach((p) => { book[p.code] = p.name; });
        return book;
      });
      const addRow = () => {
        const code = String(newCode.value || "").trim();
        const vol = Number(newVol.value);
        if (!/^\d{6}$/.test(code)) { toast.warn("基金代码要填 6 位数字，例如 510300"); return; }
        if (!(vol > 0) || vol !== Math.floor(vol)) { toast.warn("份额要填大于 0 的整数"); return; }
        const ex = editRows.value.find((r) => r.code === code);
        if (ex) ex.volume = +ex.volume + vol;
        else editRows.value.push({ code, name: nameBook.value[code] || "（保存后显示名称）", asset_class: "", volume: vol, price: null, value: null, weight: null, _new: true });
        newCode.value = "";
        newVol.value = null;
      };
      const removeRow = (r) => { editRows.value = editRows.value.filter((x) => x !== r); };
      const saveHoldings = async () => {
        const positionsMap = {};
        for (const r of editRows.value) {
          const v = Number(r.volume);
          if (!(v >= 0) || v !== Math.floor(v)) { toast.warn(`${r.name || r.code} 的份额要填不小于 0 的整数`); return; }
          if (v > 0) positionsMap[r.code] = (positionsMap[r.code] || 0) + v;
        }
        const cash = Number(editCash.value);
        if (!(cash >= 0)) { toast.warn("现金要填不小于 0 的数字"); return; }
        saveBusy.value = true;
        try {
          hold.value = await api.put("/api/etf/holdings", { cash, positions: positionsMap });
          toast.success("持仓已保存");
          loadAdvice(false);
        } catch (e) { /* 已提示 */ } finally { saveBusy.value = false; }
      };
      const navOpt = computed(() => {
        const nav = (hold.value && hold.value.nav) || [];
        return nav.length >= 2 ? navOption(nav, colors()) : null;
      });

      // 回测
      const bt = ref(null);
      const btLoading = ref(false);
      const btErr = ref("");
      const btStart = ref("2014-06-01");
      const btCapital = ref(200000);
      const runBt = async () => {
        btLoading.value = true;
        btErr.value = "";
        try {
          bt.value = await api.get("/api/etf/backtest", { start: btStart.value, capital: btCapital.value }, { silent: true });
        } catch (e) {
          btErr.value = e.detail || e.message;
        } finally { btLoading.value = false; }
      };
      const bench = computed(() => {
        const b = bt.value && bt.value.benchmark_metrics;
        if (!b) return null;
        if (isNum(b.cagr)) return b;
        const name = bt.value.benchmark_name;
        return (name && b[name]) || Object.values(b).find((x) => x && isNum(x.cagr)) || null;
      });
      const btSummary = computed(() => {
        const b = bt.value;
        if (!b || !b.equity || !b.equity.length) return null;
        const last = b.equity[b.equity.length - 1];
        return {
          start: b.start || b.equity[0].date, end: b.end || last.date, capital: b.capital || btCapital.value,
          final: isNum(b.final_value) ? b.final_value : last.strategy, benchFinal: last.benchmark,
        };
      });
      const eqOpt = computed(() => (bt.value && bt.value.equity && bt.value.equity.length ? equityOption(bt.value, colors()) : null));
      const yearRows = computed(() => (bt.value && bt.value.yearly) || []);
      const yearOpt = computed(() => (yearRows.value.length ? yearlyOption(yearRows.value, colors()) : null));
      const classOpt = computed(() => (bt.value && bt.value.class_weights && bt.value.class_weights.length ? classOption(bt.value, colors()) : null));
      const needData = computed(() => /行情数据|更新ETF/.test(btErr.value) || (dataInfo.value && !dataInfo.value.has_data));

      watch(tab, (t) => {
        if ((props.query && props.query.tab) !== t) go("/etf", t === "advice" ? null : { tab: t });
        if (t === "holdings" && !hold.value) loadHoldings();
        if (t === "backtest" && !bt.value && !btLoading.value) runBt();
      });
      watch(() => props.query && props.query.tab, (t) => { if (TABS.some((x) => x.value === t) && t !== tab.value) tab.value = t; });
      onMounted(() => {
        loadAdvice(false);
        loadHoldings();
        if (tab.value === "backtest") runBt();
      });

      return {
        store, fmt, TABS, STEPS, STARTS, HELP, CLASS_NOTES, tab, introOpen,
        adv, advLoading, advUpdating, advErr, loadAdvice, capital, initBusy, init, applyOpen, applyBusy, doApply, dataJob, startDataJob, onDataDone,
        needsInit, orders, sells, buys, sumOf, positions, allocation, mix, dataInfo, candTip, clsColor,
        hold, holdLoading, holdErr, loadHoldings, editCash, editRows, newCode, newVol, saveBusy, holdDirty, addRow, removeRow, saveHoldings, resetEdit, navOpt,
        bt, btLoading, btErr, btStart, btCapital, runBt, bench, btSummary, eqOpt, yearOpt, classOpt, needData, shortMoney, IN_SAMPLE_NOTE,
      };
    },
    template: `<div class="stack etf-page">
      <div class="etf-intro qw-banner info">
        <qw-icon name="shield" :size="22"/>
        <div class="qw-banner-body">
          <b>稳健ETF</b>：不炒个股，把钱分散买入几类指数基金（ETF），每周看一次趋势、小幅调整。目标是长期稳稳地赚，回撤比股票小得多。
          <button class="linkbtn" @click="introOpen = !introOpen">{{ introOpen ? '收起' : '它是怎么做的？' }}<qw-icon :name="introOpen ? 'chevronDown' : 'chevronRight'" :size="13"/></button>
          <ol v-if="introOpen" class="etf-steps"><li v-for="(s, i) in STEPS" :key="i"><b>{{ s[0] }}</b>{{ s[1] }}</li></ol>
        </div>
      </div>

      <qw-tabs v-model="tab" :items="TABS"/>

      <!-- ============ 本周建议 ============ -->
      <template v-if="tab === 'advice'">
        <template v-if="advLoading"><qw-skeleton type="tiles" :count="4"/><qw-card><qw-skeleton :rows="6"/></qw-card></template>
        <qw-card v-else-if="advErr && !adv">
          <qw-empty icon="alert" title="本周建议暂时算不出来" :desc="advErr" action-text="再试一次" @action="loadAdvice(false)">
            <button class="btn" :disabled="!!dataJob" @click="startDataJob"><qw-icon name="download" :size="15"/>更新ETF行情（约1分钟）</button>
          </qw-empty>
          <div v-if="dataJob" style="padding:0 8px 8px"><qw-job :job-id="dataJob" title="更新ETF行情" @done="onDataDone"/></div>
        </qw-card>

        <template v-else-if="needsInit">
          <qw-card>
            <div class="etf-onboard">
              <div class="eo-ico"><qw-icon name="wallet" :size="28"/></div>
              <div class="eo-body">
                <h2>先告诉我你准备投入多少钱</h2>
                <p>{{ adv.message || '填写准备投入的金额，就能算出每只 ETF 该买多少份。' }}</p>
                <div class="eo-form">
                  <label class="money-in"><input type="number" min="1000" step="10000" v-model.number="capital" aria-label="投入资金" @keydown.enter="init"><em>元</em></label>
                  <button class="btn primary lg" :disabled="initBusy" @click="init">{{ initBusy ? '设置中…' : '开始使用' }}</button>
                </div>
                <div class="chips-wrap"><button v-for="v in [50000, 100000, 200000, 500000]" :key="v" class="chip" :class="{active: capital === v}" @click="capital = v">{{ shortMoney(v) }}</button></div>
                <p class="muted" style="font-size:12.5px;margin-top:10px">只是记录在本机，不会连接券商、不会真的下单。建议先用模拟金额跑一两个月，熟悉流程后再用真钱。</p>
              </div>
            </div>
          </qw-card>
          <qw-card v-if="mix.length" title="如果现在开始，本周的目标配置" icon="grid" :sub="adv.signal_date ? '依据 ' + adv.signal_date + ' 收盘数据' : ''">
            <div class="mix-bar"><i v-for="m in mix" :key="m.name" :style="{width: m.w * 100 + '%', background: m.color}" v-tip="m.name + '：' + $fmt.ratio(m.w, 1)"></i></div>
            <div class="mix-legend"><span v-for="m in mix" :key="m.name"><i :style="{background: m.color}"></i>{{ m.name }} <b class="num">{{ $fmt.ratio(m.w, 1) }}</b></span></div>
            <ul class="alloc-list section-gap">
              <li v-for="a in allocation" :key="a.asset_class"><span class="al-cls">{{ a.asset_class }}</span><span class="al-pick">{{ a.code ? a.name + '（' + a.code + '）' : '不持有' }}</span><span class="al-reason muted">{{ a.reason }}</span></li>
            </ul>
          </qw-card>
        </template>

        <template v-else-if="adv">
          <div v-if="dataInfo && !dataInfo.fresh" class="qw-banner warn"><qw-icon name="alert" :size="18"/><div class="qw-banner-body">ETF 行情不是最新的（本地最新到 {{ dataInfo.last_complete_day || '—' }}）。点右边的“刷新建议”会先下载最新行情，大约需要 1 分钟。</div></div>
          <div class="etf-head">
            <div class="muted">信号依据 <b class="num">{{ adv.signal_date }}</b> 收盘数据 · 参考价日期 <b class="num">{{ adv.price_date }}</b></div>
            <button class="btn" :disabled="advUpdating" @click="loadAdvice(true)"><qw-icon name="refresh" :size="15" :class="{spin: advUpdating}"/>{{ advUpdating ? '正在下载最新行情…' : '刷新建议' }}</button>
          </div>
          <div class="etf-tiles">
            <qw-stat label="当前总资产" :value="$fmt.money(adv.total_value)" flat/>
            <qw-stat label="当前现金" :value="$fmt.money(adv.cash)" flat/>
            <qw-stat label="调仓后现金" :value="$fmt.money(adv.cash_after)" :help="HELP.cashAfter" flat/>
            <qw-stat label="组合波动" :value="$fmt.ratio(adv.portfolio_vol, 1)" :help="HELP.vol" :sub="adv.vol_scale < 0.999 ? '已整体降仓到 ' + $fmt.ratio(adv.vol_scale, 0) : '在 10% 以内'" flat/>
          </div>

          <qw-card title="本周要做的操作" icon="clock" :help="HELP.order" :pad="false"
            :sub="orders.length ? '先卖 ' + sells.length + ' 笔，再买 ' + buys.length + ' 笔' : ''">
            <template #extra>
              <span v-if="adv.applied" class="qw-tag green"><qw-icon name="check" :size="12"/>&nbsp;已按建议更新持仓</span>
            </template>
            <div v-if="orders.length && store.isPhone" class="order-cards">
              <div v-for="o in orders" :key="o._i" class="order-card">
                <span class="order-n num">{{ o._n }}</span>
                <span class="side-tag" :class="o.side === '卖出' ? 'sell' : 'buy'">{{ o.side === '卖出' ? '▼ 卖出' : '▲ 买入' }}</span>
                <span class="etf-name"><b>{{ o.name }}</b><small>{{ o.code }} · 参考价 {{ $fmt.price(o.price, 3) }}</small></span>
                <span class="oc-right"><b class="num">{{ $fmt.int(o.volume) }} 份</b><small class="muted num">{{ $fmt.num(o.amount, 0) }} 元</small></span>
              </div>
            </div>
            <qw-table v-else-if="orders.length" :columns="[{key:'_n',label:'顺序',width:'54px'},{key:'side',label:'操作'},{key:'name',label:'基金'},{key:'volume',label:'数量',align:'right'},{key:'price',label:'参考价',align:'right'},{key:'amount',label:'金额',align:'right'},{key:'commission',label:'手续费',align:'right'}]" :rows="orders" row-key="_i">
              <template #cell-_n="{row}"><span class="order-n num">{{ row._n }}</span></template>
              <template #cell-side="{row}"><span class="side-tag" :class="row.side === '卖出' ? 'sell' : 'buy'">{{ row.side === '卖出' ? '▼ 卖出' : '▲ 买入' }}</span></template>
              <template #cell-name="{row}"><span class="etf-name"><b>{{ row.name }}</b><small>{{ row.code }}</small></span></template>
              <template #cell-volume="{row}"><span class="nowrap"><b class="num">{{ $fmt.int(row.volume) }}</b> <small class="muted">份</small></span></template>
              <template #cell-price="{row}"><span class="num">{{ $fmt.price(row.price, 3) }}</span></template>
              <template #cell-amount="{row}"><span class="num">{{ $fmt.num(row.amount, 0) }}</span></template>
              <template #cell-commission="{row}"><span class="num muted">{{ $fmt.num(row.commission, 2) }}</span></template>
            </qw-table>
            <qw-empty v-else compact icon="checkCircle" title="本周不需要调仓" desc="目前的持仓已经和目标配置很接近（偏差不到 3%），继续持有就好，下周再来看。"/>
            <template #footer v-if="orders.length">
              <div class="row between" style="width:100%">
                <span>卖出合计 <b class="num">{{ $fmt.num(sumOf(sells), 0) }}</b> 元 · 买入合计 <b class="num">{{ $fmt.num(sumOf(buys), 0) }}</b> 元</span>
                <button class="btn primary" :disabled="adv.applied" @click="applyOpen = true"><qw-icon name="check" :size="15"/>{{ adv.applied ? '已更新过持仓' : '我已按建议下单' }}</button>
              </div>
            </template>
          </qw-card>

          <div class="grid etf-row2">
            <qw-card title="目标持仓" icon="target" :help="HELP.target" :pad="false">
              <qw-table :columns="[{key:'name',label:'基金'},{key:'target_weight',label:'目标占比',minWidth:'130px',sortable:true},{key:'vol',label:'份额（现在 → 目标）',align:'right'},{key:'target_value',label:'目标市值',align:'right',sortable:true}]" :rows="positions" row-key="code" dense>
                <template #cell-name="{row}"><span class="etf-name"><b>{{ row.name }}</b><small>{{ row.code }} · {{ row.asset_class }}</small></span></template>
                <template #cell-target_weight="{row}"><div class="wbar"><span class="track"><i :style="{width: Math.min(100, (row.target_weight || 0) / 0.3 * 100) + '%', background: clsColor(row.asset_class)}"></i></span><b class="num">{{ $fmt.ratio(row.target_weight, 1) }}</b></div></template>
                <template #cell-vol="{row}"><span class="num nowrap">{{ $fmt.int(row.current_volume) }} → <b :class="row.target_volume > row.current_volume ? 'up' : row.target_volume < row.current_volume ? 'down' : ''">{{ $fmt.int(row.target_volume) }}</b></span></template>
                <template #cell-target_value="{row}"><span class="num">{{ $fmt.num(row.target_value, 0) }}</span></template>
              </qw-table>
            </qw-card>
            <qw-card title="各类资产本周怎么安排" icon="grid" :help="HELP.reason">
              <div class="mix-bar"><i v-for="m in mix" :key="m.name" :style="{width: m.w * 100 + '%', background: m.color}" v-tip="m.name + '：' + $fmt.ratio(m.w, 1)"></i></div>
              <div class="mix-legend"><span v-for="m in mix" :key="m.name"><i :style="{background: m.color}"></i>{{ m.name }} <b class="num">{{ $fmt.ratio(m.w, 1) }}</b></span></div>
              <ul class="alloc-list section-gap">
                <li v-for="a in allocation" :key="a.asset_class">
                  <span class="al-cls" v-tip="CLASS_NOTES[a.asset_class] || ''">{{ a.asset_class }}</span>
                  <span class="al-pick"><template v-if="a.code">{{ a.name }} <b class="num">{{ $fmt.ratio(a.weight, 1) }}</b></template><span v-else class="muted">不持有</span></span>
                  <span class="al-reason text-2">{{ a.reason }}<qw-help v-if="a.candidates && a.candidates.length" :text="candTip(a)"/></span>
                </li>
              </ul>
            </qw-card>
          </div>

          <qw-card v-if="(adv.warnings && adv.warnings.length) || (adv.ignored && Object.keys(adv.ignored).length)" title="注意" icon="alert">
            <ul class="warn-list">
              <li v-for="(w, i) in adv.warnings" :key="i"><qw-icon name="alert" :size="14"/><span>{{ w }}</span></li>
              <li v-if="adv.ignored && Object.keys(adv.ignored).length" class="note"><qw-icon name="info" :size="14"/><span>这些持仓不在策略范围内，建议里没有考虑它们：{{ Object.keys(adv.ignored).map((k) => k + '（' + adv.ignored[k] + '份）').join('、') }}</span></li>
            </ul>
          </qw-card>
        </template>

        <qw-modal v-model="applyOpen" title="确认已经按建议下单了？" width="500px">
          <p class="text-2">确认后，会把“我的持仓”更新成按建议买卖之后的样子（数量按建议、价格按参考价估算）。</p>
          <ul class="apply-sum">
            <li v-for="o in orders" :key="o._i"><span class="side-tag" :class="o.side === '卖出' ? 'sell' : 'buy'">{{ o.side }}</span>{{ o.name }} <b class="num">{{ $fmt.int(o.volume) }}</b> 份</li>
          </ul>
          <p class="muted" style="font-size:12.5px">如果实际成交的数量不一样，之后可以在“我的持仓”里手动修改。</p>
          <template #footer><button class="btn ghost" @click="applyOpen = false">还没下单</button><button class="btn primary" :disabled="applyBusy" @click="doApply">{{ applyBusy ? '更新中…' : '确认，更新持仓' }}</button></template>
        </qw-modal>
      </template>

      <!-- ============ 我的持仓 ============ -->
      <template v-if="tab === 'holdings'">
        <template v-if="holdLoading"><qw-skeleton type="tiles" :count="4"/><qw-card><qw-skeleton :rows="5"/></qw-card></template>
        <qw-card v-else-if="holdErr && !hold"><qw-empty icon="alert" title="持仓暂时读取不到" :desc="holdErr" action-text="重新加载" @action="loadHoldings"/></qw-card>
        <qw-card v-else-if="hold && hold.needs_init">
          <qw-empty icon="wallet" title="还没有设置持仓" desc="先在“本周建议”里填写准备投入的资金，系统会帮你算出每只 ETF 该买多少。" action-text="去设置资金" @action="tab = 'advice'"/>
        </qw-card>
        <template v-else-if="hold">
          <div class="etf-tiles">
            <qw-stat label="总资产" :value="$fmt.money(hold.total_value)" flat :sub="hold.price_date ? '按 ' + hold.price_date + ' 价格' : ''"/>
            <qw-stat label="持仓市值" :value="$fmt.money(hold.market_value)" flat/>
            <qw-stat label="现金" :value="$fmt.money(hold.cash)" flat/>
            <qw-stat label="累计收益" :value="$fmt.ratio(hold.total_return, 2, true)" :tone="$fmt.dir(hold.total_return)" :sub="hold.start_value ? '起始 ' + $fmt.money(hold.start_value) : ''" flat/>
          </div>
          <qw-card title="持仓明细" icon="wallet" sub="券商实际成交和建议不一样时，在这里改成真实数量" :pad="false">
            <template #extra><span v-if="holdDirty" class="qw-tag warn">有未保存的修改</span></template>
            <div class="hs-box"><div class="hold-edit" v-hscroll>
              <table class="qw-table dense">
                <thead><tr><th>基金</th><th class="al-right">份额</th><th class="al-right">价格</th><th class="al-right">市值</th><th>占比</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="r in editRows" :key="r.code">
                    <td><span class="etf-name"><b>{{ r.name }}</b><small>{{ r.code }}<template v-if="r.asset_class"> · {{ r.asset_class }}</template><template v-if="r.in_strategy === false"> · 非策略品种</template></small></span></td>
                    <td class="al-right"><input class="qw-input vol-in num" type="number" min="0" step="100" v-model.number="r.volume" :aria-label="r.name + '份额'"></td>
                    <td class="al-right num">{{ $fmt.price(r.price, 3) }}</td>
                    <td class="al-right num">{{ r.price ? $fmt.num(r.volume * r.price, 0) : '—' }}</td>
                    <td><div class="wbar" v-if="r.weight != null"><span class="track"><i :style="{width: Math.min(100, r.weight / 0.3 * 100) + '%', background: clsColor(r.asset_class)}"></i></span><span class="num">{{ $fmt.ratio(r.weight, 1) }}</span></div><span v-else class="muted">—</span></td>
                    <td class="al-right"><button class="qw-iconbtn" style="width:28px;height:28px" @click="removeRow(r)" v-tip="'删除这一行'" aria-label="删除"><qw-icon name="x" :size="14"/></button></td>
                  </tr>
                  <tr class="add-row">
                    <td><input class="qw-input code-in" v-model="newCode" maxlength="6" inputmode="numeric" :placeholder="store.isPhone ? '代码 如510300' : '基金代码，如 510300'" aria-label="新基金代码" @keydown.enter="addRow"></td>
                    <td class="al-right"><input class="qw-input vol-in" type="number" min="0" step="100" v-model.number="newVol" placeholder="份额" aria-label="新基金份额" @keydown.enter="addRow"></td>
                    <td colspan="4"><button class="btn sm" @click="addRow"><qw-icon name="plus" :size="14"/>添加一行</button></td>
                  </tr>
                </tbody>
              </table>
              <p v-if="!editRows.length" class="muted" style="padding:10px 16px">目前没有持仓，只有现金。</p>
            </div></div>
            <template #footer>
              <div class="row between" style="width:100%">
                <label class="cash-in">现金 <input class="qw-input num" type="number" min="0" step="100" v-model.number="editCash" aria-label="现金"> 元</label>
                <span class="row"><button v-if="holdDirty" class="btn ghost" @click="resetEdit">撤销修改</button><button class="btn primary" :disabled="!holdDirty || saveBusy" @click="saveHoldings"><qw-icon name="check" :size="15"/>{{ saveBusy ? '保存中…' : '保存持仓' }}</button></span>
              </div>
            </template>
          </qw-card>
          <qw-card v-if="navOpt" title="资产记录" icon="trend" sub="每次按建议调仓后记一笔"><qw-chart :option="navOpt" height="220px"/></qw-card>
          <p class="muted" style="font-size:12.5px">修改持仓只是更新本机的记录，不会真的买卖。</p>
        </template>
      </template>

      <!-- ============ 历史回测 ============ -->
      <template v-if="tab === 'backtest'">
        <div class="bt-ctrl">
          <label>从<select v-model="btStart" class="qw-input" aria-label="回测开始时间"><option v-for="s in STARTS" :key="s.value" :value="s.value">{{ s.label }}</option></select></label>
          <label>投入<input class="qw-input num" type="number" min="10000" step="10000" v-model.number="btCapital" aria-label="回测资金" style="width:120px">元</label>
          <button class="btn primary" :disabled="btLoading" @click="runBt"><qw-icon name="refresh" :size="15" :class="{spin: btLoading}"/>{{ btLoading ? '计算中…' : '开始回测' }}</button>
          <span class="muted" style="font-size:12.5px">用过去的真实价格，按每周调仓的规则模拟，已含手续费（样本内，成绩偏乐观，见下方说明）。</span>
        </div>
        <template v-if="btLoading && !bt"><qw-skeleton type="tiles" :count="4"/><qw-card><qw-skeleton height="360px"/><p class="muted" style="margin-top:10px">第一次计算大约需要 5~10 秒…</p></qw-card></template>
        <qw-card v-else-if="btErr && !bt">
          <qw-empty icon="alert" title="回测没能完成" :desc="btErr">
            <button v-if="needData" class="btn primary" :disabled="!!dataJob" @click="startDataJob"><qw-icon name="download" :size="15"/>更新ETF行情（约1分钟）</button>
            <button class="btn" @click="runBt">再试一次</button>
          </qw-empty>
          <div v-if="dataJob" style="padding:0 8px 8px"><qw-job :job-id="dataJob" title="更新ETF行情" @done="onDataDone"/></div>
        </qw-card>
        <template v-else-if="bt">
          <div class="qw-banner warn bt-insample"><qw-icon name="alert" :size="20"/><div class="qw-banner-body">
            <b>先看这条：</b>{{ bt.in_sample_note || IN_SAMPLE_NOTE }}
          </div></div>
          <div v-if="btSummary" class="qw-banner info bt-say"><qw-icon name="trend" :size="20"/><div class="qw-banner-body">
            按这套规则回放历史：如果 {{ btSummary.start.slice(0, 4) }}年{{ $fmt.cnDate(btSummary.start) }}投入 <b>{{ shortMoney(btSummary.capital) }}</b> 元，到 {{ btSummary.end }} 账面上是 <b class="big">{{ shortMoney(btSummary.final) }}</b> 元；
            同期一直拿着沪深300是 <b>{{ shortMoney(btSummary.benchFinal) }}</b> 元。<span class="muted">这是用选参数时用过的同一段历史算的（样本内），以后实际通常会更差；过去的表现不代表未来。</span></div></div>
          <div class="etf-tiles six">
            <qw-stat label="年化收益" :value="$fmt.ratio(bt.metrics.cagr, 1, true)" :tone="$fmt.dir(bt.metrics.cagr)" :help="HELP.cagr" :sub="bench ? '沪深300 ' + $fmt.ratio(bench.cagr, 1, true) : ''" flat/>
            <qw-stat label="最大回撤" :value="$fmt.ratio(bt.metrics.max_drawdown, 1, true)" :help="HELP.dd" :sub="bench ? '沪深300 ' + $fmt.ratio(bench.max_drawdown, 1, true) : ''" flat/>
            <qw-stat label="夏普比率" :value="$fmt.num(bt.metrics.sharpe, 2)" :help="HELP.sharpe" :sub="bench ? '沪深300 ' + $fmt.num(bench.sharpe, 2) : ''" flat/>
            <qw-stat label="总收益" :value="$fmt.ratio(bt.metrics.total_return, 0, true)" :tone="$fmt.dir(bt.metrics.total_return)" :sub="bench ? '沪深300 ' + $fmt.ratio(bench.total_return, 0, true) : ''" flat/>
            <qw-stat label="年化波动" :value="$fmt.ratio(bt.metrics.vol, 1)" :help="HELP.vol2" :sub="bench ? '沪深300 ' + $fmt.ratio(bench.vol, 1) : ''" flat/>
            <qw-stat label="赚钱的年份" :value="$fmt.ratio(bt.metrics.positive_years, 0)" :help="HELP.posYears" :sub="isFinite(bt.metrics.underwater_days) ? '最长 ' + Math.round(bt.metrics.underwater_days) + ' 天回本' : ''" flat/>
          </div>
          <qw-card title="策略 vs 一直拿着沪深300" icon="trend"><qw-chart v-if="eqOpt" :option="eqOpt" height="420px"/></qw-card>
          <div class="grid etf-row2">
            <qw-card title="每年收益" icon="bars"><qw-chart v-if="yearOpt" :option="yearOpt" height="280px"/><p v-else class="muted">没有年度数据。</p></qw-card>
            <qw-card title="各类资产仓位变化" icon="grid" sub="每周的目标配置"><qw-chart v-if="classOpt" :option="classOpt" height="280px"/><p v-else class="muted">没有仓位数据。</p></qw-card>
          </div>
          <p class="muted" style="font-size:12.5px">回测区间 {{ bt.start }} ~ {{ bt.end }}<template v-if="bt.rebalance_count"> · 调仓 {{ bt.rebalance_count }} 次</template><template v-if="bt.trade_count"> · 成交 {{ bt.trade_count }} 笔</template><template v-if="bt.total_commission"> · 手续费合计 {{ $fmt.num(bt.total_commission, 0) }} 元</template>。</p>
        </template>
      </template>
    </div>`,
  });
})();
