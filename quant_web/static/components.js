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

  reg.push(["qw-kline-pro", KlinePro], ["qw-chips", Chips], ["qw-ind-picker", IndPicker]);
})();
