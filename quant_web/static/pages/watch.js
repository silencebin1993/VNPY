/* 看盘 #/watch/:code?：无代码时显示搜索 + 最近看过/自选/今日热门；有代码时显示行情、K线/分时、AI评估、公司资料等 */
(function () {
  "use strict";
  const { ref, reactive, computed, watch, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, usePoll, colors, tooltipBase, axisBase, isNum, setTitle, recent } = QW;

  const HELP = {
    turn: "换手率：今天成交的股数占流通股的比例。越高说明买卖越活跃，短线资金越多。",
    amp: "振幅：今天最高价和最低价之间差了多少（相对昨收）。越大说明波动越剧烈。",
    pe: "市盈率：股价 ÷ 每股收益，大致表示“按现在的盈利，多少年能回本”。负数表示公司在亏损。",
    pb: "市净率：股价 ÷ 每股净资产。小于1表示股价比账面资产还便宜。",
    fcap: "流通市值：能在市场上自由买卖的股票总价值。越小的股票越容易被资金拉动。",
    vr: "量比：今天的成交量和过去5天平均成交量相比。大于1说明今天比平时活跃。",
    lu: "涨停价/跌停价：今天允许的最高价和最低价。主板±10%，创业板/科创板±20%，北交所±30%，ST股±5%。",
    ai: "模型根据历史上类似情况，估算这只股票明天收盘涨停的可能性（连板晋级、首板潜力），或明天开盘买入、按计划卖出的预期收益（强势股波段）。它只是参考，不是保证。",
    roe: "净资产收益率：公司用股东的钱赚钱的效率，越高越好（报告期累计值）。",
    lhb: "龙虎榜：股票当天涨跌异常时，交易所公布买卖最多的5家营业部。净买入为正说明大资金在买。",
    flow: "主力资金：大单和超大单的净买入金额，红色表示大资金在买，绿色表示在卖。第三方估算，仅供参考。",
    qfq: "前复权：把分红送股造成的价格跳空抹平，方便看连续走势；涨停标记按当天真实价格判断。",
    lhist: "近一年这只股票每次涨停的日期和当时是第几个连板。",
  };
  const KIND_LABEL = { swing: "强势股波段", streak: "连板晋级", first: "首板潜力" };

  // ------------------------------------------------------------------ 无代码：搜索首页
  const WatchHome = {
    setup() {
      const recents = ref(recent.list());
      const hot = ref([]);
      const hotLoading = ref(true);
      onMounted(async () => {
        QW.watch.refresh();
        try {
          const ov = await api.get("/api/market/overview", null, { silent: true });
          const list = [];
          (ov.ladder || []).slice().sort((a, b) => b.streak - a.streak).forEach((l) => (l.stocks || []).forEach((s) => list.push({ ...s, streak: l.streak })));
          hot.value = list.slice(0, 18);
        } catch (e) { hot.value = []; } finally { hotLoading.value = false; }
      });
      usePoll(() => QW.watch.refresh(), 5000);
      const clearRecent = () => { QW.safeStore.set("qw-recent", "[]"); recents.value = []; };
      return { recents, hot, hotLoading, clearRecent, store };
    },
    template: `<div class="watch-home stack">
      <div class="watch-hero">
        <h2>想看哪只股票？</h2>
        <p>输入 6 位代码、股票名称或拼音首字母，例如 600519、茅台、gzmt</p>
        <qw-stock-search size="lg" autofocus/>
      </div>
      <qw-card title="最近看过" icon="history">
        <template #extra><button v-if="recents.length" class="linkbtn" @click="clearRecent">清空</button></template>
        <div v-if="recents.length" class="chips-wrap"><a v-for="r in recents" :key="r.code" class="chip" :href="'#/watch/' + r.code">{{ r.name }} <span class="muted">{{ r.code }}</span></a></div>
        <qw-empty v-else compact icon="history" title="还没有看过的股票" desc="搜索一只股票看看，会自动记在这里。"/>
      </qw-card>
      <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(300px, 1fr))">
        <qw-card title="我的自选" icon="star" :pad="false">
          <template #extra><a class="linkbtn" href="#/watchlist">管理自选 <qw-icon name="chevronRight" :size="14"/></a></template>
          <qw-table v-if="store.watchList.length" :rows="store.watchList" dense clickable @row-click="(r) => $go('/watch/' + r.code)"
            :columns="[{key:'name',label:'名称'},{key:'price',label:'现价',align:'right'},{key:'pct',label:'涨跌幅',align:'right',sortable:true}]">
            <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
            <template #cell-price="{row}"><qw-price :value="row.price" :ref-value="row.prev_close"/></template>
            <template #cell-pct="{row}"><qw-price :value="row.pct" pct/></template>
          </qw-table>
          <qw-empty v-else compact icon="star" title="还没有自选股" desc="在看盘页点“加自选”，常看的股票就会出现在这里。"/>
        </qw-card>
        <qw-card title="今日涨停热门" icon="flame" sub="按连板天数排序">
          <qw-skeleton v-if="hotLoading" :rows="3"/>
          <div v-else-if="hot.length" class="chips-wrap">
            <a v-for="s in hot" :key="s.code" class="chip" :href="'#/watch/' + s.code"><qw-streak :n="s.streak"/>{{ s.name }}</a>
          </div>
          <qw-empty v-else compact icon="flame" title="暂无涨停数据" desc="更新数据后会显示今天涨停的股票。"/>
        </qw-card>
      </div>
    </div>`,
  };

  // ------------------------------------------------------------------ 有代码：个股详情
  const WatchStock = {
    props: { code: String },
    setup(props) {
      const code = props.code;
      const quote = ref(null);
      const notFound = ref("");
      const profile = ref(null);
      const profileErr = ref("");
      const profileLoading = ref(true);
      const tab = ref(["交易中", "午间休市"].includes(store.phase) ? "minute" : "day");
      const klines = reactive({ day: null, week: null, month: null });
      const klineErr = reactive({ day: "", week: "", month: "" });
      const minute = ref(null);
      const minuteErr = ref("");
      const minute5 = ref(null);
      const minute5Err = ref("");
      // 第三版：指标选择（记在浏览器里）+ 筹码分布
      const readList = (k, d) => {
        try { const v = JSON.parse(localStorage.getItem(k)); return Array.isArray(v) && v.every((x) => typeof x === "string") ? v : d; } catch (e) { return d; }
      };
      // 第三版：个股诊断（主力阶段 + 排雷）
      const diag = ref(null);
      const diagErr = ref("");
      const diagLoading = ref(true);
      const loadDiag = async () => {
        try { diag.value = await api.get(`/api/stock/${code}/diagnosis`, null, { silent: true }); diagErr.value = ""; }
        catch (e) { diagErr.value = e.detail || e.message; } finally { diagLoading.value = false; }
      };
      const indMain = ref(readList("qw-ind-main", ["ma"]));
      const indSubs = ref(readList("qw-ind-subs", ["vol", "macd"]));
      const indCatalog = ref(QW.indCatalog || []);
      const pickerOpen = ref(false);
      const showCost = ref(false);
      const loadCatalog = async () => {
        if (QW.indCatalog) { indCatalog.value = QW.indCatalog; return; }
        try {
          const r = await api.get("/api/indicators/catalog", null, { silent: true });
          QW.indCatalog = r.items || [];
          indCatalog.value = QW.indCatalog;
        } catch (e) { /* 目录取不到时只是不能换指标 */ }
      };

      const loadQuote = async () => {
        try {
          const q = await api.get(`/api/stock/${code}/quote`, null, { silent: true });
          quote.value = q;
          notFound.value = "";
          if (q && q.name) { setTitle(`看盘 · ${q.name}`); recent.add(code, q.name); }
        } catch (e) {
          if (!quote.value) notFound.value = e.detail || e.message;
        }
      };
      const loadProfile = async () => {
        try {
          profile.value = await api.get(`/api/stock/${code}/profile`, null, { silent: true });
          profileErr.value = "";
          const p = profile.value && profile.value.prediction;
          if (p && p.kind === "swing" && !(p.gate && typeof p.gate === "object")) loadGate();
        } catch (e) { profileErr.value = e.detail || e.message; } finally { profileLoading.value = false; }
      };
      // 波段候选：今天的闸门（操作 / 不操作）。个股资料里没有时，取“短线预测”的波段名单（服务端有 5 分钟缓存）
      const gateData = ref(null);
      const loadGate = async () => {
        try {
          const d = await api.get("/api/predict/today", { kind: "swing" }, { silent: true });
          gateData.value = d && d.gate && typeof d.gate === "object" ? d.gate : null;
        } catch (e) { gateData.value = null; }
      };
      // K 线 + 所选指标 + 筹码（日线）一次取回（/api/stock/{code}/chart，字段兼容原来的 /kline）
      const loadKline = async (period) => {
        const count = period === "day" ? 600 : period === "week" ? 400 : 240;
        const ind = [...new Set([...indMain.value, ...indSubs.value])].join(",");
        try {
          klines[period] = await api.get(`/api/stock/${code}/chart`, { period, count, ind, chips: period === "day" }, { silent: true });
          klineErr[period] = "";
        } catch (e) { klineErr[period] = e.detail || e.message; }
      };
      let indTimer = null;
      watch([indMain, indSubs], ([m, s]) => {
        try { localStorage.setItem("qw-ind-main", JSON.stringify(m)); localStorage.setItem("qw-ind-subs", JSON.stringify(s)); } catch (e) { /* 忽略 */ }
        clearTimeout(indTimer);
        indTimer = setTimeout(() => {
          ["day", "week", "month"].forEach((p) => { if (p !== tab.value && p !== "day") klines[p] = null; });
          if (["day", "week", "month"].includes(tab.value)) loadKline(tab.value);
          if (tab.value !== "day") loadKline("day");
        }, 250);
      });
      onBeforeUnmount(() => clearTimeout(indTimer));
      const dayChips = computed(() => (klines.day && klines.day.chips && klines.day.chips.dist && klines.day.chips.dist.prices.length ? klines.day.chips : null));
      // 筹码的白话解读
      const chipsText = computed(() => {
        const ch = dayChips.value;
        if (!ch) return [];
        const l = ch.last || {};
        const px = isNum(q.value.price) ? q.value.price : null;
        const out = [];
        if (isNum(l.winner)) {
          const w = Math.round(l.winner * 100);
          if (l.winner >= 0.9) out.push(`约 ${w}% 的持股人处于盈利状态，获利盘很多，继续上涨时可能有人兑现利润。`);
          else if (l.winner <= 0.1) out.push(`只有约 ${w}% 的持股人在赚钱，大部分人被套；股价反弹到平均成本附近时，容易遇到“解套就卖”的抛压。`);
          else out.push(`约 ${w}% 的持股人处于盈利状态。`);
        }
        if (isNum(l.avg_cost) && px) {
          const d = px / l.avg_cost - 1;
          out.push(`现价${d >= 0 ? "高于" : "低于"}估算的平均成本 ${Math.abs(d * 100).toFixed(1)}%。`);
        }
        if (isNum(l.conc90)) {
          if (l.conc90 < 0.1) out.push("筹码高度集中：90% 的筹码挤在很窄的价格范围里，常见于长期横盘或主力控盘。");
          else if (l.conc90 > 0.3) out.push("筹码比较分散：持股人的成本差别很大。");
        }
        return out;
      });

      const loadMinute = async () => {
        try {
          minute.value = await api.get(`/api/stock/${code}/minute`, null, { silent: true });
          minuteErr.value = "";
        } catch (e) { minuteErr.value = e.detail || e.message; }
      };
      const loadMinute5 = async () => {
        try {
          minute5.value = await api.get(`/api/stock/${code}/minute`, { days: 5 }, { silent: true });
          minute5Err.value = "";
        } catch (e) { minute5Err.value = e.detail || e.message; }
      };
      const isMinuteTab = (t) => t === "minute" || t === "minute5";
      const ensureTab = (t) => {
        if (t === "minute") { if (!minute.value) loadMinute(); } else if (t === "minute5") { if (!minute5.value) loadMinute5(); } else if (!klines[t]) loadKline(t);
      };
      watch(tab, ensureTab);
      const userPicked = ref(false);
      const tabModel = computed({ get: () => tab.value, set: (v) => { userPicked.value = true; tab.value = v; } });
      watch(() => store.phase, (ph) => {
        if (!userPicked.value && ["交易中", "午间休市"].includes(ph)) tab.value = "minute";
      });
      onMounted(() => {
        loadQuote(); loadProfile(); ensureTab(tab.value); loadCatalog(); loadDiag();
        if (tab.value !== "day") setTimeout(() => { if (!klines.day) loadKline("day"); }, 600);   // 筹码卡片需要日线
      });
      let pollN = 0;
      usePoll(() => {
        loadQuote();
        if (tab.value === "minute") loadMinute();
        if (tab.value === "minute5" && ++pollN % 6 === 0) loadMinute5();     // 五日分时 30 秒刷新一次
      }, 5000);
      const offDone = bus.on("job-done", () => { loadQuote(); loadProfile(); loadDiag(); if (!isMinuteTab(tab.value)) loadKline(tab.value); });
      onBeforeUnmount(offDone);

      const q = computed(() => quote.value || {});
      const name = computed(() => q.value.name || (profile.value && profile.value.name) || code);
      const board = computed(() => (profile.value && profile.value.board) || boardOf(code));
      const isST = computed(() => /ST/i.test(name.value || ""));
      const dir = computed(() => fmt.dir(isNum(q.value.price) && isNum(q.value.prev_close) ? q.value.price - q.value.prev_close : q.value.pct));
      const isLimitUp = computed(() => isNum(q.value.price) && isNum(q.value.limit_up) && q.value.price >= q.value.limit_up - 0.005);
      const isLimitDown = computed(() => isNum(q.value.price) && isNum(q.value.limit_down) && q.value.price <= q.value.limit_down + 0.005);
      const grid = computed(() => {
        const x = q.value;
        const pc = x.prev_close;
        const amp = isNum(x.amplitude) ? x.amplitude / 100 : isNum(x.high) && isNum(x.low) && pc ? (x.high - x.low) / pc : null;
        const items = [
          { k: "今开", v: fmt.price(x.open), cls: fmt.dir(x.open - pc) },
          { k: "最高", v: fmt.price(x.high), cls: fmt.dir(x.high - pc) },
          { k: "最低", v: fmt.price(x.low), cls: fmt.dir(x.low - pc) },
          { k: "昨收", v: fmt.price(pc) },
          { k: "成交量", v: fmt.vol(x.volume) },
          { k: "成交额", v: fmt.money(x.amount) },
          { k: "换手率", v: isNum(x.turnover) ? x.turnover.toFixed(2) + "%" : "—", help: HELP.turn },
          { k: "振幅", v: isNum(amp) ? fmt.ratio(amp, 2) : "—", help: HELP.amp },
          { k: "市盈率", v: isNum(x.pe) ? (x.pe < 0 ? "亏损" : x.pe.toFixed(1)) : "—", help: HELP.pe },
          { k: "流通市值", v: fmt.money(x.float_cap), help: HELP.fcap },
          { k: "总市值", v: fmt.money(x.total_cap) },
        ];
        if (isNum(x.volume_ratio)) items.push({ k: "量比", v: x.volume_ratio.toFixed(2), help: HELP.vr });
        const pb = isNum(x.pb) ? x.pb : profile.value && profile.value.valuation ? profile.value.valuation.pb : null;
        if (isNum(pb)) items.push({ k: "市净率", v: pb.toFixed(2), help: HELP.pb });
        return items.slice(0, 12);
      });

      const book = computed(() => {
        const x = q.value;
        const asks = (x.asks || []).slice(0, 5);
        const bids = (x.bids || []).slice(0, 5);
        if (!asks.length && !bids.length) return null;
        const pad = (arr) => Array.from({ length: 5 }, (_, i) => arr[i] || [null, null]);
        const hasAsk = asks.some((a) => isNum(a[0]) && a[0] > 0 && a[1] > 0);
        const hasBid = bids.some((b) => isNum(b[0]) && b[0] > 0 && b[1] > 0);
        // 封板说明：涨停时卖盘为空、买一排队；跌停时买盘为空、卖一排队
        let seal = null;
        if (isLimitUp.value && !hasAsk && hasBid) seal = { up: true, v: bids[0][1], amt: bids[0][0] * bids[0][1] };
        else if (isLimitDown.value && !hasBid && hasAsk) seal = { up: false, v: asks[0][1], amt: asks[0][0] * asks[0][1] };
        return { asks: pad(asks).map((a, i) => ({ lb: "卖" + (i + 1), p: a[0], v: a[1] })).reverse(), bids: pad(bids).map((b, i) => ({ lb: "买" + (i + 1), p: b[0], v: b[1] })), seal };
      });
      const isOneWord = computed(() => {
        const x = q.value;
        return isLimitUp.value && isNum(x.open) && isNum(x.low) && x.open >= x.limit_up - 0.005 && x.low >= x.limit_up - 0.005;
      });
      const kl = computed(() => klines[tab.value] || null);
      const inWatch = computed(() => store.watchCodes.includes(code));
      const toggleWatch = () => QW.watch.toggle(code, name.value).catch(() => {});

      const tabs = [
        { value: "minute", label: "分时" }, { value: "minute5", label: "五日" },
        { value: "day", label: "日K" }, { value: "week", label: "周K" }, { value: "month", label: "月K" },
      ];
      const chartH = computed(() => (store.isPhone ? "340px" : "460px"));
      const fiveOpt = computed(() => (minute5.value && Array.isArray(minute5.value.days) && minute5.value.days.length ? fiveDayOption(minute5.value, chartH.value) : null));

      // AI 评估
      const pred = computed(() => {
        const p = profile.value && profile.value.prediction;
        if (!p) return null;
        if (p.code || isNum(p.prob) || isNum(p.score)) return p;
        const one = p.streak || p.first;
        return one ? { ...one, kind: one.kind || (p.streak ? "streak" : "first") } : null;
      });
      const predScore = computed(() => (pred.value ? (isNum(pred.value.score) ? pred.value.score : pred.value.prob) : null));
      // 消息面加分（后端 score_no_news = 不含消息面的综合概率）
      const newsBoost = computed(() => {
        const p = pred.value;
        if (!p || !isNum(p.score) || !isNum(p.score_no_news)) return null;
        const d = p.score - p.score_no_news;
        return Math.abs(d) >= 0.005 ? d : null;
      });
      const swingGate = computed(() => {
        const p = pred.value;
        if (!p || p.kind !== "swing") return null;
        const g = p.gate && typeof p.gate === "object" ? p.gate : gateData.value;
        if (!g || typeof g.trade !== "boolean") return null;
        const thv = isNum(g.threshold) ? fmt.frac(g.threshold, 0.5) : 0.01;
        const th = +(thv * 100).toFixed(2) + "%";
        const n = isNum(g.n_picks) ? g.n_picks : null;
        if (g.trade) {
          return { trade: true, text: `今天波段可以操作${n ? `（${n} 只达标）` : ""}：` + (p.pick ? "这只是其中之一，会记进模拟盘跟踪（不用真钱）。" : "这只没达标，按计划不买。") };
        }
        return { trade: false, text: `今天波段不操作：没有预期收益 ≥ ${th} 的股票，这只也不买，模拟盘记为“不操作”。` };
      });
      // 名单徽章：说清是哪个名单、是“观察”还是“模拟跟踪”，别让新手以为“在名单里”就是推荐买
      const pillText = computed(() => {
        const p = pred.value;
        if (!p) return "";
        const rk = p.rank ? ` 第 ${p.rank} 名` : "";
        if (p.kind === "swing") return `波段候选${rk}` + (p.pick ? " · 达标" : " · 今天不买");
        return `${KIND_LABEL[p.kind] || "预测"}${rk} · 仅观察`;
      });
      const pillTip = computed(() => {
        const p = pred.value;
        if (!p) return "";
        if (p.kind === "swing") return p.pick ? "预期收益达到了门槛，会记进模拟盘跟踪（不用真钱）。它还在检验阶段，不建议直接用真钱。" : "它是今天的波段候选，但预期收益没达到门槛，按计划今天不买。";
        return "连板晋级、首板潜力只是观察名单：能看出谁更可能涨停，但历史上照着买是亏的，不是买入信号。";
      });
      // 休市/收盘后显示的是哪天的行情
      const quoteWhen = computed(() => {
        const t = q.value.time;
        if (!t) return "";
        const d = String(t).slice(0, 10);
        if (/^\d{4}-\d{2}-\d{2}$/.test(d) && store.today && d !== store.today) return `行情 ${fmt.cnDate(d)} ${fmt.time(t)}`;
        return `更新于 ${fmt.time(t)}`;
      });
      const reasonCls = (r) => (/↑|提高|有利/.test(r) ? "pos" : /↓|降低|不利/.test(r) ? "neg" : "");
      const plainReason = (t) => String(t || "").replace(/^振幅 0(\.0+)?%/, "$&（一字板，全天没打开）");   // “振幅 0.0%”就是一字板
      const reasonIcon = (r) => (reasonCls(r) === "pos" ? "▲" : reasonCls(r) === "neg" ? "▼" : "•");

      // 公司资料
      const pf = computed(() => profile.value || {});
      const fin = computed(() => pf.value.finance || null);
      const val = computed(() => pf.value.valuation || {});
      const conceptsOpen = ref(false);
      const concepts = computed(() => {
        const list = pf.value.concepts || [];
        return conceptsOpen.value ? list : list.slice(0, 10);
      });
      const errText = computed(() => {
        const e = pf.value.errors || {};
        const names = { fund_flow: "资金流", lhb: "龙虎榜", news: "新闻", finance: "财务", concepts: "概念", valuation: "估值" };
        return Object.keys(e).map((k) => `${names[k] || k}：${e[k]}`).join("；");
      });
      const lhist = computed(() => (pf.value.limit_history || []).slice().sort((a, b) => (a.date < b.date ? 1 : -1)));
      const lhistSummary = computed(() => {
        const l = lhist.value;
        return { count: l.length, max: l.reduce((a, x) => Math.max(a, x.streak || 0), 0), oneWord: l.filter((x) => x.one_word).length };
      });
      const flowOption = computed(() => {
        const list = (pf.value.fund_flow || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));
        if (!list.length) return null;
        const c = colors();
        const ax = axisBase(c);
        return {
          animation: false, textStyle: { fontFamily: c.font },
          grid: { left: 4, right: 8, top: 8, bottom: 22, containLabel: true },
          tooltip: { ...tooltipBase(c), trigger: "axis", formatter: (ps) => `${list[ps[0].dataIndex].date}<br>主力${ps[0].value >= 0 ? "净买入" : "净卖出"} <b>${fmt.money(Math.abs(ps[0].value))}</b>` },
          xAxis: { type: "category", data: list.map((x) => x.date), ...ax, axisLabel: { ...ax.axisLabel, formatter: (v) => String(v).slice(5), hideOverlap: true } },
          yAxis: { type: "value", splitNumber: 3, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => fmt.money(v, 0) } },
          series: [{ type: "bar", data: list.map((x) => ({ value: x.main_net, itemStyle: { color: x.main_net >= 0 ? c.up : c.down, borderRadius: 2 } })), barMaxWidth: 12 }],
        };
      });
      const retryAll = () => { notFound.value = ""; loadQuote(); loadProfile(); ensureTab(tab.value); };

      return {
        code, store, fmt, HELP, KIND_LABEL, quote, notFound, profile, profileErr, profileLoading, tab, tabs, klines, klineErr, minute, minuteErr,
        tabModel, book, kl, q, name, board, isST, dir, isLimitUp, isLimitDown, grid, inWatch, toggleWatch, chartH, pred, predScore, reasonCls, reasonIcon,
        pf, fin, val, concepts, conceptsOpen, errText, lhist, lhistSummary, flowOption, retryAll, loadKline, loadMinute, isNum,
        minute5, minute5Err, loadMinute5, fiveOpt, isOneWord, newsBoost, ONE_WORD_TIP: QW.ONE_WORD_TIP, pillText, pillTip, quoteWhen, plainReason, swingGate,
        indMain, indSubs, indCatalog, pickerOpen, showCost, dayChips, chipsText, CHIPS_HELP: QW.term ? QW.term("筹码分布") : "",
        diag, diagErr, diagLoading, loadDiag,
      };
    },
    template: `<div>
      <qw-card v-if="notFound && !quote">
        <qw-empty icon="search" title="没有找到这只股票" :desc="'代码 ' + code + '：' + notFound">
          <button class="btn" @click="retryAll">再试一次</button><a class="btn primary" href="#/watch">重新搜索</a>
        </qw-empty>
      </qw-card>
      <div v-else>
      <div class="watch-top">
        <div class="watch-top-l">
          <qw-card>
            <div v-if="!quote"><qw-skeleton :rows="4"/></div>
            <template v-else>
              <div class="watch-head">
                <div style="min-width:0">
                  <div class="watch-name">
                    <h2>{{ name }}</h2><span class="code num">{{ code }}</span><qw-board :board="board"/>
                    <span v-if="isST" class="qw-tag warn" v-tip="'ST：公司有退市风险或财务异常，被交易所特别处理，涨跌幅限制更严。'">风险警示</span>
                    <span v-if="pf.industry" class="qw-tag" v-tip="pf.industry">{{ $fmt.industry(pf.industry) }}</span>
                    <span v-if="isLimitUp" class="qw-tag red">▲ 今日涨停</span><span v-if="isOneWord" class="qw-tag red" v-tip="ONE_WORD_TIP" style="cursor:help">一字板 ?</span><span v-if="isLimitDown" class="qw-tag green">▼ 今日跌停</span>
                  </div>
                  <div class="watch-price">
                    <span class="big num" :class="dir">{{ $fmt.price(q.price) }}</span>
                    <span class="chg num"><qw-price :value="q.change" change arrow/><qw-price :value="q.pct" pct/></span>
                  </div>
                  <div class="watch-limits">
                    <span>涨停价 <b class="up num">{{ $fmt.price(q.limit_up) }}</b></span>
                    <span>跌停价 <b class="down num">{{ $fmt.price(q.limit_down) }}</b></span><qw-help :text="HELP.lu"/>
                    <span class="muted" v-if="q.time" v-tip="store.phase === '交易中' ? '' : '现在不是交易时间，显示的是最近一次的行情'">{{ quoteWhen }}<template v-if="store.phase === '交易中'"> · 每5秒刷新</template></span>
                  </div>
                </div>
                <div class="watch-head-actions">
                  <button class="btn" :class="{'active-star': inWatch}" @click="toggleWatch"><qw-icon :name="inWatch ? 'starFill' : 'star'" :size="15"/>{{ inWatch ? '已自选' : '加自选' }}</button>
                  <a v-if="pred && pred.in_list !== false" class="btn soft watch-pred-pill" :class="{obs: pred.kind !== 'swing' || !pred.pick}" :href="'#/predict?kind=' + (pred.kind || 'streak')" v-tip="pillTip"><qw-icon :name="pred.kind === 'swing' ? 'trend' : 'sparkles'" :size="15"/>{{ pillText }}</a>
                </div>
              </div>
              <div class="quote-grid">
                <div v-for="g in grid" :key="g.k"><div class="k">{{ g.k }}<qw-help v-if="g.help" :text="g.help"/></div><div class="v num" :class="g.cls">{{ g.v }}</div></div>
              </div>
            </template>
          </qw-card>

          <qw-card :pad="false">
            <div style="padding:6px 16px 0"><qw-tabs v-model="tabModel" :items="tabs"/></div>
            <div style="padding:10px 8px 4px">
              <template v-if="tab === 'minute'">
                <qw-minute v-if="minute && minute.points && minute.points.length" :data="minute" :height="chartH"/>
                <qw-empty v-else-if="minuteErr" compact icon="alert" title="分时数据暂时拿不到" :desc="minuteErr" action-text="重试" @action="loadMinute"/>
                <qw-empty v-else-if="minute" compact icon="clock" title="今天还没有分时数据" desc="开盘（9:30）后这里会显示当天每分钟的价格走势。"/>
                <qw-skeleton v-else :height="chartH"/>
              </template>
              <template v-else-if="tab === 'minute5'">
                <qw-chart v-if="fiveOpt" :option="fiveOpt" :height="chartH"/>
                <qw-empty v-else-if="minute5Err" compact icon="alert" title="五日分时暂时拿不到" :desc="minute5Err" action-text="重试" @action="loadMinute5"/>
                <qw-empty v-else-if="minute5" compact icon="clock" title="没有五日分时数据"/>
                <qw-skeleton v-else :height="chartH"/>
              </template>
              <template v-else>
                <div class="ind-bar">
                  <button class="btn sm" :class="{soft: pickerOpen}" @click="pickerOpen = !pickerOpen"><qw-icon name="sliders" :size="14"/>指标</button>
                  <span class="muted ind-cur">{{ indCatalog.filter((x) => indMain.includes(x.id) || indSubs.includes(x.id)).map((x) => x.name).join(' · ') }}</span>
                  <label v-if="tab === 'day'" class="ind-cost"><input type="checkbox" v-model="showCost">平均成本线<qw-help :text="CHIPS_HELP"/></label>
                </div>
                <qw-ind-picker v-if="pickerOpen" v-model:main="indMain" v-model:subs="indSubs" :catalog="indCatalog"/>
                <qw-kline-pro v-if="klines[tab] && klines[tab].bars && klines[tab].bars.length" :key="tab" :bars="klines[tab].bars" :indicators="klines[tab].indicators || {}"
                  :main="indMain" :subs="indSubs" :chips="klines[tab].chips" :show-cost="showCost && tab === 'day'" :limit-days="klines[tab].limit_days || []" :period="tab"
                  :initial-bars="tab === 'day' ? 120 : 80" :main-height="store.isPhone ? 220 : 300" :sub-height="store.isPhone ? 78 : 96"/>
                <qw-empty v-else-if="klineErr[tab]" compact icon="alert" title="K线数据暂时拿不到" :desc="klineErr[tab]" action-text="重试" @action="loadKline(tab)"/>
                <qw-empty v-else-if="klines[tab]" compact icon="candle" title="没有K线数据"/>
                <qw-skeleton v-else :height="chartH"/>
              </template>
            </div>
            <div class="chart-note" style="padding:0 16px 12px">
              <template v-if="tab === 'day'"><span><i class="mk"></i>红色小三角 = 当天收盘涨停</span></template>
              <template v-if="tab === 'minute5'"><span>最近 5 个交易日每分钟的价格，竖线隔开每一天；虚线 = 5 天前的收盘价，高于它是红色区域</span></template>
              <template v-else-if="tab !== 'minute'">
                <span v-if="!kl || kl.adjust !== ''">价格为前复权 <qw-help :text="HELP.qfq"/></span><span v-else>价格为不复权（本地日线）</span>
                <span>鼠标移到K线上看当天详情；滚轮或双指缩放，拖动查看更早的走势</span>
                <span v-for="(w, i) in (kl && kl.warnings) || []" :key="i" class="qw-tag warn">{{ w }}</span>
              </template>
              <template v-else><span>蓝线 = 价格，黄线 = 当天平均成交价；虚线 = 昨天收盘价，线上方是涨、下方是跌</span></template>
            </div>
          </qw-card>
        </div>
        <div class="watch-top-r">
          <qw-stage-card :diag="diag" :loading="diagLoading" :error="diagErr" @reload="loadDiag"/>
        </div>
      </div>

      <div class="watch-grid">
          <qw-risk-card :risk="diag && diag.risk" :loading="diagLoading"/>
          <qw-card :title="pred && pred.kind === 'swing' ? 'AI波段评估' : pred ? 'AI涨停评估' : 'AI评估'" icon="sparkles" :help="HELP.ai" :loading="profileLoading" skeleton-height="260px">
            <template #extra><span v-if="pred" class="qw-tag">{{ KIND_LABEL[pred.kind] || pred.label || 'AI预测' }}</span></template>
            <template v-if="pred">
              <div v-if="pred.kind === 'swing'" class="ai-prob" :class="{ low: !(pred.exp_ret > 0) }"><span class="big num">{{ $fmt.pct(pred.exp_ret, 2) }}</span><span class="muted">明天开盘买、按计划卖出的预期收益（模拟跟踪中）</span></div>
              <div v-else class="ai-prob" :class="{ low: isNum(pred.base_prob) && isNum(predScore) && predScore < pred.base_prob }"><span class="big num">{{ $fmt.prob(predScore, 1) }}</span><span class="muted">明天收盘涨停的估计概率（仅供观察）</span></div>
              <div class="row muted" style="font-size:12.5px;margin-top:2px;flex-wrap:wrap;gap:4px 8px">
                <span v-if="isNum(pred.base_prob)">今天全部候选平均 {{ $fmt.prob(pred.base_prob, 1) }}</span>
                <span v-if="pred.kind === 'swing' && isNum(pred.base_exp_ret)">今天全部候选平均 {{ $fmt.pct(pred.base_exp_ret, 2) }}</span>
                <span v-if="pred.kind !== 'swing' && pred.prob != null && pred.score != null && Math.abs(pred.prob - pred.score) > 0.0005">· 模型原始概率 {{ $fmt.prob(pred.prob, 1) }}</span>
                <span v-if="pred.rank">· 今日名单第 {{ pred.rank }} 名</span>
                <span v-else-if="pred.in_list && pred.beyond_rows">· 在今日名单里，排在前 {{ pred.beyond_rows }} 名之后</span>
                <span v-if="pred.pick" class="qw-tag" :class="pred.kind === 'swing' ? 'blue' : 'warn'" v-tip="pillTip">{{ pred.kind === 'swing' ? '★ 达标（模拟跟踪）' : '★ 排在最前（仅观察）' }}</span>
              </div>
              <div v-if="pred.kind === 'swing' && swingGate" class="ai-gate" :class="swingGate.trade ? 'go' : 'stop'"><qw-icon :name="swingGate.trade ? 'play' : 'pause'" :size="14"/><span>{{ swingGate.text }} <a href="#/predict?kind=swing">看今天的计划 →</a></span></div>
              <div v-if="pred.kind !== 'swing'" class="ai-obs"><qw-icon name="alert" :size="13"/><span>观察名单：照着买历史上平均是亏的；一字板通常买不进</span></div>
              <div v-if="newsBoost != null && pred.kind !== 'swing'" class="drawer-note" style="margin-top:8px">
                <qw-icon name="news" :size="15"/>
                <span>其中消息面{{ newsBoost > 0 ? '加了' : '减了' }} <b class="num">{{ Math.abs(newsBoost * 100).toFixed(1) }}</b> 个百分点<template v-if="pred.news_count || pred.policy_count">（最近24小时相关消息 {{ pred.news_count ?? 0 }} 条、政策 {{ pred.policy_count ?? 0 }} 条）</template>，不含消息面是 {{ $fmt.prob(pred.score_no_news, 1) }}。消息面没经过历史检验，新闻多也可能是利空。</span>
              </div>
              <div v-if="isOneWord && pred.kind === 'streak'" class="drawer-note warn" style="margin-top:8px"><qw-icon name="alert" :size="15"/><span>今天是一字板：就算明天还涨停，也很可能一开盘就封死，普通人排队买不到。</span></div>
              <div v-if="pred.in_list === false" class="muted" style="font-size:12.5px;margin-top:6px">这只股票是模型的候选，但被你在「预测设置」里的筛选条件（如板块、流通市值、股价）挡在了今天的名单之外。</div>
              <qw-radar :dims="pred.dims" height="250px"/>
              <ul v-if="pred.reasons && pred.reasons.length" class="reasons">
                <li v-for="(r, i) in pred.reasons" :key="i" :class="reasonCls(r)"><span class="ri">{{ reasonIcon(r) }}</span><span>{{ plainReason(r) }}</span></li>
              </ul>
              <div class="row between section-gap" style="margin-top:12px">
                <span class="muted" style="font-size:12px">{{ pred.kind === 'swing' ? '预期收益是历史上类似股票的平均值，只是参考，不是保证。' : '概率来自历史数据，只是参考，不是保证。' }}</span>
                <a class="linkbtn" :href="'#/predict?kind=' + (pred.kind || 'streak')">看完整名单 <qw-icon name="chevronRight" :size="14"/></a>
              </div>
            </template>
            <qw-empty v-else compact icon="sparkles" title="今天不在预测名单中"
              desc="模型只评估三类股票：今天涨了5%以上但没封涨停的强势股（强势股波段）、今天涨停的（看明天能不能连板），以及最近5天没涨停、有机会首次涨停的。这只股票今天不在候选里。">
              <a class="btn sm" href="#/predict">去看预测名单</a>
            </qw-empty>
          </qw-card>

          <qw-card v-if="dayChips" title="筹码分布" icon="layers" :help="CHIPS_HELP" :sub="'截至 ' + dayChips.as_of">
            <div class="chip-stats">
              <div><span class="muted">获利比例</span><b class="num">{{ isNum(dayChips.last.winner) ? Math.round(dayChips.last.winner * 100) + '%' : '—' }}</b></div>
              <div><span class="muted">平均成本</span><b class="num">{{ $fmt.price(dayChips.last.avg_cost) }}</b></div>
              <div><span class="muted">90%筹码区间</span><b class="num">{{ $fmt.price(dayChips.last.p5) }}–{{ $fmt.price(dayChips.last.p95) }}</b></div>
              <div><span class="muted">集中度</span><b class="num">{{ isNum(dayChips.last.conc90) ? (dayChips.last.conc90 * 100).toFixed(1) + '%' : '—' }}</b></div>
            </div>
            <qw-chips :chips="dayChips" :price="isNum(q.price) ? q.price : null" height="280px"/>
            <ul class="chip-read"><li v-for="(t, i) in chipsText" :key="i">{{ t }}</li></ul>
            <div class="muted" style="font-size:12px">红色 = 成本低于现价（获利盘），蓝色 = 成本高于现价（套牢盘）。{{ dayChips.note }}</div>
          </qw-card>

          <qw-card v-if="flowOption" title="主力资金（近20日）" icon="wallet" :help="HELP.flow">
            <qw-chart :option="flowOption" height="180px"/>
            <div class="flow-legend"><span><i style="background:var(--up)"></i>红 = 大资金净买入</span><span><i style="background:var(--down)"></i>绿 = 净卖出</span><span>第三方估算，仅供参考</span></div>
          </qw-card>

          <qw-card v-if="book" title="买卖五档" icon="bars" help="挂单情况：卖1~卖5是最便宜的5档卖单，买1~买5是出价最高的5档买单，数量单位是手（100股）。">
            <div v-if="book.seal" class="book-seal" :class="book.seal.up ? 'up' : 'down'">
              <template v-if="book.seal.up"><b>涨停封板中</b>：没有人卖，买一有 <b class="num">{{ $fmt.int(book.seal.v / 100) }}</b> 手（约 {{ $fmt.money(book.seal.amt) }}）排队等着买。排队的钱越多，封得越牢，散户越难买到。</template>
              <template v-else><b>跌停封板中</b>：没有人买，卖一有 <b class="num">{{ $fmt.int(book.seal.v / 100) }}</b> 手（约 {{ $fmt.money(book.seal.amt) }}）排队等着卖，想卖也卖不掉。</template>
            </div>
            <div class="book-hd"><span>卖盘（价格 · 手）</span><span>买盘（价格 · 手）</span></div>
            <div class="book">
              <div class="book-side"><div v-for="a in book.asks" :key="a.lb"><span class="lb">{{ a.lb }}</span><span class="pr num" :class="$fmt.dir(a.p - q.prev_close)">{{ a.p ? $fmt.price(a.p) : '—' }}</span><span class="vo num">{{ a.v ? $fmt.int(a.v / 100) : '' }}</span></div></div>
              <div class="book-side"><div v-for="b in book.bids" :key="b.lb"><span class="lb">{{ b.lb }}</span><span class="pr num" :class="$fmt.dir(b.p - q.prev_close)">{{ b.p ? $fmt.price(b.p) : '—' }}</span><span class="vo num">{{ b.v ? $fmt.int(b.v / 100) : '' }}</span></div></div>
            </div>
          </qw-card>
          <qw-card title="公司资料" icon="building" :loading="profileLoading">
            <qw-empty v-if="profileErr && !profile" compact icon="alert" title="资料暂时拿不到" :desc="profileErr"/>
            <template v-else>
              <div class="kv">
                <div><div class="k">所属行业</div><div class="v" style="font-size:14px" v-tip="pf.industry">{{ $fmt.industry(pf.industry) }}</div></div>
                <div><div class="k">上市日期</div><div class="v num" style="font-size:14px">{{ $fmt.date(pf.list_date) }}</div></div>
                <div><div class="k">市盈率TTM<qw-help :text="HELP.pe"/></div><div class="v num">{{ val.pe_ttm == null ? '—' : (val.pe_ttm < 0 ? '亏损' : $fmt.num(val.pe_ttm, 1)) }}</div></div>
                <div><div class="k">市净率<qw-help :text="HELP.pb"/></div><div class="v num">{{ $fmt.num(val.pb, 2) }}</div></div>
                <div><div class="k">总市值</div><div class="v num">{{ $fmt.money(val.total_cap) }}</div></div>
                <div><div class="k">流通市值<qw-help :text="HELP.fcap"/></div><div class="v num">{{ $fmt.money(val.float_cap) }}</div></div>
              </div>
              <div v-if="fin" class="section-gap">
                <div class="muted" style="font-size:12px;margin-bottom:6px">最新财报（{{ $fmt.date(fin.report_date) }}，报告期累计）</div>
                <div class="kv">
                  <div><div class="k">营业收入</div><div class="v num">{{ $fmt.money(fin.revenue) }}</div><qw-price v-if="fin.revenue_yoy != null" :value="fin.revenue_yoy" pct :digits="1" style="font-size:12px"/></div>
                  <div><div class="k">净利润</div><div class="v num" :class="fin.net_profit < 0 ? 'down' : ''">{{ $fmt.money(fin.net_profit) }}</div><qw-price v-if="fin.net_profit_yoy != null" :value="fin.net_profit_yoy" pct :digits="1" style="font-size:12px"/></div>
                  <div><div class="k">净资产收益率<qw-help :text="HELP.roe"/></div><div class="v num">{{ fin.roe == null ? '—' : $fmt.pct(fin.roe, 2, false) }}</div></div>
                  <div><div class="k">每股收益</div><div class="v num">{{ fin.eps == null ? '—' : $fmt.num(fin.eps, 3) + '元' }}</div></div>
                </div>
              </div>
              <div v-if="concepts.length" class="section-gap">
                <div class="muted" style="font-size:12px;margin-bottom:6px">相关概念</div>
                <div class="concepts"><span v-for="cp in concepts" :key="cp" class="qw-tag">{{ cp }}</span>
                  <button v-if="(pf.concepts || []).length > 10" class="linkbtn" style="font-size:12px" @click="conceptsOpen = !conceptsOpen">{{ conceptsOpen ? '收起' : '全部 ' + pf.concepts.length + ' 个' }}</button>
                </div>
              </div>
              <div v-if="errText" class="err-note"><qw-icon name="info" :size="13"/>部分资料没拿到（{{ errText }}）</div>
            </template>
          </qw-card>

          <qw-card title="龙虎榜" icon="users" :help="HELP.lhb" sub="最近上榜记录" :loading="profileLoading">
            <ul v-if="(pf.lhb || []).length" class="list-plain">
              <li v-for="(x, i) in pf.lhb.slice(0, 6)" :key="i" style="justify-content:space-between">
                <div style="min-width:0"><div class="num" style="font-weight:600">{{ $fmt.date(x.date) }}</div><div class="muted ellipsis" style="font-size:12px;max-width:190px" v-tip="x.reason">{{ x.reason }}</div></div>
                <div style="text-align:right"><div class="muted" style="font-size:12px">净买入</div><b class="num" :class="$fmt.dir(x.net_buy)">{{ x.net_buy > 0 ? '+' : '' }}{{ $fmt.money(x.net_buy) }}</b></div>
              </li>
            </ul>
            <qw-empty v-else compact icon="users" title="近期没有上龙虎榜" desc="只有涨跌幅、换手率异常时才会上榜。"/>
          </qw-card>


          <qw-card title="涨停记录" icon="flame" :help="HELP.lhist" sub="近一年" :loading="profileLoading">
            <template v-if="lhist.length">
              <div class="lu-summary"><span><b class="num">{{ lhistSummary.count }}</b>次涨停</span><span>最高<b class="num">{{ lhistSummary.max }}</b>连板</span><span v-if="lhistSummary.oneWord">一字<b class="num">{{ lhistSummary.oneWord }}</b>次</span></div>
              <ul class="list-plain lu-hist">
                <li v-for="(x, i) in lhist" :key="i" style="justify-content:space-between;align-items:center"><span class="num">{{ $fmt.date(x.date) }}</span><qw-streak :n="x.streak" :one-word="x.one_word"/></li>
              </ul>
            </template>
            <qw-empty v-else compact icon="flame" title="近一年没有涨停过"/>
          </qw-card>

          <qw-card title="相关新闻" icon="news" :loading="profileLoading">
            <ul v-if="(pf.news || []).length" class="list-plain news-list">
              <li v-for="(n, i) in pf.news.slice(0, 8)" :key="i" style="display:block">
                <a v-if="n.url" :href="n.url" target="_blank" rel="noopener noreferrer">{{ n.title }} <qw-icon name="external" :size="12" style="opacity:.5"/></a>
                <span v-else>{{ n.title }}</span>
                <div class="meta"><span class="num">{{ $fmt.datetime(n.time) }}</span><span>{{ n.source }}</span></div>
              </li>
            </ul>
            <qw-empty v-else compact icon="news" title="暂无相关新闻"/>
          </qw-card>
        </div>
      </div>
    </div>`,
  };

  // 五日分时：5 天的分钟价格首尾相接，竖线分隔每天；基准线 = 第一天的昨收
  function fiveDayOption(d, height) {
    const c = colors();
    const days = d.days || [];
    const base = days[0] && days[0].prev_close;
    const xs = [];
    const prices = [];
    const avgs = [];
    const vols = [];
    const info = [];
    const dayStart = {};
    days.forEach((day) => {
      let last = day.prev_close;
      dayStart[xs.length] = String(day.date || "").slice(5);
      (day.points || []).forEach((p) => {
        xs.push(String(day.date || "").slice(5) + " " + p.time);
        prices.push(p.price);
        avgs.push(isNum(p.avg_price) ? p.avg_price : null);
        vols.push({ value: p.volume, itemStyle: { color: p.price >= last ? c.up : c.down, opacity: 0.75 } });
        info.push({ date: day.date, time: p.time, price: p.price, avg: p.avg_price, vol: p.volume, pc: day.prev_close });
        last = p.price;
      });
    });
    const valid = prices.filter(isNum);
    let dev = isNum(base) ? base * 0.01 : 0.01;
    valid.forEach((v) => { dev = Math.max(dev, Math.abs(v - base)); });
    dev *= 1.08;
    const H = parseInt(height, 10) || 400;
    const top = 30;
    const avail = H - top - 30;
    const h1 = Math.round(avail * 0.72);
    const h2 = avail - h1 - 14;
    const left = 58;
    const right = 12;
    const ax = axisBase(c);
    const isStart = (i) => i in dayStart;
    const row = (k, v, col) => `<div style="display:flex;justify-content:space-between;gap:14px"><span>${k}</span><b style="${col ? "color:" + col : ""}">${v}</b></div>`;
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 2, left: left - 6, itemWidth: 14, itemHeight: 3, icon: "rect", textStyle: { color: c.text2, fontSize: 12 }, data: ["价格", "均价"] },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true,
        axisPointer: { type: "cross", label: { backgroundColor: c.text2, fontSize: 11 }, crossStyle: { color: c.text3 } },
        formatter: (ps) => {
          const x = info[ps[0] && ps[0].dataIndex];
          if (!x) return "";
          const pc = isNum(x.pc) && x.pc ? x.price / x.pc - 1 : null;
          const col = pc > 0 ? c.up : pc < 0 ? c.down : null;
          return `<div style="font-weight:700;margin-bottom:4px">${x.date} ${x.time}</div>` + row("价格", x.price.toFixed(2), col) +
            row("当天涨跌", fmt.ratio(pc, 2, true), col) + (isNum(x.avg) ? row("当天均价", x.avg.toFixed(2)) : "") + row("成交量", fmt.vol(x.vol));
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left, right, top, height: h1 }, { left, right, top: top + h1 + 14, height: h2 }],
      xAxis: [
        {
          type: "category", data: xs, gridIndex: 0, boundaryGap: false, ...ax, axisLabel: { show: false }, axisPointer: { label: { show: false } },
          splitLine: { show: true, interval: (i) => isStart(i) && i > 0, lineStyle: { color: c.axis } },
        },
        {
          type: "category", data: xs, gridIndex: 1, boundaryGap: false, ...ax,
          splitLine: { show: true, interval: (i) => isStart(i) && i > 0, lineStyle: { color: c.axis } },
          axisLabel: { ...ax.axisLabel, interval: (i) => isStart(i), formatter: (v, i) => dayStart[i] || "", align: "left" },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, min: isNum(base) ? base - dev : undefined, max: isNum(base) ? base + dev : undefined, interval: dev / 2, ...ax, axisLine: { show: false },
          axisLabel: { ...ax.axisLabel, color: (v) => (!isNum(base) ? c.text3 : v > base + 1e-6 ? c.up : v < base - 1e-6 ? c.down : c.text3), formatter: (v) => v.toFixed(2), showMinLabel: true, showMaxLabel: true }, axisPointer: QW.AP_PRICE,
        },
        { gridIndex: 1, splitNumber: 2, ...ax, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, showMaxLabel: false, formatter: (v) => fmt.volShort(v) }, axisPointer: QW.AP_VOL },
      ],
      series: [
        {
          name: "价格", type: "line", data: prices, showSymbol: false, lineStyle: { width: 1.6, color: c.series[0] }, itemStyle: { color: c.series[0] },
          areaStyle: { color: c.series[0], opacity: 0.07, origin: "start" },
          markLine: isNum(base) ? {
            symbol: "none", silent: true, lineStyle: { type: "dashed", color: c.text3, width: 1 },
            label: { formatter: "5天前收盘 " + base.toFixed(2), position: "insideEndTop", color: c.text3, fontSize: 11 }, data: [{ yAxis: base }],
          } : undefined,
        },
        { name: "均价", type: "line", data: avgs, showSymbol: false, connectNulls: false, lineStyle: { width: 1.2, color: c.series[3] }, itemStyle: { color: c.series[3] } },
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols, barWidth: "60%" },
      ],
    };
  }

  function boardOf(code) {
    if (/^(300|301)/.test(code)) return "chinext";
    if (/^(688|689)/.test(code)) return "star";
    if (/^(4|8|92)/.test(code)) return "bj";
    return "main";
  }

  QW.page("watch", {
    props: ["params", "query"],
    components: { WatchHome, WatchStock },
    computed: { code() { return ((this.params && this.params.code) || "").trim(); } },
    template: `<watch-stock v-if="code" :code="code"/><watch-home v-else/>`,
  });
})();
