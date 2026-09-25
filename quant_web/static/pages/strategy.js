/* 策略中心 #/strategy：模板（选股 + 买入方式 + 卖出规则 + 仓位 + 大盘过滤）→ 诚实回测（真正的模拟盘规则逐日跑，和同样规则的随机选股比）
   → 开启模拟跟踪（每天自动在模拟账户里买卖）→ 生成实盘建议（进入明日计划，买入仍要你确认） */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted } = Vue;
  const { api, fmt, toast, jobs, isNum } = QW;

  const VERDICT = { good: ["red", "比随机有优势"], weak: ["warn", "还不够可信"], bad: ["green", "没有比随机好"], short: ["gray", "留出期太短"] };
  const pct = (v, d = 1) => (isNum(v) ? fmt.ratio(v, d, true) : "—");
  const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");

  QW.page("strategy", {
    props: ["params", "query"],
    setup() {
      const home = ref(null);
      const err = ref("");
      const selTpl = ref("reversal_value");
      const selItem = ref(null);
      const form = reactive({ name: "", params: {} });
      const bt = ref(null);
      const btJob = ref("");
      const runJob = ref("");
      const segTab = ref("holdout");

      const load = async () => {
        try { home.value = await api.get("/api/strategy", null, { silent: true }); err.value = ""; } catch (e) { err.value = e.detail || e.message; }
      };
      onMounted(async () => { await load(); pickTemplate(selTpl.value); });

      const tpl = computed(() => (home.value ? home.value.templates.find((t) => t.id === selTpl.value) : null));
      const item = computed(() => (home.value && selItem.value ? home.value.items.find((x) => x.id === selItem.value) : null));
      const pickTemplate = (id) => {
        selTpl.value = id; selItem.value = null; bt.value = null; btJob.value = "";
        const t = home.value && home.value.templates.find((x) => x.id === id);
        if (t) { form.name = t.name; form.params = JSON.parse(JSON.stringify(t.defaults)); }
      };
      const pickItem = async (x) => {
        selTpl.value = x.template; selItem.value = x.id; btJob.value = "";
        form.name = x.name; form.params = JSON.parse(JSON.stringify(x.params));
        bt.value = null;
        if (x.backtest) { try { bt.value = await api.get("/api/strategy/backtest/" + x.backtest.key, null, { silent: true }); } catch (e) { /* 还没有 */ } }
      };
      const saveItem = async () => {
        try {
          const r = await api.post("/api/strategy/items", { template: selTpl.value, params: form.params, name: form.name, item_id: selItem.value });
          toast.success(selItem.value ? "已保存修改" : "已保存为我的策略");
          await load();
          selItem.value = r.id;
        } catch (e) { /* 已提示 */ }
      };
      const runBacktest = async (force) => {
        try {
          const r = await api.post("/api/strategy/backtest", { template: selTpl.value, params: form.params, item_id: selItem.value, force: !!force });
          if (r.result) { bt.value = r.result; toast.info("参数没变，直接显示上次的回测"); return; }
          btJob.value = r.job_id;
          jobs.track(r.job_id, { title: "策略回测", onDone: async (j) => {
            const key = j && j.result && j.result.key;
            if (key) { try { bt.value = await api.get("/api/strategy/backtest/" + key, null, { silent: true }); } catch (e) { /* 忽略 */ } }
            load();
          } });
        } catch (e) { /* 已提示 */ }
      };
      const toggle = async (x, what, on) => {
        if (what === "follow" && on && !confirm("开启模拟跟踪：程序会新建一个模拟账户，每天收盘后按这个策略自动挂第二天的单（模拟，不花真钱）。确定？")) return;
        if (what === "live" && on && !confirm("开启实盘建议：每天收盘后，这个策略的前几名会出现在“交易 → 明日计划”的候选买入里。\n买入永远要你自己确认，程序不会替你买。确定？")) return;
        try { await api.post(`/api/strategy/items/${x.id}/${what}`, { enabled: on }); toast.success(on ? "已开启" : "已关闭"); await load(); } catch (e) { /* 已提示 */ }
      };
      const removeItem = async (x) => {
        if (!confirm(`删除策略“${x.name}”？（它的模拟账户会保留在交易页）`)) return;
        try { await api.del("/api/strategy/items/" + x.id); toast.success("已删除"); selItem.value = null; await load(); pickTemplate(selTpl.value); } catch (e) { /* 已提示 */ }
      };
      const runNow = async () => {
        try { const r = await api.post("/api/strategy/run", {}); runJob.value = r.job_id; jobs.track(r.job_id, { title: "策略模拟跟踪", onDone: load }); } catch (e) { /* 已提示 */ }
      };

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

      return { home, err, selTpl, selItem, tpl, item, form, pickTemplate, pickItem, saveItem, runBacktest, bt, btJob, toggle, removeItem, runNow, runJob,
        segTab, seg, curveOption, pct, cls, isNum, VERDICT };
    },
    template: `<div class="stack">
      <div class="gd-note"><qw-icon name="info" :size="15"/><span>策略 = 用什么选股 + 怎么买 + 怎么卖 + 买多少 + 大盘不好时少买。三步：
        <b>① 诚实回测</b>（用真正的模拟盘规则逐日跑，和“同样规则、随机挑股票”比，看选股本身有没有用）→ <b>② 开启模拟跟踪</b>（每天自动在模拟账户里买卖）→
        <b>③ 实盘建议</b>（进入明日计划，买入永远要你确认）。这里的模板用的是选股器方案，它们都没有显著跑赢随机——所以先模拟、仓位宁小勿大；样本外显著跑赢随机的是“量化选股”（每周调仓的组合，单独在那一页）。</span></div>
      <qw-empty v-if="err" icon="alert" title="策略中心读取不到" :desc="err"/>
      <qw-skeleton v-else-if="!home" :rows="8"/>
      <template v-else>
        <qw-card v-if="home.items.length" title="我的策略" icon="compass" :pad="false">
          <template #extra><button class="btn sm" @click="runNow" v-tip="'平时每天收盘后会自动跑；这里可以手动按最近一天收盘跑一次'">现在跑一次</button></template>
          <qw-table :rows="home.items" row-key="id" dense clickable :active-key="selItem" @row-click="pickItem"
            :columns="[{key:'name',label:'策略'},{key:'backtest',label:'回测结论'},{key:'follow',label:'模拟跟踪',align:'center'},{key:'live',label:'实盘建议',align:'center'},{key:'account',label:'模拟账户',align:'right'},{key:'act',label:'',align:'right'}]">
            <template #cell-name="{row}"><b>{{ row.name }}</b><div class="muted" style="font-size:12px">{{ (home.templates.find((t) => t.id === row.template) || {}).name }}</div></template>
            <template #cell-backtest="{row}"><span v-if="row.backtest" class="qw-tag" :class="(VERDICT[row.backtest.verdict.key] || ['gray'])[0]">{{ (VERDICT[row.backtest.verdict.key] || [0, '—'])[1] }}</span><span v-else class="muted">还没回测</span></template>
            <template #cell-follow="{row}"><label class="sc-chk" @click.stop><input type="checkbox" :checked="row.follow && row.follow.enabled" @change="toggle(row, 'follow', $event.target.checked)"></label></template>
            <template #cell-live="{row}"><label class="sc-chk" @click.stop><input type="checkbox" :checked="row.live" @change="toggle(row, 'live', $event.target.checked)"></label></template>
            <template #cell-account="{row}"><template v-if="row.account"><span class="num" :class="cls(row.account.return)">{{ pct(row.account.return, 2) }}</span>
              <div class="muted" style="font-size:12px">{{ row.account.positions }} 只持仓 · <a :href="'#/trade'">去看</a></div></template><span v-else class="muted">—</span></template>
            <template #cell-act="{row}"><button class="qw-iconbtn sm" @click.stop="removeItem(row)" v-tip="'删除'"><qw-icon name="trash" :size="14"/></button></template>
          </qw-table>
        </qw-card>
        <qw-job v-if="runJob" :job-id="runJob" title="策略模拟跟踪"/>

        <div class="fm-layout">
          <qw-card class="fm-list" :pad="false">
            <div class="fm-list-hd"><b>策略模板</b><span class="muted" style="font-size:12px">选一个，改参数，先回测</span></div>
            <div class="fm-items">
              <button v-for="t in home.templates" :key="t.id" class="fm-item" :class="{on: t.id === selTpl && !selItem}" @click="pickTemplate(t.id)">
                <div class="fm-item-hd"><b>{{ t.name }}</b><span class="qw-tag" :class="t.signal.type === 'model' ? 'blue' : 'gray'">{{ t.signal.type === 'model' ? '模型' : '选股方案' }}</span></div>
                <div class="muted fm-item-desc">适合：{{ t.who }}</div>
              </button>
            </div>
          </qw-card>

          <div class="stack">
            <qw-card v-if="tpl" :title="selItem ? '我的策略：' + form.name : tpl.name" icon="compass">
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
                  <button class="btn primary" @click="runBacktest(false)"><qw-icon name="history" :size="14"/>诚实回测</button>
                  <button class="btn" @click="saveItem"><qw-icon name="save" :size="14"/>{{ selItem ? '保存修改' : '保存为我的策略' }}</button>
                  <template v-if="item">
                    <label class="sc-chk"><input type="checkbox" :checked="item.follow && item.follow.enabled" @change="toggle(item, 'follow', $event.target.checked)">模拟跟踪</label>
                    <label class="sc-chk"><input type="checkbox" :checked="item.live" @change="toggle(item, 'live', $event.target.checked)">实盘建议</label>
                  </template>
                </div>
              </template>
            </qw-card>

            <qw-card v-if="item && item.latest && item.latest.picks && item.latest.picks.length" :title="'最近的实盘建议（' + item.latest.date + '）'" icon="listCheck" :pad="false" sub="已放进“明日计划”的候选买入，需要你确认">
              <qw-table :rows="item.latest.picks" row-key="code" dense
                :columns="[{key:'rank',label:'#',width:'36px'},{key:'name',label:'股票'},{key:'close',label:'收盘',align:'right'},{key:'stop',label:'止损 / 股数',align:'right'},{key:'act',label:'',align:'right'}]">
                <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
                <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span></template>
                <template #cell-stop="{row}"><span class="num">{{ $fmt.price(row.stop) }}</span><div class="num">{{ row.shares }} 股</div></template>
                <template #cell-act="{row}"><a class="btn sm" :href="'#/watch/' + row.code">诊断</a></template>
              </qw-table>
            </qw-card>

            <qw-job v-if="btJob" :job-id="btJob" title="策略回测"/>
            <qw-card v-if="bt" title="诚实回测" icon="history" :sub="bt.data_start + ' ~ ' + bt.data_end + ' · 初始资金 ' + $fmt.money(bt.capital) + ' · 随机对照 ' + bt.seeds + ' 次 · 回测于 ' + (bt.saved_at || '')">
              <template #extra><button class="btn sm ghost" @click="runBacktest(true)">重新回测</button></template>
              <div class="st-advice" :class="bt.verdict.credible ? 't-good' : 't-neutral'"><qw-icon name="target" :size="15"/><span>{{ bt.verdict.text }}</span></div>
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
          </div>
        </div>
      </template>
    </div>`,
  });
})();
