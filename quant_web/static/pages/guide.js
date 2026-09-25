/* 新手指南 #/guide：首次使用向导（资金/权限/看盘时间/周期/风险 → settings.profile）、每天怎么用、名词解释、重要提醒 */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted } = Vue;
  const { api, fmt, store, toast } = QW;

  const BOARD_OPTS = [
    { value: "main", label: "沪深主板", sub: "60、00 开头", need: "开户就能买" },
    { value: "chinext", label: "创业板", sub: "30 开头", need: "需要在券商单独开通（有资产和交易经验要求）" },
    { value: "star", label: "科创板", sub: "68 开头", need: "需要单独开通，门槛较高（50 万资产 + 2 年经验）" },
    { value: "bj", label: "北交所", sub: "8、4、92 开头", need: "需要单独开通，门槛较高（50 万资产 + 2 年经验）" },
  ];
  const WATCH_OPTS = [
    { value: "evening", label: "只能晚上看", desc: "收盘后程序生成“明日计划”；止损靠券商 App 的条件单，或盘中自动推送提醒。建议做波段。" },
    { value: "sometimes", label: "白天偶尔看手机", desc: "盘中出现重要情况（跌破止损、疑似出货）推送到微信，你看到后处理。" },
    { value: "fulltime", label: "能全天盯盘", desc: "可以用盘中实时监控，也可以尝试短线（风险更大）。" },
  ];
  const HORIZON_OPTS = [
    { value: "swing", label: "波段（几天到几周）", desc: "吃一段上涨就走，不用天天盯盘。最适合新手和上班族。" },
    { value: "short", label: "短线（1–3 天）", desc: "节奏快、要盯盘，手续费和判断失误都更多。新手不推荐。" },
    { value: "long", label: "中长线（几个月以上）", desc: "更看公司质地和大趋势，操作少，但要能扛住中途的回撤。" },
  ];
  const RISK_OPTS = [
    { value: 0.005, label: "很保守 0.5%" },
    { value: 0.01, label: "稳健 1%（推荐）" },
    { value: 0.02, label: "平衡 2%" },
    { value: 0.03, label: "激进 3%" },
  ];

  // 每天怎么用：page 为页面名（页面还没做好时显示“建设中”）
  const ROUTINE = [
    { when: "每天收盘后（15:35 以后）", title: "一键更新数据", page: "data", icon: "refresh", text: "程序会在交易日收盘后自动更新；如果电脑当时没开，打开后点右上角“一键更新”。" },
    { when: "晚上第 1 步", title: "看大盘环境", page: "dashboard", icon: "pulse", text: "先看今天市场是强势、震荡还是弱势，以及建议的总仓位上限。弱势时少买甚至不买。" },
    { when: "晚上第 2 步", title: "处理持仓", page: "trade", query: "tab=nightly", icon: "briefcase", text: "看“明日计划”里每只持仓的止损价、目标价，有没有出现出货信号。需要卖的，按计划执行。" },
    { when: "晚上第 3 步", title: "选股", page: "screener", icon: "filter", text: "用默认的选股方案看看今天有哪些候选，程序会标出主力阶段、风险和建议股数。在「策略中心」开了“实盘建议”的策略，它的候选已经放进明日计划里了。" },
    { when: "晚上第 4 步", title: "逐只诊断", page: "watch", icon: "candle", text: "点进候选股看 K 线、量价关系、筹码和排雷清单，确认没有明显问题。" },
    { when: "晚上第 5 步", title: "确认明日买单 + 设条件单", page: "trade", query: "tab=nightly", icon: "listCheck", text: "在明日计划里确认要买的股票；然后按“条件单清单”在券商 App 里把止损条件单设好（电脑关机也能止损）。" },
    { when: "每周一次", title: "复盘", page: "trade", query: "tab=review", icon: "history", text: "看看自己有没有挪止损、追高、补仓摊平。纪律比选股更重要。" },
  ];

  const REMINDERS = [
    { icon: "alert", tone: "warn", title: "没有稳赚的方法", text: "任何工具（包括基金经理的系统）都不能保证选的股票一定涨。本程序能做的是：把每个方法的真实历史成绩摆给你看，帮你避开明显的坑，用纪律管住冲动。" },
    { icon: "shield", tone: "", title: "先模拟，再小钱，最后才加钱", text: "新方法先在模拟账户里跑至少 1–2 个月，确认自己能按计划执行，再用小资金实盘。" },
    { icon: "stop", tone: "warn", title: "止损是第一纪律", text: "每笔买入前就定好止损价，跌到就卖，不要“等反弹”。一笔最多亏总资金的 1%，连亏几笔也伤不到根本。" },
    { icon: "layers", tone: "", title: "大部分钱放稳健配置", text: "建议把大部分资金（比如 70%）放在“稳健 ETF”做底仓，小部分（比如 30%）用来选股。选股亏了也不影响大局。" },
    { icon: "target", tone: "", title: "只看样本外成绩", text: "页面上所有胜率和收益都是模型“没见过的数据”上的成绩，并且扣过手续费、和随机乱买做过对比。t 值小于 2 的会标“还不够可信”。" },
    { icon: "flame", tone: "warn", title: "别追高，别补仓摊平", text: "当天已经大涨的股票要二次确认；亏损的股票不要越跌越买。程序会在你这样做时提醒你。" },
  ];

  // 功能说明：每个页面是干什么的、现在能不能拿来赚钱（实话实说）
  const STATUS = { ok: ["blue", "可以用"], ref: ["gray", "只能参考"], unproven: ["warn", "没证明能赚钱"], watch: ["red", "只观察，别照着买"] };
  const FEATURES = [
    { group: "行情", items: [
      { page: "dashboard", name: "市场情绪（首页）", status: "ok", text: "大盘环境（强势/震荡/弱势 → 建议总仓位上限）、情绪温度、你的账户、明日计划、未读提醒。大盘环境只用来控制仓位，不用来猜涨跌。" },
      { page: "lianban", name: "连板", status: "ref", text: "连板天梯（封单、首封时间、炸板次数）、各高度第二天的晋级率和平均表现、一年的情绪周期、涨停/炸板/跌停池；可以选过去的日子复盘第二天谁晋级了。看短线情绪用——历史上照着买连板股是亏的。" },
      { page: "watch", name: "看盘 / 个股诊断", status: "ref", text: "多指标 K 线、筹码分布（估算）、主力阶段证据（吸筹/洗盘/拉升/出货）、排雷清单、资金流。能帮你看清量价、避开明显的坑；主力阶段的历史验证没有跑赢随机，别单凭它买。" },
      { page: "watchlist", name: "自选股", status: "ok", text: "关注的股票和实时行情；盘中监控也会给自选股做异动提醒。" },
      { page: "sectors", name: "板块强弱", status: "ref", text: "按行业汇总的 5/20/60 日强弱和龙头股，看热点轮动用。" },
    ] },
    { group: "选股", items: [
      { page: "mf", name: "量化选股（核心）", status: "ok", text: "34 个有公开研究依据的因子 + LightGBM，每周给出下周要持有的 50 只主板股票和按资金算好的下单清单。样本外（2022 年起、扣全部费用）500 万资金每年比同池随机多约 14%（t≈2.3，跑赢全部 60 个随机组合）；资金越大优势越小，上亿基本没有。偏中小盘，2024 年基本持平，不是保证——以前向跟踪为准。" },
      { page: "screener", name: "选股器", status: "unproven", text: "按方案（范围 → 条件 → 排雷 → 打分）选股，给出建议止损和按风险算好的股数；每个方案都能做历史回测（样本外、和同日随机比）。这些按条件筛选的方案都没有显著跑赢随机（默认方案的好处是回撤更小）；要选股请优先看“量化选股”。" },
      { page: "formula", name: "公式库", status: "unproven", text: "通达信风格的公式（25 个内置 + 你自己写的），禁止未来函数；可以看历史上信号出现后真实的表现。经典的均线金叉、放量突破都没有跑赢随机。" },
    ] },
    { group: "策略", items: [
      { page: "strategy", name: "策略中心", status: "unproven", text: "选股 + 买卖规则 + 仓位 + 大盘过滤组成一个策略：先诚实回测（真正的模拟盘规则，和同样规则随机选股比），再模拟跟踪，最后才生成实盘建议。" },
      { page: "lab", name: "模型实验室", status: "unproven", text: "自己组合股票范围、因子、预测目标和模型训练选股模型（你点了才训练），结果全部是样本外的；在留出期有优势的模型才值得启用到选股器。" },
      { page: "predict", name: "短线预测（连板/首板）", status: "watch", text: "能预测谁更可能涨停，但按真实规则去买会亏钱，比随机还差。只用来观察市场热度。" },
    ] },
    { group: "交易", items: [
      { page: "trade", name: "交易", status: "ok", text: "模拟盘和实盘账户、下单前逐条风控检查、交易计划（止损/目标/移动止盈）、明日计划和券商条件单清单、提醒、复盘（R 倍数、人性陷阱）、推送设置、一键停止实盘。" },
      { page: "paper", name: "策略跟踪（旧）", status: "unproven", text: "旧的“强势股波段”模型的模拟跟踪记录。留出期没有跑赢随机。" },
    ] },
    { group: "更多", items: [
      { page: "etf", name: "稳健 ETF", status: "ok", text: "多资产 ETF 趋势配置，适合放大部分资金做底仓（历史回测年化约 8.9%、最大回撤约 11%，参数在同一段历史上选的，真实效果可能差一些）。" },
      { page: "data", name: "数据中心", status: "ok", text: "一键更新数据、后台任务、扩展数据（资金流、融资融券、解禁等）。" },
      { page: "sources", name: "数据源", status: "ok", text: "每种数据用哪个数据源、失败时换哪个，可以测试连接。" },
    ] },
  ];

  QW.page("guide", {
    props: ["params", "query"],
    setup(props) {
      const tab = ref(props.query && props.query.tab ? props.query.tab : "setup");
      const tabs = [
        { value: "setup", label: "首次设置", icon: "sliders" },
        { value: "routine", label: "每天怎么用", icon: "calendar" },
        { value: "glossary", label: "名词解释", icon: "book" },
        { value: "remind", label: "重要提醒", icon: "alert" },
        { value: "features", label: "功能说明", icon: "grid" },
      ];

      // ---------------------------------------------------------- 向导
      const loading = ref(true);
      const saving = ref(false);
      const step = ref(0);
      const form = reactive({ capital: 100000, boards: ["main"], watch_time: "evening", horizon: "swing", risk_per_trade: 0.01 });
      const onboarded = ref(false);
      const STEPS = ["资金", "交易权限", "看盘时间", "操作周期", "风险承受", "完成"];
      onMounted(async () => {
        try {
          const s = await api.get("/api/settings", null, { silent: true });
          const p = s.profile || {};
          Object.assign(form, {
            capital: p.capital || 100000, boards: (p.boards && p.boards.length ? p.boards : ["main"]).slice(),
            watch_time: p.watch_time || "evening", horizon: p.horizon || "swing", risk_per_trade: p.risk_per_trade || 0.01,
          });
          onboarded.value = !!p.onboarded;
        } catch (e) { /* 用默认值 */ } finally { loading.value = false; }
      });
      const toggleBoard = (b) => {
        if (b === "main") return;           // 主板必选
        const i = form.boards.indexOf(b);
        if (i >= 0) form.boards.splice(i, 1); else form.boards.push(b);
      };
      const riskMoney = computed(() => Math.round((form.capital || 0) * form.risk_per_trade));
      const perStockMax = computed(() => Math.round(riskMoney.value / 0.08));        // 止损 8% 时单只最多买多少钱
      const capOk = computed(() => isFinite(form.capital) && form.capital >= 1000);
      const horizonHint = computed(() => (form.watch_time === "evening" && form.horizon !== "swing"
        ? "你只能晚上看盘，短线需要盯盘、中长线要扛回撤，建议选“波段”。" : ""));
      const next = () => { if (step.value === 0 && !capOk.value) { toast.warn("资金至少 1000 元"); return; } step.value = Math.min(step.value + 1, STEPS.length - 1); };
      const prev = () => { step.value = Math.max(step.value - 1, 0); };
      const save = async () => {
        saving.value = true;
        try {
          const order = BOARD_OPTS.map((b) => b.value);
          const boards = order.filter((b) => form.boards.includes(b));
          await api.put("/api/settings", { profile: { ...form, boards, onboarded: true } });
          onboarded.value = true;
          store.needOnboard = false;
          toast.success("已保存。之后可以随时回到这里修改。");
          tab.value = "routine";
        } catch (e) { /* 已提示 */ } finally { saving.value = false; }
      };

      // ---------------------------------------------------------- 名词解释
      const q = ref("");
      const cat = ref("全部");
      const cats = computed(() => ["全部", ...(QW.GLOSSARY_CATS || [])].map((c) => ({ value: c, label: c })));
      const terms = computed(() => {
        const G = QW.GLOSSARY || {};
        const kw = q.value.trim().toLowerCase();
        return Object.keys(G)
          .filter((k) => cat.value === "全部" || G[k].cat === cat.value)
          .filter((k) => !kw || k.toLowerCase().includes(kw) || G[k].text.toLowerCase().includes(kw))
          .map((k) => ({ k, ...G[k] }));
      });

      const pageReady = (name) => !!QW.pages[name];
      const pageLink = (name, query) => {
        const r = (QW.ROUTES || []).find((x) => x.name === name);
        return r ? "#" + (r.nav || r.path) + (query ? "?" + query : "") : "#/";
      };

      return {
        store, fmt, tab, tabs, loading, saving, step, STEPS, form, onboarded, BOARD_OPTS, WATCH_OPTS, HORIZON_OPTS, RISK_OPTS,
        toggleBoard, riskMoney, perStockMax, capOk, horizonHint, next, prev, save, q, cat, cats, terms, ROUTINE, REMINDERS, FEATURES, STATUS,
        pageReady, pageLink,
      };
    },
    template: `<div class="stack">
      <qw-tabs v-model="tab" :items="tabs"/>

      <!-- ================= 首次设置 ================= -->
      <qw-card v-if="tab === 'setup'" title="首次使用向导" icon="sliders" :sub="onboarded ? '你已经完成过向导，可以随时修改' : '花 1 分钟告诉程序你的情况，它会据此给出仓位和止损建议'" :loading="loading">
        <div class="gd-steps">
          <button v-for="(s, i) in STEPS" :key="s" class="gd-step" :class="{on: i === step, done: i < step}" @click="i <= step || onboarded ? (step = i) : null">
            <i>{{ i + 1 }}</i><span>{{ s }}</span>
          </button>
        </div>

        <div v-if="step === 0" class="gd-body">
          <h3>你准备用多少钱来买股票？</h3>
          <p class="muted">只用亏了也不影响生活的钱。这个数字用来计算每只股票买多少股（不会动你的真实账户）。</p>
          <label class="gd-money"><input v-model.number="form.capital" type="number" min="1000" step="1000" class="qw-input num"><span>元</span></label>
          <div class="gd-note"><qw-icon name="layers" :size="16"/><span>建议：大部分资金（比如 70%）放“稳健 ETF”做底仓，这里只填准备用来<b>选股</b>的那部分。</span></div>
        </div>

        <div v-else-if="step === 1" class="gd-body">
          <h3>你的账户能买哪些板块？</h3>
          <p class="muted">不确定就只选“沪深主板”。选了没开通的板块，程序可能推荐你买不了的股票。</p>
          <div class="gd-options">
            <button v-for="b in BOARD_OPTS" :key="b.value" class="gd-opt" :class="{on: form.boards.includes(b.value), fixed: b.value === 'main'}" @click="toggleBoard(b.value)">
              <b>{{ b.label }} <small class="muted">{{ b.sub }}</small></b><span class="muted">{{ b.need }}</span>
              <qw-icon v-if="form.boards.includes(b.value)" name="checkCircle" :size="18" class="gd-check"/>
            </button>
          </div>
        </div>

        <div v-else-if="step === 2" class="gd-body">
          <h3>你平时什么时候能看股票？</h3>
          <div class="gd-options">
            <button v-for="o in WATCH_OPTS" :key="o.value" class="gd-opt" :class="{on: form.watch_time === o.value}" @click="form.watch_time = o.value; if (o.value === 'evening') form.horizon = 'swing'">
              <b>{{ o.label }}</b><span class="muted">{{ o.desc }}</span>
              <qw-icon v-if="form.watch_time === o.value" name="checkCircle" :size="18" class="gd-check"/>
            </button>
          </div>
        </div>

        <div v-else-if="step === 3" class="gd-body">
          <h3>一只股票一般打算拿多久？</h3>
          <div class="gd-options">
            <button v-for="o in HORIZON_OPTS" :key="o.value" class="gd-opt" :class="{on: form.horizon === o.value}" @click="form.horizon = o.value">
              <b>{{ o.label }}</b><span class="muted">{{ o.desc }}</span>
              <qw-icon v-if="form.horizon === o.value" name="checkCircle" :size="18" class="gd-check"/>
            </button>
          </div>
          <div v-if="horizonHint" class="gd-note warn"><qw-icon name="alert" :size="16"/><span>{{ horizonHint }}</span></div>
        </div>

        <div v-else-if="step === 4" class="gd-body">
          <h3>一笔交易最多能接受亏多少？</h3>
          <qw-segmented v-model="form.risk_per_trade" :options="RISK_OPTS"/>
          <div class="gd-calc">
            <div>本金 <b class="num">{{ fmt.money(form.capital) }}</b>，一笔最多亏 <b class="num down">{{ fmt.money(riskMoney) }}</b></div>
            <div class="muted">举例：止损设在买入价下方 8%，那么这只股票最多买 <b class="num">{{ fmt.money(perStockMax) }}</b>（再按 100 股取整，并且单只不超过总资金 20%）。止损越近，能买得越多；止损越远，买得越少——亏损金额始终被控制住。</div>
          </div>
        </div>

        <div v-else class="gd-body">
          <h3>确认你的情况</h3>
          <div class="gd-summary">
            <div><span class="muted">选股资金</span><b class="num">{{ fmt.money(form.capital) }}</b></div>
            <div><span class="muted">能买的板块</span><b>{{ BOARD_OPTS.filter((b) => form.boards.includes(b.value)).map((b) => b.label).join('、') }}</b></div>
            <div><span class="muted">看盘时间</span><b>{{ (WATCH_OPTS.find((o) => o.value === form.watch_time) || {}).label }}</b></div>
            <div><span class="muted">操作周期</span><b>{{ (HORIZON_OPTS.find((o) => o.value === form.horizon) || {}).label }}</b></div>
            <div><span class="muted">单笔最多亏</span><b>{{ fmt.ratio(form.risk_per_trade, 1) }}（{{ fmt.money(riskMoney) }}）</b></div>
          </div>
          <p class="muted" style="margin-top:10px">这些只影响程序给出的建议（选股范围、仓位、提醒方式），不会动你的真实账户。以后随时可以回来改。</p>
        </div>

        <template #footer>
          <div class="row between" style="width:100%">
            <button class="btn ghost" :disabled="step === 0" @click="prev"><qw-icon name="chevronLeft" :size="15"/>上一步</button>
            <button v-if="step < STEPS.length - 1" class="btn primary" @click="next">下一步<qw-icon name="chevronRight" :size="15"/></button>
            <button v-else class="btn primary" :disabled="saving" @click="save"><qw-icon name="check" :size="15"/>{{ saving ? '保存中…' : '保存' }}</button>
          </div>
        </template>
      </qw-card>

      <!-- ================= 每天怎么用 ================= -->
      <qw-card v-else-if="tab === 'routine'" title="只能晚上看盘，每天这样用" icon="calendar" sub="大约 15–30 分钟">
        <div class="gd-routine">
          <div v-for="(r, i) in ROUTINE" :key="i" class="gd-rt">
            <div class="gd-rt-icon"><qw-icon :name="r.icon" :size="18"/></div>
            <div class="gd-rt-main">
              <div class="gd-rt-when muted">{{ r.when }}</div>
              <div class="gd-rt-title">{{ r.title }}
                <a v-if="pageReady(r.page)" class="btn sm" :href="pageLink(r.page, r.query)">去看看<qw-icon name="chevronRight" :size="13"/></a>
                <span v-else class="qw-tag">建设中</span>
              </div>
              <div class="text-2">{{ r.text }}</div>
            </div>
          </div>
        </div>
      </qw-card>

      <!-- ================= 名词解释 ================= -->
      <qw-card v-else-if="tab === 'glossary'" title="名词解释" icon="book" :sub="'共 ' + terms.length + ' 条，页面上带虚线的词都可以悬停查看'">
        <div class="gd-gl-bar">
          <input v-model="q" class="qw-input" placeholder="搜索，例如：洗盘、MACD、止损" style="max-width:280px">
          <qw-segmented v-model="cat" :options="cats" size="sm"/>
        </div>
        <div class="gd-gl">
          <div v-for="t in terms" :key="t.k" class="gd-gl-item">
            <div class="gd-gl-k">{{ t.k }} <span class="qw-tag">{{ t.cat }}</span></div>
            <div class="text-2">{{ t.text }}</div>
            <div v-if="t.tip" class="gd-gl-tip"><qw-icon name="alert" :size="14"/>{{ t.tip }}</div>
          </div>
          <qw-empty v-if="!terms.length" icon="search" title="没有找到" desc="换个词试试" compact/>
        </div>
      </qw-card>

      <!-- ================= 功能说明 ================= -->
      <template v-else-if="tab === 'features'">
        <div class="gd-note"><qw-icon name="info" :size="15"/><span>每个页面是干什么的，以及<b>现在能不能拿来赚钱</b>（实话实说）。“没证明能赚钱”不是说一定亏，而是历史样本外检验没有显著跑赢“同一天随便买”，所以只能小仓位、先模拟。</span></div>
        <qw-card v-for="g in FEATURES" :key="g.group" :title="g.group" :pad="false">
          <div class="gd-feat" v-for="f in g.items" :key="f.page">
            <div class="gd-feat-hd"><b>{{ f.name }}</b><span class="qw-tag" :class="STATUS[f.status][0]">{{ STATUS[f.status][1] }}</span>
              <a v-if="pageReady(f.page)" class="btn sm ghost" :href="pageLink(f.page)">打开<qw-icon name="chevronRight" :size="13"/></a></div>
            <p class="text-2">{{ f.text }}</p>
          </div>
        </qw-card>
      </template>

      <!-- ================= 重要提醒 ================= -->
      <div v-else class="gd-remind">
        <qw-card v-for="r in REMINDERS" :key="r.title">
          <div class="gd-rm" :class="r.tone"><qw-icon :name="r.icon" :size="22"/><div><b>{{ r.title }}</b><p class="text-2">{{ r.text }}</p></div></div>
        </qw-card>
      </div>
    </div>`,
  });
})();
