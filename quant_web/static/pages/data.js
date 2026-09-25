/* 数据中心 #/data：新手引导、数据状态、更新/训练/每日流程/ETF 行情按钮、自动更新开关、后台任务列表与日志 */
(function () {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount, nextTick } = Vue;
  const { api, fmt, store, bus, jobs, toast, usePoll, isNum } = QW;

  const POOL_NAMES = { zt: "涨停池", zb: "炸板池", dt: "跌停池", prev: "昨日涨停", strong: "强势股", sub_new: "次新股" };
  const TRAIN_OPTS = [{ value: "all", label: "全部" }, { value: "swing", label: "强势股波段" }, { value: "streak", label: "连板晋级" }, { value: "first", label: "首板潜力" }];
  const STATUS = {
    running: { label: "进行中", cls: "run", icon: "refresh" }, done: { label: "已完成", cls: "ok", icon: "checkCircle" },
    failed: { label: "失败", cls: "bad", icon: "xCircle" }, queued: { label: "排队中", cls: "wait", icon: "clock" },
  };
  const HELP = {
    panel: "全市场每只股票每天的开盘价、收盘价、成交量等，是一切分析和模型的基础。从 2019 年开始。",
    universe: "全部 A 股的代码、名称、板块、上市日期、行业。",
    fund: "上市公司的财报摘要（营收、利润、ROE 等），按“公布日期”使用，不会用到未来数据。",
    lhb: "龙虎榜：交易所公布的异常波动股票的买卖营业部数据。",
    pools: "东方财富的涨停池、炸板池等只能取到当天的数据，所以每天收盘后存档一次，存得越久越有用。",
    auto: "开启后，每个交易日收盘后自动下载当天数据、生成预测。需要保持“量化助手”程序开着（黑色窗口不要关）。",
  };
  const ACTIONS = [
    { key: "update", url: "/api/jobs/update", title: "更新数据", icon: "download", jobName: "update", jobTitle: "更新数据",
      desc: "下载最新的全市场日线、股票列表、涨停池、龙虎榜和财报。", time: "首次约 15~30 分钟，之后每天约 1~3 分钟" },
    { key: "train", url: "/api/jobs/train", title: "训练模型", icon: "cpu", jobName: "train", jobTitle: "训练模型",
      desc: "用最新数据重新学习一遍，并用样本外检验评估效果。数据有较大更新或模型超过一个月时建议重训。", time: "连板约 1~3 分钟，首板约 3~8 分钟，波段约 2~3 分钟；全部约 6~14 分钟" },
    { key: "daily", url: "/api/jobs/daily", title: "每日流程", icon: "sparkles", jobName: "daily", jobTitle: "一键更新（数据+模型+预测）",
      desc: "一步做完：更新数据 → 模型超过 30 天自动重训 → 生成今天的预测。和顶部“一键更新”按钮一样。", time: "通常 2~5 分钟" },
    { key: "etf", url: "/api/jobs/etf_update", title: "更新ETF行情", icon: "shield", jobName: "etf_update", jobTitle: "更新ETF行情",
      desc: "下载“稳健ETF”用到的十几只基金的最新行情。", time: "约 1 分钟" },
  ];

  QW.page("data", {
    props: ["params", "query"],
    setup() {
      const ds = ref(null);
      const dsLoading = ref(true);
      const dsErr = ref("");
      const jobList = ref([]);
      const jobsLoaded = ref(false);
      const openLogs = ref({});
      const fullLogs = ref({});
      const trainKind = ref("all");
      const autoUpdate = ref(null);
      const autoBusy = ref(false);
      const starting = ref({});
      const consoles = {};

      const loadStatus = async () => {
        try {
          ds.value = await api.get("/api/data/status", null, { silent: true });
          dsErr.value = "";
        } catch (e) {
          if (e.status === 404 && store.status) ds.value = { panel: store.status.panel, models: store.status.models };
          else dsErr.value = e.detail || e.message;
        } finally { dsLoading.value = false; }
      };
      const loadSettings = async () => {
        try {
          const s = await api.get("/api/settings", null, { silent: true });
          autoUpdate.value = !!s.auto_update;
        } catch (e) { autoUpdate.value = store.status ? !!store.status.auto_update : null; }
      };
      const loadLog = async (id) => {
        try {
          const j = await api.get("/api/jobs/" + encodeURIComponent(id), null, { silent: true });
          fullLogs.value = { ...fullLogs.value, [id]: j.logs || [] };
          await nextTick();
          const el = consoles[id];
          if (el) el.scrollTop = el.scrollHeight;
        } catch (e) { /* 任务可能已被清理 */ }
      };
      let tick = 0;
      const loadJobs = async (force) => {
        tick += 1;
        const anyRunning = jobList.value.some((j) => j.status === "running");
        if (!force && !anyRunning && tick % 8) return;
        try {
          const list = await api.get("/api/jobs", null, { silent: true });
          const before = new Set(jobList.value.filter((j) => j.status === "running").map((j) => j.id));
          jobList.value = Array.isArray(list) ? list : [];
          jobList.value.forEach((j) => { if (j.status === "running" && !store.jobs[j.id]) jobs.track(j.id, { title: j.title }); });
          if (jobList.value.some((j) => before.has(j.id) && j.status !== "running")) loadStatus();
          Object.keys(openLogs.value).forEach((id) => { if (openLogs.value[id]) { const j = jobList.value.find((x) => x.id === id); if (j && (j.status === "running" || !fullLogs.value[id])) loadLog(id); } });
        } catch (e) { /* 忽略 */ } finally { jobsLoaded.value = true; }
      };
      onMounted(() => { loadStatus(); loadSettings(); loadJobs(true); });
      usePoll(() => loadJobs(false), 2000, { live: false });
      const off1 = bus.on("job-done", () => { loadStatus(); loadJobs(true); });
      const off2 = bus.on("job-failed", () => loadJobs(true));
      onBeforeUnmount(() => { off1(); off2(); });

      const start = async (a) => {
        starting.value = { ...starting.value, [a.key]: true };
        try {
          const body = a.key === "train" ? { kind: trainKind.value } : {};
          const label = a.key === "train" ? `训练模型（${(TRAIN_OPTS.find((o) => o.value === trainKind.value) || {}).label}）` : a.jobTitle;
          const id = await jobs.start(a.url, body, label);
          if (id) { openLogs.value = { ...openLogs.value, [id]: true }; await loadJobs(true); }
        } catch (e) { /* 已提示 */ } finally {
          starting.value = { ...starting.value, [a.key]: false };
        }
      };
      const runningOf = (a) => jobList.value.find((j) => j.status === "running" && j.name === a.jobName) || null;
      const heavyBusy = computed(() => jobList.value.some((j) => j.status === "running" && ["update", "train", "daily"].includes(j.name)));
      const toggleLog = (j) => {
        const on = !openLogs.value[j.id];
        openLogs.value = { ...openLogs.value, [j.id]: on };
        if (on) loadLog(j.id);
      };
      const setConsole = (id) => (el) => { if (el) consoles[id] = el; else delete consoles[id]; };
      const setAuto = async (v) => {
        autoBusy.value = true;
        const old = autoUpdate.value;
        autoUpdate.value = v;
        try {
          const s = await api.put("/api/settings", { auto_update: v });
          autoUpdate.value = !!s.auto_update;
          if (store.status) store.status.auto_update = autoUpdate.value;
          toast.success(v ? "已开启自动更新：每个交易日收盘后自动下载数据并生成预测" : "已关闭自动更新，需要时请手动点“一键更新”");
        } catch (e) { autoUpdate.value = old; } finally { autoBusy.value = false; }
      };

      // 训练用时：模型 meta 里记了上次训练用了多久（train_seconds）时，在说明后面按实际写出来
      const actions = computed(() => ACTIONS.map((a) => {
        if (a.key !== "train") return a;
        const ms = models.value || {};
        const took = [["swing", "波段"], ["streak", "连板"], ["first", "首板"]]
          .filter(([k]) => ms[k] && isNum(ms[k].train_seconds) && ms[k].train_seconds > 0)
          .map(([k, l]) => `${l} ${fmt.num(Math.max(0.1, ms[k].train_seconds / 60), 1).replace(/\.0$/, "")} 分钟`);
        return took.length ? { ...a, time: `${a.time}（上次实际：${took.join("、")}）` } : a;
      }));
      const panel = computed(() => (ds.value && ds.value.panel) || null);
      const hasPanel = computed(() => !!(panel.value && panel.value.end && panel.value.rows));
      const models = computed(() => (ds.value && ds.value.models) || (store.status && store.status.models) || {});
      const hasModels = computed(() => !!(models.value.swing || models.value.streak || models.value.first));
      const scheduler = computed(() => (store.status && store.status.scheduler) || null);
      const guideSteps = computed(() => [
        { n: 1, title: "下载全市场行情", desc: "第一次使用要把 2019 年以来全部股票的日线下载到本机。", time: "约 15~30 分钟", done: hasPanel.value, action: ACTIONS[0] },
        { n: 2, title: "训练预测模型", desc: "让模型用这些历史数据学习“什么样的股票第二天容易涨停、强势股按规则买卖能不能赚钱”。", time: "约 6~14 分钟", done: hasModels.value, action: ACTIONS[1], locked: !hasPanel.value },
        { n: 3, title: "每天收盘后更新", desc: "之后每个交易日收盘后点一次“一键更新”，或者打开下面的自动更新。", time: "每天约 2~5 分钟", done: hasPanel.value && hasModels.value && !!autoUpdate.value, action: ACTIONS[2], locked: !hasModels.value },
      ]);
      const showGuide = computed(() => !dsLoading.value && (!hasPanel.value || !hasModels.value));

      const fileCards = computed(() => {
        const d = ds.value || {};
        const svc = d.service || {};
        const out = [];
        const u = d.universe || {};
        const su = svc.universe || {};
        out.push({ key: "universe", title: "股票列表", icon: "users", help: HELP.universe, ok: !!(u.exists || su.stocks),
          main: isNum(su.stocks) && su.stocks ? fmt.int(su.stocks) + " 只" : u.rows ? fmt.int(u.rows) + " 只" : "还没下载",
          sub: isNum(su.listed) && su.listed ? `其中在市 ${fmt.int(su.listed)} 只` : "", time: u.updated_at });
        const f = d.fundamentals || {};
        const sf = svc.fundamentals || {};
        out.push({ key: "fund", title: "财报数据", icon: "building", help: HELP.fund, ok: !!(f.exists || sf.rows),
          main: f.rows || sf.rows ? fmt.int(f.rows || sf.rows) + " 条" : "还没下载", sub: isNum(sf.stocks) && sf.stocks ? `覆盖 ${fmt.int(sf.stocks)} 家公司` : "", time: f.updated_at });
        const l = d.lhb || {};
        const sl = svc.lhb || {};
        out.push({ key: "lhb", title: "龙虎榜", icon: "flame", help: HELP.lhb, ok: !!(l.exists || sl.rows),
          main: l.rows || sl.rows ? fmt.int(l.rows || sl.rows) + " 条" : "还没下载", sub: sl.start ? `${sl.start} ~ ${sl.end}` : "", time: l.updated_at });
        const e = d.etf;
        if (e) out.push({ key: "etf", title: "ETF 行情", icon: "shield", ok: !!e.has_data, warn: e.has_data && !e.fresh,
          main: e.has_data ? (e.last_complete_day ? "到 " + fmt.cnDate(e.last_complete_day) : "已下载") : "还没下载", sub: e.has_data ? (e.fresh ? "已是最新" : "不是最新，可以更新") : "", time: e.updated_at });
        return out;
      });
      const pools = computed(() => {
        const p = (ds.value && ds.value.pools) || {};
        return Object.keys(p).map((k) => ({ k, name: POOL_NAMES[k] || k, ...p[k] }));
      });
      const modelCards = computed(() => [["swing", "强势股波段"], ["streak", "连板晋级"], ["first", "首板潜力"]].map(([k, label]) => {
        const m = models.value[k];
        return { k, label, m, stale: !!(m && m.stale), age: m ? m.age_days : null };
      }));

      const statusOf = (j) => STATUS[j.queued && j.status === "running" ? "queued" : j.status] || STATUS.running;
      const resultText = (j) => {
        const r = j.result;
        if (r == null) return "";
        if (typeof r === "string") return r;
        if (typeof r !== "object") return String(r);
        const parts = [];
        if (r.message) parts.push(r.message);
        if (r.last_date) parts.push("数据到 " + r.last_date);
        if (isNum(r.updated)) parts.push("更新 " + r.updated + " 只");
        if (Array.isArray(r.failed) && r.failed.length) parts.push("失败 " + r.failed.length + " 只");
        if (isNum(r.rows)) parts.push("新增 " + fmt.int(r.rows) + " 行");
        return parts.join("，");
      };
      const mmdd = (d) => {
        const t = String(d || "").replace(/-/g, "");
        return t.length >= 8 ? t.slice(4, 6) + "-" + t.slice(6, 8) : t || "—";
      };
      const elapsedText = (s) => {
        if (!isNum(s)) return "";
        if (s < 60) return Math.round(s) + " 秒";
        if (s < 3600) return Math.floor(s / 60) + " 分 " + Math.round(s % 60) + " 秒";
        return Math.floor(s / 3600) + " 小时 " + Math.round((s % 3600) / 60) + " 分";
      };

      return {
        store, fmt, HELP, ACTIONS, actions, TRAIN_OPTS, ds, dsLoading, dsErr, loadStatus, jobList, jobsLoaded, openLogs, fullLogs, trainKind, autoUpdate, autoBusy,
        starting, start, runningOf, heavyBusy, toggleLog, setConsole, setAuto, panel, hasPanel, models, scheduler, guideSteps, showGuide, fileCards, pools,
        modelCards, statusOf, resultText, elapsedText, loadJobs, mmdd,
      };
    },
    template: `<div class="stack data-page">
      <qw-card v-if="showGuide" class="guide-card">
        <div class="guide-hd"><qw-icon name="sparkles" :size="20"/><div><h2>第一次使用？跟着这三步走</h2><p class="muted">下载过程中可以随便浏览其他页面，进度会显示在顶部。</p></div></div>
        <ol class="guide-steps">
          <li v-for="s in guideSteps" :key="s.n" :class="{done: s.done, locked: s.locked && !s.done}">
            <span class="gs-n">{{ s.done ? '✓' : s.n }}</span>
            <div class="gs-body"><b>{{ s.title }}</b><p>{{ s.desc }}</p><small class="muted"><qw-icon name="clock" :size="12"/> {{ s.time }}</small></div>
            <button v-if="!s.done" class="btn" :class="{primary: !s.locked}" :disabled="s.locked || heavyBusy || starting[s.action.key]" @click="start(s.action)">{{ s.locked ? '先完成上一步' : runningOf(s.action) ? '进行中…' : '开始' }}</button>
          </li>
        </ol>
      </qw-card>

      <div class="data-top grid">
        <qw-card title="日线行情" icon="candle" :help="HELP.panel">
          <qw-skeleton v-if="dsLoading" :rows="3"/>
          <template v-else-if="hasPanel">
            <div class="dp-range num">{{ $fmt.date(panel.start) }} <span class="muted">~</span> {{ $fmt.date(panel.end) }}</div>
            <div class="kv section-gap">
              <div><div class="k">股票数</div><div class="v num">{{ $fmt.int(panel.stocks) }}</div></div>
              <div><div class="k">数据行数</div><div class="v num">{{ $fmt.int(panel.rows) }}</div></div>
              <div><div class="k">最后更新</div><div class="v num">{{ panel.updated_at ? $fmt.cnDate(panel.updated_at) + ' ' + $fmt.time(panel.updated_at) : '—' }}</div></div>
            </div>
          </template>
          <qw-empty v-else compact icon="download" title="还没有下载行情" desc="点上面的“开始”或下面的“更新数据”下载。"/>
          <p v-if="dsErr" class="err-note"><qw-icon name="alert" :size="13"/>{{ dsErr }}</p>
        </qw-card>
        <qw-card title="预测模型" icon="cpu">
          <template #extra><a class="linkbtn" href="#/model">模型中心 <qw-icon name="chevronRight" :size="14"/></a></template>
          <ul class="dm-list">
            <li v-for="c in modelCards" :key="c.k">
              <b>{{ c.label }}</b>
              <template v-if="c.m"><span class="muted num">{{ $fmt.date(c.m.trained_at) }} 训练</span><span class="qw-tag" :class="c.stale ? 'warn' : 'green'">{{ c.stale ? (c.age != null ? c.age + ' 天前 · 建议重训' : '建议重训') : '正常' }}</span></template>
              <template v-else><span class="muted">还没训练</span><span class="qw-tag warn">未训练</span></template>
            </li>
          </ul>
        </qw-card>
        <qw-card title="自动更新" icon="clock" :help="HELP.auto">
          <div class="auto-row">
            <button type="button" role="switch" class="qw-switch" :class="{on: autoUpdate}" :aria-checked="String(!!autoUpdate)" aria-label="自动更新" :disabled="autoBusy || autoUpdate === null" @click="setAuto(!autoUpdate)"><i></i></button>
            <div><b>{{ autoUpdate ? '已开启' : autoUpdate === false ? '已关闭' : '读取中…' }}</b>
              <p class="muted">交易日收盘后（北京时间 {{ scheduler && scheduler.run_after ? scheduler.run_after : '15:30' }} 以后）自动更新数据并生成预测。需要保持程序开着。</p></div>
          </div>
          <p v-if="scheduler && scheduler.message" class="auto-msg"><qw-icon name="info" :size="13"/>{{ scheduler.message }}</p>
        </qw-card>
      </div>

      <div class="data-files">
        <div v-for="f in fileCards" :key="f.key" class="df-item">
          <div class="df-hd"><qw-icon :name="f.icon" :size="15"/><span>{{ f.title }}</span><qw-help v-if="f.help" :text="f.help"/><i class="df-dot" :class="f.ok ? (f.warn ? 'warn' : 'ok') : 'no'"></i></div>
          <div class="df-main num">{{ f.main }}</div>
          <div class="df-sub muted">{{ f.sub }}</div>
          <div v-if="f.time" class="df-time muted num">更新于 {{ $fmt.datetime(f.time) }}</div>
        </div>
        <div class="df-item df-pools">
          <div class="df-hd"><qw-icon name="ladder" :size="15"/><span>涨停池存档</span><qw-help :text="HELP.pools"/><i class="df-dot" :class="pools.length ? 'ok' : 'no'"></i></div>
          <ul v-if="pools.length"><li v-for="p in pools" :key="p.k"><span>{{ p.name }}</span><b class="num">{{ p.days }} 天</b><small class="muted num">{{ mmdd(p.first) }} ~ {{ mmdd(p.last) }}</small></li></ul>
          <div v-else class="df-sub muted">还没有存档，每天更新数据时会自动存</div>
        </div>
      </div>

      <qw-card title="手动操作" icon="refresh" sub="同一时间只能运行一个大任务（更新/训练），可以离开这个页面，进度在顶部">
        <div class="act-list">
          <div v-for="a in actions" :key="a.key" class="act-item">
            <span class="act-ico"><qw-icon :name="a.icon" :size="20"/></span>
            <div class="act-body"><b>{{ a.title }}</b><p>{{ a.desc }}</p><small class="muted"><qw-icon name="clock" :size="12"/> {{ a.time }}</small></div>
            <div class="act-go">
              <qw-segmented v-if="a.key === 'train'" v-model="trainKind" :options="TRAIN_OPTS" size="sm"/>
              <button class="btn" :class="{primary: a.key === 'daily'}" :disabled="!!runningOf(a) || starting[a.key] || (a.key !== 'etf' && heavyBusy)" @click="start(a)">
                <qw-icon :name="runningOf(a) ? 'refresh' : a.icon" :size="15" :class="{spin: !!runningOf(a)}"/>{{ runningOf(a) ? '进行中 ' + Math.round((runningOf(a).progress || 0) * 100) + '%' : '开始' }}
              </button>
            </div>
          </div>
        </div>
      </qw-card>

      <qw-card title="后台任务" icon="history" :pad="false" :sub="jobList.length ? '最近 ' + jobList.length + ' 个，程序重启后清空' : ''">
        <template #extra><button class="btn sm" @click="loadJobs(true)"><qw-icon name="refresh" :size="14"/>刷新</button></template>
        <div v-if="!jobsLoaded" style="padding:0 16px 16px"><qw-skeleton :rows="3"/></div>
        <qw-empty v-else-if="!jobList.length" compact icon="inbox" title="还没有运行过任务" desc="点上面的按钮开始更新数据或训练模型，进度和日志会显示在这里。"/>
        <ul v-else class="job-list">
          <li v-for="j in jobList" :key="j.id" class="job-item" :class="statusOf(j).cls">
            <div class="ji-hd">
              <span class="ji-status"><qw-icon :name="statusOf(j).icon" :size="15" :class="{spin: j.status === 'running' && !j.queued}"/>{{ statusOf(j).label }}</span>
              <b class="ji-title">{{ j.title || j.name }}</b>
              <span class="muted num ji-time">{{ $fmt.datetime(j.started_at) }}<template v-if="elapsedText(j.elapsed)"> · 用时 {{ elapsedText(j.elapsed) }}</template></span>
              <span class="grow"></span>
              <button class="linkbtn" @click="toggleLog(j)"><qw-icon name="chevronDown" :size="14" :style="openLogs[j.id] ? '' : 'transform:rotate(-90deg)'"/>{{ openLogs[j.id] ? '收起日志' : '查看日志' }}</button>
            </div>
            <div v-if="j.status === 'running'" class="ji-prog"><div class="qw-progress"><i :style="{width: Math.round((j.progress || 0) * 100) + '%'}"></i></div><span class="num">{{ Math.round((j.progress || 0) * 100) }}%</span></div>
            <div class="ji-msg" :class="{up: j.status === 'failed'}">{{ j.status === 'failed' ? (j.error || j.message) : j.status === 'done' ? (resultText(j) || j.message) : j.message }}</div>
            <pre v-if="openLogs[j.id]" class="console" :ref="setConsole(j.id)">{{ ((fullLogs[j.id] || j.logs || []).join('\\n')) || '（暂时没有日志）' }}</pre>
          </li>
        </ul>
      </qw-card>
    </div>`,
  });
})();
