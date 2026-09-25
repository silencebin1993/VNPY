/* 连板 #/lianban：连板天梯（按高度分层；可以选过去的日子复盘"次日有没有晋级"）、各高度晋级率、情绪周期、
   涨停 / 炸板 / 跌停池。全部是看盘复盘用的，不是买入信号：历史上按真实规则去买连板股是亏钱的（见模型中心）。 */
(function () {
  "use strict";
  const { ref, computed, onMounted, watch } = Vue;
  const { api, fmt, isNum } = QW;

  const HELP = {
    ladder: "按连续涨停天数把当天涨停的股票分成几列，越靠左连板越多。“一字”= 开盘就封死涨停、全天没打开，普通人很难买进。选过去的日子时，每只股票下面会显示它第二天的结果（晋级 = 第二天继续涨停）。",
    promote: "统计最近一段时间里，当天是首板 / 2板 / 3板……的股票，第二天继续涨停（晋级）的比例和第二天的平均表现。高度越高晋级率一般越高，但一字板买不进，实际能拿到的收益比平均值差得多。",
    cycle: "短线情绪的周期：涨停家数、跌停家数、炸板率、最高连板、昨天涨停的股票今天的晋级率。情绪由冷转热时连板股容易走强，由热转冷时高位股容易大跌。",
    model: "连板晋级模型能预测谁明天更可能继续涨停（样本外 AUC 约 0.72），但按真实规则（一字买不进、次日开盘价买入、手续费）去买，历史上平均是亏钱的，而且不如同一天随便挑。所以这里的概率只用来观察，不要照着买。",
  };

  QW.page("lianban", {
    props: ["params", "query"],
    setup(props) {
      const day = ref((props.query && props.query.date) || "");
      const mainOnly = ref(false);
      const data = ref(null);
      const stats = ref(null);
      const loading = ref(true);
      const err = ref("");
      const statDays = ref(60);
      const tab = ref("zt");
      const hlInd = ref("");

      const load = async () => {
        loading.value = true;
        try {
          data.value = await api.get("/api/lianban/ladder", { date: day.value || undefined, main_only: mainOnly.value }, { silent: true });
          err.value = "";
          if (!day.value && data.value) day.value = data.value.date;
        } catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
      };
      const loadStats = async () => {
        try { stats.value = await api.get("/api/lianban/stats", { days: statDays.value, main_only: mainOnly.value }, { silent: true }); }
        catch (e) { stats.value = { error: e.detail || e.message }; }
      };
      onMounted(() => { load(); loadStats(); });
      watch(day, (v, old) => { if (old && v !== old) load(); });
      watch(mainOnly, () => { load(); loadStats(); });
      watch(statDays, loadStats);

      const dates = computed(() => (stats.value && stats.value.dates) || []);
      const shiftDay = (k) => {
        const list = dates.value;                 // 新 → 旧
        const i = list.indexOf(day.value);
        if (i < 0) return;
        const j = i - k;                          // k=+1 → 更新的一天
        if (j >= 0 && j < list.length) day.value = list[j];
      };

      const s = computed(() => (data.value && data.value.summary) || {});
      const kpis = computed(() => {
        const x = s.value;
        return [
          { label: "涨停", value: isNum(x.n_limit_up) ? x.n_limit_up : "—", unit: "家", tone: "up" },
          { label: "跌停", value: isNum(x.n_limit_down) ? x.n_limit_down : "—", unit: "家", tone: "down" },
          { label: "炸板率", value: isNum(x.break_rate) ? (x.break_rate * 100).toFixed(0) : "—", unit: "%", help: "盘中碰到涨停但收盘没封住的股票，占“碰到过涨停”的比例。越高说明资金越不愿意封板。" },
          { label: "最高板", value: isNum(x.max_streak) ? x.max_streak : "—", unit: "板" },
          { label: "连板股", value: isNum(x.n_streak2) ? x.n_streak2 : "—", unit: "家", help: "连续涨停 2 天及以上的股票数" },
          { label: "昨涨停今晋级", value: isNum(x.prev_lu_promote) ? (x.prev_lu_promote * 100).toFixed(0) : "—", unit: "%", help: "昨天涨停的股票，今天继续涨停的比例" },
          { label: "昨涨停今表现", value: isNum(x.prev_lu_premium) ? (x.prev_lu_premium > 0 ? "+" : "") + x.prev_lu_premium.toFixed(2) : "—", unit: "%", tone: fmt.dir(x.prev_lu_premium), help: "昨天涨停的股票今天的平均涨跌幅（接力情绪）" },
          { label: "情绪温度", value: isNum(x.temperature) ? Math.round(x.temperature) : "—", unit: x.label || "", help: "把涨停、跌停、炸板率、昨涨停溢价、最高板、上涨家数综合成 0~100 的分位（只用过去一年数据比较）" },
        ];
      });

      const tiers = computed(() => ((data.value && data.value.tiers) || []));
      const zt = computed(() => tiers.value.flatMap((t) => t.stocks));
      const industries = computed(() => {
        const m = {};
        zt.value.forEach((r) => { const k = r.industry || "未知"; m[k] = (m[k] || 0) + 1; });
        return Object.entries(m).map(([industry, count]) => ({ industry, count })).sort((a, b) => b.count - a.count).slice(0, 14);
      });
      const past = computed(() => !!(data.value && data.value.next_date));
      const chipCls = (st) => {
        const c = [];
        if (hlInd.value) c.push(st.industry === hlInd.value ? "hl" : "dim");
        if (past.value && st.next_limit_up) c.push("promoted");
        return c;
      };
      const yi = (v) => (isNum(v) ? (v / 1e8).toFixed(v >= 1e9 ? 1 : 2) + "亿" : "—");
      const chipTip = (st) => {
        const lines = [`${st.name}（${st.code}）  ${st.industry || ""}`, `当天涨幅 ${fmt.ratio(st.pct, 2, true)}，成交 ${fmt.money(st.amount)}，换手 ${isNum(st.turn) ? st.turn.toFixed(1) + "%" : "—"}`];
        if (st.first_time) lines.push(`首次封板 ${st.first_time}${st.last_time && st.last_time !== st.first_time ? "，最后封板 " + st.last_time : ""}，炸板 ${st.open_times || 0} 次`);
        if (isNum(st.seal_amount)) lines.push(`封板资金 ${yi(st.seal_amount)}`);
        if (st.stat) lines.push(`近期涨停统计 ${st.stat}（天/板）`);
        if (isNum(st.prob)) lines.push(`模型晋级概率 ${fmt.ratio(st.prob, 0)}（观察用）`);
        if (past.value) lines.push(st.next_pct === null || st.next_pct === undefined ? "次日：停牌" : `次日：开盘 ${fmt.ratio(st.next_open_pct, 1, true)}，收盘 ${fmt.ratio(st.next_pct, 1, true)}${st.next_limit_up ? "，继续涨停（晋级）" : ""}`);
        lines.push("点击看盘");
        return lines.join("\n");
      };

      const indOption = computed(() => {
        const c = QW.colors();
        const list = industries.value;
        const ax = QW.axisBase(c);
        return {
          animation: false, textStyle: { fontFamily: c.font },
          grid: { left: 8, right: 30, top: 4, bottom: 4, containLabel: true },
          tooltip: { ...QW.tooltipBase(c), trigger: "item", formatter: (p) => `${list[p.dataIndex].industry}<br>涨停 <b>${p.value}</b> 家<br><span style="opacity:.7">点击在天梯里高亮</span>` },
          xAxis: { type: "value", minInterval: 1, ...ax, axisLabel: { show: false }, splitLine: { show: false }, axisLine: { show: false } },
          yAxis: { type: "category", inverse: true, data: list.map((x) => fmt.industry(x.industry)), ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, color: c.text2 } },
          series: [{ type: "bar", barWidth: 12, cursor: "pointer", label: { show: true, position: "right", color: c.text2, fontSize: 12 },
            data: list.map((x) => ({ value: x.count, itemStyle: { color: c.up, opacity: !hlInd.value || hlInd.value === x.industry ? 0.9 : 0.25, borderRadius: [0, 4, 4, 0] } })) }],
        };
      });
      const onIndClick = (p) => { const it = industries.value[p.dataIndex]; if (it) hlInd.value = hlInd.value === it.industry ? "" : it.industry; };

      const promoRows = computed(() => (stats.value && stats.value.promotion && stats.value.promotion.rows) || []);
      const cycle = computed(() => ((stats.value && stats.value.cycle) || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1)));
      const cycleOption = computed(() => {
        const c = QW.colors();
        const h = cycle.value;
        if (!h.length) return null;
        const ax = QW.axisBase(c);
        const dates = h.map((r) => r.date.slice(5));
        const mark = day.value ? day.value.slice(5) : null;
        return {
          animation: false, textStyle: { fontFamily: c.font },
          legend: { top: 0, textStyle: { color: c.text2, fontSize: 12 }, itemWidth: 14, itemHeight: 8 },
          grid: [{ left: 40, right: 44, top: 30, height: "44%" }, { left: 40, right: 44, top: "66%", bottom: 24 }],
          tooltip: { ...QW.tooltipBase(c), trigger: "axis" },
          axisPointer: { link: [{ xAxisIndex: "all" }] },
          xAxis: [
            { type: "category", data: dates, gridIndex: 0, ...ax, axisLabel: { show: false } },
            { type: "category", data: dates, gridIndex: 1, ...ax },
          ],
          yAxis: [
            { type: "value", gridIndex: 0, ...ax, name: "家", nameTextStyle: { color: c.text3 } },
            { type: "value", gridIndex: 0, ...ax, position: "right", splitLine: { show: false }, name: "板", minInterval: 1, nameTextStyle: { color: c.text3 } },
            { type: "value", gridIndex: 1, min: 0, max: 100, ...ax, axisLabel: { ...ax.axisLabel, formatter: "{value}%" } },
          ],
          series: [
            { name: "涨停", type: "bar", data: h.map((r) => r.n_limit_up), itemStyle: { color: c.up, opacity: 0.75 }, barMaxWidth: 8,
              markLine: mark ? { symbol: "none", silent: true, lineStyle: { color: c.text3, type: "dashed" }, data: [{ xAxis: mark }], label: { show: false } } : undefined },
            { name: "跌停", type: "bar", data: h.map((r) => r.n_limit_down), itemStyle: { color: c.down, opacity: 0.75 }, barMaxWidth: 8 },
            { name: "最高板", type: "line", yAxisIndex: 1, data: h.map((r) => r.max_streak), symbol: "none", lineStyle: { width: 1.6, color: c.series[2] }, itemStyle: { color: c.series[2] } },
            { name: "炸板率", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: h.map((r) => (isNum(r.break_rate) ? +(fmt.frac(r.break_rate, 1.5) * 100).toFixed(1) : null)), symbol: "none", lineStyle: { width: 1.4, color: c.series[3] }, itemStyle: { color: c.series[3] } },
            { name: "昨涨停今晋级率", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: h.map((r) => (isNum(r.prev_lu_promote) ? +(r.prev_lu_promote * 100).toFixed(1) : null)), symbol: "none", lineStyle: { width: 1.4, color: c.series[0] }, itemStyle: { color: c.series[0] } },
            { name: "情绪温度", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: h.map((r) => (isNum(r.temperature) ? Math.round(r.temperature) : null)), symbol: "none", lineStyle: { width: 1.2, type: "dashed", color: c.series[1] }, itemStyle: { color: c.series[1] } },
          ],
        };
      });
      const onCycleClick = (p) => { const r = cycle.value[p.dataIndex]; if (r && dates.value.includes(r.date)) day.value = r.date; };

      const poolRows = computed(() => {
        if (!data.value) return [];
        if (tab.value === "zb") return data.value.broken || [];
        if (tab.value === "dt") return data.value.limit_down || [];
        return zt.value;
      });
      const poolCols = computed(() => {
        const cols = [
          { key: "name", label: "名称", minWidth: "110px" },
          { key: "industry", label: "行业" },
          { key: "streak", label: tab.value === "dt" ? "连续跌停" : "连板", align: "right", sortable: true },
          { key: "pct", label: "涨跌幅", align: "right", sortable: true },
          { key: "amount", label: "成交额", align: "right", sortable: true },
          { key: "turn", label: "换手", align: "right", sortable: true },
        ];
        if (data.value && data.value.source === "pool") {
          cols.push({ key: "first_time", label: tab.value === "dt" ? "—" : "首封", align: "right" });
          cols.push({ key: "open_times", label: tab.value === "dt" ? "开板次数" : "炸板", align: "right", sortable: true });
          if (tab.value !== "zb") cols.push({ key: "seal_amount", label: "封单", align: "right", sortable: true });
        }
        if (tab.value === "zt" && data.value && data.value.has_probs) cols.push({ key: "prob", label: "晋级概率(观察)", align: "right", sortable: true, help: HELP.model });
        if (past.value) {
          cols.push({ key: "next_open_pct", label: "次日开盘", align: "right", sortable: true });
          cols.push({ key: "next_pct", label: "次日收盘", align: "right", sortable: true });
        }
        return cols;
      });
      const tabs = computed(() => [
        { value: "zt", label: "涨停", badge: zt.value.length },
        { value: "zb", label: "炸板", badge: ((data.value && data.value.broken) || []).length },
        { value: "dt", label: "跌停", badge: ((data.value && data.value.limit_down) || []).length },
      ]);

      return {
        HELP, fmt, isNum, day, mainOnly, data, stats, loading, err, statDays, tab, hlInd, dates, shiftDay, kpis, tiers, industries,
        past, chipCls, chipTip, yi, indOption, onIndClick, promoRows, cycleOption, onCycleClick, poolRows, poolCols, tabs, load,
      };
    },
    template: `<div class="lb-page">
      <div class="lb-bar">
        <div class="row" style="gap:6px">
          <button class="qw-iconbtn" :disabled="!dates.length || dates.indexOf(day) >= dates.length - 1" @click="shiftDay(-1)" v-tip="'前一个交易日'"><qw-icon name="chevronLeft" :size="16"/></button>
          <select v-model="day" class="qw-input lb-date"><option v-for="d in dates" :key="d" :value="d">{{ d }}</option></select>
          <button class="qw-iconbtn" :disabled="!dates.length || dates.indexOf(day) <= 0" @click="shiftDay(1)" v-tip="'后一个交易日'"><qw-icon name="chevronRight" :size="16"/></button>
          <label class="lb-check"><input v-model="mainOnly" type="checkbox"> 只看主板</label>
          <span v-if="data" class="muted">{{ data.source === 'pool' ? '数据：涨停池存档（含封单、首封时间）' : '数据：日线识别（没有封单、首封时间）' }}{{ data.next_date ? '，已附 ' + data.next_date + ' 次日结果' : '' }}</span>
        </div>
        <div class="row" style="gap:8px">
          <a class="btn sm soft" href="#/predict?kind=streak" v-tip="HELP.model"><qw-icon name="rocket" :size="14"/>连板晋级模型（观察）</a>
        </div>
      </div>

      <qw-empty v-if="err" icon="alert" title="连板数据暂时算不出来" :desc="err" action-text="重试" @action="load"/>
      <template v-else>
        <div class="lb-kpis">
          <qw-stat v-for="k in kpis" :key="k.label" :label="k.label" :value="k.value" :unit="k.unit" :tone="k.tone" :help="k.help" size="sm" :loading="loading && !data"/>
        </div>

        <qw-card title="连板天梯" icon="ladder" :help="HELP.ladder" :sub="data ? data.date + (past ? ' · 绿框 = 次日晋级' : '') : ''" :loading="loading && !data">
          <template #extra>
            <button v-if="hlInd" class="chip active" @click="hlInd = ''">{{ $fmt.industry(hlInd) }} ×</button>
          </template>
          <div v-if="tiers.length" class="ladder lb-ladder">
            <div v-for="col in tiers" :key="col.streak" class="ladder-col" :class="{first: col.streak <= 1}">
              <div class="ladder-hd"><qw-streak :n="col.streak"/>
                <span class="cnt">{{ col.count }} 只<template v-if="past && col.promoted !== null">，晋级 {{ col.promoted }}</template></span></div>
              <div class="ladder-bd">
                <a v-for="st in col.stocks" :key="st.code" class="ladder-chip lb-chip" :class="chipCls(st)" :href="'#/watch/' + st.code" v-tip="chipTip(st)">
                  <span class="nm">{{ st.name }}</span>
                  <span v-if="st.one_word" class="yz">一字</span>
                  <span v-else-if="st.open_times" class="lb-open" v-tip="'炸板 ' + st.open_times + ' 次后回封'">炸{{ st.open_times }}</span>
                  <span v-if="isNum(st.prob) && !past" class="lb-prob" v-tip="HELP.model">{{ Math.round(st.prob * 100) }}%</span>
                  <span v-if="past" class="lb-next" :class="$fmt.dir(st.next_pct)">{{ st.next_pct === null || st.next_pct === undefined ? '停牌' : fmt.ratio(st.next_pct, 1, true) }}</span>
                </a>
              </div>
            </div>
          </div>
          <qw-empty v-else-if="data" compact icon="ladder" title="这一天没有涨停的股票"/>
        </qw-card>

        <div class="lb-row3">
          <qw-card title="各高度晋级率" icon="target" :help="HELP.promote" :sub="stats && stats.promotion && stats.promotion.start ? stats.promotion.start + ' ~ ' + stats.promotion.end : ''" :pad="false">
            <template #extra>
              <select v-model.number="statDays" class="qw-input" style="height:28px;width:auto"><option :value="20">近20日</option><option :value="60">近60日</option><option :value="120">近半年</option><option :value="250">近一年</option></select>
            </template>
            <div class="lb-scroll">
              <table class="mini-table lb-promo">
                <thead><tr><th>当天</th><th>样本</th><th>次日晋级</th><th>次日开盘</th><th>次日收盘</th><th>次日红盘</th><th>次日一字</th></tr></thead>
                <tbody>
                  <tr v-for="r in promoRows" :key="r.tier">
                    <td><b>{{ r.label }}</b></td>
                    <td class="num">{{ r.n }}</td>
                    <td class="num"><b>{{ fmt.ratio(r.promote, 0) }}</b></td>
                    <td class="num" :class="$fmt.dir(r.open_prem)">{{ fmt.ratio(r.open_prem, 1, true) }}</td>
                    <td class="num" :class="$fmt.dir(r.close_ret)">{{ fmt.ratio(r.close_ret, 1, true) }}</td>
                    <td class="num">{{ fmt.ratio(r.red, 0) }}</td>
                    <td class="num">{{ fmt.ratio(r.one_word_next, 0) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="muted lb-foot">{{ stats && stats.promotion ? stats.promotion.note : '' }}</div>
          </qw-card>
          <qw-card title="涨停行业分布" icon="bars" sub="点柱子在天梯里高亮">
            <qw-chart v-if="industries.length" :option="indOption" :height="Math.max(220, industries.length * 24 + 20) + 'px'" @chart-click="onIndClick"/>
            <qw-empty v-else compact icon="bars" title="没有涨停股"/>
          </qw-card>
          <qw-card title="情绪周期（近一年）" icon="pulse" :help="HELP.cycle" sub="点图切换到那一天">
            <qw-chart v-if="cycleOption" :option="cycleOption" height="330px" @chart-click="onCycleClick"/>
            <qw-skeleton v-else :rows="6"/>
          </qw-card>
        </div>

        <qw-card title="股票池" icon="listCheck" :sub="data ? data.date + (past ? ' · 附次日表现' : '') : ''" :pad="false">
          <div class="lb-tabs"><qw-tabs v-model="tab" :items="tabs"/></div>
          <qw-table :columns="poolCols" :rows="poolRows" row-key="code" dense clickable max-height="520px" :page-size="200"
            :default-sort="{key: 'streak', order: 'desc'}" @row-click="(r) => $go('/watch/' + r.code)" empty-text="没有股票">
            <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
            <template #cell-industry="{row}"><span class="muted">{{ $fmt.industry(row.industry) }}</span></template>
            <template #cell-streak="{row}"><span class="num">{{ tab === 'dt' ? (row.dt_days || '—') : row.streak }}</span></template>
            <template #cell-pct="{row}"><qw-price :value="row.pct" ratio/></template>
            <template #cell-amount="{row}"><span class="num">{{ $fmt.money(row.amount) }}</span></template>
            <template #cell-turn="{row}"><span class="num">{{ isNum(row.turn) ? row.turn.toFixed(1) + '%' : '—' }}</span></template>
            <template #cell-first_time="{row}"><span class="num muted">{{ row.first_time || '—' }}</span></template>
            <template #cell-open_times="{row}"><span class="num">{{ isNum(row.open_times) ? row.open_times : '—' }}</span></template>
            <template #cell-seal_amount="{row}"><span class="num">{{ yi(row.seal_amount) }}</span></template>
            <template #cell-prob="{row}"><span class="num muted">{{ isNum(row.prob) ? fmt.ratio(row.prob, 0) : '—' }}</span></template>
            <template #cell-next_open_pct="{row}"><qw-price :value="row.next_open_pct" ratio/></template>
            <template #cell-next_pct="{row}"><span v-if="row.next_limit_up" class="qw-tag red">晋级</span> <qw-price :value="row.next_pct" ratio/></template>
          </qw-table>
        </qw-card>
      </template>
    </div>`,
  });
})();
