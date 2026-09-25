/* 市场情绪（首页 #/）：首次使用引导、指数、情绪温度、关键指标、连板天梯、行业分布、近60日情绪 */
(function () {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, jobs, usePoll, colors, tooltipBase, axisBase, isNum } = QW;

  const HELP = {
    temp: "把涨停家数、跌停家数、炸板率、昨日涨停股今天的表现、连板高度、上涨家数占比，和过去一年比较后算出的 0~100 分。越高说明短线越热。",
    lu: "收盘时封住涨停板（涨到当天允许的最高价）的股票数量。越多说明市场越热。",
    ld: "收盘时跌到当天允许最低价的股票数量。越多说明恐慌越重。",
    brk: "盘中碰到过涨停、但收盘没封住的比例（俗称“炸板”）。越高说明追涨的人越容易被套。",
    streak: "今天连续涨停天数最多的股票连了几天，代表短线资金敢不敢往上冲。",
    prem: "昨天涨停的股票今天平均涨了多少。正数说明前一天追涨的人今天能赚钱，负数说明亏钱。",
    ladder: "按连续涨停天数把今天涨停的股票分成几列，越靠左连板越多。“一字”指开盘就封死涨停、全天没打开，普通人很难买进。",
    ind: "今天涨停的股票都来自哪些行业。某个行业涨停多，说明它是今天的热点板块。",
    hist: "最近60个交易日的短线情绪变化，鼠标移到图上可以看每天的数值，四张图的日期是联动的。",
    amount: "沪深北三个市场今天一共成交了多少钱。成交越多说明参与的人越多。",
    median: "把所有股票今天的涨跌幅从高到低排，排在正中间那只的涨跌幅，比指数更能反映普通股票的表现。",
    promote: "昨天涨停的股票里，今天继续涨停的比例，也叫“晋级率”。越高说明连板接力越容易成功。",
    sprem: "昨天已经连板（2板及以上）的股票今天平均涨跌多少，反映高位股的风险。",
  };

  function summaryText(s, brk, prem) {
    if (!s) return "";
    const t = isNum(s.temperature) ? Math.round(s.temperature) : null;
    const parts = [];
    parts.push(`今天市场情绪「${s.label || "—"}」${t != null ? `（${t}分）` : ""}`);
    parts.push(`${s.n_limit_up ?? "—"} 家涨停、${s.n_limit_down ?? "—"} 家跌停`);
    if (isNum(brk)) parts.push(`炸板率 ${fmt.ratio(brk, 0)}，${brk < 0.2 ? "封板很牢" : brk < 0.35 ? "封板一般" : "封板不稳，追高容易被套"}`);
    if (isNum(s.max_streak)) parts.push(`最高 ${s.max_streak} 连板`);
    let sent = parts.join("，") + "。";
    if (isNum(prem)) {
      if (prem > 0.02) sent += `昨天涨停的股票今天平均还涨了 ${fmt.ratio(prem, 1)}，接力的资金在赚钱。`;
      else if (prem > 0) sent += `昨天涨停的股票今天平均小涨 ${fmt.ratio(prem, 1)}，接力赚钱效应一般。`;
      else sent += `昨天涨停的股票今天平均跌了 ${fmt.ratio(Math.abs(prem), 1)}，追涨的人在亏钱。`;
    }
    const advice = { 冰点: "情绪冰冷，适合多看少动。", 低迷: "情绪偏弱，少动手、别追高。", 正常: "情绪平稳，保持平常心。", 活跃: "赚钱效应不错，但别盲目追高。", 过热: "情绪过热，小心随时降温回落。" };
    return sent + (advice[s.label] || "");
  }

  function smallLine(c, dates, values, color, yfmt, name, extra) {
    const ax = axisBase(c);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      grid: { left: 42, right: 10, top: 8, bottom: 22 },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true, axisPointer: { type: "line", lineStyle: { color: c.text3 } },
        formatter: (ps) => `${dates[ps[0].dataIndex]}<br>${name}：<b>${yfmt(ps[0].value, true)}</b>`,
      },
      xAxis: { type: "category", data: dates, boundaryGap: false, ...ax, axisLabel: { ...ax.axisLabel, formatter: (v) => String(v).slice(5), hideOverlap: true }, splitLine: { show: false } },
      yAxis: { type: "value", splitNumber: 3, scale: !!(extra && extra.scale), min: extra && extra.min, max: extra && extra.max, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => yfmt(v) } },
      series: [{
        type: "line", name, data: values, showSymbol: false, smooth: false, lineStyle: { width: 2, color }, itemStyle: { color },
        areaStyle: { color, opacity: 0.07 },
        markLine: extra && extra.mark != null ? { symbol: "none", silent: true, label: { show: false }, lineStyle: { type: "dashed", color: c.text3, width: 1 }, data: [{ yAxis: extra.mark }] } : undefined,
      }],
    };
  }

  QW.page("dashboard", {
    props: ["params", "query"],
    setup() {
      const ov = ref(null);
      const loading = ref(true);
      const err = ref("");
      const hlInd = ref("");
      const onboardJob = ref(null);
      // 新手三步走（可关闭，记在本机浏览器）
      const guideOpen = ref(QW.safeStore.get("qw-guide-hide") !== "1");
      const hideGuide = () => { guideOpen.value = false; QW.safeStore.set("qw-guide-hide", "1"); };

      const load = async () => {
        try {
          ov.value = await api.get("/api/market/overview", null, { silent: true });
          err.value = "";
        } catch (e) {
          err.value = e.detail || e.message;
        } finally {
          loading.value = false;
        }
      };
      onMounted(load);
      usePoll(load, 10000);
      const offDone = bus.on("job-done", load);
      onBeforeUnmount(offDone);

      const panelEmpty = computed(() => {
        const s = store.status;
        return !!s && (s.first_run === true || !s.panel || s.panel.empty === true || !s.panel.end || !s.panel.rows);
      });
      const runningUpdate = computed(() => jobs.running().find((j) => j.name === "update" || j.name === "daily"));
      const shownJob = computed(() => onboardJob.value || (runningUpdate.value && runningUpdate.value.id));
      const startDownload = async () => {
        try { onboardJob.value = await jobs.start("/api/jobs/update", {}, "下载全市场数据"); } catch (e) { /* 已提示 */ }
      };
      const onJobDone = () => { QW.refreshStatus(); load(); };

      const s = computed(() => (ov.value && ov.value.sentiment) || null);
      const brk = computed(() => (s.value ? fmt.frac(s.value.break_rate, 1.5) : null));
      // 后端约定：涨幅类（prev_lu_premium、prev_streak_premium、median_pct）为百分数，比率类（break_rate、prev_lu_promote）为 0~1 小数
      const pct2frac = (v) => (isNum(v) ? v / 100 : null);
      const prem = computed(() => (s.value ? pct2frac(s.value.prev_lu_premium) : null));
      const promote = computed(() => (s.value ? fmt.frac(s.value.prev_lu_promote, 1.5) : null));
      const history = computed(() => ((ov.value && ov.value.history) || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1)));
      const prevRow = computed(() => {
        if (!s.value) return null;
        const h = history.value.filter((r) => !s.value.date || r.date < s.value.date);
        return h.length ? h[h.length - 1] : null;
      });
      const delta = (key, norm) => {
        if (!s.value || !prevRow.value) return null;
        const a = norm ? fmt.frac(s.value[key], norm) : s.value[key];
        const b = norm ? fmt.frac(prevRow.value[key], norm) : prevRow.value[key];
        return isNum(a) && isNum(b) ? a - b : null;
      };
      const kpis = computed(() => {
        const x = s.value;
        if (!x) return [];
        const dLu = delta("n_limit_up");
        const dLd = delta("n_limit_down");
        const dBrk = delta("break_rate", 1.5);
        const dStreak = delta("max_streak");
        return [
          { label: "涨停家数", value: x.n_limit_up, unit: "家", tone: "up", help: HELP.lu, delta: dLu, deltaText: isNum(dLu) ? `${Math.abs(dLu)} 较昨日` : "" },
          { label: "跌停家数", value: x.n_limit_down, unit: "家", tone: "down", help: HELP.ld, delta: dLd, deltaText: isNum(dLd) ? `${Math.abs(dLd)} 较昨日` : "" },
          {
            label: "炸板率", value: isNum(brk.value) ? (brk.value * 100).toFixed(1) : null, unit: "%", help: HELP.brk,
            delta: dBrk, deltaText: isNum(dBrk) ? `${Math.abs(dBrk * 100).toFixed(1)} 个百分点` : "", sub: isNum(x.n_broken) ? `炸板 ${x.n_broken} 家` : "",
          },
          { label: "连板高度", value: x.max_streak, unit: "板", help: HELP.streak, delta: dStreak, deltaText: isNum(dStreak) ? `${Math.abs(dStreak)} 较昨日` : "", sub: isNum(x.n_streak2) ? `2板以上 ${x.n_streak2} 家` : "" },
          {
            label: "昨日涨停今日", value: isNum(prem.value) ? (prem.value > 0 ? "+" : "") + (prem.value * 100).toFixed(2) : null, unit: "%",
            tone: fmt.dir(prem.value), help: HELP.prem, sub: "平均涨跌幅",
          },
        ];
      });
      const secondary = computed(() => {
        const x = s.value;
        if (!x) return [];
        const med = pct2frac(x.median_pct);
        const sp = pct2frac(x.prev_streak_premium);
        return [
          { k: "两市成交额", v: fmt.money(x.amount_total), s: isNum(x.amount_ratio20) ? `约为近20日均值的 ${fmt.num(x.amount_ratio20, 2)} 倍` : "", help: HELP.amount },
          { k: "个股涨跌中位数", v: fmt.ratio(med, 2, true), cls: fmt.dir(med), s: "一半股票比它涨得多", help: HELP.median },
          { k: "昨日涨停晋级率", v: fmt.ratio(promote.value, 1), s: "昨天涨停今天继续涨停", help: HELP.promote },
          { k: "昨日连板股今日", v: fmt.ratio(sp, 2, true), cls: fmt.dir(sp), s: "平均涨跌幅", help: HELP.sprem },
        ];
      });
      const summary = computed(() => summaryText(s.value, brk.value, prem.value));
      const breadth = computed(() => {
        const x = s.value;
        if (!x || !isNum(x.n_up) || !isNum(x.n_down)) return null;
        const total = isNum(x.n_stocks) && x.n_stocks > 0 ? x.n_stocks : x.n_up + x.n_down;
        const flat = Math.max(0, total - x.n_up - x.n_down);
        return { up: x.n_up, down: x.n_down, flat, total, upP: (x.n_up / total) * 100, flatP: (flat / total) * 100, downP: (x.n_down / total) * 100 };
      });

      const ladder = computed(() => ((ov.value && ov.value.ladder) || []).filter((l) => l.stocks && l.stocks.length).slice().sort((a, b) => b.streak - a.streak));
      const ladderTotal = computed(() => ladder.value.reduce((a, l) => a + l.stocks.length, 0));
      const chipTip = (st) => {
        return `${st.name}（${st.code}）\n行业：${st.industry || "—"}\n今日涨幅：${fmt.pct(st.pct, 2)}${st.one_word ? "\n一字涨停：开盘即封死，普通人难买进" : ""}\n点击看盘`;
      };
      const chipCls = (st) => (hlInd.value ? (st.industry === hlInd.value ? "hl" : "dim") : "");

      const industries = computed(() => ((ov.value && ov.value.industries) || []).filter((x) => x.count > 0).slice().sort((a, b) => b.count - a.count).slice(0, 12));
      const indHeight = computed(() => Math.max(200, industries.value.length * 28 + 30) + "px");
      const indOption = computed(() => {
        const c = colors();
        const list = industries.value;
        const ax = axisBase(c);
        return {
          animation: false, textStyle: { fontFamily: c.font },
          grid: { left: 8, right: 34, top: 4, bottom: 8, containLabel: true },
          tooltip: { ...tooltipBase(c), trigger: "item", formatter: (p) => `${list[p.dataIndex].industry}<br>涨停 <b>${p.value}</b> 家<br><span style="opacity:.7">点击高亮天梯里的这个行业</span>` },
          xAxis: { type: "value", minInterval: 1, ...ax, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
          yAxis: {
            type: "category", inverse: true, data: list.map((x) => fmt.industry(x.industry)), ...ax, axisLine: { show: false },
            axisLabel: { ...ax.axisLabel, color: c.text2, fontSize: 12 },
          },
          series: [{
            type: "bar", barWidth: 14, cursor: "pointer",
            data: list.map((x) => ({
              value: x.count,
              itemStyle: { color: c.up, opacity: !hlInd.value || hlInd.value === x.industry ? 0.9 : 0.25, borderRadius: [0, 4, 4, 0] },
            })),
            label: { show: true, position: "right", color: c.text2, fontSize: 12, formatter: "{c}" },
          }],
        };
      });
      const onIndClick = (p) => {
        const it = industries.value[p.dataIndex];
        if (!it) return;
        hlInd.value = hlInd.value === it.industry ? "" : it.industry;
      };

      const histCharts = computed(() => {
        const c = colors();
        const h = history.value;
        if (!h.length) return [];
        const dates = h.map((r) => r.date);
        const last = h[h.length - 1];
        const intF = (v, tip) => (isNum(v) ? String(Math.round(v)) + (tip ? " 家" : "") : "—");
        const pctF = (v, tip) => (isNum(v) ? (v * 100).toFixed(tip ? 1 : 0) + "%" : "—");
        const tempF = (v) => (isNum(v) ? String(Math.round(v)) : "—");
        const brkVals = h.map((r) => fmt.frac(r.break_rate, 1.5));
        return [
          { key: "lu", title: "涨停家数", latest: intF(last.n_limit_up, true), option: smallLine(c, dates, h.map((r) => r.n_limit_up), c.up, intF, "涨停家数") },
          { key: "ld", title: "跌停家数", latest: intF(last.n_limit_down, true), option: smallLine(c, dates, h.map((r) => r.n_limit_down), c.down, intF, "跌停家数") },
          { key: "brk", title: "炸板率", latest: pctF(brkVals[brkVals.length - 1], true), option: smallLine(c, dates, brkVals, c.series[3], pctF, "炸板率") },
          {
            key: "temp", title: "情绪温度", latest: tempF(last.temperature),
            option: smallLine(c, dates, h.map((r) => r.temperature), c.series[1], tempF, "情绪温度", { min: 0, max: 100, mark: 50 }),
          },
        ];
      });

      const idxList = computed(() => (ov.value && ov.value.indexes) || []);
      const asOf = computed(() => (ov.value && ov.value.as_of) || "");
      const retry = () => { loading.value = true; load(); };

      return {
        store, fmt, HELP, ov, loading, err, panelEmpty, shownJob, startDownload, onJobDone, s, kpis, secondary, summary, breadth,
        ladder, ladderTotal, chipTip, chipCls, hlInd, industries, indHeight, indOption, onIndClick, histCharts, idxList, asOf, retry,
        guideOpen, hideGuide,
      };
    },
    template: `<div class="stack">
      <qw-card v-if="panelEmpty || (shownJob && !s)">
        <div class="dash-onboard">
          <div class="dash-onboard-ico"><qw-icon name="download" :size="28"/></div>
          <div class="grow">
            <h2>欢迎使用量化助手</h2>
            <p>第一次使用需要先把全市场股票（约5000只，2019年至今）的日线数据下载到电脑上，大约 15 分钟，只需要做一次。之后每天收盘后点“一键更新”，几十秒就能更新完。</p>
          </div>
          <button class="btn primary lg" :disabled="!!shownJob" @click="startDownload"><qw-icon name="download" :size="17"/>{{ shownJob ? '正在下载…' : '第一次使用：下载全市场数据，约15分钟' }}</button>
        </div>
        <div v-if="shownJob" style="padding:0 24px 20px"><qw-job :job-id="shownJob" title="下载全市场数据" @done="onJobDone"/></div>
      </qw-card>

      <template v-if="loading && !ov">
        <qw-skeleton type="tiles" :count="5"/>
        <div class="grid dash-row1"><qw-card><qw-skeleton height="220px"/></qw-card><qw-card><qw-skeleton type="tiles" :count="5"/></qw-card></div>
        <qw-card><qw-skeleton height="240px"/></qw-card>
      </template>

      <qw-card v-else-if="!ov && err && !panelEmpty">
        <qw-empty icon="alert" title="市场数据暂时拿不到" :desc="err" action-text="重新加载" @action="retry"/>
      </qw-card>

      <qw-card v-else-if="!ov && panelEmpty && !shownJob">
        <qw-empty icon="pulse" title="下载完数据后，这里会显示今天的市场情绪" desc="包括情绪温度、涨停跌停家数、连板天梯、热门行业和近60日走势。"/>
      </qw-card>

      <template v-if="ov">
        <div v-if="guideOpen && s" class="dash-guide">
          <div class="dash-guide-hd">
            <b><qw-icon name="sparkles" :size="16"/>新手三步走</b>
            <span class="muted">不用懂交易，照着点就行；建议先只看不买。</span>
            <button class="linkbtn dash-guide-x" @click="hideGuide">知道了，不再显示</button>
          </div>
          <ol class="dash-guide-steps">
            <li class="here">
              <span class="n">1</span>
              <div><b>看大盘环境和情绪</b><p>就是这一页。下面的<b>大盘环境</b>告诉你现在敢用多少仓位{{ s.label ? '；情绪温度今天「' + s.label + '」' : '' }}。环境弱、温度低时多看少动。</p>
                <a class="linkbtn" href="#/guide">还没填“我的情况”？去新手指南 <qw-icon name="chevronRight" :size="14"/></a></div>
            </li>
            <li>
              <span class="n">2</span>
              <div><b>每周看“量化选股”，每晚看“明日计划”</b><p>量化选股每周最后一个交易日收盘后给出下周要持有的 50 只和下单清单（样本外扣费后每年比同池随机多约 14%，但不是保证）；
                明日计划告诉你持仓明天怎么做、要在券商 App 设的止损条件单。</p>
                <a class="linkbtn" href="#/mf">去看量化选股 <qw-icon name="chevronRight" :size="14"/></a>
                <a class="linkbtn" href="#/trade?tab=nightly">明日计划 <qw-icon name="chevronRight" :size="14"/></a></div>
            </li>
            <li>
              <span class="n">3</span>
              <div><b>先用模拟盘练，看复盘再说真钱</b><p>在「交易」建模拟账户按计划下单，或在「策略中心」开启模拟跟踪；过一两个月看「复盘」里的平均 R 和“守纪律”比例，满意再小仓位实盘。
                要记住：选股器里的条件方案和经典公式验证下来都没有跑赢随机；目前只有“量化选股”在样本外显著跑赢，也要先小资金或模拟跟踪。</p>
                <a class="linkbtn" href="#/strategy">打开策略中心 <qw-icon name="chevronRight" :size="14"/></a></div>
            </li>
          </ol>
        </div>

        <div v-if="idxList.length" class="dash-indexes">
          <div v-for="ix in idxList" :key="ix.code" class="dash-index">
            <div class="nm">{{ ix.name }}</div>
            <div class="px num" :class="$fmt.dir(ix.pct)">{{ $fmt.num(ix.price, 2) }}</div>
            <div class="ch num"><qw-price :value="ix.change" change/><qw-price :value="ix.pct" pct/></div>
          </div>
        </div>

        <qw-regime-card/>
        <qw-trade-summary/>

        <qw-card v-if="!s">
          <qw-empty icon="thermo" title="情绪数据还没准备好" :desc="panelEmpty ? '下载完全市场数据后，这里会显示情绪温度和关键数字。' : '可能是本地数据还没更新，点右上角“一键更新”试试。'"/>
        </qw-card>
        <div v-else class="grid dash-row1">
          <qw-card title="情绪温度" icon="thermo" :help="HELP.temp">
            <template #extra><span class="muted" style="font-size:12px" v-if="asOf">{{ $fmt.datetime(asOf) }}</span></template>
            <div class="dash-temp">
              <qw-gauge v-if="s.temperature != null" :value="s.temperature" :label="s.label" height="220px"/>
              <p class="dash-temp-sum">{{ summary }}</p>
            </div>
          </qw-card>
          <qw-card title="今日短线关键数字" icon="flame" sub="点“?”看每个数字的意思">
            <div class="dash-kpis">
              <qw-stat v-for="k in kpis" :key="k.label" :label="k.label" :value="k.value" :unit="k.unit" :tone="k.tone" :help="k.help" :delta="k.delta" :delta-text="k.deltaText" :sub="k.sub" flat/>
            </div>
            <div v-if="secondary.length" class="dash-sec">
              <div v-for="x in secondary" :key="x.k"><div class="k">{{ x.k }}<qw-help :text="x.help"/></div><div class="v num" :class="x.cls">{{ x.v }}</div><div class="s" v-if="x.s">{{ x.s }}</div></div>
            </div>
            <div v-if="breadth" class="dash-breadth">
              <div class="dash-breadth-bar" v-tip="'上涨 ' + breadth.up + ' 家 · 平盘 ' + breadth.flat + ' 家 · 下跌 ' + breadth.down + ' 家'">
                <i :style="{width: breadth.upP + '%', background: 'var(--up)'}"></i>
                <i :style="{width: breadth.flatP + '%', background: 'var(--g3)'}"></i>
                <i :style="{width: breadth.downP + '%', background: 'var(--down)'}"></i>
              </div>
              <div class="dash-breadth-lbl">
                <span class="up">▲ 上涨 {{ breadth.up }} 家</span>
                <span class="muted" v-if="breadth.flat">平盘 {{ breadth.flat }} 家</span>
                <span class="down">▼ 下跌 {{ breadth.down }} 家</span>
              </div>
            </div>
          </qw-card>
        </div>

        <qw-card title="连板天梯" icon="ladder" :help="HELP.ladder" :sub="ladderTotal ? '今日涨停 ' + ladderTotal + ' 只，点名字看盘' : ''">
          <template #extra>
            <button v-if="hlInd" class="chip active" @click="hlInd = ''" v-tip="'取消行业高亮'">{{ $fmt.industry(hlInd) }} ×</button>
            <a class="btn sm soft" href="#/predict?kind=streak" v-tip="'连板晋级名单：看谁明天更可能继续涨停。它是观察名单，历史上照着买是亏的，不是买入信号。'"><qw-icon name="rocket" :size="14"/>谁可能晋级？（观察）</a>
          </template>
          <div v-if="ladder.length" class="ladder">
            <div v-for="col in ladder" :key="col.streak" class="ladder-col" :class="{first: col.streak <= 1}">
              <div class="ladder-hd"><qw-streak :n="col.streak"/><span class="cnt">{{ col.stocks.length }} 只</span></div>
              <div class="ladder-bd">
                <a v-for="st in col.stocks" :key="st.code" class="ladder-chip" :class="chipCls(st)" :href="'#/watch/' + st.code" v-tip="chipTip(st)">
                  <span class="nm">{{ st.name }}</span><span v-if="st.one_word" class="yz">一字</span>
                </a>
              </div>
            </div>
          </div>
          <qw-empty v-else compact icon="ladder" title="今天还没有涨停的股票" desc="开盘后这里会实时出现涨停股，按连板天数排好队。"/>
        </qw-card>

        <div class="grid dash-row2">
          <qw-card title="涨停行业分布" icon="bars" :help="HELP.ind" sub="点击柱子高亮天梯">
            <qw-chart v-if="industries.length" :option="indOption" :height="indHeight" @chart-click="onIndClick"/>
            <qw-empty v-else compact title="暂无行业数据"/>
          </qw-card>
          <qw-card title="近60日情绪走势" icon="trend" :help="HELP.hist">
            <div v-if="histCharts.length" class="hist-grid">
              <div v-for="h in histCharts" :key="h.key" class="hist-cell">
                <div class="t"><span>{{ h.title }}</span><b class="num">{{ h.latest }}</b></div>
                <qw-chart :option="h.option" height="140px" group="dash-hist"/>
              </div>
            </div>
            <qw-empty v-else compact title="历史数据不足" desc="下载历史数据后会显示近60个交易日的走势。"/>
          </qw-card>
        </div>
        <div v-if="ov.warnings && ov.warnings.length" class="qw-banner warn">
          <qw-icon name="alert" :size="18"/>
          <div class="qw-banner-body"><div v-for="(w, i) in ov.warnings" :key="i">{{ w }}</div></div>
        </div>
      </template>
    </div>`,
  });
})();
