/* 策略中心 #/strategy：把"选出来的股票"变成每天能照做、能自动跟踪的交易规则。
   策略 = 选股来源（量化选股 / 选股器方案 / 实验室模型）+ 怎么买 + 怎么卖 + 买多少 + 大盘不好时少买。
   ① 诚实回测（真正的模拟盘规则逐日跑，和同样规则的随机选股比）→ ② 模拟跟踪（自动开一个模拟账户每天照做，在“交易”页看）
   → ③ 实盘建议（前几名进“交易 → 明日计划”的候选买入，买入仍要你确认）。
   "量化选股 每周调仓"规则固定，回测就是量化选股页的回测，只需要开模拟跟踪。
   缓存：同样模板 + 参数回测过就直接显示（后端按参数查）；每个模板 / 策略的回测、进行中的任务按选中项记住；页面被 keep-alive 缓存。 */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted, nextTick } = Vue;
  const { api, fmt, toast, jobs, isNum } = QW;

  const VERDICT = { good: ["red", "比随机有优势"], weak: ["warn", "还不够可信"], bad: ["green", "没有比随机好"], short: ["gray", "留出期太短"] };
  const PARAM_KEYS = ["entry", "trail", "target_r", "max_days", "max_positions", "exit_distribution", "regime"];
  const pct = (v, d = 1) => (isNum(v) ? fmt.ratio(v, d, true) : "—");
  const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");

  QW.page("strategy", {
    props: ["params", "query"],
    setup(props) {
      const home = ref(null);
      const err = ref("");
      const selTpl = ref((props.query && props.query.tpl) || "mf_weekly");
      const selItem = ref(null);
      const form = reactive({ name: "", params: {} });
      const bt = ref(null);
      const btJob = ref("");
      const btLoading = ref(false);
      const runJob = ref("");
      const segTab = ref("holdout");
      const detailRef = ref(null);
      const memo = {};                         // "tpl:模板" / "item:策略编号" → {bt, btJob}

      const load = async () => {
        try { home.value = await api.get("/api/strategy", null, { silent: true }); err.value = ""; } catch (e) { err.value = e.detail || e.message; }
      };
      onMounted(async () => {
        await load();
        const mine = props.query && props.query.item && home.value && home.value.items.find((x) => x.id === props.query.item);
        if (mine) pickItem(mine); else pickTemplate(selTpl.value);
      });
      QW.onReturn(load);                       // 切回来：刷新模拟账户收益、开关状态

      const tpl = computed(() => (home.value ? home.value.templates.find((t) => t.id === selTpl.value) : null));
      const item = computed(() => (home.value && selItem.value ? home.value.items.find((x) => x.id === selItem.value) : null));
      const isMf = computed(() => !!tpl.value && tpl.value.signal.type === "mf");
      const mfItem = computed(() => (home.value ? home.value.items.find((x) => x.is_mf) : null));
      const selKey = () => (selItem.value ? "item:" + selItem.value : "tpl:" + selTpl.value);
      const remember = () => { memo[selKey()] = { bt: bt.value, btJob: btJob.value }; };
      const restore = () => {
        const m = memo[selKey()];
        bt.value = m ? m.bt : null;
        btJob.value = m ? m.btJob : "";
        return !!m;
      };
      // 同样的模板 + 参数以前回测过就直接显示（只查缓存，不启动任务）
      const peek = async () => {
        // 旧版后端不认 peek（会直接开始回测）：没有 mf 字段 = 旧版，不查
        if (isMf.value || bt.value || btJob.value || !home.value || !("mf" in home.value)) return;
        const key = selKey();
        btLoading.value = true;
        try {
          const r = await api.post("/api/strategy/backtest", { template: selTpl.value, params: form.params, item_id: selItem.value, peek: true }, { silent: true });
          if (r.result && selKey() === key) bt.value = r.result;
        } catch (e) { /* 没有 */ } finally { btLoading.value = false; }
      };
      const pickTemplate = (id) => {
        if (home.value) remember();
        const t = home.value && home.value.templates.find((x) => x.id === id);
        if (!t) { if (home.value && home.value.templates.length && id !== home.value.templates[0].id) pickTemplate(home.value.templates[0].id); return; }
        selTpl.value = id; selItem.value = null;
        form.name = t.name; form.params = JSON.parse(JSON.stringify(t.defaults));
        if (!restore()) peek();
      };
      const pickItem = (x) => {
        remember();
        selTpl.value = x.template; selItem.value = x.id;
        form.name = x.name; form.params = JSON.parse(JSON.stringify(x.params));
        if (!restore()) peek();
        nextTick(() => { const el = detailRef.value && detailRef.value.$el; if (el && el.getBoundingClientRect().top > window.innerHeight - 120) el.scrollIntoView({ behavior: "smooth", block: "start" }); });
      };
      const saveItem = async (quiet) => {
        try {
          const r = await api.post("/api/strategy/items", { template: selTpl.value, params: form.params, name: form.name, item_id: selItem.value });
          if (!quiet) toast.success(selItem.value ? "已保存修改" : "已保存为我的策略");
          remember();
          const was = selKey();
          selItem.value = r.id;
          if (!memo[selKey()]) memo[selKey()] = memo[was];
          await load();
          return r;
        } catch (e) { return null; }
      };
      const runBacktest = async (force) => {
        const key = selKey();
        try {
          const r = await api.post("/api/strategy/backtest", { template: selTpl.value, params: form.params, item_id: selItem.value, force: !!force });
          if (r.result) { bt.value = r.result; toast.info("同样的参数回测过，直接显示结果；要用最新数据重算，点“重新回测”"); return; }
          btJob.value = r.job_id;
          jobs.track(r.job_id, {
            title: "策略回测",
            onDone: async (j) => {
              const k = j && j.result && j.result.key;
              let res = null;
              if (k) { try { res = await api.get("/api/strategy/backtest/" + k, null, { silent: true }); } catch (e) { /* 忽略 */ } }
              if (selKey() === key) { bt.value = res; btJob.value = ""; } else memo[key] = { bt: res, btJob: "" };
              load();
            },
            onFail: () => { if (selKey() === key) btJob.value = ""; else memo[key] = { ...(memo[key] || {}), btJob: "" }; },
          });
        } catch (e) { /* 已提示 */ }
      };
      const toggle = async (x, what, on, ev) => {
        const undo = () => { if (ev && ev.target) ev.target.checked = !on; return false; };   // 取消或失败：勾选框恢复原样
        if (what === "follow" && on && !confirm(x.is_mf
          ? "开启模拟跟踪：程序会新建一个模拟账户（起始资金取你的资金和 100 万里大的那个，保证 50 只每只都买得起一手），每个调仓日收盘后按量化选股的组合自动挂第二天开盘的单（模拟，不花真钱）。确定？"
          : "开启模拟跟踪：程序会新建一个模拟账户，每天收盘后按这个策略自动挂第二天的单（模拟，不花真钱）。确定？")) return undo();
        if (what === "live" && on && !confirm("开启实盘建议：每天收盘后，这个策略的前几名会出现在“交易 → 明日计划”的候选买入里。\n买入永远要你自己确认，程序不会替你买。确定？")) return undo();
        try { await api.post(`/api/strategy/items/${x.id}/${what}`, { enabled: on }); toast.success(on ? "已开启" : "已关闭"); await load(); return true; } catch (e) { await load(); return undo(); }
      };
      // 当前模板还没存成"我的策略"时，先存再开开关
      const toggleHere = async (what, on, ev) => {
        let x = item.value;
        if (!x) {
          const r = await saveItem(true);
          if (!r) { if (ev && ev.target) ev.target.checked = !on; return; }
          x = home.value.items.find((i) => i.id === r.id) || r;
        }
        await toggle(x, what, on, ev);
      };
      const followMf = async () => {
        if (mfItem.value) { await toggle(mfItem.value, "follow", true); pickItem(home.value.items.find((x) => x.id === mfItem.value.id)); return; }
        selTpl.value = "mf_weekly"; selItem.value = null;
        const t = home.value.templates.find((x) => x.id === "mf_weekly");
        form.name = t.name; form.params = JSON.parse(JSON.stringify(t.defaults));
        await toggleHere("follow", true);
      };
      const removeItem = async (x) => {
        if (!confirm(`删除策略“${x.name}”？（它的模拟账户会保留在交易页）`)) return;
        try { await api.del("/api/strategy/items/" + x.id); toast.success("已删除"); delete memo["item:" + x.id]; selItem.value = null; await load(); pickTemplate(selTpl.value); } catch (e) { /* 已提示 */ }
      };
      const runNow = async () => {
        try { const r = await api.post("/api/strategy/run", {}); runJob.value = r.job_id; jobs.track(r.job_id, { title: "策略模拟跟踪", onDone: () => { runJob.value = ""; load(); } }); } catch (e) { /* 已提示 */ }
      };

      const btStale = computed(() => !!bt.value && !!bt.value.spec && PARAM_KEYS.some((k) => k in form.params && bt.value.spec[k] !== form.params[k]));
      const verdictKey = computed(() => (bt.value ? bt.value.verdict.key : null));
      const accLink = (x) => (x && x.follow && x.follow.account_id ? "#/trade?acc=" + x.follow.account_id : "#/trade");
      const seg = computed(() => (bt.value && bt.value.segments[segTab.value]) || null);
      const curveOption = computed(() => {
        const s = seg.value;
        if (!s || !s.curve.dates.length) return null;
        const c = QW.colors();
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 52, right: 14, top: 30, bottom: 24 },
          legend: { top: 0, data: ["这个策略", "同样规则随机选股（平均）"], textStyle: { color: c.text2 } },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => v + "%" },
          xAxis: { type: "category", data: s.curve.dates, ...QW.axisBase(c) },
          yAxis: { type: "value", ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => v + "%" } },
          series: [
            { name: "这个策略", type: "line", showSymbol: false, data: s.curve.strategy.map((v) => +(v * 100).toFixed(2)), lineStyle: { width: 2, color: c.primary }, itemStyle: { color: c.primary } },
            { name: "同样规则随机选股（平均）", type: "line", showSymbol: false, data: s.curve.random.map((v) => +(v * 100).toFixed(2)), lineStyle: { width: 1.5, color: c.text3, type: "dashed" }, itemStyle: { color: c.text3 } },
          ],
        };
      });

      return { home, err, selTpl, selItem, tpl, item, isMf, mfItem, form, pickTemplate, pickItem, saveItem, runBacktest, bt, btJob, btLoading, toggle,
        toggleHere, followMf, removeItem, runNow, runJob, segTab, seg, curveOption, pct, cls, isNum, VERDICT, btStale, verdictKey, accLink, detailRef };
    },
    template: `<div class="stack">
      <div class="st-flow">
        <div class="st-step"><i>1</i><div><b>选股来源</b><span>用什么挑股票：<a href="#/mf">量化选股</a>、<a href="#/screener">选股器</a>方案或<a href="#/lab">模型实验室</a>的模型</span></div></div>
        <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
        <div class="st-step"><i>2</i><div><b>诚实回测（这里）</b><span>加上怎么买、怎么卖、买多少，用真正的模拟盘规则逐日跑，和“同样规则、随机挑股票”比</span></div></div>
        <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
        <div class="st-step"><i>3</i><div><b>模拟跟踪</b><span>自动开一个模拟账户，每天收盘后照这个策略挂第二天的单，在<a href="#/trade">交易</a>页看真实成绩（不花钱）</span></div></div>
        <qw-icon name="chevronRight" :size="16" class="st-arrow"/>
        <div class="st-step"><i>4</i><div><b>实盘建议</b><span>前几名出现在<a href="#/trade?tab=nightly">交易 → 明日计划</a>的候选买入，买不买你确认</span></div></div>
      </div>
      <div class="gd-note"><qw-icon name="info" :size="15"/><span><b>结论先说：</b>目前只有“量化选股 每周调仓”在样本外显著跑赢同池随机，它的规则固定，直接开模拟跟踪就行；
        其他模板用的是选股器方案，历史回测都没有明显优势——可以回测、可以模拟观察，但不建议照着买。</span></div>
      <qw-empty v-if="err" icon="alert" title="策略中心读取不到" :desc="err"/>
      <qw-skeleton v-else-if="!home" :rows="8"/>
      <template v-else>
        <qw-card v-if="home.items.length" title="我的策略" icon="compass" :pad="false" sub="点一行看详情；打勾就开启">
          <template #extra><button class="btn sm" :disabled="!!runJob" @click="runNow" v-tip="'平时每天收盘后会自动跑（程序要开着）；这里可以手动按最近一天收盘跑一次'">现在跑一次</button></template>
          <qw-table :rows="home.items" row-key="id" dense clickable :active-key="selItem" @row-click="pickItem" max-height="300px"
            :columns="[{key:'name',label:'策略'},{key:'backtest',label:'回测结论'},{key:'follow',label:'模拟跟踪',align:'center'},{key:'live',label:'实盘建议',align:'center'},{key:'account',label:'模拟账户',align:'right'},{key:'act',label:'',align:'right'}]">
            <template #cell-name="{row}"><b>{{ row.name }}</b><div class="muted" style="font-size:12px">{{ (home.templates.find((t) => t.id === row.template) || {}).name }}</div></template>
            <template #cell-backtest="{row}"><span v-if="row.backtest" class="qw-tag" :class="(VERDICT[row.backtest.verdict.key] || ['gray'])[0]">{{ (VERDICT[row.backtest.verdict.key] || [0, '—'])[1] }}</span><span v-else class="muted">还没回测</span></template>
            <template #cell-follow="{row}"><label class="sc-chk" @click.stop><input type="checkbox" :checked="row.follow && row.follow.enabled" @change="toggle(row, 'follow', $event.target.checked, $event)"></label></template>
            <template #cell-live="{row}"><span v-if="row.is_mf" class="muted" style="font-size:12px" v-tip="'调仓清单每个调仓日自动出现在明日计划'">自动</span>
              <label v-else class="sc-chk" @click.stop><input type="checkbox" :checked="row.live" @change="toggle(row, 'live', $event.target.checked, $event)"></label></template>
            <template #cell-account="{row}"><template v-if="row.account"><span class="num" :class="cls(row.account.return)">{{ pct(row.account.return, 2) }}</span>
              <div class="muted" style="font-size:12px">{{ row.account.positions }} 只持仓 · <a :href="accLink(row)" @click.stop>去看</a></div></template><span v-else class="muted">—</span></template>
            <template #cell-act="{row}"><button class="qw-iconbtn sm" @click.stop="removeItem(row)" v-tip="'删除'"><qw-icon name="trash" :size="14"/></button></template>
          </qw-table>
        </qw-card>
        <qw-job v-if="runJob" :job-id="runJob" title="策略模拟跟踪"/>

        <div class="fm-layout">
          <qw-card class="fm-list" :pad="false">
            <div class="fm-list-hd"><b>策略模板</b><span class="muted" style="font-size:12px">选一个 → 看回测 → 满意就开模拟跟踪</span></div>
            <div class="fm-items">
              <button v-for="t in home.templates" :key="t.id" class="fm-item" :class="{on: t.id === selTpl && !selItem}" @click="pickTemplate(t.id)">
                <div class="fm-item-hd"><b>{{ t.name }}</b>
                  <span v-if="t.verified" class="qw-tag red">样本外有优势</span>
                  <span v-else class="qw-tag" :class="t.signal.type === 'model' ? 'blue' : 'gray'">{{ t.signal.type === 'model' ? '模型' : '选股方案' }}</span></div>
                <div class="muted fm-item-desc">适合：{{ t.who }}</div>
              </button>
            </div>
          </qw-card>

          <div class="stack">
            <!-- 量化选股 每周调仓：规则固定，回测 = 量化选股页的回测 -->
            <template v-if="tpl && isMf">
              <qw-card ref="detailRef" :title="selItem ? '我的策略：' + form.name : tpl.name" icon="target">
                <p class="text-2" style="line-height:1.7;margin-top:0">{{ tpl.desc }}</p>
                <div v-if="home.mf" class="mf-plan-sum st-mf-nums">
                  <div><span>样本外年化</span><b :class="$fmt.dir(home.mf.cagr)">{{ pct(home.mf.cagr) }}</b></div>
                  <div><span>同池随机</span><b>{{ pct(home.mf.bench_cagr) }}</b></div>
                  <div><span>每年超额</span><b :class="$fmt.dir(home.mf.excess_ann)">{{ pct(home.mf.excess_ann) }}</b></div>
                  <div><span>t 值</span><b>{{ $fmt.t(home.mf.excess_t) }}</b></div>
                  <div><span>最大回撤</span><b class="down">{{ pct(home.mf.maxdd) }}</b></div>
                </div>
                <p v-if="home.mf" class="muted" style="font-size:12px;margin:6px 0 0">{{ home.mf.verdict.text }} 回测生成于 {{ home.mf.generated_at }}；完整回测、容量表和逐年成绩在<a href="#/mf">量化选股</a>页。
                  资金越大优势越小，上亿基本没有优势。</p>
                <div v-else class="gd-note warn"><qw-icon name="alert" :size="15"/><span>还没有量化选股的回测报告：去<a href="#/mf">量化选股</a>页点“重新回测”（约 10 分钟）。</span></div>
                <div class="st-rules">
                  <b>规则（固定，和回测一样）</b>
                  <ul>
                    <li>每周最后一个交易日收盘后定组合（50 只，单行业最多 8 只；已持有的只要还在前 150 名就不卖，少换手）。</li>
                    <li>下一个交易日开盘：先卖掉掉出组合的，再按“总资产 ÷ 50”等权买新进组合的；卖出回笼的钱当天就用来买。</li>
                    <li>开盘涨停买不进 / 跌停卖不出的，之后每天收盘后自动补单；单只不设止损，周中不换股。</li>
                  </ul>
                </div>
                <template #footer>
                  <div class="row">
                    <template v-if="mfItem && mfItem.follow && mfItem.follow.enabled">
                      <span class="qw-tag red">模拟跟踪已开启</span>
                      <span class="muted" style="font-size:12.5px">{{ mfItem.follow.target_date ? '已按 ' + mfItem.follow.target_date + ' 的组合下单' : '下一次收盘后（或点“现在跑一次”）开始建仓' }}</span>
                      <a class="btn sm" :href="accLink(mfItem)">去看模拟账户</a>
                      <button class="btn sm ghost" @click="toggle(mfItem, 'follow', false)">关闭</button>
                    </template>
                    <template v-else>
                      <button class="btn primary" @click="followMf"><qw-icon name="play" :size="14"/>开启模拟跟踪</button>
                      <span class="muted" style="font-size:12.5px">不花真钱：新建一个模拟账户，每个调仓日自动照组合调仓，成绩在“交易”页看。</span>
                    </template>
                  </div>
                  <div class="row muted" style="font-size:12.5px;margin-top:8px"><qw-icon name="info" :size="13"/>
                    <span>实盘：调仓日晚上<a href="#/trade?tab=nightly">明日计划</a>会列出要卖 / 要买的股票，按你的资金算好的股数在<a href="#/mf">量化选股 → 下单清单</a>。</span></div>
                </template>
              </qw-card>
            </template>

            <!-- 其他模板：参数 → 诚实回测 → 下一步 -->
            <template v-else-if="tpl">
              <qw-card ref="detailRef" :title="selItem ? '我的策略：' + form.name : tpl.name" icon="compass">
                <p class="text-2" style="line-height:1.7;margin-top:0">{{ tpl.desc }}</p>
                <div v-if="tpl.signal.type === 'model' && !home.model_enabled" class="gd-note warn"><qw-icon name="alert" :size="15"/><span>这个策略要用模型实验室里启用的模型。请先去<a href="#/lab">模型实验室</a>训练一个模型，并点“启用到选股器”。</span></div>
                <div class="td-form">
                  <label><span>名字</span><input v-model="form.name" maxlength="40" class="qw-input"></label>
                  <label><span>怎么买</span><select v-model="form.params.entry" class="qw-input"><option v-for="(v, k) in home.entry" :key="k" :value="k">{{ v }}</option></select></label>
                  <label><span>怎么卖（移动止盈 / 止损）</span><select v-model="form.params.trail" class="qw-input"><option v-for="(v, k) in home.trail" :key="k" :value="k">{{ v }}</option></select></label>
                  <label><span>{{ home.param_names.target_r }}</span><input v-model.number="form.params.target_r" type="number" min="0" max="10" step="0.5" class="qw-input">
                    <em class="hint">例如 2 = 涨到“买入价 + 2 倍止损幅度”时全部卖出</em></label>
                  <label><span>{{ home.param_names.max_days }}</span><input v-model.number="form.params.max_days" type="number" min="1" max="250" class="qw-input">
                    <em class="hint">到期就卖（交易日）</em></label>
                  <label><span>{{ home.param_names.max_positions }}</span><input v-model.number="form.params.max_positions" type="number" min="1" max="30" class="qw-input">
                    <em class="hint">每只按“一笔最多亏总资金的比例”和止损算股数</em></label>
                </div>
                <div class="row" style="margin-top:12px">
                  <label class="sc-chk"><input type="checkbox" v-model="form.params.exit_distribution">出现出货迹象就卖</label>
                  <label class="sc-chk"><input type="checkbox" v-model="form.params.regime">大盘弱势时少买（按设置里的仓位上限）</label>
                </div>
                <template #footer>
                  <div class="row">
                    <button class="btn primary" :disabled="!!btJob" @click="runBacktest(false)"><qw-icon name="history" :size="14"/>{{ btJob ? '回测中…' : '① 诚实回测' }}</button>
                    <button class="btn" @click="saveItem(false)"><qw-icon name="save" :size="14"/>{{ selItem ? '保存修改' : '保存为我的策略' }}</button>
                    <label class="sc-chk" v-tip="'还没保存时会先存成“我的策略”'"><input type="checkbox" :checked="item && item.follow && item.follow.enabled" @change="toggleHere('follow', $event.target.checked, $event)">② 模拟跟踪</label>
                    <label class="sc-chk" v-tip="'还没保存时会先存成“我的策略”'"><input type="checkbox" :checked="item && item.live" @change="toggleHere('live', $event.target.checked, $event)">③ 实盘建议</label>
                    <a v-if="item && item.follow && item.follow.account_id" class="btn sm ghost" :href="accLink(item)">去看模拟账户</a>
                  </div>
                </template>
              </qw-card>

              <qw-card v-if="item && item.latest && item.latest.picks && item.latest.picks.length" :title="'最近的实盘建议（' + item.latest.date + '）'" icon="listCheck" :pad="false" sub="已放进“明日计划”的候选买入，需要你确认">
                <qw-table :rows="item.latest.picks" row-key="code" dense max-height="320px"
                  :columns="[{key:'rank',label:'#',width:'36px'},{key:'name',label:'股票'},{key:'close',label:'收盘',align:'right'},{key:'stop',label:'止损 / 股数',align:'right'},{key:'act',label:'',align:'right'}]">
                  <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
                  <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span></template>
                  <template #cell-stop="{row}"><span class="num">{{ $fmt.price(row.stop) }}</span><div class="num">{{ row.shares }} 股</div></template>
                  <template #cell-act="{row}"><a class="btn sm" :href="'#/watch/' + row.code">诊断</a></template>
                </qw-table>
              </qw-card>

              <qw-job v-if="btJob" :job-id="btJob" title="策略回测"/>
              <qw-card v-if="btLoading && !bt" title="诚实回测"><qw-skeleton :rows="4"/></qw-card>
              <qw-card v-else-if="bt" title="诚实回测" icon="history" :sub="bt.data_start + ' ~ ' + bt.data_end + ' · 初始资金 ' + $fmt.money(bt.capital) + ' · 随机对照 ' + bt.seeds + ' 次 · 回测于 ' + (bt.saved_at || '')">
                <template #extra><button class="btn sm ghost" :disabled="!!btJob" @click="runBacktest(true)">重新回测</button></template>
                <div v-if="btStale" class="gd-note warn" style="margin-bottom:8px"><qw-icon name="alert" :size="15"/><span>参数改过了：下面是改之前的参数的回测，点“诚实回测”看新参数。</span></div>
                <div class="st-advice" :class="bt.verdict.credible ? 't-good' : 't-neutral'"><qw-icon name="target" :size="15"/><span>{{ bt.verdict.text }}</span></div>
                <div class="st-next">
                  <b>回测完接下来做什么？</b>
                  <ol v-if="verdictKey === 'good'">
                    <li>点“保存为我的策略”，勾上<b>② 模拟跟踪</b>：程序每天收盘后在它自己的模拟账户里照做（在<a href="#/trade">交易</a>页看）。</li>
                    <li>观察 1~3 个月：模拟账户的成绩和回测差不多，再勾<b>③ 实盘建议</b>，前几名会出现在<a href="#/trade?tab=nightly">明日计划</a>的候选买入里。</li>
                    <li>实盘从小仓位开始；买入永远要你确认，程序不会替你买。</li>
                  </ol>
                  <ol v-else>
                    <li>这个策略<b>没有证明比随机挑股票好</b>：不建议开“实盘建议”，更不建议照着买。</li>
                    <li>想看看它真实跑起来怎样，可以只开<b>② 模拟跟踪</b>（不花钱）。</li>
                    <li>想要样本外验证过有优势的方法：用左边的<a href="javascript:void(0)" @click="pickTemplate('mf_weekly')">“量化选股 每周调仓”</a>。</li>
                  </ol>
                </div>
                <div class="hs-box" style="margin-top:12px"><div style="overflow-x:auto" v-hscroll>
                  <table class="mini-table fm-table">
                    <thead><tr><th>时段</th><th>年化（策略）</th><th>年化（随机平均 / 范围）</th><th>超额</th><th>t 值</th><th>最大回撤（策略 / 随机）</th><th>夏普</th><th>平均仓位</th><th>交易笔数</th><th>胜率</th><th>平均 R</th></tr></thead>
                    <tbody><tr v-for="s in [['selection','选择期（2025-07 前）'],['holdout','留出期（样本外）']]" :key="s[0]" :class="{hl: s[0] === 'holdout'}">
                      <td>{{ s[1] }}</td>
                      <template v-if="bt.segments[s[0]]">
                        <td class="num" :class="cls(bt.segments[s[0]].strategy.cagr)"><b>{{ pct(bt.segments[s[0]].strategy.cagr) }}</b></td>
                        <td class="num">{{ pct(bt.segments[s[0]].random.cagr) }} <span class="muted">（{{ pct(bt.segments[s[0]].random.cagr_min) }} ~ {{ pct(bt.segments[s[0]].random.cagr_max) }}）</span></td>
                        <td class="num" :class="cls(bt.segments[s[0]].excess_cagr)"><b>{{ pct(bt.segments[s[0]].excess_cagr) }}</b></td>
                        <td class="num">{{ bt.segments[s[0]].t ?? '—' }}</td>
                        <td class="num">{{ pct(bt.segments[s[0]].strategy.max_dd) }} / {{ pct(bt.segments[s[0]].random.max_dd) }}</td>
                        <td class="num">{{ isNum(bt.segments[s[0]].strategy.sharpe) ? bt.segments[s[0]].strategy.sharpe.toFixed(2) : '—' }}</td>
                        <td class="num">{{ $fmt.ratio(bt.segments[s[0]].strategy.exposure, 0) }}</td>
                        <td class="num">{{ bt.segments[s[0]].strategy.trades.n }}</td>
                        <td class="num">{{ bt.segments[s[0]].strategy.trades.n ? $fmt.ratio(bt.segments[s[0]].strategy.trades.win, 0) : '—' }}</td>
                        <td class="num">{{ isNum(bt.segments[s[0]].strategy.trades.avg_r) ? bt.segments[s[0]].strategy.trades.avg_r.toFixed(2) : '—' }}</td>
                      </template><td v-else colspan="10" class="muted">这一段没有数据</td>
                    </tr></tbody>
                  </table></div></div>
                <div class="row" style="margin:12px 0 4px"><qw-segmented v-model="segTab" size="sm" :options="[{value:'selection',label:'选择期'},{value:'holdout',label:'留出期'}]"/>
                  <span class="muted" style="font-size:12px">每一段都从新账户开始</span></div>
                <qw-chart v-if="curveOption" :option="curveOption" height="260px"/>
                <p class="muted" style="font-size:12px;line-height:1.7">{{ bt.rules }}</p>
              </qw-card>
              <qw-card v-else-if="!btJob" title="诚实回测" icon="history">
                <p class="muted" style="margin:0;line-height:1.7">这组参数还没有回测。点“① 诚实回测”：用真正的模拟盘规则把 2020 年以来每天跑一遍，并和“同样买卖规则、随机挑股票”比（后台运行，要几分钟，可以先去别的页面）。</p>
              </qw-card>
            </template>
          </div>
        </div>
      </template>
    </div>`,
  });
})();
