/* 模型中心 #/model：三个模型的定位（模拟跟踪 / 观察）、训练状态与重新训练、强势股波段的逐笔交易成绩（选择期/留出期、随机基准）、
   滚动检验各期表现、各维度重要性、概率校准、前N名命中率、已知局限 */
(function () {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, jobs, isNum, colors, tooltipBase, axisBase, LABELS, DIM_HELP, normTrade, tWord, T_HELP, tHelp, KIND_VALUES, BASE_HELP, MATCH_HELP, UNCAPPED_HELP, holdLabel, recentVerdict, HOLD_CAVEAT } = QW;

  const SHORT = {
    swing: "今天涨 5% 以上、没封涨停的主板股，估算明天开盘买、按规则卖出平均能赚多少",
    streak: "今天涨停的股票，明天还能不能继续涨停",
    first: "最近5天没涨停过的股票，明天会不会第一次涨停",
  };
  const KINDS = QW.KINDS.map((k) => ({ ...k, desc: SHORT[k.value] || k.desc }));
  const GROUPS = ["sentiment", "capital", "fundamental", "theme", "technical"];
  const HELP = {
    oos: "“样本外”是指模型训练时没见过的那段历史。每一期都只用之前的数据训练，再去预测之后半年，相当于“闭卷考试”，尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观。",
    auc: "AUC：衡量模型把“第二天会涨停”和“不会涨停”的股票区分开的能力。0.5 等于瞎猜，1 是完美；在股市里 0.6~0.7 已经算有用。",
    top5: "每天挑模型打分最高的 5 只，第二天真的收盘涨停的比例（样本外）。",
    base: "同一时期全部候选股第二天涨停的平均比例，相当于“闭着眼睛随便挑”的命中率。",
    folds: "把历史按半年切成很多段，每一段都用它之前的数据训练、再预测这一段。每段的成绩都列在这里，看模型是不是一直有效。",
    imp: "模型在做判断时，每一方面的信息用得多不多（按对预测的贡献占比）。占比越高，说明模型越依赖这一面。",
    calib: "把模型给出的概率分成几档，看每一档“说的概率”和“实际涨停比例”是否接近。两根柱子越接近，说明概率越靠谱。",
    topn: "每天只看模型排名前 N 的股票，第二天真的涨停的比例。",
    samples: "训练用到的“股票·交易日”样本数量（有明确结果的）。",
    mean: "用模型没见过的历史，每天按名单买入、按规则卖出，平均每一笔赚或亏多少（已扣手续费、印花税和滑点）。",
    rnd: BASE_HELP,
    match: MATCH_HELP,
    acct: "按你在「预测设置」里的本金和仓位，把样本外那段历史真实模拟一遍：每笔佣金最低 5 元、一手 100 股买不起就跳过。比“模型检验”（按比例收费、不受资金限制）更接近你真照着做的结果。",
    period: "选择期（2025-07 以前）：研究时比较多种做法、挑出方案的那段历史；" + HOLD_CAVEAT + "留出期比选择期更接近现在的真实水平；行业/ST 用当前归属，也会偏乐观一点。" +
      "每次重新训练，成绩都会有些波动（交易笔数不多）；页面上是这一次训练的真实结果，不要往好的方向猜。",
    ic: "IC：每天模型排的先后，和股票实际收益先后的一致程度。0 等于瞎猜，0.03~0.05 以上在股市里就算有用。",
    years: "每一年按名单买卖的平均结果（样本外，模型检验：按比例收费、不受资金限制，比按你的本金回测偏乐观），看是不是只靠某一年赚的。一年少于 30 笔的，样本太少，看不出什么。",
    recent: "先看最近一段：" + HOLD_CAVEAT + "左边是这一段里按规则买卖的平均每笔；右边是同一段时间里、只在模型出手的那些天，从同一批候选股里随便挑同样多只的平均每笔（随机 20 次平均）。左边要明显高于右边，才说明模型真的会挑股票。",
    full: "全部样本外 = 选择期 + 留出期。选择期是研究时比较多种做法、挑出方案的那段历史，放在一起算会偏乐观，所以上面的大数字只看最近一段（留出期）。",
  };
  const ROLE = {
    swing: { tone: "sim", title: "定位：模拟跟踪中", text: "它是三个模型里唯一按“能赚多少”训练、在模拟盘里跟踪的，但还没有证明自己：按小资金真实算（每笔佣金最低 5 元、贵的股票买不起一手）收益更低，最近一段（留出期）是不是比“同一天随便挑”更好，以上面的数字为准；每次重新训练成绩也会波动。每天达标的股票会自动记进模拟盘，攒够几十笔、结果明显好过随便挑，再考虑小仓位用真钱。", link: "#/paper", linkText: "去模拟盘看前向跟踪" },
    streak: { tone: "watch", title: "定位：观察名单", text: "它能看出谁明天更可能继续涨停，但按真实规则照着买是亏的（好票多半一开盘就买不进）。用来看热点、看懂连板梯队，不作为买入信号。" },
    first: { tone: "watch", title: "定位：观察名单", text: "它能看出谁更可能第一次涨停，但按真实规则照着买是亏的。用来发现可能启动的股票，不作为买入信号。" },
  };
  const LIMITS = [
    { icon: "target", title: "只用样本外成绩", text: "页面上的命中率、收益都只来自“样本外”预测：每一期都只用之前的数据训练，再预测之后半年，尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观。" },
    { icon: "trend", title: "分清“挑日子”和“挑股票”", text: "强势股波段比“天天随便挑”多赚的部分，有一部分来自“在合适的日子出手”（那几天同池随便挑也赚），只有比“同一天随便挑同样多只”多赚的才是挑股票的本事（具体数字见上面的逐笔成绩），所以要和两种随机挑选对比着看。" },
    { icon: "alert", title: "ST 状态是近似的", text: "ST 状态用股票现在的名字判断（名字带“退”的也按 ST 算），并套用到全部历史，个别股票早年的状态可能不准。" },
    { icon: "building", title: "行业用的是现在的归属", text: "行业按股票现在所属的行业计算，历史上换过行业的股票会有轻微“后视偏差”。" },
    { icon: "news", title: "消息面没有参与训练", text: "新闻和政策只有最近的实时数据、没有历史，所以不参与训练和回测；消息热度≠利好，默认 0 不参与排序。" },
    { icon: "grid", title: "概念板块数据暂缺", text: "概念板块成分股（东方财富）本机取不到，“题材政策面”暂时用行业代替，接口恢复后可以补上。" },
    { icon: "history", title: "过去不代表未来", text: "市场风格会变，模型在历史上有效，不保证以后一直有效。模型超过 30 天没重新训练，“一键更新”会自动重训。" },
  ];

  const pctNum = (v) => (isNum(v) ? +(fmt.frac(v, 1.5) * 100).toFixed(1) : null);
  const half = (d) => (d ? String(d).slice(2, 4) + (+String(d).slice(5, 7) <= 6 ? "上" : "下") : "");

  function foldOption(folds, c) {
    const ax = axisBase(c);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: c.text2, fontSize: 12 } },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", axisPointer: { type: "shadow" }, confine: true,
        formatter: (ps) => {
          const f = folds[ps[0].dataIndex];
          return `<div style="font-weight:700;margin-bottom:4px">第 ${f.fold} 期：${f.test_start} ~ ${f.test_end}</div>` +
            `<div>前5名次日涨停率 <b>${fmt.ratio(fmt.frac(f.top5_hit, 1.5), 1)}</b></div>` +
            `<div>全部候选平均 <b>${fmt.ratio(fmt.frac(f.base_rate, 1.5), 1)}</b></div>` +
            `<div>AUC <b>${fmt.num(f.auc, 3)}</b></div>` +
            `<div style="opacity:.7">用 ${f.train_end} 及以前的数据训练</div>`;
        },
      },
      grid: { left: 44, right: 8, top: 34, bottom: 26 },
      xAxis: { type: "category", data: folds.map((f) => half(f.test_start)), ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, interval: 0, hideOverlap: true } },
      yAxis: { type: "value", ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: [
        { name: "模型前5名次日涨停率", type: "bar", data: folds.map((f) => pctNum(f.top5_hit)), barMaxWidth: 16, barGap: "15%", itemStyle: { color: c.series[1], borderRadius: [4, 4, 0, 0] } },
        { name: "全部候选平均（基准）", type: "bar", data: folds.map((f) => pctNum(f.base_rate)), barMaxWidth: 16, itemStyle: { color: c.bands[2], borderRadius: [4, 4, 0, 0] } },
      ],
    };
  }

  function impOption(items, c) {
    const ax = axisBase(c);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      tooltip: {
        ...tooltipBase(c), trigger: "item",
        formatter: (p) => `<b>${items[p.dataIndex].label}</b>：${p.value}%<br><span style="opacity:.75">${DIM_HELP[items[p.dataIndex].key] || ""}</span>`,
        extraCssText: "max-width:260px;white-space:normal;box-shadow:0 6px 20px rgba(0,0,0,.14);border-radius:8px;",
      },
      grid: { left: 8, right: 44, top: 4, bottom: 4, containLabel: true },
      xAxis: { type: "value", max: (v) => Math.max(10, Math.ceil(v.max / 10) * 10), ...ax, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
      yAxis: { type: "category", inverse: true, data: items.map((x) => x.label), ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, color: c.text2, fontSize: 12.5 } },
      series: [{
        type: "bar", barWidth: 16, data: items.map((x) => x.value),
        itemStyle: { color: c.series[0], borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: c.text2, fontSize: 12, formatter: "{c}%" },
      }],
    };
  }

  function calibOption(rows, c) {
    const ax = axisBase(c);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: c.text2, fontSize: 12 } },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", axisPointer: { type: "shadow" }, confine: true,
        formatter: (ps) => {
          const r = rows[ps[0].dataIndex];
          return `<div style="font-weight:700;margin-bottom:4px">模型概率在 ${r.bucket}</div>` +
            `<div>模型平均给出 <b>${fmt.ratio(fmt.frac(r.pred, 1.5), 1)}</b></div>` +
            `<div>实际第二天涨停 <b>${fmt.ratio(fmt.frac(r.actual, 1.5), 1)}</b></div><div style="opacity:.7">共 ${fmt.int(r.n)} 个样本</div>`;
        },
      },
      grid: { left: 44, right: 8, top: 34, bottom: rows.length > 7 ? 42 : 26 },
      xAxis: {
        type: "category", data: rows.map((r) => r.bucket), ...ax, splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, interval: 0, rotate: rows.length > 7 ? 30 : 0, fontSize: rows.length > 7 ? 10 : 11 },
      },
      yAxis: { type: "value", ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: [
        { name: "模型说的概率", type: "bar", data: rows.map((r) => pctNum(r.pred)), barMaxWidth: 16, barGap: "15%", itemStyle: { color: c.series[0], borderRadius: [4, 4, 0, 0] } },
        { name: "实际涨停比例", type: "bar", data: rows.map((r) => pctNum(r.actual)), barMaxWidth: 16, itemStyle: { color: c.series[1], borderRadius: [4, 4, 0, 0] } },
      ],
    };
  }

  const emptyMap = (v) => Object.fromEntries(KIND_VALUES.map((k) => [k, v]));

  QW.page("model", {
    props: ["params", "query"],
    setup(props) {
      const reports = ref(emptyMap(null));
      const errors = ref(emptyMap(""));
      const loading = ref(true);
      const jobIds = ref(emptyMap(null));
      const q = props.query && props.query.kind;
      const detailKind = ref(KIND_VALUES.includes(q) ? q : "swing");

      const loadOne = async (kind) => {
        try {
          const r = await api.get("/api/model/report", { kind }, { silent: true });
          reports.value = { ...reports.value, [kind]: r };
          errors.value = { ...errors.value, [kind]: "" };
        } catch (e) {
          reports.value = { ...reports.value, [kind]: null };
          // 404 = 还没训练；400/422 = 后台还不认识这个类型，也当作还没训练
          errors.value = { ...errors.value, [kind]: [400, 404, 422].includes(e.status) ? "" : e.detail || e.message };
        }
      };
      // streak/first 的 meta 没有逐笔成绩时，按当前买卖规则回测一次：命中率高不等于能赚钱；
      // 波段另按用户的本金/仓位回测一次（最低佣金 5 元、一手 100 股），作为主数字（模型检验不受资金限制、偏乐观）
      const bts = ref(emptyMap(null));
      const btCap = ref(emptyMap(null));
      const btBusy = ref(emptyMap(false));
      const loadBt = async (kind) => {
        btBusy.value = { ...btBusy.value, [kind]: true };
        try {
          const r = await api.post("/api/predict/backtest", { predict: { kind } }, { silent: true });
          const t = r && r.settings_used && r.settings_used.trade;
          bts.value = { ...bts.value, [kind]: normTrade(r) };
          btCap.value = { ...btCap.value, [kind]: t && isNum(t.capital) ? t.capital : null };
        } catch (e) { bts.value = { ...bts.value, [kind]: null }; } finally { btBusy.value = { ...btBusy.value, [kind]: false }; }
      };
      const load = async () => {
        loading.value = true;
        await Promise.all(KINDS.map((k) => loadOne(k.value)));
        loading.value = false;
        KINDS.forEach((k) => {
          const r = reports.value[k.value];
          const t = r ? normTrade(r.trade_oos) : null;
          if (r && (k.value === "swing" || !(t && isNum(t.mean)))) loadBt(k.value);
        });
      };
      onMounted(load);
      const off = bus.on("job-done", (j) => { if (j && (j.name === "train" || j.name === "daily")) load(); });
      onBeforeUnmount(off);

      const runningTrain = computed(() => Object.values(store.jobs).find((j) => j.status === "running" && j.name === "train") || null);
      // 别处启动的训练任务：按标题判断属于哪个模型（"全部模型"都算）
      const jobFor = (kind) => {
        if (jobIds.value[kind]) return jobIds.value[kind];
        const j = runningTrain.value;
        if (!j) return null;
        const label = (KINDS.find((k) => k.value === kind) || {}).label;
        const t = j.title || "";
        return t.includes(label) || t.includes(kind) || t.includes("全部") || t.includes("all") || !/连板晋级|首板潜力|强势股波段|streak|first|swing/.test(t) ? j.id : null;
      };
      const train = async (kind) => {
        const label = kind === "all" ? "全部模型" : (KINDS.find((k) => k.value === kind) || {}).label;
        try {
          const id = await jobs.start("/api/jobs/train", { kind }, `训练模型（${label}）`);
          if (kind === "all") jobIds.value = emptyMap(id);
          else jobIds.value = { ...jobIds.value, [kind]: id };
        } catch (e) { /* 已提示 */ }
      };
      const busy = (kind) => {
        const id = jobFor(kind);
        return !!(id && (!store.jobs[id] || store.jobs[id].status === "running"));
      };
      const onDone = (kind) => { jobIds.value = { ...jobIds.value, [kind]: null }; load(); };

      const summary = (kind) => {
        const r = reports.value[kind];
        if (!r) return null;
        const oos = r.oos || {};
        const top5 = fmt.frac(isNum(oos.top5_hit) ? oos.top5_hit : r.top5_hit, 1.5);
        const base = fmt.frac(isNum(oos.base_rate) ? oos.base_rate : r.base_rate, 1.5);
        const auc = isNum(oos.auc) ? oos.auc : r.auc;
        let age = null;
        const t = String(r.trained_at || "").slice(0, 10);
        if (t && store.today) age = Math.round((Date.parse(store.today) - Date.parse(t)) / 86400000);
        const trade = normTrade(r.trade_oos);
        const acct = kind === "swing" && bts.value.swing && isNum(bts.value.swing.mean) ? bts.value.swing : null;
        const cap = btCap.value[kind];
        return {
          top5, base, auc, mult: isNum(top5) && isNum(base) && base > 0 ? top5 / base : null, age,
          stale: age != null && age > 30, n: r.n_samples, pos: r.n_positive,
          trade: trade && isNum(trade.mean) ? trade : bts.value[kind] && isNum(bts.value[kind].mean) ? { ...bts.value[kind], fromBt: true } : null,
          acct, capText: isNum(cap) && cap > 0 ? (cap >= 1e4 ? fmt.num(cap / 1e4, cap % 1e4 ? 1 : 0) + " 万" : fmt.int(cap) + " 元") : "你的本金",
          ic: isNum(oos.ic) ? oos.ic : isNum(oos.daily_ic) ? oos.daily_ic : isNum(r.ic) ? r.ic : null,
          seeds: Array.isArray(r.seeds) ? r.seeds.length : 0,
        };
      };
      const summaries = computed(() => Object.fromEntries(KINDS.map((k) => [k.value, summary(k.value)])));
      // t 值以留出期为准（方案是在选择期里挑出来的，全部样本外的 t 偏乐观）；波段取“按你的本金”和“模型检验”里低的那个
      const swingT = computed(() => {
        const s = summaries.value.swing;
        const t = s && s.trade;
        const mt = t && t.hold && isNum(t.hold.t) ? t.hold.t : null;
        const at = s && s.acct && s.acct.hold && isNum(s.acct.hold.t) ? s.acct.hold.t : null;
        if (isNum(at) && (!isNum(mt) || at <= mt)) return { t: at, label: "留出期 ", src: "acct", other: mt };
        if (isNum(mt)) return { t: mt, label: "留出期 ", src: "meta", other: at };
        return { t: t ? t.t : null, label: "", src: "meta", other: null };
      });
      // 主数字：按你的本金回测（拿不到时用模型检验）
      const swingMain = computed(() => {
        const s = summaries.value.swing;
        if (!s) return null;
        if (s.acct) return { ...s.acct, src: "acct" };
        return s.trade ? { ...s.trade, src: "meta" } : null;
      });
      // 主数字：最近一段（留出期）按你的本金回测的平均每笔 vs 同一段“同一天随便挑同样多只”；结论按数据算（QW.recentVerdict）
      const swingRecent = computed(() => {
        const s = summaries.value.swing;
        if (!s) return null;
        let src = null;
        let part = null;
        if (s.acct && s.acct.hold && isNum(s.acct.hold.mean)) { src = "acct"; part = s.acct.hold; }
        else if (btBusy.value.swing) return { loading: true };
        else if (s.trade && s.trade.hold && isNum(s.trade.hold.mean)) { src = "meta"; part = s.trade.hold; }
        if (!part) return null;
        const rnd = part.match && isNum(part.match.mean) ? part.match.mean : null;
        const t = swingT.value && swingT.value.label ? swingT.value.t : part.t;
        return { src, mean: part.mean, n: part.n, rnd, t, label: holdLabel(part), verdict: recentVerdict(part.mean, rnd, t) };
      });
      const swingSay = computed(() => {
        const s = summaries.value.swing;
        const x = swingMain.value;
        if (!s || !x || !isNum(x.mean)) return null;
        const r = swingRecent.value;
        if (r && r.loading) return null;
        if (r && r.verdict) {
          const src = r.src === "acct" ? `按你的本金（${s.capText}）回测` : "模型检验（不受资金限制，偏乐观）";
          let txt = `一句话：${r.verdict.text}。${r.label}${src}平均每笔 ${fmt.ratio(r.mean, 2, true)}`;
          if (isNum(r.rnd)) txt += `，同一天随便挑同样多只 ${fmt.ratio(r.rnd, 2, true)}`;
          if (isNum(r.t)) txt += `，t 值 ${fmt.t(r.t)}（${tWord(r.t)}）`;
          txt += `。全部样本外 ${fmt.ratio(x.mean, 2, true)}/笔，里面包含研究时挑方案用的那段历史，偏乐观`;
          if (x.src === "acct" && s.trade && isNum(s.trade.mean)) txt += `；模型检验（不受资金限制）${fmt.ratio(s.trade.mean, 2, true)}/笔，更偏乐观`;
          txt += s.seeds > 1 ? `。样本外成绩已是 ${s.seeds} 个随机种子取平均的结果，但每次重新训练仍会有波动，先用模拟盘跟踪` : "。每次重新训练成绩都会有波动，先用模拟盘跟踪";
          return { text: txt + "。", bad: r.verdict.tone === "warn" };
        }
        const edge = x.base && isNum(x.base.mean) ? x.mean - x.base.mean : null;
        const pick = x.match && !x.match.same && isNum(x.match.mean) ? x.mean - x.match.mean : null;
        let txt = x.src === "acct" ? `按你的本金（${s.capText}）回测，样本外平均每笔 ${fmt.ratio(x.mean, 2, true)}（已扣费用，含最低佣金）`
          : `模型检验（不受资金限制，偏乐观）样本外平均每笔 ${fmt.ratio(x.mean, 2, true)}`;
        if (x.hold && isNum(x.hold.mean)) txt += `，其中留出期 ${fmt.ratio(x.hold.mean, 2, true)}`;
        if (isNum(edge)) txt += `；比天天随便挑多 ${fmt.num(edge * 100, 2)} 个百分点`;
        if (isNum(pick)) {
          txt += (isNum(edge) && pick < edge / 2 ? "，但其中一大半来自择时" : "") +
            `，只比挑股票（同一天随便挑同样多只）多 ${fmt.num(pick * 100, 2)} 个百分点`;
        }
        const tt = swingT.value;
        if (isNum(tt.t)) txt += `；${tt.label}t 值 ${fmt.t(tt.t)}，${tWord(tt.t)}`;
        if (x.src === "acct" && s.trade && isNum(s.trade.mean)) txt += `。模型检验（不受资金限制）是 ${fmt.ratio(s.trade.mean, 2, true)}/笔，偏乐观`;
        // 不写研究里的 t 值范围（和当前模型对不上）；种子个数取自模型的 meta.seeds
        txt += s.seeds > 1 ? `。样本外成绩已是 ${s.seeds} 个随机种子取平均的结果，但每次重新训练仍会有波动，先用模拟盘跟踪`
          : "。每次重新训练成绩都会有波动，先用模拟盘跟踪";
        return { text: txt + "。", bad: x.mean <= 0 || (isNum(edge) && edge <= 0) || !isNum(tt.t) || tt.t < 2 };
      });
      const periodRows = (t, prefix) => {
        if (!t) return [];
        const rows = [["sel", "选择期", t.sel], ["hold", "留出期", t.hold], ["all", "全部", t]].filter((x) => x[2]);
        return rows.map(([k, label, o]) => ({
          k: prefix + k, label, n: o.n, mean: o.mean, bmean: o.base ? o.base.mean : null,
          mmean: o.match && !o.match.same ? o.match.mean : null, win: o.win, t: o.t,
          span: o.start && o.end ? `${fmt.date(o.start).slice(0, 7)} ~ ${fmt.date(o.end).slice(0, 7)}` : k === "sel" ? "2025-07 以前" : k === "hold" ? "2025-07 以后" : "",
        }));
      };
      // 两组：按你的本金回测（主）、模型检验（不受资金限制）
      const swingPeriods = computed(() => {
        const s = summaries.value.swing;
        if (!s) return [];
        const out = [];
        if (s.acct) out.push({ group: `按你的本金（${s.capText}）回测`, help: HELP.acct, rows: periodRows(s.acct, "a") });
        if (s.trade) out.push({ group: "模型检验（按比例收费、不受资金限制，偏乐观）", help: HELP.mean, rows: periodRows(s.trade, "m") });
        return out;
      });
      // t 值里有没有 Newey-West（后端 nw_t）：有时说明“取两种算法里低的”
      const swingNw = computed(() => {
        const s = summaries.value.swing;
        return !!(s && ((s.trade && s.trade.hasNw) || (s.acct && s.acct.hasNw)));
      });
      const swingTHelp = computed(() => tHelp({ hasNw: swingNw.value }));
      // 按你的本金回测时钱不够/一手太贵跳过了很多信号：另给“每个信号都买”的逐笔结果（后端 per_trade_uncapped）
      const swingSkip = computed(() => {
        const s = summaries.value.swing;
        const a = s && s.acct;
        return a && isNum(a.skipShare) && a.skipShare > QW.SKIP_HEAVY ? { share: a.skipShare, unc: a.uncapped } : null;
      });
      const periodCols = computed(() => [
        { key: "label", label: "", minWidth: "96px" },
        { key: "n", label: "笔数", align: "right" },
        { key: "mean", label: "平均每笔", align: "right", help: HELP.mean },
        { key: "bmean", label: "天天随便挑", align: "right", help: HELP.rnd },
        { key: "mmean", label: "同日同数量", align: "right", help: HELP.match },
        { key: "win", label: "胜率", align: "right" },
        { key: "t", label: "t 值", align: "right", help: swingTHelp.value },
      ]);

      const cur = computed(() => reports.value[detailKind.value]);
      const curSwing = computed(() => detailKind.value === "swing");
      const folds = computed(() => ((cur.value && cur.value.folds) || []).filter((f) => f && !f.skipped && isNum(f.top5_hit)));
      const allFolds = computed(() => ((cur.value && cur.value.folds) || []).filter((f) => f && !f.skipped));
      const skipped = computed(() => ((cur.value && cur.value.folds) || []).filter((f) => f && f.skipped).length);
      const foldRows = computed(() => folds.value.map((f) => {
        const t = fmt.frac(f.top5_hit, 1.5);
        const b = fmt.frac(f.base_rate, 1.5);
        return { ...f, _t: t, _b: b, _m: isNum(t) && isNum(b) && b > 0 ? t / b : null };
      }));
      // 波段的滚动检验：字段按实际有的来（样本数 / IC / 平均每笔 / 胜率 / 随机挑）
      const swingFoldRows = computed(() => allFolds.value.map((f) => ({
        ...f,
        _ic: [f.ic, f.daily_ic, f.rank_ic].find(isNum) ?? null,
        _mean: fmt.frac([f.top5_mean, f.mean, f.avg_return, f.top_mean, f.trade_mean].find(isNum) ?? null, 0.5),
        _win: fmt.frac([f.top5_win, f.win_rate, f.win].find(isNum) ?? null, 1.5),
        _base: fmt.frac([f.base_mean, f.random_mean, f.baseline_mean].find(isNum) ?? (f.baseline && [f.baseline.mean, f.baseline.avg_return].find(isNum)) ?? null, 0.5),
      })));
      const swingFoldCols = computed(() => {
        const rs = swingFoldRows.value;
        const has = (k) => rs.some((r) => isNum(r[k]));
        return [
          { key: "fold", label: "期", width: "44px" },
          { key: "range", label: "检验区间", help: HELP.folds },
          { key: "n", label: "样本", align: "right" },
          has("_ic") ? { key: "_ic", label: "IC", align: "right", help: HELP.ic } : null,
          has("_mean") ? { key: "_mean", label: "前5名每笔", align: "right", help: "每天模型排名前 5 的股票，按规则买卖的平均净收益（已扣费用，没加 1% 门槛）。" } : null,
          has("_base") ? { key: "_base", label: "全部平均", align: "right", help: "这一期全部候选股按同样规则买卖的平均净收益，相当于“随便挑”。前5名要明显高于它才算有用。" } : null,
          has("_win") ? { key: "_win", label: "胜率", align: "right" } : null,
        ].filter(Boolean);
      });
      const beatCount = computed(() => foldRows.value.filter((f) => f._m > 1).length);
      const foldOpt = computed(() => (folds.value.length ? foldOption(folds.value, colors()) : null));
      const impItems = computed(() => {
        const imp = (cur.value && cur.value.importance) || {};
        return GROUPS.filter((g) => isNum(imp[g])).map((g) => ({ key: g, label: LABELS[g], value: +(fmt.frac(imp[g], 1.5) * 100).toFixed(1) }))
          .sort((a, b) => b.value - a.value);
      });
      const impOpt = computed(() => (impItems.value.length ? impOption(impItems.value, colors()) : null));
      const topFeatures = computed(() => ((cur.value && cur.value.top_features) || []).slice(0, 10));
      const topMax = computed(() => Math.max(0.0001, ...topFeatures.value.map((f) => f.share || 0)));
      const calRows = computed(() => (curSwing.value ? [] : ((cur.value && cur.value.calibration) || []).filter((r) => r && r.n > 0)));
      const calOpt = computed(() => (calRows.value.length ? calibOption(calRows.value, colors()) : null));
      const topn = computed(() => {
        const r = cur.value;
        if (!r || curSwing.value) return [];
        const base = summaries.value[detailKind.value] ? summaries.value[detailKind.value].base : null;
        let list = Array.isArray(r.topn_hit) ? r.topn_hit : [];
        if (!list.length && r.oos) list = [1, 3, 5, 10].filter((n) => isNum(r.oos["top" + n + "_hit"])).map((n) => ({ n, hit_rate: r.oos["top" + n + "_hit"] }));
        return list.map((x) => {
          const h = fmt.frac(x.hit_rate, 1.5);
          return { ...x, h, mult: isNum(h) && isNum(base) && base > 0 ? h / base : null };
        });
      });
      const curTrade = computed(() => (cur.value ? normTrade(cur.value.trade_oos) : null));
      const years = computed(() => (curTrade.value ? curTrade.value.years : []));
      const yearMax = computed(() => Math.max(0.0001, ...years.value.map((y) => Math.abs(y.mean || 0))));
      const extraNotes = computed(() => {
        const known = /样本外|行业|ST|消息面|概念/;
        return ((cur.value && cur.value.notes) || []).filter((n) => !known.test(n));
      });
      const foldCols = [
        { key: "fold", label: "期", width: "44px" },
        { key: "range", label: "检验区间", help: HELP.folds },
        { key: "train_end", label: "训练数据截至" },
        { key: "n", label: "样本", align: "right" },
        { key: "auc", label: "AUC", align: "right", help: HELP.auc },
        { key: "_t", label: "前5名命中率", align: "right", help: HELP.top5 },
        { key: "_b", label: "基准率", align: "right", help: HELP.base },
        { key: "_m", label: "倍数", align: "right" },
      ];
      const kindTabs = computed(() => KINDS.map((k) => ({ value: k.value, label: k.label, badge: reports.value[k.value] ? k.badge : "未训练", tone: reports.value[k.value] ? k.tone : "" })));
      const anyReport = computed(() => KINDS.some((k) => reports.value[k.value]));
      const others = KINDS.filter((k) => k.value !== "swing");
      const swingKind = KINDS.find((k) => k.value === "swing");

      return {
        bts, btBusy, store, fmt, KINDS, others, swingKind, HELP, LIMITS, LABELS, ROLE, T_HELP, tWord, reports, errors, loading, load, train, jobFor, busy, onDone, runningTrain,
        summaries, swingSay, swingRecent, swingT, swingMain, swingPeriods, periodCols, swingTHelp, swingSkip, UNCAPPED_HELP, SKIP_HEAVY: QW.SKIP_HEAVY, detailKind, cur, curSwing, anyReport,
        folds, foldRows, swingFoldRows, swingFoldCols, skipped, beatCount, foldOpt, impItems, impOpt, topFeatures, topMax, calRows, calOpt, topn, years, yearMax,
        extraNotes, foldCols, kindTabs,
      };
    },
    template: `<div class="stack">
      <div class="model-intro">
        <p>模型用过去几年的行情“学习”规律。为了尽量如实反映效果，所有成绩都来自<b>样本外检验</b><qw-help :text="HELP.oos"/>：只用之前的数据训练，再去预测之后的日子。三个模型里，<b>强势股波段</b>在模拟跟踪，另外两个只作观察。</p>
        <button class="btn" :disabled="!!runningTrain" @click="train('all')"><qw-icon name="cpu" :size="15"/>全部重新训练</button>
      </div>

      <div class="grid model-sums">
        <qw-card class="ms-main" :icon="swingKind.icon">
          <template #title><span>{{ swingKind.label }}</span>
            <span class="qw-tag blue">{{ swingKind.badge }}</span>
            <span v-if="loading" class="qw-tag">读取中</span>
            <span v-else-if="!reports.swing" class="qw-tag warn">还没训练</span>
            <span v-else-if="summaries.swing.stale" class="qw-tag warn">{{ summaries.swing.age }} 天前训练 · 建议重训</span>
            <span v-else class="qw-tag green"><qw-icon name="check" :size="12"/>&nbsp;已训练</span>
          </template>
          <template #extra><button class="btn sm" :class="{primary: !reports.swing && !loading}" :disabled="busy('swing')" @click="train('swing')"><qw-icon name="refresh" :size="13" :class="{spin: busy('swing')}"/>{{ reports.swing ? '重新训练' : '开始训练' }}</button></template>
          <p class="muted" style="font-size:13px;margin-bottom:12px">{{ swingKind.desc }}</p>
          <qw-skeleton v-if="loading" :rows="4"/>
          <div v-else-if="reports.swing" class="ms-swing">
            <div class="ms-swing-l">
              <template v-if="swingRecent && !swingRecent.loading">
                <div class="ms-pgroup" style="margin-top:0">{{ swingRecent.label }}<template v-if="swingRecent.src === 'acct'">，按你的本金（{{ summaries.swing.capText }}）回测</template><template v-else>，模型检验（不受资金限制，偏乐观）</template><qw-help :text="HELP.recent + ' ' + (swingRecent.src === 'acct' ? HELP.acct : HELP.mean)"/></div>
                <div class="ms-nums">
                  <div class="trust-num"><div class="v num" :class="$fmt.dir(swingRecent.mean)">{{ $fmt.ratio(swingRecent.mean, 2, true) }}</div><div class="k">平均每笔<template v-if="swingRecent.n">（{{ $fmt.int(swingRecent.n) }} 笔）</template></div></div>
                  <div class="trust-vs">对比</div>
                  <div class="trust-num"><div class="v num" :class="$fmt.dir(swingRecent.rnd)">{{ swingRecent.rnd == null ? '—' : $fmt.ratio(swingRecent.rnd, 2, true) }}</div><div class="k">同一天随便挑<qw-help :text="HELP.match"/></div></div>
                  <div class="trust-num"><div class="v num">{{ $fmt.t(swingT.t) }}</div><div class="k">{{ swingT.label }}t 值<template v-if="swingT.other != null">（取低的）</template><qw-help :text="swingTHelp + (swingT.other != null ? ' 这里取“按你的本金回测”和“模型检验”两个留出期 t 值里低的那个；另一个是 ' + $fmt.t(swingT.other) + '。' : '') + ' ' + HELP.period"/></div></div>
                </div>
                <p v-if="swingMain" class="ms-sec">全部样本外<template v-if="swingMain.src === 'acct'">（按你的本金）</template> <b class="num" :class="$fmt.dir(swingMain.mean)">{{ $fmt.ratio(swingMain.mean, 2, true) }}</b>/笔<template v-if="swingMain.base && swingMain.base.mean != null"> · 天天随便挑 {{ $fmt.ratio(swingMain.base.mean, 2, true) }}</template><template v-if="swingMain.match && !swingMain.match.same && swingMain.match.mean != null"> · 同日同数量 {{ $fmt.ratio(swingMain.match.mean, 2, true) }}</template><qw-help :text="HELP.full + ' ' + HELP.rnd"/></p>
                <p v-if="swingMain && swingMain.src === 'acct' && summaries.swing.trade" class="ms-sec">模型检验（不受资金限制）<b class="num" :class="$fmt.dir(summaries.swing.trade.mean)">{{ $fmt.ratio(summaries.swing.trade.mean, 2, true) }}</b>/笔<template v-if="summaries.swing.trade.hold && summaries.swing.trade.hold.mean != null">、留出期 <b class="num" :class="$fmt.dir(summaries.swing.trade.hold.mean)">{{ $fmt.ratio(summaries.swing.trade.hold.mean, 2, true) }}</b></template>，偏乐观<qw-help :text="HELP.mean + ' 这里按比例收费、不受资金多少限制，比按你的本金回测偏乐观。'"/></p>
              </template>
              <div v-else-if="swingMain && !(swingRecent && swingRecent.loading)" class="ms-nums">
                <div class="trust-num"><div class="v num" :class="$fmt.dir(swingMain.mean)">{{ $fmt.ratio(swingMain.mean, 2, true) }}</div><div class="k">{{ swingMain.src === 'acct' ? '按你的本金回测 平均每笔' : '模型检验 平均每笔（偏乐观）' }}<qw-help :text="swingMain.src === 'acct' ? HELP.acct : HELP.mean"/></div></div>
                <div class="trust-vs">对比</div>
                <div class="trust-num"><div class="v num" :class="$fmt.dir(swingMain.base && swingMain.base.mean)">{{ swingMain.base ? $fmt.ratio(swingMain.base.mean, 2, true) : '—' }}</div><div class="k">天天随便挑<qw-help :text="HELP.rnd"/></div></div>
                <div class="trust-num"><div class="v num">{{ $fmt.t(swingT.t) }}</div><div class="k">{{ swingT.label }}t 值<qw-help :text="swingTHelp + ' ' + HELP.period"/></div></div>
              </div>
              <p v-else-if="!swingMain" class="muted">这个模型的报告里还没有逐笔交易成绩（可能是旧版本训练的），重新训练一次就有了。</p>
              <p v-if="btBusy.swing && !summaries.swing.acct" class="ms-bt"><qw-icon name="refresh" :size="15" class="spin"/><span>正在按你的本金回测（每笔佣金最低 5 元、一手 100 股），结果通常比模型检验低，稍等几秒…</span></p>
              <p v-if="swingSay" class="ms-say" :class="{bad: swingSay.bad}">{{ swingSay.text }}</p>
              <p v-if="swingSkip" class="ms-bt bad"><qw-icon name="wallet" :size="15"/><span>按你的本金回测时，钱不够或一手太贵<b>跳过了 {{ $fmt.ratio(swingSkip.share, 0) }} 的信号</b>，账户成绩只代表其中一部分<template v-if="swingSkip.unc">；每个信号都买（不受资金限制）平均每笔 <b class="num" :class="$fmt.dir(swingSkip.unc.mean)">{{ $fmt.ratio(swingSkip.unc.mean, 2, true) }}</b><template v-if="swingSkip.unc.n">（{{ $fmt.int(swingSkip.unc.n) }} 笔）</template></template>。<qw-help :text="UNCAPPED_HELP"/></span></p>
              <div class="ms-role sim">
                <qw-icon name="clipboard" :size="16"/>
                <div><b>{{ ROLE.swing.title }}</b><p>{{ ROLE.swing.text }} <a :href="ROLE.swing.link">{{ ROLE.swing.linkText }} →</a></p></div>
              </div>
            </div>
            <div class="ms-swing-r">
              <template v-if="swingPeriods.length">
                <h4 class="sub-h" style="margin-top:0">选择期 vs 留出期<qw-help :text="HELP.period"/></h4>
                <div v-for="(g, gi) in swingPeriods" :key="g.group" :class="{'section-gap': gi > 0}">
                  <div class="ms-pgroup">{{ g.group }}<qw-help :text="g.help"/></div>
                  <qw-table :columns="periodCols" :rows="g.rows" row-key="k" dense>
                    <template #cell-label="{row}"><b>{{ row.label }}</b><small class="muted cell-date">{{ row.span }}</small></template>
                    <template #cell-n="{row}"><span class="num">{{ $fmt.int(row.n) }}</span></template>
                    <template #cell-mean="{row}"><qw-price :value="row.mean" ratio :digits="2"/></template>
                    <template #cell-bmean="{row}"><span class="num" :class="$fmt.dir(row.bmean)">{{ $fmt.ratio(row.bmean, 2, true) }}</span></template>
                    <template #cell-mmean="{row}"><span class="num" :class="$fmt.dir(row.mmean)">{{ row.mmean == null ? '—' : $fmt.ratio(row.mmean, 2, true) }}</span></template>
                    <template #cell-win="{row}"><span class="num">{{ $fmt.ratio(row.win, 0) }}</span></template>
                    <template #cell-t="{row}"><span class="num" v-tip="tWord(row.t)">{{ $fmt.t(row.t) }}</span></template>
                  </qw-table>
                </div>
              </template>
              <div class="kv ms-kv">
                <div><div class="k">训练时间</div><div class="v">{{ $fmt.datetime(reports.swing.trained_at) }}</div></div>
                <div><div class="k">样本外区间<qw-help :text="HELP.oos"/></div><div class="v"><template v-if="reports.swing.oos_start"><span class="nowrap">{{ $fmt.date(reports.swing.oos_start) }}</span> ~ <span class="nowrap">{{ $fmt.date(reports.swing.oos_end) }}</span></template><template v-else>—</template></div></div>
                <div><div class="k">训练样本<qw-help :text="HELP.samples"/></div><div class="v num">{{ $fmt.int(summaries.swing.n) }}</div></div>
              </div>
            </div>
          </div>
          <template v-else>
            <qw-empty compact icon="cpu" :title="errors.swing ? '模型信息读取失败' : '这个模型还没有训练'" :desc="errors.swing || '训练就是让模型用历史数据学习一遍，需要先下载好行情数据。全市场训练大约需要 3~8 分钟（训练时电脑会比较忙）。'"/>
            <div class="ms-role sim"><qw-icon name="clipboard" :size="16"/><div><b>{{ ROLE.swing.title }}</b><p>{{ ROLE.swing.text }}</p></div></div>
          </template>
          <div v-if="jobFor('swing')" class="section-gap"><qw-job :job-id="jobFor('swing')" :title="'训练模型（' + swingKind.label + '）'" @done="onDone('swing')"/></div>
        </qw-card>

        <qw-card v-for="k in others" :key="k.value" :icon="k.icon">
          <template #title><span>{{ k.label }}</span>
            <span class="qw-tag">{{ k.badge }}</span>
            <span v-if="loading" class="qw-tag">读取中</span>
            <span v-else-if="!reports[k.value]" class="qw-tag warn">还没训练</span>
            <span v-else-if="summaries[k.value].stale" class="qw-tag warn">{{ summaries[k.value].age }} 天前训练 · 建议重训</span>
            <span v-else class="qw-tag green"><qw-icon name="check" :size="12"/>&nbsp;已训练</span>
          </template>
          <template #extra><button class="btn sm" :class="{primary: !reports[k.value] && !loading}" :disabled="busy(k.value)" @click="train(k.value)"><qw-icon name="refresh" :size="13" :class="{spin: busy(k.value)}"/>{{ reports[k.value] ? '重新训练' : '开始训练' }}</button></template>
          <p class="muted" style="font-size:13px;margin-bottom:12px">{{ k.desc }}</p>
          <qw-skeleton v-if="loading" :rows="4"/>
          <template v-else-if="reports[k.value]">
            <div class="ms-nums">
              <div class="trust-num"><div class="v num up">{{ $fmt.ratio(summaries[k.value].top5, 1) }}</div><div class="k">前5名次日涨停率<qw-help :text="HELP.top5"/></div></div>
              <div class="trust-vs">对比</div>
              <div class="trust-num"><div class="v num">{{ $fmt.ratio(summaries[k.value].base, 1) }}</div><div class="k">全部候选平均<qw-help :text="HELP.base"/></div></div>
              <div class="trust-num ms-auc"><div class="v num">{{ $fmt.num(summaries[k.value].auc, 3) }}</div><div class="k">AUC<qw-help :text="HELP.auc"/></div></div>
            </div>
            <p class="ms-say">
              <template v-if="summaries[k.value].mult > 1.05">每天挑模型最看好的 5 只，第二天真的涨停的比例约为随便挑的 <b>{{ $fmt.num(summaries[k.value].mult, 1) }} 倍</b>。</template>
              <template v-else>模型在样本外<b>没有明显好过随便挑</b>，它的名单请只当参考。</template>
            </p>
            <p v-if="summaries[k.value].trade" class="ms-bt" :class="summaries[k.value].trade.mean < 0 ? 'bad' : ''">
              <qw-icon :name="summaries[k.value].trade.mean < 0 ? 'alert' : 'wallet'" :size="15"/>
              <span>但{{ summaries[k.value].trade.fromBt ? '按你现在的买卖规则' : '按真实规则' }}模拟交易：平均每笔 <b class="num" :class="$fmt.dir(summaries[k.value].trade.mean)">{{ $fmt.ratio(summaries[k.value].trade.mean, 2, true) }}</b>（已扣费）<template v-if="summaries[k.value].trade.base && summaries[k.value].trade.base.mean != null">，同池随机挑 {{ $fmt.ratio(summaries[k.value].trade.base.mean, 2, true) }}</template><template v-if="summaries[k.value].trade.fill != null">，{{ summaries[k.value].trade.fill > 0.99 ? '信号基本都能买进' : $fmt.ratio(1 - summaries[k.value].trade.fill, 0) + ' 的信号第二天买不进' }}</template><template v-if="summaries[k.value].trade.skipShare > SKIP_HEAVY">；另外钱不够或一手太贵跳过了 {{ $fmt.ratio(summaries[k.value].trade.skipShare, 0) }}<template v-if="summaries[k.value].trade.uncapped">，每个信号都买（不受资金限制）平均每笔 <b class="num" :class="$fmt.dir(summaries[k.value].trade.uncapped.mean)">{{ $fmt.ratio(summaries[k.value].trade.uncapped.mean, 2, true) }}</b></template><qw-help :text="UNCAPPED_HELP"/></template>。<template v-if="summaries[k.value].trade.mean < 0"><b>命中率高 ≠ 能赚钱。</b></template>
                <a :href="'#/settings?kind=' + k.value + '&run=1'">看详细检验 →</a></span>
            </p>
            <div class="ms-role watch"><qw-icon name="info" :size="16"/><div><b>{{ ROLE[k.value].title }}</b><p>{{ ROLE[k.value].text }}</p></div></div>
            <div class="kv ms-kv">
              <div><div class="k">训练时间</div><div class="v">{{ $fmt.datetime(reports[k.value].trained_at) }}</div></div>
              <div><div class="k">数据范围</div><div class="v"><span class="nowrap">{{ $fmt.date(reports[k.value].data_start) }}</span> ~ <span class="nowrap">{{ $fmt.date(reports[k.value].data_end) }}</span></div></div>
              <div><div class="k">样本外区间<qw-help :text="HELP.oos"/></div><div class="v"><template v-if="reports[k.value].oos_start"><span class="nowrap">{{ $fmt.date(reports[k.value].oos_start) }}</span> ~ <span class="nowrap">{{ $fmt.date(reports[k.value].oos_end) }}</span></template><template v-else>—</template></div></div>
              <div><div class="k">训练样本<qw-help :text="HELP.samples"/></div><div class="v num">{{ $fmt.int(summaries[k.value].n) }}</div></div>
              <div><div class="k">其中第二天涨停</div><div class="v num">{{ $fmt.int(summaries[k.value].pos) }}</div></div>
              <div><div class="k">基准率<qw-help :text="HELP.base"/></div><div class="v num">{{ $fmt.ratio($fmt.frac(reports[k.value].base_rate, 1.5), 1) }}</div></div>
            </div>
          </template>
          <template v-else>
            <qw-empty compact icon="cpu" :title="errors[k.value] ? '模型信息读取失败' : '这个模型还没有训练'" :desc="errors[k.value] || '训练就是让模型用历史数据学习一遍，需要先下载好行情数据。全市场训练大约需要 ' + (k.value === 'streak' ? '1~3' : '3~8') + ' 分钟。'"/>
          </template>
          <div v-if="jobFor(k.value)" class="section-gap"><qw-job :job-id="jobFor(k.value)" :title="'训练模型（' + k.label + '）'" @done="onDone(k.value)"/></div>
        </qw-card>
      </div>

      <template v-if="!loading && anyReport">
        <qw-tabs v-model="detailKind" :items="kindTabs"/>
        <qw-card v-if="!cur"><qw-empty icon="cpu" title="这个模型还没有训练" desc="训练完成后，这里会显示它每一期的成绩、最依赖哪些信息。" action-text="开始训练" @action="train(detailKind)"/></qw-card>
        <template v-else>
          <template v-if="curSwing">
            <div class="grid model-row2">
              <qw-card title="每年的逐笔成绩" sub="模型检验（不受资金限制，偏乐观）" icon="history" :help="HELP.years" :pad="false">
                <qw-table v-if="years.length" :columns="[{key:'year',label:'年份',minWidth:'92px'},{key:'mean',label:'平均每笔',align:'right'},{key:'bar',label:'',minWidth:'100px'},{key:'n',label:'笔数',align:'right'},{key:'win',label:'胜率',align:'right'},{key:'t',label:'t 值',align:'right'}]" :rows="years" row-key="year" dense>
                  <template #cell-year="{row}"><span class="nowrap">{{ row.year }}<span v-if="row.n != null && row.n < 30" class="qw-tag warn yr-few" v-tip="'这一年只有 ' + row.n + ' 笔，样本太少，平均数很容易被一两笔大赚大亏带偏。'">样本太少</span></span></template>
                  <template #cell-mean="{row}"><span v-if="row.n != null && row.n < 30" class="num muted">{{ $fmt.ratio(row.mean, 2, true) }}</span><qw-price v-else :value="row.mean" ratio :digits="2"/></template>
                  <template #cell-bar="{row}"><span class="ybar" :class="$fmt.dir(row.mean)" :style="row.n != null && row.n < 30 ? 'opacity:.4' : ''"><i :style="{width: Math.min(100, Math.abs(row.mean || 0) / yearMax * 100) + '%'}"></i></span></template>
                  <template #cell-n="{row}"><span class="num">{{ $fmt.int(row.n) }}</span></template>
                  <template #cell-win="{row}"><span class="num">{{ $fmt.ratio(row.win, 0) }}</span></template>
                  <template #cell-t="{row}"><span class="num">{{ $fmt.t(row.t) }}</span></template>
                </qw-table>
                <div v-else style="padding:0 16px 16px"><p class="muted">报告里没有分年数据。</p></div>
              </qw-card>
              <qw-card title="滚动检验：每一期" icon="history" :help="HELP.folds" :pad="false" :sub="swingFoldRows.length ? '共 ' + swingFoldRows.length + ' 期' : ''">
                <qw-table v-if="swingFoldRows.length" :columns="swingFoldCols" :rows="swingFoldRows" row-key="fold" dense>
                  <template #cell-range="{row}"><span class="num nowrap">{{ row.test_start }} ~ {{ row.test_end }}</span></template>
                  <template #cell-train_end="{row}"><span class="num muted nowrap">{{ row.train_end }}</span></template>
                  <template #cell-n="{row}"><span class="num">{{ $fmt.int(row.n) }}</span></template>
                  <template #cell-_ic="{row}"><span class="num" :class="row._ic > 0 ? 'up' : row._ic < 0 ? 'down' : ''">{{ $fmt.num(row._ic, 3) }}</span></template>
                  <template #cell-_mean="{row}"><qw-price :value="row._mean" ratio :digits="2"/></template>
                  <template #cell-_base="{row}"><span class="num">{{ $fmt.ratio(row._base, 2, true) }}</span></template>
                  <template #cell-_win="{row}"><span class="num">{{ $fmt.ratio(row._win, 0) }}</span></template>
                </qw-table>
                <div v-else style="padding:0 16px 16px"><p class="muted">没有滚动检验记录。</p></div>
              </qw-card>
            </div>
          </template>
          <qw-card v-else title="滚动检验：每一期的成绩" icon="history" :help="HELP.folds" :sub="foldRows.length ? '共 ' + foldRows.length + ' 期，其中 ' + beatCount + ' 期好过随便挑' : ''">
            <template v-if="foldRows.length">
              <qw-chart v-if="foldOpt" :option="foldOpt" height="240px"/>
              <div class="section-gap">
                <qw-table :columns="foldCols" :rows="foldRows" row-key="fold" dense>
                  <template #cell-range="{row}"><span class="num nowrap">{{ row.test_start }} ~ {{ row.test_end }}</span></template>
                  <template #cell-train_end="{row}"><span class="num muted nowrap">{{ row.train_end }}</span></template>
                  <template #cell-n="{row}"><span class="num">{{ $fmt.int(row.n) }}</span></template>
                  <template #cell-auc="{row}"><span class="num">{{ $fmt.num(row.auc, 3) }}</span></template>
                  <template #cell-_t="{row}"><b class="num">{{ $fmt.ratio(row._t, 1) }}</b></template>
                  <template #cell-_b="{row}"><span class="num">{{ $fmt.ratio(row._b, 1) }}</span></template>
                  <template #cell-_m="{row}"><span class="num" :class="row._m > 1.05 ? 'up' : row._m < 0.95 ? 'down' : ''">{{ row._m ? $fmt.num(row._m, 2) + ' 倍' : '—' }}</span></template>
                </qw-table>
              </div>
              <p v-if="skipped" class="fld-hint" style="margin-top:8px">另有 {{ skipped }} 期因为训练样本太少跳过。</p>
            </template>
            <qw-empty v-else compact icon="history" title="没有滚动检验记录" desc="数据时间太短时无法做滚动检验，需要 2022 年以前的数据。"/>
          </qw-card>

          <div class="grid model-row2">
            <qw-card title="模型最看重哪一面" icon="bars" :help="HELP.imp">
              <qw-chart v-if="impOpt" :option="impOpt" height="190px"/>
              <p v-else class="muted">没有重要性数据。</p>
              <template v-if="topFeatures.length">
                <h4 class="sub-h">最重要的 {{ topFeatures.length }} 个指标</h4>
                <ul class="feat-list">
                  <li v-for="f in topFeatures" :key="f.feature">
                    <span class="fl-name ellipsis" v-tip="f.label + '（' + f.feature + '）'">{{ f.label }}</span>
                    <span class="qw-tag">{{ LABELS[f.group] || f.group || '—' }}</span>
                    <span class="fl-bar"><i :style="{width: (f.share / topMax * 100) + '%'}"></i></span>
                    <span class="fl-v num">{{ $fmt.ratio(f.share, 1) }}</span>
                  </li>
                </ul>
              </template>
            </qw-card>
            <div v-if="!curSwing" class="stack">
              <qw-card title="概率准不准" icon="target" :help="HELP.calib">
                <qw-chart v-if="calOpt" :option="calOpt" height="240px"/>
                <p v-else class="muted">没有足够的样本。</p>
              </qw-card>
              <qw-card title="前 N 名命中率" icon="sparkles" :help="HELP.topn" :pad="false">
                <qw-table :columns="[{key:'n',label:'每天看前几名'},{key:'h',label:'次日涨停率',align:'right'},{key:'mult',label:'是基准的',align:'right'}]" :rows="topn" row-key="n" dense>
                  <template #cell-n="{row}">前 {{ row.n }} 名</template>
                  <template #cell-h="{row}"><b class="num">{{ $fmt.ratio(row.h, 1) }}</b></template>
                  <template #cell-mult="{row}"><span class="num" :class="row.mult > 1.05 ? 'up' : ''">{{ row.mult ? $fmt.num(row.mult, 2) + ' 倍' : '—' }}</span></template>
                </qw-table>
              </qw-card>
            </div>
            <qw-card v-else title="怎么读这个模型" icon="info">
              <ul class="limit-list one">
                <li><span class="li-ico"><qw-icon name="wallet" :size="16"/></span><div><b>它估算的是“能赚多少”，不是涨停概率</b><p>训练目标就是按真实规则（明天开盘买、断板收盘卖、最多5天、扣费用）实际拿到的收益，所以没有“概率准不准”这一项。</p></div></li>
                <li><span class="li-ico"><qw-icon name="target" :size="16"/></span><div><b>看“比随机挑多赚多少”</b><p>同一批候选股随便挑也可能赚钱（行情好的时候）。“天天随便挑”每天都挑满，包括模型不操作的日子；“同日同数量”只在模型出手的日子挑同样多只。模型比后者多赚的，才是挑股票的本事；两种随机之间的差，是挑日子（择时）的作用。</p></div></li>
                <li><span class="li-ico"><qw-icon name="bars" :size="16"/></span><div><b>先看最近一段（留出期）和 t 值</b><p>留出期的数据没有参与训练；但方案是在研究中比较多种做法后选出的，结果可能偏乐观。留出期要明显好过“同一天随便挑”，t 值 ≥2 才算比较可信。两样都过关之前，只在模拟盘里跟踪。</p></div></li>
              </ul>
            </qw-card>
          </div>
        </template>
      </template>

      <qw-card title="需要知道的局限" icon="info">
        <ul class="limit-list">
          <li v-for="l in LIMITS" :key="l.title"><span class="li-ico"><qw-icon :name="l.icon" :size="16"/></span><div><b>{{ l.title }}</b><p>{{ l.text }}</p></div></li>
          <li v-for="n in extraNotes" :key="n"><span class="li-ico"><qw-icon name="info" :size="16"/></span><div><p>{{ n }}</p></div></li>
        </ul>
      </qw-card>
    </div>`,
  });
})();
