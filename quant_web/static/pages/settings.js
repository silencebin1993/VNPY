/* 预测设置 #/settings：名单类型（切换时载入该类型的推荐设置）、风格预设、选股范围、五维权重、门槛、买卖规则、资金与费用；保存；
   用样本外历史检验这套设置（和同池随机挑选对比、t 值、选择期/留出期分开） */
(function () {
  "use strict";
  const { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;
  const { api, fmt, store, toast, isNum, colors, tooltipBase, axisBase, LABELS, DIM_HELP, BOARDS, setTitle, normTrade, tWord, T_HELP, tHelp, BASE_HELP, MATCH_HELP, TUNED_HELP, UNCAPPED_HELP, fillPct, holdLabel, recentVerdict, HOLD_CAVEAT } = QW;

  const GROUPS = ["sentiment", "capital", "fundamental", "theme", "technical"];
  const DEFAULTS = {
    predict: {
      kind: "swing", weights: { sentiment: 1, capital: 1, fundamental: 1, theme: 1, technical: 1 }, news_weight: 0,
      exclude_st: true, boards: ["main", "chinext", "star"], min_float_cap: 0, max_float_cap: 500, min_price: 2, max_price: 200,
      streak_min: 1, streak_max: 10, exclude_one_word: false, threshold: 0, top_n: 5,
    },
    trade: {
      capital: 100000, position_pct: 0.2, max_positions: 5, max_gap_pct: 7, exit_rule: "next_close", stop_loss_pct: 0,
      fee_rate: 0.00025, stamp_duty: 0.0005, slippage: 0.001,
    },
  };
  const KINDS = QW.KINDS.map((k) => ({ value: k.value, label: k.label, icon: k.icon, badge: k.badge, tone: k.tone, desc: k.desc }));
  const KIND_VALUES = KINDS.map((k) => k.value);
  // 随名单切换的字段（后端 settings.KIND_FIELDS）：门槛含义不同（波段=预期收益，连板/首板=概率）时换成该名单的推荐值，其余沿用保存的
  const KIND_FIELDS = { predict: ["threshold", "top_n", "boards", "max_float_cap", "max_price"], trade: ["position_pct", "max_positions", "exit_rule", "max_gap_pct"] };
  const family = (k) => (k === "swing" ? "swing" : "prob");
  const PRESET_ICON = { 稳健: "shield", 均衡: "target", 激进: "flame" };
  const EXIT_RULES = [
    { value: "next_open", title: "第二天开盘就卖", short: "次日开盘卖", desc: "买进后的下一个交易日一开盘就卖出。持有时间最短，不恋战，但也吃不到当天的涨幅。" },
    { value: "next_close", title: "第二天收盘卖", short: "次日收盘卖", desc: "买进后的下一个交易日收盘前卖出，多给一天冲高的机会。" },
    { value: "until_break", title: "拿到不涨停为止", short: "断板才卖", desc: "每天收盘还是涨停就继续拿；哪天没封住涨停，下一个交易日开盘卖出（最多拿10天）。赚得多，回撤也大。" },
    { value: "until_break_close", title: "不涨停就当天收盘卖（最多5天）", short: "断板收盘卖", desc: "从第2天起哪天收盘没涨停就当天收盘卖，最多5天（决定卖出那天跌停卖不出，就顺延到下一个交易日收盘）。" },
  ];
  const HELP = {
    st: "ST 股是连续亏损或有其他问题、被交易所“特别处理”的股票，风险很大，涨跌幅限制也不同。",
    boards: "主板：沪深两市的传统板块，涨跌幅±10%；创业板、科创板：±20%；北交所：±30%，流动性较差。",
    fcap: "流通市值：能在市场上自由买卖的股票总价值。盘子越小越容易被资金拉动，但也越容易暴跌。",
    price: "只看股价在这个范围内的股票。股价太低的往往是问题股，太高的一手要花很多钱。",
    streak: "只看连续涨停天数在这个范围内的股票。连板越高，第二天分歧越大。",
    oneword: "一字板：开盘就封死涨停、全天没打开。这种股票第二天往往也一开盘就涨停，普通人买不进。",
    weights: "模型从五个方面判断明天涨停的可能。拉动滑块可以改变你对每一面的重视程度。",
    news: "消息热度≠利好：新闻多的股票可能是利好，也可能是利空（跌停的股票新闻也很多）。它只有最近24小时的实时数据、没经过历史检验，默认 0 不参与排序，在“历史检验”里也不起作用。",
    topn: "每天按综合概率从高到低，重点关注前几名。数量越少越精挑细选，但机会也越少。",
    threshold: "明天涨停概率低于这个数的股票，就算排进前几名也不选。设为 0 表示不设门槛。",
    thresholdSwing: "模型估算的预期收益（已扣费用）低于这个数的股票不选；一只都没达到时，当天就“不操作”。默认 1%。",
    kind: "三个名单各有一套推荐的选股和买卖规则。切换名单时，下面显示的是这个名单现在实际在用的设置（和“短线预测”页一致）；改了之后点“保存设置”才生效。",
    viewing: "设置只保存一份。看别的名单时，门槛、每天几只、板块、流通市值和股价上限、单只仓位、最多持有、卖出规则、开盘涨太多不买用这个名单的推荐值（连板晋级和首板潜力之间直接沿用），" +
      "其余（权重、价格下限、资金、费用、止损等）沿用你保存的——这就是这个名单现在实际在用的设置。改了以后点“保存设置”，保存的就换成这个名单的设置。",
    fillRate: "能按规则买进的信号占全部信号的比例。买不进 = 开盘就涨停（多是一字板）+ 高开太多放弃 + 买入日停牌；钱不够、一手太贵没买的不算在这里。",
    base: BASE_HELP,
    match: MATCH_HELP,
    tuned: TUNED_HELP,
    period: "选择期（2025-07-01 之前）：研究时比较多种做法、挑出推荐设置的那段历史；" + HOLD_CAVEAT + "留出期比选择期更接近现在的真实水平；行业/ST 用当前归属，也会偏乐观一点。" +
      "注意：如果你看着这里的检验结果反复改设置，留出期也被你拿来挑设置了，会更偏乐观。",
    recent: "先看最近一段：" + HOLD_CAVEAT + "左边是这一段里按这套设置买卖的平均每笔（全新账户单独算）；右边是同一段时间里、只在模型出手的那些天，从同一批候选股里随便挑同样多只的平均每笔（同样的账户规则，随机 20 次平均）。" +
      "左边要明显高于右边，才说明模型真的会挑股票。",
    gap: "第二天开盘涨得太多（比如 +8%）再追进去，性价比很低，这时放弃买入。",
    stop: "持有期间价格跌到“买入价 ×（1 - 止损比例）”以下就卖出，控制单笔最大亏损。填 0 表示不止损。",
    capital: "用来模拟交易的初始资金，只影响历史检验的金额，不会真的下单。",
    pos: "每买一只股票，花掉当时总资产的百分之多少。比例越小越分散，单只踩雷的伤害越小。",
    maxpos: "同一时间最多持有几只股票，满了就不再买新的。",
    fee: "券商佣金，买卖都收（最低5元）。常见是万分之1到万分之3，0.025% 即万分之2.5。",
    stamp: "印花税：只在卖出时收，目前是成交金额的 0.05%（万分之5）。",
    slip: "滑点：实际成交价比想要的价格差一点点（买得贵一点、卖得便宜一点），用来让检验更接近真实。",
    oos: "“样本外”是指模型训练时没见过的那段历史，只用这段历史来检验，才接近模型在新数据上的表现。尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观。",
    trades: "按规则模拟买卖的次数。开盘一字涨停或开盘涨幅太高而没买的，不算在内。",
    win: "赚钱的交易占全部交易的比例（已扣除手续费等费用）。",
    avg: "平均每笔交易赚或亏的百分比（已扣费用）。长期看这个数必须是正的才有意义。",
    cagr: "年化收益：把整段时间的总收益折算成“平均每年赚多少”。",
    dd: "最大回撤：账户资产从某个最高点，最多往下跌了多少。它代表你中途可能要忍受的最大亏损。",
    hit: "命中率：被选中的股票第二天真的涨停的比例；基准率：同期全部候选股第二天涨停的平均比例。命中率明显高于基准率，才说明模型挑得比随便挑强。",
    pf: "盈亏比：所有赚钱交易的总盈利 ÷ 所有亏钱交易的总亏损。大于1说明赚的比亏的多。",
    sharpe: "夏普比率：每承担一份波动换来多少收益，越高越好，大于1算不错。",
    calib: "把模型给出的概率分成几档，看每一档“说的概率”和“实际涨停比例”是否接近。两根柱子越接近，说明概率越靠谱。",
    topn_t: "每天只看模型排名前 N 的股票，第二天真的涨停的比例。",
  };

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const stable = (o) => {
    if (Array.isArray(o)) return "[" + o.map(stable).join(",") + "]";
    if (o && typeof o === "object") return "{" + Object.keys(o).sort().map((k) => JSON.stringify(k) + ":" + stable(o[k])).join(",") + "}";
    return JSON.stringify(o);
  };
  const pick = (s) => ({ predict: s.predict, trade: s.trade });
  const shortMoney = (v) => {
    if (!isNum(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (a >= 1e4) return (v / 1e4).toFixed(a >= 1e6 ? 0 : 1).replace(/\.0$/, "") + "万";
    return String(Math.round(v));
  };
  const weightWord = (v) => (!isNum(v) ? "" : v <= 0.001 ? "不看" : v < 0.95 ? "少看" : v <= 1.05 ? "按模型" : v < 1.95 ? "多看" : "加倍");

  // 草稿与上次检验结果：离开页面再回来时保留
  let draft = null;
  let lastBt = null;

  // ------------------------------------------------------------------ 小表单组件
  const NumField = {
    name: "NumField",
    props: {
      modelValue: Number, scale: { type: Number, default: 1 }, min: Number, max: Number, step: { type: Number, default: 1 },
      unit: String, digits: { type: Number, default: 2 }, invalid: Boolean, width: String, label: String,
    },
    emits: ["update:modelValue"],
    data() { return { text: "", focused: false }; },
    watch: { modelValue: { immediate: true, handler(v) { if (!this.focused) this.text = this.show(v); } } },
    methods: {
      show(v) { return isNum(v) ? String(+(v * this.scale).toFixed(this.digits)) : ""; },
      emitDisplay(n) { this.$emit("update:modelValue", +(n / this.scale).toFixed(10)); },
      onInput(e) {
        this.text = e.target.value;
        const n = parseFloat(this.text);
        if (isFinite(n)) this.emitDisplay(n);
      },
      onBlur() {
        this.focused = false;
        let n = parseFloat(this.text);
        if (!isFinite(n)) n = isNum(this.modelValue) ? this.modelValue * this.scale : isNum(this.min) ? this.min : 0;
        if (isNum(this.min)) n = Math.max(this.min, n);
        if (isNum(this.max)) n = Math.min(this.max, n);
        this.emitDisplay(+n.toFixed(this.digits));
        this.text = String(+n.toFixed(this.digits));
      },
      bump(d) {
        let n = (isNum(this.modelValue) ? this.modelValue * this.scale : 0) + d * this.step;
        if (isNum(this.min)) n = Math.max(this.min, n);
        if (isNum(this.max)) n = Math.min(this.max, n);
        n = +n.toFixed(this.digits);
        this.emitDisplay(n);
        this.text = String(n);
      },
    },
    template: `<span class="num-in" :class="{invalid}" :style="width ? {width} : null">
      <button type="button" class="num-step" tabindex="-1" @click="bump(-1)" :aria-label="'减少' + (label || '')">−</button>
      <input type="text" inputmode="decimal" :value="text" :aria-label="label" @focus="focused = true" @input="onInput" @blur="onBlur"
        @keydown.up.prevent="bump(1)" @keydown.down.prevent="bump(-1)" @keydown.enter="$event.target.blur()">
      <em v-if="unit">{{ unit }}</em>
      <button type="button" class="num-step" tabindex="-1" @click="bump(1)" :aria-label="'增加' + (label || '')">+</button>
    </span>`,
  };
  const Switch = {
    name: "QwSwitchLocal",
    props: { modelValue: Boolean, disabled: Boolean, label: String },
    emits: ["update:modelValue"],
    template: `<button type="button" role="switch" class="qw-switch" :class="{on: modelValue}" :aria-checked="String(!!modelValue)" :aria-label="label" :disabled="disabled" @click="$emit('update:modelValue', !modelValue)"><i></i></button>`,
  };

  // ------------------------------------------------------------------ 图表
  function equityOption(eq, dd, capital) {
    const c = colors();
    const ax = axisBase(c);
    const dates = eq.map((p) => p.date);
    const vals = eq.map((p) => p.value);
    let ddv;
    if (dd && dd.length === eq.length) ddv = dd.map((p) => (isNum(p.value) ? +(fmt.frac(p.value, 1.5) * 100).toFixed(2) : null));
    else {
      let peak = -Infinity;
      ddv = vals.map((v) => { peak = Math.max(peak, v); return peak > 0 ? +((v / peak - 1) * 100).toFixed(2) : 0; });
    }
    const titleStyle = { fontSize: 12, color: c.text2, fontWeight: 500, fontFamily: c.font };
    return {
      animation: false, textStyle: { fontFamily: c.font },
      title: [{ text: "账户资产（元）", left: 0, top: 0, textStyle: titleStyle }, { text: "回撤：离最高点跌了多少", left: 0, top: 246, textStyle: titleStyle }],
      tooltip: {
        ...tooltipBase(c), trigger: "axis", confine: true, axisPointer: { type: "line", lineStyle: { color: c.text3 } },
        formatter: (ps) => {
          const i = ps[0] ? ps[0].dataIndex : 0;
          const v = vals[i];
          const r = isNum(capital) && capital ? v / capital - 1 : null;
          const col = r > 0 ? c.up : r < 0 ? c.down : c.text;
          return `<div style="font-weight:700;margin-bottom:4px">${dates[i]}</div>` +
            `<div>账户资产 <b>${fmt.num(v, 0)}</b> 元</div>` +
            (r != null ? `<div>累计收益 <b style="color:${col}">${fmt.ratio(r, 2, true)}</b></div>` : "") +
            `<div>回撤 <b>${isNum(ddv[i]) ? ddv[i].toFixed(2) + "%" : "—"}</b></div>`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left: 62, right: 16, top: 28, height: 204 }, { left: 62, right: 16, top: 272, height: 62 }],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, boundaryGap: false, ...ax, axisLabel: { show: false }, splitLine: { show: false } },
        { type: "category", data: dates, gridIndex: 1, boundaryGap: false, ...ax, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, hideOverlap: true, formatter: (v) => String(v).slice(0, 7) } },
      ],
      yAxis: [
        { type: "value", scale: true, gridIndex: 0, splitNumber: 4, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: shortMoney } },
        { type: "value", gridIndex: 1, max: 0, splitNumber: 2, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => Math.round(v) + "%" } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        {
          type: "slider", xAxisIndex: [0, 1], bottom: 4, height: 18, borderColor: c.border, backgroundColor: "transparent",
          fillerColor: c.dark ? "rgba(91,143,240,0.16)" : "rgba(42,111,219,0.10)",
          dataBackground: { lineStyle: { color: c.axis }, areaStyle: { color: c.surface3 } },
          selectedDataBackground: { lineStyle: { color: c.primary }, areaStyle: { color: c.primaryWeak } },
          handleStyle: { color: c.surface, borderColor: c.text3 }, moveHandleStyle: { color: c.axis }, textStyle: { color: c.text3, fontSize: 10 },
        },
      ],
      series: [
        {
          name: "账户资产", type: "line", data: vals, showSymbol: false, sampling: "lttb", lineStyle: { width: 2, color: c.series[0] },
          itemStyle: { color: c.series[0] }, areaStyle: { color: c.series[0], opacity: 0.06 },
          markLine: isNum(capital) ? {
            symbol: "none", silent: true, lineStyle: { type: "dashed", color: c.text3, width: 1 },
            label: { formatter: "本金 " + shortMoney(capital), position: "insideEndTop", color: c.text3, fontSize: 11 }, data: [{ yAxis: capital }],
          } : undefined,
        },
        {
          name: "回撤", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: ddv, showSymbol: false, sampling: "lttb",
          lineStyle: { width: 1.5, color: c.down }, itemStyle: { color: c.down }, areaStyle: { color: c.down, opacity: 0.16 },
        },
      ],
    };
  }

  function calibOption(rows) {
    const c = colors();
    const ax = axisBase(c);
    const pctv = (v) => (isNum(v) ? +(fmt.frac(v, 1.5) * 100).toFixed(1) : null);
    return {
      animation: false, textStyle: { fontFamily: c.font },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: c.text2, fontSize: 12 } },
      tooltip: {
        ...tooltipBase(c), trigger: "axis", axisPointer: { type: "shadow" }, confine: true,
        formatter: (ps) => {
          const r = rows[ps[0].dataIndex];
          return `<div style="font-weight:700;margin-bottom:4px">模型概率在 ${r.bucket}</div>` +
            `<div>模型平均给出 <b>${fmt.ratio(fmt.frac(r.pred, 1.5), 1)}</b></div>` +
            `<div>实际第二天涨停 <b>${fmt.ratio(fmt.frac(r.actual, 1.5), 1)}</b></div>` +
            `<div style="opacity:.7">共 ${fmt.int(r.n)} 个样本</div>`;
        },
      },
      grid: { left: 44, right: 10, top: 34, bottom: rows.length > 6 ? 44 : 28 },
      xAxis: {
        type: "category", data: rows.map((r) => r.bucket), ...ax, splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, interval: 0, rotate: rows.length > 6 ? 35 : 0, fontSize: rows.length > 6 ? 10 : 11 },
      },
      yAxis: { type: "value", ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v + "%" } },
      series: [
        { name: "模型说的概率", type: "bar", data: rows.map((r) => pctv(r.pred)), barMaxWidth: 16, barGap: "15%", itemStyle: { color: c.series[0], borderRadius: [4, 4, 0, 0] } },
        { name: "实际涨停比例", type: "bar", data: rows.map((r) => pctv(r.actual)), barMaxWidth: 16, itemStyle: { color: c.series[1], borderRadius: [4, 4, 0, 0] } },
      ],
    };
  }

  // ------------------------------------------------------------------ 页面
  QW.page("settings", {
    props: ["params", "query"],
    components: { "num-field": NumField, "sw": Switch },
    setup(props) {
      const loading = ref(true);
      const loadErr = ref("");
      const saved = ref(null);
      const presets = ref({});
      const form = reactive(clone(DEFAULTS));
      const saving = ref(false);
      const feeOpen = ref(false);
      const bt = ref(lastBt ? lastBt.result : null);
      const btKey = ref(lastBt ? lastBt.key : "");
      const btLoading = ref(false);
      const btErr = ref("");
      const btRef = ref(null);
      const resetOpen = ref(false);

      // 把任意来源的设置补齐成和表单一样的形状（缺的字段用 DEFAULTS）
      const normForm = (src) => {
        const s = clone(src || {});
        const predict = { ...clone(DEFAULTS.predict), ...(s.predict || {}) };
        predict.weights = { ...DEFAULTS.predict.weights, ...((s.predict && s.predict.weights) || {}) };
        return { predict, trade: { ...clone(DEFAULTS.trade), ...(s.trade || {}) } };
      };
      const fill = (src) => {
        const n = normForm(src);
        Object.assign(form.predict, n.predict);
        Object.assign(form.trade, n.trade);
      };
      let draftRestored = false;
      const load = async () => {
        loading.value = true;
        try {
          const [s, p] = await Promise.all([
            api.get("/api/settings", null, { silent: true }),
            api.get("/api/settings/presets", null, { silent: true }).catch(() => ({})),
          ]);
          saved.value = s;
          presets.value = p && typeof p === "object" ? p : {};
          fill(s);
          draftRestored = false;
          if (draft && stable(draft) !== stable(pick(s))) {
            fill(draft);
            draftRestored = true;
            toast.info("已恢复你上次还没保存的修改");
          }
          loadErr.value = "";
        } catch (e) {
          loadErr.value = e.detail || e.message;
        } finally {
          loading.value = false;
        }
      };
      // 打开时看哪个名单：地址里的 ?kind= 优先；从侧边栏进来时用“短线预测”页最后看的名单（qw-pred-kind2，默认波段）
      onMounted(() => {
        const qk = props.query && props.query.kind;
        const stored = QW.safeStore.get("qw-pred-kind2");
        const k = KIND_VALUES.includes(qk) ? qk : KIND_VALUES.includes(stored) ? stored : "swing";
        const run = props.query && props.query.run === "1";      // #/settings?kind=first&run=1：打开后直接检验
        load().then(async () => {
          if (loadErr.value) return;
          // 恢复了没保存的修改时，除非地址里指定了名单，就停在修改的那个名单
          if (k !== form.predict.kind && (KIND_VALUES.includes(qk) || !draftRestored)) await switchKind(k, { silent: true });
          else await ensureEffective(form.predict.kind);
          if (run) runBt(false);
        });
      });

      // ------------------------------------------------------------ 名单类型：切换时载入该名单的推荐设置
      const kindBusy = ref(false);
      const kindDefaults = {};
      const defRules = reactive({});   // 名单 → 它的推荐卖出规则（卖出规则卡片上标“推荐”）
      const perKind = {};   // 本页里每个名单还没保存的修改，切回来时恢复
      const effBase = reactive({});   // 名单 → 它现在实际在用的设置（保存的设置按 KIND_FIELDS 换成该名单的推荐值）；“有没有改过”以它为准
      const curKind = computed(() => KINDS.find((x) => x.value === form.predict.kind) || KINDS[0]);
      const savedKind = computed(() => (saved.value && saved.value.predict && saved.value.predict.kind) || "");
      const savedKindLabel = computed(() => (KINDS.find((x) => x.value === savedKind.value) || { label: savedKind.value }).label);
      const isSwing = computed(() => form.predict.kind === "swing");
      // “上次保存的是…”：只有真的保存过设置（设置文件存在，后端 saved=true）才提
      const everSaved = computed(() => !!(saved.value && saved.value.saved === true));
      const fetchDefaults = async (k) => {
        if (kindDefaults[k]) return kindDefaults[k];
        const r = await api.get("/api/settings/defaults", { kind: k }, { silent: true });
        const src = r && typeof r === "object" ? (r.settings && typeof r.settings === "object" ? r.settings : r) : null;
        if (!src || !(src.predict || src.trade)) throw new Error("推荐设置格式不对");
        kindDefaults[k] = clone(src);
        if (src.trade && src.trade.exit_rule) defRules[k] = src.trade.exit_rule;
        if (r.kind_fields && typeof r.kind_fields === "object") apiKindFields = r.kind_fields;   // 后端若给出随名单切换的字段，以它为准
        return kindDefaults[k];
      };
      let apiKindFields = null;
      // 某个名单现在实际在用的设置（与后端 paper.settings_for 相同）：保存的就是这个名单 → 原样；
      // 同类（连板/首板，门槛都是概率）→ 只换名单；不同类 → KIND_FIELDS 换成该名单的推荐值，其余沿用保存的
      const effectiveFor = async (k) => {
        const s = saved.value;
        if (!s || !s.predict) return null;
        const out = normForm(pick(s));
        out.predict.kind = k;
        if (s.predict.kind === k || family(s.predict.kind) === family(k)) return out;
        const d = await fetchDefaults(k);
        const from = await fetchDefaults(s.predict.kind).catch(() => null);
        for (const part of ["predict", "trade"]) {
          // 随名单切换的字段：前端清单 ∪ 后端给的清单 ∪ 两个名单推荐值不一样的字段（后端给某个名单加了专门的默认值时，这里自动跟上）
          const fields = new Set([...KIND_FIELDS[part], ...((apiKindFields && Array.isArray(apiKindFields[part]) && apiKindFields[part]) || [])]);
          if (from && from[part] && d[part]) Object.keys(d[part]).forEach((f) => { if (f !== "kind" && stable(d[part][f]) !== stable(from[part][f])) fields.add(f); });
          for (const f of fields) if (d[part] && d[part][f] !== undefined) out[part][f] = clone(d[part][f]);
        }
        return out;
      };
      const ensureEffective = async (k) => {
        if (!k || effBase[k] || savedKind.value === k) return effBase[k] || null;
        try { const e = await effectiveFor(k); if (e) effBase[k] = e; return e; } catch (e) { return null; }
      };
      const switchKind = async (k, opts = {}) => {
        if (!KIND_VALUES.includes(k) || k === form.predict.kind || kindBusy.value) return;
        const label = (KINDS.find((x) => x.value === k) || {}).label || k;
        const old = form.predict.kind;
        if (dirty.value) perKind[old] = clone({ predict: form.predict, trade: form.trade });
        else delete perKind[old];
        kindBusy.value = true;
        try {
          const eff = savedKind.value === k ? normForm(pick(saved.value)) : await ensureEffective(k);
          if (perKind[k]) { fill(perKind[k]); toast.info(`已切换到「${label}」，恢复了你刚才没保存的修改`); }
          else if (eff) { fill(eff); if (!opts.silent) toast.info(`已切换到「${label}」：下面是这个名单现在实际在用的设置`); }
          else toast.warn(`没能读取「${label}」的推荐设置，先沿用现在的数值`);
        } finally {
          form.predict.kind = k;
          kindBusy.value = false;
        }
      };

      watch(() => form.predict.kind, (k) => { if (k && !defRules[k]) fetchDefaults(k).catch(() => {}); }, { immediate: true });

      const formKey = computed(() => stable({ predict: form.predict, trade: form.trade }));
      // 预测类型只决定检验哪个模型，不算“修改”
      const noKind = (s) => stable({ predict: { ...s.predict, kind: null }, trade: s.trade });
      // “有没有改过”：和这个名单现在实际在用的设置比（保存的就是这个名单时就是保存的设置）
      const baseline = computed(() => {
        const s = saved.value;
        if (!s) return null;
        const k = form.predict.kind;
        return savedKind.value === k ? s : effBase[k] || s;
      });
      const dirty = computed(() => !!baseline.value && noKind(form) !== noKind(normForm(pick(baseline.value))));
      watch(dirty, (d) => setTitle(d ? "预测设置 · 未保存" : ""), { immediate: true });
      const onUnload = (e) => { if (dirty.value) { e.preventDefault(); e.returnValue = ""; } };
      window.addEventListener("beforeunload", onUnload);
      onBeforeUnmount(() => {
        window.removeEventListener("beforeunload", onUnload);
        draft = dirty.value ? clone({ predict: form.predict, trade: form.trade }) : null;
        if (dirty.value) toast.warn("预测设置还没保存，回到这个页面可以继续修改");
      });

      // 校验（服务端还会再校验一次）
      const errs = computed(() => {
        const p = form.predict;
        const t = form.trade;
        const e = {};
        if (!p.boards.length) e.boards = "至少选一个板块";
        if (!(p.max_float_cap > p.min_float_cap)) e.cap = "上限要大于下限";
        if (!(p.max_price > p.min_price)) e.price = "上限要大于下限";
        if (p.kind === "streak" && p.streak_max < p.streak_min) e.streak = "上限不能小于下限";
        if (!(t.capital >= 1000)) e.capital = "至少 1000 元";
        if (!(t.position_pct > 0 && t.position_pct <= 1)) e.pos = "要在 1% 到 100% 之间";
        return e;
      });
      const errCount = computed(() => Object.keys(errs.value).length);

      // 波段：只显示后端给了波段门槛的风格（老版本的风格是按连板设计的，套到波段上会改乱买卖规则）
      const presetNames = computed(() => Object.keys(presets.value || {}).filter((n) => {
        if (form.predict.kind !== "swing") return true;
        const by = (presets.value[n] && presets.value[n].threshold_by_kind) || {};
        const over = presets.value[n] && presets.value[n].kind_overrides;
        return isNum(by.swing) || !!(over && over.swing);
      }));
      const thresholdOf = (p, kind) => {
        const by = (p && p.threshold_by_kind) || {};
        if (isNum(by[kind])) return by[kind];
        if (kind === "swing") return undefined;   // 波段门槛是预期收益，不能套用概率门槛
        return p && p.predict ? p.predict.threshold : undefined;
      };
      // 某个风格在当前名单下的实际取值：通用值 + kind_overrides[名单] + threshold_by_kind[名单]
      const presetFor = (name, kind) => {
        const p = presets.value[name];
        if (!p) return null;
        const over = (p.kind_overrides && p.kind_overrides[kind]) || {};
        const predict = { ...(p.predict || {}), ...(over.predict || {}) };
        delete predict.kind;
        const th = thresholdOf(p, kind);
        if (isNum(th)) predict.threshold = th;
        else delete predict.threshold;
        return { predict, trade: { ...(p.trade || {}), ...(over.trade || {}) } };
      };
      const presetMatch = (name) => {
        const p = presetFor(name, form.predict.kind);
        if (!p) return false;
        for (const k of Object.keys(p.predict)) if (stable(form.predict[k]) !== stable(p.predict[k])) return false;
        for (const k of Object.keys(p.trade)) if (stable(form.trade[k]) !== stable(p.trade[k])) return false;
        return true;
      };
      const activePreset = computed(() => presetNames.value.find(presetMatch) || "");
      const presetChips = (name) => {
        const p = presetFor(name, form.predict.kind);
        if (!p) return [];
        const pp = p.predict;
        const tt = p.trade;
        const out = [];
        if (isNum(pp.top_n)) out.push(`每天前 ${pp.top_n} 名`);
        const th = pp.threshold;
        if (isNum(th)) out.push(th > 0 ? (form.predict.kind === "swing" ? `预期收益 ≥ ${fmt.ratio(th, 1)}` : `概率 ≥ ${fmt.ratio(th, 0)}`) : "不设门槛");
        if (isNum(tt.position_pct)) out.push(`单只 ${fmt.ratio(tt.position_pct, 0)} 仓位`);
        if (isNum(tt.stop_loss_pct)) out.push(tt.stop_loss_pct > 0 ? `跌 ${fmt.ratio(tt.stop_loss_pct, 0)} 止损` : "不止损");
        const rule = EXIT_RULES.find((r) => r.value === tt.exit_rule);
        if (rule) out.push(rule.short);
        return out;
      };
      // 风格说明是按连板写的；波段只在后端给了专门说明时显示
      const presetDesc = (name) => {
        const p = presets.value[name] || {};
        const over = (p.kind_overrides && p.kind_overrides[form.predict.kind]) || {};
        return over.description || (form.predict.kind === "swing" ? "" : p.description || "");
      };
      const applyPreset = (name) => {
        const p = presetFor(name, form.predict.kind);
        if (!p) return;
        const kind = form.predict.kind;
        Object.assign(form.predict, clone(p.predict));
        Object.assign(form.trade, clone(p.trade));
        form.predict.kind = kind;
        toast.info(`已套用“${name}”风格，点“保存设置”后生效`);
      };

      const toggleBoard = (b) => {
        const list = form.predict.boards;
        form.predict.boards = list.includes(b) ? list.filter((x) => x !== b) : Object.keys(BOARDS).filter((x) => x === b || list.includes(x));
      };
      const resetWeights = () => {
        GROUPS.forEach((g) => { form.predict.weights[g] = 1; });
        form.predict.news_weight = DEFAULTS.predict.news_weight;
      };
      const weightsChanged = computed(() => GROUPS.some((g) => Math.abs((form.predict.weights[g] ?? 1) - 1) > 1e-9) || Math.abs(form.predict.news_weight - DEFAULTS.predict.news_weight) > 1e-9);
      const sliderStyle = (v) => ({ "--p": ((isNum(v) ? v : 0) / 2) * 100 + "%" });
      const perStock = computed(() => (isNum(form.trade.capital) && isNum(form.trade.position_pct) ? form.trade.capital * form.trade.position_pct : null));

      const doReset = async () => {
        const kind = form.predict.kind;
        resetOpen.value = false;
        try { fill(await fetchDefaults(kind)); } catch (e) { fill(DEFAULTS); }
        form.predict.kind = kind;
        toast.info("已恢复为默认设置，点“保存设置”后生效");
      };
      const undo = () => { if (baseline.value) fill(baseline.value); draft = null; };
      const save = async () => {
        if (errCount.value) { toast.warn("有几项填得不对，请先改正标红的地方"); return; }
        saving.value = true;
        try {
          const s = await api.put("/api/settings", { predict: clone(form.predict), trade: clone(form.trade) });
          saved.value = s;
          fill(s);
          draft = null;
          Object.keys(perKind).forEach((k) => { delete perKind[k]; });
          Object.keys(effBase).forEach((k) => { delete effBase[k]; });
          toast.success("设置已保存，“短线预测”的名单会按新设置筛选");
        } catch (e) { /* 已提示 */ } finally { saving.value = false; }
      };

      // ------------------------------------------------------------ 历史检验
      const runBt = async (scroll) => {
        if (errCount.value) { toast.warn("有几项填得不对，请先改正标红的地方"); return; }
        btLoading.value = true;
        btErr.value = "";
        if (scroll && btRef.value) btRef.value.scrollIntoView({ behavior: "smooth", block: "start" });
        const key = formKey.value;
        try {
          const r = await api.post("/api/predict/backtest", { predict: clone(form.predict), trade: clone(form.trade) }, { silent: true });
          bt.value = r;
          btKey.value = key;
          lastBt = { result: r, key };
          const used = r && r.settings_used;
          if (used && used.predict && used.trade) noteTried(used.predict.kind || form.predict.kind, used);
        } catch (e) {
          btErr.value = e.detail || e.message;
          bt.value = null;
        } finally {
          btLoading.value = false;
        }
      };
      const btStale = computed(() => !!bt.value && btKey.value !== formKey.value);
      const barOpen = ref(false);          // 手机：底部保存栏的“更多”
      const m = computed(() => (bt.value && bt.value.metrics) || null);
      const mm = computed(() => {
        const x = m.value;
        if (!x) return null;
        const hit = fmt.frac(x.hit_rate, 1.5);
        const base = fmt.frac(x.base_rate, 1.5);
        return {
          win: fmt.frac(x.win_rate, 1.5), avg: fmt.frac(x.avg_return, 0.5), med: fmt.frac(x.median_return, 0.5),
          cagr: fmt.frac(x.cagr, 5), total: fmt.frac(x.total_return, 50), dd: fmt.frac(x.max_drawdown, 1.5),
          hit, base, mult: isNum(hit) && isNum(base) && base > 0 ? hit / base : null,
        };
      });
      const btKindValue = computed(() => (bt.value && bt.value.settings_used && bt.value.settings_used.predict && bt.value.settings_used.predict.kind) || (bt.value && bt.value.kind) || form.predict.kind);
      const btKind = computed(() => (KINDS.find((x) => x.value === btKindValue.value) || { label: btKindValue.value }).label);
      const btSwing = computed(() => btKindValue.value === "swing");
      // 和同池随机挑选对比、t 值、选择期/留出期
      const nt = computed(() => normTrade(bt.value));
      const base = computed(() => (nt.value && nt.value.base) || null);
      const hasPeriods = computed(() => !!(nt.value && (nt.value.sel || nt.value.hold)));
      // 这套设置和该名单的推荐设置不一样（后端 tuned：只算选股/买卖规则，不算本金和费用）→ 历史成绩偏乐观
      const tuned = computed(() => (bt.value && bt.value.tuned && bt.value.tuned.modified ? bt.value.tuned.fields || [] : null));
      // 在这台电脑上用历史数据检验过几套不同的设置（按名单分开数）：试得越多，挑出来的那套越可能只是碰巧好看
      const TRIED_KEY = "qw-bt-tried";
      const readTried = () => { try { return JSON.parse(QW.safeStore.get(TRIED_KEY) || "{}") || {}; } catch (e) { return {}; } };
      const hashKey = (text) => { let h = 5381; for (let i = 0; i < text.length; i++) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0; return h.toString(36); };
      const triedTick = ref(0);
      const noteTried = (kind, s) => {
        const all = readTried();
        const key = hashKey(stable({ predict: { ...s.predict, kind: null, news_weight: null }, trade: s.trade }));
        const list = Array.isArray(all[kind]) ? all[kind] : [];
        if (!list.includes(key)) list.push(key);
        all[kind] = list.slice(-500);
        QW.safeStore.set(TRIED_KEY, JSON.stringify(all));
        triedTick.value++;
      };
      const triedCount = computed(() => {
        triedTick.value;              // 依赖：每次检验后重新读
        const list = readTried()[btKindValue.value];
        return Array.isArray(list) ? list.length : 0;
      });
      const cmpRows = computed(() => {
        const x = nt.value;
        if (!x) return [];
        const cols = [["all", x], ["sel", x.sel], ["hold", x.hold]];
        const cell = (o, f) => (o ? f(o) : { text: "—" });
        const pctCell = (v) => ({ text: fmt.ratio(v, 2, true), cls: fmt.dir(v) });
        const diff = (a, b) => {
          const d = isNum(a) && isNum(b) ? a - b : null;
          return { text: isNum(d) ? (d > 0 ? "+" : "") + (d * 100).toFixed(2) + " 个百分点" : "—", cls: fmt.dir(d) };
        };
        const hasMatch = !!(x.match && !x.match.same);
        const defs = [
          { key: "n", label: "交易笔数", f: (o) => ({ text: fmt.int(o.n) }) },
          { key: "mean", label: "平均每笔（这套设置）", f: (o) => ({ ...pctCell(o.mean), bold: true }) },
          { key: "bmean", label: "平均每笔（天天随便挑）", help: HELP.base, f: (o) => pctCell(o.base ? o.base.mean : null) },
          { key: "edge", label: "比天天随便挑多赚", f: (o) => diff(o.mean, o.base ? o.base.mean : null) },
          hasMatch ? { key: "mmean", label: "平均每笔（同日同数量随便挑）", help: HELP.match, f: (o) => pctCell(o.match ? o.match.mean : null) } : null,
          hasMatch ? { key: "pedge", label: "只比挑股票多赚", help: HELP.match, f: (o) => diff(o.mean, o.match ? o.match.mean : null) } : null,
          { key: "win", label: base.value && isNum(base.value.win) ? "胜率（这套 / 随机）" : "胜率", f: (o) => ({ text: fmt.ratio(o.win, 0) + (o.base && isNum(o.base.win) ? " / " + fmt.ratio(o.base.win, 0) : "") }) },
          { key: "t", label: "t 值", help: tHelp(x), f: (o) => ({ text: isNum(o.t) ? fmt.t(o.t) + "（" + tWord(o.t) + "）" : "—", cls: isNum(o.t) && o.t >= 2 && !tuned.value ? "good" : "" }) },
        ].filter(Boolean);
        return defs.filter((d) => (d.key !== "bmean" && d.key !== "edge") || !!base.value).map((d) => {
          const r = { key: d.key, label: d.label, help: d.help };
          cols.forEach(([k, o]) => { r[k] = cell(o, d.f); });
          return r;
        });
      });
      const cmpCols = computed(() => {
        const x = nt.value || {};
        const span = (o, fb) => (o && o.start && o.end ? `${fmt.date(o.start).slice(0, 7)} ~ ${fmt.date(o.end).slice(0, 7)}` : fb);
        const cols = [{ key: "label", label: "", minWidth: "150px" }, { key: "all", label: "全部样本外", align: "right" }];
        if (hasPeriods.value) {
          cols.push({ key: "sel", label: "选择期 " + span(x.sel, "（2025-07 以前）"), align: "right", help: HELP.period });
          cols.push({ key: "hold", label: "留出期 " + span(x.hold, "（2025-07 以后）"), align: "right", help: HELP.period });
        }
        return cols;
      });
      const btCapital = computed(() => {
        const t = bt.value && bt.value.settings_used && bt.value.settings_used.trade;
        return t && isNum(t.capital) ? t.capital : form.trade.capital;
      });
      const period = computed(() => {
        const x = m.value || {};
        const eq = (bt.value && bt.value.equity) || [];
        const s = x.start || (eq[0] && eq[0].date);
        const e = x.end || (eq.length && eq[eq.length - 1].date);
        return s && e ? `${fmt.date(s)} ~ ${fmt.date(e)}` : "";
      });
      const extras = computed(() => {
        const x = m.value;
        if (!x) return [];
        const out = [
          { k: "中位数收益", v: fmt.ratio(mm.value.med, 2, true), cls: fmt.dir(mm.value.med), help: "把每笔收益从高到低排，正中间那笔的收益。比平均数更能代表“一般情况”。" },
          { k: "盈亏比", v: fmt.num(x.profit_factor, 2), help: HELP.pf },
          { k: "夏普比率", v: fmt.num(x.sharpe, 2), help: HELP.sharpe },
          { k: "平均持有", v: isNum(x.avg_hold_days) ? fmt.num(x.avg_hold_days, 1) + " 天" : "—", help: "从买入到卖出平均隔了几个交易日。" },
          { k: "总收益", v: fmt.ratio(mm.value.total, 1, true), cls: fmt.dir(mm.value.total) },
        ];
        if (isNum(x.final_equity)) out.push({ k: "期末资产", v: fmt.money(x.final_equity) });
        if (isNum(x.total_fees)) out.push({ k: "费用合计", v: fmt.money(x.total_fees), help: "佣金、印花税、滑点加在一起一共花了多少。" });
        if (isNum(x.signals)) out.push({ k: "信号次数", v: fmt.int(x.signals), help: "符合条件、准备第二天买入的次数（包括最后没买成的）。" });
        const n = nt.value;
        if (n && isNum(n.fill)) out.push({ k: "能买进的比例", v: fillPct(n.fill), help: HELP.fillRate + (n.fillSrc === "calc" ? "（这里按“1 − 开盘涨停次数 ÷ 信号次数”估算。）" : "") });
        if (isNum(x.suspended) && x.suspended > 0) out.push({ k: "停牌买不进", v: fmt.int(x.suspended), help: "准备买入的那天股票停牌，没买成的次数。" });
        if (isNum(x.skipped_gap)) out.push({ k: "开盘太高放弃", v: fmt.int(x.skipped_gap), help: HELP.gap });
        if (isNum(x.skipped_full)) out.push({ k: "持仓已满放弃", v: fmt.int(x.skipped_full), help: HELP.maxpos });
        if (isNum(x.skipped_lot) && x.skipped_lot > 0) out.push({ k: "买不起一手放弃", v: fmt.int(x.skipped_lot), help: "按单只仓位算出来的金额不够买 100 股（一手）的次数，多是股价较高的股票。把单只仓位或初始资金调大就能买上。" });
        if (isNum(x.skipped_cash) && x.skipped_cash > 0) out.push({ k: "现金不够放弃", v: fmt.int(x.skipped_cash), help: "账户里剩的现金不够买这只的次数（钱被别的持仓占着）。" });
        if (!btSwing.value && isNum(mm.value.hit)) out.push({ k: "次日涨停命中率", v: fmt.ratio(mm.value.hit, 1), help: HELP.hit });
        if (!btSwing.value && isNum(mm.value.base)) out.push({ k: "全部候选涨停率", v: fmt.ratio(mm.value.base, 1), help: HELP.hit });
        return out;
      });
      const verdict = computed(() => {
        const v = verdictAll.value;
        if (!v || !recentBt.value) return v;
        return { ...v, tone: v.tone === "warn" || recentBt.value.verdict.tone === "warn" ? "warn" : v.tone, text: "全部样本外：" + v.text };
      });
      const verdictAll = computed(() => {
        const x = mm.value;
        if (!x || !m.value.trades) return null;
        const b = base.value;
        const t = nt.value ? nt.value.t : null;
        const bText = b && isNum(b.mean) ? fmt.ratio(b.mean, 2, true) : null;
        if (isNum(x.avg) && x.avg < 0) {
          const worse = bText && b.mean > x.avg ? `，还不如同一批股票里随便挑（${bText}）` : "";
          return { tone: "warn", text: `按这套设置，历史上平均每笔 <b>亏 ${fmt.ratio(-x.avg, 2)}</b>（已扣费用）${worse}。${btSwing.value ? "建议恢复这个名单的推荐设置再试试。" : "就算挑得准，买入价偏高、第二天冲高回落也会吃掉收益，建议只当观察名单。"}` };
        }
        if (bText && isNum(x.avg) && x.avg <= b.mean + 0.0005) {
          return { tone: "warn", text: `平均每笔 ${fmt.ratio(x.avg, 2, true)}，但同一批股票里<b>随便挑也有 ${bText}</b>：模型没多挑出好股票，赚的主要是那段行情的钱。` };
        }
        const ddText = isNum(x.dd) ? `，中途最多回撤过 <b>${fmt.ratio(Math.abs(x.dd), 1)}</b>` : "";
        const hitText = !btSwing.value && isNum(x.mult) ? `选中股票的次日涨停率约为随便挑的 <b>${fmt.num(x.mult, 1)} 倍</b>；` : "";
        const edgeText = bText ? `，比天天随便挑（${bText}）多 <b>${fmt.num((x.avg - b.mean) * 100, 2)}</b> 个百分点` : "";
        const mt = nt.value && nt.value.match && !nt.value.match.same ? nt.value.match.mean : null;
        const pickText = isNum(mt) ? `（只比挑股票，即同一天随便挑同样多只，多 <b>${fmt.num((x.avg - mt) * 100, 2)}</b> 个百分点）` : "";
        let tText = !isNum(t) ? "过去的表现不代表未来，请先用模拟盘跟踪。"
          : t < 2 ? `t 值 ${fmt.t(t)}，不到 2（${tWord(t)}），建议先用模拟盘跟踪几十笔再说。`
            : tuned.value ? `t 值 ${fmt.t(t)}，但这套设置改过，历史成绩偏乐观，t 值也跟着偏高：请先用模拟盘验证。`
              : `t 值 ${fmt.t(t)}，比较可信；但过去不代表未来，请先模拟、再小仓位。`;
        // 波段的方案是在选择期里挑出来的：留出期不到 2 时如实说出来
        const ht = nt.value && nt.value.hold ? nt.value.hold.t : null;
        if (btSwing.value && isNum(t) && t >= 2 && isNum(ht) && ht < 2) {
          tText = `全部 t 值 ${fmt.t(t)}，但最近一段<b>留出期只有 ${fmt.t(ht)}</b>（${tWord(ht)}），先用模拟盘跟踪，不建议直接用真钱。`;
        }
        return { tone: "info", text: `${hitText}按这套设置交易，历史上平均每笔赚 <b>${fmt.ratio(x.avg, 2)}</b>${edgeText}${pickText}，胜率 ${fmt.ratio(x.win, 0)}${ddText}。${tText}` };
      });
      // 最近一段（留出期，by_period.holdout）：这套设置 vs 同一段“同一天随便挑同样多只”（baseline_matched），结论按数据算
      const recentBt = computed(() => {
        const x = nt.value;
        const h = x && x.hold;
        if (!h || !isNum(h.mean) || !(m.value && m.value.trades)) return null;
        const rnd = h.match && isNum(h.match.mean) ? h.match.mean : null;
        return { mean: h.mean, n: h.n, rnd, t: h.t, label: holdLabel(h), verdict: recentVerdict(h.mean, rnd, h.t, !btSwing.value) };
      });
      // 钱不够/一手太贵跳过了很多信号：账户成绩只代表一部分信号，另给“每个信号都买”的逐笔结果（后端 per_trade_uncapped）
      const skipHeavy = computed(() => {
        const n = nt.value;
        if (!n || !isNum(n.skipShare) || n.skipShare <= QW.SKIP_HEAVY) return null;
        const parts = [n.skippedLot > 0 ? `买不起一手 ${fmt.int(n.skippedLot)} 次` : "", n.skippedCash > 0 ? `现金不够 ${fmt.int(n.skippedCash)} 次` : ""].filter(Boolean).join("、");
        return { share: n.skipShare, parts, unc: n.uncapped };
      });
      // 回测的说明（后端 notes）：跳过信号的那条已经在上面的横幅里讲了数字，这里不重复
      const btNotes = computed(() => ((bt.value && Array.isArray(bt.value.notes) && bt.value.notes) || [])
        .filter((t) => typeof t === "string" && t && !(skipHeavy.value && /资金不足|一手太贵/.test(t))));
      const eqOption = computed(() => {
        const eq = (bt.value && bt.value.equity) || [];
        return eq.length ? equityOption(eq, bt.value.drawdown || [], btCapital.value) : null;
      });
      const calRows = computed(() => ((bt.value && bt.value.calibration) || []).filter((r) => r && r.n > 0));
      const calOption = computed(() => (calRows.value.length ? calibOption(calRows.value) : null));
      const yearly = computed(() => ((bt.value && bt.value.yearly) || []).slice().sort((a, b) => b.year - a.year));
      const yearMax = computed(() => Math.max(0.0001, ...yearly.value.map((y) => Math.abs(fmt.frac(y.return, 5) || 0))));
      const topn = computed(() => ((bt.value && bt.value.topn_hit) || []).map((r) => {
        const h = fmt.frac(r.hit_rate, 1.5);
        const base = mm.value ? mm.value.base : null;
        return { ...r, h, mult: isNum(h) && isNum(base) && base > 0 ? h / base : null };
      }));
      const trades = computed(() => ((bt.value && bt.value.trades) || []).map((t, i) => ({ ...t, _k: i })));
      const tradeCols = computed(() => [
        { key: "signal_date", label: "信号日", sortable: true, help: "模型选出这只股票的那天（收盘后）。" },
        { key: "name", label: "股票", minWidth: "96px" },
        { key: "score", label: btSwing.value ? "预期收益" : "涨停概率", align: "right", sortable: true },
        { key: "entry", label: "买入", align: "right", sortBy: (r) => r.entry_date },
        { key: "exit", label: "卖出", align: "right", sortBy: (r) => r.exit_date },
        { key: "ret", label: "收益", align: "right", sortable: true, help: "这笔交易扣除费用后的收益率。" },
        { key: "reason", label: "卖出原因" },
      ]);

      return {
        store, fmt, LABELS, DIM_HELP, BOARDS, GROUPS, KINDS, EXIT_RULES, HELP, PRESET_ICON,
        loading, loadErr, load, saved, presets, presetNames, activePreset, presetChips, presetDesc, applyPreset, form, errs, errCount, dirty,
        saving, save, undo, resetOpen, doReset, feeOpen, toggleBoard, resetWeights, weightsChanged, sliderStyle, weightWord, perStock,
        bt, btLoading, btErr, btRef, runBt, btStale, m, mm, btKind, btSwing, period, extras, verdict, eqOption, calOption, calRows, yearly, yearMax,
        topn, trades, tradeCols, nt, base, hasPeriods, cmpRows, cmpCols, T_HELP, tWord, tuned, triedCount, recentBt, everSaved, defRules,
        kindBusy, switchKind, curKind, isSwing, savedKind, savedKindLabel, barOpen, skipHeavy, btNotes, tHelp, UNCAPPED_HELP,
      };
    },
    template: `<div class="stack set-page">
      <template v-if="loading">
        <qw-skeleton type="tiles" :count="3"/>
        <div class="grid set-grid"><qw-card><qw-skeleton :rows="7"/></qw-card><qw-card><qw-skeleton :rows="7"/></qw-card></div>
      </template>
      <qw-card v-else-if="loadErr">
        <qw-empty icon="alert" title="设置暂时读取不到" :desc="loadErr" action-text="重新加载" @action="load"/>
      </qw-card>
      <template v-else>
        <div class="set-intro">
          <p>这里决定“短线预测”每天的名单怎么<b>挑股票</b>，以及“历史检验”按什么规则<b>模拟买卖</b>。不懂的话，先选名单，用它的推荐设置，再点最下面的“用历史数据检验”看看效果。</p>
        </div>

        <div class="set-kind">
          <div class="sk-top">
            <span class="sk-l">这套设置用于<qw-help :text="HELP.kind"/></span>
            <qw-segmented :model-value="form.predict.kind" :options="KINDS" :size="store.isPhone ? 'sm' : ''" :block="store.isPhone" @update:model-value="switchKind"/>
            <span class="qw-tag" :class="curKind.tone === 'sim' ? 'blue' : ''">{{ curKind.badge }}</span>
            <span v-if="kindBusy" class="muted" style="font-size:12.5px">正在读取推荐设置…</span>
          </div>
          <p class="sk-now"><qw-icon name="info" :size="14"/><span>正在查看：<b>{{ curKind.label }}</b> 的设置（保存后生效）<qw-help :text="HELP.viewing"/></span></p>
          <p class="sk-desc">{{ curKind.desc }}</p>
        </div>

        <div v-if="presetNames.length" class="preset-cards" role="radiogroup" aria-label="风格预设">
          <button v-for="n in presetNames" :key="n" type="button" role="radio" class="preset-card" :class="['p-' + n, {active: activePreset === n}]" :aria-checked="activePreset === n" @click="applyPreset(n)">
            <span class="pc-hd"><span class="pc-ico"><qw-icon :name="PRESET_ICON[n] || 'sliders'" :size="20"/></span><b>{{ n }}</b><span v-if="activePreset === n" class="pc-on"><qw-icon name="check" :size="14"/>当前</span></span>
            <span v-if="presetDesc(n)" class="pc-desc">{{ presetDesc(n) }}</span>
            <span class="pc-chips"><span v-for="c in presetChips(n)" :key="c" class="qw-tag">{{ c }}</span></span>
          </button>
        </div>

        <div class="set-form">
          <div class="grid set-grid">
            <qw-card title="① 选股范围" icon="filter" sub="先把不想要的股票挡在门外">
              <div class="fld"><div class="fld-l">排除 ST 股<qw-help :text="HELP.st"/></div><div class="fld-c"><sw v-model="form.predict.exclude_st" label="排除ST股"/><span class="fld-hint">{{ form.predict.exclude_st ? '已排除，更安全' : '包含 ST 股，风险较大' }}</span></div></div>
              <div class="fld"><div class="fld-l">板块<qw-help :text="HELP.boards"/></div>
                <div class="fld-c"><div class="chips-wrap">
                  <button v-for="(lab, b) in BOARDS" :key="b" type="button" class="chip" :class="{active: form.predict.boards.includes(b)}" :aria-pressed="form.predict.boards.includes(b)" @click="toggleBoard(b)"><qw-icon v-if="form.predict.boards.includes(b)" name="check" :size="13"/>{{ lab }}</button>
                </div><div v-if="errs.boards" class="fld-err">{{ errs.boards }}</div></div></div>
              <div class="fld"><div class="fld-l">流通市值<qw-help :text="HELP.fcap"/></div>
                <div class="fld-c"><div class="range-pair"><num-field v-model="form.predict.min_float_cap" :min="0" :max="100000" :step="10" :digits="0" unit="亿" label="流通市值下限" :invalid="!!errs.cap"/><span class="muted">~</span><num-field v-model="form.predict.max_float_cap" :min="1" :max="100000" :step="50" :digits="0" unit="亿" label="流通市值上限" :width="form.predict.max_float_cap >= 10000 ? '150px' : null" :invalid="!!errs.cap"/><span v-if="form.predict.max_float_cap >= 100000" class="muted" style="font-size:12px;white-space:nowrap">（上限 = 不限）</span></div>
                <div v-if="errs.cap" class="fld-err">{{ errs.cap }}</div></div></div>
              <div class="fld"><div class="fld-l">股价<qw-help :text="HELP.price"/></div>
                <div class="fld-c"><div class="range-pair"><num-field v-model="form.predict.min_price" :min="0" :max="10000" :step="1" :digits="2" unit="元" label="最低股价" :invalid="!!errs.price"/><span class="muted">~</span><num-field v-model="form.predict.max_price" :min="0.01" :max="10000" :step="10" :digits="2" unit="元" label="最高股价" :invalid="!!errs.price"/><span v-if="form.predict.max_price >= 10000" class="muted" style="font-size:12px;white-space:nowrap">（上限 = 不限）</span></div>
                <div v-if="errs.price" class="fld-err">{{ errs.price }}</div></div></div>
              <div v-if="form.predict.kind === 'streak'" class="fld"><div class="fld-l">连板数<qw-help :text="HELP.streak"/></div>
                <div class="fld-c"><div class="range-pair"><num-field v-model="form.predict.streak_min" :min="0" :max="30" :digits="0" unit="板" label="连板数下限" :invalid="!!errs.streak"/><span class="muted">~</span><num-field v-model="form.predict.streak_max" :min="0" :max="30" :digits="0" unit="板" label="连板数上限" :invalid="!!errs.streak"/></div>
                <div v-if="errs.streak" class="fld-err">{{ errs.streak }}</div></div></div>
              <div v-if="form.predict.kind !== 'swing'" class="fld"><div class="fld-l">排除一字板<qw-help :text="HELP.oneword"/></div><div class="fld-c"><sw v-model="form.predict.exclude_one_word" label="排除一字板"/><span class="fld-hint">一字板第二天大概率买不进</span></div></div>
            </qw-card>

            <qw-card title="② 五维权重" icon="sliders" :help="HELP.weights">
              <template #extra><button v-if="weightsChanged" class="linkbtn" @click="resetWeights">全部恢复为 1</button></template>
              <p class="w-explain"><b>1</b> = 完全按模型，<b>0</b> = 不看这一面，<b>2</b> = 加倍重视</p>
              <div v-for="g in GROUPS" :key="g" class="w-row">
                <div class="w-l">{{ LABELS[g] }}<qw-help :text="DIM_HELP[g]"/></div>
                <input type="range" class="qw-range" min="0" max="2" step="0.1" v-model.number="form.predict.weights[g]" :style="sliderStyle(form.predict.weights[g])" :aria-label="LABELS[g] + '权重'">
                <div class="w-v"><b class="num">{{ $fmt.num(form.predict.weights[g], 1) }}</b><small>{{ weightWord(form.predict.weights[g]) }}</small></div>
              </div>
              <div class="w-row w-news">
                <div class="w-l">消息面<qw-help :text="HELP.news"/></div>
                <input type="range" class="qw-range" min="0" max="2" step="0.1" v-model.number="form.predict.news_weight" :style="sliderStyle(form.predict.news_weight)" aria-label="消息面权重">
                <div class="w-v"><b class="num">{{ $fmt.num(form.predict.news_weight, 1) }}</b><small>{{ weightWord(form.predict.news_weight) }}</small></div>
              </div>
              <p class="fld-hint w-news-hint"><qw-icon name="info" :size="13"/><span><b>消息热度≠利好</b>，默认 0 不参与排序。新闻多也可能是利空；它只有实时数据，只影响今天的名单，不参与历史检验。</span></p>
            </qw-card>

            <div class="stack">
              <qw-card title="③ 选股数量与门槛" icon="target">
                <div class="fld"><div class="fld-l">每天重点看前几名<qw-help :text="HELP.topn"/></div><div class="fld-c"><num-field v-model="form.predict.top_n" :min="1" :max="50" :digits="0" unit="只" label="每天选几只"/></div></div>
                <div class="fld"><div class="fld-l">{{ isSwing ? '预期收益至少' : '概率至少' }}<qw-help :text="isSwing ? HELP.thresholdSwing : HELP.threshold"/></div>
                  <div class="fld-c"><num-field v-model="form.predict.threshold" :scale="100" :min="0" :max="isSwing ? 20 : 100" :step="isSwing ? 0.5 : 1" :digits="1" unit="%" :label="isSwing ? '预期收益门槛' : '概率门槛'"/>
                  <div v-if="isSwing" class="fld-hint">{{ form.predict.threshold > 0 ? '预期收益低于 ' + $fmt.ratio(form.predict.threshold, 1) + ' 的不选；一只都达不到时当天“不操作”' : '不设门槛（不建议：会把把握不大的也选进来）' }}。推荐 1%。</div>
                  <div v-else class="fld-hint">{{ form.predict.threshold > 0 ? '明天涨停概率低于 ' + $fmt.ratio(form.predict.threshold, 1) + ' 的不选' : '不设门槛' }}；首板模型的概率整体偏低（多在 2%~10%），门槛要设得低一些。</div></div></div>
              </qw-card>
              <qw-card title="⑤ 资金管理" icon="wallet" sub="只用于历史检验，不会真的下单">
                <div class="fld"><div class="fld-l">初始资金<qw-help :text="HELP.capital"/></div><div class="fld-c"><num-field v-model="form.trade.capital" :min="1000" :max="1000000000" :step="10000" :digits="0" unit="元" width="170px" label="初始资金" :invalid="!!errs.capital"/><div v-if="errs.capital" class="fld-err">{{ errs.capital }}</div></div></div>
                <div class="fld"><div class="fld-l">单只仓位<qw-help :text="HELP.pos"/></div><div class="fld-c"><num-field v-model="form.trade.position_pct" :scale="100" :min="1" :max="100" :step="5" :digits="0" unit="%" label="单只仓位" :invalid="!!errs.pos"/><span class="fld-hint">每只约 {{ $fmt.money(perStock) }}</span><div v-if="errs.pos" class="fld-err">{{ errs.pos }}</div></div></div>
                <div class="fld"><div class="fld-l">最多同时持有<qw-help :text="HELP.maxpos"/></div><div class="fld-c"><num-field v-model="form.trade.max_positions" :min="1" :max="50" :digits="0" unit="只" label="最多同时持有"/></div></div>
              </qw-card>
            </div>

            <qw-card title="④ 买卖规则" icon="clock" sub="第 T 天收盘出名单 → 第二天开盘买入">
              <div class="opt-cards" role="radiogroup" aria-label="卖出规则">
                <button v-for="r in EXIT_RULES" :key="r.value" type="button" role="radio" class="opt-card" :class="{active: form.trade.exit_rule === r.value}" :aria-checked="form.trade.exit_rule === r.value" @click="form.trade.exit_rule = r.value">
                  <span class="oc-dot"></span><span class="oc-body"><b>{{ r.title }}<span v-if="defRules[form.predict.kind] === r.value" class="qw-tag blue oc-def">「{{ curKind.label }}」推荐</span></b><small>{{ r.desc }}</small></span>
                </button>
              </div>
              <div class="fld"><div class="fld-l">开盘涨太多就不买<qw-help :text="HELP.gap"/></div><div class="fld-c"><num-field v-model="form.trade.max_gap_pct" :min="0" :max="30" :step="1" :digits="1" unit="%" label="开盘涨幅上限"/><span class="fld-hint">开盘涨幅超过 {{ $fmt.num(form.trade.max_gap_pct, 1) }}% 放弃</span></div></div>
              <div class="fld"><div class="fld-l">止损<qw-help :text="HELP.stop"/></div><div class="fld-c"><num-field v-model="form.trade.stop_loss_pct" :scale="100" :min="0" :max="50" :step="1" :digits="1" unit="%" label="止损比例"/><span class="fld-hint">{{ form.trade.stop_loss_pct > 0 ? '跌 ' + $fmt.ratio(form.trade.stop_loss_pct, 1) + ' 就卖' : '0 = 不止损' }}</span></div></div>
            </qw-card>
          </div>

          <qw-card class="fee-card">
            <template #title><button type="button" class="fee-toggle" @click="feeOpen = !feeOpen" :aria-expanded="feeOpen"><qw-icon :name="feeOpen ? 'chevronDown' : 'chevronRight'" :size="16"/>⑥ 交易费用（高级）<small class="qw-card-sub">一般不用改，默认按普通券商费率</small></button></template>
            <div v-if="feeOpen" class="grid fee-grid">
              <div class="fld"><div class="fld-l">佣金费率<qw-help :text="HELP.fee"/></div><div class="fld-c"><num-field v-model="form.trade.fee_rate" :scale="100" :min="0" :max="1" :step="0.005" :digits="4" unit="%" label="佣金费率"/></div></div>
              <div class="fld"><div class="fld-l">印花税<qw-help :text="HELP.stamp"/></div><div class="fld-c"><num-field v-model="form.trade.stamp_duty" :scale="100" :min="0" :max="1" :step="0.01" :digits="4" unit="%" label="印花税"/></div></div>
              <div class="fld"><div class="fld-l">滑点<qw-help :text="HELP.slip"/></div><div class="fld-c"><num-field v-model="form.trade.slippage" :scale="100" :min="0" :max="5" :step="0.05" :digits="3" unit="%" label="滑点"/></div></div>
            </div>
          </qw-card>

          <div class="set-bar" :class="{dirty, compact: store.isPhone, open: store.isPhone && barOpen}">
            <template v-if="store.isPhone">
              <button type="button" class="set-bar-state sb-toggle" :aria-expanded="String(barOpen)" aria-label="更多操作" @click="barOpen = !barOpen"><i class="dot"></i>{{ errCount ? errCount + ' 项要改' : dirty ? '未保存' : '已保存' }}<qw-icon :name="barOpen ? 'chevronDown' : 'chevronUp'" :size="14"/></button>
              <span class="grow"></span>
              <button class="btn sm" :class="dirty ? 'primary' : ''" :disabled="saving || !dirty" @click="save"><qw-icon name="check" :size="14"/>{{ saving ? '保存中…' : '保存' }}</button>
              <button class="btn sm soft" :disabled="btLoading" @click="runBt(true)"><qw-icon name="history" :size="14"/>检验</button>
              <div v-if="barOpen" class="sb-more">
                <span class="sb-more-t">{{ errCount ? '有 ' + errCount + ' 项需要改正（标红的地方）' : dirty ? '「' + curKind.label + '」有未保存的修改' : '「' + curKind.label + '」的设置和现在在用的一样' }}</span>
                <button v-if="dirty" class="btn ghost sm" @click="undo(); barOpen = false">撤销修改</button>
                <button class="btn sm" @click="resetOpen = true; barOpen = false">恢复默认</button>
              </div>
            </template>
            <template v-else>
              <span class="set-bar-state"><i class="dot"></i>{{ errCount ? '有 ' + errCount + ' 项需要改正' : dirty ? '有未保存的修改' : '设置已保存' }}</span>
              <button v-if="dirty" class="btn ghost sm" @click="undo">撤销修改</button>
              <span class="grow"></span>
              <button class="btn" @click="resetOpen = true">恢复默认</button>
              <button class="btn" :class="dirty ? 'primary' : ''" :disabled="saving || !dirty" @click="save"><qw-icon name="check" :size="15"/>{{ saving ? '保存中…' : '保存设置' }}</button>
              <button class="btn soft" :disabled="btLoading" @click="runBt(true)"><qw-icon name="history" :size="15"/>用历史数据检验这套设置</button>
            </template>
          </div>
        </div>

        <div ref="btRef" class="bt-anchor"></div>
        <qw-card title="历史检验" icon="history" :help="HELP.oos">
          <template #extra>
            <span class="qw-tag" :class="curKind.tone === 'sim' ? 'blue' : ''">检验：{{ curKind.label }}</span>
            <button class="btn primary sm" :disabled="btLoading" @click="runBt(false)"><qw-icon name="refresh" :size="14" :class="{spin: btLoading}"/>{{ bt ? '重新检验' : '开始检验' }}</button>
          </template>
          <div class="oos-label"><qw-icon name="info" :size="15"/><span>以下为<b>样本外检验</b>（模型没见过的历史）<template v-if="bt && period">：{{ btKind }}，{{ period }}</template>。已扣除佣金、印花税和滑点，开盘一字涨停买不进的已经跳过；并和<b>同一批候选股里随机挑选</b>的结果对比。</span></div>

          <div v-if="btLoading" class="bt-loading">
            <qw-skeleton type="tiles" :count="6"/>
            <p class="muted" style="margin-top:12px">正在用历史数据按你的规则模拟买卖，通常几秒钟…</p>
          </div>
          <qw-empty v-else-if="btErr" icon="alert" title="检验没能完成" :desc="btErr">
            <button class="btn primary" @click="runBt(false)">再试一次</button>
            <a class="btn" href="#/model">去模型中心</a>
          </qw-empty>
          <qw-empty v-else-if="!bt" icon="history" title="还没检验过这套设置" desc="点“开始检验”，用模型没见过的历史数据，按上面的规则模拟买卖，看看这套设置过去的表现。" action-text="开始检验" @action="runBt(false)"/>

          <template v-else-if="m">
            <div v-if="btStale" class="bt-stale"><qw-icon name="alert" :size="15"/>你修改了设置，下面还是修改前的检验结果。<button class="linkbtn" @click="runBt(false)">按新设置重新检验</button></div>
            <qw-empty v-if="!m.trades" compact icon="filter" title="这套设置在历史上一笔都没买成" desc="可能是条件太严（门槛太高、范围太窄），或者开盘涨幅上限太低。放宽一些再试试。"/>
            <template v-else>
              <div v-if="recentBt" class="bt-recent" :class="recentBt.verdict ? recentBt.verdict.tone : ''">
                <div class="br-hd">{{ recentBt.label }}：这套设置 vs 同一天随便挑<qw-help :text="HELP.recent"/></div>
                <div class="br-nums">
                  <div><b class="num" :class="$fmt.dir(recentBt.mean)">{{ $fmt.ratio(recentBt.mean, 2, true) }}</b><small>这套设置 平均每笔<template v-if="recentBt.n">（{{ $fmt.int(recentBt.n) }} 笔）</template></small></div>
                  <span class="br-vs">对比</span>
                  <div><b class="num" :class="$fmt.dir(recentBt.rnd)">{{ recentBt.rnd == null ? '—' : $fmt.ratio(recentBt.rnd, 2, true) }}</b><small>同一天随便挑同样多只<qw-help :text="HELP.match"/></small></div>
                  <div v-if="recentBt.t != null"><b class="num">{{ $fmt.t(recentBt.t) }}</b><small>留出期 t 值（{{ tWord(recentBt.t) }}）</small></div>
                </div>
                <p v-if="recentBt.verdict" class="br-say"><b>一句话：{{ recentBt.verdict.text }}。</b><span class="muted">下面的格子是全部样本外（含研究时挑方案用的选择期），偏乐观。</span></p>
              </div>
              <div class="bt-tiles">
                <qw-stat label="交易次数" :value="$fmt.int(m.trades)" unit="笔" :help="HELP.trades" :sub="m.unfilled ? '另有 ' + m.unfilled + ' 次买不进' : ''" flat/>
                <qw-stat label="胜率" :value="$fmt.ratio(mm.win, 1)" :help="HELP.win" :sub="base && base.win != null ? '天天随便挑 ' + $fmt.ratio(base.win, 1) : ''" flat/>
                <qw-stat label="平均每笔收益" :value="$fmt.ratio(mm.avg, 2, true)" :tone="$fmt.dir(mm.avg)" :help="HELP.avg" flat>
                  <template #sub><span v-if="base && base.mean != null" class="bt-vs">天天随便挑 <b :class="$fmt.dir(base.mean)">{{ $fmt.ratio(base.mean, 2, true) }}</b><qw-help :text="HELP.base"/></span><span v-else>中位数 {{ $fmt.ratio(mm.med, 2, true) }}</span></template>
                </qw-stat>
                <qw-stat label="年化收益" :value="$fmt.ratio(mm.cagr, 1, true)" :tone="$fmt.dir(mm.cagr)" :help="HELP.cagr" :sub="base && base.cagr != null ? '天天随便挑 ' + $fmt.ratio(base.cagr, 1, true) : '总收益 ' + $fmt.ratio(mm.total, 1, true)" flat/>
                <qw-stat label="最大回撤" :value="$fmt.ratio(mm.dd, 1, true)" :help="HELP.dd" sub="中途最多亏过这么多" flat/>
                <qw-stat v-if="nt && nt.t != null" label="可信度（t 值）" :value="$fmt.t(nt.t)" :help="tHelp(nt)" flat>
                  <template #sub><span class="t-tag" :class="nt.t >= 2 ? (tuned ? 'mid' : 'ok') : nt.t >= 1 ? 'mid' : 'bad'" style="margin-left:0">{{ tWord(nt.t) }}</span><span>≥2 才算比较可信<template v-if="nt.hasNw">（两种算法取低的）</template></span></template>
                </qw-stat>
                <qw-stat v-else-if="!btSwing" label="命中率 vs 基准率" :value="$fmt.ratio(mm.hit, 1)" :help="HELP.hit" :sub="'基准 ' + $fmt.ratio(mm.base, 1) + (mm.mult ? ' · 约 ' + $fmt.num(mm.mult, 1) + ' 倍' : '')" flat/>
              </div>
              <div v-if="tuned" class="qw-banner warn section-gap bt-tuned"><qw-icon name="alert" :size="20"/><div class="qw-banner-body">
                你改过「{{ btKind }}」的推荐设置（{{ tuned.slice(0, 6).join('、') }}{{ tuned.length > 6 ? ' 等' : '' }}）。如果是看着这里的检验结果改的，下面的历史成绩——<b>包括留出期</b>——都会偏乐观，只能靠之后的模拟盘来验证<template v-if="triedCount > 1">；你在这台电脑上已经检验过 <b class="num">{{ triedCount }}</b> 套不同的设置，试得越多，挑出来的那套越可能只是碰巧在这段历史上好看</template>。<qw-help :text="HELP.tuned"/>
              </div></div>
              <div v-if="verdict" class="qw-banner section-gap" :class="verdict.tone"><qw-icon :name="verdict.tone === 'warn' ? 'alert' : 'info'" :size="20"/><div class="qw-banner-body" v-html="verdict.text"></div></div>
              <div v-if="skipHeavy" class="qw-banner warn section-gap bt-skip"><qw-icon name="wallet" :size="20"/><div class="qw-banner-body">
                按你的本金，钱不够或一手太贵<b>跳过了 {{ $fmt.ratio(skipHeavy.share, 0) }} 的信号</b><template v-if="skipHeavy.parts">（{{ skipHeavy.parts }}）</template>，上面账户的成绩只代表其中一部分信号，主要是前一段时间的。
                <template v-if="skipHeavy.unc">每个信号都买（不受资金限制、按比例收费）：<b class="num">{{ $fmt.int(skipHeavy.unc.n) }}</b> 笔，平均每笔 <b class="num" :class="$fmt.dir(skipHeavy.unc.mean)">{{ $fmt.ratio(skipHeavy.unc.mean, 2, true) }}</b><template v-if="skipHeavy.unc.win != null">，胜率 {{ $fmt.ratio(skipHeavy.unc.win, 0) }}</template>。</template>
                本金越少、股价越高、账户亏得越多，后面的信号就越容易买不起。<qw-help :text="UNCAPPED_HELP"/>
              </div></div>
              <div v-if="cmpRows.length && (base || hasPeriods || (nt && nt.t != null))" class="bt-sub section-gap bt-cmp">
                <h4>这套设置 vs 同池随机挑选<template v-if="hasPeriods">：选择期 / 留出期分开看</template><qw-help :text="HELP.period"/></h4>
                <qw-table :columns="cmpCols" :rows="cmpRows" row-key="key" dense>
                  <template #cell-label="{row}"><span class="nowrap">{{ row.label }}</span><qw-help v-if="row.help" :text="row.help"/></template>
                  <template #cell-all="{row}"><span class="num" :class="[row.all.cls, {b: row.all.bold}]">{{ row.all.text }}</span></template>
                  <template #cell-sel="{row}"><span class="num" :class="[row.sel.cls, {b: row.sel.bold}]">{{ row.sel.text }}</span></template>
                  <template #cell-hold="{row}"><span class="num" :class="[row.hold.cls, {b: row.hold.bold}]">{{ row.hold.text }}</span></template>
                </qw-table>
                <p class="fld-hint" style="margin-top:8px"><b>怎么看：</b>“这套设置”要明显高于“随机挑”，模型才算真的挑出了好股票；“天天随便挑”每天都挑满（包括不操作的日子），比它多赚的里面也有择时的作用<template v-if="nt && nt.match && !nt.match.same">，“同日同数量随便挑”才是只比挑股票</template>；t 值 ≥2 才算比较可信<template v-if="hasPeriods && !tuned">；留出期的数据没有参与训练，比选择期更接近现在的真实水平，但方案是在研究中比较多种做法后选出的，结果可能偏乐观</template><template v-else-if="hasPeriods">；你改过设置，留出期也被你拿来挑设置了，更偏乐观</template>。每个时段（包括“全部”）的策略和随机挑都是各用一个全新账户单独算的。</p>
              </div>
              <div class="kv section-gap bt-extra">
                <div v-for="x in extras" :key="x.k"><div class="k">{{ x.k }}<qw-help v-if="x.help" :text="x.help"/></div><div class="v num" :class="x.cls">{{ x.v }}</div></div>
              </div>

              <div class="section-gap"><qw-chart v-if="eqOption" :option="eqOption" height="396px"/></div>

              <div class="grid bt-grid section-gap" :class="{'bt-grid-one': btSwing}">
                <div class="bt-sub"><h4>每年表现</h4>
                  <qw-table :columns="[{key:'year',label:'年份'},{key:'return',label:'当年收益',align:'right'},{key:'bar',label:'',minWidth:'110px'},{key:'trades',label:'交易',align:'right'},{key:'win_rate',label:'胜率',align:'right'}]" :rows="yearly" row-key="year" dense>
                    <template #cell-return="{row}"><qw-price :value="$fmt.frac(row.return, 5)" ratio :digits="1"/></template>
                    <template #cell-bar="{row}"><span class="ybar" :class="$fmt.dir(row.return)"><i :style="{width: Math.min(100, Math.abs($fmt.frac(row.return, 5) || 0) / yearMax * 100) + '%'}"></i></span></template>
                    <template #cell-trades="{row}"><span class="num">{{ row.trades }}</span></template>
                    <template #cell-win_rate="{row}"><span class="num">{{ $fmt.ratio($fmt.frac(row.win_rate, 1.5), 0) }}</span></template>
                  </qw-table>
                </div>
                <div v-if="!btSwing" class="bt-sub"><h4>前 N 名命中率<qw-help :text="HELP.topn_t"/></h4>
                  <qw-table :columns="[{key:'n',label:'每天看前几名'},{key:'h',label:'次日涨停率',align:'right'},{key:'mult',label:'是基准的',align:'right'},{key:'picks',label:'样本数',align:'right'}]" :rows="topn" row-key="n" dense>
                    <template #cell-n="{row}">前 {{ row.n }} 名</template>
                    <template #cell-h="{row}"><b class="num">{{ $fmt.ratio(row.h, 1) }}</b></template>
                    <template #cell-mult="{row}"><span class="num" :class="row.mult > 1.05 ? 'up' : ''">{{ row.mult ? $fmt.num(row.mult, 2) + ' 倍' : '—' }}</span></template>
                    <template #cell-picks="{row}"><span class="num muted">{{ row.picks != null ? $fmt.int(row.picks) : '—' }}</span></template>
                  </qw-table>
                  <p class="fld-hint" style="margin-top:8px">基准率 {{ $fmt.ratio(mm.base, 1) }}：同期全部候选股第二天涨停的平均比例。</p>
                </div>
                <div v-if="!btSwing" class="bt-sub"><h4>概率准不准<qw-help :text="HELP.calib"/></h4>
                  <qw-chart v-if="calOption" :option="calOption" height="240px"/>
                  <p v-else class="muted">没有足够的样本。</p>
                </div>
              </div>

              <div v-if="btNotes.length" class="bt-sub section-gap bt-notes"><h4>说明</h4>
                <ul class="warn-list"><li v-for="(n, i) in btNotes" :key="i" class="note"><qw-icon name="info" :size="14"/><span>{{ n }}</span></li></ul>
              </div>
              <div class="bt-sub section-gap"><h4>最近的模拟交易 <small class="muted">（最多 500 笔，点击看盘）</small></h4>
                <qw-table :columns="tradeCols" :rows="trades" row-key="_k" :page-size="12" :default-sort="{key:'signal_date',order:'desc'}" clickable dense @row-click="(r) => $go('/watch/' + r.code)">
                  <template #cell-signal_date="{row}"><span class="num nowrap">{{ row.signal_date }}</span></template>
                  <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
                  <template #cell-score="{row}"><span class="num">{{ btSwing ? $fmt.ratio($fmt.frac(row.score, 0.5), 2, true) : $fmt.ratio($fmt.frac(row.score, 1.5), 1) }}</span></template>
                  <template #cell-entry="{row}"><span class="num">{{ $fmt.price(row.entry) }}</span><small class="muted cell-date">{{ $fmt.md(row.entry_date) }}</small></template>
                  <template #cell-exit="{row}"><span class="num">{{ $fmt.price(row.exit) }}</span><small class="muted cell-date">{{ $fmt.md(row.exit_date) }}</small></template>
                  <template #cell-ret="{row}"><qw-price :value="$fmt.frac(row.ret, 1.5)" ratio :digits="2"/></template>
                  <template #cell-reason="{row}"><span class="text-2">{{ row.reason || '—' }}</span></template>
                </qw-table>
              </div>
            </template>
          </template>
        </qw-card>

        <qw-modal v-model="resetOpen" title="恢复默认设置？" width="440px">
          <p class="text-2">会把选股范围、五维权重、门槛、买卖规则、资金和费用都改回「{{ curKind.label }}」的推荐值。点“保存设置”之前不会生效。</p>
          <template #footer><button class="btn ghost" @click="resetOpen = false">取消</button><button class="btn primary" @click="doReset">恢复默认</button></template>
        </qw-modal>
      </template>
    </div>`,
  });
})();
