/* 量化选股 #/mf：多因子 + LightGBM 的每周选股（核心功能）。
   上半部分：本周调仓组合、全部排名、按资金算的下单清单；下半部分：如实的样本外回测（和同池随机、指数比）、
   按资金规模的容量表、逐年成绩、方法对照、因子表、前向跟踪。所有成绩都是样本外、扣费后的。 */
(function () {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount, watch } = Vue;
  const { api, fmt, isNum } = QW;

  const GROUPS = { reversal: "反转", activity: "低换手", risk: "低波动", sentiment: "情绪", value: "估值", quality: "质量", surprise: "业绩", size: "小市值", trend: "趋势" };
  const GROUP_KEYS = ["reversal", "activity", "risk", "sentiment", "value", "size"];
  const HELP = {
    method: "每周最后一个交易日收盘后，用 34 个公开研究里有依据的因子（反转、换手、波动、涨停次数、估值、业绩等），让 LightGBM 学“哪些股票下一周比同池平均涨得多”。模型每次只用之前三年的数据训练（滚动重训），所以历史成绩全部是样本外的。",
    random: "同池随机 = 在同样的可买股票里等权买全部股票（相当于随便挑的平均结果），而且不扣任何费用——对它有利，结论更保守。蒙特卡洛随机组合 = 随便挑 50 只、换手率和策略一样、同样扣费，重复 60 次。",
    capacity: "资金越大，每只股票要买的金额越大：一是流动性不够的股票不能买（单只下单金额超过它日均成交额 5% 的排除），二是冲击成本变高（按平方根模型估算）。所以同一个方法，资金规模不同成绩不同。",
    t: "t 值衡量“超额收益是不是运气”：大于 2 通常认为比较可信，1~2 说明有迹象但不能排除运气，小于 1 基本看不出来。",
    keep: "为了少交手续费：已持有的股票只要排名还在前 150 名就不卖；空出来的位置按排名补。单个行业最多 8 只。",
  };

  QW.page("mf", {
    props: ["params", "query"],
    setup() {
      const data = ref(null);
      const report = ref(null);
      const track = ref(null);
      const plan = ref(null);
      const loading = ref(true);
      const err = ref("");
      const tab = ref("target");
      const capital = ref(null);
      const capSel = ref(null);
      const job = ref("");
      const jobTitle = ref("");

      const loadToday = async () => {
        try { data.value = await api.get("/api/mf/today", null, { silent: true }); err.value = ""; }
        catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
        if (data.value && capital.value === null) capital.value = data.value.capital;
      };
      const loadReport = async () => {
        try { report.value = (await api.get("/api/mf/report", null, { silent: true })).report; } catch (e) { report.value = null; }
        if (report.value && !capSel.value) capSel.value = report.value.spec.default_capital;
      };
      const loadTrack = async () => { try { track.value = await api.get("/api/mf/track", null, { silent: true }); } catch (e) { track.value = null; } };
      const loadPlan = async () => {
        if (!data.value || !data.value.today) return;
        try { plan.value = await api.get("/api/mf/plan", { capital: capital.value || undefined }, { silent: true }); } catch (e) { plan.value = null; }
      };
      onMounted(async () => { await loadToday(); loadReport(); loadTrack(); loadPlan(); });
      QW.onReturn(() => { loadToday().then(loadPlan); loadTrack(); });   // 切回来：收盘后的自动打分可能已经更新了名单
      let timer = null;
      watch(capital, () => { clearTimeout(timer); timer = setTimeout(loadPlan, 400); });
      onBeforeUnmount(() => clearTimeout(timer));

      const today = computed(() => data.value && data.value.today);
      const rowsBy = computed(() => { const m = {}; ((today.value && today.value.rows) || []).forEach((r) => { m[r.code] = r; }); return m; });
      const prevSet = computed(() => new Set((today.value && today.value.prev_target) || []));
      const targetRows = computed(() => ((today.value && today.value.target) || []).map((c, i) => ({ ...(rowsBy.value[c] || { code: c, name: "" }), slot: i + 1, isNew: !prevSet.value.has(c) })));
      const sellRows = computed(() => [...prevSet.value].filter((c) => !((today.value && today.value.target) || []).includes(c)).map((c) => ({ code: c, name: (rowsBy.value[c] || {}).name || "", rank: (rowsBy.value[c] || {}).rank })));
      const allRows = computed(() => (today.value && today.value.rows) || []);

      const runJob = async (url, title) => {
        try { job.value = await QW.jobs.start(url, {}, title); jobTitle.value = title; } catch (e) { /* 已提示 */ }
      };
      const onJobDone = () => { job.value = ""; loadToday().then(loadPlan); loadReport(); loadTrack(); };

      // ------------------------------------------------ 回测：选中的资金规模
      const capRows = computed(() => (report.value && report.value.capacity) || []);
      const capNow = computed(() => capRows.value.find((c) => c.capital === capSel.value) || capRows.value[0] || null);
      const segOOS = computed(() => (capNow.value ? capNow.value.segments["全部样本外"] : null));
      const mainCap = computed(() => capRows.value.find((c) => report.value && c.capital === report.value.spec.default_capital) || null);
      const verdict = computed(() => {
        const s = mainCap.value && mainCap.value.segments["全部样本外"];
        if (!s) return null;
        const good = s.excess_ann > 0 && s.excess_t >= 2;
        const some = s.excess_ann > 0 && s.excess_t >= 1;
        return {
          tone: good ? "good" : some ? "mid" : "bad",
          title: good ? "样本外显著跑赢同池随机" : some ? "样本外跑赢同池随机，但统计上还不算铁证" : "样本外没有跑赢同池随机",
          s,
        };
      });
      const capLabel = (v) => (v >= 1e8 ? (v / 1e8) + " 亿" : (v / 1e4) + " 万");
      const tCls = (t) => (isNum(t) ? (t >= 2 ? "up" : t >= 1 ? "" : "muted") : "muted");

      const navOption = computed(() => {
        const r = report.value;
        if (!r || !r.nav || !r.nav.dates) return null;
        const c = QW.colors();
        const ax = QW.axisBase(c);
        const n = r.nav;
        const series = [
          { name: "量化选股（扣费）", type: "line", data: n.strategy, symbol: "none", lineStyle: { width: 2.2, color: c.up }, itemStyle: { color: c.up }, z: 5 },
          { name: "同池随机（不扣费）", type: "line", data: n.random, symbol: "none", lineStyle: { width: 1.6, color: c.text2 }, itemStyle: { color: c.text2 } },
        ];
        const idxColor = { sh000852: c.series[1], sh000905: c.series[2], sh000300: c.series[3] };
        Object.entries(r.indexes || {}).forEach(([code, label]) => {
          if (n[code]) series.push({ name: label, type: "line", data: n[code], symbol: "none", lineStyle: { width: 1.2, type: "dashed", color: idxColor[code] }, itemStyle: { color: idxColor[code] } });
        });
        if (n.random_p10 && n.random_p90) {
          series.push({ name: "随机组合 10%~90% 区间", type: "line", data: n.random_p10, symbol: "none", lineStyle: { opacity: 0 }, stack: "band", silent: true, itemStyle: { color: c.text3 } });
          series.push({ name: "随机组合 10%~90% 区间", type: "line", data: n.random_p90.map((v, i) => v - n.random_p10[i]), symbol: "none", lineStyle: { opacity: 0 }, stack: "band", silent: true, areaStyle: { color: c.text3, opacity: 0.12 }, itemStyle: { color: c.text3 } });
        }
        return {
          animation: false, textStyle: { fontFamily: c.font },
          legend: { top: 0, textStyle: { color: c.text2, fontSize: 12 }, itemWidth: 16, itemHeight: 8 },
          grid: { left: 50, right: 16, top: 34, bottom: 28 },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => (isNum(v) ? v.toFixed(3) : "—") },
          xAxis: { type: "category", data: n.dates, ...ax },
          yAxis: { type: "value", scale: true, ...ax },
          series,
        };
      });
      const histOption = computed(() => {
        const r = report.value;
        const s = mainCap.value && mainCap.value.segments["全部样本外"];
        if (!r || !r.random_cagr || !r.random_cagr.length || !s) return null;
        const c = QW.colors();
        const vals = r.random_cagr;
        const lo = Math.min(...vals, s.cagr) - 0.02;
        const hi = Math.max(...vals, s.cagr) + 0.02;
        const bins = 16;
        const w = (hi - lo) / bins;
        const counts = Array(bins).fill(0);
        vals.forEach((v) => { counts[Math.min(bins - 1, Math.floor((v - lo) / w))] += 1; });
        const labels = counts.map((_, i) => ((lo + (i + 0.5) * w) * 100).toFixed(0) + "%");
        const sBin = Math.min(bins - 1, Math.floor((s.cagr - lo) / w));
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 36, right: 12, top: 16, bottom: 30 },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis" },
          xAxis: { type: "category", data: labels, ...QW.axisBase(c), name: "年化收益", nameLocation: "middle", nameGap: 22 },
          yAxis: { type: "value", minInterval: 1, ...QW.axisBase(c) },
          series: [{ type: "bar", data: counts.map((v, i) => ({ value: v, itemStyle: { color: i === sBin ? c.up : c.text3, opacity: i === sBin ? 0.9 : 0.5 } })), barCategoryGap: "10%",
            markPoint: { symbol: "pin", symbolSize: 40, data: [{ coord: [sBin, counts[sBin]], value: "策略", itemStyle: { color: c.up } }], label: { fontSize: 11 } } }],
        };
      });
      const factorRows = computed(() => (report.value && report.value.factors) || []);
      const trackRows = computed(() => ((track.value && track.value.periods) || []).slice().reverse());
      const trackSum = computed(() => {
        const ps = ((track.value && track.value.periods) || []).filter((p) => p.complete);
        if (!ps.length) return null;
        const s = ps.reduce((a, p) => a * (1 + p.ret), 1) - 1;
        const b = ps.reduce((a, p) => a * (1 + (isNum(p.bench) ? p.bench : 0)), 1) - 1;
        return { n: ps.length, s, b };
      });
      const groupBar = (v) => (isNum(v) ? Math.round(v * 100) : 0);
      // 主力阶段对照：组合里 / 全部可买股票里，各阶段下一周比同池平均多赚多少（样本外）
      const stageRows = computed(() => {
        const sc = report.value && report.value.stage_check;
        if (!sc) return [];
        const pool = Object.fromEntries((sc.pool || []).map((x) => [x.stage, x]));
        const held = Object.fromEntries((sc.held || []).map((x) => [x.stage, x]));
        return ["unclear", "decline", "accumulation", "washout", "markup", "distribution"].filter((k) => held[k] || pool[k])
          .map((k) => ({ key: k, label: (held[k] || pool[k]).label, held: held[k] || null, pool: pool[k] || null }));
      });
      const stageNote = computed(() => {
        const sc = report.value && report.value.stage_check;
        const mk = stageRows.value.find((r) => r.key === "markup");
        if (!sc || !mk || !mk.pool) return null;
        return { markup: mk.pool.excess_wk, markupT: mk.pool.t, all: sc.held_excess_wk, drop: sc.drop_excess_wk, dropShare: sc.drop_share, weeks: sc.weeks };
      });
      const wk = (v) => (isNum(v) ? (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%" : "—");

      return {
        HELP, GROUPS, GROUP_KEYS, fmt, isNum, data, report, track, plan, loading, err, tab, capital, capSel, job, jobTitle, today, targetRows,
        sellRows, allRows, runJob, onJobDone, capRows, capNow, segOOS, mainCap, verdict, capLabel, tCls, navOption, histOption, factorRows,
        trackRows, trackSum, groupBar, loadPlan, stageRows, stageNote, wk,
      };
    },
    template: `<div class="mf-page">
      <div v-if="verdict" class="mf-verdict" :class="verdict.tone">
        <div class="mf-verdict-hd">
          <qw-icon :name="verdict.tone === 'bad' ? 'xCircle' : 'checkCircle'" :size="20"/>
          <b>{{ verdict.title }}</b>
          <span class="muted">样本外 {{ report.oos_start }} ~ {{ report.data_end }}，{{ capLabel(report.spec.default_capital) }}资金，扣除手续费、印花税和冲击成本</span>
        </div>
        <div class="mf-verdict-nums">
          <div><span>年化收益</span><b :class="$fmt.dir(verdict.s.cagr)">{{ fmt.ratio(verdict.s.cagr, 1, true) }}</b></div>
          <div><span>同池随机<qw-help :text="HELP.random"/></span><b>{{ fmt.ratio(verdict.s.bench_cagr, 1, true) }}</b></div>
          <div><span>每年超额</span><b :class="$fmt.dir(verdict.s.excess_ann)">{{ fmt.ratio(verdict.s.excess_ann, 1, true) }}</b></div>
          <div><span>t 值<qw-help :text="HELP.t"/></span><b :class="tCls(verdict.s.excess_t)">{{ fmt.t(verdict.s.excess_t) }}</b></div>
          <div><span>跑赢随机组合</span><b>{{ isNum(verdict.s.random_pct) ? fmt.ratio(verdict.s.random_pct, 0) : '—' }}</b></div>
          <div><span>最大回撤</span><b class="down">{{ fmt.ratio(verdict.s.maxdd, 1) }}</b><small>随机 {{ fmt.ratio(verdict.s.bench_maxdd, 1) }}</small></div>
          <div><span>比中证1000</span><b :class="$fmt.dir(verdict.s.vs_sh000852)">{{ fmt.ratio(verdict.s.vs_sh000852, 1, true) }}/年</b></div>
        </div>
        <div class="mf-verdict-risk"><qw-icon name="alert" :size="14"/><span>不是保证：历史上 2024 年基本持平；组合偏中小市值、低换手、低波动的股票，小盘股集体大跌时（如 2024 年初）也会一起跌；
          市场整体下跌时组合也会亏钱。资金越大优势越小，上亿资金基本没有优势（见下方容量表）。
          另外，这是在几种方法里挑出的最好的一种（见“为什么用 LightGBM”），实际表现大概率比回测差一些——以“前向跟踪”的真实记录为准。</span></div>
      </div>

      <div class="mf-goal">
        <div class="mf-goal-hd"><qw-icon name="target" :size="16"/><b>这个功能的目标</b>
          <span>每周换一次仓、同时拿 50 只股票，长期（按年算）比“随便买”的平均多赚<template v-if="verdict">约 <b class="up">{{ fmt.ratio(verdict.s.excess_ann, 0) }}/年</b></template>。
          它不是挑“明天就涨”的股票：单只有涨有跌，靠一篮子整体取胜，所以不追涨、单只不设止损。</span></div>
        <div class="st-flow mf-flow">
          <div class="st-step"><i>1</i><div><b>每周最后一个交易日晚上</b><span>程序自动打分（开着就行），下面“本周选股”给出这一期的 50 只</span></div></div>
          <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
          <div class="st-step"><i>2</i><div><b>下一个交易日开盘</b><span>按右边“下单清单”先卖掉掉出组合的、再买新进组合的（100 股一手）</span></div></div>
          <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
          <div class="st-step"><i>3</i><div><b>拿一周，下周重复</b><span>周中不换股；调仓日晚上<a href="#/trade?tab=nightly">明日计划</a>会提醒你要买卖哪些</span></div></div>
          <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
          <div class="st-step"><i>✓</i><div><b>先用模拟盘验证</b><span>在<a href="#/strategy?tpl=mf_weekly">策略中心</a>开“模拟跟踪”，程序每周自动在模拟账户里照做，不花钱</span></div></div>
        </div>
      </div>

      <div class="mf-bar">
        <div class="row" style="gap:8px">
          <span v-if="today" class="muted">打分日期 <b>{{ today.date }}</b>（{{ today.generated_at }} 生成）·
            {{ today.rebalance_day ? '今天是本周最后一个交易日：下面的组合就是 ' + today.next_trade_day + ' 开盘要调仓的组合' : '不是调仓日：组合按上周的持仓推算，只供参考，正式调仓在本周最后一个交易日收盘后' }}</span>
          <span v-else-if="!loading" class="muted">还没有打分结果</span>
        </div>
        <div class="row" style="gap:8px">
          <button class="btn sm" :disabled="!!job" @click="runJob('/api/mf/run', '量化选股：给最新一天打分')"><qw-icon name="refresh" :size="14"/>重新打分</button>
          <button class="btn sm ghost" :disabled="!!job" @click="runJob('/api/mf/rebuild', '量化选股：完整回测（约10分钟）')" v-tip="'重新做一遍 2020 年以来的滚动训练和样本外回测（约 10 分钟）'"><qw-icon name="target" :size="14"/>重新回测</button>
        </div>
      </div>
      <qw-job v-if="job" :job-id="job" :title="jobTitle" @done="onJobDone" @failed="job = ''"/>

      <qw-empty v-if="err" icon="alert" title="读取失败" :desc="err"/>
      <qw-empty v-else-if="!loading && !today" icon="target" title="还没有量化选股结果" desc="点“重新打分”，大约 1 分钟：用最新日线给全部主板股票打分，并给出本周调仓组合。">
        <button class="btn primary" :disabled="!!job" @click="runJob('/api/mf/run', '量化选股：给最新一天打分')">现在打分</button>
      </qw-empty>

      <div v-if="today" class="mf-top">
        <qw-card :pad="false" class="mf-list" title="本周选股" icon="target" :help="HELP.method"
          :sub="'多因子 + LightGBM，持有前 ' + (data.config ? data.config.top_n : 50) + ' 名'">
          <div class="mf-tabs"><qw-tabs v-model="tab" :items="[{value:'target',label:'调仓组合',badge:targetRows.length},{value:'all',label:'全部排名',badge:allRows.length},{value:'sell',label:'要卖出',badge:sellRows.length}]"/>
            <span class="muted mf-tabs-note">可买股票 {{ today.universe_n }} 只<template v-if="today.capacity_n < today.universe_n">，按你的资金（{{ capLabel(today.capital) }}）流动性够的 {{ today.capacity_n }} 只</template> · <span v-tip="HELP.keep">持有规则</span></span></div>
          <qw-table v-if="tab !== 'sell'" :rows="tab === 'target' ? targetRows : allRows" row-key="code" dense clickable max-height="560px" :page-size="300"
            @row-click="(r) => $go('/watch/' + r.code)" empty-text="没有股票"
            :columns="[{key:'rank',label:'排名',align:'right',sortable:true,width:'58px'},{key:'name',label:'股票',minWidth:'120px'},{key:'industry',label:'行业',minWidth:'76px'},{key:'close',label:'收盘',align:'right'},{key:'chg',label:'当日',align:'right',sortable:true},{key:'amount20',label:'日均成交',align:'right',sortable:true},{key:'float_cap',label:'流通市值',align:'right',sortable:true},{key:'groups',label:'因子画像（越长越符合）',minWidth:'250px'},{key:'flag',label:'',align:'right'}]">
            <template #cell-rank="{row}"><span class="num">{{ row.rank || '—' }}</span></template>
            <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
            <template #cell-industry="{row}"><span class="muted">{{ $fmt.industry(row.industry) }}</span></template>
            <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span></template>
            <template #cell-chg="{row}"><qw-price :value="row.chg" ratio/></template>
            <template #cell-amount20="{row}"><span class="num">{{ $fmt.money(row.amount20) }}</span></template>
            <template #cell-float_cap="{row}"><span class="num">{{ $fmt.money(row.float_cap) }}</span></template>
            <template #cell-groups="{row}"><div class="mf-gbars"><span v-for="g in GROUP_KEYS" :key="g" v-tip="GROUPS[g] + '：在可买股票里排 ' + groupBar(row.groups && row.groups[g]) + '% 分位'"><i :style="{height: groupBar(row.groups && row.groups[g]) + '%'}"></i><em>{{ GROUPS[g].slice(0, 2) }}</em></span></div></template>
            <template #cell-flag="{row}"><span v-if="tab === 'target' && row.isNew" class="qw-tag red">新买入</span><span v-else-if="tab === 'target'" class="qw-tag gray">继续持有</span><span v-if="row.limit_up" class="qw-tag warn" v-tip="'今天收盘涨停：下一个交易日很可能开盘就涨停买不进，买不进就跳过，换下一只'">涨停</span></template>
          </qw-table>
          <div v-else class="mf-sell">
            <qw-empty v-if="!sellRows.length" compact icon="check" title="没有要卖出的股票" :desc="today.prev_target && today.prev_target.length ? '上一期的持仓排名都还在前 150 名以内。' : '这是第一期组合（还没有上一期）。'"/>
            <div v-else class="mf-sell-list"><a v-for="s in sellRows" :key="s.code" class="chip" :href="'#/watch/' + s.code">{{ s.name || s.code }} <span class="muted">{{ s.rank ? '现排 ' + s.rank : '掉出前 300' }}</span></a></div>
          </div>
        </qw-card>

        <qw-card title="下单清单" icon="wallet" :sub="plan ? '按 ' + capLabel(plan.capital) + ' 等权分配，100 股一手' : ''">
          <div class="mf-cap"><label>资金（元）</label><input v-model.number="capital" type="number" min="10000" step="10000" class="qw-input"></div>
          <template v-if="plan">
            <div class="mf-plan-sum">
              <div><span>每只约</span><b>{{ $fmt.money(plan.per_stock) }}</b></div>
              <div><span>实际用掉</span><b>{{ $fmt.money(plan.used) }}</b></div>
              <div><span>剩余现金</span><b>{{ $fmt.money(plan.cash_left) }}</b></div>
            </div>
            <div v-for="n in plan.notes" :key="n" class="gd-note warn" style="margin-bottom:8px"><qw-icon name="alert" :size="14"/><span>{{ n }}</span></div>
            <div class="mf-plan-list">
              <table class="mini-table">
                <thead><tr><th>股票</th><th>操作</th><th class="num">股数</th><th class="num">金额</th></tr></thead>
                <tbody>
                  <tr v-for="it in plan.items" :key="it.code" :class="{muted: it.too_small}">
                    <td><a :href="'#/watch/' + it.code">{{ it.name || it.code }}</a></td>
                    <td><span :class="it.action === '买入' ? 'up' : 'muted'">{{ it.action }}</span></td>
                    <td class="num">{{ it.shares }}</td>
                    <td class="num">{{ $fmt.money(it.amount) }}</td>
                  </tr>
                  <tr v-for="s in plan.sells" :key="'s' + s.code"><td><a :href="'#/watch/' + s.code">{{ s.name || s.code }}</a></td><td class="down">全部卖出</td><td></td><td></td></tr>
                </tbody>
              </table>
            </div>
          </template>
          <div class="mf-howto">
            <b>怎么执行</b>
            <ol>
              <li>每周最后一个交易日晚上看这里的组合；下一个交易日开盘（集合竞价）按清单买卖。</li>
              <li>开盘就涨停买不进的跳过，改买排名下一只；开盘跌停卖不出的，等能卖的那天再卖。</li>
              <li>不在周中追加或换股；组合整体跟随市场涨跌，单只股票不设止损（回测就是这样做的）。</li>
              <li>不确定就先<a href="#/strategy?tpl=mf_weekly">开模拟跟踪</a>：程序在模拟账户里每周自动照做，跑一段时间看看再用真钱。</li>
            </ol>
          </div>
        </qw-card>
      </div>

      <qw-card v-if="today" class="mf-stage" title="选出来的股票为什么多是“洗盘 / 吸筹 / 下跌”，没有能直接跟进的“拉升”？" icon="info">
        <div class="mf-stage-grid">
          <div class="mf-stage-text">
            <p>这是故意的，也正是它赚钱的地方。量化选股专门挑<b>最近跌过、冷门（换手低）、波动小、没被炒过</b>的股票——模型最看重的因子是“近 60 日涨停次数”，<b>越少越好</b>。
              这类股票在诊断页的“主力阶段”里自然多半显示为洗盘、吸筹、下跌或不明确。</p>
            <p v-if="stageNote">而看起来“能直接跟进”的<b>拉升</b>股，历史上是下一周表现<b>最差</b>的一组：样本外 {{ stageNote.weeks }} 周里，
              全部可买股票中被判为拉升的，下一周平均比同池<b class="down">{{ wk(stageNote.markup) }}</b>（t 值 {{ $fmt.t(stageNote.markupT) }}），追进去平均是亏的。</p>
            <p v-else>而看起来“能直接跟进”的拉升股，历史上下一周平均跑输同池（A 股短期追涨的普遍规律；连板、趋势类方案的回测也都跑输随机）。</p>
            <p v-if="stageNote">组合里的股票，不管显示哪个阶段，整体都跑赢同池（每周平均 <b class="up">{{ wk(stageNote.all) }}</b>）。如果只留“不是洗盘 / 吸筹 / 下跌”的，
              每周是 {{ wk(stageNote.drop) }}——几乎一样，但组合只剩约 {{ $fmt.ratio(stageNote.dropShare, 0) }} 的股票，更集中、更靠运气。</p>
            <p class="mf-stage-do"><qw-icon name="checkCircle" :size="14"/><span><b>怎么做：</b>照整个组合买，不要再按主力阶段挑一遍；也不要因为某只“看起来弱”就不买、看到“拉升”就追。主力阶段只是描述走势形态，不是买卖信号。</span></p>
          </div>
          <div v-if="stageRows.length" class="mf-scroll">
            <table class="mini-table">
              <thead><tr><th>主力阶段<br><small class="muted">选股当天</small></th><th class="num">组合里<br>占比</th><th class="num">组合里<br><small class="muted">下周比同池</small></th><th class="num">全部可买股票<br><small class="muted">下周比同池</small></th></tr></thead>
              <tbody>
                <tr v-for="r in stageRows" :key="r.key" :class="{hl: r.key === 'markup'}">
                  <td><b>{{ r.label }}</b></td>
                  <td class="num">{{ r.held ? $fmt.ratio(r.held.share, 0) : '—' }}</td>
                  <td class="num" :class="$fmt.dir(r.held && r.held.excess_wk)">{{ r.held ? wk(r.held.excess_wk) : '—' }}<small v-if="r.held && r.held.n < 500" class="muted">（样本少）</small></td>
                  <td class="num" :class="$fmt.dir(r.pool && r.pool.excess_wk)">{{ r.pool ? wk(r.pool.excess_wk) : '—' }}<small v-if="r.pool" class="muted"> t {{ $fmt.t(r.pool.t) }}</small></td>
                </tr>
              </tbody>
            </table>
            <div class="muted mf-foot">样本外 {{ report.oos_start }} 起每周一期；“比同池”= 下一期（次日开盘买、下期次日开盘卖）收益减去同一天全部可买股票的平均，未扣费。
              主力阶段来自选股器的逐日历史表。</div>
          </div>
        </div>
      </qw-card>

      <template v-if="report">
        <div class="mf-row2">
          <qw-card title="样本外净值" icon="trend" :sub="'2022 年起，' + capLabel(report.spec.default_capital) + '资金，起点 = 1'">
            <qw-chart v-if="navOption" :option="navOption" height="360px"/>
          </qw-card>
          <qw-card title="按资金规模的容量" icon="scale" :help="HELP.capacity" :pad="false">
            <div class="mf-scroll">
              <table class="mini-table mf-cap-table">
                <thead><tr><th>资金</th><th class="num">年化</th><th class="num">同池随机</th><th class="num">每年超额</th><th class="num">t 值</th><th class="num">最大回撤</th><th class="num">可买股票</th></tr></thead>
                <tbody>
                  <tr v-for="c in capRows" :key="c.capital" :class="{hl: c.capital === capSel}" @click="capSel = c.capital" style="cursor:pointer">
                    <td><b>{{ capLabel(c.capital) }}</b></td>
                    <td class="num" :class="$fmt.dir(c.segments['全部样本外'].cagr)">{{ fmt.ratio(c.segments['全部样本外'].cagr, 1, true) }}</td>
                    <td class="num">{{ fmt.ratio(c.segments['全部样本外'].bench_cagr, 1, true) }}</td>
                    <td class="num" :class="$fmt.dir(c.segments['全部样本外'].excess_ann)"><b>{{ fmt.ratio(c.segments['全部样本外'].excess_ann, 1, true) }}</b></td>
                    <td class="num" :class="tCls(c.segments['全部样本外'].excess_t)">{{ fmt.t(c.segments['全部样本外'].excess_t) }}</td>
                    <td class="num down">{{ fmt.ratio(c.segments['全部样本外'].maxdd, 1) }}</td>
                    <td class="num muted">{{ c.universe_median }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="muted mf-foot">点一行，下面的逐年成绩切换到那个资金规模。可买股票 = 流动性够的股票数（中位数）。</div>
          </qw-card>
        </div>

        <div class="mf-row3">
          <qw-card :title="'逐年成绩（' + (capNow ? capLabel(capNow.capital) : '') + '）'" icon="calendar" :pad="false">
            <div class="mf-scroll">
              <table class="mini-table">
                <thead><tr><th>年份</th><th class="num">量化选股</th><th class="num">同池随机</th><th class="num">超额</th><th v-for="(lab, code) in report.indexes" :key="code" class="num">{{ lab }}</th></tr></thead>
                <tbody>
                  <tr v-for="y in (capNow ? capNow.yearly : [])" :key="y.year">
                    <td><b>{{ y.year }}</b><small v-if="y.weeks < 50" class="muted"> ({{ y.weeks }}周)</small></td>
                    <td class="num" :class="$fmt.dir(y.strategy)"><b>{{ fmt.ratio(y.strategy, 1, true) }}</b></td>
                    <td class="num" :class="$fmt.dir(y.random)">{{ fmt.ratio(y.random, 1, true) }}</td>
                    <td class="num" :class="$fmt.dir(y.strategy - y.random)">{{ fmt.ratio(y.strategy - y.random, 1, true) }}</td>
                    <td v-for="(lab, code) in report.indexes" :key="code" class="num" :class="$fmt.dir(y[code])">{{ fmt.ratio(y[code], 1, true) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="capNow" class="mf-segs">
              <div v-for="(s, name) in capNow.segments" :key="name"><span>{{ name }}</span><b :class="$fmt.dir(s.excess_ann)">超额 {{ fmt.ratio(s.excess_ann, 1, true) }}/年</b><small :class="tCls(s.excess_t)">t {{ fmt.t(s.excess_t) }}</small></div>
            </div>
          </qw-card>
          <qw-card title="随机对照" icon="grid" :help="HELP.random">
            <qw-chart v-if="histOption" :option="histOption" height="220px"/>
            <p class="muted mf-small">灰柱 = 60 个“随便挑 50 只、同样换手、同样扣费”的组合的年化收益分布；红柱 = 量化选股所在的位置。</p>
          </qw-card>
          <qw-card title="为什么用 LightGBM" icon="flask" :pad="false">
            <div class="mf-scroll">
              <table class="mini-table">
                <thead><tr><th>打分方法</th><th class="num">IC</th><th class="num">每年超额</th><th class="num">t 值</th><th class="num">周换手</th></tr></thead>
                <tbody>
                  <tr v-for="m in report.methods" :key="m.key" :class="{hl: m.key === 'LGB'}">
                    <td>{{ m.label }}</td>
                    <td class="num">{{ fmt.num(m.ic, 3) }}</td>
                    <td class="num" :class="$fmt.dir(m.excess_ann)"><b>{{ fmt.ratio(m.excess_ann, 1, true) }}</b></td>
                    <td class="num" :class="tCls(m.excess_t)">{{ fmt.t(m.excess_t) }}</td>
                    <td class="num muted">{{ fmt.ratio(m.turnover, 0) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="muted mf-foot">同样的因子、同样的规则和费用。把因子简单加权（前两种）排序能力不差，但选出来的前 50 名扣完换手成本后跑不赢随机；非线性的 LightGBM 能避开“看起来便宜但会继续跌”的股票，所以采用它。</div>
          </qw-card>
        </div>

        <div class="mf-row4">
          <qw-card title="因子表" icon="sigma" :sub="'每周 RankIC：2020-2024 / 2025 至今；重要性 = 最近一次训练里模型用它的程度'" :pad="false">
            <qw-table :rows="factorRows" row-key="key" dense max-height="460px" :page-size="100"
              :columns="[{key:'label',label:'因子',minWidth:'130px'},{key:'group_label',label:'类别'},{key:'direction',label:'方向'},{key:'ic_dev',label:'IC 2020-24',align:'right',sortable:true},{key:'t_dev',label:'t',align:'right',sortable:true},{key:'ic_hold',label:'IC 2025-',align:'right',sortable:true},{key:'t_hold',label:'t',align:'right',sortable:true},{key:'importance',label:'重要性',align:'right',sortable:true}]">
              <template #cell-label="{row}"><span v-tip="row.desc">{{ row.label }}</span></template>
              <template #cell-group_label="{row}"><span class="muted">{{ row.group_label }}</span></template>
              <template #cell-direction="{row}"><span class="muted">{{ row.direction > 0 ? '越大越好' : '越小越好' }}</span></template>
              <template #cell-ic_dev="{row}"><span class="num" :class="$fmt.dir(row.ic_dev)">{{ fmt.num(row.ic_dev, 3) }}</span></template>
              <template #cell-t_dev="{row}"><span class="num" :class="tCls(row.t_dev)">{{ fmt.t(row.t_dev) }}</span></template>
              <template #cell-ic_hold="{row}"><span class="num" :class="$fmt.dir(row.ic_hold)">{{ fmt.num(row.ic_hold, 3) }}</span></template>
              <template #cell-t_hold="{row}"><span class="num" :class="tCls(row.t_hold)">{{ fmt.t(row.t_hold) }}</span></template>
              <template #cell-importance="{row}"><span class="num">{{ fmt.ratio(row.importance, 1) }}</span></template>
            </qw-table>
          </qw-card>
          <div class="mf-col">
            <qw-card title="组合画像" icon="users">
              <div v-if="report.traits" class="mf-traits">
                <div><span>市值分位</span><b>{{ fmt.ratio(report.traits.size, 0) }}</b><small>50% = 和可买股票平均一样；越低越偏小盘</small></div>
                <div><span>换手分位</span><b>{{ fmt.ratio(report.traits.turn, 0) }}</b><small>偏低换手（冷门）</small></div>
                <div><span>波动分位</span><b>{{ fmt.ratio(report.traits.vol, 0) }}</b><small>偏低波动</small></div>
                <div><span>持仓流通市值中位数</span><b>{{ $fmt.money(report.traits.cap) }}</b><small>可买股票中位数 {{ $fmt.money(report.traits.univ_cap) }}</small></div>
              </div>
            </qw-card>
            <qw-card title="前向跟踪（真实时间记录）" icon="clipboard" :sub="track && track.started ? '从 ' + track.started + ' 开始' : ''">
              <p class="muted mf-small">每周最后一个交易日收盘后，程序把当时的组合记下来、以后不再改动；这里的成绩是事后按真实行情算的，和回测无关，是最诚实的检验。需要程序在调仓日收盘后运行（开着就会自动做）。</p>
              <div v-if="trackSum" class="mf-plan-sum"><div><span>已完成 {{ trackSum.n }} 周</span><b :class="$fmt.dir(trackSum.s)">{{ fmt.ratio(trackSum.s, 1, true) }}</b></div><div><span>同期同池随机</span><b>{{ fmt.ratio(trackSum.b, 1, true) }}</b></div></div>
              <div v-if="trackRows.length" class="mf-scroll" style="max-height:220px">
                <table class="mini-table"><thead><tr><th>记录日</th><th class="num">组合</th><th class="num">同池随机</th></tr></thead>
                  <tbody><tr v-for="t in trackRows" :key="t.date"><td>{{ t.date }}<small v-if="!t.complete" class="muted">（进行中）</small></td><td class="num" :class="$fmt.dir(t.ret)">{{ fmt.ratio(t.ret, 2, true) }}</td><td class="num">{{ fmt.ratio(t.bench, 2, true) }}</td></tr></tbody></table>
              </div>
              <qw-empty v-else-if="track && track.records" compact icon="clipboard" :title="'已记录 ' + track.records + ' 期组合'" desc="第一期要等下一个交易日开盘买入、再过一周才有成绩。"/>
              <qw-empty v-else compact icon="clipboard" title="还没有记录" desc="第一个调仓日收盘后开始记录。"/>
            </qw-card>
          </div>
        </div>
        <p class="muted mf-small">回测口径：{{ report.data_start }} ~ {{ report.data_end }} 主板日线（含已退市股票）；财报按公告日之后才使用；ST 用 baostock 逐日历史状态（{{ report.st_source }}）；
          可买股票 = 主板、非 ST、上市满一年、近 20 日日均成交额 ≥ 2000 万、股价 ≥ 2 元、不是“亏损且营收 < 3 亿”；次日开盘买卖，开盘涨停买不进、跌停卖不出；
          佣金万 2.5、印花税（2023-08-28 前 0.1%，之后 0.05%）、冲击成本按资金规模估算。指数为价格指数（不含分红）。生成于 {{ report.generated_at }}。</p>
      </template>
      <qw-card v-else-if="!loading" title="还没有回测报告" icon="target">
        <p class="muted">点“重新回测”（约 10 分钟），会从 2020 年起滚动训练、给出样本外成绩、容量表和因子表。</p>
      </qw-card>
    </div>`,
  });
})();
