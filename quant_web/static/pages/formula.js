/* 公式库 #/formula：内置公式（分组）+ 我的公式；语法检查、今天选出哪些股票、在个股上预览、历史验证（样本外 + 扣成本 + 和同日随机比） */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted, watch } = Vue;
  const { api, fmt, store, toast, jobs, isNum } = QW;

  const TRIED_KEY = "qw-formula-tried";
  const readTried = () => { try { return JSON.parse(localStorage.getItem(TRIED_KEY)) || {}; } catch (e) { return {}; } };
  const HOLD_LABEL = { 5: "持有 5 天", 10: "持有 10 天", 20: "持有 20 天" };
  const HONEST = "规则：信号出现后第二天开盘买（开盘就涨停算买不进），持有 N 个交易日收盘卖（跌停卖不掉顺延），收益含分红送股并扣除佣金、印花税、滑点；"
    + "“同日随便买”= 同一天同一范围里所有能买进的股票按同样规则的平均。只有比它强，才说明公式有用。";

  QW.page("formula", {
    props: ["params", "query"],
    setup(props) {
      const loading = ref(true);
      const err = ref("");
      const lib = ref({ groups: [], items: [], mine: [], reference: null });
      const group = ref("全部");
      const selId = ref((props.query && props.query.id) || "");
      const editing = reactive({ id: "", name: "", text: "", note: "" });
      const editMode = ref(false);
      const check = ref(null);
      const scan = ref(null);
      const scanning = ref(false);
      const valJob = ref("");
      const result = ref(null);
      const resultLoading = ref(false);
      const previewCode = ref("");
      const preview = ref(null);
      const previewChart = ref(null);
      const previewing = ref(false);
      const refOpen = ref(false);
      const tried = ref(readTried());

      const load = async () => {
        try {
          lib.value = await api.get("/api/formula/library", null, { silent: true });
          err.value = "";
          if (!selId.value && lib.value.items.length) selId.value = lib.value.items[0].id;
        } catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
      };
      onMounted(load);

      const all = computed(() => [...lib.value.items, ...lib.value.mine.map((m) => ({ ...m, mine: true }))]);
      const groups = computed(() => ["全部", ...lib.value.groups, "我的公式"].map((g) => ({ value: g, label: g,
        badge: g === "我的公式" ? lib.value.mine.length || "" : "" })));
      const shown = computed(() => all.value.filter((f) => group.value === "全部" ? !f.mine : group.value === "我的公式" ? f.mine : f.group === group.value));
      const sel = computed(() => all.value.find((f) => f.id === selId.value) || null);
      const text = computed(() => (editMode.value ? editing.text : sel.value ? sel.value.text : ""));

      const choose = (f) => {
        selId.value = f.id;
        editMode.value = false;
        check.value = null; scan.value = null; result.value = null; preview.value = null; previewChart.value = null; valJob.value = "";
        if (f.verdicts) loadResult(f.result_key);
      };
      watch(sel, (f) => { if (f && f.verdicts && !result.value) loadResult(f.result_key); }, { immediate: true });

      const startNew = () => {
        Object.assign(editing, { id: "", name: "", text: "XG:C>MA(C,20) AND V>MA(V,20)*1.5;", note: "" });
        editMode.value = true; check.value = null; scan.value = null; result.value = null;
      };
      const copyToMine = () => {
        const f = sel.value;
        if (!f) return;
        Object.assign(editing, { id: f.mine ? f.id : "", name: f.mine ? f.name : f.name + "（我的）", text: f.text, note: f.note || "" });
        editMode.value = true;
      };
      const doCheck = async () => {
        try { check.value = await api.post("/api/formula/check", { text: text.value }); } catch (e) { /* 已提示 */ }
      };
      const save = async () => {
        try {
          const item = await api.post("/api/formula/mine", { ...editing });
          toast.success("已保存到“我的公式”");
          await load();
          group.value = "我的公式";
          selId.value = item.id;
          editMode.value = false;
        } catch (e) { /* 已提示 */ }
      };
      const remove = async () => {
        const f = sel.value;
        if (!f || !f.mine || !confirm(`删除“${f.name}”？`)) return;
        try { await api.del("/api/formula/mine/" + f.id); toast.success("已删除"); selId.value = ""; await load(); } catch (e) { /* 已提示 */ }
      };
      const body = () => (editMode.value ? { text: editing.text } : sel.value && sel.value.mine ? { text: sel.value.text } : { fid: selId.value });

      const doScan = async () => {
        scanning.value = true;
        try { scan.value = await api.post("/api/formula/scan", body()); } catch (e) { /* 已提示 */ } finally { scanning.value = false; }
      };
      const loadResult = async (key) => {
        if (!key) return;
        resultLoading.value = true;
        try { result.value = await api.get("/api/formula/result/" + key, null, { silent: true }); } catch (e) { /* 还没验证过 */ } finally { resultLoading.value = false; }
      };
      const doValidate = async (force) => {
        const k = normKey(text.value);
        tried.value = { ...tried.value, [k]: (tried.value[k] || 0) + 1 };
        try { localStorage.setItem(TRIED_KEY, JSON.stringify(tried.value)); } catch (e) { /* 忽略 */ }
        try {
          const r = await api.post("/api/formula/validate", { ...body(), force: !!force });
          if (r.result) { result.value = r.result; toast.info("这个公式已经验证过，直接显示结果"); return; }
          valJob.value = r.job_id;
          jobs.track(r.job_id, { title: "公式历史验证", onDone: () => { loadResult(r.key); load(); } });
        } catch (e) { /* 已提示 */ }
      };
      const normKey = (t) => String(t || "").replace(/\s+/g, "").toUpperCase().slice(0, 200);
      const triedCount = computed(() => Object.keys(tried.value).length);

      const doPreview = async (it) => {
        const code = it && it.code ? it.code : previewCode.value;
        if (!/^\d{6}$/.test(code || "")) { toast.warn("先选一只股票"); return; }
        previewCode.value = code;
        previewing.value = true;
        try {
          const [p, chart] = await Promise.all([
            api.post("/api/formula/preview", { code, ...body() }),
            api.get(`/api/stock/${code}/chart`, { period: "day", count: 400, ind: "ma,vol", chips: false }, { silent: true }),
          ]);
          preview.value = p;
          previewChart.value = chart;
        } catch (e) { /* 已提示 */ } finally { previewing.value = false; }
      };
      // 把公式输出线按日期对齐到 K 线；数值和股价差不多的画在主图，其他画一个副图
      const previewInd = computed(() => {
        const p = preview.value;
        const ch = previewChart.value;
        if (!p || !ch) return null;
        const pos = new Map(p.dates.map((d, i) => [d, i]));
        const dates = ch.bars.map((b) => String(b.date).slice(0, 10));
        const closes = ch.bars.map((b) => b.close).filter(isNum).sort((a, b) => a - b);
        const mid = closes.length ? closes[Math.floor(closes.length / 2)] : 1;
        const main = []; const sub = [];
        Object.entries(p.series || {}).forEach(([name, arr]) => {
          const data = dates.map((d) => (pos.has(d) ? arr[pos.get(d)] : null));
          const vals = data.filter(isNum).sort((a, b) => a - b);
          const m = vals.length ? vals[Math.floor(vals.length / 2)] : null;
          const line = { key: name, label: name, style: "line", data };
          (m != null && m > mid * 0.5 && m < mid * 1.5 ? main : sub).push(line);
        });
        const ind = { ...(ch.indicators || {}) };
        if (main.length) ind.fmain = { name: "公式", pane: "main", lines: main };
        if (sub.length) ind.fsub = { name: "公式输出", pane: "sub", lines: sub, refs: [] };
        return ind;
      });
      const previewMarkers = computed(() => (preview.value ? preview.value.signals.map((d) => ({ date: d, text: "信", pos: "below" })) : []));

      const holds = computed(() => (result.value ? Object.keys(result.value.holds).sort((a, b) => a - b) : []));
      const pctCls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
      const yearOption = (h) => {
        const r = result.value && result.value.holds[h];
        if (!r || !r.years.length) return null;
        const c = QW.colors();
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 48, right: 10, top: 10, bottom: 22 },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", formatter: (ps) => { const y = r.years[ps[0].dataIndex]; return `${y.year} 年：${y.n} 笔<br>平均 ${fmt.ratio(y.mean, 2, true)}，比同日随便买 ${fmt.ratio(y.excess, 2, true)}`; } },
          xAxis: { type: "category", data: r.years.map((y) => String(y.year)), ...QW.axisBase(c) },
          yAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => (v * 100).toFixed(1) + "%" } },
          series: [{ type: "bar", barMaxWidth: 26, data: r.years.map((y) => ({ value: y.excess, itemStyle: { color: y.excess >= 0 ? c.up : c.down } })) }],
        };
      };

      return {
        store, fmt, loading, err, lib, group, groups, shown, sel, selId, choose, editing, editMode, startNew, copyToMine, text,
        check, doCheck, save, remove, scan, scanning, doScan, valJob, doValidate, result, resultLoading, holds, pctCls, yearOption,
        previewCode, preview, previewChart, previewing, doPreview, previewInd, previewMarkers, refOpen, triedCount, HOLD_LABEL, HONEST, isNum,
        term: QW.term, BOARDS: QW.BOARDS,
      };
    },
    template: `<div class="fm-layout">
      <qw-card class="fm-list" :pad="false">
        <div class="fm-list-hd">
          <qw-segmented v-model="group" :options="groups" size="sm"/>
          <button class="btn sm primary" @click="startNew"><qw-icon name="plus" :size="14"/>写新公式</button>
        </div>
        <qw-skeleton v-if="loading" :rows="8" style="padding:12px"/>
        <qw-empty v-else-if="err" icon="alert" title="公式库读取不到" :desc="err"/>
        <div v-else class="fm-items">
          <button v-for="f in shown" :key="f.id" class="fm-item" :class="{on: f.id === selId && !editMode}" @click="choose(f)">
            <div class="fm-item-hd"><b>{{ f.name }}</b>
              <span class="qw-tag" :class="f.kind === 'warn' ? 'warn' : 'blue'">{{ f.kind === 'warn' ? '风险预警' : '选股' }}</span>
              <span v-if="f.uses_chips" class="qw-tag gray" v-tip="'用到筹码分布，计算较慢'">筹码</span>
            </div>
            <div class="muted fm-item-desc">{{ f.explain || f.note || '（我写的公式）' }}</div>
            <div v-if="f.verdicts" class="fm-item-v"><span class="qw-tag" :class="f.verdicts['10'] && f.verdicts['10'].credible ? 'red' : 'gray'">{{ f.verdicts['10'] && f.verdicts['10'].credible ? '验证：有一定优势' : '验证：没有明显优势' }}</span></div>
          </button>
          <qw-empty v-if="!shown.length" compact icon="sigma" :title="group === '我的公式' ? '还没有自己的公式' : '没有公式'" :desc="group === '我的公式' ? '点右上角“写新公式”，或者在内置公式上点“复制为我的公式”再修改。' : ''"/>
        </div>
      </qw-card>

      <div class="fm-right">
      <div class="stack">
        <qw-card v-if="editMode" :title="editing.id ? '修改我的公式' : '写新公式'" icon="edit">
          <div class="form-grid" style="margin-bottom:10px">
            <label><span>名字</span><input v-model="editing.name" class="qw-input" maxlength="40" placeholder="比如：我的放量突破"></label>
            <label><span>备注（可不填）</span><input v-model="editing.note" class="qw-input" maxlength="300" placeholder="为什么这样写"></label>
          </div>
          <textarea v-model="editing.text" class="qw-input fm-code" rows="7" spellcheck="false"></textarea>
          <div class="row" style="margin-top:8px">
            <button class="btn" @click="doCheck"><qw-icon name="check" :size="14"/>语法检查</button>
            <button class="btn primary" @click="save"><qw-icon name="save" :size="14"/>保存到我的公式</button>
            <button class="btn ghost" @click="editMode = false">取消</button>
            <button class="linkbtn" @click="refOpen = true">写法说明</button>
          </div>
        </qw-card>

        <qw-card v-else-if="sel" :title="sel.name" icon="sigma" :sub="sel.mine ? '我的公式' : sel.group">
          <template #extra>
            <button class="btn sm" @click="copyToMine"><qw-icon name="edit" :size="14"/>{{ sel.mine ? '修改' : '复制为我的公式' }}</button>
            <button v-if="sel.mine" class="btn sm ghost" @click="remove"><qw-icon name="trash" :size="14"/>删除</button>
          </template>
          <div v-if="sel.explain" class="fm-explain">
            <p><b>是什么：</b>{{ sel.explain }}</p>
            <p v-if="sel.usage"><b>怎么用：</b>{{ sel.usage }}</p>
            <p v-if="sel.trap" class="fm-trap"><qw-icon name="alert" :size="14"/><span><b>注意：</b>{{ sel.trap }}</span></p>
          </div>
          <pre class="fm-code">{{ sel.text }}</pre>
          <div class="row" style="margin-top:8px">
            <button class="btn" :disabled="scanning" @click="doScan"><qw-icon name="filter" :size="14"/>{{ scanning ? '正在选股…' : '今天选出哪些股票' }}</button>
            <button class="btn primary" @click="doValidate(false)"><qw-icon name="target" :size="14"/>历史验证</button>
            <button class="linkbtn" @click="refOpen = true">写法说明</button>
          </div>
        </qw-card>

        <qw-card v-if="check" :title="check.ok ? '语法正确' : '公式有问题'" :icon="check.ok ? 'checkCircle' : 'xCircle'">
          <p v-if="!check.ok" class="down" style="white-space:pre-wrap">{{ check.error }}</p>
          <div v-else class="muted">输出线：{{ check.outputs.join('、') || '（无）' }}；选股条件：{{ check.condition || '（无）' }}；大约需要 {{ check.lookback }} 天历史数据{{ check.uses_chips ? '；用到了筹码函数（计算较慢）' : '' }}</div>
        </qw-card>

      </div>
      <div class="stack">
        <qw-card v-if="scan" :title="'今天（' + scan.date + '）选出 ' + scan.total + ' 只'" icon="filter" :sub="'范围：' + scan.boards.map((b) => BOARDS[b] || b).join('、') + (scan.truncated ? '，只显示成交额最大的 500 只' : '')" :pad="false">
          <qw-table v-if="scan.rows.length" :rows="scan.rows" row-key="code" dense clickable :page-size="20" @row-click="(r) => doPreview(r)"
            :columns="[{key:'name',label:'名称'},{key:'industry',label:'行业'},{key:'close',label:'收盘',align:'right'},{key:'pct',label:'涨跌幅',align:'right',sortable:true},{key:'amount',label:'成交额',align:'right',sortable:true},{key:'turn',label:'换手',align:'right',sortable:true},{key:'act',label:'',align:'right'}]">
            <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
            <template #cell-industry="{row}"><span class="muted">{{ $fmt.industry(row.industry) }}</span></template>
            <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span></template>
            <template #cell-pct="{row}"><qw-price :value="row.pct" pct/></template>
            <template #cell-amount="{row}"><span class="num">{{ $fmt.money(row.amount) }}</span></template>
            <template #cell-turn="{row}"><span class="num">{{ isNum(row.turn) ? row.turn.toFixed(2) + '%' : '—' }}</span></template>
            <template #cell-act="{row}"><button class="btn sm ghost" @click.stop="doPreview(row)">看信号</button></template>
          </qw-table>
          <qw-empty v-else compact icon="filter" title="今天没有股票满足这个公式"/>
          <div class="muted" style="font-size:12px;padding:8px 16px">这只是“满足条件的股票”，不等于买入建议。先看上面的历史验证：没有优势的公式，选出来的股票并不比随便买更好。</div>
        </qw-card>

        <qw-card title="在个股上看信号" icon="candle" sub="K 线上的“信”字 = 公式成立的日子">
          <div class="row" style="margin-bottom:8px">
            <qw-stock-search :navigate="false" placeholder="输入股票，看这个公式在它身上什么时候出现" @select="doPreview"/>
            <span v-if="previewing" class="muted">计算中…</span>
            <span v-else-if="preview" class="muted">{{ previewCode }}：历史上出现 {{ preview.signals.length }} 次</span>
          </div>
          <qw-kline-pro v-if="previewChart && previewInd" :bars="previewChart.bars" :indicators="previewInd" :main="['ma', 'fmain']"
            :subs="['vol', 'fsub'].filter((k) => previewInd[k])" :markers="previewMarkers" :initial-bars="160" :main-height="260"/>
          <qw-empty v-else compact icon="candle" title="选一只股票试试" desc="可以从上面“今天选出”的列表里点“看信号”。"/>
        </qw-card>
      </div>
      <div class="fm-wide">
        <qw-job v-if="valJob" :job-id="valJob" title="公式历史验证"/>

        <qw-card v-if="result" title="历史验证结果" icon="target" :sub="'数据 ' + result.data_start + ' ~ ' + result.data_end + ' · 验证于 ' + (result.saved_at || '')">
          <template #extra><button class="btn sm ghost" @click="doValidate(true)">重新验证</button></template>
          <div class="gd-note"><qw-icon name="info" :size="16"/><span>{{ HONEST }}</span></div>
          <div v-if="result.data_end && store.dataDate && (new Date(store.dataDate) - new Date(result.data_end)) / 86400000 > 30" class="gd-note warn"><qw-icon name="alert" :size="16"/>
            <span>这份验证的数据只到 {{ result.data_end }}，已经过去一个多月：点“重新验证”用最新数据算一遍，结论可能会变。</span></div>
          <div v-if="triedCount >= 5" class="gd-note warn"><qw-icon name="alert" :size="16"/><span>你已经验证过 {{ triedCount }} 个不同的公式。试得越多，越容易碰巧找到一个“看起来很好”的——以“留出期”（最近一段、没参与挑选）的成绩为准。</span></div>
          <div class="fm-holds"><div v-for="h in holds" :key="h" class="fm-hold">
            <div class="fm-hold-hd"><b>{{ HOLD_LABEL[h] || ('持有 ' + h + ' 天') }}</b>
              <span class="qw-tag" :class="result.holds[h].verdict.credible ? 'red' : result.holds[h].verdict.key === 'bad' ? 'green' : 'gray'">{{ result.holds[h].verdict.credible ? '有一定可信度' : '还不够可信' }}</span>
              <span class="muted">共 {{ result.holds[h].signals }} 次信号，{{ result.holds[h].unfilled }} 次买不进，{{ result.holds[h].pending }} 次还没到卖出日</span>
            </div>
            <p class="fm-verdict">{{ result.holds[h].verdict.text }}</p>
            <div class="hs-box"><div style="overflow-x:auto" v-hscroll>
              <table class="mini-table fm-table">
                <thead><tr><th>时段</th><th>笔数</th><th>平均每笔</th><th>同日随便买</th><th>超额<qw-help :text="'平均每笔 − 同日随便买。正数才说明公式比随便买强。'"/></th><th>赚钱比例</th><th>t 值<qw-help :text="term('t值')"/></th></tr></thead>
                <tbody>
                  <tr v-for="seg in [['selection','选择期（2025-07 前）'],['holdout','留出期（2025-07 后，样本外）'],['all','全部']]" :key="seg[0]" :class="{hl: seg[0] === 'holdout'}">
                    <td>{{ seg[1] }}</td>
                    <template v-if="result.holds[h][seg[0]].n">
                      <td class="num">{{ result.holds[h][seg[0]].n }}</td>
                      <td class="num" :class="pctCls(result.holds[h][seg[0]].mean)">{{ fmt.ratio(result.holds[h][seg[0]].mean, 2, true) }}</td>
                      <td class="num">{{ fmt.ratio(result.holds[h][seg[0]].base, 2, true) }}</td>
                      <td class="num" :class="pctCls(result.holds[h][seg[0]].excess)"><b>{{ fmt.ratio(result.holds[h][seg[0]].excess, 2, true) }}</b></td>
                      <td class="num">{{ fmt.ratio(result.holds[h][seg[0]].win, 0) }}</td>
                      <td class="num" :class="isNum(result.holds[h][seg[0]].t) && result.holds[h][seg[0]].t >= 2 ? 'up' : ''">{{ isNum(result.holds[h][seg[0]].t) ? fmt.t(result.holds[h][seg[0]].t) : '—' }}</td>
                    </template>
                    <td v-else colspan="6" class="muted">没有信号</td>
                  </tr>
                </tbody>
              </table>
            </div></div>
            <qw-chart v-if="yearOption(h)" :option="yearOption(h)" height="150px"/>
          </div>
          </div>
        </qw-card>
        <qw-card v-else-if="resultLoading" title="历史验证结果"><qw-skeleton :rows="4"/></qw-card>
      </div>
      </div>

      <qw-drawer v-model="refOpen" title="公式写法说明" width="560px">
        <div v-if="lib.reference" class="fm-ref">
          <p class="muted">和通达信基本一样：每句用分号结尾；X:=… 是中间变量；XG:… 是选股条件；NAME:… 会画成一条线。AND/OR/NOT 表示并且/或者/不是。</p>
          <h4>数据</h4><ul><li v-for="(v, k) in lib.reference.data" :key="k"><code>{{ k }}</code> {{ v }}</li></ul>
          <h4>函数</h4><ul><li v-for="(v, k) in lib.reference.funcs" :key="k"><code>{{ k }}</code> {{ v }}</li></ul>
          <h4>指标引用</h4><ul><li v-for="(v, k) in lib.reference.indicators" :key="k"><code>{{ k }}</code>.{{ v.join(' / ') }}</li></ul>
          <h4 class="down">禁止使用的未来函数</h4><ul><li v-for="(v, k) in lib.reference.future" :key="k"><code>{{ k }}</code> {{ v }}</li></ul>
        </div>
      </qw-drawer>
    </div>`,
  });
})();
