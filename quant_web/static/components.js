/*
 * 第三版共享组件（经 QW.extraComponents 注册）
 *  <qw-kline-pro :bars :indicators :main="['ma']" :subs="['vol','macd']" :chips :show-cost :limit-days :markers :period :initial-bars>
 *      多指标 K 线：主图（蜡烛 + 叠加指标 + 涨停/信号标记 + 可选平均成本线）+ 任意个副图，共用缩放和十字光标。
 *      indicators 为 /api/stock/{code}/chart 返回的 indicators；markers: [{date, text, color, pos:'above'|'below'}]
 *  <qw-chips :chips :price height>   筹码分布（横向柱：获利盘红、套牢盘蓝；平均成本/现价参考线）
 *  <qw-ind-picker v-model:main v-model:subs :catalog>   指标选择（主图多选、副图多选，最多 4 个副图）
 */
(function () {
  "use strict";
  const { computed } = Vue;
  const QW = (window.QW = window.QW || {});
  const reg = (QW.extraComponents = QW.extraComponents || []);

  const isNum = (v) => typeof v === "number" && isFinite(v);
  const row = (k, v, color) => `<div style="display:flex;justify-content:space-between;gap:16px;line-height:1.6"><span style="opacity:.7">${k}</span><b style="font-weight:600${color ? ";color:" + color : ""}">${v}</b></div>`;
  const cornerPos = (pt, params, dom, rect, size) => {
    const w = size.contentSize[0];
    const vw = size.viewSize[0];
    return [pt[0] < vw / 2 ? vw - w - 8 : 8, 6];
  };
  const numText = (v, big) => {
    if (!isNum(v)) return "—";
    const a = Math.abs(v);
    if (big || a >= 1e5) return QW.fmt.volShort ? QW.fmt.volShort(v) : v.toFixed(0);
    return a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  };
  // 副图每条线的颜色：沿用主题的系列色（K/D/J、DIF/DEA 等按顺序取色）
  const lineColor = (c, i) => c.series[(i + 3) % c.series.length];

  const KlinePro = {
    name: "QwKlinePro",
    props: {
      bars: { type: Array, default: () => [] },
      indicators: { type: Object, default: () => ({}) },
      main: { type: Array, default: () => ["ma"] },
      subs: { type: Array, default: () => ["vol", "macd"] },
      chips: { type: Object, default: null },
      showCost: Boolean,
      limitDays: { type: Array, default: () => [] },
      markers: { type: Array, default: () => [] },
      period: { type: String, default: "day" },
      initialBars: { type: Number, default: 120 },
      mainHeight: { type: Number, default: 300 },
      subHeight: { type: Number, default: 96 },
    },
    emits: ["hover"],
    setup(props) {
      const subIds = computed(() => props.subs.filter((id) => props.indicators[id] && !props.indicators[id].error));
      const height = computed(() => props.mainHeight + subIds.value.length * (props.subHeight + 22) + 26 + 52);
      const option = computed(() => {
        const c = QW.colors();
        const bars = props.bars || [];
        const n = bars.length;
        const dates = bars.map((b) => String(b.date).slice(0, 10));
        const ax = QW.axisBase(c);
        const lset = new Set(props.period === "day" ? props.limitDays || [] : []);
        const left = 62;
        const right = 14;
        const top0 = 26;
        const subs = subIds.value;
        const grids = [{ left, right, top: top0, height: props.mainHeight }];
        let y = top0 + props.mainHeight + 22;
        subs.forEach(() => { grids.push({ left, right, top: y, height: props.subHeight }); y += props.subHeight + 22; });
        const gi = grids.map((_, i) => i);
        const start = n > props.initialBars ? (1 - props.initialBars / n) * 100 : 0;
        const dateLabel = (v) => (props.period === "month" ? String(v).slice(0, 7) : String(v).slice(2, 10));
        const xAxis = grids.map((_, i) => ({
          type: "category", data: dates, gridIndex: i, boundaryGap: true, ...ax, splitLine: { show: false },
          axisLabel: i === grids.length - 1 ? { ...ax.axisLabel, formatter: dateLabel, hideOverlap: true } : { show: false },
          axisTick: { show: i === grids.length - 1 }, axisPointer: { label: { show: i === grids.length - 1 } },
        }));
        const yAxis = grids.map((_, i) => ({
          scale: true, gridIndex: i, splitNumber: i === 0 ? 4 : 2, ...ax, axisLine: { show: false },
          splitLine: i === 0 ? ax.splitLine : { show: false },
          axisLabel: { ...ax.axisLabel, showMaxLabel: i === 0, formatter: (v) => numText(v, subs[i - 1] === "vol" || subs[i - 1] === "obv") },
        }));
        const series = [{
          name: "K线", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0, barMaxWidth: 14,
          data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          itemStyle: { color: c.up, color0: c.down, borderColor: c.up, borderColor0: c.down },
        }];
        const legend = [];
        // 主图叠加
        props.main.forEach((id) => {
          const ind = props.indicators[id];
          if (!ind || ind.error) return;
          ind.lines.forEach((ln, k) => {
            const color = c.series[(k + (id === "ma" ? 3 : 0)) % c.series.length];
            const name = `${ln.label}`;
            legend.push(name);
            if (ln.style === "dot") {
              series.push({ name, type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: ln.data, symbolSize: 3, itemStyle: { color: c.warn }, emphasis: { disabled: true } });
            } else {
              series.push({ name, type: "line", xAxisIndex: 0, yAxisIndex: 0, data: ln.data, showSymbol: false, z: 3,
                lineStyle: { width: 1.3, color, type: ln.style === "dash" ? "dashed" : "solid" }, itemStyle: { color }, emphasis: { disabled: true } });
            }
          });
        });
        if (props.showCost && props.chips && props.chips.series && props.chips.series.avg_cost) {
          legend.push("平均成本");
          series.push({ name: "平均成本", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: props.chips.series.avg_cost, showSymbol: false, z: 3,
            lineStyle: { width: 1.2, color: c.gold, type: "dashed" }, itemStyle: { color: c.gold }, emphasis: { disabled: true } });
        }
        // 涨停与信号标记
        if (lset.size) {
          series.push({ name: "涨停", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: bars.filter((b) => lset.has(String(b.date).slice(0, 10))).map((b) => [String(b.date).slice(0, 10), b.high]),
            symbol: "triangle", symbolRotate: 180, symbolSize: 7, symbolOffset: [0, -9], itemStyle: { color: c.up }, z: 5, tooltip: { show: false }, emphasis: { disabled: true } });
        }
        if (props.markers && props.markers.length) {
          const idx = new Map(dates.map((d, i) => [d, i]));
          const pts = props.markers.filter((m) => idx.has(String(m.date).slice(0, 10))).map((m) => {
            const b = bars[idx.get(String(m.date).slice(0, 10))];
            const above = m.pos !== "below";
            return {
              value: [String(m.date).slice(0, 10), above ? b.high : b.low],
              symbol: "pin", symbolSize: 26, symbolRotate: above ? 0 : 180, symbolOffset: [0, above ? -14 : 14],
              itemStyle: { color: m.color || c.primary },
              label: { show: true, formatter: m.text || "", color: "#fff", fontSize: 10, offset: [0, above ? -1 : 3] },
            };
          });
          series.push({ name: "信号", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: pts, z: 6, tooltip: { show: false }, emphasis: { disabled: true } });
        }
        // 副图
        const titles = [];
        subs.forEach((id, k) => {
          const ind = props.indicators[id];
          const g = k + 1;
          const pv = ind.params ? Object.values(ind.params).join(",") : "";
          titles.push({ text: ind.name + (pv ? `(${pv})` : ""), left: left + 4, top: grids[g].top - 18, textStyle: { fontSize: 11, color: c.text3, fontWeight: 500 } });
          ind.lines.forEach((ln, j) => {
            const color = lineColor(c, j);
            const base = { name: `${id}:${ln.label}`, xAxisIndex: g, yAxisIndex: g, emphasis: { disabled: true } };
            if (ln.style === "vol") {
              series.push({ ...base, type: "bar", barMaxWidth: 14, data: ln.data.map((v, i) => {
                const b = bars[i];
                const prev = i > 0 ? bars[i - 1].close : b.open;
                const up = b.close > b.open || (b.close === b.open && b.close >= prev);
                return { value: v, itemStyle: { color: up ? c.up : c.down, opacity: 0.7 } };
              }) });
            } else if (ln.style === "macd") {
              series.push({ ...base, type: "bar", barMaxWidth: 6, data: ln.data.map((v) => ({ value: v, itemStyle: { color: isNum(v) && v >= 0 ? c.up : c.down } })) });
            } else if (ln.style === "bar") {
              series.push({ ...base, type: "bar", barMaxWidth: 10, data: ln.data, itemStyle: { color: c.axis } });
            } else {
              series.push({ ...base, type: "line", data: ln.data, showSymbol: false, lineStyle: { width: 1.2, color, type: ln.style === "dash" ? "dashed" : "solid" }, itemStyle: { color } });
            }
          });
          if (ind.refs && ind.refs.length) {
            series.push({ name: `${id}:ref`, type: "line", xAxisIndex: g, yAxisIndex: g, data: [], silent: true,
              markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: c.text3, type: "dashed", width: 1, opacity: 0.6 },
                data: ind.refs.map((r) => ({ yAxis: r })) } });
          }
        });
        const tooltip = {
          ...QW.tooltipBase(c), trigger: "axis", confine: true, position: cornerPos,
          axisPointer: { type: "cross", label: { backgroundColor: c.text2, fontSize: 11 }, crossStyle: { color: c.text3 } },
          formatter: (ps) => {
            if (!ps || !ps.length) return "";
            const i = ps[0].dataIndex;
            const b = bars[i];
            if (!b) return "";
            const pc = i > 0 ? bars[i - 1].close : null;
            const chg = pc ? b.close / pc - 1 : null;
            const col = (v) => (pc == null ? null : v > pc ? c.up : v < pc ? c.down : null);
            let html = `<div style="font-weight:700;margin-bottom:3px">${dates[i]}${lset.has(dates[i]) ? ` <span style="color:${c.up}">涨停</span>` : ""}</div>`;
            html += row("开 / 收", `<span style="color:${col(b.open) || "inherit"}">${b.open.toFixed(2)}</span> / <span style="color:${col(b.close) || "inherit"}">${b.close.toFixed(2)}</span>`);
            html += row("高 / 低", `${b.high.toFixed(2)} / ${b.low.toFixed(2)}`);
            if (chg != null) html += row("涨跌幅", QW.fmt.ratio(chg, 2, true), chg > 0 ? c.up : chg < 0 ? c.down : null);
            if (isNum(b.turnover)) html += row("换手率", b.turnover.toFixed(2) + "%");
            const parts = [];
            [...props.main, ...subs].forEach((id) => {
              const ind = props.indicators[id];
              if (!ind || ind.error) return;
              const vals = ind.lines.filter((ln) => ln.style !== "vol").map((ln) => `${ln.label} ${numText(ln.data[i], id === "obv")}`);
              if (vals.length) parts.push(`<div style="opacity:.85;line-height:1.6"><span style="opacity:.7">${ind.name}</span> ${vals.join("　")}</div>`);
            });
            if (props.chips && props.chips.series && isNum(props.chips.series.winner && props.chips.series.winner[i])) {
              parts.push(`<div style="opacity:.85;line-height:1.6"><span style="opacity:.7">筹码</span> 获利 ${(props.chips.series.winner[i] * 100).toFixed(0)}%　平均成本 ${numText(props.chips.series.avg_cost[i])}</div>`);
            }
            return html + (parts.length ? `<div style="margin-top:4px;padding-top:4px;border-top:1px solid ${c.border};max-width:340px;white-space:normal">${parts.join("")}</div>` : "");
          },
        };
        return {
          animation: false, textStyle: { fontFamily: c.font }, title: titles,
          legend: { show: legend.length > 0, top: 2, left: left - 6, itemWidth: 14, itemHeight: 3, icon: "rect", data: legend,
            textStyle: { color: c.text2, fontSize: 12 }, inactiveColor: c.text3 },
          tooltip, axisPointer: { link: [{ xAxisIndex: "all" }] },
          grid: grids, xAxis, yAxis, series,
          dataZoom: [
            { type: "inside", xAxisIndex: gi, start, end: 100, minValueSpan: 15 },
            { type: "slider", xAxisIndex: gi, start, end: 100, bottom: 6, height: 20, borderColor: c.border, backgroundColor: "transparent",
              fillerColor: c.dark ? "rgba(91,143,240,0.16)" : "rgba(42,111,219,0.10)", dataBackground: { lineStyle: { color: c.axis }, areaStyle: { color: c.surface3 } },
              selectedDataBackground: { lineStyle: { color: c.primary }, areaStyle: { color: c.primaryWeak } }, handleStyle: { color: c.surface, borderColor: c.text3 },
              moveHandleStyle: { color: c.axis }, textStyle: { color: c.text3, fontSize: 10 }, labelFormatter: (v, s) => String(s).slice(0, 10) },
          ],
        };
      });
      const heightPx = computed(() => height.value + "px");
      return { option, heightPx };
    },
    template: `<qw-chart :option="option" :height="heightPx"/>`,
  };

  const Chips = {
    name: "QwChips",
    props: { chips: { type: Object, default: null }, price: { type: Number, default: null }, height: { type: String, default: "360px" } },
    setup(props) {
      const option = computed(() => {
        const c = QW.colors();
        const d = (props.chips && props.chips.dist) || { prices: [], shares: [] };
        const last = (props.chips && props.chips.last) || {};
        const px = isNum(props.price) ? props.price : null;
        // 纵轴只显示主要部分：累计 1%~99% 的价格区间（再包含现价和平均成本），很久以前的零星筹码不拉宽坐标
        let acc = 0;
        let lo = null;
        let hi = null;
        d.prices.forEach((p, i) => {
          acc += d.shares[i];
          if (lo == null && acc >= 0.01) lo = p;
          if (hi == null && acc >= 0.99) hi = p;
        });
        const ext = [lo, hi, px, last.avg_cost].filter(isNum);
        const yMin = ext.length ? Math.min(...ext) * 0.97 : undefined;
        const yMax = ext.length ? Math.max(...ext) * 1.03 : undefined;
        const data = d.prices.map((p, i) => ({
          value: [d.shares[i] * 100, p],
          itemStyle: { color: px != null && p <= px ? c.up : c.primary, opacity: 0.8 },
        })).filter((x) => yMin == null || (x.value[1] >= yMin && x.value[1] <= yMax));
        const marks = [];
        if (isNum(last.avg_cost)) marks.push({ yAxis: last.avg_cost, name: "平均成本", lineStyle: { color: c.gold, type: "dashed" }, label: { formatter: "成本 " + last.avg_cost.toFixed(2), color: c.gold, position: "insideEndTop", fontSize: 10 } });
        if (px != null) marks.push({ yAxis: px, name: "现价", lineStyle: { color: c.text2 }, label: { formatter: "现价 " + px.toFixed(2), color: c.text2, position: "insideEndBottom", fontSize: 10 } });
        // 横向柱：每格从 0 画到占比（纵轴是连续的价格，每格高 1%）
        const renderItem = (params, api) => {
          const share = api.value(0);
          const price = api.value(1);
          const a = api.coord([0, price]);
          const b = api.coord([share, price]);
          const step = Math.abs(api.coord([0, price * 1.01])[1] - a[1]);
          const h = Math.max(1, step * 0.85);
          return { type: "rect", shape: { x: a[0], y: a[1] - h / 2, width: Math.max(1, b[0] - a[0]), height: h }, style: api.style() };
        };
        return {
          animation: false, textStyle: { fontFamily: c.font },
          grid: { left: 58, right: 14, top: 10, bottom: 24 },
          xAxis: { type: "value", min: 0, ...QW.axisBase(c), splitLine: { show: false }, axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          yAxis: { type: "value", min: yMin, max: yMax, ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v.toFixed(2) } },
          tooltip: { ...QW.tooltipBase(c), trigger: "item", formatter: (p) => `价格约 ${p.value[1].toFixed(2)}<br>这一档筹码占 ${p.value[0].toFixed(2)}%` },
          series: [{ type: "custom", renderItem, data, encode: { x: 0, y: 1 }, clip: true, markLine: { symbol: "none", silent: true, data: marks } }],
        };
      });
      return { option };
    },
    template: `<qw-chart :option="option" :height="height"/>`,
  };

  const IndPicker = {
    name: "QwIndPicker",
    props: { main: { type: Array, default: () => [] }, subs: { type: Array, default: () => [] }, catalog: { type: Array, default: () => [] }, maxSubs: { type: Number, default: 4 } },
    emits: ["update:main", "update:subs"],
    setup(props, { emit }) {
      const mains = computed(() => props.catalog.filter((x) => x.pane === "main"));
      const subsAll = computed(() => props.catalog.filter((x) => x.pane === "sub"));
      const toggleMain = (id) => emit("update:main", props.main.includes(id) ? props.main.filter((x) => x !== id) : [...props.main, id]);
      const toggleSub = (id) => {
        if (props.subs.includes(id)) { emit("update:subs", props.subs.filter((x) => x !== id)); return; }
        if (props.subs.length >= props.maxSubs) { QW.toast.info(`最多同时显示 ${props.maxSubs} 个副图，先关掉一个`); return; }
        emit("update:subs", [...props.subs, id]);
      };
      const tip = (x) => `${x.explain}\n\n用法：${x.usage}\n\n⚠ ${x.trap}`;
      return { mains, subsAll, toggleMain, toggleSub, tip };
    },
    template: `<div class="ind-picker">
      <div class="ind-row"><span class="ind-lab">主图</span>
        <button v-for="x in mains" :key="x.id" class="chip" :class="{active: main.includes(x.id)}" v-tip="tip(x)" @click="toggleMain(x.id)">{{ x.name }}</button></div>
      <div class="ind-row"><span class="ind-lab">副图</span>
        <button v-for="x in subsAll" :key="x.id" class="chip" :class="{active: subs.includes(x.id)}" v-tip="tip(x)" @click="toggleSub(x.id)">{{ x.name }}</button></div>
    </div>`,
  };


  // ------------------------------------------------------------------ 大盘环境
  const REGIME_LABEL = { strong: "强势", neutral: "震荡", weak: "弱势" };
  const RegimeCard = {
    name: "QwRegimeCard",
    props: { compact: Boolean },
    setup() {
      const data = Vue.ref(null);
      const err = Vue.ref("");
      const open = Vue.ref(false);
      const load = async (refresh) => {
        try { data.value = await QW.api.get("/api/market/regime", refresh ? { refresh: true } : null, { silent: true }); err.value = ""; }
        catch (e) { err.value = e.detail || e.message; }
      };
      Vue.onMounted(() => load(false));
      const option = computed(() => {
        const d = data.value;
        if (!d || !d.history || !d.history.length) return null;
        const c = QW.colors();
        const bands = [];
        d.history.forEach((h) => {
          const last = bands[bands.length - 1];
          if (last && last.regime === h.regime) last.end = h.date;
          else bands.push({ regime: h.regime, start: h.date, end: h.date });
        });
        const shown = bands.filter((b) => b.regime !== "neutral");
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 46, right: 10, top: 10, bottom: 22 },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", formatter: (ps) => {
            const h = d.history[ps[0].dataIndex];
            return `${h.date}<br>环境：${REGIME_LABEL[h.regime]}（${h.score} 分）<br>站上 20 日线 ${(h.above20 * 100).toFixed(0)}%`;
          } },
          xAxis: { type: "category", data: d.history.map((h) => h.date), ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => String(v).slice(2, 7) } },
          yAxis: { type: "value", scale: true, ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => (v >= 100 ? v.toFixed(0) : v.toFixed(2)) } },
          series: [{
            type: "line", showSymbol: false, lineStyle: { width: 1.8, color: c.text2 }, itemStyle: { color: c.text2 },
            data: d.history.map((h) => h.idx),
            // 背景色标出每段环境：红 = 强势，绿 = 弱势，无色 = 震荡
            markArea: { silent: true, data: shown.map((b) => [{ xAxis: b.start, itemStyle: { color: b.regime === "strong" ? c.up : c.down, opacity: 0.12 } }, { xAxis: b.end }]) },
          }],
        };
      });
      const pct = (v, d = 1) => (isNum(v) ? (v * 100).toFixed(d) + "%" : "—");
      const segs = [["selection", "选择期"], ["holdout", "留出期"]];
      return { data, err, open, load, option, pct, segs, REGIME_LABEL };
    },
    template: `<qw-card class="rg-card" :title="data ? '大盘环境：' + data.label : '大盘环境'" icon="gauge" :sub="data ? data.date + ' · ' + data.index_name : ''">
      <template #extra><button class="btn sm ghost" @click="load(true)" v-tip="'重新计算（日线更新后会自动重算）'"><qw-icon name="refresh" :size="14"/></button></template>
      <qw-empty v-if="err && !data" compact icon="alert" title="大盘环境暂时算不出来" :desc="err"/>
      <qw-skeleton v-else-if="!data" :rows="3"/>
      <template v-else>
        <div class="rg-head">
          <span class="rg-badge" :class="'t-' + data.tone">{{ data.label }}</span>
          <div class="rg-cap">建议总仓位不超过 <b class="num">{{ pct(data.cap, 0) }}</b></div>
          <div class="muted rg-advice">{{ data.advice }}</div>
        </div>
        <div class="rg-comps">
          <div v-for="c in data.components" :key="c.name" class="rg-comp"><span class="muted">{{ c.name }}</span><b>{{ c.text }}</b>
            <span class="qw-tag" :class="c.points > 0 ? 'red' : c.points < 0 ? 'green' : 'gray'">{{ c.points > 0 ? '+' + c.points : c.points }}</span></div>
        </div>
        <qw-chart v-if="!compact && option" :option="option" height="150px"/>
        <div v-if="!compact && option" class="muted" style="font-size:12px">曲线 = {{ data.index_name }}；背景红色 = 当时判为强势，绿色 = 弱势，没有颜色 = 震荡</div>
        <ul v-if="data.insights && data.insights.length" class="rg-ins"><li v-for="(t, i) in data.insights" :key="i">{{ t }}</li></ul>
        <button class="linkbtn" @click="open = !open">{{ open ? '收起历史统计' : '看历史统计（各环境之后 ' + data.fwd_days + ' 天的表现）' }}</button>
        <div v-if="open" class="hs-box"><div style="overflow-x:auto" v-hscroll>
          <table class="mini-table">
            <thead><tr><th>环境</th><th>时段</th><th>天数</th><th>之后平均涨跌</th><th>上涨概率</th><th>最差 10%</th><th>跌超 5% 概率</th><th>t 值</th></tr></thead>
            <tbody><template v-for="rg in ['strong','neutral','weak']" :key="rg"><tr v-for="sg in segs" :key="rg + sg[0]">
              <td>{{ REGIME_LABEL[rg] }}</td><td>{{ sg[1] }}</td>
              <template v-if="data.stats[rg] && data.stats[rg][sg[0]] && data.stats[rg][sg[0]].mean != null">
                <td class="num">{{ data.stats[rg][sg[0]].n }}</td><td class="num">{{ pct(data.stats[rg][sg[0]].mean, 2) }}</td>
                <td class="num">{{ pct(data.stats[rg][sg[0]].win, 0) }}</td><td class="num">{{ pct(data.stats[rg][sg[0]].p10, 1) }}</td>
                <td class="num">{{ pct(data.stats[rg][sg[0]].loss5, 0) }}</td><td class="num">{{ data.stats[rg][sg[0]].t ?? '—' }}</td>
              </template><td v-else colspan="6" class="muted">样本太少</td>
            </tr></template></tbody>
          </table>
        </div></div>
        <div class="muted" style="font-size:12px;margin-top:6px">{{ data.note }}</div>
      </template>
    </qw-card>`,
  };

  // ------------------------------------------------------------------ 主力阶段
  const STAGE_ORDER = ["accumulation", "washout", "markup", "distribution", "decline"];
  const STAGE_LABEL = { accumulation: "吸筹", washout: "洗盘", markup: "拉升", distribution: "出货", decline: "下跌", unclear: "不明确" };
  const StageCard = {
    name: "QwStageCard",
    props: { diag: { type: Object, default: null }, loading: Boolean, error: { type: String, default: "" } },
    emits: ["reload"],
    setup(props, { emit }) {
      const showAll = Vue.ref(false);
      const jobId = Vue.ref("");
      const st = computed(() => (props.diag ? props.diag.stage : null));
      const scores = computed(() => (st.value ? STAGE_ORDER.map((k) => ({ key: k, label: STAGE_LABEL[k], score: st.value.scores[k], pre: st.value.prereq[k] })) : []));
      const hist = computed(() => (st.value && st.value.history ? st.value.history[st.value.key] : null));
      const anyHist = computed(() => !!(st.value && st.value.history && Object.values(st.value.history).some((v) => v)));
      const runStats = async () => {
        try {
          jobId.value = await QW.jobs.start("/api/jobs/run", { name: "stage_stats" }, "主力阶段历史验证");
          QW.jobs.track(jobId.value, { title: "主力阶段历史验证", onDone: () => emit("reload") });
        } catch (e) { /* 已提示 */ }
      };
      const icon = (e) => (e.missing ? "—" : e.ok ? "✓" : "✗");
      const cls = (e) => (e.missing ? "miss" : e.ok ? "yes" : "no");
      const pct = (v) => (isNum(v) ? Math.round(v * 100) + "%" : "—");
      const STAGE_HELP = (QW.term && QW.term("主力")) || "";
      return { st, scores, hist, anyHist, showAll, runStats, jobId, icon, cls, pct, STAGE_LABEL, STAGE_ORDER, STAGE_HELP };
    },
    template: `<qw-card class="st-card" title="主力阶段" icon="compass" :help="STAGE_HELP" :loading="loading && !diag">
      <qw-empty v-if="error && !diag" compact icon="alert" title="诊断暂时做不了" :desc="error"/>
      <template v-else-if="st">
        <div class="st-head">
          <span class="st-badge" :class="'t-' + st.tone">{{ st.label }}</span>
          <span class="muted">把握：{{ st.confidence }}{{ st.score != null ? '（' + pct(st.score) + ' 的证据成立）' : '' }}</span>
        </div>
        <p class="st-desc">{{ st.desc }}</p>
        <div class="st-advice" :class="'t-' + st.tone"><qw-icon name="info" :size="15"/><span>{{ st.advice }}</span></div>
        <div v-if="hist" class="st-hist" :class="{weak: !hist.credible}"><qw-icon name="target" :size="14"/><span>{{ hist.text }}</span></div>
        <div v-else-if="!anyHist" class="st-hist weak"><qw-icon name="target" :size="14"/>
          <span>还没有做历史验证，不知道这些阶段判断之后的真实表现。<button class="linkbtn" @click="runStats">开始验证（约几分钟）</button></span></div>
        <qw-job v-if="jobId" :job-id="jobId" title="主力阶段历史验证"/>
        <div class="st-scores">
          <div v-for="s in scores" :key="s.key" class="st-score" :class="{on: s.key === st.key}" v-tip="s.pre === false ? '前提不满足：' + st.prereq_text[s.key] : ''">
            <span>{{ s.label }}</span><i><b :style="{width: (s.pre === false ? 0 : (s.score || 0) * 100) + '%'}"></b></i><em class="num">{{ s.pre === false ? '—' : pct(s.score) }}</em>
          </div>
        </div>
        <div class="st-ev-hd"><b>{{ st.key === 'unclear' ? '各阶段的证据' : '“' + st.label + '”的证据' }}</b>
          <button class="linkbtn" @click="showAll = !showAll">{{ showAll ? '只看当前阶段' : '看全部阶段的证据' }}</button></div>
        <template v-for="k in (showAll || st.key === 'unclear' ? STAGE_ORDER : [st.key])" :key="k">
          <div v-if="showAll || st.key === 'unclear'" class="st-ev-group">{{ STAGE_LABEL[k] }}<span v-if="st.prereq[k] === false" class="muted">（前提不满足：{{ st.prereq_text[k] }}）</span></div>
          <ul class="st-ev"><li v-for="e in st.evidence[k]" :key="e.key" :class="cls(e)"><span class="ic">{{ icon(e) }}</span><span>{{ e.label }}<em v-if="e.missing" class="muted">（没有{{ e.needs === 'chips' ? '筹码' : '' }}数据，不计入）</em></span></li></ul>
        </template>
        <div v-if="diag.timeline && diag.timeline.length" class="st-tl">
          <div class="muted" style="font-size:12px;margin-bottom:4px">最近 {{ diag.timeline.reduce((a, s) => a + s.days, 0) }} 个交易日的阶段变化（鼠标移上去看日期）</div>
          <div class="st-strip"><i v-for="(s, i) in diag.timeline" :key="i" :class="'s-' + s.stage" :style="{flex: s.days}" v-tip="s.label + '：' + s.start + ' ~ ' + s.end + '（' + s.days + ' 天）'"></i></div>
          <div class="st-legend"><span v-for="k in [...STAGE_ORDER, 'unclear']" :key="k"><i :class="'s-' + k"></i>{{ STAGE_LABEL[k] }}</span></div>
        </div>
        <div v-if="diag.volume_price && diag.volume_price.length" class="st-vp">
          <div class="muted" style="font-size:12px;margin:8px 0 4px">量价关系</div>
          <ul><li v-for="(v, i) in diag.volume_price" :key="i" :class="'t-' + v.tone">{{ v.text }}</li></ul>
        </div>
        <div v-if="diag.reference && diag.reference.length" class="st-ref">
          <div v-for="r in diag.reference" :key="r.key" :class="'t-' + r.tone"><span class="muted">{{ r.label }}</span><div>{{ r.text }}</div></div>
        </div>
        <p class="muted" style="font-size:12px;margin-top:8px">{{ diag.note }}</p>
      </template>
    </qw-card>`,
  };

  const RiskCard = {
    name: "QwRiskCard",
    props: { risk: { type: Object, default: null }, loading: Boolean },
    setup() {
      const dot = { red: "red", yellow: "warn", green: "green", none: "gray" };
      return { dot };
    },
    template: `<qw-card class="rk-card" title="排雷体检" icon="shield" :loading="loading && !risk">
      <template #extra><span v-if="risk" class="qw-tag" :class="dot[risk.level]">{{ risk.level_text }}</span></template>
      <ul v-if="risk" class="rk-list">
        <li v-for="it in risk.items" :key="it.key" :class="'lv-' + it.level">
          <i class="rk-dot"></i><div><b>{{ it.title }}</b><span class="rk-lv">{{ it.level_text }}</span><div class="muted">{{ it.detail }}<template v-if="it.date"> · {{ it.date }}</template></div></div>
        </li>
      </ul>
      <p v-if="risk" class="muted" style="font-size:12px;margin-top:6px">{{ risk.note }} 灰色 = 没有数据（可到“数据源”页下载扩展数据），不等于没有风险。</p>
    </qw-card>`,
  };

  reg.push(["qw-kline-pro", KlinePro], ["qw-chips", Chips], ["qw-ind-picker", IndPicker],
    ["qw-regime-card", RegimeCard], ["qw-stage-card", StageCard], ["qw-risk-card", RiskCard]);
})();
