/* 模型实验室 #/lab：下拉组合“股票范围 × 因子组 × 预测目标 × 模型 × 参数预设” → 训练前估算（内存/时间，超上限拦下）→ 后台滚动训练
   → 训练记录（样本外 IC / 分五组 / 前 K 名 vs 同日随机，选择期 / 留出期）→ 启用到选股器；单因子检验。训练只在点按钮后运行。 */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted, watch } = Vue;
  const { api, fmt, store, toast, jobs, isNum } = QW;

  const LS_FT = "qw.lab.factorTest";
  const VERDICT = { good: ["red", "有一定优势"], weak: ["warn", "还不够可信"], bad: ["green", "没有用"], short: ["gray", "留出期太短"] };
  const SEGS = [["selection", "选择期（2025-07 前）"], ["holdout", "留出期（样本外）"], ["all", "全部"]];
  const pct = (v, d = 2) => (isNum(v) ? fmt.ratio(v, d, true) : "—");
  const num = (v, d = 3) => (isNum(v) ? v.toFixed(d) : "—");
  const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
  const readLS = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const writeLS = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } };

  QW.page("lab", {
    props: ["params", "query"],
    setup(props) {
      const tab = ref((props.query && props.query.tab) || "train");
      const opts = ref(null);
      const err = ref("");
      const cfg = reactive({});
      const est = ref(null);
      const estErr = ref("");
      const estLoading = ref(false);
      const advanced = ref(false);
      const trainJob = ref("");
      const runs = ref([]);
      const enabledId = ref(null);
      const trials = ref({ count: 0, log: [] });
      const sel = ref(null);
      const detail = ref(null);
      const detLoading = ref(false);
      const years = computed(() => { const y = new Date().getFullYear(); const out = []; for (let i = 2019; i <= y - 1; i++) out.push(i); return out; });

      const load = async () => {
        try {
          opts.value = await api.get("/api/lab/options", null, { silent: true });
          Object.assign(cfg, JSON.parse(JSON.stringify(opts.value.defaults)));
          trials.value = opts.value.trials;
          enabledId.value = opts.value.enabled;
          err.value = "";
        } catch (e) { err.value = e.detail || e.message; }
      };
      const loadRuns = async () => {
        try {
          const r = await api.get("/api/lab/runs", null, { silent: true });
          runs.value = r.runs; enabledId.value = r.enabled; trials.value = r.trials;
          if (sel.value && !runs.value.some((x) => x.id === sel.value)) { sel.value = null; detail.value = null; }
        } catch (e) { /* 忽略 */ }
      };
      onMounted(async () => { await load(); loadRuns(); loadFt(); });

      // ---------------------------------------------------------- 配置与估算
      const modelInfo = computed(() => (opts.value ? opts.value.models.find((m) => m.key === cfg.model) : null) || {});
      const labelInfo = computed(() => (opts.value ? opts.value.labels.find((m) => m.key === cfg.label) : null) || {});
      const uniInfo = computed(() => (opts.value ? opts.value.universes.find((m) => m.key === cfg.universe) : null) || {});
      const presetInfo = computed(() => (opts.value ? opts.value.presets.find((m) => m.key === cfg.preset) : null) || {});
      const modelOk = (m) => m.available && m.tasks.includes(labelInfo.value.task || "reg");
      const toggleSet = (k) => {
        const i = cfg.factor_sets.indexOf(k);
        if (i >= 0) { if (cfg.factor_sets.length > 1) cfg.factor_sets.splice(i, 1); } else cfg.factor_sets.push(k);
      };
      const minAmtWan = computed({ get: () => Math.round((cfg.min_amount || 0) / 1e4), set: (v) => { cfg.min_amount = Number(v || 0) * 1e4; } });
      let estTimer = null;
      const estimate = async () => {
        if (!opts.value) return;
        estLoading.value = true;
        try { est.value = await api.post("/api/lab/estimate", { config: { ...cfg } }, { silent: true }); estErr.value = ""; }
        catch (e) { est.value = null; estErr.value = e.detail || e.message; } finally { estLoading.value = false; }
      };
      watch(cfg, () => { clearTimeout(estTimer); estTimer = setTimeout(estimate, 400); }, { deep: true });
      watch(() => cfg.label, () => { if (opts.value && !modelOk(modelInfo.value)) { const m = opts.value.models.find(modelOk); if (m) cfg.model = m.key; } });

      const startTrain = async () => {
        if (!est.value || est.value.blocked) return;
        const msg = `开始训练：${est.value.describe}\n\n预计约 ${est.value.minutes} 分钟、最多用约 ${est.value.mem_gb} GB 内存（粗略估计）。\n训练在后台运行，期间电脑会比较忙；可以关掉这个页面。\n\n确定开始？`;
        if (!confirm(msg)) return;
        try {
          const r = await api.post("/api/lab/train", { config: { ...cfg } });
          trainJob.value = r.job_id;
          jobs.track(r.job_id, { title: "模型训练", onDone: async () => { await loadRuns(); tab.value = "runs"; if (runs.value.length) openRun(runs.value[0].id); } });
          toast.success("已开始训练（后台运行）");
        } catch (e) { /* 已提示 */ }
      };

      // ---------------------------------------------------------- 训练记录
      const openRun = async (id) => {
        sel.value = id; detLoading.value = true;
        try { detail.value = await api.get("/api/lab/runs/" + id, null, { silent: true }); } catch (e) { toast.error(e.detail || e.message); } finally { detLoading.value = false; }
      };
      const enableRun = async (r) => {
        const credible = r.verdict && r.verdict.credible;
        if (!credible && !confirm("这个模型在留出期（样本外）没有显著优势。启用后，选股器的“模型打分”会用它排序，但它的排序可能和随便挑差不多。\n\n仍然启用？")) return;
        try { await api.post(`/api/lab/runs/${r.id}/enable`); toast.success("已启用：选股器里选“模型打分”就会用它"); await loadRuns(); if (detail.value) openRun(r.id); } catch (e) { /* 已提示 */ }
      };
      const disable = async () => { try { await api.post("/api/lab/disable"); toast.success("已停用"); await loadRuns(); } catch (e) { /* 已提示 */ } };
      const removeRun = async (r) => {
        if (!confirm(`删除这条训练记录？\n${r.describe}`)) return;
        try { await api.del("/api/lab/runs/" + r.id); toast.success("已删除"); if (sel.value === r.id) { sel.value = null; detail.value = null; } await loadRuns(); } catch (e) { /* 已提示 */ }
      };
      const scoreJob = ref("");
      const rescore = async () => {
        try { const r = await api.post("/api/lab/score", { force: true }); scoreJob.value = r.job_id; jobs.track(r.job_id, { title: "模型打分" }); } catch (e) { /* 已提示 */ }
      };

      const seg = (k) => (detail.value && detail.value.evaluation.segments[k]) || {};
      const chartBase = (c) => ({ animation: false, textStyle: { fontFamily: c.font }, grid: { left: 52, right: 14, top: 30, bottom: 24 } });
      const hoLine = (c, dates) => {
        const ho = detail.value.evaluation.holdout_start;
        const idx = dates.findIndex((d) => d >= ho);
        return idx > 0 ? { symbol: "none", silent: true, label: { formatter: "留出期开始", color: c.text3, fontSize: 10 }, lineStyle: { color: c.text3, type: "dashed" }, data: [{ xAxis: dates[idx] }] } : undefined;
      };
      const icOption = computed(() => {
        const d = detail.value;
        if (!d || !d.evaluation.curves.ic_dates.length) return null;
        const c = QW.colors();
        const cv = d.evaluation.curves;
        return {
          ...chartBase(c), tooltip: { ...QW.tooltipBase(c), trigger: "axis" },
          xAxis: { type: "category", data: cv.ic_dates, ...QW.axisBase(c) }, yAxis: { type: "value", ...QW.axisBase(c) },
          series: [{ name: "累计 RankIC", type: "line", showSymbol: false, data: cv.cum_rank_ic, lineStyle: { width: 2, color: c.primary }, itemStyle: { color: c.primary }, markLine: hoLine(c, cv.ic_dates) }],
        };
      });
      const topkOption = computed(() => {
        const d = detail.value;
        if (!d || !d.evaluation.curves.topk_dates.length) return null;
        const c = QW.colors();
        const cv = d.evaluation.curves;
        return {
          ...chartBase(c), legend: { top: 0, data: [`前 ${d.evaluation.top_k} 名`, "同日随机"], textStyle: { color: c.text2 } },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => v + "%" },
          xAxis: { type: "category", data: cv.topk_dates, ...QW.axisBase(c) },
          yAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          series: [
            { name: `前 ${d.evaluation.top_k} 名`, type: "line", showSymbol: false, data: cv.topk_port.map((v) => +(v * 100).toFixed(2)), lineStyle: { width: 2, color: c.primary }, itemStyle: { color: c.primary }, markLine: hoLine(c, cv.topk_dates) },
            { name: "同日随机", type: "line", showSymbol: false, data: cv.topk_base.map((v) => +(v * 100).toFixed(2)), lineStyle: { width: 1.5, color: c.text3, type: "dashed" }, itemStyle: { color: c.text3 } },
          ],
        };
      });
      const quintOption = computed(() => {
        const d = detail.value;
        if (!d) return null;
        const a = seg("all").quintiles; const h = seg("holdout").quintiles;
        if (!a) return null;
        const c = QW.colors();
        const bar = (arr) => (arr || []).map((v) => +(v * 100).toFixed(3));
        return {
          ...chartBase(c), legend: { top: 0, data: ["全部", "留出期"], textStyle: { color: c.text2 } },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => v + "%" },
          xAxis: { type: "category", data: ["最低 20%", "第 2 组", "第 3 组", "第 4 组", "最高 20%"], ...QW.axisBase(c) },
          yAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          series: [
            { name: "全部", type: "bar", data: bar(a), itemStyle: { color: c.primary } },
            ...(h ? [{ name: "留出期", type: "bar", data: bar(h), itemStyle: { color: c.warn } }] : []),
          ],
        };
      });
      const impOption = computed(() => {
        const d = detail.value;
        if (!d || !d.importance) return null;
        const c = QW.colors();
        const top = d.importance.top.slice(0, 15).reverse();
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 150, right: 20, top: 8, bottom: 20 },
          tooltip: { ...QW.tooltipBase(c), valueFormatter: (v) => v + "%" },
          xAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          yAxis: { type: "category", data: top.map((x) => x.label), ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, width: 140, overflow: "truncate" } },
          series: [{ type: "bar", data: top.map((x) => +(x.value * 100).toFixed(2)), itemStyle: { color: c.primary } }],
        };
      });

      // ---------------------------------------------------------- 单因子检验
      const ft = reactive({ universe: "main", factor_sets: ["basic", "ta", "mainforce", "fundamental"], label: "excess_10", start_year: 2020 });
      const ftJob = ref("");
      const ftRes = ref(null);
      const ftFilter = ref("all");
      const ftAll = ref(false);
      const loadFt = async (key) => {
        const k = key || readLS(LS_FT);
        if (!k) return;
        try { ftRes.value = await api.get("/api/lab/factor_test/" + k, null, { silent: true }); writeLS(LS_FT, k); } catch (e) { /* 还没有 */ }
      };
      const toggleFt = (k) => { const i = ft.factor_sets.indexOf(k); if (i >= 0) { if (ft.factor_sets.length > 1) ft.factor_sets.splice(i, 1); } else ft.factor_sets.push(k); };
      const startFt = async () => {
        if (!confirm("单因子检验要读取全部历史数据、计算选中的因子（不训练模型），大约几分钟到二十分钟。确定开始？")) return;
        try {
          const r = await api.post("/api/lab/factor_test", { config: { ...ft } });
          ftJob.value = r.job_id;
          jobs.track(r.job_id, { title: "单因子检验", onDone: () => loadFt(r.key) });
        } catch (e) { /* 已提示 */ }
      };
      const ftRows = computed(() => {
        if (!ftRes.value) return [];
        const rows = ftRes.value.rows.filter((r) => ftFilter.value === "all" || r.set === ftFilter.value);
        return ftAll.value ? rows : rows.slice(0, 40);
      });
      const setLabel = (k) => ((opts.value && opts.value.factor_sets.find((s) => s.key === k)) || { label: k }).label;

      const tabs = computed(() => [
        { value: "train", label: "训练模型", icon: "flask" },
        { value: "runs", label: "训练记录", icon: "history", badge: runs.value.length || null },
        { value: "factor", label: "单因子检验", icon: "sigma" },
      ]);

      return {
        store, fmt, isNum, pct, num, cls, tab, tabs, opts, err, cfg, est, estErr, estLoading, advanced, years, modelInfo, labelInfo, uniInfo,
        presetInfo, modelOk, toggleSet, minAmtWan, startTrain, trainJob, runs, enabledId, trials, sel, detail, detLoading, openRun, enableRun,
        disable, removeRun, rescore, scoreJob, seg, icOption, topkOption, quintOption, impOption, ft, ftJob, ftRes, ftFilter, ftAll, toggleFt,
        startFt, ftRows, setLabel, VERDICT, SEGS,
      };
    },
    template: `<div class="stack">
      <div class="gd-note"><qw-icon name="info" :size="15"/><span>模型实验室：自己组合“股票范围 × 因子 × 预测目标 × 模型”训练选股模型。
        <b>开发和日常使用都不会自动训练</b>——只在你点“开始训练”后在后台运行。结果全部是样本外的：每一段只用之前的数据训练，扣成本，和同一天随便买比，
        <b>留出期（{{ opts ? opts.holdout_start : '2025-07-01' }} 之后）单独给结论</b>。你已经做了 <b>{{ trials.count }}</b> 次试验：试得越多，越容易碰巧试出好看的结果。</span></div>
      <qw-tabs v-model="tab" :items="tabs"/>
      <qw-empty v-if="err" icon="alert" title="实验室读取不到" :desc="err"/>
      <qw-skeleton v-else-if="!opts" :rows="8"/>

      <!-- ====================================================== 训练模型 -->
      <div v-else-if="tab === 'train'" class="lb-train">
        <qw-card title="一次训练" icon="flask">
          <div class="td-form">
            <label><span>股票范围</span>
              <select v-model="cfg.universe" class="qw-input"><option v-for="u in opts.universes" :key="u.key" :value="u.key">{{ u.label }}</option></select>
              <em class="hint">{{ uniInfo.desc }}</em></label>
            <label><span>预测目标</span>
              <select v-model="cfg.label" class="qw-input"><option v-for="l in opts.labels" :key="l.key" :value="l.key">{{ l.label }}</option></select>
              <em class="hint">{{ labelInfo.desc }}</em></label>
            <label class="td-wide"><span>因子组（可多选）<qw-help text="因子就是“描述一只股票某方面特点的数字”，例如最近涨了多少、换手率、市盈率。模型从这些数字里找和之后涨跌有关的规律。因子越多越慢、越占内存，也越容易学到噪声。"/></span>
              <div class="lb-sets">
                <button v-for="s in opts.factor_sets" :key="s.key" type="button" class="lb-set" :class="{on: cfg.factor_sets.includes(s.key)}" :disabled="!s.available" @click="toggleSet(s.key)">
                  <b>{{ s.label }}</b><span class="muted">{{ s.available ? s.n + ' 个' : '不可用' }}</span><em>{{ s.available ? s.desc : s.reason }}</em></button>
              </div></label>
            <label><span>模型</span>
              <select v-model="cfg.model" class="qw-input"><option v-for="m in opts.models" :key="m.key" :value="m.key" :disabled="!modelOk(m)">{{ m.label }}{{ !m.available ? '（未安装）' : !modelOk(m) ? '（不能配这个目标）' : '' }}</option></select>
              <em class="hint">{{ modelInfo.available ? modelInfo.desc : modelInfo.reason }}<template v-if="modelInfo.sampled && modelInfo.sampled[cfg.preset]">；每期最多抽样 {{ (modelInfo.sampled[cfg.preset] / 1e4).toFixed(0) }} 万行训练</template></em></label>
            <label><span>参数预设</span>
              <qw-segmented v-model="cfg.preset" :options="opts.presets.map((p) => ({value: p.key, label: p.label}))"/>
              <em class="hint">{{ presetInfo.desc }}</em></label>
            <label><span>从哪一年开始</span>
              <select v-model.number="cfg.start_year" class="qw-input"><option v-for="y in years" :key="y" :value="y">{{ y }} 年</option></select>
              <em class="hint">前两年只用来训练，之后才开始出样本外成绩</em></label>
            <label><span>每期买前几名（评估用）</span><input v-model.number="cfg.top_k" type="number" min="1" max="100" class="qw-input">
              <em class="hint">每隔“持有天数”调仓一次，看分数最高的几只和同一天随便买比</em></label>
          </div>
          <button class="btn sm ghost" style="margin-top:10px" @click="advanced = !advanced">{{ advanced ? '收起' : '更多设置' }}</button>
          <div v-if="advanced" class="td-form" style="margin-top:10px">
            <label><span>因子标准化</span>
              <select v-model="cfg.normalize" class="qw-input"><option v-for="n in opts.normalize" :key="n.key" :value="n.key">{{ n.label }}</option></select></label>
            <label><span>20 日平均成交额下限（万元）</span><input v-model.number="minAmtWan" type="number" min="0" step="500" class="qw-input"></label>
            <label class="td-wide"><span>备注</span><input v-model="cfg.note" maxlength="100" class="qw-input" placeholder="例如：去掉 Alpha101 看看"></label>
          </div>
        </qw-card>

        <div class="stack">
          <qw-card title="训练前估算" icon="gauge" :sub="estLoading ? '计算中…' : ''">
            <p v-if="estErr" class="down">{{ estErr }}</p>
            <template v-else-if="est">
              <p class="text-2" style="margin:0 0 10px">{{ est.describe }}</p>
              <div class="g2">
                <qw-stat label="样本" :value="(est.rows / 1e4).toFixed(0)" unit="万行" :sub="est.stocks + ' 只股票 · ' + est.features + ' 个因子'" flat/>
                <qw-stat label="滚动训练" :value="est.folds" unit="段" :sub="'每段只用之前的数据'" flat/>
                <qw-stat label="内存" :value="est.mem_gb" unit="GB" :tone="est.mem_gb > est.mem_cap_gb ? 'down' : ''" :sub="'上限 ' + est.mem_cap_gb + ' GB' + (est.mem_avail_gb != null ? '，现在可用 ' + est.mem_avail_gb + ' GB' : '')" flat/>
                <qw-stat label="大概时间" :value="est.minutes" unit="分钟" sub="粗略估计" flat/>
              </div>
              <div v-if="est.blocked" class="gd-note warn" style="margin-top:10px"><qw-icon name="alert" :size="15"/><span>{{ est.reason }}</span></div>
              <p v-if="est.warning" class="muted" style="font-size:12px">{{ est.warning }}</p>
              <p class="muted" style="font-size:12px">{{ est.note }}</p>
            </template>
            <qw-skeleton v-else :rows="3"/>
            <template #footer>
              <button class="btn primary" :disabled="!est || est.blocked" @click="startTrain"><qw-icon name="play" :size="14"/>开始训练{{ est && !est.blocked ? '（约 ' + est.minutes + ' 分钟）' : '' }}</button>
            </template>
          </qw-card>
          <qw-job v-if="trainJob" :job-id="trainJob" title="模型训练"/>
          <qw-card title="怎么看结果" icon="book">
            <ul class="td-steps" style="list-style:disc">
              <li><b>RankIC</b>：每天“模型分数”和“之后涨跌”的排名相关性。0.02~0.05 就算有用；接近 0 = 没用；t 值超过 2 才算比较可信。</li>
              <li><b>分五组</b>：按分数分成 5 组，好模型应该一组比一组赚得多。</li>
              <li><b>前 K 名 vs 同日随机</b>：每期买分数最高的几只，和同一天随便买比，扣成本后多赚多少。</li>
              <li><b>以留出期为准</b>：选择期的成绩可能被你（和我）反复试配置“调”出来；留出期更接近真实。</li>
              <li>复杂模型连“等权打分”都比不过，就说明学到的多半是噪声。</li>
            </ul>
          </qw-card>
        </div>
      </div>

      <!-- ====================================================== 训练记录 -->
      <template v-else-if="tab === 'runs'">
        <qw-card title="训练记录" icon="history" :pad="false" :sub="enabledId ? '选股器正在用：' + enabledId : '还没有启用的模型'">
          <template #extra>
            <button v-if="enabledId" class="btn sm" @click="rescore">给最新一天打分</button>
            <button v-if="enabledId" class="btn sm ghost" @click="disable">停用</button>
          </template>
          <qw-table v-if="runs.length" :rows="runs" row-key="id" dense clickable :active-key="sel" @row-click="(r) => openRun(r.id)"
            :columns="[{key:'created',label:'时间'},{key:'describe',label:'配置',minWidth:'240px'},{key:'verdict',label:'结论'},{key:'ho',label:'留出期 RankIC / 前K超额',align:'right'},{key:'sel',label:'选择期 RankIC / 前K超额',align:'right'},{key:'act',label:'',align:'right'}]">
            <template #cell-created="{row}"><span class="num" style="font-size:12px">{{ row.created }}</span><div v-if="row.enabled" class="qw-tag blue" style="margin-top:2px">选股器在用</div></template>
            <template #cell-describe="{row}"><span style="font-size:12.5px">{{ row.describe }}</span><div v-if="row.config && row.config.note" class="muted" style="font-size:12px">{{ row.config.note }}</div></template>
            <template #cell-verdict="{row}"><span v-if="row.verdict" class="qw-tag" :class="(VERDICT[row.verdict.key] || ['gray'])[0]">{{ (VERDICT[row.verdict.key] || [0, row.verdict.key])[1] }}</span></template>
            <template #cell-ho="{row}"><span class="num" :class="cls(row.holdout.rank_ic)">{{ num(row.holdout.rank_ic) }}</span> <span class="muted num">t {{ row.holdout.rank_ic_t ?? '—' }}</span>
              <div class="num"><span :class="cls(row.holdout.excess)">{{ pct(row.holdout.excess) }}</span> <span class="muted">t {{ row.holdout.t ?? '—' }}</span></div></template>
            <template #cell-sel="{row}"><span class="num" :class="cls(row.selection.rank_ic)">{{ num(row.selection.rank_ic) }}</span> <span class="muted num">t {{ row.selection.rank_ic_t ?? '—' }}</span>
              <div class="num"><span :class="cls(row.selection.excess)">{{ pct(row.selection.excess) }}</span> <span class="muted">t {{ row.selection.t ?? '—' }}</span></div></template>
            <template #cell-act="{row}"><span class="row" style="flex-wrap:nowrap;gap:4px">
              <button v-if="!row.enabled" class="btn sm" @click.stop="enableRun(row)">启用</button>
              <button class="qw-iconbtn sm" @click.stop="removeRun(row)" v-tip="'删除'"><qw-icon name="trash" :size="14"/></button></span></template>
          </qw-table>
          <qw-empty v-else icon="flask" title="还没有训练记录" desc="在“训练模型”里选好配置，点“开始训练”。" action-text="去训练" @action="tab = 'train'"/>
        </qw-card>
        <qw-job v-if="scoreJob" :job-id="scoreJob" title="模型打分"/>
        <qw-card v-if="detLoading && !detail" title="训练结果"><qw-skeleton :rows="6"/></qw-card>
        <template v-if="detail">
          <qw-card :title="'训练结果 · ' + detail.id" icon="bars" :sub="detail.describe">
            <template #extra>
              <button v-if="!detail.summary.enabled" class="btn sm primary" @click="enableRun(detail.summary)">启用到选股器</button>
              <span v-else class="qw-tag blue">选股器在用</span>
            </template>
            <div class="st-advice" :class="detail.evaluation.verdict.credible ? 't-good' : 't-neutral'"><qw-icon name="target" :size="15"/><span>{{ detail.evaluation.verdict.text }}</span></div>
            <div class="hs-box" style="margin-top:12px"><div style="overflow-x:auto" v-hscroll>
              <table class="mini-table fm-table">
                <thead><tr><th>时段</th><th>交易日</th><th>RankIC 平均</th><th>ICIR</th><th>t 值</th><th>IC&gt;0 的天数</th><th>前 {{ detail.evaluation.top_k }} 名每期超额</th><th>跑赢随机的期数</th><th>t 值</th><th>年化（模型 / 随机）</th><th>最大回撤（模型 / 随机）</th></tr></thead>
                <tbody><tr v-for="s in SEGS" :key="s[0]" :class="{hl: s[0] === 'holdout'}">
                  <td>{{ s[1] }}</td>
                  <template v-if="seg(s[0]).days">
                    <td class="num">{{ seg(s[0]).days }}</td>
                    <td class="num" :class="cls(seg(s[0]).rank_ic.mean)"><b>{{ num(seg(s[0]).rank_ic.mean) }}</b></td>
                    <td class="num">{{ num(seg(s[0]).rank_ic.ir, 2) }}</td><td class="num">{{ seg(s[0]).rank_ic.t ?? '—' }}</td>
                    <td class="num">{{ $fmt.ratio(seg(s[0]).rank_ic.pos, 0) }}</td>
                    <template v-if="seg(s[0]).topk">
                      <td class="num" :class="cls(seg(s[0]).topk.excess)"><b>{{ pct(seg(s[0]).topk.excess) }}</b></td><td class="num">{{ $fmt.ratio(seg(s[0]).topk.win, 0) }}</td>
                      <td class="num">{{ seg(s[0]).topk.t ?? '—' }}</td>
                      <td class="num">{{ pct(seg(s[0]).topk.cagr, 1) }} / {{ pct(seg(s[0]).topk.base_cagr, 1) }}</td>
                      <td class="num">{{ pct(seg(s[0]).topk.max_dd, 1) }} / {{ pct(seg(s[0]).topk.base_max_dd, 1) }}</td>
                    </template><td v-else colspan="5" class="muted">没有调仓期</td>
                  </template><td v-else colspan="10" class="muted">没有样本外预测</td>
                </tr></tbody>
              </table></div></div>
            <p class="muted" style="font-size:12px;margin-top:8px">规则：每天收盘后用“之前的数据训练的模型”打分 → 第二天开盘买（开盘涨停买不进）→ 持有 {{ detail.hold }} 个交易日收盘卖，扣佣金、印花税、滑点；
              “随机” = 同一天范围内所有买得进的股票的平均。t 值取普通 t 和 Newey-West t 中较小的，小于 2 表示还不够可信。
              <template v-if="detail.data.survivorship">注意：这个范围用的是现在的指数成分股回看历史，有幸存者偏差，结果偏乐观。</template></p>
          </qw-card>
          <div class="g2">
            <qw-card title="累计 RankIC" icon="trend" help="每天的 RankIC 累加起来。稳定向上 = 排序一直有用；走平或向下 = 没用了。">
              <qw-chart v-if="icOption" :option="icOption" height="240px"/><qw-empty v-else compact icon="trend" title="没有数据"/></qw-card>
            <qw-card :title="'前 ' + detail.evaluation.top_k + ' 名 vs 同日随机'" icon="bars" help="每期买分数最高的几只（不重叠），和同一天随便买的累计收益。组合按每期平均分配资金简化计算。">
              <qw-chart v-if="topkOption" :option="topkOption" height="240px"/><qw-empty v-else compact icon="bars" title="没有数据"/></qw-card>
            <qw-card title="分五组的超额收益" icon="layers" help="每天按分数从低到高分成 5 组，每组之后比同日平均多赚多少。好模型应该从左到右越来越高。">
              <qw-chart v-if="quintOption" :option="quintOption" height="240px"/><qw-empty v-else compact icon="layers" title="没有数据"/></qw-card>
            <qw-card title="因子重要性（前 15）" icon="sigma" :sub="detail.importance ? '' : '这个模型不提供'">
              <qw-chart v-if="impOption" :option="impOption" height="300px"/>
              <div v-if="detail.importance" class="lb-groups"><span v-for="g in detail.importance.groups" :key="g.key">{{ g.label }} <b>{{ $fmt.ratio(g.value, 0) }}</b></span></div>
              <qw-empty v-else compact icon="sigma" title="这个模型没有因子重要性"/></qw-card>
          </div>
          <qw-card :title="'最新打分（' + detail.latest.date + '，前 20）'" icon="listCheck" :pad="false" sub="只是模型排序，不是买入建议">
            <qw-table :rows="detail.latest.rows.slice(0, 20)" row-key="code" dense
              :columns="[{key:'rank',label:'#',width:'40px'},{key:'code',label:'股票'},{key:'score',label:'分数',align:'right'},{key:'why',label:'主要原因（LightGBM 才有）',minWidth:'260px'}]">
              <template #cell-rank="{row}"><span class="muted num">{{ row.rank }}</span></template>
              <template #cell-code="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
              <template #cell-score="{row}"><span class="num">{{ num(row.score, 4) }}</span></template>
              <template #cell-why="{row}"><span v-if="row.why" class="lb-why"><span v-for="w in row.why" :key="w.name" :class="w.value > 0 ? 'up' : 'down'">{{ w.label }}{{ w.value > 0 ? '↑' : '↓' }}</span></span><span v-else class="muted">—</span></template>
            </qw-table>
          </qw-card>
          <qw-card title="数据与滚动训练" icon="database">
            <p class="text-2" style="font-size:13px">{{ detail.data.start }} ~ {{ detail.data.end }} · {{ detail.data.stocks }} 只股票 · {{ detail.data.days }} 个交易日 · {{ (detail.data.rows / 1e4).toFixed(1) }} 万行 ·
              {{ detail.features.length }} 个因子 · 用时 {{ Math.round(detail.seconds / 60) }} 分钟{{ detail.data.dropped_empty && detail.data.dropped_empty.length ? ' · 去掉了全为空的因子 ' + detail.data.dropped_empty.length + ' 个' : '' }}</p>
            <div class="hs-box"><div style="overflow-x:auto" v-hscroll>
              <table class="mini-table"><thead><tr><th>训练截止</th><th>测试区间</th><th>训练行数</th><th>验证行数</th><th>抽样</th><th>最佳轮数</th></tr></thead>
                <tbody><tr v-for="(f, i) in detail.folds" :key="i"><td class="num">{{ f.train_end }}</td><td class="num">{{ f.test_start }} ~ {{ f.test_end }}</td>
                  <template v-if="!f.skipped"><td class="num">{{ f.train_rows }}</td><td class="num">{{ f.valid_rows }}</td><td class="num">{{ f.sampled || '—' }}</td><td class="num">{{ f.best_iter || '—' }}</td></template>
                  <td v-else colspan="4" class="muted">{{ f.skipped }}</td></tr></tbody></table>
            </div></div>
          </qw-card>
        </template>
      </template>

      <!-- ====================================================== 单因子检验 -->
      <template v-else-if="tab === 'factor'">
        <qw-card title="单因子检验（不训练模型）" icon="sigma" sub="每个因子本身和之后涨跌有没有关系">
          <div class="td-form">
            <label><span>股票范围</span><select v-model="ft.universe" class="qw-input"><option v-for="u in opts.universes" :key="u.key" :value="u.key">{{ u.label }}</option></select></label>
            <label><span>预测目标</span><select v-model="ft.label" class="qw-input"><option v-for="l in opts.labels.filter((x) => x.task === 'reg')" :key="l.key" :value="l.key">{{ l.label }}</option></select></label>
            <label><span>从哪一年开始</span><select v-model.number="ft.start_year" class="qw-input"><option v-for="y in years" :key="y" :value="y">{{ y }} 年</option></select></label>
            <label class="td-wide"><span>因子组</span><div class="row">
              <button v-for="s in opts.factor_sets" :key="s.key" type="button" class="chip" :class="{active: ft.factor_sets.includes(s.key)}" :disabled="!s.available" @click="toggleFt(s.key)">{{ s.label }}（{{ s.n }}）</button></div></label>
          </div>
          <template #footer><button class="btn primary" @click="startFt"><qw-icon name="play" :size="14"/>开始检验</button></template>
        </qw-card>
        <qw-job v-if="ftJob" :job-id="ftJob" title="单因子检验"/>
        <qw-card v-if="ftRes" :title="'检验结果（' + ftRes.rows.length + ' 个因子）'" icon="listCheck" :pad="false" :sub="ftRes.describe">
          <template #extra>
            <select v-model="ftFilter" class="qw-input" style="height:28px;width:auto"><option value="all">全部因子组</option>
              <option v-for="s in [...new Set(ftRes.rows.map((r) => r.set))]" :key="s" :value="s">{{ setLabel(s) }}</option></select>
          </template>
          <p class="muted" style="font-size:12px;padding:8px 16px 0">{{ ftRes.note }}</p>
          <qw-table :rows="ftRows" row-key="name" dense
            :columns="[{key:'label',label:'因子'},{key:'all',label:'全部 RankIC',align:'right'},{key:'selection',label:'选择期 RankIC（t）',align:'right'},{key:'holdout',label:'留出期 RankIC（t）',align:'right'},{key:'consistent',label:'方向一致'},{key:'coverage',label:'覆盖率',align:'right'},{key:'credible',label:''}]">
            <template #cell-label="{row}"><span>{{ row.label }}</span><div class="muted" style="font-size:11.5px">{{ setLabel(row.set) }} · {{ row.name }}</div></template>
            <template #cell-all="{row}"><span class="num" :class="cls(row.all.mean)">{{ num(row.all.mean) }}</span></template>
            <template #cell-selection="{row}"><span class="num" :class="cls(row.selection.mean)">{{ num(row.selection.mean) }}</span> <span class="muted num">（{{ row.selection.t ?? '—' }}）</span></template>
            <template #cell-holdout="{row}"><span class="num" :class="cls(row.holdout.mean)"><b>{{ num(row.holdout.mean) }}</b></span> <span class="muted num">（{{ row.holdout.t ?? '—' }}）</span></template>
            <template #cell-consistent="{row}"><span class="qw-tag" :class="row.consistent ? 'blue' : 'gray'">{{ row.consistent ? '一致' : '反了' }}</span></template>
            <template #cell-coverage="{row}"><span class="num">{{ $fmt.ratio(row.coverage, 0) }}</span></template>
            <template #cell-credible="{row}"><span v-if="row.credible" class="qw-tag red">比较可信</span></template>
          </qw-table>
          <div v-if="!ftAll && ftRes.rows.length > 40" style="padding:8px 16px"><button class="btn sm ghost" @click="ftAll = true">显示全部</button></div>
        </qw-card>
      </template>
    </div>`,
  });
})();
