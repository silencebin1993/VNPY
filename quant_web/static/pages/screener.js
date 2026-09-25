/* 选股器 #/screener：方案（内置 + 我的）→ 条件编辑 → 运行（排雷 + 打分 + 建议止损/股数）→ 历史回测（样本外 + 和同日随机比）
   结果区是一张卡片里的两个标签："选股结果"和"历史回测"，点哪个按钮就切到哪个标签。
   缓存：每个方案的结果、回测、当前标签按方案记住（切方案再切回来还在）；页面本身被 keep-alive 缓存（切页面再回来还在）；
   没改过条件的方案运行后后端会存下来，第一次打开时直接显示"上次运行的结果"。 */
(function () {
  "use strict";
  const { ref, computed, onMounted, nextTick, watch } = Vue;
  const { api, fmt, store, toast, jobs, isNum } = QW;

  const BOARD_OPTS = [["main", "沪深主板"], ["chinext", "创业板"], ["star", "科创板"], ["bj", "北交所"]];
  const STAGE_TONE = { accumulation: "gray", washout: "gray", markup: "warn", distribution: "warn", decline: "gray", unclear: "gray" };   // 阶段只是形态描述：只把历史上之后偏弱的"拉升 / 出货"标黄
  const RISK_TAG = { red: ["red", "红灯"], yellow: ["warn", "注意"], green: ["green", "未发现"] };
  const clone = (x) => JSON.parse(JSON.stringify(x));

  QW.page("screener", {
    props: ["params", "query"],
    setup(props) {
      const meta = ref(null);
      const loading = ref(true);
      const err = ref("");
      const selId = ref((props.query && props.query.id) || "reversal_value");
      const draft = ref(null);
      const snap = ref("");                    // 方案保存时的样子；draft 和它不一样 = 改过了
      const editOpen = ref(false);
      const running = ref(false);
      const result = ref(null);
      const resultSig = ref("");               // 这份选股结果是按什么条件算的
      const runErr = ref("");
      const bt = ref(null);
      const btSig = ref("");
      const btJob = ref("");
      const btLoading = ref(false);
      const regime = ref(null);
      const view = ref("result");              // 结果区当前标签：result / bt
      const outRef = ref(null);
      const memo = {};                         // 方案编号 → 它的结果、回测、标签（切方案再切回来还在）

      const load = async (keepDraft = false) => {
        try {
          meta.value = await api.get("/api/screener/schemes", null, { silent: true });
          err.value = "";
          if (!keepDraft || !draft.value) pick(selId.value);
        } catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
      };
      onMounted(async () => {
        await load();
        try { regime.value = await api.get("/api/market/regime", null, { silent: true }); } catch (e) { /* 没有大盘数据 */ }
      });
      QW.onReturn(() => load(true));           // 切回来：刷新方案列表上的回测标签，不动正在改的条件

      const schemes = computed(() => (meta.value ? meta.value.schemes : []));
      const current = computed(() => schemes.value.find((s) => s.id === selId.value) || null);
      const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== snap.value);
      // 结果 / 回测是按哪些条件算的：只看会影响选股的部分（改名字、另存为不算"条件改过了"）
      const sigOf = (d) => JSON.stringify({ universe: d.universe, conditions: d.conditions, match: d.match, risk: d.risk, scoring: d.scoring, top_n: d.top_n });
      const sigNow = computed(() => (draft.value ? sigOf(draft.value) : ""));
      const remember = () => {
        memo[selId.value] = { result: result.value, resultSig: resultSig.value, bt: bt.value, btSig: btSig.value, btJob: btJob.value, view: view.value };
      };
      const setMemo = (sid, patch) => { memo[sid] = { ...(memo[sid] || {}), ...patch }; };
      const pick = (id) => {
        const s = schemes.value.find((x) => x.id === id) || schemes.value[0];
        if (!s) return;
        if (draft.value) remember();            // 先记下当前方案的结果（另存为时 selId 已经换成新方案，结果跟着走）
        selId.value = s.id;
        const d = clone(s);
        d.universe = d.universe || {};
        d.conditions = (d.conditions || []).map((c) => (c.type === "stage" ? { ...c, include: c.include || [], exclude: c.exclude || [] } : c));
        draft.value = d;
        snap.value = JSON.stringify(d);
        runErr.value = "";
        const m = memo[s.id];
        result.value = m ? m.result : null;
        resultSig.value = m ? m.resultSig : sigOf(d);
        bt.value = m ? m.bt : null;
        btSig.value = m ? m.btSig : sigOf(d);
        btJob.value = m ? m.btJob || "" : "";
        view.value = m ? m.view : "result";
        if (!result.value && s.latest_date) loadLatest(s.id);
        if (!bt.value && !btJob.value && s.backtest && s.backtest.key) loadBt(s.backtest.key, s.id);
      };
      // 结果区在屏幕外时滚过去（点了按钮却"没反应"，多半是结果显示在看不到的地方）
      const reveal = () => nextTick(() => {
        const el = outRef.value && outRef.value.$el;
        if (!el) return;
        const r = el.getBoundingClientRect();
        if (r.top > window.innerHeight - 160 || r.top < 60) el.scrollIntoView({ behavior: "smooth", block: "start" });
      });

      // ---------------------------------------------------------- 条件编辑
      const fieldOpts = computed(() => (meta.value ? Object.entries(meta.value.fields).map(([k, v]) => ({ value: k, label: v.label, unit: v.unit, scale: v.scale })) : []));
      const fieldInfo = (k) => (meta.value && meta.value.fields[k]) || { label: k, unit: "", scale: 1 };
      const dispVal = (c, i) => {
        const sc = fieldInfo(c.field).scale || 1;
        const v = c.op === "between" ? (c.value || [0, 0])[i] : c.value;
        return isNum(v) ? +(v * sc).toFixed(4) : "";
      };
      const setVal = (c, i, raw) => {
        const sc = fieldInfo(c.field).scale || 1;
        const v = raw === "" ? null : Number(raw) / sc;
        if (c.op === "between") { const arr = Array.isArray(c.value) ? c.value.slice() : [0, 0]; arr[i] = v; c.value = arr; } else c.value = v;
      };
      const onOp = (c) => { if (c.op === "between" && !Array.isArray(c.value)) c.value = [c.value || 0, c.value || 0]; if (c.op !== "between" && Array.isArray(c.value)) c.value = c.value[0]; };
      const addCond = (type) => {
        const c = type === "field" ? { type, field: "rps120", op: ">=", value: 70 } : type === "formula" ? { type, fid: "ma_bull" } : { type, include: [], exclude: ["distribution", "decline"] };
        draft.value.conditions.push(c);
      };
      const delCond = (i) => draft.value.conditions.splice(i, 1);
      const toggleIn = (arr, v) => { const i = arr.indexOf(v); if (i >= 0) arr.splice(i, 1); else arr.push(v); };
      const boardsOf = computed(() => (draft.value && draft.value.universe.boards) || (meta.value && meta.value.profile_boards) || ["main"]);
      const toggleBoard = (b) => {
        const cur = boardsOf.value.slice();
        const i = cur.indexOf(b);
        if (i >= 0) { if (cur.length === 1) return; cur.splice(i, 1); } else cur.push(b);
        draft.value.universe.boards = cur;
      };
      const noPerm = computed(() => boardsOf.value.filter((b) => !((meta.value && meta.value.profile_boards) || ["main"]).includes(b)));
      const amtYi = computed({ get: () => +((draft.value.universe.min_amount || 0) / 1e8).toFixed(2), set: (v) => { draft.value.universe.min_amount = Number(v || 0) * 1e8; } });
      const scoringOpts = computed(() => (meta.value ? Object.entries(meta.value.scoring).map(([k, v]) => ({ value: k, label: v.name })) : []));
      const termOpts = computed(() => (meta.value ? Object.entries(meta.value.terms).map(([k, v]) => ({ key: k, label: v.label, dir: v.direction })) : []));
      const setWeight = (k, v) => { draft.value.scoring.weights = { ...(draft.value.scoring.weights || {}), [k]: Number(v) }; };

      // ---------------------------------------------------------- 运行 / 保存
      const run = async () => {
        const sid = selId.value;
        const sig = sigNow.value;
        running.value = true; runErr.value = ""; view.value = "result";
        try {
          // 没改过条件：按方案编号跑，后端会存下来（下次打开直接显示）；改过：按当前条件跑，只在这一页里记住
          const r = await api.post("/api/screener/run", dirty.value || !current.value ? { scheme: draft.value } : { scheme_id: sid });
          r._fresh = true;
          if (selId.value === sid) { result.value = r; resultSig.value = sig; reveal(); } else setMemo(sid, { result: r, resultSig: sig, view: "result" });
        } catch (e) { runErr.value = e.detail || e.message; } finally { running.value = false; }
      };
      const loadLatest = async (sid) => {
        try {
          const r = await api.get("/api/screener/latest/" + sid, null, { silent: true });
          if (selId.value === sid) { if (!result.value) { result.value = r; resultSig.value = sigNow.value; } } else if (!(memo[sid] && memo[sid].result)) setMemo(sid, { result: r, resultSig: "" });
        } catch (e) { /* 还没运行过 */ }
      };
      const save = async () => {
        const body = clone(draft.value);
        if (current.value && current.value.builtin) { delete body.id; body.name = body.name.replace(/（推荐）/, "") + "（我的）"; }
        try {
          const s = await api.post("/api/screener/schemes", { scheme: body });
          toast.success("已保存为我的方案");
          selId.value = s.id;
          await load();
        } catch (e) { /* 已提示 */ }
      };
      const remove = async () => {
        if (!current.value || current.value.builtin || !confirm(`删除方案“${current.value.name}”？`)) return;
        try { await api.del("/api/screener/schemes/" + current.value.id); toast.success("已删除"); delete memo[current.value.id]; draft.value = null; selId.value = "reversal_value"; await load(); } catch (e) { /* 已提示 */ }
      };
      const loadBt = async (key, sid, sig) => {
        btLoading.value = true;
        try {
          const r = await api.get("/api/screener/backtest/" + key, null, { silent: true });
          if (selId.value === sid) { bt.value = r; btSig.value = sig || sigNow.value; btJob.value = ""; } else setMemo(sid, { bt: r, btSig: sig || "", btJob: "" });
        } catch (e) { /* 还没有 */ } finally { btLoading.value = false; }
      };
      const backtest = async (force) => {
        const sid = selId.value;
        const sig = sigNow.value;
        view.value = "bt";
        reveal();
        try {
          const r = await api.post("/api/screener/backtest", { ...(dirty.value || !current.value ? { scheme: draft.value } : { scheme_id: sid }), force: !!force });
          if (r.result) {
            if (selId.value === sid) { bt.value = r.result; btSig.value = sig; }
            toast.info(`这个方案${r.result.saved_at ? "在 " + r.result.saved_at + " " : ""}回测过，结果在"历史回测"标签里；要用最新数据重算，点"重新回测"`);
            return;
          }
          if (selId.value === sid) btJob.value = r.job_id;
          jobs.track(r.job_id, { title: "选股方案回测", onDone: () => { loadBt(r.key, sid, sig); load(true); },
            onFail: () => { if (selId.value === sid) btJob.value = ""; else setMemo(sid, { btJob: "" }); } });
        } catch (e) { /* 已提示 */ }
      };
      const resultStale = computed(() => !!result.value && !!resultSig.value && resultSig.value !== sigNow.value);
      // 回测是按到哪天的数据算的：离现在一个多月就提醒重算（留出期变长，结论可能会变）
      const btAgeDays = computed(() => (bt.value && bt.value.data_end && store.dataDate
        ? Math.round((new Date(store.dataDate) - new Date(bt.value.data_end)) / 86400000) : 0));
      watch(() => props.query && props.query.id, (id) => { if (id && meta.value && id !== selId.value) pick(id); });
      const btStale = computed(() => !!bt.value && !!btSig.value && btSig.value !== sigNow.value);
      const viewTabs = computed(() => [
        { value: "result", label: "选股结果", icon: "listCheck", badge: result.value ? result.value.rows.length : null },
        { value: "bt", label: "历史回测", icon: "history", badge: btJob.value ? "回测中" : bt.value ? (bt.value.verdict.credible ? "有优势" : "没有明显优势") : null },
      ]);
      const addWatch = (r) => QW.watch.toggle(r.code, r.name).catch(() => {});
      // 去交易页下单：带上建议止损、股数和理由（下单前还会再做一遍风控检查）
      const tradeLink = (r) => "#/trade?" + new URLSearchParams({ code: r.code, name: r.name || "", side: "buy", stop: r.stop || "", qty: r.shares || "",
        reason: `选股器：${(draft.value && draft.value.name) || ""}` }).toString();

      const btOption = computed(() => {
        const b = bt.value;
        if (!b || !b.curve || !b.curve.length) return null;
        const c = QW.colors();
        let p = 1; let q = 1;
        const pc = []; const qc = [];
        b.curve.forEach((x) => { p *= 1 + x.port; q *= 1 + x.base; pc.push(+(p * 100 - 100).toFixed(2)); qc.push(+(q * 100 - 100).toFixed(2)); });
        const ho = b.holdout_start;
        const idx = b.curve.findIndex((x) => x.date >= ho);
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 52, right: 12, top: 30, bottom: 24 },
          legend: { top: 0, data: ["这个方案", "同日随机"], textStyle: { color: c.text2 } },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => v + "%" },
          xAxis: { type: "category", data: b.curve.map((x) => x.date), ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => String(v).slice(0, 7) } },
          yAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          series: [
            { name: "这个方案", type: "line", showSymbol: false, data: pc, lineStyle: { width: 2, color: c.primary }, itemStyle: { color: c.primary },
              markLine: idx > 0 ? { symbol: "none", silent: true, label: { formatter: "留出期开始", color: c.text3, fontSize: 10 }, lineStyle: { color: c.text3, type: "dashed" }, data: [{ xAxis: b.curve[idx].date }] } : undefined },
            { name: "同日随机", type: "line", showSymbol: false, data: qc, lineStyle: { width: 1.5, color: c.text3, type: "dashed" }, itemStyle: { color: c.text3 } },
          ],
        };
      });
      const pct = (v, d = 2) => (isNum(v) ? fmt.ratio(v, d, true) : "—");
      const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");

      return {
        store, fmt, meta, loading, err, schemes, selId, current, pick, draft, dirty, editOpen, fieldOpts, fieldInfo, dispVal, setVal, onOp,
        addCond, delCond, toggleIn, boardsOf, toggleBoard, noPerm, amtYi, scoringOpts, termOpts, setWeight, run, running, result, runErr,
        save, remove, bt, btJob, btLoading, backtest, btOption, pct, cls, addWatch, tradeLink, regime, BOARD_OPTS, STAGE_TONE, RISK_TAG, isNum,
        view, viewTabs, outRef, resultStale, btStale, btAgeDays,
      };
    },
    template: `<div class="fm-layout">
      <qw-card class="fm-list" :pad="false">
        <div class="fm-list-hd"><b>选股方案</b><span class="muted" style="font-size:12px">点一个方案，再点“运行选股”</span></div>
        <qw-skeleton v-if="loading" :rows="6" style="padding:12px"/>
        <qw-empty v-else-if="err" icon="alert" title="选股方案读取不到" :desc="err"/>
        <div v-else class="fm-items">
          <button v-for="s in schemes" :key="s.id" class="fm-item" :class="{on: s.id === selId}" @click="pick(s.id)">
            <div class="fm-item-hd"><b>{{ s.name }}</b><span v-if="!s.builtin" class="qw-tag blue">我的</span></div>
            <div class="muted fm-item-desc">{{ s.desc || '（我的方案）' }}</div>
            <div v-if="s.backtest" class="fm-item-v"><span class="qw-tag" :class="s.backtest.verdict.credible ? 'red' : 'gray'">{{ s.backtest.verdict.credible ? '回测：有一定优势' : '回测：没有明显优势' }}</span></div>
          </button>
        </div>
      </qw-card>

      <div class="stack">
        <div v-if="regime" class="sc-regime" :class="'t-' + regime.tone"><qw-icon name="gauge" :size="16"/>
          <span>大盘环境：<b>{{ regime.label }}</b>，仓位上限（你设置的经验规则，不是涨跌预测）<b>{{ $fmt.ratio(regime.cap, 0) }}</b>。{{ regime.advice }}</span></div>

        <qw-card v-if="draft" :title="draft.name" icon="filter" :sub="dirty ? '（已修改，未保存）' : ''">
          <template #extra>
            <button class="btn sm" @click="editOpen = !editOpen"><qw-icon name="sliders" :size="14"/>{{ editOpen ? '收起条件' : '修改条件' }}</button>
            <button class="btn sm" @click="save"><qw-icon name="save" :size="14"/>{{ current && !current.builtin ? '保存修改' : '另存为我的方案' }}</button>
            <button v-if="current && !current.builtin" class="btn sm ghost" @click="remove"><qw-icon name="trash" :size="14"/></button>
          </template>
          <p class="text-2" style="line-height:1.7">{{ draft.desc }}</p>
          <div v-if="editOpen" class="sc-editor">
            <div class="sc-sec"><b>名字</b><input v-model="draft.name" class="qw-input" maxlength="40" style="max-width:320px"></div>
            <div class="sc-sec"><b>范围</b>
              <div class="row">
                <button v-for="b in BOARD_OPTS" :key="b[0]" class="chip" :class="{active: boardsOf.includes(b[0])}" @click="toggleBoard(b[0])">{{ b[1] }}</button>
                <label class="sc-chk"><input type="checkbox" v-model="draft.universe.exclude_st">排除 ST</label>
                <label class="sc-inl">20 日均成交额 ≥ <input v-model.number="amtYi" type="number" step="0.1" class="qw-input sc-num"> 亿元</label>
                <label class="sc-inl">股价 <input v-model.number="draft.universe.price_min" type="number" class="qw-input sc-num"> ~ <input v-model.number="draft.universe.price_max" type="number" class="qw-input sc-num"> 元</label>
              </div>
              <div v-if="noPerm.length" class="gd-note warn"><qw-icon name="alert" :size="15"/><span>你在“我的情况”里没有勾选{{ noPerm.map((b) => BOARD_OPTS.find((x) => x[0] === b)[1]).join('、') }}的交易权限，选出来的这些股票可能买不了。</span></div>
            </div>
            <div class="sc-sec"><b>条件</b>
              <qw-segmented v-model="draft.match" :options="[{value:'all',label:'全部满足'},{value:'any',label:'满足任意一个'}]" size="sm"/>
              <div v-for="(c, i) in draft.conditions" :key="i" class="sc-cond">
                <template v-if="c.type === 'field'">
                  <select v-model="c.field" class="qw-input sc-sel"><option v-for="f in fieldOpts" :key="f.value" :value="f.value">{{ f.label }}</option></select>
                  <select v-model="c.op" class="qw-input sc-op" @change="onOp(c)"><option v-for="(v, k) in meta.ops" :key="k" :value="k">{{ v }}</option></select>
                  <input :value="dispVal(c, 0)" @change="setVal(c, 0, $event.target.value)" type="number" class="qw-input sc-num">
                  <template v-if="c.op === 'between'"> ~ <input :value="dispVal(c, 1)" @change="setVal(c, 1, $event.target.value)" type="number" class="qw-input sc-num"></template>
                  <span class="muted">{{ fieldInfo(c.field).unit }}</span>
                </template>
                <template v-else-if="c.type === 'formula'">
                  <span class="muted">公式</span>
                  <select v-if="!c.text" v-model="c.fid" class="qw-input sc-sel"><option v-for="f in meta.formulas" :key="f.id" :value="f.id">{{ f.group }} · {{ f.name }}</option></select>
                  <code v-else class="sc-code">{{ c.text }}</code>
                </template>
                <template v-else>
                  <span class="muted">主力阶段是</span>
                  <button v-for="(lab, k) in meta.stages" :key="'i' + k" class="chip sm" :class="{active: c.include.includes(k)}" @click="toggleIn(c.include, k)">{{ lab }}</button>
                  <span class="muted">排除</span>
                  <button v-for="(lab, k) in meta.stages" :key="'e' + k" class="chip sm" :class="{active: c.exclude.includes(k)}" @click="toggleIn(c.exclude, k)">{{ lab }}</button>
                </template>
                <button class="qw-iconbtn sm" @click="delCond(i)" aria-label="删除条件"><qw-icon name="x" :size="14"/></button>
              </div>
              <div class="row">
                <button class="btn sm ghost" @click="addCond('field')"><qw-icon name="plus" :size="13"/>数值条件</button>
                <button class="btn sm ghost" @click="addCond('formula')"><qw-icon name="plus" :size="13"/>公式条件</button>
                <button class="btn sm ghost" @click="addCond('stage')"><qw-icon name="plus" :size="13"/>主力阶段条件</button>
              </div>
            </div>
            <div class="sc-sec"><b>排雷</b>
              <label class="sc-chk"><input type="checkbox" v-model="draft.risk.exclude_red">去掉红灯（建议回避）</label>
              <label class="sc-chk"><input type="checkbox" v-model="draft.risk.exclude_yellow">也去掉黄灯（需要注意）</label>
            </div>
            <div class="sc-sec"><b>打分排序</b>
              <select v-model="draft.scoring.scheme" class="qw-input sc-sel"><option v-for="o in scoringOpts" :key="o.value" :value="o.value">{{ o.label }}</option></select>
              <span class="muted">{{ meta.scoring[draft.scoring.scheme] && meta.scoring[draft.scoring.scheme].desc }}（这是打分思路，不代表有效：看“历史回测”的留出期结果再判断）</span>
              <div v-if="draft.scoring.scheme === 'custom'" class="sc-weights">
                <label v-for="t in termOpts" :key="t.key"><span>{{ t.label }}<em class="muted">{{ t.dir > 0 ? '（越大越好）' : '（越小越好）' }}</em></span>
                  <input type="range" min="0" max="3" step="0.5" :value="(draft.scoring.weights || {})[t.key] || 0" @input="setWeight(t.key, $event.target.value)"><b class="num">{{ (draft.scoring.weights || {})[t.key] || 0 }}</b></label>
              </div>
            </div>
            <div class="sc-sec"><b>显示前</b><input v-model.number="draft.top_n" type="number" min="1" max="200" class="qw-input sc-num"> 只</div>
          </div>
          <template #footer>
            <div class="row">
              <button class="btn primary" :disabled="running" @click="run"><qw-icon name="play" :size="14"/>{{ running ? '正在选股（第一次约 10 秒）…' : '运行选股' }}</button>
              <button class="btn" @click="backtest(false)"><qw-icon name="history" :size="14"/>历史回测</button>
            </div>
          </template>
        </qw-card>

        <qw-card v-if="draft" ref="outRef" class="sc-out" :pad="false">
          <div class="mf-tabs"><qw-tabs v-model="view" :items="viewTabs"/>
            <span v-if="view === 'result' && result" class="muted mf-tabs-note">数据日期 {{ result.date }}<template v-if="!result._fresh"> · 上次运行的结果（点“运行选股”按最新数据重算）</template></span>
            <span v-else-if="view === 'bt' && bt" class="muted mf-tabs-note">回测于 {{ bt.saved_at || '—' }}</span></div>

          <div v-show="view === 'result'">
            <div v-if="runErr" class="gd-note warn" style="margin:8px 16px"><qw-icon name="alert" :size="15"/><span>选股出错：{{ runErr }}</span></div>
            <qw-skeleton v-if="running && !result" :rows="6" style="padding:12px 16px"/>
            <template v-else-if="result">
              <div v-if="resultStale" class="gd-note warn" style="margin:8px 16px 0"><qw-icon name="alert" :size="15"/><span>条件改过了：下面还是按修改前的条件选出来的，点“运行选股”更新。</span></div>
              <div class="sc-funnel">
                <span>符合范围 <b>{{ result.universe_n }}</b> 只</span><qw-icon name="chevronRight" :size="14"/>
                <span>满足条件 <b>{{ result.matched_n }}</b> 只</span><qw-icon name="chevronRight" :size="14"/>
                <span>排雷后 <b>{{ result.after_risk_n }}</b> 只</span><qw-icon name="chevronRight" :size="14"/>
                <span>显示前 <b>{{ result.rows.length }}</b> 只</span>
                <span class="muted">条件：{{ result.conditions_text.join('；') || '无' }}</span>
              </div>
              <qw-table v-if="result.rows.length" :rows="result.rows" row-key="code" dense :page-size="50" max-height="640px"
                :columns="[{key:'rank',label:'#',width:'36px'},{key:'name',label:'名称'},{key:'close',label:'收盘 / 涨跌',align:'right'},{key:'score',label:'打分',align:'right',sortable:true},{key:'stage',label:'主力阶段',help:'只是按量价描述走势处在什么形态，不是买卖信号：历史验证里按它选股没有跑赢随机'},{key:'risk',label:'排雷'},{key:'plan',label:'建议止损 / 股数',align:'right',help:'止损价按技术位置算（不超过默认止损比例）；股数按“一笔最多亏总资金的比例”算，按一手取整'},{key:'reasons',label:'理由',minWidth:'220px'},{key:'act',label:'',align:'right'}]">
                <template #cell-rank="{index}"><span class="muted num">{{ index + 1 }}</span></template>
                <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/><div class="muted" style="font-size:12px">{{ $fmt.industry(row.industry) }}</div></template>
                <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span><div><qw-price :value="isNum(row.pct) ? row.pct * 100 : null" pct/></div></template>
                <template #cell-score="{row}"><span class="num"><b>{{ row.score.toFixed(0) }}</b></span></template>
                <template #cell-stage="{row}"><span class="qw-tag" :class="STAGE_TONE[row.stage]">{{ row.stage_label }}</span></template>
                <template #cell-risk="{row}"><span class="qw-tag" :class="RISK_TAG[row.risk] ? RISK_TAG[row.risk][0] : 'gray'" v-tip="row.risk_reasons || '未发现明显风险'">{{ RISK_TAG[row.risk] ? RISK_TAG[row.risk][1] : '—' }}</span></template>
                <template #cell-plan="{row}"><div class="num" v-tip="row.stop_basis">止损 {{ $fmt.price(row.stop) }} <span class="muted">（-{{ (row.stop_pct * 100).toFixed(1) }}%）</span></div>
                  <div class="num" v-tip="row.size_note">{{ row.shares ? row.shares + ' 股 · ' + $fmt.money(row.amount) : '不买' }}</div></template>
                <template #cell-reasons="{row}"><span class="sc-reason">{{ row.reasons || '—' }}</span><div v-if="row.risk_reasons" class="muted" style="font-size:12px">注意：{{ row.risk_reasons }}</div></template>
                <template #cell-act="{row}"><span class="row" style="flex-wrap:nowrap;gap:4px"><a class="btn sm" :href="'#/watch/' + row.code">诊断</a>
                  <a v-if="row.shares" class="btn sm" :href="tradeLink(row)" v-tip="'去交易页：已填好建议止损和股数，下单前会再做一遍风控检查'">下单</a>
                  <button class="qw-iconbtn sm" @click="addWatch(row)" v-tip="'加入/移出自选'"><qw-icon :name="store.watchCodes.includes(row.code) ? 'starFill' : 'star'" :size="14"/></button></span></template>
              </qw-table>
              <qw-empty v-else compact icon="filter" title="今天没有股票满足这个方案" desc="可以放宽条件，或者今天就不买——没有合适的就空仓，也是一种操作。"/>
              <p class="muted" style="font-size:12px;padding:8px 16px">{{ result.note }}</p>
            </template>
            <qw-empty v-else compact icon="filter" title="还没有运行这个方案" desc="点上面的“运行选股”：按最新一天的收盘数据，从全部股票里筛出符合这个方案的股票（第一次约 10 秒）。"/>
          </div>

          <div v-show="view === 'bt'" class="sc-bt">
            <qw-job v-if="btJob" :job-id="btJob" title="选股方案回测"/>
            <qw-skeleton v-if="btLoading && !bt" :rows="4"/>
            <template v-else-if="bt">
              <div v-if="btAgeDays > 30" class="gd-note warn" style="margin-bottom:8px"><qw-icon name="alert" :size="15"/><span>这份回测的数据只到 {{ bt.data_end }}，已经过去 {{ btAgeDays }} 天：点“重新回测”用最新数据算一遍，结论可能会变。</span></div>
              <div v-if="btStale" class="gd-note warn" style="margin-bottom:8px"><qw-icon name="alert" :size="15"/><span>条件改过了：下面是修改前的方案的回测，点“历史回测”回测修改后的条件。</span></div>
              <div class="sc-bt-hd">
                <span class="muted">{{ bt.data_start }} ~ {{ bt.data_end }} · 每 {{ bt.rebalance }} 个交易日选一次，持有 {{ bt.hold }} 天</span>
                <button class="btn sm ghost" @click="backtest(true)"><qw-icon name="refresh" :size="13"/>重新回测</button>
              </div>
              <div class="st-advice" :class="bt.verdict.credible ? 't-good' : 't-neutral'"><qw-icon name="target" :size="15"/><span>{{ bt.verdict.text }}</span></div>
              <qw-chart v-if="btOption" :option="btOption" height="240px"/>
              <div class="hs-box"><div style="overflow-x:auto" v-hscroll>
                <table class="mini-table fm-table">
                  <thead><tr><th>时段</th><th>调仓次数</th><th>每期平均</th><th>同日随机</th><th>超额</th><th>跑赢随机的期数</th><th>t 值</th><th>年化（方案 / 随机）</th><th>最大回撤（方案 / 随机）</th></tr></thead>
                  <tbody><tr v-for="seg in [['selection','选择期（2025-07 前）'],['holdout','留出期（样本外）'],['all','全部']]" :key="seg[0]" :class="{hl: seg[0] === 'holdout'}">
                    <td>{{ seg[1] }}</td>
                    <template v-if="bt.stats[seg[0]].n">
                      <td class="num">{{ bt.stats[seg[0]].n }}</td><td class="num" :class="cls(bt.stats[seg[0]].period_mean)">{{ pct(bt.stats[seg[0]].period_mean) }}</td>
                      <td class="num">{{ pct(bt.stats[seg[0]].base_mean) }}</td><td class="num" :class="cls(bt.stats[seg[0]].excess)"><b>{{ pct(bt.stats[seg[0]].excess) }}</b></td>
                      <td class="num">{{ $fmt.ratio(bt.stats[seg[0]].win_periods, 0) }}</td><td class="num">{{ bt.stats[seg[0]].t ?? '—' }}</td>
                      <td class="num">{{ pct(bt.stats[seg[0]].cagr, 1) }} / {{ pct(bt.stats[seg[0]].base_cagr, 1) }}</td>
                      <td class="num">{{ pct(bt.stats[seg[0]].max_dd, 1) }} / {{ pct(bt.stats[seg[0]].base_max_dd, 1) }}</td>
                    </template><td v-else colspan="8" class="muted">没有选出股票</td>
                  </tr></tbody>
                </table>
              </div></div>
              <div class="sc-next">
                <b>回测完接下来做什么？</b>
                <span v-if="bt.verdict.credible">留出期（样本外）比同日随机好。可以<a v-if="current && current.strategy_template" :href="'#/strategy?tpl=' + current.strategy_template">去策略中心把它做成策略</a><template v-else>在策略中心加上买卖规则再回测一次</template>，先开“模拟跟踪”看一段时间真实表现，再考虑小仓位实盘。</span>
                <span v-else>留出期没有明显比随机好 = 按这个方案选股和随便挑差不多。<b>不建议照着它买</b>；它可以当作“缩小范围”的工具，买不买再看诊断。
                  真正在样本外显著跑赢随机的是<a href="#/mf">量化选股</a>（每周一次的 50 只组合）。</span>
              </div>
              <p class="muted" style="font-size:12px;margin-top:8px">规则：选股当天收盘后才知道结果 → 第二天开盘买（开盘涨停买不进），持有 {{ bt.hold }} 个交易日收盘卖，扣佣金、印花税、滑点；“同日随机”= 同一天所有符合范围的股票的平均。
                主力阶段逐日重算；解禁、质押、业绩预告等没有完整历史的排雷项目不参与回测。组合曲线按每期平均分配资金简化计算，只用来看趋势和回撤。</p>
            </template>
            <qw-empty v-else-if="!btJob" compact icon="history" title="这个方案还没有回测" desc="点上面的“历史回测”：用 2019 年以来的日线，按“选出来 → 第二天开盘买 → 持有几天卖”逐期算，和同一天随便挑的股票比（要几分钟，在后台跑，可以先去别的页面）。"/>
          </div>
        </qw-card>
      </div>
    </div>`,
  });
})();
