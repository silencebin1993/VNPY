/*
 * 量化助手 前端外壳：路由、API、共享组件、工具函数（Vue 3 全局构建 + ECharts 5，无打包）
 *
 * ── 全局命名空间 window.QW ─────────────────────────────────────────────
 *  QW.page(name, component)          注册页面（pages/*.js 调用）；name 对应 ROUTES 里的 name
 *                                    页面组件会收到 props: params（路径参数）、query（?后的参数）
 *  QW.api.get(path, params?, opts?) / post(path, body?, opts?) / put(path, body?, opts?) / del(path, body?, opts?)
 *                                    返回 JSON；出错时 toast 显示后端中文 detail 并抛 Error（err.status, err.detail）
 *                                    opts.silent=true 不弹 toast
 *  QW.toast.success|error|info|warn(text, {duration})
 *  QW.fmt   num(v,d) int(v) price(v,d) pct(v,d,sign=true)[v 为百分数 3.2→"+3.20%"]
 *           ratio(v,d=1,sign=false)[v 为小数 0.123→"12.3%"] frac(v,max)[把可能是百分数的值归一成小数]
 *           money(元) cap(亿) vol(股→手/万手) date md cnDate time industry(证监会行业→短名) board dir(v→up/down/flat)
 *           t(v)[t 值，一位小数；四舍五入会越过 2/1/-1 时改两位小数、不进位]
 *  QW.go(path, query?)  QW.route（reactive {name,path,params,query,title}）  QW.setTitle(text)
 *  QW.store  reactive：status phase dataDate theme isPhone isNarrow jobs{id:job} watchCodes offline clock
 *  QW.usePoll(fn, ms, {live=true, immediate=false})  只能在 setup() 里调用；live=true 时只在"交易中"轮询；页面隐藏时暂停
 *  QW.jobs.start(url, body, title) → job_id；QW.jobs.track(id, {title,onDone,onFail})；QW.jobs.running()
 *  QW.watch.refresh() / has(code) / toggle(code, name)
 *  QW.bus.on(evt, fn) → off；QW.bus.emit(evt, data)   事件：'job-done'(job) 'watch-changed' 'status'(status)
 *  QW.colors()  当前主题的图表配色 {up,down,text,text2,text3,grid,axis,surface,border,primary,series[6],bands[5]}
 *  QW.tooltipBase(c) / QW.axisBase(c)   ECharts 通用片段
 *  QW.LABELS 五维中文名；QW.DIM_HELP 五维白话解释；QW.ONE_WORD_TIP 一字板白话解释；QW.recent.add/list 最近看过
 *  QW.KINDS  三个预测名单 [{value:"swing"|"streak"|"first", label, icon, badge, tone("sim"|"watch"), role, desc}]（swing 在前 = 默认）
 *           QW.KIND_VALUES / kindInfo(k) / kindLabel(k)
 *  QW.normTrade(o)  把 meta.trade_oos / 回测返回 / 其中一段 统一成 {n,mean,median,win,t,fill,cagr,total,start,end,base{mean,win,...,same},match{同上},sel,hold,years[]}
 *                   （收益、胜率为小数；字段缺失为 null；base=天天随便挑，match=同日同数量随便挑）；QW.tWord(t) t 值白话；QW.T_HELP t 值解释
 *                   QW.BASE_HELP / MATCH_HELP 两种随机对比的白话；QW.TUNED_HELP 改过设置后历史成绩偏乐观的白话
 *                   t = daily_t 与 nw_t（Newey-West）里低的那个；QW.tHelp(norm) 有 nw_t 时在 T_HELP 后追加 T_NW_NOTE
 *                   QW.SKIP_HEAVY（0.2）：钱不够/一手太贵跳过的信号占比超过它时，显示 per_trade_uncapped（QW.UNCAPPED_HELP）
 *                   QW.recentVerdict(mean, rnd, t) 最近一段（留出期）对比“同一天随便挑”的一句话结论 {key,tone,text}；QW.holdLabel(o) “留出期（最近约 N 个月）”
 *                   QW.fillPct(v) 能买进的比例（不到 100% 不进位成 100%）；QW.HOLD_CAVEAT 留出期的如实说明
 *  模板里可用：$fmt  $go  $store  $labels
 *  单位约定（与后端一致）：pct/涨幅类为百分数（10.0 = 10%，用 fmt.pct）；prob/score/break_rate/win_rate 等为 0~1 小数（用 fmt.ratio）；
 *           行情 quote 的 float_cap/total_cap 单位元（fmt.money），预测行 float_cap 单位亿（fmt.cap）；volume 单位股（fmt.vol）
 *
 * ── 共享组件（模板里用 kebab-case）───────────────────────────────────
 *  <qw-icon name size>                                 内联 SVG 图标（名称见 ICONS）
 *  <qw-card title help sub icon :pad=true :loading>    卡片；插槽 default / extra（标题右侧）/ footer
 *  <qw-help text>                                      "?" 圆圈，悬停/点击显示一句话解释
 *  v-tip="文字"  v-tip.right="文字"                    任意元素的浮动提示指令
 *  <div class="hs-box"><div style="overflow-x:auto" v-hscroll>…</div></div>  宽表格：右侧渐隐 + 手机上“左右滑动查看”（qw-table 已内置）
 *  <qw-stat label value unit help :delta delta-text tone sub size(sm|lg) :loading to flat>
 *                                                      数字磁贴；delta>0 红▲ <0 绿▼；tone=up|down|flat 给数值上色；to=点击跳转
 *  <qw-table :columns :rows row-key :page-size :default-sort="{key,order}" clickable max-height :loading
 *            empty-text dense :row-class :active-key @row-click @sort-change>
 *            columns: [{key,label,sortable,align:'left'|'right'|'center',width,minWidth,help,format(v,row),cls(v,row),sortBy(row),sub(表头第二行小字),wrap(表头可换行)}]
 *            插槽 #cell-<key>="{row,value,index}"、#empty
 *  <qw-tabs v-model :items="[{value,label,badge,tone,icon}]">   标签页头（tone=sim|watch 给徽章上色）
 *  <qw-segmented v-model :options="[{value,label,icon}]" block size="sm">
 *  <qw-drawer v-model title width>                     右侧抽屉（手机上为底部弹出）；插槽 default / title / footer
 *  <qw-modal v-model title width :closable=true :mask-closable=true>   插槽 default / footer
 *  <qw-skeleton :rows height type="tiles">            骨架屏
 *  <qw-empty icon title desc action-text action-to compact @action>   空状态；插槽放更多按钮
 *  <qw-price :value :ref-value pct|ratio|change :digits arrow :color-by suffix>
 *                                                      红涨绿跌数字；pct=百分数、ratio=小数，自动带 +/- 号
 *  <qw-pctbar :value(0~1) :digits tone="heat">         概率条
 *  <qw-minibars :dims="{sentiment:0-100,...}" :keys :height>   五维小柱（50 为中性）
 *  <qw-stock code name :show-code=true inline>         股票名称链接 → 看盘
 *  <qw-board board>  <qw-streak :n :one-word>          板块标签 / 连板徽章
 *  <qw-chart :option height group :not-merge=true @ready @chart-click>   通用 ECharts 容器（自适应、卸载时销毁）
 *  <qw-kline :bars :limit-days height compact period :initial-bars=120>   K线+MA5/10/20/60+成交量+缩放+涨停标记
 *  <qw-minute :data="{prev_close,points}" height :limit-up :limit-down>   分时（价格+均价+成交量，昨收基准线）
 *  <qw-gauge :value(0~100) label height>               情绪温度仪表盘
 *  <qw-radar :dims :keys height :neutral=true>         五维雷达（虚线为 50 中性线）
 *  <qw-job :job-id title @done @failed>                后台任务进度（进度条+消息+日志）
 *  <qw-stock-search placeholder size="lg" autofocus :navigate=true @select>   股票搜索（自动补全）
 */
(function () {
  "use strict";
  const { createApp, reactive, ref, computed, watch, onMounted, onBeforeUnmount, nextTick, markRaw } = Vue;
  const QW = (window.QW = window.QW || {});
  QW.pages = QW.pages || {};
  QW.page = (name, comp) => { QW.pages[name] = comp; };

  // ------------------------------------------------------------------ 常量
  const LABELS = {
    sentiment: "情绪面", capital: "资金面", fundamental: "基本面", theme: "题材政策面", technical: "技术面", news: "消息面",
  };
  const DIM_HELP = {
    sentiment: "整个市场今天热不热、连板股多不多，以及这只股票自己的连板情况。",
    capital: "换手率、成交额、封板强度、龙虎榜等，看资金愿不愿意买它。",
    fundamental: "市盈率、业绩增长、负债等公司本身的质地。",
    theme: "所在行业今天有没有集体上涨，是不是市场热点。",
    technical: "近期涨幅、均线位置、股价处在近期高位还是低位等走势特征。",
    news: "最近24小时的新闻和政策热度（只有实时数据，没有参与模型训练和回测）。",
  };
  const BOARDS = { main: "主板", chinext: "创业板", star: "科创板", bj: "北交所" };
  const ONE_WORD_TIP = "一字板：一开盘就涨停、全天没打开过，K线像个“一”字。说明抢的人极多，第二天也常常一开盘就涨停——普通人挂单排队基本买不到。";
  // 三个预测名单：swing 模拟跟踪（还没证明能赚钱，只在模拟盘里跟踪），streak/first 只作观察（照着买历史上是亏的）
  const KINDS = [
    {
      value: "swing", label: "强势股波段", icon: "trend", badge: "模拟跟踪中", tone: "sim", role: "模拟跟踪",
      desc: "今天涨了 5% 以上、但没封住涨停的主板股，按“明天开盘买、按规则卖出”的预期收益排名——它不是涨停预测，也不保证赚钱，目前只在模拟盘里跟踪。",
    },
    {
      value: "streak", label: "连板晋级", icon: "ladder", badge: "观察", tone: "watch", role: "观察",
      desc: "今天涨停的股票里，谁明天更可能继续涨停——这是看热点、看懂连板梯队的观察名单，不是买入信号。",
    },
    {
      value: "first", label: "首板潜力", icon: "rocket", badge: "观察", tone: "watch", role: "观察",
      desc: "最近 5 天没涨停过的股票里，谁明天更可能第一次涨停——用来发现可能启动的股票，不是买入信号。",
    },
  ];
  const KIND_VALUES = KINDS.map((k) => k.value);
  const kindInfo = (k) => KINDS.find((x) => x.value === k) || null;
  const kindLabel = (k) => (kindInfo(k) || { label: k || "—" }).label;
  // 导航分组（电脑侧栏显示组标题；手机“更多”里按组排列）。route.group 对应这里的 key
  const NAV_GROUPS = [
    { key: "market", title: "行情" },
    { key: "pick", title: "选股" },
    { key: "strategy", title: "策略" },
    { key: "trade", title: "交易" },
    { key: "more", title: "更多" },
  ];
  const ROUTES = [
    { name: "dashboard", path: "/", title: "市场情绪", icon: "pulse", tab: true, group: "market" },
    { name: "lianban", path: "/lianban", title: "连板", icon: "ladder", group: "market" },
    { name: "watch", path: "/watch/:code?", nav: "/watch", title: "看盘", icon: "candle", tab: true, group: "market" },
    { name: "watchlist", path: "/watchlist", title: "自选股", icon: "star", group: "market" },
    { name: "news", path: "/news", title: "消息政策", icon: "news", group: "market" },
    { name: "sectors", path: "/sectors", title: "板块强弱", icon: "layers", group: "market" },
    { name: "mf", path: "/mf", title: "量化选股", icon: "target", tab: true, group: "pick" },
    { name: "screener", path: "/screener", title: "选股器", icon: "filter", group: "pick" },
    { name: "formula", path: "/formula", title: "公式库", icon: "sigma", group: "pick" },
    { name: "strategy", path: "/strategy", title: "策略中心", icon: "compass", group: "strategy" },
    { name: "lab", path: "/lab", title: "模型实验室", icon: "flask", group: "strategy" },
    { name: "predict", path: "/predict", title: "短线预测", icon: "rocket", group: "strategy" },
    { name: "model", path: "/model", title: "模型中心", icon: "cpu", group: "strategy" },
    { name: "settings", path: "/settings", title: "预测设置", icon: "sliders", group: "strategy" },
    { name: "trade", path: "/trade", title: "交易", icon: "wallet", tab: true, group: "trade" },
    { name: "paper", path: "/paper", title: "策略跟踪", icon: "clipboard", group: "trade" },
    { name: "etf", path: "/etf", title: "稳健ETF", icon: "shield", group: "more" },
    { name: "data", path: "/data", title: "数据中心", icon: "database", group: "more" },
    { name: "sources", path: "/sources", title: "数据源", icon: "plug", group: "more" },
    { name: "guide", path: "/guide", title: "新手指南", icon: "book", group: "more" },
  ];
  // 按组整理（hidden 的路由可以访问但不出现在导航里）
  const groupRoutes = (list) => NAV_GROUPS
    .map((g) => ({ ...g, routes: list.filter((r) => (r.group || "more") === g.key && !r.hidden) }))
    .filter((g) => g.routes.length);

  const ICONS = {
    pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    candle: '<path d="M9 5v4"/><rect width="4" height="6" x="7" y="9" rx="1"/><path d="M9 15v2"/><path d="M17 3v2"/><rect width="4" height="8" x="15" y="5" rx="1"/><path d="M17 13v3"/><path d="M3 3v18h18"/>',
    rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    starFill: '<polygon fill="currentColor" points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    news: '<path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/><path d="M18 14h-8"/><path d="M15 18h-5"/><path d="M10 6h8v4h-8V6Z"/>',
    sliders: '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    menu: '<line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    chevronLeft: '<path d="m15 18-6-6 6-6"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    chevronUp: '<path d="m18 15-6-6-6 6"/>',
    arrowLeft: '<path d="m12 19-7-7 7-7M19 12H5"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    checkCircle: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    xCircle: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    plus: '<path d="M5 12h14M12 5v14"/>',
    flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    sparkles: '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    bars: '<path d="M3 3v18h18"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
    building: '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    trend: '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    ladder: '<path d="M8 3v18M16 3v18M8 7h8M8 12h8M8 17h8"/>',
    thermo: '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    clipboard: '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/>',
    pause: '<circle cx="12" cy="12" r="10"/><path d="M10 15V9M14 15V9"/>',
    play: '<circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4V8z"/>',
    // 第三版新增
    plug: '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    book: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
    bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    flask: '<path d="M9 3h6"/><path d="M10 9V3h4v6l5 9a2 2 0 0 1-1.7 3H6.7A2 2 0 0 1 5 18l5-9Z"/><path d="M7.5 15h9"/>',
    sigma: '<path d="M18 7V4H6l6 8-6 8h12v-3"/>',
    compass: '<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
    layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    listCheck: '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    calendar: '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
    lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    briefcase: '<rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    eye: '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    stop: '<polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2"/><line x1="15" x2="9" y1="9" y2="15"/><line x1="9" x2="15" y1="9" y2="15"/>',
    scale: '<path d="M12 3v18"/><path d="M5 21h14"/><path d="M3 7h18"/><path d="M6 7l-3 7a3 3 0 0 0 6 0Z"/><path d="M18 7l-3 7a3 3 0 0 0 6 0Z"/>',
  };

  // ------------------------------------------------------------------ 全局状态
  const store = reactive({
    status: null, phase: "", dataDate: "", theme: "light", themePref: "auto",
    width: window.innerWidth, jobs: {}, watchCodes: [], watchList: [], offline: false,
    clock: "", today: "", pageTitle: "",
    profile: null, needOnboard: false,      // 第三版：settings.profile；还没完成新手向导时显示提醒条
  });
  const isPhone = computed(() => store.width < 700);
  const isNarrow = computed(() => store.width < 1100);
  Object.defineProperty(store, "isPhone", { get: () => isPhone.value });
  Object.defineProperty(store, "isNarrow", { get: () => isNarrow.value });

  const safeStore = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* 隐私模式等 */ } },
  };
  QW.safeStore = safeStore;

  // ------------------------------------------------------------------ 事件总线
  const listeners = {};
  const bus = {
    on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); return () => bus.off(evt, fn); },
    off(evt, fn) { listeners[evt] = (listeners[evt] || []).filter((f) => f !== fn); },
    emit(evt, data) { (listeners[evt] || []).slice().forEach((f) => { try { f(data); } catch (e) { console.error(e); } }); },
  };

  // ------------------------------------------------------------------ 格式化
  const DASH = "—";
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const toFixed = (v, d) => v.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const IND_SHORT = {
    "计算机、通信和其他电子设备制造业": "电子设备", "软件和信息技术服务业": "软件信息", "电气机械和器材制造业": "电气机械",
    "化学原料和化学制品制造业": "化工", "医药制造业": "医药", "专用设备制造业": "专用设备", "通用设备制造业": "通用设备",
    "汽车制造业": "汽车", "互联网和相关服务": "互联网", "电力、热力生产和供应业": "电力", "有色金属冶炼和压延加工业": "有色金属",
    "黑色金属冶炼和压延加工业": "钢铁", "橡胶和塑料制品业": "橡胶塑料", "非金属矿物制品业": "非金属矿物",
    "酒、饮料和精制茶制造业": "酒饮料", "铁路、船舶、航空航天和其他运输设备制造业": "运输设备", "仪器仪表制造业": "仪器仪表",
    "金属制品业": "金属制品", "农副食品加工业": "农副食品", "纺织服装、服饰业": "纺织服装", "房地产业": "房地产", "零售业": "零售",
    "批发业": "批发", "土木工程建筑业": "建筑", "建筑装饰和其他建筑业": "建筑装饰", "货币金融服务": "银行", "资本市场服务": "券商",
    "保险业": "保险", "广播、电视、电影和影视录音制作业": "影视", "新闻和出版业": "出版", "食品制造业": "食品", "造纸和纸制品业": "造纸",
    "家具制造业": "家具", "文教、工美、体育和娱乐用品制造业": "文体用品", "石油加工、炼焦和核燃料加工业": "石油加工",
    "化学纤维制造业": "化纤", "电信、广播电视和卫星传输服务": "电信", "道路运输业": "道路运输", "水上运输业": "航运",
    "航空运输业": "航空", "生态保护和环境治理业": "环保", "煤炭开采和洗选业": "煤炭", "商务服务业": "商务服务",
    "研究和试验发展": "研发服务", "专业技术服务业": "技术服务", "燃气生产和供应业": "燃气", "水的生产和供应业": "水务",
    "纺织业": "纺织", "皮革、毛皮、羽毛及其制品和制鞋业": "皮革制鞋", "印刷和记录媒介复制业": "印刷", "文化艺术业": "文化艺术",
    "有色金属矿采选业": "有色矿采", "黑色金属矿采选业": "黑色矿采", "石油和天然气开采业": "油气开采", "开采专业及辅助性活动": "采矿服务",
    "装卸搬运和运输代理业": "物流", "仓储业": "仓储", "多式联运和运输代理业": "物流", "住宿业": "酒店", "餐饮业": "餐饮",
    "教育": "教育", "卫生": "医疗服务", "体育": "体育", "娱乐业": "娱乐", "公共设施管理业": "公共设施", "其他金融业": "其他金融",
  };
  const fmt = {
    num(v, d = 2) { return isNum(v) ? toFixed(v, d) : DASH; },
    int(v) { return isNum(v) ? Math.round(v).toLocaleString("zh-CN") : DASH; },
    price(v, d = 2) { return isNum(v) ? v.toFixed(d) : DASH; },
    pct(v, d = 2, sign = true) { return isNum(v) ? (sign && v > 0 ? "+" : "") + v.toFixed(d) + "%" : DASH; },
    ratio(v, d = 1, sign = false) { return isNum(v) ? (sign && v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%" : DASH; },
    // 概率：很小的值不显示成 "0.0%"，而是 "<0.1%"
    prob(v, d = 1) { const m = Math.pow(10, -d); return isNum(v) && v > 0 && v * 100 < m ? "<" + m.toFixed(d) + "%" : fmt.ratio(v, d); },
    frac(v, max = 1.5) { return isNum(v) ? (Math.abs(v) > max ? v / 100 : v) : v; },
    // t 值：一位小数；四舍五入会越过 2 / 1 / -1 这几条线时（如 1.97 → "2.0" 却“不够可信”），改为两位小数且不进位
    t(v) {
      if (!isNum(v)) return DASH;
      const s1 = v.toFixed(1);
      const one = +s1;
      const up = [2, 1, -1].some((th) => v < th && one >= th);
      const down = v > -1 && one <= -1;
      if (!up && !down) return s1 === "-0.0" ? "0.0" : s1;
      return ((up ? Math.floor(v * 100) : Math.ceil(v * 100)) / 100).toFixed(2);
    },
    money(v, d = 2) {
      if (!isNum(v)) return DASH;
      const a = Math.abs(v);
      if (a >= 1e12) return (v / 1e12).toFixed(d) + "万亿";
      if (a >= 1e8) return (v / 1e8).toFixed(d) + "亿";
      if (a >= 1e4) return (v / 1e4).toFixed(a >= 1e6 ? 0 : 1) + "万";
      return v.toFixed(0) + "元";
    },
    cap(yi, d = 1) {
      if (!isNum(yi)) return DASH;
      return Math.abs(yi) >= 10000 ? (yi / 10000).toFixed(2) + "万亿" : yi.toFixed(yi >= 1000 ? 0 : d) + "亿";
    },
    vol(shares) {
      if (!isNum(shares)) return DASH;
      const h = shares / 100;
      if (Math.abs(h) >= 1e8) return (h / 1e8).toFixed(2) + "亿手";
      if (Math.abs(h) >= 1e4) return (h / 1e4).toFixed(Math.abs(h) >= 1e6 ? 0 : 1) + "万手";
      return Math.round(h) + "手";
    },
    volShort(shares) {
      if (!isNum(shares)) return "";
      const h = shares / 100;
      if (h >= 1e8) return (h / 1e8).toFixed(1) + "亿";
      if (h >= 1e4) return (h / 1e4).toFixed(0) + "万";
      return String(Math.round(h));
    },
    date(s) { return s ? String(s).slice(0, 10) : DASH; },
    md(s) { return s ? String(s).slice(5, 10) : DASH; },
    cnDate(s) {
      if (!s) return DASH;
      const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})/);
      return m ? `${+m[2]}月${+m[3]}日` : String(s);
    },
    time(s) {
      if (!s) return DASH;
      const m = String(s).match(/(\d{2}:\d{2})(:\d{2})?/);
      return m ? m[1] : String(s);
    },
    datetime(s) { return s ? String(s).replace("T", " ").slice(0, 16) : DASH; },
    industry(s) {
      if (!s) return DASH;
      const t = String(s).replace(/^[A-Z]\d*/, "").trim();
      if (IND_SHORT[t]) return IND_SHORT[t];
      let short = t.split(/[、和，,]/)[0].replace(/(制造业|制品业|业)$/, "");
      if (!short) short = t;
      return short.length > 6 ? short.slice(0, 6) : short;
    },
    board(b) { return BOARDS[b] || b || DASH; },
    dir(v) { return !isNum(v) ? "" : v > 1e-9 ? "up" : v < -1e-9 ? "down" : "flat"; },
    arrow(v) { return !isNum(v) ? "" : v > 1e-9 ? "▲" : v < -1e-9 ? "▼" : ""; },
  };

  // ------------------------------------------------------------------ 逐笔交易指标（模型 meta.trade_oos / 回测结果 / 模拟盘，字段名宽松兼容）
  const pickNum = (o, keys) => {
    if (!o || typeof o !== "object") return null;
    for (const k of keys) if (isNum(o[k])) return o[k];
    return null;
  };
  const pickObj = (o, keys) => {
    if (!o || typeof o !== "object") return null;
    for (const k of keys) if (o[k] && typeof o[k] === "object" && !Array.isArray(o[k])) return o[k];
    return null;
  };
  // t 值：后端可能同时给按日 t（daily_t）和 Newey-West t（nw_t，考虑持仓跨好几天、相邻几天收益相关）；取两者里低的（更保守）
  const T_DAILY_KEYS = ["daily_t", "t_daily", "t"];
  const T_NW_KEYS = ["nw_t", "t_nw", "newey_west_t"];
  function pickT(o, pre = "") {
    const d = pickNum(o, T_DAILY_KEYS.map((k) => pre + k));
    const w = pickNum(o, T_NW_KEYS.map((k) => pre + k));
    return { t: isNum(d) && isNum(w) ? Math.min(d, w) : isNum(d) ? d : w, tDaily: d, tNw: w };
  }
  function normYears(v) {
    if (!v) return [];
    const list = Array.isArray(v) ? v : Object.keys(v).map((y) => (isNum(v[y]) ? { year: y, mean: v[y] } : { year: y, ...v[y] }));
    return list.filter((x) => x && x.year != null).map((x) => ({
      year: String(x.year),
      n: pickNum(x, ["n", "n_trades", "trades"]),
      mean: fmt.frac(pickNum(x, ["mean", "avg_return", "avg", "return"]), 0.5),
      win: fmt.frac(pickNum(x, ["win_rate", "win"]), 1.5),
      t: pickT(x).t,
    })).sort((a, b) => (a.year < b.year ? -1 : 1));
  }
  /* normTrade(o) → {n, mean, median, win, t, tDaily, tNw, fill, fillSrc, cagr, total, start, end, base{mean,win,...,same}, match, sel, hold, years[],
                     signals, unfilled, skippedGap, suspended, skippedLot, skippedCash, skipShare, uncapped{n,mean,win}|null, heldSkipped, notes[]} | null
     o 可以是 meta.trade_oos、POST /api/predict/backtest 的整个返回（metrics+baseline+by_period）、或其中一段；
     收益/胜率统一成小数（0.012 = 1.2%）；t = daily_t 和 nw_t 里低的那个（只有一个时用那个）；
     fill 优先用后端的 fill_rate（fillSrc="backend"），没有时按 1 - unfilled/signals 估算（fillSrc="calc"）；
     skipShare = (skipped_lot + skipped_cash) / signals：钱不够、一手太贵没买的信号占比 */
  function normTrade(o, depth = 0) {
    if (!o || typeof o !== "object") return null;
    const m = o.metrics && typeof o.metrics === "object" ? { ...o, ...o.metrics } : o;
    const signals = pickNum(m, ["signals", "n_signals"]);
    const unfilled = pickNum(m, ["unfilled"]);
    const filled = pickNum(m, ["filled"]);
    let fill = fmt.frac(pickNum(m, ["fill_rate", "fillable_rate", "buyable_rate"]), 1.5);
    let fillSrc = isNum(fill) ? "backend" : null;
    if (!isNum(fill) && isNum(signals) && signals > 0) {
      if (isNum(unfilled)) fill = Math.max(0, 1 - unfilled / signals);
      else if (isNum(filled)) fill = Math.min(1, filled / signals);
      if (isNum(fill)) fillSrc = "calc";
    }
    const skippedLot = pickNum(m, ["skipped_lot"]);
    const skippedCash = pickNum(m, ["skipped_cash"]);
    const skipN = (skippedLot || 0) + (skippedCash || 0);
    const unc = pickObj(m, ["per_trade_uncapped", "uncapped"]);
    const tt = pickT(m);
    const out = {
      n: pickNum(m, ["n", "n_trades", "trades"]),
      mean: fmt.frac(pickNum(m, ["mean", "avg_return", "avg", "mean_return"]), 0.5),
      median: fmt.frac(pickNum(m, ["median", "median_return"]), 0.5),
      win: fmt.frac(pickNum(m, ["win_rate", "win"]), 1.5),
      t: tt.t, tDaily: tt.tDaily, tNw: tt.tNw,
      cagr: fmt.frac(pickNum(m, ["cagr"]), 5),
      total: fmt.frac(pickNum(m, ["total_return"]), 50),
      fill, fillSrc, signals, unfilled,
      skippedGap: pickNum(m, ["skipped_gap"]), suspended: pickNum(m, ["suspended"]), skippedLot, skippedCash,
      skipShare: isNum(signals) && signals > 0 && (isNum(skippedLot) || isNum(skippedCash)) ? skipN / signals : null,
      uncapped: unc && isNum(pickNum(unc, ["avg_return", "mean"])) ? {
        n: pickNum(unc, ["trades", "n"]), mean: fmt.frac(pickNum(unc, ["avg_return", "mean"]), 0.5), win: fmt.frac(pickNum(unc, ["win_rate", "win"]), 1.5),
      } : null,
      heldSkipped: pickNum(m, ["held_skipped"]),
      notes: depth < 1 && Array.isArray(o.notes) ? o.notes.filter((x) => typeof x === "string" && x) : [],
      start: m.start || m.oos_start || null, end: m.end || m.oos_end || null,
      base: null, sel: null, hold: null, years: normYears(m.by_year || m.yearly),
    };
    const normBase = (b) => (b ? {
      mean: fmt.frac(pickNum(b, ["mean", "avg_return", "avg"]), 0.5), win: fmt.frac(pickNum(b, ["win_rate", "win"]), 1.5),
      cagr: fmt.frac(pickNum(b, ["cagr"]), 5), total: fmt.frac(pickNum(b, ["total_return"]), 50), t: pickT(b).t,
      n: pickNum(b, ["n", "trades"]), same: b.same_as_baseline === true,
    } : null);
    // base：天天随便挑 top_n（包括模型“不操作”的日子）；match：只在模型出手的日子、随便挑同样多只（same=true 表示两者相同）
    out.base = normBase(pickObj(m, ["baseline", "random", "random_baseline"]));
    out.match = normBase(pickObj(m, ["baseline_matched", "random_matched"]));
    // 扁平摘要（/api/predict/today 的 model.trade）：baseline_avg_return、selection_avg_return、holdout_* ……
    const flatBase = (pre) => (isNum(m[pre + "_avg_return"]) ? { mean: fmt.frac(m[pre + "_avg_return"], 0.5), win: fmt.frac(pickNum(m, [pre + "_win_rate"]), 1.5), cagr: null, total: null, t: null, n: null, same: false } : null);
    if (!out.base) out.base = flatBase("baseline");
    if (!out.match) out.match = flatBase("baseline_matched");
    if (depth < 1) {
      const per = pickObj(m, ["by_period", "periods"]) || m;
      out.sel = normTrade(pickObj(per, ["selection", "sel"]), depth + 1);
      out.hold = normTrade(pickObj(per, ["holdout", "hold"]), depth + 1);
      const flat = (pre) => {
        if (!isNum(m[pre + "_avg_return"])) return null;
        const ft = pickT(m, pre + "_");
        return {
          n: pickNum(m, [pre + "_n"]), mean: fmt.frac(m[pre + "_avg_return"], 0.5), median: null, win: fmt.frac(pickNum(m, [pre + "_win_rate"]), 1.5),
          t: ft.t, tDaily: ft.tDaily, tNw: ft.tNw, fill: null, cagr: null, total: null, start: null, end: null, years: [], sel: null, hold: null,
          base: flatBase(pre + "_baseline"), match: flatBase(pre + "_baseline_matched"),
        };
      };
      if (!out.sel) out.sel = flat("selection");
      if (!out.hold) out.hold = flat("holdout");
    }
    // 这组数里有没有用到 Newey-West t（页面据此决定要不要解释“取两种算法里低的”）
    out.hasNw = [out, out.sel, out.hold].some((x) => x && isNum(x.tNw));
    return out;
  }
  // t 值白话：把每天平均收益和它的波动放在一起比；>2 才算比较可信
  // 正负对称：t ≥ 2 比较可信（仍需模拟盘验证）；1~2 略好但不够确定；-1~1 看不出真本事；-2~-1 偏亏但不够确定；≤ -2 稳定地亏钱
  function tWord(t) {
    if (!isNum(t)) return "";
    if (t >= 2) return "比较可信（仍需模拟盘验证）";
    if (t >= 1) return "略好，但不够确定";
    if (t > -1) return "看不出真本事";
    if (t > -2) return "偏亏，但不够确定";
    return "稳定地亏钱";
  }
  // 能买进的比例：不到 100% 时不四舍五入成 100%（0.9967 → "99.7%"，0.9996 → "99.9%"）
  function fillPct(v) {
    if (!isNum(v)) return DASH;
    if (v >= 1) return "100%";
    const x = Math.min(99.9, Math.round(v * 1000) / 10);
    return x.toFixed(1) + "%";
  }
  // 一段时间大约几个月（start/end 为日期字符串）；算不出时 null
  function spanMonths(start, end) {
    const a = Date.parse(String(start || "").slice(0, 10));
    const b = Date.parse(String(end || "").slice(0, 10));
    if (!isFinite(a) || !isFinite(b) || b <= a) return null;
    return Math.max(1, Math.round((b - a) / 86400000 / 30.44));
  }
  // 留出期的名字：“留出期（最近约 15 个月）”；没有起止日期时写分界
  function holdLabel(o) {
    const n = o ? spanMonths(o.start, o.end) : null;
    return n ? `留出期（最近约 ${n} 个月）` : "留出期（2025-07 以后）";
  }
  /* 最近一段（留出期）的一句话结论：mean = 留出期平均每笔，rnd = 同一段“同一天随便挑同样多只”的平均每笔，t = 留出期 t 值（取保守的）。
     watch=true（连板/首板这类观察名单）时结尾说“只当观察名单”而不是“只做模拟跟踪”。
     → {key: "worse"|"loss"|"norand"|"weak"|"good", tone: "warn"|"info", text}；mean 缺失时 null */
  function recentVerdict(mean, rnd, t, watch = false) {
    if (!isNum(mean)) return null;
    const nope = "目前没有证据证明它能赚钱，" + (watch ? "只当观察名单" : "只做模拟跟踪");
    if (mean <= 0) return { key: "loss", tone: "warn", text: "最近一段时间照着买平均是亏的" + (isNum(rnd) && mean <= rnd ? "，也不比同一天随便挑好" : "") + "——" + nope };
    if (isNum(rnd) && mean <= rnd) return { key: "worse", tone: "warn", text: "最近一段时间，它并不比同一天随便挑更好——" + nope };
    if (!isNum(rnd)) return { key: "norand", tone: "warn", text: "最近一段时间没有“同一天随便挑”的对比数据，还不能确定是真本事，" + (watch ? "只当观察名单" : "只做模拟跟踪") };
    if (!isNum(t) || t < 2) return { key: "weak", tone: "warn", text: "略好于随便挑，但还不能确定是真本事" };
    return { key: "good", tone: "info", text: "最近表现较好，但仍需模拟盘验证" };
  }
  // 留出期的如实说明（不要说它完全没被用来挑方案、调参数——那说过头了）
  const HOLD_CAVEAT = "留出期（2025-07 以后）的数据没有参与训练；但方案是在研究中比较多种做法后选出的，结果可能偏乐观。";
  // 样本外成绩的如实说明（不要写“不存在偷看答案”“从没用来调参数”这类过头的话）
  const OOS_CAVEAT = "尽量避免偷看答案；行业/ST 用当前归属、方案是在2022–2025年的检验中选出的，结果可能偏乐观。";
  const T_HELP = "t 值：把每天的平均收益和它的上下波动放在一起比，看赚钱是不是稳定。t 值 ≥2 才算比较可信（仍需模拟盘验证）；1~2 之间略好，但不够确定（可能只是运气）；" +
    "-1~1 之间看不出真本事；-2~-1 之间偏亏，但不够确定；≤ -2 是稳定地亏钱。";
  // 有 Newey-West t 时追加的白话（后端 nw_t）
  const T_NW_NOTE = "一笔交易常常连着拿好几天，相邻几天的盈亏会一起涨跌，直接按天算会把可信度算高；另一种算法（Newey-West）把这一点考虑进去，算出来通常更低。页面上的 t 值取两种算法里低的那个（更保守）。";
  const tHelp = (x) => T_HELP + (x && x.hasNw ? "\n" + T_NW_NOTE : "");
  // 按用户本金回测时，钱不够/一手太贵跳过了很多信号：账户结果主要代表前一段时间，另给“每个信号都买”的逐笔结果
  const SKIP_HEAVY = 0.2;
  const UNCAPPED_HELP = "按你的本金回测时，钱不够或股价太高（一手 100 股都买不起）的信号会被跳过；跳过得多时，账户的成绩只代表其中一部分信号。" +
    "这里另算一遍：不管资金多少，每个能买进的信号都按同样规则买卖、按比例收费，看平均每笔多少。它不受资金限制、偏乐观，只用来对照。";
  // 两种随机对比（后端 baseline / baseline_matched，见 ARCHITECTURE 7.3）
  const BASE_HELP = "天天随便挑：同一批候选股里，每个交易日都随便挑同样多只（每天挑满，包括模型因为没有达标股票而“不操作”的日子），按同样的规则买卖，随机 20 次取平均。" +
    "所以模型比它多赚的部分里，既有“挑股票”的本事，也有“挑日子”（不操作的日子躲开了）的作用。";
  const MATCH_HELP = "同日同数量随便挑：只在模型出手的那些天，从同一批候选股里随便挑和模型一样多只，按同样规则买卖（随机 20 次平均）。" +
    "模型比它多赚的，才是真正“挑股票”的本事；它比“天天随便挑”多赚的，是“挑日子”（择时）的作用。";
  // 改过设置再看历史检验：挑设置本身就用到了这段历史
  const TUNED_HELP = "如果你是看着历史检验的结果改设置（改一改、检验一下、再改），挑出来的那套设置在这段历史上自然好看——相当于看过答案再考试。" +
    "这时历史成绩（包括留出期）都会偏乐观，只有之后的模拟盘成绩才能说明它是不是真的有效。";

  // ------------------------------------------------------------------ Toast
  const toastState = reactive({ list: [] });
  let toastSeq = 0;
  function closeToast(id) { toastState.list = toastState.list.filter((t) => t.id !== id); }
  function showToast(type, text, opts = {}) {
    const dup = toastState.list.find((t) => t.text === text && t.type === type);
    if (dup) return dup.id;
    const id = ++toastSeq;
    const duration = opts.duration != null ? opts.duration : type === "error" ? 6000 : 3500;
    toastState.list.push({ id, type, text: String(text) });
    if (toastState.list.length > 4) toastState.list.shift();
    if (duration > 0) setTimeout(() => closeToast(id), duration);
    return id;
  }
  const toast = {
    show: showToast, close: closeToast,
    success: (t, o) => showToast("success", t, o), error: (t, o) => showToast("error", t, o),
    info: (t, o) => showToast("info", t, o), warn: (t, o) => showToast("warn", t, o),
  };

  // ------------------------------------------------------------------ API
  async function request(method, path, { params, body, silent } = {}) {
    let url = path;
    if (params) {
      const qs = new URLSearchParams();
      Object.keys(params).forEach((k) => {
        const v = params[k];
        if (v !== undefined && v !== null && v !== "") qs.append(k, v);
      });
      const s = qs.toString();
      if (s) url += (url.includes("?") ? "&" : "?") + s;
    }
    const init = { method, headers: { Accept: "application/json" } };
    if (body !== undefined && body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, init);
    } catch (e) {
      const err = new Error("连接不到本地服务，请确认“量化助手”程序还开着");
      err.status = 0;
      err.detail = err.message;
      if (!silent) toast.error(err.message);
      throw err;
    }
    const text = await resp.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
    if (!resp.ok) {
      let detail = data && typeof data === "object" ? data.detail : null;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("；");
      if (!detail || typeof detail !== "string") detail = `请求失败（${resp.status}）`;
      const err = new Error(detail);
      err.status = resp.status;
      err.detail = detail;
      if (!silent) toast.error(detail);
      throw err;
    }
    return data;
  }
  const api = {
    request,
    get: (path, params, opts = {}) => request("GET", path, { ...opts, params }),
    post: (path, body, opts = {}) => request("POST", path, { ...opts, body: body === undefined ? {} : body }),
    put: (path, body, opts = {}) => request("PUT", path, { ...opts, body }),
    del: (path, body, opts = {}) => request("DELETE", path, { ...opts, body }),
  };

  // ------------------------------------------------------------------ 路由
  function matchRoute(pattern, segs) {
    const ps = pattern.split("/").filter(Boolean);
    if (segs.length > ps.length) return null;
    const params = {};
    for (let i = 0; i < ps.length; i++) {
      const p = ps[i];
      const s = segs[i];
      if (p.startsWith(":")) {
        const key = p.replace(/^:|\?$/g, "");
        if (s == null) { if (!p.endsWith("?")) return null; continue; }
        params[key] = decodeURIComponent(s);
      } else if (p !== s) return null;
    }
    return params;
  }
  function parseHash() {
    let h = decodeURI(location.hash.replace(/^#/, "")) || "/";
    if (!h.startsWith("/")) h = "/" + h;
    const [p, qs] = h.split("?");
    const query = Object.fromEntries(new URLSearchParams(qs || ""));
    const segs = p.split("/").filter(Boolean);
    for (const r of ROUTES) {
      const params = matchRoute(r.path, segs);
      if (params) return { name: r.name, path: p, params, query, title: r.title };
    }
    return { name: "notfound", path: p, params: {}, query, title: "页面不存在" };
  }
  const route = reactive(parseHash());
  window.addEventListener("hashchange", () => {
    const r = parseHash();
    const samePage = r.name === route.name && JSON.stringify(r.params) === JSON.stringify(route.params);
    Object.assign(route, r);
    if (!samePage) { store.pageTitle = ""; window.scrollTo(0, 0); }
    hideTip();
  });
  function go(path, query) {
    let h = path.startsWith("/") ? path : "/" + path;
    if (query) {
      const qs = new URLSearchParams();
      Object.keys(query).forEach((k) => { if (query[k] != null && query[k] !== "") qs.append(k, query[k]); });
      const s = qs.toString();
      if (s) h += "?" + s;
    }
    if (location.hash !== "#" + h) location.hash = h;
  }
  function setTitle(t) { store.pageTitle = t || ""; }

  // ------------------------------------------------------------------ 浮动提示
  const tip = reactive({ show: false, text: "", x: -9999, y: -9999 });
  let tipEl = null;
  let tipAnchor = null;
  function showTip(el, text, place = "top") {
    if (!text) return;
    tipAnchor = el;
    tip.text = text;
    tip.show = true;
    tip.x = -9999;
    tip.y = -9999;
    nextTick(() => {
      if (!tipEl || tipAnchor !== el) return;
      const r = el.getBoundingClientRect();
      const w = tipEl.offsetWidth;
      const h = tipEl.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let x;
      let y;
      if (place === "right") {
        x = r.right + 10;
        y = r.top + r.height / 2 - h / 2;
      } else {
        x = r.left + r.width / 2 - w / 2;
        y = r.top - h - 8;
        if (y < 6) y = r.bottom + 8;
      }
      tip.x = Math.max(8, Math.min(x, vw - w - 8));
      tip.y = Math.max(6, Math.min(y, vh - h - 6));
    });
  }
  function hideTip() { tip.show = false; tipAnchor = null; }
  window.addEventListener("scroll", hideTip, true);
  document.addEventListener("click", (e) => {
    if (tipAnchor && !tipAnchor.contains(e.target)) hideTip();
  });
  /* v-hscroll：放在可以左右滚动的元素上（其父元素带 class="hs-box"）。内容比容器宽时给父元素加 hs-over（手机上显示“左右滑动查看”），
     右边还有没看到的列时加 hs-r（右侧渐隐阴影），已经往右滑过时加 hs-l */
  const hscrollDirective = {
    mounted(el) {
      const box = el.parentElement;
      const upd = () => {
        if (!box) return;
        const max = el.scrollWidth - el.clientWidth;
        const over = max > 4;
        box.classList.toggle("hs-over", over);
        box.classList.toggle("hs-l", over && el.scrollLeft > 4);
        box.classList.toggle("hs-r", over && el.scrollLeft < max - 4);
      };
      let raf = 0;
      el._hsUpd = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; upd(); }); };
      el.addEventListener("scroll", el._hsUpd, { passive: true });
      window.addEventListener("resize", el._hsUpd);
      if (window.ResizeObserver) {
        el._hsRo = new ResizeObserver(el._hsUpd);
        el._hsRo.observe(el);
        if (el.firstElementChild) el._hsRo.observe(el.firstElementChild);
      }
      el._hsUpd();
    },
    updated(el) { if (el._hsUpd) el._hsUpd(); },
    beforeUnmount(el) {
      el.removeEventListener("scroll", el._hsUpd);
      window.removeEventListener("resize", el._hsUpd);
      if (el._hsRo) el._hsRo.disconnect();
    },
  };
  const tipDirective = {
    mounted(el, b) {
      el._tip = b.value;
      el._tipPlace = b.modifiers.right ? "right" : "top";
      el._tipEnter = () => { if (el._tip) showTip(el, el._tip, el._tipPlace); };
      el._tipLeave = () => { if (tipAnchor === el) hideTip(); };
      el._tipClick = (e) => {
        if (!el._tip) return;
        if (!(tipAnchor === el && tip.show)) showTip(el, el._tip, el._tipPlace);
        if (b.modifiers.click) e.stopPropagation();
      };
      el.addEventListener("mouseenter", el._tipEnter);
      el.addEventListener("mouseleave", el._tipLeave);
      el.addEventListener("focus", el._tipEnter);
      el.addEventListener("blur", el._tipLeave);
      el.addEventListener("click", el._tipClick);
    },
    updated(el, b) {
      el._tip = b.value;
      if (tipAnchor === el && tip.show) tip.text = b.value || "";
    },
    beforeUnmount(el) {
      if (tipAnchor === el) hideTip();
      el.removeEventListener("mouseenter", el._tipEnter);
      el.removeEventListener("mouseleave", el._tipLeave);
      el.removeEventListener("focus", el._tipEnter);
      el.removeEventListener("blur", el._tipLeave);
      el.removeEventListener("click", el._tipClick);
    },
  };

  // ------------------------------------------------------------------ 主题与配色
  const sysDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function applyTheme() {
    const t = store.themePref === "auto" ? (sysDark && sysDark.matches ? "dark" : "light") : store.themePref;
    document.documentElement.setAttribute("data-theme", t);
    store.theme = t;
  }
  function toggleTheme() {
    store.themePref = store.theme === "dark" ? "light" : "dark";
    safeStore.set("qw-theme", store.themePref);
    applyTheme();
  }
  store.themePref = safeStore.get("qw-theme") || "auto";
  applyTheme();
  if (sysDark && sysDark.addEventListener) sysDark.addEventListener("change", () => { if (store.themePref === "auto") applyTheme(); });

  let colorCache = { theme: null, c: null };
  function colors() {
    const theme = store.theme; // 建立响应式依赖：主题切换时图表重算
    if (colorCache.theme === theme && colorCache.c) return colorCache.c;
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    const c = {
      dark: theme === "dark", up: v("--up"), down: v("--down"), text: v("--text"), text2: v("--text-2"), text3: v("--text-3"),
      grid: v("--chart-grid"), axis: v("--chart-axis"), surface: v("--surface"), surface2: v("--surface-2"),
      surface3: v("--surface-3"), border: v("--border"), primary: v("--primary"), primaryWeak: v("--primary-weak"),
      warn: v("--warn"), gold: v("--gold"), font: v("--font"),
      series: ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"].map(v),
      bands: ["--g1", "--g2", "--g3", "--g4", "--g5"].map(v),
    };
    colorCache = { theme, c };
    return c;
  }
  function tooltipBase(c) {
    return {
      backgroundColor: c.surface, borderColor: c.border, borderWidth: 1, padding: [8, 10],
      textStyle: { color: c.text, fontSize: 12, fontFamily: c.font },
      extraCssText: "box-shadow:0 6px 20px rgba(0,0,0,.14);border-radius:8px;",
    };
  }
  function axisBase(c) {
    return {
      axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false },
      axisLabel: { color: c.text3, fontSize: 11, fontFamily: c.font },
      splitLine: { lineStyle: { color: c.grid } },
    };
  }

  // ------------------------------------------------------------------ 轮询
  function usePoll(fn, ms, { live = true, immediate = false } = {}) {
    let timer = null;
    const tick = () => {
      if (document.hidden) return;
      if (live && store.phase !== "交易中") return;
      fn();
    };
    const onVis = () => { if (!document.hidden) tick(); };
    onMounted(() => {
      if (immediate) fn();
      timer = setInterval(tick, ms);
      document.addEventListener("visibilitychange", onVis);
    });
    onBeforeUnmount(() => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    });
    return { trigger: fn };
  }

  // ------------------------------------------------------------------ 状态/任务/自选
  let statusTimer = null;
  async function refreshStatus() {
    try {
      const s = await api.get("/api/status", null, { silent: true });
      store.status = s;
      store.phase = s.phase || "";
      store.dataDate = (s.panel && s.panel.end) || "";
      store.offline = false;
      (s.jobs || []).forEach((j) => { if (j && j.id && j.status === "running") jobs.track(j.id, { title: j.title }); });
      bus.emit("status", s);
      return s;
    } catch (e) {
      store.offline = e.status === 0;
      return null;
    }
  }
  const tracked = {};
  const jobs = {
    track(id, { title, onDone, onFail } = {}) {
      if (!id) return;
      if (tracked[id]) {
        if (onDone) tracked[id].onDone.push(onDone);
        if (onFail) tracked[id].onFail.push(onFail);
        return;
      }
      const t = (tracked[id] = { onDone: onDone ? [onDone] : [], onFail: onFail ? [onFail] : [], errors: 0 });
      if (!store.jobs[id]) store.jobs[id] = { id, title: title || "后台任务", status: "running", progress: 0, message: "正在启动…", logs: [] };
      const poll = async () => {
        let job;
        try {
          job = await api.get("/api/jobs/" + encodeURIComponent(id), null, { silent: true });
          t.errors = 0;
        } catch (e) {
          t.errors += 1;
          if (t.errors > 20 || e.status === 404) { delete tracked[id]; delete store.jobs[id]; return; }
          setTimeout(poll, 3000);
          return;
        }
        if (!job.title && title) job.title = title;
        store.jobs[id] = job;
        if (job.status === "done") {
          delete tracked[id];
          toast.success(`${job.title || "任务"}已完成`);
          bus.emit("job-done", job);
          refreshStatus();
          t.onDone.forEach((f) => f(job));
          setTimeout(() => { if (store.jobs[id] && store.jobs[id].status !== "running") delete store.jobs[id]; }, 60000);
        } else if (job.status === "failed") {
          delete tracked[id];
          toast.error(`${job.title || "任务"}失败：${job.error || job.message || "未知原因"}`, { duration: 10000 });
          bus.emit("job-failed", job);
          t.onFail.forEach((f) => f(job));
        } else {
          setTimeout(poll, 1200);
        }
      };
      setTimeout(poll, 500);
    },
    async start(url, body, title) {
      const r = await api.post(url, body || {});
      const id = r && r.job_id;
      if (id) {
        jobs.track(id, { title });
        toast.info(`已开始：${title}，可以继续浏览其他页面`);
      }
      return id;
    },
    running() { return Object.values(store.jobs).filter((j) => j.status === "running"); },
  };
  const watchApi = {
    async refresh() {
      try {
        const list = await api.get("/api/watchlist", null, { silent: true });
        store.watchList = Array.isArray(list) ? list : [];
        store.watchCodes = store.watchList.map((x) => x.code);
      } catch (e) { /* 忽略 */ }
      return store.watchList;
    },
    has(code) { return store.watchCodes.includes(code); },
    async toggle(code, name) {
      const label = name ? `${name}（${code}）` : code;
      if (watchApi.has(code)) {
        await api.del("/api/watchlist", { code });
        store.watchCodes = store.watchCodes.filter((c) => c !== code);
        toast.success(`已从自选移除：${label}`);
      } else {
        await api.post("/api/watchlist", { code });
        store.watchCodes = store.watchCodes.concat([code]);
        toast.success(`已加入自选：${label}`);
      }
      bus.emit("watch-changed", code);
      watchApi.refresh();
    },
  };
  const recent = {
    list() {
      try { return JSON.parse(safeStore.get("qw-recent") || "[]"); } catch (e) { return []; }
    },
    add(code, name) {
      if (!code) return;
      const list = recent.list().filter((x) => x.code !== code);
      list.unshift({ code, name: name || code });
      safeStore.set("qw-recent", JSON.stringify(list.slice(0, 12)));
    },
  };

  // ================================================================== 组件
  const Icon = {
    name: "QwIcon",
    props: { name: String, size: { type: [Number, String], default: 18 } },
    computed: { paths() { return ICONS[this.name] || ""; } },
    template: `<svg class="qw-icon" :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="paths"></svg>`,
  };

  const HelpTip = {
    name: "QwHelp",
    props: { text: String },
    template: `<span class="qw-help" role="button" tabindex="0" aria-label="说明" v-tip.click="text">?</span>`,
  };

  const Skeleton = {
    name: "QwSkeleton",
    props: { rows: { type: Number, default: 3 }, height: String, type: String, count: { type: Number, default: 4 } },
    data() { return { widths: ["92%", "76%", "84%", "64%", "88%"] }; },
    template: `<div class="qw-skel-wrap" aria-busy="true">
      <div v-if="type==='tiles'" class="qw-skel-tiles"><div v-for="i in count" :key="i" class="qw-skel"></div></div>
      <div v-else-if="height" class="qw-skel" :style="{height}"></div>
      <template v-else><div v-for="i in rows" :key="i" class="qw-skel qw-skel-line" :style="{width: widths[i % widths.length]}"></div></template>
    </div>`,
  };

  const Card = {
    name: "QwCard",
    props: { title: String, help: String, sub: String, icon: String, pad: { type: Boolean, default: true }, loading: Boolean, skeletonHeight: String },
    template: `<section class="qw-card" :class="{'qw-card--flush': !pad}">
      <header v-if="title || $slots.extra || $slots.title" class="qw-card-hd">
        <div class="qw-card-title"><qw-icon v-if="icon" :name="icon" :size="16"/><slot name="title"><span>{{ title }}</span></slot><qw-help v-if="help" :text="help"/><small v-if="sub" class="qw-card-sub">{{ sub }}</small></div>
        <div v-if="$slots.extra" class="qw-card-extra"><slot name="extra"/></div>
      </header>
      <div class="qw-card-bd"><div v-if="loading" :style="pad ? null : {padding: '0 16px 16px'}"><qw-skeleton :height="skeletonHeight" :rows="4"/></div><slot v-else/></div>
      <footer v-if="$slots.footer" class="qw-card-ft"><slot name="footer"/></footer>
    </section>`,
  };

  const Stat = {
    name: "QwStat",
    props: {
      label: String, value: [String, Number], unit: String, help: String, delta: Number, deltaText: String,
      tone: String, sub: String, size: String, loading: Boolean, to: String, flat: Boolean,
    },
    computed: {
      deltaDir() { return fmt.dir(this.delta); },
    },
    methods: { click() { if (this.to) go(this.to); } },
    template: `<div class="qw-stat" :class="[size, {clickable: !!to, 'flat-bg': flat}]" @click="click">
      <div class="qw-stat-label"><span>{{ label }}</span><qw-help v-if="help" :text="help" @click.stop/></div>
      <div v-if="loading" class="qw-skel" style="height:30px;margin-top:8px;width:70%"></div>
      <div v-else class="qw-stat-value" :class="tone"><span>{{ value == null || value === '' ? '—' : value }}</span><small v-if="unit">{{ unit }}</small></div>
      <div v-if="!loading && (deltaText || sub || $slots.sub)" class="qw-stat-sub">
        <span v-if="deltaText" class="delta" :class="deltaDir">{{ deltaDir==='up' ? '▲' : deltaDir==='down' ? '▼' : '' }} {{ deltaText }}</span>
        <span v-if="sub">{{ sub }}</span><slot name="sub"/>
      </div>
    </div>`,
  };

  const Empty = {
    name: "QwEmpty",
    props: { icon: { type: String, default: "inbox" }, title: String, desc: String, actionText: String, actionTo: String, compact: Boolean },
    emits: ["action"],
    methods: { act() { this.$emit("action"); if (this.actionTo) go(this.actionTo); } },
    template: `<div class="qw-empty" :class="{compact}">
      <div class="qw-empty-icon"><qw-icon :name="icon" :size="compact ? 20 : 26"/></div>
      <div v-if="title" class="qw-empty-title">{{ title }}</div>
      <div v-if="desc" class="qw-empty-desc">{{ desc }}</div>
      <div v-if="actionText || $slots.default" class="qw-empty-actions">
        <button v-if="actionText" class="btn primary" @click="act">{{ actionText }}</button><slot/>
      </div>
    </div>`,
  };

  const PriceText = {
    name: "QwPrice",
    props: {
      value: Number, refValue: Number, pct: Boolean, ratio: Boolean, change: Boolean, digits: { type: Number, default: 2 },
      arrow: Boolean, colorBy: Number, suffix: String,
    },
    computed: {
      dir() {
        let basis = null;
        if (isNum(this.colorBy)) basis = this.colorBy;
        else if (isNum(this.value) && isNum(this.refValue)) basis = this.value - this.refValue;
        else if (this.pct || this.ratio || this.change) basis = this.value;
        return fmt.dir(basis);
      },
      text() {
        const v = this.value;
        if (!isNum(v)) return DASH;
        if (this.pct) return fmt.pct(v, this.digits, true);
        if (this.ratio) return fmt.ratio(v, this.digits, true);
        if (this.change) return (v > 0 ? "+" : "") + v.toFixed(this.digits);
        return v.toFixed(this.digits);
      },
    },
    template: `<span class="qw-price" :class="dir"><span v-if="arrow && (dir==='up' || dir==='down')" class="arr">{{ dir==='up' ? '▲' : '▼' }}</span>{{ text }}{{ suffix || '' }}</span>`,
  };

  const PctBar = {
    name: "QwPctbar",
    props: { value: Number, digits: { type: Number, default: 1 }, max: { type: Number, default: 1 }, tone: String },
    computed: {
      w() { return isNum(this.value) ? Math.max(0, Math.min(100, (this.value / this.max) * 100)) : 0; },
      text() { return isNum(this.value) ? fmt.ratio(this.value, this.digits) : DASH; },
    },
    template: `<div class="qw-pctbar" :class="tone"><div class="track"><div class="fill" :style="{width: w + '%'}"></div></div><span class="txt">{{ text }}</span></div>`,
  };

  const MiniBars = {
    name: "QwMinibars",
    props: {
      dims: Object, keys: { type: Array, default: () => ["sentiment", "capital", "fundamental", "theme", "technical"] },
      height: { type: Number, default: 26 },
    },
    computed: {
      bars() {
        const d = this.dims || {};
        return this.keys.map((k) => {
          const v = isNum(d[k]) ? Math.max(0, Math.min(100, d[k])) : null;
          return { k, v, cls: v == null ? "mid" : v >= 55 ? "pos" : v <= 45 ? "neg" : "mid" };
        });
      },
      tipText() {
        return this.bars.map((b) => `${LABELS[b.k] || b.k}：${b.v == null ? "—" : Math.round(b.v)}`).join("\n") +
          "\n（50 为中性，越高越有利）";
      },
    },
    template: `<div class="qw-minibars" :style="{height: height + 'px'}" v-tip="tipText" tabindex="0" aria-label="五维评分">
      <span v-for="b in bars" :key="b.k" class="mb" :class="b.cls"><i :style="{height: (b.v == null ? 50 : Math.max(6, b.v)) + '%'}"></i></span>
    </div>`,
  };

  const StockLink = {
    name: "QwStock",
    props: { code: String, name: String, showCode: { type: Boolean, default: true }, inline: Boolean },
    template: `<a class="qw-stock" :class="{inline}" :href="'#/watch/' + code" @click.stop :title="(name || '') + ' ' + code + ' · 点击看盘'"><b>{{ name || code }}</b><small v-if="showCode && name">{{ code }}</small></a>`,
  };

  const BoardTag = {
    name: "QwBoard",
    props: { board: String },
    computed: { label() { return BOARDS[this.board] || this.board; } },
    template: `<span v-if="board" class="qw-tag" :class="'b-' + board">{{ label }}</span>`,
  };

  const StreakBadge = {
    name: "QwStreak",
    props: { n: Number, oneWord: Boolean },
    computed: {
      lv() { const n = this.n || 0; return n >= 5 ? "lv5" : n >= 3 ? "lv3" : n >= 2 ? "lv2" : "lv1"; },
      text() { const n = this.n || 0; return n >= 2 ? `${n}连板` : n === 1 ? "首板" : "—"; },
      tipText() { return this.oneWord ? ONE_WORD_TIP : null; },
    },
    template: `<span class="qw-streak" :class="lv" v-tip="tipText">{{ text }}<template v-if="oneWord"> · 一字</template></span>`,
  };

  const DataTable = {
    name: "QwTable",
    props: {
      columns: { type: Array, default: () => [] }, rows: { type: Array, default: () => [] },
      rowKey: { type: [String, Function], default: "code" }, pageSize: { type: Number, default: 0 },
      defaultSort: Object, clickable: Boolean, maxHeight: String, loading: Boolean,
      emptyText: { type: String, default: "暂无数据" }, dense: Boolean, rowClass: Function, activeKey: null,
    },
    emits: ["row-click", "sort-change"],
    data() {
      return { sortKey: this.defaultSort ? this.defaultSort.key : null, sortOrder: this.defaultSort ? this.defaultSort.order || "desc" : "desc", page: 1 };
    },
    computed: {
      sorted() {
        const rows = (this.rows || []).slice();
        if (!this.sortKey) return rows;
        const col = this.columns.find((c) => c.key === this.sortKey);
        const get = col && col.sortBy ? col.sortBy : (r) => r[this.sortKey];
        const sign = this.sortOrder === "asc" ? 1 : -1;
        return rows.sort((a, b) => {
          const va = get(a);
          const vb = get(b);
          const na = va == null || va === "" || (typeof va === "number" && !isFinite(va));
          const nb = vb == null || vb === "" || (typeof vb === "number" && !isFinite(vb));
          if (na && nb) return 0;
          if (na) return 1;
          if (nb) return -1;
          if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb), "zh-CN") * sign;
          return (va - vb) * sign;
        });
      },
      pages() { return this.pageSize ? Math.max(1, Math.ceil(this.sorted.length / this.pageSize)) : 1; },
      offset() { return this.pageSize ? (this.page - 1) * this.pageSize : 0; },
      paged() { return this.pageSize ? this.sorted.slice(this.offset, this.offset + this.pageSize) : this.sorted; },
    },
    watch: { rows() { if (this.page > this.pages) this.page = 1; } },
    methods: {
      toggleSort(c) {
        if (!c.sortable) return;
        if (this.sortKey === c.key) this.sortOrder = this.sortOrder === "desc" ? "asc" : "desc";
        else { this.sortKey = c.key; this.sortOrder = c.firstOrder || "desc"; }
        this.page = 1;
        this.$emit("sort-change", { key: this.sortKey, order: this.sortOrder });
      },
      keyOf(row, i) { return typeof this.rowKey === "function" ? this.rowKey(row) : row[this.rowKey] != null ? row[this.rowKey] : i; },
      display(row, c) {
        const v = row[c.key];
        if (c.format) return c.format(v, row);
        return v == null || v === "" ? DASH : v;
      },
      alignOf(c) { return c.align === "right" ? "al-right" : c.align === "center" ? "al-center" : ""; },
      thStyle(c) { return c.width ? { width: c.width, minWidth: c.width } : c.minWidth ? { minWidth: c.minWidth } : null; },
    },
    template: `<div class="qw-table-box">
      <div class="hs-box"><div class="qw-table-wrap" v-hscroll :style="maxHeight ? {maxHeight} : null">
        <table class="qw-table" :class="{dense, clickable}">
          <thead><tr>
            <th v-for="c in columns" :key="c.key" :class="[alignOf(c), {sortable: c.sortable, sorted: sortKey === c.key, wrap: c.wrap}]" :style="thStyle(c)" @click="toggleSort(c)">
              <span class="th-inner"><span class="th-l">{{ c.label }}<small v-if="c.sub" class="th-sub">{{ c.sub }}</small></span><qw-help v-if="c.help" :text="c.help" @click.stop/><span v-if="c.sortable" class="sort-ind">{{ sortKey === c.key ? (sortOrder === 'desc' ? '↓' : '↑') : '↕' }}</span></span>
            </th>
          </tr></thead>
          <tbody v-if="!loading">
            <tr v-for="(row, i) in paged" :key="keyOf(row, offset + i)" :class="[rowClass ? rowClass(row) : '', {active: activeKey != null && keyOf(row, offset + i) === activeKey}]" @click="$emit('row-click', row, offset + i)">
              <td v-for="c in columns" :key="c.key" :class="[alignOf(c), c.cls ? c.cls(row[c.key], row) : '']">
                <slot v-if="$slots['cell-' + c.key]" :name="'cell-' + c.key" :row="row" :value="row[c.key]" :index="offset + i"></slot>
                <template v-else>{{ display(row, c) }}</template>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="loading" class="qw-table-loading"><qw-skeleton :rows="6"/></div>
        <div v-else-if="!rows || !rows.length" class="qw-table-empty"><slot name="empty"><qw-empty :title="emptyText" compact/></slot></div>
      </div></div>
      <div v-if="pageSize && pages > 1" class="qw-pager">
        <span>共 {{ sorted.length }} 条</span>
        <button class="btn sm" :disabled="page <= 1" @click="page--" aria-label="上一页"><qw-icon name="chevronLeft" :size="14"/></button>
        <span class="num">{{ page }} / {{ pages }}</span>
        <button class="btn sm" :disabled="page >= pages" @click="page++" aria-label="下一页"><qw-icon name="chevronRight" :size="14"/></button>
      </div>
    </div>`,
  };

  const Tabs = {
    name: "QwTabs",
    props: { modelValue: null, items: { type: Array, default: () => [] } },
    emits: ["update:modelValue"],
    template: `<div class="qw-tabs" role="tablist">
      <button v-for="it in items" :key="it.value" type="button" role="tab" class="qw-tab" :class="{active: it.value === modelValue}" :aria-selected="it.value === modelValue" @click="$emit('update:modelValue', it.value)"><qw-icon v-if="it.icon" :name="it.icon" :size="15"/>{{ it.label }}<span v-if="it.badge != null" class="qw-tab-badge" :class="it.tone ? 'tone-' + it.tone : ''">{{ it.badge }}</span></button>
    </div>`,
  };

  const Segmented = {
    name: "QwSegmented",
    props: { modelValue: null, options: { type: Array, default: () => [] }, block: Boolean, size: String },
    emits: ["update:modelValue"],
    template: `<div class="qw-seg" :class="{block, sm: size === 'sm'}" role="radiogroup">
      <button v-for="o in options" :key="o.value" type="button" role="radio" :aria-checked="o.value === modelValue" :class="{active: o.value === modelValue}" @click="$emit('update:modelValue', o.value)"><qw-icon v-if="o.icon" :name="o.icon" :size="15"/>{{ o.label }}</button>
    </div>`,
  };

  let scrollLocks = 0;
  function lockScroll(on) {
    scrollLocks = Math.max(0, scrollLocks + (on ? 1 : -1));
    document.body.style.overflow = scrollLocks ? "hidden" : "";
  }
  function useOverlay(props, close) {
    const onKey = (e) => { if (e.key === "Escape") close(); };
    watch(() => props.modelValue, (v, old) => {
      if (v === old) return;
      if (v) { document.addEventListener("keydown", onKey); lockScroll(true); }
      else { document.removeEventListener("keydown", onKey); lockScroll(false); }
    }, { immediate: true });
    onBeforeUnmount(() => { if (props.modelValue) { document.removeEventListener("keydown", onKey); lockScroll(false); } });
  }

  const Drawer = {
    name: "QwDrawer",
    props: { modelValue: Boolean, title: String, width: { type: String, default: "520px" } },
    emits: ["update:modelValue", "close"],
    setup(props, { emit }) {
      const close = () => { emit("update:modelValue", false); emit("close"); };
      useOverlay(props, close);
      return { close };
    },
    template: `<teleport to="body">
      <transition name="qw-fade"><div v-if="modelValue" class="qw-mask" @click="close"></div></transition>
      <transition name="qw-slide">
        <aside v-if="modelValue" class="qw-drawer" :style="{'--dw': width}" role="dialog" aria-modal="true">
          <header class="qw-drawer-hd"><div class="qw-drawer-title"><slot name="title">{{ title }}</slot></div><button class="qw-iconbtn" @click="close" aria-label="关闭"><qw-icon name="x"/></button></header>
          <div class="qw-drawer-bd"><slot/></div>
          <footer v-if="$slots.footer" class="qw-drawer-ft"><slot name="footer"/></footer>
        </aside>
      </transition>
    </teleport>`,
  };

  const Modal = {
    name: "QwModal",
    props: { modelValue: Boolean, title: String, width: { type: String, default: "520px" }, closable: { type: Boolean, default: true }, maskClosable: { type: Boolean, default: true } },
    emits: ["update:modelValue", "close"],
    setup(props, { emit }) {
      const close = () => { if (!props.closable) return; emit("update:modelValue", false); emit("close"); };
      const maskClick = () => { if (props.maskClosable) close(); };
      useOverlay(props, close);
      return { close, maskClick };
    },
    template: `<teleport to="body">
      <transition name="qw-fade"><div v-if="modelValue" class="qw-mask"></div></transition>
      <transition name="qw-pop">
        <div v-if="modelValue" class="qw-modal-wrap" @click.self="maskClick">
          <div class="qw-modal" :style="{'--mw': width}" role="dialog" aria-modal="true">
            <header class="qw-modal-hd"><div class="qw-modal-title"><slot name="title">{{ title }}</slot></div><button v-if="closable" class="qw-iconbtn" @click="close" aria-label="关闭"><qw-icon name="x"/></button></header>
            <div class="qw-modal-bd"><slot/></div>
            <footer v-if="$slots.footer" class="qw-modal-ft"><slot name="footer"/></footer>
          </div>
        </div>
      </transition>
    </teleport>`,
  };

  // ------------------------------------------------------------------ 图表
  const EChart = {
    name: "QwChart",
    props: { option: Object, height: { type: String, default: "300px" }, group: String, notMerge: { type: Boolean, default: true } },
    emits: ["ready", "chart-click"],
    setup(props, { emit }) {
      const el = ref(null);
      let chart = null;
      let ro = null;
      let raf = 0;
      const apply = () => {
        if (!chart || !props.option) return;
        try { chart.setOption(props.option, { notMerge: props.notMerge }); } catch (e) { console.error("图表渲染失败", e); }
      };
      onMounted(() => {
        chart = markRaw(echarts.init(el.value, null, { renderer: "canvas" }));
        if (props.group) { chart.group = props.group; echarts.connect(props.group); }
        chart.on("click", (p) => emit("chart-click", p));
        apply();
        emit("ready", chart);
        ro = new ResizeObserver(() => {
          cancelAnimationFrame(raf);
          raf = requestAnimationFrame(() => { if (chart) chart.resize(); });
        });
        ro.observe(el.value);
      });
      watch(() => props.option, apply);
      onBeforeUnmount(() => {
        cancelAnimationFrame(raf);
        if (ro) ro.disconnect();
        if (chart) chart.dispose();
        chart = null;
      });
      return { el };
    },
    template: `<div ref="el" class="qw-echart" :style="{height}"></div>`,
  };

  // 十字光标在坐标轴上的数字：价格保留两位小数、成交量用万/亿
  const AP_PRICE = { label: { formatter: (p) => (isNum(Number(p.value)) ? Number(p.value).toFixed(2) : "") } };
  const AP_VOL = { label: { formatter: (p) => fmt.volShort(Number(p.value)) } };
  function maLine(closes, p) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < closes.length; i++) {
      sum += closes[i];
      if (i >= p) sum -= closes[i - p];
      out.push(i >= p - 1 ? +(sum / p).toFixed(3) : null);
    }
    return out;
  }
  const row = (k, v, color) => `<div style="display:flex;justify-content:space-between;gap:16px;line-height:1.7"><span style="opacity:.7">${k}</span><b style="font-weight:600${color ? ";color:" + color : ""}">${v}</b></div>`;
  const cornerPos = (pt, params, dom, rect, size) => {
    const w = size.contentSize[0];
    const vw = size.viewSize[0];
    return [pt[0] < vw / 2 ? vw - w - 8 : 8, 6];
  };

  const KlineChart = {
    name: "QwKline",
    props: {
      bars: { type: Array, default: () => [] }, limitDays: { type: Array, default: () => [] }, height: { type: String, default: "440px" },
      compact: Boolean, period: { type: String, default: "day" }, initialBars: { type: Number, default: 120 },
    },
    computed: {
      option() {
        const c = colors();
        const bars = this.bars || [];
        const n = bars.length;
        const dates = bars.map((b) => b.date);
        const closes = bars.map((b) => b.close);
        const lset = new Set(this.period === "day" ? this.limitDays || [] : []);
        const maDefs = [[5, c.series[3]], [10, c.series[0]], [20, c.series[4]], [60, c.series[5]]];
        const mas = maDefs.map(([p]) => maLine(closes, p));
        const H = parseInt(this.height, 10) || 420;
        const legendH = this.compact ? 6 : 28;
        const bottomReserve = (this.compact ? 0 : 34) + 22;
        const avail = H - legendH - bottomReserve - 8;
        const h1 = Math.round(avail * 0.74);
        const h2 = avail - h1 - 16;
        const left = 58;
        const right = 12;
        const start = n > this.initialBars ? (1 - this.initialBars / n) * 100 : 0;
        const ax = axisBase(c);
        const dateLabel = (v) => (this.period === "month" ? String(v).slice(0, 7) : String(v).slice(2, 10));
        const volData = bars.map((b, i) => {
          const prev = i > 0 ? bars[i - 1].close : b.open;
          const up = b.close > b.open || (b.close === b.open && b.close >= prev);
          return { value: b.volume, itemStyle: { color: up ? c.up : c.down, opacity: 0.7 } };
        });
        const period = this.period;
        const tooltip = {
          ...tooltipBase(c), trigger: "axis", confine: true, position: cornerPos,
          axisPointer: { type: "cross", label: { backgroundColor: c.text2, fontSize: 11 }, crossStyle: { color: c.text3 } },
          formatter: (ps) => {
            const p = ps.find((x) => x.seriesType === "candlestick") || ps[0];
            if (!p) return "";
            const i = p.dataIndex;
            const b = bars[i];
            if (!b) return "";
            const pc = i > 0 ? bars[i - 1].close : null;
            const chg = pc ? b.close / pc - 1 : null;
            const col = (v) => (pc == null ? null : v > pc ? c.up : v < pc ? c.down : null);
            let html = `<div style="font-weight:700;margin-bottom:4px">${b.date}${lset.has(b.date) ? ` <span style="color:${c.up}">▼涨停</span>` : ""}</div>`;
            html += row("开盘", b.open.toFixed(2), col(b.open)) + row("最高", b.high.toFixed(2), col(b.high)) +
              row("最低", b.low.toFixed(2), col(b.low)) + row("收盘", b.close.toFixed(2), col(b.close));
            if (chg != null) html += row(period === "day" ? "涨跌幅" : "区间涨跌", fmt.ratio(chg, 2, true), chg > 0 ? c.up : chg < 0 ? c.down : null);
            html += row("成交量", fmt.vol(b.volume)) + (b.amount ? row("成交额", fmt.money(b.amount)) : "");
            maDefs.forEach(([pp, color], k) => { if (mas[k][i] != null) html += row(`<span style="color:${color}">●</span> MA${pp}`, mas[k][i].toFixed(2)); });
            return html;
          },
        };
        const zoom = [{ type: "inside", xAxisIndex: [0, 1], start, end: 100, minValueSpan: 15 }];
        if (!this.compact) {
          zoom.push({
            type: "slider", xAxisIndex: [0, 1], start, end: 100, bottom: 6, height: 20, borderColor: c.border,
            backgroundColor: "transparent", fillerColor: c.dark ? "rgba(91,143,240,0.16)" : "rgba(42,111,219,0.10)",
            dataBackground: { lineStyle: { color: c.axis }, areaStyle: { color: c.surface3 } },
            selectedDataBackground: { lineStyle: { color: c.primary }, areaStyle: { color: c.primaryWeak } },
            handleStyle: { color: c.surface, borderColor: c.text3 }, moveHandleStyle: { color: c.axis },
            textStyle: { color: c.text3, fontSize: 10 }, labelFormatter: (v, s) => String(s).slice(0, 10),
          });
        }
        return {
          animation: false, textStyle: { fontFamily: c.font },
          legend: {
            show: !this.compact, top: 2, left: left - 6, itemWidth: 14, itemHeight: 3, icon: "rect",
            textStyle: { color: c.text2, fontSize: 12 }, inactiveColor: c.text3, data: maDefs.map(([p]) => "MA" + p),
          },
          tooltip, axisPointer: { link: [{ xAxisIndex: "all" }] },
          grid: [{ left, right, top: legendH, height: h1 }, { left, right, top: legendH + h1 + 16, height: h2 }],
          xAxis: [
            { type: "category", data: dates, gridIndex: 0, boundaryGap: true, ...ax, axisLabel: { show: false }, splitLine: { show: false }, axisPointer: { label: { show: false } } },
            { type: "category", data: dates, gridIndex: 1, boundaryGap: true, ...ax, axisLabel: { ...ax.axisLabel, formatter: dateLabel, hideOverlap: true }, splitLine: { show: false } },
          ],
          yAxis: [
            { scale: true, gridIndex: 0, splitNumber: 4, ...ax, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v) => v.toFixed(2) }, axisPointer: AP_PRICE },
            { scale: false, gridIndex: 1, splitNumber: 2, ...ax, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, showMaxLabel: false, formatter: (v) => fmt.volShort(v) }, axisPointer: AP_VOL },
          ],
          dataZoom: zoom,
          series: [
            {
              name: "K线", type: "candlestick", data: bars.map((b) => [b.open, b.close, b.low, b.high]), barMaxWidth: 14,
              itemStyle: { color: c.up, color0: c.down, borderColor: c.up, borderColor0: c.down },
            },
            ...maDefs.map(([p, color], k) => ({
              name: "MA" + p, type: "line", data: mas[k], showSymbol: false, smooth: false, z: 3,
              lineStyle: { width: 1.5, color }, itemStyle: { color }, emphasis: { disabled: true },
            })),
            {
              name: "涨停", type: "scatter", data: bars.filter((b) => lset.has(b.date)).map((b) => [b.date, b.high]),
              symbol: "triangle", symbolRotate: 180, symbolSize: 8, symbolOffset: [0, -9], itemStyle: { color: c.up }, z: 5,
              tooltip: { show: false }, emphasis: { disabled: true },
            },
            { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volData, barMaxWidth: 14 },
          ],
        };
      },
    },
    template: `<qw-chart :option="option" :height="height"/>`,
  };

  const MINUTE_SLOTS = (() => {
    const out = [];
    const push = (a, b) => {
      for (let t = a; t <= b; t++) out.push(String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0"));
    };
    push(9 * 60 + 30, 11 * 60 + 30);
    push(13 * 60 + 1, 15 * 60);
    return out;
  })();
  const MINUTE_LABELS = { "09:30": "09:30", "10:30": "10:30", "11:30": "11:30/13:00", "14:00": "14:00", "15:00": "15:00" };
  function normTime(t) {
    const s = String(t || "").replace(/^.*\s/, "");
    const m = s.match(/^(\d{1,2}):?(\d{2})/);
    return m ? m[1].padStart(2, "0") + ":" + m[2] : s;
  }

  const MinuteChart = {
    name: "QwMinute",
    props: { data: Object, height: { type: String, default: "400px" }, limitUp: Number, limitDown: Number },
    computed: {
      option() {
        const c = colors();
        const d = this.data || {};
        const prev = d.prev_close;
        const map = {};
        (d.points || []).forEach((p) => { map[normTime(p.time)] = p; });
        const prices = [];
        const avgs = [];
        const vols = [];
        let last = prev;
        MINUTE_SLOTS.forEach((t) => {
          const p = map[t];
          if (!p) { prices.push(null); avgs.push(null); vols.push(null); return; }
          prices.push(p.price);
          avgs.push(isNum(p.avg_price) ? p.avg_price : null);
          vols.push({ value: p.volume, itemStyle: { color: p.price >= last ? c.up : c.down, opacity: 0.75 } });
          last = p.price;
        });
        const valid = prices.filter(isNum);
        let dev = isNum(prev) ? prev * 0.01 : 0.01;
        valid.forEach((v) => { dev = Math.max(dev, Math.abs(v - prev)); });
        avgs.forEach((v) => { if (isNum(v)) dev = Math.max(dev, Math.abs(v - prev)); });
        dev *= 1.08;
        const ymin = isNum(prev) ? prev - dev : undefined;
        const ymax = isNum(prev) ? prev + dev : undefined;
        const H = parseInt(this.height, 10) || 400;
        const top = 30;
        const avail = H - top - 30;
        const h1 = Math.round(avail * 0.72);
        const h2 = avail - h1 - 14;
        const left = 58;
        const right = 12;
        const ax = axisBase(c);
        const labelColor = (v) => (!isNum(prev) ? c.text3 : v > prev + 1e-6 ? c.up : v < prev - 1e-6 ? c.down : c.text3);
        const pctOf = (v) => (isNum(prev) && prev ? v / prev - 1 : null);
        return {
          animation: false, textStyle: { fontFamily: c.font },
          legend: { top: 2, left: left - 6, itemWidth: 14, itemHeight: 3, icon: "rect", textStyle: { color: c.text2, fontSize: 12 }, data: ["价格", "均价"] },
          tooltip: {
            ...tooltipBase(c), trigger: "axis", confine: true, position: cornerPos,
            axisPointer: { type: "cross", label: { backgroundColor: c.text2, fontSize: 11 }, crossStyle: { color: c.text3 } },
            formatter: (ps) => {
              const i = ps[0] && ps[0].dataIndex;
              const t = MINUTE_SLOTS[i];
              const p = map[t];
              if (!p) return `<b>${t || ""}</b>`;
              const pc = pctOf(p.price);
              const col = pc > 0 ? c.up : pc < 0 ? c.down : null;
              return `<div style="font-weight:700;margin-bottom:4px">${t}</div>` + row("价格", p.price.toFixed(2), col) +
                row("涨跌幅", fmt.ratio(pc, 2, true), col) + (isNum(p.avg_price) ? row("均价", p.avg_price.toFixed(2)) : "") +
                row("成交量", fmt.vol(p.volume));
            },
          },
          axisPointer: { link: [{ xAxisIndex: "all" }] },
          grid: [{ left, right, top, height: h1 }, { left, right, top: top + h1 + 14, height: h2 }],
          xAxis: [
            { type: "category", data: MINUTE_SLOTS, gridIndex: 0, boundaryGap: false, ...ax, axisLabel: { show: false }, splitLine: { show: false }, axisPointer: { label: { show: false } } },
            {
              type: "category", data: MINUTE_SLOTS, gridIndex: 1, boundaryGap: false, ...ax, splitLine: { show: false },
              axisLabel: { ...ax.axisLabel, interval: (i, v) => v in MINUTE_LABELS, formatter: (v) => MINUTE_LABELS[v] || "", alignMinLabel: "left", alignMaxLabel: "right" },
            },
          ],
          yAxis: [
            {
              gridIndex: 0, min: ymin, max: ymax, interval: dev / 2, ...ax, axisLine: { show: false },
              axisLabel: { ...ax.axisLabel, color: labelColor, formatter: (v) => v.toFixed(2), showMinLabel: true, showMaxLabel: true }, axisPointer: AP_PRICE,
            },
            { gridIndex: 1, splitNumber: 2, ...ax, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { ...ax.axisLabel, showMaxLabel: false, formatter: (v) => fmt.volShort(v) }, axisPointer: AP_VOL },
          ],
          series: [
            {
              name: "价格", type: "line", data: prices, showSymbol: false, connectNulls: false, lineStyle: { width: 2, color: c.series[0] }, itemStyle: { color: c.series[0] },
              areaStyle: { color: c.series[0], opacity: 0.07, origin: "start" },
              markLine: isNum(prev) ? {
                symbol: "none", silent: true, lineStyle: { type: "dashed", color: c.text3, width: 1 },
                label: { formatter: "昨收 " + prev.toFixed(2), position: "insideEndTop", color: c.text3, fontSize: 11 },
                data: [{ yAxis: prev }],
              } : undefined,
            },
            { name: "均价", type: "line", data: avgs, showSymbol: false, lineStyle: { width: 1.5, color: c.series[3] }, itemStyle: { color: c.series[3] } },
            { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols, barWidth: "60%" },
          ],
        };
      },
    },
    template: `<qw-chart :option="option" :height="height"/>`,
  };

  const Gauge = {
    name: "QwGauge",
    props: { value: Number, label: String, height: { type: String, default: "220px" } },
    computed: {
      option() {
        const c = colors();
        const v = isNum(this.value) ? Math.round(this.value * 10) / 10 : 0;
        const b = c.bands;
        return {
          textStyle: { fontFamily: c.font },
          series: [{
            type: "gauge", startAngle: 205, endAngle: -25, min: 0, max: 100, splitNumber: 5, radius: "88%", center: ["50%", "55%"],
            axisLine: { lineStyle: { width: 14, color: [[0.2, b[0]], [0.4, b[1]], [0.6, b[2]], [0.8, b[3]], [1, b[4]]] } },
            pointer: { length: "58%", width: 5, itemStyle: { color: c.text } },
            anchor: { show: true, size: 12, itemStyle: { color: c.surface, borderColor: c.text, borderWidth: 3 } },
            axisTick: { distance: -14, length: 5, splitNumber: 4, lineStyle: { color: c.surface, width: 1 } },
            splitLine: { distance: -14, length: 14, lineStyle: { color: c.surface, width: 3 } },
            axisLabel: { distance: 20, color: c.text3, fontSize: 11 },
            title: { offsetCenter: [0, "74%"], color: c.text2, fontSize: 15, fontWeight: 600 },
            detail: { valueAnimation: true, offsetCenter: [0, "44%"], fontSize: 32, fontWeight: 700, color: c.text, formatter: (x) => Math.round(x) },
            data: [{ value: v, name: this.label || "" }],
          }],
        };
      },
    },
    template: `<qw-chart :option="option" :height="height"/>`,
  };

  const RadarChart = {
    name: "QwRadar",
    props: {
      dims: Object, keys: Array, height: { type: String, default: "260px" }, neutral: { type: Boolean, default: true },
    },
    computed: {
      useKeys() {
        if (this.keys) return this.keys;
        const base = ["sentiment", "capital", "fundamental", "theme", "technical"];
        return this.dims && isNum(this.dims.news) ? base.concat(["news"]) : base;
      },
      option() {
        const c = colors();
        const d = this.dims || {};
        const keys = this.useKeys;
        const data = [{
          value: keys.map((k) => (isNum(d[k]) ? Math.round(d[k]) : 50)), name: "五维评分",
          lineStyle: { width: 2, color: c.series[1] }, itemStyle: { color: c.series[1] }, areaStyle: { color: c.series[1], opacity: 0.16 }, symbolSize: 5,
        }];
        if (this.neutral) {
          data.push({ value: keys.map(() => 50), name: "中性线(50)", lineStyle: { width: 1, type: "dashed", color: c.text3 }, itemStyle: { color: c.text3 }, symbol: "none", areaStyle: { opacity: 0 } });
        }
        return {
          textStyle: { fontFamily: c.font },
          tooltip: { ...tooltipBase(c), trigger: "item", confine: true },
          legend: { show: this.neutral, bottom: 0, itemWidth: 14, itemHeight: 3, icon: "rect", textStyle: { color: c.text2, fontSize: 12 }, data: data.map((x) => x.name) },
          radar: {
            center: ["50%", this.neutral ? "46%" : "52%"], radius: "62%", splitNumber: 4,
            indicator: keys.map((k) => ({ name: LABELS[k] || k, max: 100, min: 0 })),
            axisName: { color: c.text2, fontSize: 12 },
            splitLine: { lineStyle: { color: c.grid } }, splitArea: { show: false }, axisLine: { lineStyle: { color: c.grid } },
          },
          series: [{ type: "radar", data, emphasis: { lineStyle: { width: 2 } } }],
        };
      },
    },
    template: `<qw-chart :option="option" :height="height"/>`,
  };

  // ------------------------------------------------------------------ 任务进度
  const JobProgress = {
    name: "QwJob",
    props: { jobId: String, title: String },
    emits: ["done", "failed"],
    setup(props, { emit }) {
      const showLogs = ref(false);
      const job = computed(() => (props.jobId ? store.jobs[props.jobId] : null));
      watch(() => props.jobId, (id) => { if (id) jobs.track(id, { title: props.title }); }, { immediate: true });
      watch(() => job.value && job.value.status, (s, old) => {
        if (s === old || !job.value) return;
        if (s === "done") emit("done", job.value);
        if (s === "failed") emit("failed", job.value);
      });
      const pct = computed(() => Math.round(((job.value && job.value.progress) || 0) * 100));
      return { showLogs, job, pct };
    },
    template: `<div v-if="job" class="qw-job" :class="job.status">
      <div class="qw-job-hd"><span>{{ job.title || title }}<span v-if="job.status === 'done'" class="muted"> · 已完成</span><span v-if="job.status === 'failed'" class="up"> · 失败</span></span><span class="num">{{ pct }}%</span></div>
      <div class="qw-progress"><i :style="{width: pct + '%'}"></i></div>
      <div class="qw-job-msg"><span>{{ job.status === 'failed' ? (job.error || job.message) : job.message }}</span><button v-if="job.logs && job.logs.length" class="linkbtn" @click="showLogs = !showLogs">{{ showLogs ? '收起日志' : '查看日志' }}</button></div>
      <pre v-if="showLogs" class="qw-job-logs">{{ (job.logs || []).slice(-60).join('\\n') }}</pre>
    </div>`,
  };

  // ------------------------------------------------------------------ 股票搜索
  const StockSearch = {
    name: "QwStockSearch",
    props: { placeholder: { type: String, default: "搜股票：代码/名称/拼音" }, size: String, autofocus: Boolean, navigate: { type: Boolean, default: true } },
    emits: ["select"],
    setup(props, { emit }) {
      const q = ref("");
      const items = ref([]);
      const open = ref(false);
      const active = ref(0);
      const loading = ref(false);
      const input = ref(null);
      let timer = null;
      let seq = 0;
      const search = async () => {
        const text = q.value.trim();
        if (!text) { items.value = []; loading.value = false; return; }
        const my = ++seq;
        loading.value = true;
        try {
          const res = await api.get("/api/stock/search", { q: text }, { silent: true });
          if (my !== seq) return;
          items.value = Array.isArray(res) ? res.slice(0, 12) : [];
          active.value = 0;
        } catch (e) {
          if (my === seq) items.value = [];
        } finally {
          if (my === seq) loading.value = false;
        }
      };
      const onInput = () => { open.value = true; clearTimeout(timer); timer = setTimeout(search, 180); };
      const choose = (it) => {
        emit("select", it);
        if (props.navigate) go("/watch/" + it.code);
        q.value = "";
        items.value = [];
        open.value = false;
        if (input.value) input.value.blur();
      };
      const onEnter = () => {
        if (items.value[active.value]) choose(items.value[active.value]);
        else if (/^\d{6}$/.test(q.value.trim())) choose({ code: q.value.trim(), name: "" });
      };
      const move = (d) => {
        if (!items.value.length) return;
        active.value = (active.value + d + items.value.length) % items.value.length;
      };
      const onBlur = () => setTimeout(() => { open.value = false; }, 150);
      onMounted(() => { if (props.autofocus && input.value) input.value.focus(); });
      onBeforeUnmount(() => clearTimeout(timer));
      return { q, items, open, active, loading, input, onInput, choose, onEnter, move, onBlur, fmt };
    },
    template: `<div class="qw-search" :class="size">
      <label class="qw-search-box"><qw-icon name="search" :size="size === 'lg' ? 20 : 16"/>
        <input ref="input" v-model="q" type="search" :placeholder="placeholder" autocomplete="off" spellcheck="false" aria-label="搜索股票"
          @input="onInput" @focus="open = true" @blur="onBlur" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.enter.prevent="onEnter" @keydown.esc="open = false; $event.target.blur()">
      </label>
      <div v-if="open && q.trim()" class="qw-search-pop" role="listbox">
        <div v-for="(it, i) in items" :key="it.code" class="qw-search-item" :class="{active: i === active}" role="option" @mousedown.prevent="choose(it)" @mouseenter="active = i">
          <b>{{ it.name }}</b><span class="code">{{ it.code }}</span><qw-board :board="it.board"/><span class="ind ellipsis">{{ it.industry ? fmt.industry(it.industry) : '' }}</span>
        </div>
        <div v-if="!items.length" class="qw-search-hint">{{ loading ? '正在搜索…' : (/^\\d{6}$/.test(q.trim()) ? '按回车直接打开 ' + q.trim() : '没有找到匹配的股票，试试代码或拼音首字母') }}</div>
      </div>
    </div>`,
  };

  // ------------------------------------------------------------------ 页面占位
  const Placeholder = {
    props: ["params", "query"],
    template: `<qw-card><qw-empty icon="clock" title="这个页面还在建设中" desc="功能马上就来，先去看看市场情绪吧。" action-text="回到市场情绪" action-to="/"/></qw-card>`,
  };
  const NotFound = {
    props: ["params", "query"],
    template: `<qw-card><qw-empty icon="alert" title="页面不存在" desc="这个地址没有对应的页面。" action-text="回到首页" action-to="/"/></qw-card>`,
  };

  // ------------------------------------------------------------------ 外壳
  const App = {
    setup() {
      const moreOpen = ref(false);
      const searchOpen = ref(false);
      const navGroups = groupRoutes(ROUTES);
      const tabRoutes = ROUTES.filter((r) => r.tab && !r.hidden);
      const moreRoutes = ROUTES.filter((r) => !r.tab && !r.hidden);
      const moreGroups = groupRoutes(moreRoutes);
      const pageComp = computed(() => {
        if (route.name === "notfound") return NotFound;
        return QW.pages[route.name] || Placeholder;
      });
      const pageKey = computed(() => route.name + ":" + JSON.stringify(route.params));
      const pageTitle = computed(() => store.pageTitle || route.title);
      watch(pageTitle, (t) => { document.title = `${t} · 量化助手`; }, { immediate: true });
      const phaseCls = computed(() => ({ 交易中: "live", 午间休市: "noon", 盘前: "pre" })[store.phase] || "");
      const running = computed(() => jobs.running());
      const runPct = computed(() => {
        const r = running.value;
        if (!r.length) return 0;
        return Math.round(Math.max(...r.map((j) => j.progress || 0)) * 100);
      });
      const runTip = computed(() => running.value.map((j) => `${j.title || "任务"}：${Math.round((j.progress || 0) * 100)}% ${j.message || ""}`).join("\n"));
      const updating = computed(() => running.value.some((j) => ["daily", "update"].includes(j.name) || /更新/.test(j.title || "")));
      const oneClick = async () => {
        if (updating.value) { toast.info("正在更新中，请稍候…"); return; }
        try { await jobs.start("/api/jobs/daily", {}, "一键更新"); } catch (e) { /* 已提示 */ }
      };
      const moreActive = computed(() => moreRoutes.some((r) => r.name === route.name));
      const readHidden = () => { try { return sessionStorage.getItem("qw-onboard-later") === "1"; } catch (e) { return false; } };
      const onboardHidden = ref(readHidden());
      const hideOnboard = () => {
        onboardHidden.value = true;
        try { sessionStorage.setItem("qw-onboard-later", "1"); } catch (e) { /* 忽略 */ }
      };
      watch(() => route.path, () => { moreOpen.value = false; searchOpen.value = false; });
      const tipRef = (el) => { tipEl = el; };
      return {
        store, route, tip, tipRef, toastState, closeToast, navGroups, tabRoutes, moreRoutes, moreGroups, pageComp, pageKey, pageTitle,
        phaseCls, running, runPct, runTip, updating, oneClick, toggleTheme, moreOpen, searchOpen, moreActive, fmt,
        onboardHidden, hideOnboard,
      };
    },
    template: `<div class="qw-app">
      <aside v-if="!store.isPhone" class="qw-side">
        <a class="qw-logo" href="#/"><span class="qw-logo-mark"><qw-icon name="trend" :size="19"/></span><span class="qw-logo-text">量化助手</span></a>
        <nav class="qw-nav" aria-label="主导航">
          <template v-for="(g, gi) in navGroups" :key="g.key">
            <div class="qw-nav-group" :class="{first: gi === 0}">{{ g.title }}</div>
            <a v-for="r in g.routes" :key="r.name" :href="'#' + (r.nav || r.path)" class="qw-nav-item" :class="{active: route.name === r.name}" v-tip.right="store.isNarrow ? r.title : ''"><qw-icon :name="r.icon" :size="19"/><span>{{ r.title }}</span></a>
          </template>
        </nav>
        <div class="qw-side-ft">数据仅供学习研究<br>不构成投资建议</div>
      </aside>
      <div class="qw-main">
        <header class="qw-top">
          <h1 class="qw-page-title">{{ pageTitle }}</h1>
          <div class="qw-top-status">
            <span v-if="!store.isPhone" class="qw-clock num"><small>北京时间</small>{{ store.clock }}</span>
            <span v-if="store.phase" class="qw-phase" :class="phaseCls" v-tip="'A股交易时间：工作日 9:30-11:30、13:00-15:00（北京时间）'"><i class="dot"></i>{{ store.phase }}</span>
            <span v-if="store.dataDate && !store.isPhone" class="qw-datadate" v-tip="'本地日线数据的最新交易日'"><span class="qw-datadate-label">数据 </span>{{ fmt.cnDate(store.dataDate) }}</span>
          </div>
          <div class="qw-top-right">
            <qw-stock-search v-if="!store.isPhone"/>
            <button v-else class="qw-iconbtn" @click="searchOpen = true" aria-label="搜索股票"><qw-icon name="search"/></button>
            <button class="btn primary" :class="{sm: store.isPhone}" @click="oneClick" :disabled="updating" v-tip="'下载最新行情、更新涨停数据并生成今天的预测'">
              <qw-icon name="refresh" :size="15" :class="{spin: updating}"/><span v-if="!store.isPhone || updating">{{ updating ? '更新中 ' + runPct + '%' : '一键更新' }}</span>
            </button>
            <button class="qw-iconbtn" @click="toggleTheme" :aria-label="store.theme === 'dark' ? '切换到浅色' : '切换到深色'" v-tip="store.theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'"><qw-icon :name="store.theme === 'dark' ? 'sun' : 'moon'"/></button>
          </div>
          <div v-if="running.length" class="qw-top-progress" :class="{indet: runPct < 2}" v-tip="runTip"><i :style="{width: runPct + '%'}"></i></div>
        </header>
        <div v-if="store.offline" class="qw-offline"><qw-icon name="alert" :size="16"/>连接不到本地服务，请确认“量化助手”程序还开着（关掉黑色窗口会停止服务）。</div>
        <div v-if="store.needOnboard && !onboardHidden && route.name !== 'guide'" class="qw-onboard">
          <qw-icon name="book" :size="16"/><span>第一次使用？花 1 分钟完成新手向导：告诉程序你的资金、能买的板块和风险承受度，它会据此给出仓位和止损建议。</span>
          <a class="btn sm primary" href="#/guide">去设置</a><button class="btn sm ghost" @click="hideOnboard">以后再说</button>
        </div>
        <main class="qw-content"><component :is="pageComp" :key="pageKey" :params="route.params" :query="route.query"/></main>
      </div>
      <nav v-if="store.isPhone" class="qw-tabbar" aria-label="主导航">
        <a v-for="r in tabRoutes" :key="r.name" :href="'#' + (r.nav || r.path)" :class="{active: route.name === r.name}"><qw-icon :name="r.icon" :size="21"/>{{ r.title }}</a>
        <button :class="{active: moreActive}" @click="moreOpen = true"><qw-icon name="grid" :size="21"/>更多</button>
      </nav>
      <qw-drawer v-model="moreOpen" title="更多功能">
        <template v-for="g in moreGroups" :key="g.key">
          <div class="qw-more-group">{{ g.title }}</div>
          <div class="qw-more-list">
            <a v-for="r in g.routes" :key="r.name" :href="'#' + (r.nav || r.path)" :class="{active: route.name === r.name}"><qw-icon :name="r.icon" :size="22"/>{{ r.title }}</a>
          </div>
        </template>
        <div class="section-gap row between"><span class="muted">深色模式</span><button class="btn sm" @click="toggleTheme">{{ store.theme === 'dark' ? '切换到浅色' : '切换到深色' }}</button></div>
        <p class="muted section-gap" style="font-size:12px">北京时间 {{ store.clock }} · 数据 {{ fmt.cnDate(store.dataDate) }}<br>数据仅供学习研究，不构成投资建议。</p>
      </qw-drawer>
      <teleport to="body">
        <div v-if="searchOpen" class="qw-search-overlay">
          <div class="row"><qw-stock-search autofocus style="flex:1" @select="searchOpen = false"/><button class="btn ghost" @click="searchOpen = false">取消</button></div>
          <p class="muted" style="font-size:13px">输入 6 位代码、股票名称或拼音首字母，例如 600519、茅台、gzmt</p>
        </div>
      </teleport>
      <teleport to="body">
        <div class="qw-toasts" aria-live="polite">
          <transition-group name="qw-toast">
            <div v-for="t in toastState.list" :key="t.id" class="qw-toast" :class="t.type" role="status">
              <qw-icon :name="t.type === 'success' ? 'checkCircle' : t.type === 'error' ? 'xCircle' : t.type === 'warn' ? 'alert' : 'info'" :size="17"/>
              <span>{{ t.text }}</span><button class="qw-toast-x" @click="closeToast(t.id)" aria-label="关闭">×</button>
            </div>
          </transition-group>
        </div>
        <div v-show="tip.show" :ref="tipRef" class="qw-tip" :style="{left: tip.x + 'px', top: tip.y + 'px'}" role="tooltip">{{ tip.text }}</div>
      </teleport>
    </div>`,
  };

  // ------------------------------------------------------------------ 时钟
  const clockFmt = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const dayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
  function tickClock() {
    const now = new Date();
    store.clock = clockFmt.format(now);
    store.today = dayFmt.format(now);
  }

  // ------------------------------------------------------------------ 导出与启动
  Object.assign(QW, {
    api, toast, fmt, store, route, go, setTitle, bus, jobs, watch: watchApi, recent, usePoll, colors, tooltipBase, axisBase,
    refreshStatus, LABELS, DIM_HELP, BOARDS, ROUTES, NAV_GROUPS, ICONS, tip: { show: showTip, hide: hideTip }, isNum, ONE_WORD_TIP, AP_PRICE, AP_VOL,
    KINDS, KIND_VALUES, kindInfo, kindLabel, normTrade, tWord, T_HELP, T_NW_NOTE, OOS_CAVEAT, tHelp, SKIP_HEAVY, UNCAPPED_HELP, BASE_HELP, MATCH_HELP, TUNED_HELP,
    fillPct, spanMonths, holdLabel, recentVerdict, HOLD_CAVEAT,
  });

  function boot() {
    const app = createApp(App);
    app.config.globalProperties.$fmt = fmt;
    app.config.globalProperties.$go = go;
    app.config.globalProperties.$store = store;
    app.config.globalProperties.$labels = LABELS;
    app.config.errorHandler = (err, inst, info) => {
      console.error("页面出错", info, err);
      toast.error("页面出了点问题：" + (err && err.message ? err.message : String(err)));
    };
    app.directive("tip", tipDirective);
    app.directive("hscroll", hscrollDirective);
    const comps = {
      "qw-icon": Icon, "qw-help": HelpTip, "qw-skeleton": Skeleton, "qw-card": Card, "qw-stat": Stat, "qw-empty": Empty,
      "qw-price": PriceText, "qw-pctbar": PctBar, "qw-minibars": MiniBars, "qw-stock": StockLink, "qw-board": BoardTag,
      "qw-streak": StreakBadge, "qw-table": DataTable, "qw-tabs": Tabs, "qw-segmented": Segmented, "qw-drawer": Drawer,
      "qw-modal": Modal, "qw-chart": EChart, "qw-kline": KlineChart, "qw-minute": MinuteChart, "qw-gauge": Gauge,
      "qw-radar": RadarChart, "qw-job": JobProgress, "qw-stock-search": StockSearch,
    };
    Object.keys(comps).forEach((k) => app.component(k, comps[k]));
    (QW.extraComponents || []).forEach(([k, c]) => app.component(k, c));
    tickClock();
    setInterval(tickClock, 1000);
    window.addEventListener("resize", () => { store.width = window.innerWidth; hideTip(); });
    refreshStatus();
    api.get("/api/settings", null, { silent: true }).then((s) => {
      store.profile = (s && s.profile) || null;
      store.needOnboard = !!(store.profile && !store.profile.onboarded);
    }).catch(() => { /* 读不到设置时不提醒 */ });
    statusTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
    watchApi.refresh();
    app.mount("#app");
    QW.app = app;
  }
  QW.statusTimer = () => statusTimer;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else setTimeout(boot, 0);
})();
