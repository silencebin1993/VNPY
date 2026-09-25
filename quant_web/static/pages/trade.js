/* 交易 #/trade：账户（模拟盘 / 实盘）→ 持仓与交易计划、委托、成交 → 下单（风控检查 → 确认）→ 明日计划（条件单清单）→ 提醒 → 复盘 → 交易设置（风控 / 实盘 / 推送 / 盘中监控 / 一键停止） */
(function () {
  "use strict";
  const { ref, reactive, computed, onMounted, watch } = Vue;
  const { api, fmt, store, toast, isNum } = QW;

  const LS_KEY = "qw.trade.account";
  const OPEN = ["submitted", "waiting_trigger", "pending_manual", "partial", "queued"];
  const STATUS = {
    submitted: ["blue", "已报，等成交"], waiting_trigger: ["blue", "条件单，等触发"], pending_manual: ["warn", "请到券商下单"],
    partial: ["warn", "部分成交"], queued: ["blue", "排队，开盘自动提交"], filled: ["gray", "已成交"], cancelled: ["gray", "已撤"],
    expired: ["gray", "已过期"], rejected: ["red", "被拒绝"],
  };
  const KIND_TEXT = { limit: "限价", market: "市价", stop: "止损条件单", take_profit: "止盈条件单" };
  const LEVEL = { block: ["red", "禁止"], confirm: ["warn", "需要你确认"], warn: ["warn", "提醒"], info: ["gray", "说明"] };
  const ALERT_LV = { urgent: ["red", "紧急"], warn: ["warn", "重要"], info: ["gray", "一般"] };
  const TONE_TAG = { bad: "green", good: "red", watch: "warn", neutral: "gray" };
  const STAGE_TAG = { accumulation: "warn", washout: "warn", markup: "red", distribution: "green", decline: "green", unclear: "gray" };
  const STAGE_LABEL = { accumulation: "吸筹", washout: "洗盘", markup: "拉升", distribution: "出货", decline: "下跌", unclear: "不明确" };
  const RISK_TAG = { red: ["red", "红灯：建议回避"], yellow: ["warn", "黄灯：需要注意"], green: ["green", "未发现明显风险"] };
  const pct = (v, d = 2) => (isNum(v) ? fmt.ratio(v, d, true) : "—");
  const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
  const readLS = () => { try { return localStorage.getItem(LS_KEY); } catch (e) { return null; } };
  const writeLS = (v) => { try { if (v) localStorage.setItem(LS_KEY, v); } catch (e) { /* 浏览器不让存就算了 */ } };

  // 交割单文件：券商导出的多是 GBK 编码，先按 UTF-8 严格解码，失败再按 GBK
  const readText = (file) => file.arrayBuffer().then((buf) => {
    try { return new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (e) { return new TextDecoder("gbk").decode(buf); }
  });

  QW.page("trade", {
    props: ["params", "query"],
    setup(props) {
      const tab = ref("overview");
      const meta = ref(null);                 // 券商接口、移动止盈规则、实盘开关、盘中监控状态
      const accounts = ref([]);
      const accId = ref(null);
      const detail = ref(null);
      const loading = ref(true);
      const accLoading = ref(false);
      const err = ref("");
      const unread = ref(0);

      const account = computed(() => accounts.value.find((a) => a.id === accId.value) || null);
      const brokerOf = (name) => ((meta.value && meta.value.brokers) || []).find((b) => b.name === name) || { label: name, can_auto: false };
      const liveOn = computed(() => !!(meta.value && meta.value.live && meta.value.live.enabled));
      const hasLive = computed(() => accounts.value.some((a) => a.kind === "live"));

      const loadMeta = async () => { try { meta.value = await api.get("/api/trading/brokers", null, { silent: true }); } catch (e) { /* 下面会提示 */ } };
      const loadAccounts = async () => {
        try {
          accounts.value = await api.get("/api/trading/accounts", null, { silent: true });
          err.value = "";
          if (!accounts.value.some((a) => a.id === accId.value)) {
            const saved = readLS();
            accId.value = (accounts.value.find((a) => a.id === saved) || accounts.value[0] || {}).id || null;
          }
        } catch (e) { err.value = e.detail || e.message; }
      };
      const loadDetail = async (refresh = true) => {
        if (!accId.value) { detail.value = null; return; }
        accLoading.value = true;
        try { detail.value = await api.get("/api/trading/accounts/" + accId.value, { refresh }, { silent: true }); } catch (e) { toast.error(e.detail || e.message); } finally { accLoading.value = false; }
      };
      const loadUnread = async () => { try { unread.value = (await api.get("/api/alerts", { unread: true, limit: 1 }, { silent: true })).unread; } catch (e) { /* 忽略 */ } };
      const refreshAll = async () => { await loadAccounts(); await loadDetail(); loadUnread(); };
      watch(accId, (v) => { writeLS(v); loadDetail(); if (tab.value === "review") loadReview(); });
      onMounted(async () => {
        await Promise.all([loadMeta(), loadAccounts()]);
        loading.value = false;
        loadUnread();
        applyQuery(props.query);
      });
      QW.usePoll(() => { if (tab.value === "overview" && accId.value && store.status && store.status.market_open) loadDetail(); }, 30000);

      // ---------------------------------------------------------- 账户
      const newOpen = ref(false);
      const newAcc = reactive({ name: "", kind: "paper", broker: "paper", initial_cash: 100000 });
      watch(() => newAcc.kind, (k) => { newAcc.broker = k === "paper" ? "paper" : "manual"; });
      const openNew = () => { newAcc.name = ""; newAcc.kind = "paper"; newAcc.initial_cash = (store.profile && store.profile.capital) || 100000; newOpen.value = true; };
      const createAccount = async () => {
        try {
          const a = await api.post("/api/trading/accounts", { ...newAcc, name: newAcc.name.trim() || (newAcc.kind === "paper" ? "模拟练习" : "我的实盘") });
          newOpen.value = false;
          toast.success("账户已建好");
          await loadAccounts();
          accId.value = a.id;
        } catch (e) { /* 已提示 */ }
      };
      const resetAcc = async () => {
        if (!confirm("清空这个模拟账户的持仓、委托、交易计划和复盘记录，资金恢复到初始金额？")) return;
        try { await api.post(`/api/trading/accounts/${accId.value}/reset`); toast.success("已重置"); refreshAll(); } catch (e) { /* 已提示 */ }
      };
      const archiveAcc = async () => {
        if (!confirm(`删除账户“${account.value.name}”？记录会留在数据库里，但页面上不再显示。`)) return;
        try { await api.del("/api/trading/accounts/" + accId.value); toast.success("已删除"); accId.value = null; await loadAccounts(); } catch (e) { /* 已提示 */ }
      };

      const planOf = (id) => ((detail.value && detail.value.plans) || []).find((p) => p.id === id) || null;
      const openOrders = computed(() => ((detail.value && detail.value.orders) || []).filter((o) => OPEN.includes(o.status)));
      const doneOrders = computed(() => ((detail.value && detail.value.orders) || []).filter((o) => !OPEN.includes(o.status)).slice(0, 30));
      const showDone = ref(false);
      // 实盘账户的止损条件单只在程序里（程序开着时盯价格），券商那边没有——要写清楚，免得以为券商会帮你止损
      const statusText = (o) => (account.value && account.value.kind === "live" && o.status === "waiting_trigger"
        ? "程序盯着（电脑开着才有效）" : (STATUS[o.status] || [0, o.status])[1]);
      const cancelOrder = async (o) => {
        const tip = o.status === "pending_manual" ? "（如果你已经在券商下了这笔单，也要去券商那边撤掉）" : "";
        if (!confirm(`撤销 ${o.name || o.code} 的${o.side === "buy" ? "买入" : "卖出"}委托？${tip}`)) return;
        try { await api.post(`/api/trading/orders/${o.id}/cancel`, { account_id: accId.value }); toast.success("已撤单"); refreshAll(); } catch (e) { /* 已提示 */ }
      };

      // 手动实盘：确认成交 / 导入交割单
      const fillOpen = ref(false);
      const fillForm = reactive({ order: null, qty: 0, price: 0 });
      const openFill = (o) => { fillForm.order = o; fillForm.qty = o.qty - (o.filled_qty || 0); fillForm.price = o.price || o.trigger || 0; fillOpen.value = true; };
      const confirmFill = async () => {
        try {
          await api.post(`/api/trading/orders/${fillForm.order.id}/fill`, { account_id: accId.value, qty: Number(fillForm.qty), price: Number(fillForm.price) });
          fillOpen.value = false; toast.success("已记账"); refreshAll();
        } catch (e) { /* 已提示 */ }
      };
      const impOpen = ref(false);
      const impText = ref("");
      const impResult = ref(null);
      const onImpFile = async (ev) => {
        const f = ev.target.files && ev.target.files[0];
        if (!f) return;
        if (f.size > 5e6) { toast.error("文件太大了"); return; }
        impText.value = await readText(f);
      };
      const doImport = async () => {
        try {
          impResult.value = await api.post("/api/trading/import", { account_id: accId.value, text: impText.value });
          toast.success(`导入完成：新增 ${impResult.value.added} 笔`);
          refreshAll();
        } catch (e) { /* 已提示 */ }
      };

      // 交易计划
      const planOpen = ref(false);
      const planForm = reactive({ plan: null, stop: null, target: null, trail: "none", note: "", allow_lower: false });
      const openPlan = (p) => { Object.assign(planForm, { plan: p, stop: p.stop, target: p.target, trail: p.trail || "none", note: p.note || "", allow_lower: false }); planOpen.value = true; };
      const lowering = computed(() => planForm.plan && isNum(Number(planForm.stop)) && Number(planForm.stop) < planForm.plan.stop);
      const savePlan = async () => {
        const p = planForm.plan;
        const body = { account_id: accId.value, trail: planForm.trail, note: planForm.note, allow_lower: planForm.allow_lower };
        if (Number(planForm.stop) !== p.stop) body.stop = Number(planForm.stop);
        if (planForm.target !== "" && planForm.target != null && Number(planForm.target) !== p.target) body.target = Number(planForm.target);
        try { await api.put("/api/trading/plans/" + p.id, body); planOpen.value = false; toast.success("交易计划已更新"); loadDetail(false); } catch (e) { /* 已提示 */ }
      };

      const equityOption = computed(() => {
        const d = detail.value;
        if (!d || !d.equity || d.equity.length < 2) return null;
        const c = QW.colors();
        const init = d.account.initial_cash;
        return {
          animation: false, textStyle: { fontFamily: c.font }, grid: { left: 60, right: 12, top: 16, bottom: 24 },
          tooltip: { ...QW.tooltipBase(c), trigger: "axis", valueFormatter: (v) => fmt.money(v) },
          xAxis: { type: "category", data: d.equity.map((x) => x.date), ...QW.axisBase(c) },
          yAxis: { type: "value", scale: true, ...QW.axisBase(c), axisLabel: { ...QW.axisBase(c).axisLabel, formatter: (v) => fmt.money(v, 1) } },
          series: [{ name: "总资产", type: "line", showSymbol: false, data: d.equity.map((x) => +x.total.toFixed(2)), lineStyle: { width: 2, color: c.primary }, itemStyle: { color: c.primary },
            markLine: { symbol: "none", silent: true, label: { formatter: "初始资金", color: c.text3, fontSize: 10 }, lineStyle: { color: c.text3, type: "dashed" }, data: [{ yAxis: init }] } }],
        };
      });

      // ---------------------------------------------------------- 下单
      const ticket = reactive({ code: "", name: "", side: "buy", kind: "limit", price: null, trigger: null, qty: null, valid: "day",
        stop: null, target: null, trail: "breakeven", max_days: 20, reason: "" });
      const pv = ref(null);
      const pvStale = ref(false);
      const pvLoading = ref(false);
      const ack = ref(false);
      const submitting = ref(false);
      const lastResult = ref(null);
      const position = computed(() => ((detail.value && detail.value.positions) || []).find((p) => p.code === ticket.code) || null);
      const lot = computed(() => (ticket.code.startsWith("688") ? 200 : 100));
      const orderBody = () => {
        const o = { code: ticket.code, name: ticket.name || null, side: ticket.side, kind: ticket.kind, qty: Number(ticket.qty) || 0,
          price: ticket.kind === "market" ? null : Number(ticket.price) || null, trigger: ["stop", "take_profit"].includes(ticket.kind) ? Number(ticket.trigger) || null : null,
          valid: ticket.valid, reason: ticket.reason };
        const plan = ticket.side === "buy" ? { stop: Number(ticket.stop) || null, target: Number(ticket.target) || null, trail: ticket.trail,
          max_days: Number(ticket.max_days) || 20, reason: ticket.reason } : null;
        return { account_id: accId.value, order: o, plan };
      };
      watch(() => [ticket.side, ticket.kind, ticket.price, ticket.trigger, ticket.qty, ticket.stop, ticket.target, ticket.trail, ticket.max_days, ticket.valid, accId.value],
        () => { if (pv.value) pvStale.value = true; ack.value = false; });
      watch(() => ticket.side, (s) => {
        if (s === "buy" && ["stop", "take_profit"].includes(ticket.kind)) ticket.kind = "limit";
        if (s === "sell" && position.value && !ticket.qty) ticket.qty = position.value.available;
      });
      watch(() => ticket.kind, (k) => { ticket.valid = ["stop", "take_profit"].includes(k) ? "gtc" : "day"; });

      const runPreview = async (fillSuggest = false) => {
        if (!accId.value) { toast.error("先建一个账户"); return; }
        if (!ticket.code) { toast.error("先选股票"); return; }
        pvLoading.value = true;
        try {
          const body = orderBody();
          if (!body.order.qty) body.order.qty = lot.value;
          const r = await api.post("/api/trading/preview", body);
          pv.value = r; pvStale.value = false; ack.value = false;
          const q = r.quote || {};
          if (q.name && !ticket.name) ticket.name = q.name;
          if (fillSuggest) {
            if (!ticket.price && q.price) ticket.price = q.price;
            if (ticket.side === "buy" && r.suggest) {
              if (!ticket.stop) ticket.stop = r.suggest.stop;
              if (!ticket.target) ticket.target = r.suggest.target;
              if (!ticket.qty && r.suggest.shares) ticket.qty = r.suggest.shares;
            }
            if (ticket.side === "sell" && !ticket.qty && position.value) ticket.qty = position.value.available;
            pvStale.value = true;               // 自动填好的数值还要再检查一次
          }
        } catch (e) { pv.value = null; } finally { pvLoading.value = false; }
      };
      const pickStock = (it) => {
        Object.assign(ticket, { code: it.code, name: it.name || "", price: null, trigger: null, qty: null, stop: null, target: null, reason: "" });
        pv.value = null; lastResult.value = null;
        runPreview(true);
      };
      const useSuggest = () => { if (pv.value && pv.value.suggest) { ticket.qty = pv.value.suggest.shares; ticket.stop = ticket.stop || pv.value.suggest.stop; } };
      const canSubmit = computed(() => pv.value && !pvStale.value && !pvLoading.value && !pv.value.checks.blocked && (!pv.value.checks.need_confirm || ack.value) && !submitting.value);
      const submitOrder = async () => {
        if (!canSubmit.value) return;
        const b = orderBody();
        const side = b.order.side === "buy" ? "买入" : "卖出";
        if (!confirm(`确认${side} ${ticket.name || ticket.code} ${b.order.qty} 股${b.order.price ? "，价格 " + b.order.price : ""}？`)) return;
        submitting.value = true;
        try {
          const r = await api.post("/api/trading/orders", { ...b, acknowledge: ack.value });
          lastResult.value = r;
          const st = r.order.status;
          toast.success(st === "pending_manual" ? "已记下：请到券商 App 下这笔单，成交后回来点“我已成交”" : st === "queued" ? "已排队：下一个交易日 9:15 以后自动提交" : "已下单");
          pv.value = null;
          refreshAll();
        } catch (e) { /* 已提示 */ } finally { submitting.value = false; }
      };
      const sellFrom = (p) => {
        Object.assign(ticket, { code: p.code, name: p.name || "", side: "sell", kind: "limit", price: p.last_price || null, trigger: null, qty: p.available, stop: null, target: null, reason: "" });
        pv.value = null; lastResult.value = null; tab.value = "order";
        runPreview(false);
      };
      const applyQuery = (q) => {
        if (!q) return;
        if (q.tab) tab.value = q.tab;
        if (!q.code) return;
        Object.assign(ticket, { code: String(q.code), name: q.name || "", side: q.side === "sell" ? "sell" : "buy", kind: "limit",
          price: q.price ? Number(q.price) : null, qty: q.qty ? Number(q.qty) : null, stop: q.stop ? Number(q.stop) : null,
          target: q.target ? Number(q.target) : null, trigger: null, reason: q.reason || "" });
        pv.value = null; lastResult.value = null;
        tab.value = "order";
        if (accId.value) runPreview(true);
      };
      watch(() => props.query, (q) => applyQuery(q));

      // ---------------------------------------------------------- 明日计划
      const nightly = ref(null);
      const nLoading = ref(false);
      const loadNightly = async (rebuild = false) => {
        nLoading.value = true;
        try { nightly.value = await api.get("/api/trading/nightly", { rebuild }, { silent: !rebuild }); } catch (e) { /* 已提示 */ } finally { nLoading.value = false; }
      };
      const copyText = async (text) => {
        try { await navigator.clipboard.writeText(text); toast.success("已复制"); } catch (e) { toast.error("复制失败，请手动选中文字复制"); }
      };
      const buyCandidate = (c) => {
        const q = new URLSearchParams({ code: c.code, name: c.name || "", side: "buy", stop: c.stop || "", qty: c.shares || "", reason: `选股方案：${c.scheme_name || c.scheme_id}` });
        QW.go("/trade?" + q.toString());
      };

      // ---------------------------------------------------------- 提醒
      const alerts = ref(null);
      const loadAlerts = async () => { try { alerts.value = await api.get("/api/alerts", { limit: 200 }, { silent: true }); unread.value = alerts.value.unread; } catch (e) { /* 忽略 */ } };
      const readAll = async () => { try { await api.post("/api/alerts/read", {}); await loadAlerts(); } catch (e) { /* 已提示 */ } };

      // ---------------------------------------------------------- 复盘
      const review = ref(null);
      const loadReview = async () => {
        if (!accId.value) return;
        try { review.value = await api.get("/api/trading/review/" + accId.value, null, { silent: true }); } catch (e) { review.value = null; }
      };

      // ---------------------------------------------------------- 交易设置
      const cfg = ref(null);
      const cfgDirty = reactive({ risk: false, live: false, notify: false, monitor: false });
      const vnpyText = ref("{}");
      const loadCfg = async () => {
        try {
          const s = await api.get("/api/settings", null, { silent: true });
          cfg.value = { risk: s.risk, live: s.live, notify: s.notify, monitor: s.monitor, profile: s.profile };
          vnpyText.value = JSON.stringify(s.live.vnpy_setting || {}, null, 2);
          Object.keys(cfgDirty).forEach((k) => { cfgDirty[k] = false; });
        } catch (e) { toast.error("读取设置失败：" + (e.detail || e.message)); }
      };
      ["risk", "live", "notify", "monitor"].forEach((k) => watch(() => cfg.value && cfg.value[k], (v, old) => { if (v && old) cfgDirty[k] = true; }, { deep: true }));
      const saveCfg = async (group) => {
        const body = { [group]: JSON.parse(JSON.stringify(cfg.value[group])) };
        if (group === "live") {
          try { body.live.vnpy_setting = JSON.parse(vnpyText.value || "{}"); } catch (e) { toast.error("vnpy 网关设置不是有效的 JSON"); return; }
        }
        try {
          const s = await api.put("/api/settings", body);
          cfg.value[group] = s[group];
          cfgDirty[group] = false;
          toast.success("已保存");
          if (group === "live" || group === "monitor") loadMeta();
        } catch (e) { /* 已提示 */ }
      };
      const toggleLive = async (on) => {
        if (on && !confirm("打开实盘总开关后，实盘账户就能下单了。\n\n· 买入永远要你确认；\n· 自动下单只限止损卖出（接口支持时）；\n· 所有实盘操作都会记进审计日志；\n· 随时可以点“一键停止”。\n\n确定打开？")) return;
        cfg.value.live.enabled = on;
        await saveCfg("live");
      };
      const kill = async () => {
        if (!confirm("一键停止：关闭实盘总开关，并撤掉所有实盘账户还没成交的委托。确定？")) return;
        try {
          const r = await api.post("/api/trading/kill", {});
          toast.success(`实盘已停止，撤单 ${r.cancelled} 笔` + (r.failed.length ? `，${r.failed.length} 笔没撤掉，请到券商检查` : ""));
          await Promise.all([loadMeta(), loadCfg()]);
          refreshAll();
        } catch (e) { /* 已提示 */ }
      };
      const toggleChannel = (ch) => {
        const arr = cfg.value.notify.channels;
        const i = arr.indexOf(ch);
        if (i >= 0) arr.splice(i, 1); else arr.push(ch);
      };
      const testChannel = async (ch) => {
        if (cfgDirty.notify) await saveCfg("notify");
        try {
          const r = await api.post("/api/notify/test", { channel: ch });
          (r.ok ? toast.success : toast.error)(r.message);
        } catch (e) { /* 已提示 */ }
      };
      const regimeCapPct = (k) => computed({
        get: () => Math.round(((cfg.value && cfg.value.risk.regime_caps[k]) || 0) * 100),
        set: (v) => { cfg.value.risk.regime_caps = { ...cfg.value.risk.regime_caps, [k]: Math.max(0, Math.min(100, Number(v) || 0)) / 100 }; },
      });
      const capStrong = regimeCapPct("strong");
      const capNeutral = regimeCapPct("neutral");
      const capWeak = regimeCapPct("weak");
      const pctField = (group, key, scale = 100) => computed({
        get: () => (cfg.value ? +(cfg.value[group][key] * scale).toFixed(2) : null),
        set: (v) => { cfg.value[group][key] = Number(v) / scale; },
      });
      const singlePct = pctField("risk", "max_single_pct");
      const dailyLossPct = pctField("risk", "daily_loss_limit");
      const stopPct = pctField("risk", "default_stop_pct");
      const minAmtWan = pctField("risk", "min_amount_20d", 1 / 1e4);

      watch(tab, (t) => {
        if (t === "nightly" && !nightly.value) loadNightly(false);
        if (t === "alerts") loadAlerts();
        if (t === "review") loadReview();
        if (t === "settings") { loadCfg(); loadMeta(); }
      });

      const tabs = computed(() => [
        { value: "overview", label: "账户与持仓", icon: "wallet" },
        { value: "order", label: "下单", icon: "edit" },
        { value: "nightly", label: "明日计划", icon: "calendar" },
        { value: "alerts", label: "提醒", icon: "bell", badge: unread.value || null, tone: "red" },
        { value: "review", label: "复盘", icon: "history" },
        { value: "settings", label: "交易设置", icon: "sliders" },
      ]);

      return {
        store, fmt, isNum, pct, cls, tab, tabs, meta, accounts, accId, account, detail, loading, accLoading, err, unread, brokerOf, liveOn, hasLive,
        refreshAll, loadDetail, newOpen, newAcc, openNew, createAccount, resetAcc, archiveAcc, planOf, openOrders, doneOrders, showDone, statusText, cancelOrder,
        fillOpen, fillForm, openFill, confirmFill, impOpen, impText, impResult, onImpFile, doImport, planOpen, planForm, openPlan, lowering, savePlan,
        equityOption, ticket, pv, pvStale, pvLoading, ack, submitting, lastResult, position, lot, runPreview, pickStock, useSuggest, canSubmit, submitOrder, sellFrom,
        nightly, nLoading, loadNightly, copyText, buyCandidate, alerts, loadAlerts, readAll, review, loadReview,
        cfg, cfgDirty, vnpyText, saveCfg, toggleLive, kill, toggleChannel, testChannel, capStrong, capNeutral, capWeak, singlePct, dailyLossPct, stopPct, minAmtWan,
        STATUS, KIND_TEXT, LEVEL, ALERT_LV, TONE_TAG, STAGE_TAG, STAGE_LABEL, RISK_TAG,
      };
    },
    template: `<div class="stack">
      <div class="td-bar">
        <div class="row td-acc">
          <qw-icon name="wallet" :size="18"/>
          <select v-if="accounts.length" v-model="accId" class="qw-input td-acc-sel" aria-label="选择账户">
            <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.kind === 'live' ? '【实盘】' : '【模拟】' }}{{ a.name }}</option>
          </select>
          <span v-else-if="!loading" class="muted">还没有账户</span>
          <button class="btn sm" @click="openNew"><qw-icon name="plus" :size="14"/>新建账户</button>
          <button v-if="accId" class="qw-iconbtn sm" @click="refreshAll" v-tip="'刷新'"><qw-icon name="refresh" :size="14"/></button>
        </div>
        <div class="row">
          <span v-if="meta" class="qw-tag" :class="liveOn ? 'red' : 'gray'" v-tip="'实盘总开关：关着时实盘账户不能下单（在“交易设置”里打开）'">实盘{{ liveOn ? '已打开' : '已关闭' }}</span>
          <span v-if="meta && meta.monitor" class="qw-tag" :class="meta.monitor.running ? 'blue' : 'gray'" v-tip="'盘中监控：程序开着时，交易时段每隔一会儿检查持仓、委托和止损'">盘中监控{{ meta.monitor.running ? '运行中' : '未运行' }}</span>
          <button v-if="liveOn || hasLive" class="btn sm td-kill" @click="kill"><qw-icon name="stop" :size="14"/>一键停止实盘</button>
        </div>
      </div>

      <qw-tabs v-model="tab" :items="tabs"/>
      <qw-empty v-if="err" icon="alert" title="交易账本读取不到" :desc="err"/>

      <!-- ====================================================== 账户与持仓 -->
      <template v-if="tab === 'overview' && !err">
        <qw-skeleton v-if="loading" :rows="6"/>
        <qw-card v-else-if="!accounts.length" title="先建一个账户" icon="wallet">
          <div class="gd-note"><qw-icon name="info" :size="15"/><span>建议先用<b>模拟盘</b>练一两个月：规则和真的一样（T+1、100 股一手、手续费、涨跌停买卖不了），亏的是假钱。
            练到能按计划止损、复盘里“守纪律”比例高了，再考虑实盘。</span></div>
          <template #footer><button class="btn primary" @click="openNew"><qw-icon name="plus" :size="14"/>新建账户</button></template>
        </qw-card>
        <template v-else-if="account">
          <div class="gd-note" :class="{warn: account.kind === 'live'}">
            <qw-icon :name="account.kind === 'live' ? 'alert' : 'info'" :size="15"/>
            <span v-if="account.kind === 'paper'">模拟盘：程序开着时盘中按实时价成交，没开时收盘后按当天行情补成交；规则和真实一样（T+1、一手、手续费、一字涨停买不进、跌停封死卖不出）。</span>
            <span v-else-if="account.broker === 'manual'">实盘-手动：程序<b>不会</b>替你下单。在这里下单后，请到券商 App 按提示下同样的单，成交后回来点“我已成交”，或者收盘后导入交割单。
              <b v-if="!liveOn">实盘总开关现在关着，这个账户下不了单。</b></span>
            <span v-else>实盘-{{ brokerOf(account.broker).label }}：买入永远要你确认；止损触发时{{ brokerOf(account.broker).can_auto ? '按“交易设置”里的规则自动卖出' : '会立即提醒你手动卖出' }}。电脑和券商软件要开着。
              <b v-if="!liveOn">实盘总开关现在关着，这个账户下不了单。</b></span>
          </div>
          <div class="g4 td-stats">
            <qw-stat label="总资产" :value="detail ? $fmt.money(detail.total) : '—'" :loading="accLoading && !detail" :sub="detail ? '初始 ' + $fmt.money(detail.account.initial_cash) : ''"/>
            <qw-stat label="累计盈亏" :value="detail ? $fmt.money(detail.pnl_total) : '—'" :tone="detail ? cls(detail.pnl_total) : ''" :sub="detail ? pct(detail.return) : ''" :loading="accLoading && !detail"/>
            <qw-stat label="可用现金" :value="detail ? $fmt.money(detail.available) : '—'" :sub="detail && detail.frozen ? '挂单冻结 ' + $fmt.money(detail.frozen) : ''" :loading="accLoading && !detail"/>
            <qw-stat label="持仓市值" :value="detail ? $fmt.money(detail.market_value) : '—'" :sub="detail && detail.total ? '仓位 ' + $fmt.ratio(detail.market_value / detail.total, 0) : ''" :loading="accLoading && !detail"/>
          </div>

          <qw-card title="持仓" icon="briefcase" :pad="false" :sub="detail ? detail.positions.length + ' 只' : ''">
            <template #extra>
              <button class="btn sm primary" @click="tab = 'order'"><qw-icon name="plus" :size="13"/>下单</button>
              <button v-if="account.broker === 'manual'" class="btn sm" @click="impOpen = true; impResult = null"><qw-icon name="upload" :size="13"/>导入交割单</button>
            </template>
            <qw-table v-if="detail && detail.positions.length" :rows="detail.positions" row-key="code" dense
              :columns="[{key:'name',label:'股票'},{key:'qty',label:'持有 / 可卖',align:'right',help:'A 股 T+1：今天买的明天才能卖'},{key:'cost',label:'成本 / 现价',align:'right'},{key:'pnl',label:'浮动盈亏',align:'right'},{key:'plan',label:'止损 / 目标',align:'right',help:'交易计划里的止损价和目标价；跌破止损要卖'},{key:'act',label:'',align:'right'}]">
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/><div class="muted" style="font-size:12px">{{ row.opened ? '买于 ' + row.opened : '' }}</div></template>
              <template #cell-qty="{row}"><span class="num">{{ row.qty }}</span><div class="muted num">{{ row.available }}</div></template>
              <template #cell-cost="{row}"><span class="num">{{ $fmt.price(row.cost, 3) }}</span><div class="num">{{ $fmt.price(row.last_price) }} <qw-price v-if="isNum(row.pct_today)" :value="row.pct_today" pct/></div></template>
              <template #cell-pnl="{row}"><span class="num" :class="cls(row.pnl)">{{ $fmt.money(row.pnl) }}</span><div class="num" :class="cls(row.pnl_pct)">{{ pct(row.pnl_pct) }}</div></template>
              <template #cell-plan="{row}">
                <template v-if="planOf(row.plan_id)"><div class="num" :class="{'down': row.last_price && row.last_price <= planOf(row.plan_id).stop}">止损 {{ $fmt.price(planOf(row.plan_id).stop) }}</div>
                  <div class="num muted">目标 {{ $fmt.price(planOf(row.plan_id).target) }}</div></template>
                <span v-else class="qw-tag warn" v-tip="'没有交易计划的持仓：不知道错了在哪认输。建议按“卖出”旁边的按钮补一个止损'">没有止损</span>
              </template>
              <template #cell-act="{row}"><span class="row" style="flex-wrap:nowrap;gap:4px">
                <button v-if="planOf(row.plan_id)" class="btn sm" @click="openPlan(planOf(row.plan_id))">改计划</button>
                <button class="btn sm" :disabled="!row.available" @click="sellFrom(row)" v-tip="row.available ? '' : '今天买的明天才能卖'">卖出</button>
                <a class="btn sm ghost" :href="'#/watch/' + row.code">诊断</a></span></template>
            </qw-table>
            <qw-empty v-else compact icon="briefcase" title="空仓" desc="没有合适的股票时空仓，也是一种操作。"/>
          </qw-card>

          <qw-card title="委托" icon="listCheck" :pad="false" :sub="openOrders.length ? openOrders.length + ' 笔没完成' : '没有未完成的委托'">
            <template #extra><button v-if="doneOrders.length" class="btn sm ghost" @click="showDone = !showDone">{{ showDone ? '只看没完成的' : '也看已完成的' }}</button></template>
            <qw-table v-if="openOrders.length || (showDone && doneOrders.length)" :rows="showDone ? openOrders.concat(doneOrders) : openOrders" row-key="id" dense
              :columns="[{key:'name',label:'股票'},{key:'side',label:'方向'},{key:'price',label:'价格',align:'right'},{key:'qty',label:'数量 / 成交',align:'right'},{key:'status',label:'状态'},{key:'created',label:'时间'},{key:'act',label:'',align:'right'}]">
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
              <template #cell-side="{row}"><span :class="row.side === 'buy' ? 'up' : 'down'">{{ row.side === 'buy' ? '买入' : '卖出' }}</span><div class="muted" style="font-size:12px">{{ KIND_TEXT[row.kind] || row.kind }}{{ row.valid === 'gtc' ? ' · 一直有效' : '' }}</div></template>
              <template #cell-price="{row}"><span class="num">{{ row.kind === 'market' ? '市价' : $fmt.price(row.price) }}</span><div v-if="row.trigger" class="muted num">触发 {{ $fmt.price(row.trigger) }}</div></template>
              <template #cell-qty="{row}"><span class="num">{{ row.qty }}</span><div class="muted num">{{ row.filled_qty || 0 }}{{ row.avg_price ? ' @ ' + $fmt.price(row.avg_price) : '' }}</div></template>
              <template #cell-status="{row}"><span class="qw-tag" :class="(STATUS[row.status] || ['gray'])[0]">{{ statusText(row) }}</span>
                <div v-if="row.message" class="muted" style="font-size:12px;max-width:220px;white-space:normal">{{ row.message }}</div></template>
              <template #cell-created="{row}"><span class="muted num" style="font-size:12px">{{ (row.created || '').slice(5, 16) }}</span></template>
              <template #cell-act="{row}"><span v-if="['submitted','waiting_trigger','pending_manual','partial','queued'].includes(row.status)" class="row" style="flex-wrap:nowrap;gap:4px">
                <button v-if="account.broker === 'manual' && ['pending_manual','partial'].includes(row.status)" class="btn sm primary" @click="openFill(row)">我已成交</button>
                <button class="btn sm ghost" @click="cancelOrder(row)">撤单</button></span></template>
            </qw-table>
          </qw-card>

          <div class="g2">
            <qw-card title="资产曲线" icon="trend" :sub="detail && detail.equity ? '每天收盘后记一次' : ''">
              <qw-chart v-if="equityOption" :option="equityOption" height="220px"/>
              <qw-empty v-else compact icon="trend" title="还没有资产记录" desc="每个交易日收盘后（每日更新时）自动记录一次。"/>
            </qw-card>
            <qw-card title="最近成交" icon="history" :pad="false">
              <div v-if="detail && detail.fills.length" class="hs-box"><div style="overflow-x:auto;max-height:260px" v-hscroll>
                <table class="mini-table"><thead><tr><th>日期</th><th>股票</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th></tr></thead>
                  <tbody><tr v-for="f in detail.fills" :key="f.id"><td class="num">{{ f.trade_date }}</td><td>{{ f.name || f.code }}</td>
                    <td :class="f.side === 'buy' ? 'up' : 'down'">{{ f.side === 'buy' ? '买' : '卖' }}</td><td class="num">{{ f.qty }}</td><td class="num">{{ $fmt.price(f.price) }}</td>
                    <td class="num">{{ (f.commission + f.stamp + f.transfer).toFixed(2) }}</td></tr></tbody></table>
              </div></div>
              <qw-empty v-else compact icon="history" title="还没有成交"/>
            </qw-card>
          </div>
          <div class="row" style="justify-content:flex-end">
            <button v-if="account.kind === 'paper'" class="btn sm ghost" @click="resetAcc">重置模拟账户</button>
            <button class="btn sm ghost" @click="archiveAcc"><qw-icon name="trash" :size="13"/>删除账户</button>
          </div>
        </template>
      </template>

      <!-- ====================================================== 下单 -->
      <template v-if="tab === 'order' && !err">
        <qw-card v-if="!accounts.length" title="先建一个账户" icon="wallet"><template #footer><button class="btn primary" @click="openNew">新建账户</button></template></qw-card>
        <div v-else class="td-order">
          <qw-card :title="account ? '下单 · ' + account.name : '下单'" icon="edit">
            <div class="td-form">
              <label><span>股票</span>
                <div class="row"><qw-stock-search :navigate="false" placeholder="输入代码/名称/拼音" @select="pickStock"/>
                  <b v-if="ticket.code">{{ ticket.name }} {{ ticket.code }}</b></div></label>
              <label><span>方向</span><qw-segmented v-model="ticket.side" :options="[{value:'buy',label:'买入'},{value:'sell',label:'卖出'}]"/></label>
              <label><span>委托方式</span>
                <select v-model="ticket.kind" class="qw-input">
                  <option value="limit">限价（推荐：价格不超过你填的）</option><option value="market">市价（按对手价尽快成交）</option>
                  <template v-if="ticket.side === 'sell'"><option value="stop">止损条件单（跌到触发价就卖）</option><option value="take_profit">止盈条件单（涨到触发价就卖）</option></template>
                </select></label>
              <label v-if="ticket.kind === 'limit'"><span>价格</span><input v-model.number="ticket.price" type="number" step="0.01" class="qw-input"></label>
              <label v-if="['stop','take_profit'].includes(ticket.kind)"><span>触发价</span><input v-model.number="ticket.trigger" type="number" step="0.01" class="qw-input"></label>
              <label><span>数量（股）</span>
                <div class="row" style="flex-wrap:nowrap"><input v-model.number="ticket.qty" type="number" :step="lot" min="0" class="qw-input">
                  <button v-if="ticket.side === 'buy' && pv && pv.suggest && pv.suggest.shares" class="btn sm" @click="useSuggest">用建议 {{ pv.suggest.shares }} 股</button>
                  <button v-if="ticket.side === 'sell' && position" class="btn sm" @click="ticket.qty = position.available">全部可卖 {{ position.available }}</button></div>
                <em class="hint">一手 {{ lot }} 股{{ position ? '；现在持有 ' + position.qty + ' 股，可卖 ' + position.available + ' 股' : '' }}</em></label>
              <template v-if="ticket.side === 'buy'">
                <label><span>止损价（必填）<qw-help text="跌到这个价就认错卖出。买之前冷静时定好，之后只许往上挪。默认按技术位置（20 日线、平均波幅）算，最多亏 8%。"/></span>
                  <input v-model.number="ticket.stop" type="number" step="0.01" class="qw-input">
                  <em v-if="pv && pv.suggest" class="hint">建议 {{ $fmt.price(pv.suggest.stop) }}（{{ pv.suggest.stop_basis }}）</em></label>
                <label><span>目标价（可选）</span><input v-model.number="ticket.target" type="number" step="0.01" class="qw-input">
                  <em class="hint">默认 = 买入价 + 2 倍止损幅度（赚 2 份风险）</em></label>
                <label><span>移动止盈</span>
                  <select v-model="ticket.trail" class="qw-input"><option v-for="(t, k) in (meta ? meta.trail_rules : {})" :key="k" :value="k">{{ t }}</option></select></label>
                <label><span>最长持有（交易日）</span><input v-model.number="ticket.max_days" type="number" min="1" max="250" class="qw-input"></label>
              </template>
              <label class="td-wide"><span>理由（复盘时看）</span><input v-model="ticket.reason" maxlength="100" class="qw-input" placeholder="例如：选股器“反转+低估值”第 2 名，缩量回踩 20 日线"></label>
            </div>
            <template #footer>
              <div class="row">
                <button class="btn" :disabled="pvLoading || !ticket.code" @click="runPreview(false)"><qw-icon name="shield" :size="14"/>{{ pvLoading ? '检查中…' : '检查风控' }}</button>
                <button class="btn primary" :disabled="!canSubmit" @click="submitOrder"><qw-icon name="check" :size="14"/>{{ submitting ? '提交中…' : '确认下单' }}</button>
                <span v-if="pvStale" class="muted" style="font-size:12px">改过内容，请重新“检查风控”</span>
              </div>
            </template>
          </qw-card>

          <div class="stack">
            <qw-card v-if="pv" title="风控检查" icon="shield" :sub="pv.session === 'open' || pv.session === 'auction' ? '交易时段' : '非交易时段'">
              <div v-if="pv.quote && pv.quote.price" class="td-quote">
                <b>{{ pv.quote.name || ticket.code }}</b><span class="num">现价 {{ $fmt.price(pv.quote.price) }}</span><qw-price :value="pv.quote.pct" pct/>
                <span class="muted num">涨停 {{ $fmt.price(pv.quote.limit_up) }} · 跌停 {{ $fmt.price(pv.quote.limit_down) }}</span>
              </div>
              <div v-if="pv.stage || pv.risk_level" class="row td-diag">
                <span v-if="pv.stage">主力阶段 <span class="qw-tag" :class="STAGE_TAG[pv.stage] || 'gray'">{{ STAGE_LABEL[pv.stage] || pv.stage }}</span></span>
                <span v-if="pv.risk_level">排雷 <span class="qw-tag" :class="(RISK_TAG[pv.risk_level] || ['gray'])[0]">{{ (RISK_TAG[pv.risk_level] || [0, pv.risk_level])[1] }}</span></span>
                <a class="muted" :href="'#/watch/' + ticket.code" style="font-size:12px">看完整诊断</a>
              </div>
              <div class="st-advice" :class="pv.checks.blocked ? 't-bad' : pv.checks.need_confirm ? 't-watch' : 't-neutral'">
                <qw-icon :name="pv.checks.blocked ? 'xCircle' : pv.checks.need_confirm ? 'alert' : 'checkCircle'" :size="15"/>
                <span>{{ pv.checks.blocked ? '有“禁止”项，这笔单不能下。' : pv.checks.need_confirm ? '有需要你确认的风险，看清楚再决定。' : '检查通过。' }}</span></div>
              <ul class="td-checks">
                <li v-for="(it, i) in pv.checks.items" :key="i"><span class="qw-tag" :class="LEVEL[it.level][0]">{{ LEVEL[it.level][1] }}</span><span>{{ it.message }}</span></li>
              </ul>
              <div v-if="pv.suggest" class="td-suggest">
                <div>按“一笔最多亏总资金的 {{ store.profile ? (store.profile.risk_per_trade * 100).toFixed(1) : '1' }}%”算：建议 <b>{{ pv.suggest.shares }}</b> 股（约 {{ $fmt.money(pv.suggest.amount) }}），
                  跌到止损最多亏约 {{ $fmt.money(pv.suggest.risk_amount) }}。</div>
                <div v-if="pv.suggest.note" class="muted">{{ pv.suggest.note }}</div>
              </div>
              <p v-for="w in pv.warnings" :key="w" class="muted" style="font-size:12px">注意：{{ w }}（相关检查跳过了）</p>
              <label v-if="pv.checks.need_confirm && !pv.checks.blocked" class="td-ack"><input type="checkbox" v-model="ack">我已经看清上面的风险，仍然要下这笔单（会记进复盘的“人性陷阱”统计）</label>
            </qw-card>
            <qw-card v-else title="下单流程" icon="info">
              <ol class="td-steps">
                <li>搜股票 → 程序自动填好现价、建议止损、按风险算好的股数；</li>
                <li>点“检查风控”：板块权限、止损、仓位、追高、主力出货、排雷、当天亏损、冷静期……逐条检查；</li>
                <li>有“禁止”项不能下；有“需要确认”项要勾选确认；</li>
                <li>点“确认下单”。买入会自动建一份交易计划（止损、目标、移动止盈），之后每天自动跟踪。</li>
              </ol>
            </qw-card>
            <qw-card v-if="lastResult" title="刚才的委托" icon="checkCircle">
              <p><span class="qw-tag" :class="(STATUS[lastResult.order.status] || ['gray'])[0]">{{ (STATUS[lastResult.order.status] || [0, lastResult.order.status])[1] }}</span>
                {{ lastResult.order.side === 'buy' ? '买入' : '卖出' }} {{ lastResult.order.name || lastResult.order.code }} {{ lastResult.order.qty }} 股</p>
              <p v-if="lastResult.order.status === 'pending_manual'" class="text-2">请现在打开券商 App，按同样的价格和数量下单；成交后回到“账户与持仓”点“我已成交”。
                <b>别忘了在券商 App 里设止损条件单（价格见“明日计划”的条件单清单）。</b></p>
              <p v-if="lastResult.plan" class="muted">已建交易计划：止损 {{ $fmt.price(lastResult.plan.stop) }}，目标 {{ $fmt.price(lastResult.plan.target) }}。</p>
            </qw-card>
          </div>
        </div>
      </template>

      <!-- ====================================================== 明日计划 -->
      <template v-if="tab === 'nightly' && !err">
        <qw-card title="明日计划" icon="calendar" :sub="nightly ? '基于 ' + nightly.date + ' 收盘 · 生成于 ' + nightly.generated_at : ''">
          <template #extra><button class="btn sm" :disabled="nLoading" @click="loadNightly(true)"><qw-icon name="refresh" :size="13"/>{{ nLoading ? '生成中…' : '重新生成' }}</button></template>
          <qw-skeleton v-if="nLoading && !nightly" :rows="5"/>
          <template v-else-if="nightly">
            <div v-if="nightly.regime" class="sc-regime" :class="'t-' + nightly.regime.tone"><qw-icon name="gauge" :size="16"/>
              <span>大盘环境：<b>{{ nightly.regime.label }}</b>，建议总仓位不超过 <b>{{ $fmt.ratio(nightly.regime.cap, 0) }}</b>。{{ nightly.regime.advice }}</span></div>
            <p class="muted" style="font-size:12.5px;margin:8px 0 0">每个交易日收盘、每日更新完成后自动生成并推送；晚上看一眼，明天照着做。</p>
          </template>
          <qw-empty v-else compact icon="calendar" title="还没有明日计划" desc="点“重新生成”，或等今天收盘后的每日更新。"/>
        </qw-card>
        <template v-if="nightly">
          <qw-card v-for="a in nightly.accounts" :key="a.id" :title="(a.kind === 'live' ? '【实盘】' : '【模拟】') + a.name" icon="briefcase" :pad="false"
            :sub="'总资产 ' + $fmt.money(a.total) + ' · 仓位 ' + $fmt.ratio(a.exposure, 0)">
            <qw-table v-if="a.positions.length" :rows="a.positions" row-key="code" dense
              :columns="[{key:'name',label:'股票'},{key:'close',label:'收盘 / 盈亏',align:'right'},{key:'plan',label:'止损 / 目标',align:'right'},{key:'stage',label:'主力阶段'},{key:'action',label:'明天怎么做'},{key:'reasons',label:'原因',minWidth:'200px'}]">
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/><div class="muted num" style="font-size:12px">{{ row.qty }} 股 · 持有 {{ row.held_days ?? '—' }} 天</div></template>
              <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span><div class="num" :class="cls(row.pnl_pct)">{{ pct(row.pnl_pct) }}</div></template>
              <template #cell-plan="{row}"><span class="num">{{ $fmt.price(row.stop) }}</span><div class="muted num">{{ $fmt.price(row.target) }}</div></template>
              <template #cell-stage="{row}"><span v-if="row.stage" class="qw-tag" :class="STAGE_TAG[row.stage] || 'gray'">{{ row.stage_label }}</span><span v-else class="muted">—</span></template>
              <template #cell-action="{row}"><span class="qw-tag" :class="TONE_TAG[row.tone] || 'gray'">{{ row.action }}</span></template>
              <template #cell-reasons="{row}"><span class="sc-reason">{{ row.reasons.join('；') || '—' }}</span></template>
            </qw-table>
            <qw-empty v-else compact icon="briefcase" title="空仓"/>
            <div v-if="a.conditional && a.conditional.length" class="td-cond">
              <div class="row" style="justify-content:space-between"><b>券商 App 条件单清单</b>
                <button class="btn sm" @click="copyText(a.conditional.map((x) => x.text).join('\\n'))"><qw-icon name="clipboard" :size="13"/>全部复制</button></div>
              <p class="muted" style="font-size:12px;margin:4px 0 8px">在券商 App 的“条件单 / 止盈止损”里照着设好——电脑关着也能止损（双保险）。</p>
              <div v-for="(x, i) in a.conditional" :key="i" class="td-cond-item"><span class="qw-tag" :class="x.type.startsWith('止损') ? 'green' : 'red'">{{ x.type }}</span><span>{{ x.text }}</span></div>
            </div>
          </qw-card>
          <qw-card title="候选买入" icon="filter" :pad="false" :sub="nightly.candidates.length ? '来自每天自动运行的选股方案' : ''">
            <qw-table v-if="nightly.candidates.length" :rows="nightly.candidates" :row-key="(r) => r.scheme_id + r.code" dense
              :columns="[{key:'name',label:'股票'},{key:'scheme',label:'方案'},{key:'close',label:'收盘',align:'right'},{key:'plan',label:'建议止损 / 股数',align:'right'},{key:'reasons',label:'理由',minWidth:'200px'},{key:'act',label:'',align:'right'}]">
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
              <template #cell-scheme="{row}"><span class="muted">{{ row.scheme_name || row.scheme_id }}</span><div class="muted" style="font-size:12px">{{ row.result_date }}</div></template>
              <template #cell-close="{row}"><span class="num">{{ $fmt.price(row.close) }}</span></template>
              <template #cell-plan="{row}"><span class="num">{{ $fmt.price(row.stop) }}</span><div class="num">{{ row.shares }} 股</div></template>
              <template #cell-reasons="{row}"><span class="sc-reason">{{ row.reasons || '—' }}</span></template>
              <template #cell-act="{row}"><span class="row" style="flex-wrap:nowrap;gap:4px"><a class="btn sm ghost" :href="'#/watch/' + row.code">诊断</a>
                <button class="btn sm primary" @click="buyCandidate(row)">去下单</button></span></template>
            </qw-table>
            <qw-empty v-else compact icon="filter" title="没有候选" desc="每天自动运行的选股方案今天没有选出股票，或者还没运行过。没有合适的就不买。"/>
            <p class="muted" style="font-size:12px;padding:8px 16px">{{ nightly.note }}</p>
          </qw-card>
        </template>
      </template>

      <!-- ====================================================== 提醒 -->
      <template v-if="tab === 'alerts' && !err">
        <qw-card title="提醒" icon="bell" :sub="alerts ? alerts.unread + ' 条未读' : ''" :pad="false">
          <template #extra><button v-if="alerts && alerts.unread" class="btn sm" @click="readAll">全部标为已读</button></template>
          <qw-skeleton v-if="!alerts" :rows="5" style="padding:12px"/>
          <div v-else-if="alerts.alerts.length" class="td-alerts">
            <div v-for="a in alerts.alerts" :key="a.id" class="td-alert" :class="{unread: !a.read}">
              <span class="qw-tag" :class="(ALERT_LV[a.level] || ['gray'])[0]">{{ (ALERT_LV[a.level] || [0, a.level])[1] }}</span>
              <div class="td-alert-main"><div><b>{{ a.title }}</b><a v-if="a.code" class="muted" :href="'#/watch/' + a.code" style="margin-left:6px">{{ a.code }}</a></div>
                <div v-if="a.body" class="text-2" style="white-space:pre-line">{{ a.body }}</div></div>
              <span class="muted num" style="font-size:12px;white-space:nowrap">{{ (a.time || '').slice(5, 16) }}</span>
            </div>
          </div>
          <qw-empty v-else compact icon="bell" title="没有提醒" desc="止损触发、委托成交、异动、明日计划等都会出现在这里；在“交易设置”里可以同时推送到微信或邮箱。"/>
        </qw-card>
      </template>

      <!-- ====================================================== 复盘 -->
      <template v-if="tab === 'review' && !err">
        <qw-card v-if="!account" title="复盘" icon="history"><qw-empty compact icon="history" title="先建一个账户"/></qw-card>
        <template v-else-if="review">
          <qw-card :title="'复盘 · ' + account.name" icon="history">
            <p v-if="!review.stats.n" class="muted">{{ review.stats.note }}</p>
            <template v-else>
              <div class="g4 td-stats">
                <qw-stat label="已平仓" :value="review.stats.n" unit="笔" flat/>
                <qw-stat label="胜率" :value="$fmt.ratio(review.stats.win_rate, 0)" flat help="赚钱的笔数 ÷ 总笔数。胜率高不代表赚钱：赢小亏大照样亏。"/>
                <qw-stat label="平均 R" :value="isNum(review.stats.avg_r) ? review.stats.avg_r.toFixed(2) : '—'" :tone="cls(review.stats.avg_r)" flat
                  help="R = 每笔赚的钱 ÷ 买入时计划的最大亏损。平均 R 大于 0 才是长期赚钱的方法；+2R 表示赚了计划风险的 2 倍。"/>
                <qw-stat label="总盈亏" :value="$fmt.money(review.stats.total_pnl)" :tone="cls(review.stats.total_pnl)" flat/>
                <qw-stat label="盈亏比" :value="isNum(review.stats.profit_factor) ? review.stats.profit_factor.toFixed(2) : '—'" flat help="所有赚的钱 ÷ 所有亏的钱。大于 1 才赚钱。"/>
                <qw-stat label="平均持有" :value="review.stats.avg_days.toFixed(1)" unit="天" flat/>
                <qw-stat label="最长连亏" :value="review.stats.max_losing_streak" unit="笔" flat/>
                <qw-stat label="守纪律" :value="$fmt.ratio(review.stats.discipline, 0)" flat help="没有任何违规（挪低止损、追高、补仓摊平、超期持有、无视风控）的交易占比。"/>
              </div>
              <p class="muted" style="font-size:12.5px;margin-top:10px">{{ review.stats.note }}</p>
            </template>
          </qw-card>
          <qw-card v-if="review.stats.traps && review.stats.traps.length" title="人性陷阱" icon="alert" sub="你最常犯的错">
            <div v-for="t in review.stats.traps" :key="t.name" class="td-trap"><b>{{ t.name }}</b><span class="qw-tag warn">{{ t.count }} 次</span><span class="text-2">{{ t.text }}</span></div>
          </qw-card>
          <qw-card v-if="review.trades.length" title="每笔交易" icon="listCheck" :pad="false">
            <qw-table :rows="review.trades" row-key="id" dense :page-size="30"
              :columns="[{key:'name',label:'股票'},{key:'dates',label:'买入 → 卖出'},{key:'px',label:'买价 / 卖价',align:'right'},{key:'pnl',label:'盈亏',align:'right',sortable:true},{key:'r_multiple',label:'R',align:'right',sortable:true},{key:'exit_reason',label:'离场原因'},{key:'violations',label:'违规'}]">
              <template #cell-name="{row}"><qw-stock :code="row.code" :name="row.name"/></template>
              <template #cell-dates="{row}"><span class="num" style="font-size:12px">{{ row.opened }} → {{ row.closed }}</span><div class="muted" style="font-size:12px">{{ row.days }} 天</div></template>
              <template #cell-px="{row}"><span class="num">{{ $fmt.price(row.entry, 3) }}</span><div class="num">{{ $fmt.price(row.exit, 3) }}</div></template>
              <template #cell-pnl="{row}"><span class="num" :class="cls(row.pnl)">{{ $fmt.money(row.pnl) }}</span></template>
              <template #cell-r_multiple="{row}"><span class="num" :class="cls(row.r_multiple)">{{ isNum(row.r_multiple) ? row.r_multiple.toFixed(2) : '—' }}</span></template>
              <template #cell-violations="{row}"><span v-for="v in row.violations" :key="v" class="qw-tag warn" style="margin-right:4px">{{ v }}</span><span v-if="!row.violations.length" class="muted">—</span></template>
            </qw-table>
          </qw-card>
        </template>
        <qw-skeleton v-else :rows="5"/>
      </template>

      <!-- ====================================================== 交易设置 -->
      <template v-if="tab === 'settings' && !err">
        <qw-skeleton v-if="!cfg" :rows="8"/>
        <template v-else>
          <qw-card title="实盘" icon="lock" :sub="cfg.live.enabled ? '总开关：已打开' : '总开关：关闭（实盘账户不能下单）'">
            <template #extra><button class="btn sm" :class="cfg.live.enabled ? '' : 'primary'" @click="toggleLive(!cfg.live.enabled)">{{ cfg.live.enabled ? '关闭实盘' : '打开实盘' }}</button></template>
            <div class="gd-note warn"><qw-icon name="alert" :size="15"/><span>安全规则（改不了）：<b>买入永远要你确认</b>；自动下单最多只做止损卖出；所有实盘操作写进只能追加的审计日志；右上角“一键停止”随时关掉实盘并撤单。</span></div>
            <div class="form-grid" style="margin-top:12px">
              <label><span>默认券商接口</span>
                <select v-model="cfg.live.broker" class="qw-input"><option v-for="b in (meta ? meta.brokers.filter((x) => x.is_live) : [])" :key="b.name" :value="b.name">{{ b.label }}{{ b.available ? '' : '（不可用）' }}</option></select>
                <em class="hint">{{ (brokerOf(cfg.live.broker).available ? brokerOf(cfg.live.broker).description : brokerOf(cfg.live.broker).reason) || '' }}</em></label>
              <label><span>止损自动卖出</span>
                <select v-model="cfg.live.auto_policy" class="qw-input"><option value="stop_only">止损触发时自动卖出（推荐）</option><option value="none">全部手动：只提醒，不自动下单</option></select>
                <em class="hint">只对能自动下单的接口（miniQMT 等）有效；手动账户永远只提醒。</em></label>
              <label><span>单笔最大金额（元）</span><input v-model.number="cfg.live.max_order_amount" type="number" min="0" step="1000" class="qw-input"><em class="hint">0 = 不限。超过的委托直接拒绝，防止手滑多打一个 0。</em></label>
              <label v-if="cfg.live.broker === 'qmt'"><span>miniQMT 目录（userdata_mini）</span><input v-model="cfg.live.qmt_path" class="qw-input" placeholder="例如 D:\\国金QMT\\userdata_mini"></label>
              <label v-if="cfg.live.broker === 'qmt'"><span>资金账号</span><input v-model="cfg.live.qmt_account" class="qw-input"></label>
              <label v-if="cfg.live.broker === 'easytrader'"><span>同花顺下单程序 xiadan.exe 路径</span><input v-model="cfg.live.easytrader_client" class="qw-input"></label>
              <label v-if="cfg.live.broker === 'vnpy_gateway'"><span>vnpy 网关类</span><input v-model="cfg.live.vnpy_gateway" class="qw-input" placeholder="例如 vnpy_xtp.XtpGateway"></label>
              <label v-if="cfg.live.broker === 'vnpy_gateway'" class="td-wide"><span>网关连接参数（JSON）</span><textarea v-model="vnpyText" rows="4" class="qw-input" spellcheck="false"></textarea></label>
            </div>
            <div v-if="meta" class="td-brokers">
              <div v-for="b in meta.brokers" :key="b.name" class="td-broker"><span class="qw-tag" :class="b.available ? 'blue' : 'gray'">{{ b.available ? '可用' : '不可用' }}</span>
                <b>{{ b.label }}</b><span class="muted">{{ b.available ? b.description : b.reason }}</span></div>
            </div>
            <template #footer><button class="btn primary" :disabled="!cfgDirty.live" @click="saveCfg('live')"><qw-icon name="save" :size="14"/>保存实盘设置</button></template>
          </qw-card>

          <qw-card title="纪律与风控" icon="shield" sub="每笔下单前都检查（模拟盘和实盘一样）">
            <div class="form-grid">
              <label><span>一笔最多亏总资金</span><div class="muted">{{ cfg.profile ? (cfg.profile.risk_per_trade * 100).toFixed(1) + '%' : '—' }}（在<a href="#/guide">新手指南 · 我的情况</a>里改）</div></label>
              <label><span>单只股票最多占总资金（%）</span><input v-model.number="singlePct" type="number" min="1" max="100" class="qw-input"></label>
              <label><span>默认止损幅度（%）</span><input v-model.number="stopPct" type="number" min="1" max="50" step="0.5" class="qw-input"><em class="hint">找不到更合适的技术位置时用</em></label>
              <label><span>当天亏损达到（%）就禁止再买</span><input v-model.number="dailyLossPct" type="number" min="0" max="20" step="0.5" class="qw-input"><em class="hint">0 = 不启用</em></label>
              <label><span>连亏几笔进入冷静期</span><input v-model.number="cfg.risk.cooldown_losses" type="number" min="0" max="20" class="qw-input"></label>
              <label><span>冷静期（交易日）</span><input v-model.number="cfg.risk.cooldown_days" type="number" min="0" max="30" class="qw-input"></label>
              <label><span>当天已涨超过（%）再买要确认</span><input v-model.number="cfg.risk.chase_warn_pct" type="number" min="0" max="30" step="0.5" class="qw-input"></label>
              <label><span>20 日平均成交额下限（万元）</span><input v-model.number="minAmtWan" type="number" min="0" step="500" class="qw-input"><em class="hint">太冷门的股票想卖时可能卖不掉</em></label>
              <label class="td-wide"><span>大盘环境 → 总仓位上限（%）</span>
                <div class="row td-caps"><span class="muted">强势</span><input v-model.number="capStrong" type="number" min="0" max="100" class="qw-input sc-num">
                  <span class="muted">震荡</span><input v-model.number="capNeutral" type="number" min="0" max="100" class="qw-input sc-num">
                  <span class="muted">弱势</span><input v-model.number="capWeak" type="number" min="0" max="100" class="qw-input sc-num"></div>
                <em class="hint">历史验证：强势之后并不比震荡涨得多，弱势之后波动更大，所以默认只在弱势时降仓。</em></label>
            </div>
            <div class="row" style="margin-top:12px">
              <label class="sc-chk"><input type="checkbox" v-model="cfg.risk.require_stop">买入必须设止损</label>
              <label class="sc-chk"><input type="checkbox" v-model="cfg.risk.block_distribution">疑似出货 / 下跌阶段禁止买入</label>
              <label class="sc-chk"><input type="checkbox" v-model="cfg.risk.block_risk_red">排雷红灯禁止买入</label>
              <label class="sc-chk"><input type="checkbox" v-model="cfg.risk.warn_average_down">亏损补仓时提醒</label>
            </div>
            <template #footer><button class="btn primary" :disabled="!cfgDirty.risk" @click="saveCfg('risk')"><qw-icon name="save" :size="14"/>保存风控设置</button></template>
          </qw-card>

          <qw-card title="提醒推送" icon="bell" sub="网页站内信一直有；微信 / 邮件要填 token 或邮箱">
            <div class="row">
              <label class="sc-chk"><input type="checkbox" checked disabled>网页站内信</label>
              <label class="sc-chk"><input type="checkbox" :checked="cfg.notify.channels.includes('pushplus')" @change="toggleChannel('pushplus')">微信 PushPlus</label>
              <label class="sc-chk"><input type="checkbox" :checked="cfg.notify.channels.includes('serverchan')" @change="toggleChannel('serverchan')">微信 Server酱</label>
              <label class="sc-chk"><input type="checkbox" :checked="cfg.notify.channels.includes('email')" @change="toggleChannel('email')">邮件</label>
            </div>
            <div class="form-grid" style="margin-top:12px">
              <label v-if="cfg.notify.channels.includes('pushplus')"><span>PushPlus token</span><div class="row" style="flex-wrap:nowrap"><input v-model="cfg.notify.pushplus_token" class="qw-input" type="password" autocomplete="off">
                <button class="btn sm" @click="testChannel('pushplus')">发送测试</button></div><em class="hint">在 pushplus.plus 微信扫码登录后获取</em></label>
              <label v-if="cfg.notify.channels.includes('serverchan')"><span>Server酱 SendKey</span><div class="row" style="flex-wrap:nowrap"><input v-model="cfg.notify.serverchan_key" class="qw-input" type="password" autocomplete="off">
                <button class="btn sm" @click="testChannel('serverchan')">发送测试</button></div></label>
              <template v-if="cfg.notify.channels.includes('email')">
                <label><span>SMTP 服务器</span><input v-model="cfg.notify.email_host" class="qw-input" placeholder="例如 smtp.qq.com"></label>
                <label><span>端口</span><input v-model.number="cfg.notify.email_port" type="number" class="qw-input"></label>
                <label><span>发件邮箱</span><input v-model="cfg.notify.email_user" class="qw-input" autocomplete="off"></label>
                <label><span>授权码 / 密码</span><input v-model="cfg.notify.email_password" type="password" class="qw-input" autocomplete="off"></label>
                <label><span>收件邮箱</span><div class="row" style="flex-wrap:nowrap"><input v-model="cfg.notify.email_to" class="qw-input" autocomplete="off"><button class="btn sm" @click="testChannel('email')">发送测试</button></div></label>
              </template>
              <label><span>推送到微信 / 邮件的级别</span>
                <select v-model="cfg.notify.min_level" class="qw-input"><option value="info">全部</option><option value="warn">重要和紧急（推荐）</option><option value="urgent">只推紧急（止损触发等）</option></select></label>
              <label><span>免打扰时段</span><div class="row" style="flex-wrap:nowrap"><input v-model="cfg.notify.quiet_start" class="qw-input sc-num" placeholder="22:30"> ~ <input v-model="cfg.notify.quiet_end" class="qw-input sc-num" placeholder="07:30"></div>
                <em class="hint">紧急提醒不受限制</em></label>
            </div>
            <template #footer><button class="btn primary" :disabled="!cfgDirty.notify" @click="saveCfg('notify')"><qw-icon name="save" :size="14"/>保存推送设置</button></template>
          </qw-card>

          <qw-card title="盘中监控" icon="eye" :sub="meta && meta.monitor ? (meta.monitor.running ? '运行中' : '未运行') + (meta.monitor.last_tick ? ' · 上次检查 ' + meta.monitor.last_tick : '') : ''">
            <div class="gd-note"><qw-icon name="info" :size="15"/><span>程序开着时，交易时段每隔一会儿检查：模拟盘撮合、实盘止损触发、开盘提交排队的委托、持仓和自选的异动。电脑关着时靠券商 App 里的条件单止损。</span></div>
            <div class="form-grid" style="margin-top:12px">
              <label><span>检查间隔（秒）</span><input v-model.number="cfg.monitor.interval_sec" type="number" min="10" max="600" class="qw-input"></label>
              <label><span>5 分钟内涨跌超过（%）提醒</span><input v-model.number="cfg.monitor.move_alert_pct" type="number" min="0.5" max="20" step="0.5" class="qw-input"></label>
            </div>
            <div class="row" style="margin-top:12px">
              <label class="sc-chk"><input type="checkbox" v-model="cfg.monitor.enabled">启用盘中监控</label>
              <label class="sc-chk"><input type="checkbox" v-model="cfg.monitor.watch_watchlist">自选股也做异动提醒</label>
            </div>
            <p v-if="meta && meta.monitor && meta.monitor.last_error" class="down" style="font-size:12px">上次出错：{{ meta.monitor.last_error }}</p>
            <template #footer><button class="btn primary" :disabled="!cfgDirty.monitor" @click="saveCfg('monitor')"><qw-icon name="save" :size="14"/>保存监控设置</button></template>
          </qw-card>
        </template>
      </template>

      <!-- ====================================================== 弹窗 -->
      <qw-modal v-model="newOpen" title="新建账户">
        <div class="form-grid">
          <label><span>类型</span><qw-segmented v-model="newAcc.kind" :options="[{value:'paper',label:'模拟盘（推荐先练）'},{value:'live',label:'实盘'}]"/></label>
          <label v-if="newAcc.kind === 'live'"><span>券商接口</span>
            <select v-model="newAcc.broker" class="qw-input"><option v-for="b in (meta ? meta.brokers.filter((x) => x.is_live) : [])" :key="b.name" :value="b.name">{{ b.label }}{{ b.available ? '' : '（不可用）' }}</option></select>
            <em class="hint">{{ brokerOf(newAcc.broker).available ? brokerOf(newAcc.broker).description : brokerOf(newAcc.broker).reason }}</em></label>
          <label><span>名字</span><input v-model="newAcc.name" maxlength="30" class="qw-input" :placeholder="newAcc.kind === 'paper' ? '模拟练习' : '我的实盘'"></label>
          <label><span>{{ newAcc.kind === 'paper' ? '模拟资金（元）' : '账户里的资金（元）' }}</span><input v-model.number="newAcc.initial_cash" type="number" min="1000" step="10000" class="qw-input"></label>
        </div>
        <p v-if="newAcc.kind === 'live'" class="gd-note warn" style="margin-top:12px"><qw-icon name="alert" :size="15"/><span>实盘账户在“交易设置”里打开实盘总开关之后才能下单。建议先用“实盘-手动”：程序只帮你算和记，下单你自己在券商 App 里点。</span></p>
        <template #footer><button class="btn" @click="newOpen = false">取消</button><button class="btn primary" @click="createAccount">建好</button></template>
      </qw-modal>

      <qw-modal v-model="fillOpen" title="确认成交（按券商 App 里的成交记录填）">
        <div v-if="fillForm.order" class="form-grid">
          <label><span>股票</span><div>{{ fillForm.order.name || fillForm.order.code }} · {{ fillForm.order.side === 'buy' ? '买入' : '卖出' }} {{ fillForm.order.qty }} 股</div></label>
          <label><span>成交数量（股）</span><input v-model.number="fillForm.qty" type="number" min="1" class="qw-input"><em class="hint">只成交了一部分就填实际成交的数量</em></label>
          <label><span>成交均价</span><input v-model.number="fillForm.price" type="number" step="0.001" class="qw-input"></label>
        </div>
        <template #footer><button class="btn" @click="fillOpen = false">取消</button><button class="btn primary" @click="confirmFill">确认记账</button></template>
      </qw-modal>

      <qw-modal v-model="impOpen" title="导入交割单" width="640px">
        <p class="text-2" style="line-height:1.7">从券商 App / 电脑版导出“当日成交”或“历史成交”（CSV 或 TXT，同花顺、通达信、东方财富的格式都能认），选文件或把内容粘贴到下面。
          同一笔成交重复导入不会重复记账。</p>
        <input type="file" accept=".csv,.txt,.xls" @change="onImpFile" style="margin:8px 0">
        <textarea v-model="impText" rows="8" class="qw-input" spellcheck="false" placeholder="成交日期,证券代码,证券名称,操作,成交数量,成交均价,成交编号…"></textarea>
        <p v-if="impResult" class="muted">识别 {{ impResult.rows }} 笔，新增 {{ impResult.added }} 笔，跳过（已导入过）{{ impResult.skipped }} 笔。</p>
        <template #footer><button class="btn" @click="impOpen = false">关闭</button><button class="btn primary" :disabled="!impText.trim()" @click="doImport">导入</button></template>
      </qw-modal>

      <qw-modal v-model="planOpen" title="修改交易计划">
        <div v-if="planForm.plan" class="form-grid">
          <label><span>止损价</span><input v-model.number="planForm.stop" type="number" step="0.01" class="qw-input">
            <em class="hint">原始止损 {{ $fmt.price(planForm.plan.initial_stop) }}，买入价 {{ $fmt.price(planForm.plan.entry) }}</em></label>
          <label><span>目标价</span><input v-model.number="planForm.target" type="number" step="0.01" class="qw-input"></label>
          <label><span>移动止盈</span><select v-model="planForm.trail" class="qw-input"><option v-for="(t, k) in (meta ? meta.trail_rules : {})" :key="k" :value="k">{{ t }}</option></select></label>
          <label class="td-wide"><span>备注</span><input v-model="planForm.note" maxlength="100" class="qw-input"></label>
        </div>
        <div v-if="lowering" class="gd-note warn" style="margin-top:12px"><qw-icon name="alert" :size="15"/>
          <span>你在<b>往下挪止损</b>。这是最常见的“小亏变大亏”：止损是买之前冷静时定的，跌下来再改，往往是不想认错。
            <label class="sc-chk" style="margin-top:6px"><input type="checkbox" v-model="planForm.allow_lower">我知道，仍然要改（会记进复盘的违规统计）</label></span></div>
        <template #footer><button class="btn" @click="planOpen = false">取消</button><button class="btn primary" :disabled="lowering && !planForm.allow_lower" @click="savePlan">保存</button></template>
      </qw-modal>
    </div>`,
  });
})();
