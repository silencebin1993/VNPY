/* 短线预测 #/predict：三个名单（强势股波段=模拟跟踪 / 连板晋级、首板潜力=观察）、每个名单如实的样本外成绩、
   波段的“今天操作不操作”闸门与买卖计划、筛选摘要、名单表格与详情抽屉 */
(function () {
  "use strict";
  const { ref, computed, watch, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, jobs, toast, go, isNum, BOARDS, DIM_HELP, LABELS, KINDS, KIND_VALUES, normTrade, tWord, T_HELP, tHelp, BASE_HELP, MATCH_HELP, TUNED_HELP, fillPct, holdLabel, recentVerdict, HOLD_CAVEAT } = QW;
  // 重训波动的提示：不写研究里的 t 值范围（会和当前模型对不上，还容易让人往好处想），以页面上这次训练的数字为准
  const RETRAIN_T_NOTE = "每次重新训练，成绩都会有些波动（交易笔数不多）。页面上是这一次训练的真实结果，不要往好的方向猜；留出期 t 值不到 2 就还不够可信。";

  const HELP = {
    trust: "“样本外”是指模型训练时没见过的那段历史，用它检验才接近模型在新数据上的表现。尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观。",
    score: "模型估算的明天收盘涨停的概率（已按你在“预测设置”里的五维权重和消息面权重换算）。只用来观察谁更强，照着买历史上是亏的。",
    expRet: "模型估算：明天开盘买入、按计划卖出（扣掉手续费、印花税和滑点）之后，平均能赚百分之几。这是历史上类似股票的平均值，单只股票的实际结果波动很大，亏 5% 以上也很常见。",
    dims: "情绪面、资金面、基本面、题材政策面、技术面五个方面的影响。50分为中性，高于50有利，低于50不利。鼠标移上去看具体分数。",
    streak: "截至今天已经连续涨停了几天。",
    turn: "换手率：今天成交的股数占流通股的比例，越高说明买卖越活跃。",
    fcap: "流通市值：能在市场上自由买卖的股票总价值。盘子越小越容易被资金拉动。",
    mean: "用模型没见过的历史（样本外），每天按名单买入、按规则卖出，平均每一笔赚或亏多少，已扣除手续费、印花税和滑点。",
    base: BASE_HELP,
    match: MATCH_HELP,
    acctMean: "按你在「预测设置」里的本金和仓位，把模型没见过的那段历史真实模拟一遍：每笔佣金最低 5 元、一手 100 股买不起就跳过、已扣印花税和滑点。这个数最接近你真照着做的结果。",
    fill: "头天收盘出名单，第二天开盘去买。如果开盘就已经涨停（多数是一字板）、停牌，或者高开太多，就算“买不进”。这个比例 = 能买进的次数 ÷ 全部信号。" +
      "按你的本金回测时，因为一手太贵（100 股都买不起）或现金不够而没买的不算“买不进”，下面另外列出个数。",
    hit: "模型每天排名前 N 的股票，第二天真的收盘涨停的比例（样本外）。",
    pick: "按分数从高到低排名；带 ★ 的是排在最前面、达到门槛的几只（数量可在设置里改）。",
    news: "消息面只用最近24小时的新闻和政策，没有历史数据，所以没有参与模型训练和回测。消息热度≠利好，默认不参与排序。",
    period: "选择期（2022-01 ~ 2025-06）：研究时比较多种做法、挑出方案的那段历史；" + HOLD_CAVEAT + "留出期比选择期更接近现在的真实水平；行业/ST 用当前归属，也会偏乐观一点。" +
      "（如果你自己看着历史检验结果改过设置，留出期也被你拿来挑设置了，同样会偏乐观。）",
    recent: "先看最近一段：" + HOLD_CAVEAT + "左边是这一段里按规则买卖的平均每笔；右边是同一段时间里、只在模型出手的那些天，从同一批候选股里随便挑同样多只的平均每笔（随机 20 次平均）。" +
      "左边要明显高于右边，才说明模型真的会挑股票。",
    full: "全部样本外 = 选择期 + 留出期。选择期是研究时比较多种做法、挑出方案的那段历史，放在一起算会偏乐观，所以上面的大数字只看最近一段（留出期）。",
  };
  const RISKS = [
    "短线交易风险很高：追涨停、买强势股，第二天大跌非常常见，一天亏 10% 以上并不少见。",
    "“连板晋级”“首板潜力”两个名单能看出谁更可能涨停，但历史上照着买是亏的——只当观察名单，看热点用。",
    "“强势股波段”是研究过的几种做法里历史成绩最不差的，但它并没有被证明能赚钱：最近一段时间（留出期）的成绩要和“同一天随便挑”对比着看（见预测页上方）；目前只在模拟盘里跟踪，不建议直接用真钱。",
    "页面上的成绩都用“样本外”数据计算，尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观；而且过去有效不代表以后有效，市场风格随时会变。",
    "建议先只看不买，或者用很小的仓位学习观察。不要借钱，不要重仓，不要把生活费投进去。",
    "本工具仅供学习研究，不构成任何投资建议，盈亏自负。",
  ];
  const PLAN_KEYS = [
    { keys: ["buy", "买入", "entry"], label: "买入", icon: "wallet" },
    { keys: ["sell", "卖出", "exit"], label: "卖出", icon: "clock" },
    { keys: ["position", "仓位", "size", "pos"], label: "仓位", icon: "target" },
  ];
  const DEFAULT_PLAN = [
    { label: "买入", icon: "wallet", text: "明天（下一个交易日）开盘价买入；如果一开盘就涨停买不进，就放弃，不追。" },
    { label: "卖出", icon: "clock", text: "买入那天算第 1 天；从第 2 天起，哪天收盘没涨停就在当天收盘卖出，最晚第 5 天收盘卖出。" },
    { label: "仓位", icon: "target", text: "每只只用总资金的 4% 左右，同时最多 25 只；先在模拟盘里跟踪，不建议直接用真钱。" },
  ];
  // plan 可能是 ["买入：…","卖出：…","仓位：…"]、"…\n…"、或 {buy, sell, position}
  function planLines(p) {
    if (!p) return DEFAULT_PLAN;
    let arr = null;
    if (Array.isArray(p)) arr = p;
    else if (typeof p === "string") arr = p.split(/\n+/);
    if (arr) {
      const out = arr.map((x) => (typeof x === "string" ? x : x && (x.text || x.value || ""))).filter(Boolean).map((s, i) => {
        const m = String(s).match(/^\s*(买入|卖出|仓位)\s*[:：]\s*(.*)$/);
        const def = PLAN_KEYS.find((k) => m && k.label === m[1]) || PLAN_KEYS[i] || PLAN_KEYS[2];
        return { label: m ? m[1] : def.label, icon: def.icon, text: m ? m[2] : String(s).trim() };
      });
      return out.length ? out : DEFAULT_PLAN;
    }
    if (typeof p === "object") {
      const out = [];
      PLAN_KEYS.forEach((k) => {
        const key = k.keys.find((x) => typeof p[x] === "string" && p[x]);
        if (key) out.push({ label: k.label, icon: k.icon, text: p[key].replace(/^\s*(买入|卖出|仓位)\s*[:：]\s*/, "") });
      });
      return out.length ? out : DEFAULT_PLAN;
    }
    return DEFAULT_PLAN;
  }

  // 门槛百分比文字：0.01 → "1%"，0.015 → "1.5%"
  const thPct = (v) => {
    if (!isNum(v)) return "—";
    const x = +(v * 100).toFixed(2);
    return (Number.isInteger(x) ? String(x) : x.toFixed(1)) + "%";
  };

  QW.page("predict", {
    props: ["params", "query"],
    setup(props) {
      const initKind = props.query && props.query.kind;
      const stored = QW.safeStore.get("qw-pred-kind2");
      const kind = ref(KIND_VALUES.includes(initKind) ? initKind : KIND_VALUES.includes(stored) ? stored : "swing");
      const data = ref(null);
      const loading = ref(true);
      const err = ref("");
      const noModel = ref(false);           // 请求报"未知类型/未训练"：当成还没训练
      const settings = ref(null);
      const riskOpen = ref(false);
      const riskChecked = ref(false);
      const ackBusy = ref(false);
      const drawerOpen = ref(false);
      const cur = ref(null);
      const mini = ref({});
      const report = ref(null);
      const bt = ref(null);
      const acct = ref(null);               // 波段：按用户本金/仓位的回测（和模型检验并列显示）
      const btErr = ref("");
      const btLoading = ref(false);
      const trainJob = ref(null);

      let seq = 0;
      const load = async () => {
        const my = ++seq;
        loading.value = true;
        try {
          const d = await api.get("/api/predict/today", { kind: kind.value }, { silent: true });
          if (my !== seq) return;
          data.value = d;
          err.value = "";
          noModel.value = !d || !d.model;
          const want = props.query && props.query.code;
          if (want) { const r = rows.value.find((x) => x.code === want); if (r) openRow(r); }
        } catch (e) {
          if (my !== seq) return;
          data.value = null;
          // 后端还不认识这个类型、或模型文件不存在：按“还没训练”处理，给训练按钮
          noModel.value = [400, 404, 422].includes(e.status) && (kind.value === "swing" || /训练|模型|未知/.test(e.detail || ""));
          err.value = noModel.value ? "" : e.detail || e.message;
        } finally {
          if (my === seq) loading.value = false;
        }
        if (my === seq) loadHonest(my);
      };
      // 如实成绩：优先用模型 meta 的逐笔样本外（trade_oos），没有再按用户的买卖规则回测
      let btSeq = 0;
      const loadHonest = async (my) => {
        report.value = null;
        bt.value = null;
        acct.value = null;
        btErr.value = "";
        if (!data.value || !data.value.model) return;
        const k = kind.value;
        const mine = ++btSeq;
        btLoading.value = true;
        try {
          const inline = normTrade(data.value.model && (data.value.model.trade_oos || data.value.model.trade));
          if (!(inline && isNum(inline.mean))) {
            const r = await api.get("/api/model/report", { kind: k }, { silent: true }).catch(() => null);
            if (mine !== btSeq || my !== seq) return;
            report.value = r;
          }
          const meta = inline && isNum(inline.mean) ? inline : normTrade(report.value && report.value.trade_oos);
          if (meta && isNum(meta.mean)) {
            // 波段：模型检验的逐笔成绩不受资金限制；另按用户的本金/仓位回测一次（最低佣金 5 元、买不起一手会拉低收益），如实并列显示
            if (k === "swing") {
              const r = await api.post("/api/predict/backtest", { predict: { kind: k } }, { silent: true }).catch(() => null);
              if (mine === btSeq && my === seq) acct.value = r;
            }
            return;
          }
          const r = await api.post("/api/predict/backtest", { predict: { kind: k } }, { silent: true });
          if (mine === btSeq) bt.value = r;
        } catch (e) {
          if (mine === btSeq) { bt.value = null; btErr.value = e.detail || e.message; }
        } finally {
          if (mine === btSeq) btLoading.value = false;
        }
      };
      const loadSettings = async () => {
        try {
          settings.value = await api.get("/api/settings", null, { silent: true });
          if (!settings.value.risk_ack) riskOpen.value = true;
        } catch (e) {
          if (store.status && store.status.settings_risk_ack === false) riskOpen.value = true;
        }
      };
      onMounted(() => { loadSettings(); load(); });
      const offDone = bus.on("job-done", () => { load(); loadSettings(); });
      onBeforeUnmount(offDone);

      watch(kind, (k) => {
        QW.safeStore.set("qw-pred-kind2", k);
        if ((props.query && props.query.kind) !== k) go("/predict", { kind: k });
        drawerOpen.value = false;
        data.value = null;
        report.value = null;
        bt.value = null;
        acct.value = null;
        trainJob.value = null;
        load();
      });
      watch(() => props.query && props.query.kind, (k) => { if (KIND_VALUES.includes(k) && k !== kind.value) kind.value = k; });

      const ackRisk = async () => {
        if (!riskChecked.value) return;
        ackBusy.value = true;
        try {
          const s = await api.put("/api/settings", { risk_ack: true });
          if (s && typeof s === "object") settings.value = s;
          riskOpen.value = false;
          if (store.status) store.status.settings_risk_ack = true;
          toast.success("好的！记住：先观察、先模拟、不借钱。");
        } catch (e) { /* 已提示 */ } finally { ackBusy.value = false; }
      };
      const declineRisk = () => { riskOpen.value = false; go("/"); };

      const kindTabs = computed(() => KINDS.map((k) => ({ value: k.value, label: k.label, badge: k.badge, tone: k.tone })));
      const kindInfo = computed(() => KINDS.find((k) => k.value === kind.value) || KINDS[0]);
      const isSwing = computed(() => kind.value === "swing");
      const model = computed(() => (data.value && data.value.model) || null);
      const trust = computed(() => {
        const m = model.value;
        if (!m || isSwing.value) return null;
        const hit = fmt.frac(m.oos_topn_hit, 1.5);
        const base = fmt.frac(m.base_rate, 1.5);
        if (!isNum(hit)) return null;
        const mult = isNum(hit) && isNum(base) && base > 0 ? hit / base : null;
        return { hit, base, mult, beat: isNum(mult) && mult > 1.05, n: m.oos_topn || (settings.value && settings.value.predict && settings.value.predict.top_n) || null };
      });
      // 逐笔成绩：meta 优先，回测兜底
      const honest = computed(() => {
        // 完整的 trade_oos（报告里有选择期/留出期的随机基准）优先于 predict/today 里的扁平摘要
        for (const src of [model.value && model.value.trade_oos, report.value && report.value.trade_oos, model.value && model.value.trade]) {
          const meta = normTrade(src);
          if (meta && isNum(meta.mean)) return { ...meta, source: "meta" };
        }
        const b = normTrade(bt.value);
        if (b && isNum(b.mean)) return { ...b, source: "bt" };
        return null;
      });
      // 波段：按用户本金/仓位的回测（最低佣金 5 元、一手 100 股）是主数字；模型检验（按比例收费、不受资金限制）偏乐观，放在旁边
      const acctInfo = computed(() => {
        if (!isSwing.value || !acct.value) return null;
        const a = normTrade(acct.value);
        if (!a || !isNum(a.mean)) return null;
        const m = acct.value.metrics || {};
        const t = (acct.value.settings_used && acct.value.settings_used.trade) || {};
        const cap = isNum(t.capital) && t.capital > 0 ? t.capital : null;
        const pos = isNum(t.position_pct) ? t.position_pct : null;
        const capText = cap ? (cap >= 1e4 ? fmt.num(cap / 1e4, cap % 1e4 ? 1 : 0) + " 万" : fmt.int(cap) + " 元") : "你的本金";
        const holdT = a.hold && isNum(a.hold.t) ? a.hold.t : null;
        const skipped = isNum(m.skipped_lot) ? m.skipped_lot : null;
        // 最低佣金 5 元让小仓位一买一卖多花的比例：按你的本金、仓位和佣金率算（不写死）
        const per = cap && isNum(pos) ? cap * pos : null;
        const fee = isNum(t.fee_rate) ? t.fee_rate : 0.00025;
        const extra = per ? 2 * Math.max(0, 5 / per - fee) : null;
        const extraText = isNum(extra) && extra >= 0.0005 ? `，每只约 ${fmt.int(per)} 元的仓位一买一卖要多花约 ${fmt.num(extra * 100, 1)}%` : "";
        const help = `“模型检验”每笔都按比例算费用，不受资金多少的限制，偏乐观。上面的主数字按你在「预测设置」里的本金和仓位（${capText}` +
          (isNum(pos) ? `、每只 ${fmt.num(pos * 100, 0)}%` : "") + `）把同一段历史真实模拟一遍：每笔佣金最低 5 元${extraText}` +
          (skipped ? `；股价太高、买不起一手（100 股）的跳过了 ${skipped} 次` : "") + "。所以按你的账户算会低一些，这个数更接近你真做的结果。";
        const tuned = acct.value.tuned && acct.value.tuned.modified ? acct.value.tuned.fields || [] : null;
        return { mean: a.mean, n: a.n, t: a.t, win: a.win, base: a.base, match: a.match, sel: a.sel, hold: a.hold, holdT, capText, help, tuned, hasNw: a.hasNw };
      });
      // 如实成绩的“主数字”：波段用按你的账户回测（拿不到时退回模型检验），其余用 honest
      const main = computed(() => {
        const h = honest.value;
        const a = acctInfo.value;
        if (isSwing.value && a) return { ...a, src: "acct" };
        return h ? { ...h, src: h.source } : null;
      });
      // t 值：波段取“按你的本金”和“模型检验”两个留出期 t 值里低的那个（方案是在选择期里挑的，全部样本外的 t 偏乐观）
      const tMain = computed(() => {
        const h = honest.value;
        if (!h) return null;
        if (isSwing.value) {
          const mt = h.hold && isNum(h.hold.t) ? h.hold.t : null;
          const at = acctInfo.value ? acctInfo.value.holdT : null;
          if (isNum(at) && (!isNum(mt) || at <= mt)) return { t: at, label: "留出期 ", src: "acct", other: mt };
          if (isNum(mt)) return { t: mt, label: "留出期 ", src: "meta", other: at };
          return isNum(h.t) ? { t: h.t, label: "", src: "meta", other: null } : null;
        }
        return isNum(h.t) ? { t: h.t, label: "", src: h.source, other: null } : null;
      });
      // 波段的主数字：最近一段（留出期）按你的本金回测的平均每笔 vs 同一段“同一天随便挑同样多只”；
      // 结论按数据算（QW.recentVerdict）；账户回测拿不到时退回模型检验（不受资金限制，标偏乐观）
      const recent = computed(() => {
        if (!isSwing.value) return null;
        const a = acctInfo.value;
        const h = honest.value;
        let src = null;
        let part = null;
        if (a && a.hold && isNum(a.hold.mean)) { src = "acct"; part = a.hold; }
        else if (btLoading.value) return { loading: true };
        else if (h && h.hold && isNum(h.hold.mean)) { src = "meta"; part = h.hold; }
        if (!part) return null;
        const rnd = part.match && isNum(part.match.mean) ? part.match.mean : null;
        const t = tMain.value && tMain.value.label ? tMain.value.t : part.t;
        return {
          src, mean: part.mean, n: part.n, rnd, t, label: holdLabel(part),
          day: part.base && isNum(part.base.mean) ? part.base.mean : null,
          verdict: recentVerdict(part.mean, rnd, t),
        };
      });
      // t 值里有没有用到 Newey-West（后端 nw_t）：有时说明“取两种算法里低的”
      const hasNw = computed(() => !!((honest.value && honest.value.hasNw) || (acctInfo.value && acctInfo.value.hasNw)));
      const tPeriodHelp = computed(() => tHelp({ hasNw: hasNw.value }) + "\n" + HELP.period);
      // 买得进吗：优先用后端的 fill_rate（买不进 = 开盘就涨停 + 高开太多放弃 + 买入日停牌），主数字的来源优先；都没有时按老算法（1 - 未成交 ÷ 信号）估
      const fillInfo = computed(() => {
        const srcs = [isSwing.value && acct.value ? normTrade(acct.value) : null, bt.value ? normTrade(bt.value) : null, honest.value].filter(Boolean);
        const x = srcs.find((s) => isNum(s.fill) && s.fillSrc === "backend") || srcs.find((s) => isNum(s.fill));
        if (!x) return null;
        const parts = [];
        if (isNum(x.unfilled) && x.unfilled > 0) parts.push(`开盘就涨停 ${fmt.int(x.unfilled)} 次`);
        if (isNum(x.skippedGap) && x.skippedGap > 0) parts.push(`高开太多放弃 ${fmt.int(x.skippedGap)} 次`);
        if (isNum(x.suspended) && x.suspended > 0) parts.push(`停牌 ${fmt.int(x.suspended)} 次`);
        // 按你的本金回测时没买的（不算“买不进”，但也没买）：一手太贵、现金不够
        const skips = [];
        if (isNum(x.skippedLot) && x.skippedLot > 0) skips.push(`${fmt.int(x.skippedLot)} 个信号因一手太贵没买`);
        if (isNum(x.skippedCash) && x.skippedCash > 0) skips.push(`${fmt.int(x.skippedCash)} 个信号因现金不够没买`);
        return { fill: x.fill, parts, skipText: skips.length ? "另有 " + skips.join("，") : "" };
      });
      // 按你的本金回测时，钱不够/一手太贵跳过了很多信号：账户结果只代表一部分信号，另给“每个信号都买”的逐笔结果
      const skipInfo = computed(() => {
        const x = main.value;
        const src = x && x.src === "acct" ? acct.value : x && x.src === "bt" ? bt.value : null;
        const n = src ? normTrade(src) : null;
        if (!n || !isNum(n.skipShare) || n.skipShare <= QW.SKIP_HEAVY) return null;
        return { share: n.skipShare, unc: n.uncapped };
      });
      // 回测返回的说明（如“资金不足/一手太贵跳过了 X% 的信号…”）：和名单的说明去重后放进“注意事项”
      const btNotes = computed(() => {
        const src = (isSwing.value && acct.value) || bt.value;
        const have = new Set(((data.value && data.value.notes) || []).concat((data.value && data.value.warnings) || []));
        return ((src && Array.isArray(src.notes) && src.notes) || []).filter((n) => typeof n === "string" && n && !have.has(n));
      });
      const edge = computed(() => {
        const x = main.value;
        return x && x.base && isNum(x.base.mean) && isNum(x.mean) ? x.mean - x.base.mean : null;
      });
      // 只比挑股票（同日同数量随机）：门槛不起作用时两种随机一样，不单独显示
      const pickEdge = computed(() => {
        const x = main.value;
        return x && x.match && !x.match.same && isNum(x.match.mean) && isNum(x.mean) ? x.mean - x.match.mean : null;
      });
      // 名单用的设置和推荐设置不一样（按历史检验结果改过）→ 历史成绩偏乐观
      const tunedFields = computed(() => {
        if (isSwing.value) return acctInfo.value ? acctInfo.value.tuned : null;
        const t = bt.value && bt.value.tuned;
        return t && t.modified ? t.fields || [] : null;
      });
      const verdict = computed(() => {
        const x = main.value;
        if (!x || !isNum(x.mean)) return null;
        const avg = fmt.ratio(x.mean, 2, true);
        const baseTxt = x.base && isNum(x.base.mean) ? fmt.ratio(x.base.mean, 2, true) : null;
        const tv = tMain.value ? tMain.value.t : null;
        const tTxt = isNum(tv) ? `${tMain.value.label}t 值 ${fmt.t(tv)}（${tWord(tv)}）` : "";
        const who = isSwing.value ? (x.src === "acct" ? `按你的本金（${x.capText}）回测，` : "模型检验（不受资金限制，偏乐观），") : "";
        const r = recent.value;
        if (isSwing.value && r && r.loading) return { tone: "info", head: "正在按你的本金回测最近一段（留出期）的成绩…", tail: "" };
        if (isSwing.value && r && r.verdict) {
          const h = honest.value;
          const src = r.src === "acct" ? `按你的本金（${x.capText}）回测` : "模型检验（不受资金限制，偏乐观）";
          const rndTxt = isNum(r.rnd) ? `，同一天随便挑同样多只是 ${fmt.ratio(r.rnd, 2, true)}` : "";
          const tTxt = isNum(r.t) ? `，t 值 ${fmt.t(r.t)}（${tWord(r.t)}）` : "";
          const matchFull = x.match && !x.match.same && isNum(x.match.mean) ? `（同日同数量随便挑 ${fmt.ratio(x.match.mean, 2, true)}）` : "";
          const full = `全部样本外平均每笔 ${avg}${matchFull}，里面包含研究时挑方案用的那段历史，偏乐观。`;
          const metaTxt = x.src === "acct" && h && isNum(h.mean) ? `模型检验（不受资金限制）${fmt.ratio(h.mean, 2, true)}/笔，更偏乐观。` : "";
          return {
            tone: r.verdict.tone,
            head: `一句话：${r.verdict.text}。`,
            tail: `${r.label}${src}平均每笔 ${fmt.ratio(r.mean, 2, true)}${rndTxt}${tTxt}。${full}${metaTxt}先用模拟盘跟踪几十笔，不建议直接用真钱。`,
          };
        }
        if (x.mean <= 0) {
          const worse = baseTxt && isNum(edge.value) && edge.value < 0 ? `，比同一批股票里天天随便挑（${baseTxt}）还差` : "";
          return {
            tone: "warn",
            head: `一句话：${who}照着买是亏的，平均每笔 ${avg}${worse}。`,
            tail: isSwing.value ? "名单先只看不买，继续在模拟盘里观察。" : "把它当“今天谁最强”的观察名单，别照着买。",
          };
        }
        if (baseTxt && isNum(edge.value) && edge.value <= 0.0005) {
          return { tone: "warn", head: `一句话：${who}平均每笔 ${avg}，但天天随便挑也有 ${baseTxt}，模型没多挑出好股票。`, tail: "赚的主要是那段行情的钱，请只当参考。" };
        }
        const more = isNum(edge.value) ? `，比天天随便挑多 ${fmt.num(edge.value * 100, 2)} 个百分点` : "";
        if (isSwing.value) {
          const h = honest.value;
          const holdMean = x.hold && isNum(x.hold.mean) ? `，其中留出期 ${fmt.ratio(x.hold.mean, 2, true)}` : "";
          const metaTxt = x.src === "acct" && h && isNum(h.mean) ? `模型检验（不受资金限制）是 ${fmt.ratio(h.mean, 2, true)}/笔，偏乐观。` : "";
          const pe = pickEdge.value;
          const pick = isNum(pe)
            ? (isNum(edge.value) && pe < edge.value / 2 ? "多出来的收益里一大半来自“哪天做、哪天不做”的择时，" : "") +
              `只比挑股票（同一天随便挑同样多只）多 ${fmt.num(pe * 100, 2)} 个百分点；`
            : "多出来的收益里相当一部分来自“哪天做、哪天不做”的择时；";
          return {
            tone: isNum(tv) && tv >= 2 ? "info" : "warn",
            head: `一句话：${who}历史上平均每笔 ${avg}${holdMean}${more}${tTxt ? "，" + tTxt : ""}。${metaTxt}`,
            tail: `${pick}每次重新训练成绩也会波动。还不能说明它真能赚钱：先用模拟盘跟踪几十笔，不建议直接用真钱。`,
          };
        }
        if (!isNum(tv) || tv < 2) {
          return { tone: "info", head: `一句话：历史上平均每笔 ${avg}${more}，但${tTxt || "可信度还没有检验"}。`, tail: "可能只是运气好，先用模拟盘跟踪几十笔再说，不建议直接用真钱。" };
        }
        return { tone: tunedFields.value ? "info" : "ok", head: `一句话：历史上平均每笔 ${avg}${more}，${tTxt}。`, tail: "但过去不代表未来，先模拟、再小仓位。" };
      });
      // 波段永远不给“可信”的绿色框：还在模拟跟踪阶段
      const boxTone = computed(() => {
        if (verdict.value) return verdict.value.tone === "warn" ? "warn" : isSwing.value || verdict.value.tone === "info" ? "info" : "ok";
        return trust.value && trust.value.beat ? "info" : "warn";
      });

      // 名单行：streak/first 算模型原始排名（提示“靠消息面排上来的”）；swing 用预期收益
      const rows = computed(() => {
        const list = (data.value && data.value.rows) || [];
        const modelRank = {};
        if (!isSwing.value) list.slice().sort((a, b) => (b.prob ?? 0) - (a.prob ?? 0)).forEach((r, i) => { modelRank[r.code] = i + 1; });
        return list.map((r, i) => {
          const boost = !isSwing.value && isNum(r.score) && isNum(r.prob) ? r.score - r.prob : 0;
          return { ...r, _rank: i + 1, _modelRank: modelRank[r.code], _boost: boost, _exp: isNum(r.exp_ret) ? r.exp_ret : null };
        });
      });
      const pickCount = computed(() => rows.value.filter((r) => r.pick).length);
      // 所有候选都一样的“全市场”理由（如“全市场涨幅中位数 -1.30%”）：不在每一行重复，改在表格上方说一次；
      // 按“↑/↓”前的文字归并（同一个市场因素对不同股票的方向可能不同）
      // “振幅 0.0%”对新手没意义：注明就是一字板
      const plainReason = (t) => String(t || "").replace(/^振幅 0(\.0+)?%/, "$&（一字板，全天没打开）");
      const reasonKey = (t) => String(t || "").split(/\s*[↑↓]/)[0].trim();
      const commonReasons = computed(() => {
        const list = rows.value;
        if (list.length < 5) return [];
        const cnt = {};
        const text = {};
        list.forEach((r) => {
          new Set((r.reasons || []).map(reasonKey)).forEach((k) => { if (k) cnt[k] = (cnt[k] || 0) + 1; });
          (r.reasons || []).forEach((t) => { const k = reasonKey(t); (text[k] = text[k] || {})[t] = (text[k][t] || 0) + 1; });
        });
        return Object.keys(cnt).filter((k) => cnt[k] >= list.length * 0.6)
          .map((k) => ({ key: k, text: Object.keys(text[k]).sort((a, b) => text[k][b] - text[k][a])[0] }));
      });
      const commonKeys = computed(() => new Set(commonReasons.value.map((x) => x.key)));
      const isCommon = (t) => commonKeys.value.has(reasonKey(t));
      const mainReason = (r) => {
        const rs = (r && r.reasons) || [];
        const own = rs.find((t) => !isCommon(t));
        if (own) return plainReason(own);
        return rs.length && commonReasons.value.length ? "主要是全市场因素（见表格上方）" : rs[0] || "";
      };
      const newsWeight = computed(() => {
        const p = settings.value && settings.value.predict;
        return p && isNum(p.news_weight) ? p.news_weight : null;
      });
      const boosted = computed(() => (isSwing.value ? [] : rows.value.filter((r) => r.pick && r._modelRank > pickCount.value && r._boost > 0.01)));
      const phoneLimit = ref(30);
      const phoneRows = computed(() => rows.value.slice(0, phoneLimit.value));
      const newsBusy = ref(false);
      const setNewsZero = async () => {
        newsBusy.value = true;
        try {
          const s = await api.put("/api/settings", { predict: { news_weight: 0 } });
          if (s && typeof s === "object") settings.value = s;
          toast.success("已把消息面权重设为 0：名单现在只按模型排序。想改回去，到「预测设置」里调。");
          drawerOpen.value = false;
          await load();
        } catch (e) { /* 已提示 */ } finally { newsBusy.value = false; }
      };
      const boostText = (r) => {
        if (!r || !isNum(r.prob) || !isNum(r.score)) return "";
        const d = (r.score - r.prob) * 100;
        const dir = d >= 0 ? "高" : "低";
        return `模型原始概率 ${fmt.prob(r.prob, 1)}，调整后 ${fmt.prob(r.score, 1)}（${dir}了 ${Math.abs(d).toFixed(1)} 个百分点）。` +
          `只按模型排是第 ${r._modelRank} 名。` + (r.news_count || r.policy_count ? `最近24小时相关消息 ${r.news_count ?? 0} 条、政策 ${r.policy_count ?? 0} 条。` : "");
      };

      // 波段：今天操作不操作
      const threshold = computed(() => {
        const d = data.value || {};
        if (isNum(d.threshold)) return fmt.frac(d.threshold, 0.5);
        const g = d.gate || {};
        if (isNum(g.threshold)) return fmt.frac(g.threshold, 0.5);
        const p = settings.value && settings.value.predict;
        if (p && p.kind === "swing" && isNum(p.threshold) && p.threshold > 0) return p.threshold;
        return 0.01;
      });
      const gate = computed(() => {
        if (!isSwing.value || !data.value || !model.value) return null;
        const g = data.value.gate && typeof data.value.gate === "object" ? data.value.gate : null;
        const n = g && isNum(g.n_picks) ? g.n_picks : g && isNum(g.count) ? g.count : pickCount.value;
        const trade = g ? !!g.trade : n > 0;
        const thText = thPct(threshold.value);
        return {
          trade, n,
          title: trade ? `今天可以按计划操作：${n} 只` : `今天不操作：没有预期收益 ≥ ${thText} 的股票`,
          reason: g && g.reason ? String(g.reason) : trade ? `名单里带 ★ 的 ${n} 只，预期收益都达到了 ${thText}。` : "宁可空仓等机会，也不去买把握不大的。明天收盘后再来看。",
        };
      });
      const plan = computed(() => planLines(data.value && data.value.plan));

      const chips = computed(() => {
        // 名单实际用的设置（后端按名单类型换过默认值，如波段只看主板）优先；没有时用保存的设置
        const used = data.value && data.value.settings_used && data.value.settings_used.predict;
        const p = used && typeof used === "object" ? used : settings.value && settings.value.predict;
        if (!p) return [];
        const out = [];
        out.push(p.exclude_st ? "排除ST股" : "包含ST股");
        if (Array.isArray(p.boards)) out.push("板块：" + (p.boards.length ? p.boards.map((b) => BOARDS[b] || b).join("、") : "无"));
        out.push(`流通市值 ${p.min_float_cap ?? 0}~${p.max_float_cap ?? "不限"}亿`);
        out.push(`股价 ${p.min_price ?? 0}~${p.max_price ?? "不限"}元`);
        if (kind.value === "streak") out.push(`连板 ${p.streak_min ?? 1}~${p.streak_max ?? 10} 板`);
        if (p.exclude_one_word && !isSwing.value) out.push("排除一字板");
        if (isSwing.value) out.push(`预期收益 ≥ ${thPct(threshold.value)}`);
        else if (isNum(p.threshold) && p.threshold > 0 && p.kind !== "swing") out.push(`概率 ≥ ${fmt.ratio(p.threshold, 0)}`);
        if (p.max_float_cap >= 100000) out.splice(out.findIndex((c) => c.startsWith("流通市值")), 1, (p.min_float_cap || 0) > 0 ? `流通市值 ≥ ${p.min_float_cap}亿` : "流通市值不限");
        if (p.max_price >= 10000) out.splice(out.findIndex((c) => c.startsWith("股价")), 1, `股价 ≥ ${p.min_price ?? 0}元`);
        if (isNum(p.top_n)) out.push(`重点关注前 ${p.top_n} 名`);
        const w = p.weights || {};
        if (Object.keys(w).some((k) => Math.abs((w[k] ?? 1) - 1) > 1e-6)) out.push("五维权重已调整");
        if (isNum(p.news_weight) && p.news_weight > 0) out.push(`消息面权重 ${p.news_weight}`);
        return out;
      });

      const columns = computed(() => [
        { key: "_rank", label: "排名", width: "58px", sortable: true, firstOrder: "asc", help: HELP.pick },
        { key: "name", label: "名称 / 代码", minWidth: "104px" },
        { key: "industry", label: "行业", sortable: true, sortBy: (r) => fmt.industry(r.industry) },
        kind.value === "streak" ? { key: "streak", label: "连板", sortable: true, help: HELP.streak } : null,
        { key: "pct", label: "今日涨幅", align: "right", sortable: true },
        { key: "turn", label: "换手", align: "right", sortable: true, help: HELP.turn },
        { key: "float_cap", label: "流通市值", align: "right", sortable: true, help: HELP.fcap },
        isSwing.value
          ? { key: "_exp", label: "预期收益", align: "right", sortable: true, help: HELP.expRet, minWidth: "96px" }
          : { key: "score", label: "明天涨停概率", sub: "（仅供观察）", sortable: true, help: HELP.score, minWidth: "150px" },
        { key: "dims", label: "五维", help: HELP.dims, align: "center" },
        { key: "reason", label: "主要理由", minWidth: "150px" },
        { key: "actions", label: "", align: "right", width: "72px" },
      ].filter(Boolean));

      const openRow = (r) => {
        cur.value = r;
        drawerOpen.value = true;
        if (!mini.value[r.code]) {
          api.get(`/api/stock/${r.code}/kline`, { period: "day", adjust: "qfq", count: 60 }, { silent: true })
            .then((k) => { mini.value = { ...mini.value, [r.code]: k }; })
            .catch((e) => { mini.value = { ...mini.value, [r.code]: { error: e.detail || e.message } }; });
        }
      };
      const toggleWatch = (r) => QW.watch.toggle(r.code, r.name).catch(() => {});
      const inWatch = (code) => store.watchCodes.includes(code);
      const reasonCls = (t) => (/↑|提高|有利/.test(t) ? "pos" : /↓|降低|不利/.test(t) ? "neg" : "");
      const reasonIcon = (t) => (reasonCls(t) === "pos" ? "▲" : reasonCls(t) === "neg" ? "▼" : "•");
      const dimList = (d) => ["sentiment", "capital", "fundamental", "theme", "technical", "news"]
        .filter((k) => d && isNum(d[k])).map((k) => ({ k, label: LABELS[k], v: Math.round(d[k]), help: DIM_HELP[k] }));
      const expCls = (r) => (!isNum(r._exp) ? "muted" : r._exp >= threshold.value * 100 - 1e-9 ? fmt.dir(r._exp) : "muted");

      const startTrain = async () => {
        try { trainJob.value = await jobs.start("/api/jobs/train", { kind: kind.value }, `训练模型（${kindInfo.value.label}）`); } catch (e) { /* 已提示 */ }
      };
      const oneClick = async () => { try { await jobs.start("/api/jobs/daily", {}, "一键更新"); } catch (e) { /* 已提示 */ } };

      return {
        store, fmt, KINDS, HELP, RISKS, T_HELP, tPeriodHelp, hasNw, fillInfo, skipInfo, btNotes, UNCAPPED_HELP: QW.UNCAPPED_HELP, kind, kindTabs, kindInfo, isSwing, data, loading, err, noModel, settings,
        riskOpen, riskChecked, ackBusy, ackRisk, declineRisk,
        model, trust, honest, main, tMain, recent, fillPct, edge, pickEdge, tunedFields, verdict, boxTone, bt, btErr, btLoading, rows, pickCount, chips, columns, drawerOpen, cur, mini, openRow, toggleWatch, inWatch,
        RETRAIN_T_NOTE, TUNED_HELP,
        acctInfo, commonReasons, isCommon, mainReason, plainReason, thText: computed(() => thPct(threshold.value)), topExp: computed(() => (isSwing.value && rows.value.length && isNum(rows.value[0]._exp) ? rows.value[0]._exp : null)),
        reasonCls, reasonIcon, dimList, expCls, trainJob, startTrain, oneClick, load, gate, plan, threshold, tWord,
        newsWeight, boosted, phoneLimit, phoneRows, newsBusy, setNewsZero, boostText, ONE_WORD_TIP: QW.ONE_WORD_TIP,
        pickOneWord: computed(() => (isSwing.value ? 0 : rows.value.filter((r) => r.pick && r.one_word).length)),
      };
    },
    template: `<div class="stack">
      <qw-modal v-model="riskOpen" title="先读一读：短线交易的风险" width="580px" :closable="false" :mask-closable="false">
        <p class="text-2" style="margin-bottom:10px">“短线预测”帮你用数据研究强势股和涨停股，但请先了解这些风险：</p>
        <ul class="risk-list"><li v-for="(r, i) in RISKS" :key="i"><span class="n">{{ i + 1 }}</span><span>{{ r }}</span></li></ul>
        <label class="check"><input type="checkbox" v-model="riskChecked">我已阅读并理解上面的风险</label>
        <template #footer>
          <button class="btn ghost" @click="declineRisk">先不看了</button>
          <button class="btn primary" :disabled="!riskChecked || ackBusy" @click="ackRisk">我已了解，开始使用</button>
        </template>
      </qw-modal>

      <div class="pred-head">
        <qw-tabs v-model="kind" :items="kindTabs"/>
        <p class="pred-kind-desc"><qw-icon :name="kindInfo.icon" :size="15"/><span>{{ kindInfo.desc }}</span></p>
      </div>

      <template v-if="loading && !data">
        <qw-card><qw-skeleton :rows="3"/></qw-card>
        <qw-card :pad="false"><div style="padding:16px"><qw-skeleton :rows="8"/></div></qw-card>
      </template>

      <qw-card v-else-if="err && !data">
        <qw-empty icon="alert" title="名单暂时拿不到" :desc="err" action-text="重新加载" @action="load">
          <button class="btn" @click="oneClick">一键更新数据</button>
        </qw-empty>
      </qw-card>

      <qw-card v-else-if="noModel || (data && !model)">
        <qw-empty icon="cpu" :title="'「' + kindInfo.label + '」模型还没训练'" desc="模型要先用历史数据“学习”一遍，才能给出名单。第一次训练大约需要几分钟到十几分钟（训练时电脑会比较忙），训练完会自动出现今天的名单。">
          <button class="btn primary" :disabled="!!trainJob" @click="startTrain"><qw-icon name="cpu" :size="15"/>直接开始训练</button>
          <a class="btn" :href="'#/model?kind=' + kind">去模型中心看看</a>
        </qw-empty>
        <div v-if="trainJob" style="padding:0 8px 8px"><qw-job :job-id="trainJob" :title="'训练模型（' + kindInfo.label + '）'" @done="load"/></div>
      </qw-card>

      <template v-else-if="data">
        <!-- 波段：先回答“今天操作不操作”，再讲成绩靠不靠得住 -->
        <div v-if="gate" class="gate-card" :class="gate.trade ? 'go' : 'stop'">
          <div class="gate-main">
            <span class="gate-ico"><qw-icon :name="gate.trade ? 'play' : 'pause'" :size="24"/></span>
            <div class="gate-txt">
              <div class="gate-title">{{ gate.title }}</div>
              <div class="gate-reason">{{ gate.reason }}</div>
              <div class="gate-when"><qw-icon name="clock" :size="13"/>依据 {{ $fmt.cnDate(data.signal_date) }} 收盘的数据 · 下一个交易日开盘执行</div>
            </div>
            <span class="qw-tag gate-tag">模拟跟踪中</span>
          </div>
          <div class="gate-plan">
            <div class="gate-plan-hd">{{ gate.trade ? '按计划操作' : '今天的计划和买卖规则' }}</div>
            <ul>
              <li v-for="p in plan" :key="p.label"><span class="gp-l"><qw-icon :name="p.icon" :size="14"/>{{ p.label }}</span><span class="gp-t">{{ p.text }}</span></li>
            </ul>
            <div class="gate-foot">
              <qw-icon name="clipboard" :size="14"/><span>每天收盘后，程序会把这份名单自动记进模拟盘，用之后的真实行情算盈亏，不用真钱。</span>
              <a class="linkbtn" href="#/paper">去模拟盘 <qw-icon name="chevronRight" :size="14"/></a>
            </div>
          </div>
        </div>

        <div class="trust-box" :class="boxTone">
          <div class="trust-cards">
            <div v-if="trust" class="trust-card">
              <div class="q"><qw-icon name="target" :size="15"/>挑得准吗？<qw-help :text="HELP.hit"/></div>
              <div class="v num up">{{ $fmt.ratio(trust.hit, 1) }}</div>
              <div class="k">每天前 {{ trust.n || 5 }} 名，第二天真的涨停的比例</div>
              <div class="s">随便挑只有 <b class="num">{{ $fmt.ratio(trust.base, 1) }}</b><template v-if="trust.beat">，约是它的 <b class="num">{{ $fmt.num(trust.mult, 1) }}</b> 倍</template></div>
              <div class="s trust-caveat"><qw-icon name="alert" :size="13"/>这是“第二天涨停”的比例，不是赚钱的概率；能不能赚钱看“照着买能赚钱吗”</div>
            </div>
            <div class="trust-card">
              <div class="q"><qw-icon name="wallet" :size="15"/>照着买能赚钱吗？<qw-help :text="recent && !recent.loading ? HELP.recent + ' ' + (recent.src === 'acct' ? HELP.acctMean : HELP.mean) : main && main.src === 'acct' ? HELP.acctMean : HELP.mean"/></div>
              <template v-if="recent && recent.loading">
                <div class="k" style="margin-top:8px">正在按你的本金回测最近一段（留出期）的成绩（每笔佣金最低 5 元、一手 100 股）…</div>
              </template>
              <template v-else-if="recent">
                <div class="rv-pair">
                  <div class="rv-x"><div class="v num" :class="$fmt.dir(recent.mean)">{{ $fmt.ratio(recent.mean, 2, true) }}</div><div class="rv-k">{{ recent.src === 'acct' ? '按你的本金' : '模型检验（偏乐观）' }}</div></div>
                  <div class="rv-vs">对比</div>
                  <div class="rv-x"><div class="v num" :class="$fmt.dir(recent.rnd)">{{ recent.rnd == null ? '—' : $fmt.ratio(recent.rnd, 2, true) }}</div><div class="rv-k">同一天随便挑<qw-help :text="HELP.match"/></div></div>
                </div>
                <div class="k">{{ recent.label }}平均每笔<template v-if="recent.src === 'acct'">，按你的本金（{{ main.capText }}）回测、已扣费用</template><template v-else>，按比例收费、不受资金多少限制</template><template v-if="recent.n">，共 {{ $fmt.int(recent.n) }} 笔</template></div>
                <div v-if="main && main.mean != null" class="s">全部样本外 <b class="num" :class="$fmt.dir(main.mean)">{{ $fmt.ratio(main.mean, 2, true) }}</b>/笔<template v-if="main.base && main.base.mean != null"> · 天天随便挑 {{ $fmt.ratio(main.base.mean, 2, true) }}</template><template v-if="main.match && !main.match.same && main.match.mean != null"> · 同日同数量 {{ $fmt.ratio(main.match.mean, 2, true) }}</template><qw-help :text="HELP.full + ' ' + HELP.base"/></div>
                <div v-if="recent.src === 'acct' && honest" class="s acct-line inl">模型检验（不受资金限制）<b class="num" :class="$fmt.dir(honest.mean)">{{ $fmt.ratio(honest.mean, 2, true) }}</b>/笔<template v-if="honest.hold && honest.hold.mean != null">、留出期 <b class="num" :class="$fmt.dir(honest.hold.mean)">{{ $fmt.ratio(honest.hold.mean, 2, true) }}</b></template>，偏乐观<qw-help :text="acctInfo.help"/></div>
                <div v-else class="s acct-line warn">按你的本金回测暂时拿不到；按小资金真实交易通常比模型检验低</div>
                <div v-if="skipInfo" class="s acct-line warn"><span>钱不够或一手太贵，跳过了 <b class="num">{{ $fmt.ratio(skipInfo.share, 0) }}</b> 的信号<template v-if="skipInfo.unc">；每个信号都买（不受资金限制）平均每笔 <b class="num" :class="$fmt.dir(skipInfo.unc.mean)">{{ $fmt.ratio(skipInfo.unc.mean, 2, true) }}</b><template v-if="skipInfo.unc.n">（{{ $fmt.int(skipInfo.unc.n) }} 笔）</template></template><qw-help :text="UNCAPPED_HELP"/></span></div>
              </template>
              <template v-else-if="main && main.mean != null">
                <div class="v num" :class="$fmt.dir(main.mean)">{{ $fmt.ratio(main.mean, 2, true) }}</div>
                <div class="k" v-if="main.src === 'acct'">按你的本金（{{ main.capText }}）回测，样本外平均每笔（已扣费用，含最低佣金 5 元）</div>
                <div class="k" v-else-if="isSwing">模型检验的样本外平均每笔（按比例收费、不受资金多少限制，偏乐观）</div>
                <div class="k" v-else>样本外平均每笔（已扣手续费）</div>
                <div class="s"><template v-if="main.base && main.base.mean != null">天天随便挑 <b class="num" :class="$fmt.dir(main.base.mean)">{{ $fmt.ratio(main.base.mean, 2, true) }}</b><qw-help :text="HELP.base"/></template><template v-else>没有随机挑选的对比数据</template><template v-if="main.match && !main.match.same && main.match.mean != null"> · 同日同数量随便挑 <b class="num" :class="$fmt.dir(main.match.mean)">{{ $fmt.ratio(main.match.mean, 2, true) }}</b><qw-help :text="HELP.match"/></template></div>
                <div v-if="isSwing && main.src === 'acct' && honest" class="s acct-line">模型检验（不受资金限制）<b class="num" :class="$fmt.dir(honest.mean)">{{ $fmt.ratio(honest.mean, 2, true) }}</b>/笔，偏乐观<qw-help :text="acctInfo.help"/></div>
                <div v-else-if="isSwing && btLoading" class="s acct-line warn">正在按你的本金回测（最低佣金 5 元、一手 100 股），通常会更低…</div>
                <div v-else-if="isSwing" class="s acct-line warn">按你的本金回测暂时拿不到；按小资金真实交易通常比这个数低</div>
                <div v-if="skipInfo" class="s acct-line warn"><span>钱不够或一手太贵，跳过了 <b class="num">{{ $fmt.ratio(skipInfo.share, 0) }}</b> 的信号<template v-if="skipInfo.unc">；每个信号都买（不受资金限制）平均每笔 <b class="num" :class="$fmt.dir(skipInfo.unc.mean)">{{ $fmt.ratio(skipInfo.unc.mean, 2, true) }}</b><template v-if="skipInfo.unc.n">（{{ $fmt.int(skipInfo.unc.n) }} 笔）</template></template><qw-help :text="UNCAPPED_HELP"/></span></div>
              </template>
              <div v-else-if="btLoading" class="k" style="margin-top:8px">正在读取样本外成绩…</div>
              <div v-else class="k" style="margin-top:8px">暂时没有逐笔成绩<template v-if="btErr">：{{ btErr }}</template></div>
            </div>
            <div v-if="tMain" class="trust-card">
              <div class="q"><qw-icon name="bars" :size="15"/>靠得住吗？<qw-help :text="tPeriodHelp"/></div>
              <div class="v num"><small v-if="tMain.label" class="t-lbl">{{ tMain.label }}</small>t = {{ $fmt.t(tMain.t) }}<span class="t-tag" :class="tMain.t >= 2 ? (isSwing ? 'mid' : 'ok') : tMain.t >= 1 ? 'mid' : 'bad'">{{ tWord(tMain.t) }}</span></div>
              <div class="k" v-if="isSwing">t 值 ≥2 才算比较可信。这里取“按你的本金回测”和“模型检验”两个留出期 t 值里<b>低的那个</b><template v-if="hasNw">；每个 t 值也都取了“按天算”和“考虑持仓连着好几天”两种算法里低的</template></div>
              <div class="k" v-else>t 值 ≥2 才算比较可信<template v-if="hasNw">；取“按天算”和“考虑持仓连着好几天”两种算法里<b>低的那个</b></template></div>
              <div class="s" v-if="main && (main.sel || main.hold)">
                <template v-if="isSwing && main.src === 'acct'">按你的本金：</template>
                <template v-if="main.sel && main.sel.mean != null">选择期 <b class="num" :class="$fmt.dir(main.sel.mean)">{{ $fmt.ratio(main.sel.mean, 2, true) }}</b></template>
                <template v-if="main.hold && main.hold.mean != null"> · 留出期 <b class="num" :class="$fmt.dir(main.hold.mean)">{{ $fmt.ratio(main.hold.mean, 2, true) }}</b><template v-if="main.hold.t != null && !tMain.label">（t {{ $fmt.t(main.hold.t) }}）</template></template>
              </div>
              <div class="s" v-else-if="main">胜率 {{ $fmt.ratio(main.win, 0) }}<template v-if="main.n"> · 共 {{ $fmt.int(main.n) }} 笔</template></div>
              <div v-if="isSwing && tMain.other != null" class="s acct-line">{{ tMain.src === 'acct' ? '模型检验（不受资金限制）' : '按你的本金回测' }}的留出期 t {{ $fmt.t(tMain.other) }}<qw-help :text="RETRAIN_T_NOTE"/></div>
            </div>
            <div v-if="fillInfo" class="trust-card">
              <div class="q"><qw-icon name="checkCircle" :size="15"/>买得进吗？<qw-help :text="HELP.fill"/></div>
              <div class="v num">{{ fillPct(fillInfo.fill) }}</div>
              <div class="k">的信号第二天能按开盘价买进</div>
              <div v-if="fillInfo.parts.length" class="s">买不进的：{{ fillInfo.parts.join('、') }}</div>
              <div v-else class="s">{{ fillInfo.fill < 0.8 ? '不少是一开盘就涨停（一字板），排队也买不到' : '大部分信号第二天都能买进' }}</div>
              <div v-if="fillInfo.skipText" class="s acct-line inl warn">{{ fillInfo.skipText }}（按你的本金回测）</div>
            </div>
          </div>
          <div class="trust-verdict">
            <qw-icon :name="boxTone === 'ok' ? 'checkCircle' : boxTone === 'info' ? 'info' : 'alert'" :size="18"/>
            <div>
              <template v-if="verdict"><b>{{ verdict.head }}</b>{{ verdict.tail }}</template>
              <template v-else-if="trust && !trust.beat">模型在样本外<b>没有明显好过随便挑</b>，名单请只当参考。</template>
              <template v-else-if="trust">名单里的票第二天更容易涨停，但<b>能不能赚钱还没有逐笔检验结果</b>，请只当观察名单。</template>
              <template v-else>还没有样本外的逐笔成绩，名单请只当参考。</template>
              <template v-if="tunedFields && tunedFields.length"> <span class="tuned-note">注意：你改过设置（{{ tunedFields.slice(0, 4).join('、') }}{{ tunedFields.length > 4 ? '等' : '' }}），如果是看着历史结果改的，这些成绩会偏乐观<qw-help :text="TUNED_HELP"/>。</span></template>
              <a v-if="isSwing" class="trust-link" href="#/paper">看模拟盘的前向跟踪 →</a>
              <a v-else class="trust-link" :href="'#/settings?kind=' + kind + '&run=1'">看详细的历史检验 →</a>
            </div>
          </div>
          <div class="trust-meta">
            <span v-if="main && main.src === 'acct'">主数字按你在「预测设置」里的本金和仓位回测<template v-if="main.n">，共 {{ $fmt.int(main.n) }} 笔</template>；模型检验<template v-if="honest && honest.n"> {{ $fmt.int(honest.n) }} 笔</template>，按默认规则、不受资金限制</span>
            <span v-else-if="honest">{{ honest.source === 'meta' ? '逐笔成绩来自模型训练时的样本外检验（默认买卖规则，不受资金限制）' : '逐笔成绩按你在「预测设置」里的买卖规则回测' }}<template v-if="honest.n">，共 {{ $fmt.int(honest.n) }} 笔</template></span>
            <span v-if="model.oos_start">样本外区间 {{ $fmt.date(model.oos_start) }} ~ {{ $fmt.date(model.oos_end) }}</span>
            <span>信号日 <b class="num">{{ $fmt.date(data.signal_date) }}</b></span>
            <span v-if="data.generated_at">生成于 {{ $fmt.datetime(data.generated_at) }}</span>
            <span v-if="model.trained_at">模型训练于 {{ $fmt.date(model.trained_at) }}</span>
          </div>
        </div>

        <div v-if="newsWeight > 0 && boosted.length" class="qw-banner info news-note">
          <qw-icon name="news" :size="18"/>
          <div class="qw-banner-body">
            今天的排名加了<b>消息面加分</b>（权重 {{ newsWeight }}）：★ 前 {{ pickCount }} 名里有 <b>{{ boosted.length }}</b> 只（{{ boosted.map((r) => r.name).join('、') }}）只按模型排进不了前 {{ pickCount }}，是靠新闻多排上来的。
            消息热度≠利好：消息面没有经过历史检验，上面的成绩也<b>不含</b>它；新闻多也可能是利空。
          </div>
          <button class="btn sm" :disabled="newsBusy" @click="setNewsZero">只按模型排序</button>
        </div>

        <div class="filter-chips">
          <qw-icon name="filter" :size="15" style="color:var(--text-3)"/>
          <span v-for="c in chips" :key="c" class="chip static">{{ c }}</span>
          <span v-if="data.filtered_out" class="muted" style="font-size:12.5px">已过滤 {{ data.filtered_out }} 只</span>
          <a class="linkbtn" :href="'#/settings?kind=' + kind" style="font-size:13px"><qw-icon name="sliders" :size="14"/>修改设置</a>
        </div>

        <qw-card :pad="false" :title="kindInfo.label + '名单'" :icon="kindInfo.icon" :sub="rows.length ? '共 ' + Math.max(rows.length, data.count || 0) + ' 只' + ((data.count || 0) > rows.length ? '（显示前 ' + rows.length + ' 只）' : '') + (pickCount ? '，★ 为重点关注的前 ' + pickCount + ' 名' : '') + '，点击一行看详情' : ''">
          <template #extra><span class="qw-tag" :class="isSwing ? 'blue' : ''">{{ kindInfo.badge }}</span></template>
          <template v-if="rows.length">
            <div v-if="gate && !gate.trade" class="pred-oneword pred-nogo"><qw-icon name="pause" :size="14"/><span>今天没有一只的预期收益达到 {{ thText }}<template v-if="topExp != null">（最高只有 {{ $fmt.pct(topExp, 2) }}）</template>，下面的排名<b>只是参考</b>：今天不操作，模拟盘也记为“不操作”。</span></div>
            <div v-if="commonReasons.length" class="pred-common">
              <span class="pcm-hd"><qw-icon name="pulse" :size="14"/>今天所有候选都一样的市场因素：</span>
              <span v-for="c in commonReasons.slice(0, 4)" :key="c.key" class="pcm-item" :class="reasonCls(c.text)">{{ reasonIcon(c.text) }} {{ c.text }}</span>
              <span v-if="commonReasons.length > 4" class="pcm-note">等 {{ commonReasons.length }} 条</span>
              <span class="pcm-note">（下面的“主要理由”只列每只股票自己的）</span>
            </div>
            <div v-if="pickOneWord" class="pred-oneword" v-tip="ONE_WORD_TIP"><qw-icon name="alert" :size="14"/><span>★ 前 {{ pickCount }} 名里有 {{ pickOneWord }} 只今天是<b>一字板</b>：明天很可能一开盘就涨停，普通人排队也买不到。</span></div>
            <div v-if="store.isPhone" class="pred-cards" style="padding:4px 12px 12px">
              <div v-for="r in phoneRows" :key="r.code" class="pred-card" :class="{pick: r.pick}" @click="openRow(r)">
                <div class="r1">
                  <span class="rank num" :class="{'pick-star': r.pick}">{{ r.pick ? '★' : r._rank }}</span>
                  <qw-stock :code="r.code" :name="r.name"/>
                  <qw-streak v-if="kind === 'streak'" :n="r.streak" :one-word="r.one_word"/>
                  <span style="margin-left:auto"><qw-price :value="r.pct" pct/></span>
                </div>
                <div v-if="isSwing" class="row" style="flex-wrap:nowrap">
                  <span class="muted" style="font-size:12.5px">预期收益</span><b class="num exp-big" :class="expCls(r)">{{ $fmt.pct(r._exp, 2) }}</b>
                  <span style="margin-left:auto"><qw-minibars :dims="r.dims" :height="22"/></span>
                </div>
                <div v-else class="row" style="flex-wrap:nowrap"><span class="muted" style="font-size:12px;white-space:nowrap">涨停概率</span><qw-pctbar :value="r.score" tone="heat" style="flex:1"/><span v-if="Math.abs(r._boost) >= 0.02" class="boost-tag" :class="r._boost > 0 ? 'plus' : 'minus'">{{ r._boost > 0 ? '含加分' : '含减分' }}</span><qw-minibars :dims="r.dims" :height="22"/></div>
                <div class="r3 ellipsis">{{ mainReason(r) }}</div>
              </div>
              <button v-if="rows.length > phoneLimit" class="btn block" @click="phoneLimit += 50">再显示 {{ Math.min(50, rows.length - phoneLimit) }} 只（共 {{ rows.length }} 只）</button>
            </div>
            <qw-table v-else :columns="columns" :rows="rows" row-key="code" clickable max-height="calc(100vh - 220px)"
              :row-class="(r) => r.pick ? 'pick' : ''" :active-key="drawerOpen && cur ? cur.code : null" @row-click="openRow">
              <template #cell-_rank="{row}"><span class="rank-cell num"><span class="pick-star" :style="{visibility: row.pick ? 'visible' : 'hidden'}">★</span>{{ row._rank }}</span></template>
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
              <template #cell-industry="{row}"><span class="ellipsis" style="display:inline-block;max-width:96px;vertical-align:middle" v-tip="row.industry">{{ $fmt.industry(row.industry) }}</span></template>
              <template #cell-streak="{row}"><qw-streak :n="row.streak" :one-word="row.one_word"/></template>
              <template #cell-pct="{row}"><qw-price :value="row.pct" pct/></template>
              <template #cell-turn="{row}"><span class="num">{{ row.turn == null ? '—' : $fmt.num(row.turn, 1) + '%' }}</span></template>
              <template #cell-float_cap="{row}"><span class="num">{{ $fmt.cap(row.float_cap) }}</span></template>
              <template #cell-_exp="{row}"><b class="num" :class="expCls(row)">{{ $fmt.pct(row._exp, 2) }}</b></template>
              <template #cell-score="{row}"><span class="score-cell"><qw-pctbar :value="row.score" tone="heat"/><span v-if="Math.abs(row._boost) >= 0.02" class="boost-tag" :class="row._boost > 0 ? 'plus' : 'minus'" v-tip="boostText(row)">{{ row._boost > 0 ? '含加分' : '含减分' }}</span></span></template>
              <template #cell-dims="{row}"><qw-minibars :dims="row.dims"/></template>
              <template #cell-reason="{row}"><div class="reason-cell ellipsis" v-tip="(row.reasons || []).join('\\n')">{{ mainReason(row) || '—' }}</div></template>
              <template #cell-actions="{row}">
                <span class="row" style="justify-content:flex-end;flex-wrap:nowrap;gap:4px">
                  <a class="qw-iconbtn" style="width:30px;height:30px" :href="'#/watch/' + row.code" @click.stop v-tip="'看盘'" aria-label="看盘"><qw-icon name="candle" :size="15"/></a>
                  <button class="qw-iconbtn" style="width:30px;height:30px" :style="inWatch(row.code) ? 'color:var(--gold)' : ''" @click.stop="toggleWatch(row)" v-tip="inWatch(row.code) ? '已在自选，点击移除' : '加入自选'" :aria-label="inWatch(row.code) ? '移出自选' : '加入自选'"><qw-icon :name="inWatch(row.code) ? 'starFill' : 'star'" :size="15"/></button>
                </span>
              </template>
            </qw-table>
          </template>
          <qw-empty v-else-if="data.filtered_out" icon="filter" title="今天的候选股都被筛选条件过滤掉了"
            :desc="'共有 ' + data.filtered_out + ' 只候选股不满足你的条件，可以把条件放宽一些。'" action-text="修改设置" :action-to="'/settings?kind=' + kind"/>
          <qw-empty v-else-if="isSwing" icon="inbox" title="今天没有符合条件的强势股" desc="今天涨 5% 以上又没涨停的主板股很少（或者数据还没更新到最新交易日）。没有就不操作，明天收盘后再来看。">
            <button class="btn" @click="oneClick"><qw-icon name="refresh" :size="15"/>一键更新</button>
          </qw-empty>
          <qw-empty v-else icon="inbox" title="今天没有候选股票" desc="可能是数据还没更新到最新交易日。更新完成后名单会自动出现。">
            <button class="btn primary" @click="oneClick"><qw-icon name="refresh" :size="15"/>一键更新</button>
          </qw-empty>
        </qw-card>

        <qw-card v-if="(data.warnings && data.warnings.length) || (data.notes && data.notes.length) || btNotes.length" title="注意事项" icon="info">
          <ul class="warn-list">
            <li v-for="(w, i) in (data.warnings || [])" :key="'w' + i"><qw-icon name="alert" :size="14"/><span>{{ w }}</span></li>
            <li v-for="(w, i) in btNotes" :key="'b' + i" :class="{note: !/资金不足|一手太贵|跳过/.test(w)}"><qw-icon :name="/资金不足|一手太贵|跳过/.test(w) ? 'alert' : 'info'" :size="14"/><span>{{ w }}</span></li>
            <li v-for="(w, i) in (data.notes || [])" :key="'n' + i" class="note"><qw-icon name="info" :size="14"/><span>{{ w }}</span></li>
          </ul>
        </qw-card>
      </template>

      <qw-drawer v-model="drawerOpen" width="560px">
        <template #title>
          <span v-if="cur" class="row" style="gap:8px"><span>{{ cur.name }}</span><span class="muted num" style="font-weight:400">{{ cur.code }}</span><qw-board :board="cur.board"/><qw-streak v-if="kind === 'streak'" :n="cur.streak" :one-word="cur.one_word"/></span>
        </template>
        <template v-if="cur">
          <div class="grid" style="grid-template-columns:repeat(3, minmax(0,1fr));gap:10px">
            <template v-if="isSwing">
              <qw-stat label="预期收益" :value="$fmt.pct(cur._exp, 2)" :tone="$fmt.dir(cur._exp)" :help="HELP.expRet" flat/>
              <qw-stat label="今日涨幅" :value="$fmt.pct(cur.pct, 2)" :tone="$fmt.dir(cur.pct)" flat/>
              <qw-stat label="收盘价" :value="$fmt.price(cur.close)" flat/>
            </template>
            <template v-else>
              <qw-stat label="涨停概率" :value="$fmt.ratio(cur.score, 1)" tone="up" :help="HELP.score" flat/>
              <qw-stat label="模型原始概率" :value="$fmt.ratio(cur.prob, 1)" help="没有经过你的权重调整的模型原始输出。" flat/>
              <qw-stat label="今日涨幅" :value="$fmt.pct(cur.pct, 2)" :tone="$fmt.dir(cur.pct)" flat/>
            </template>
          </div>
          <div class="row muted section-gap" style="font-size:13px;gap:14px">
            <span>行业：{{ $fmt.industry(cur.industry) }}</span><span v-if="!isSwing">收盘 {{ $fmt.price(cur.close) }}</span>
            <span>换手 {{ cur.turn == null ? '—' : $fmt.num(cur.turn, 1) + '%' }}</span><span>流通市值 {{ $fmt.cap(cur.float_cap) }}</span>
            <span>排名第 {{ cur._rank }}<template v-if="cur.pick"> · ★重点关注</template></span>
          </div>
          <div v-if="isSwing" class="drawer-note">
            <qw-icon name="info" :size="15"/>
            <span>预期收益是模型按历史上类似股票估算的<b>平均值</b>，单只股票的实际结果波动很大，亏 5% 以上也很常见。它目前只在模拟盘里跟踪，不建议直接用真钱。</span>
          </div>
          <div v-else class="drawer-note warn">
            <qw-icon name="alert" :size="15"/>
            <span>这是<b>观察名单</b>：模型能看出谁更可能涨停，但历史上照着买是亏的（能买进的往往第二天冲高回落）。</span>
          </div>
          <div v-if="!isSwing && Math.abs(cur._boost) >= 0.02" class="drawer-note">
            <qw-icon name="info" :size="15"/>
            <span>涨停概率比模型原始概率{{ cur._boost > 0 ? '高' : '低' }}了 <b class="num">{{ Math.abs(cur._boost * 100).toFixed(1) }}</b> 个百分点<template v-if="newsWeight > 0">，主要来自消息面（最近24小时相关消息 {{ cur.news_count ?? 0 }} 条、政策 {{ cur.policy_count ?? 0 }} 条）</template>。只按模型排，它是第 {{ cur._modelRank }} 名。消息热度≠利好，新闻多也可能是利空。</span>
          </div>
          <div v-if="cur.one_word && !isSwing" class="drawer-note warn">
            <qw-icon name="alert" :size="15"/><span>{{ ONE_WORD_TIP }}</span>
          </div>

          <div class="drawer-sec"><h4>五维评分 <qw-help :text="HELP.dims"/></h4>
            <qw-radar :dims="cur.dims" height="260px"/>
            <div class="chips-wrap" style="justify-content:center">
              <span v-for="d in dimList(cur.dims)" :key="d.k" class="chip static" v-tip="d.help">{{ d.label }} <b class="num" :class="d.v >= 55 ? 'up' : d.v <= 45 ? 'down' : ''">{{ d.v }}</b></span>
            </div>
          </div>

          <div class="drawer-sec"><h4>模型给出的理由</h4>
            <ul v-if="cur.reasons && cur.reasons.length" class="reasons"><li v-for="(r, i) in cur.reasons" :key="i" :class="reasonCls(r)"><span class="ri">{{ reasonIcon(r) }}</span><span>{{ plainReason(r) }}</span><span v-if="isCommon(r)" class="qw-tag rs-common" v-tip="'今天所有候选股这一条都差不多，是整个市场的情况，不是这只股票自己的特点。'">全市场</span></li></ul>
            <p v-else class="muted">没有理由数据。</p>
          </div>

          <div class="drawer-sec"><h4>最近60个交易日</h4>
            <template v-if="mini[cur.code] && mini[cur.code].bars">
              <qw-kline :bars="mini[cur.code].bars" :limit-days="mini[cur.code].limit_days || []" height="230px" compact :initial-bars="60"/>
              <div class="chart-note"><span><i class="mk"></i>= 涨停日</span><a :href="'#/watch/' + cur.code">看完整K线 →</a></div>
            </template>
            <p v-else-if="mini[cur.code] && mini[cur.code].error" class="muted">K线暂时拿不到：{{ mini[cur.code].error }}</p>
            <qw-skeleton v-else height="230px"/>
          </div>

          <div class="drawer-sec"><h4>消息面 <qw-help :text="HELP.news"/></h4>
            <p style="font-size:13.5px">最近24小时相关消息 <b class="num">{{ cur.news_count ?? 0 }}</b> 条，其中政策相关 <b class="num">{{ cur.policy_count ?? 0 }}</b> 条。
              <a href="#/news">看消息政策 →</a></p>
            <p class="muted" style="font-size:12px;margin-top:4px">消息热度≠利好；消息面只有实时数据，没有参与模型训练和回测。</p>
          </div>
        </template>
        <template #footer>
          <template v-if="cur">
            <button class="btn" :class="{'active-star': inWatch(cur.code)}" @click="toggleWatch(cur)"><qw-icon :name="inWatch(cur.code) ? 'starFill' : 'star'" :size="15"/>{{ inWatch(cur.code) ? '已自选' : '加自选' }}</button>
            <a class="btn primary" :href="'#/watch/' + cur.code"><qw-icon name="candle" :size="15"/>去看盘</a>
          </template>
        </template>
      </qw-drawer>
    </div>`,
  });
})();
