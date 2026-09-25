/* 消息政策 #/news：快讯流（政策标签、相关股票）、标签筛选、关键词搜索、每60秒自动刷新、政策热度与热门个股 */
(function () {
  "use strict";
  const { ref, computed, watch, onMounted } = Vue;
  const { api, fmt, store, usePoll, isNum } = QW;

  const TAG_CLS = {
    国家政策: "t-red", 部委: "t-orange", 产业扶持: "t-gold", 货币财政: "t-purple", 地方政策: "t-teal",
    监管: "t-gray", 国际: "t-blue", 产业热点: "t-pink",
  };
  const TAG_HELP = {
    国家政策: "国务院、中央会议等国家层面的政策和表态。",
    部委: "发改委、工信部、财政部等各部委发布的消息。",
    产业扶持: "补贴、规划、专项资金、国产替代等对某个产业的支持。",
    货币财政: "降准降息、财政支出、国债等资金面的消息。",
    地方政策: "省市地方政府出台的政策。",
    监管: "证监会、交易所的监管动作，处罚、问询、减持等。",
    国际: "海外市场、国际关系等外部消息。",
    产业热点: "新技术、新产品等市场关注的热门产业话题。",
  };
  const PAGE = 60;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const hl = (text, kw) => {
    const t = esc(text);
    if (!kw) return t;
    const k = esc(kw).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return t.replace(new RegExp(k, "gi"), (m) => `<mark>${m}</mark>`);
  };
  const keyOf = (it) => (it.time || "") + "|" + (it.title || it.content || "").slice(0, 40);

  QW.page("news", {
    props: ["params", "query"],
    setup(props) {
      const items = ref([]);
      const loading = ref(true);
      const err = ref("");
      const updatedAt = ref("");
      const tag = ref((props.query && props.query.tag) || "");
      const kw = ref((props.query && props.query.q) || "");
      const onlyImportant = ref(false);
      const onlyStocks = ref(false);
      const open = ref({});
      const shown = ref(PAGE);
      const fresh = ref({});
      let seen = null;

      const load = async () => {
        try {
          const list = await api.get("/api/news/flash", { limit: 200 }, { silent: true });
          const arr = (Array.isArray(list) ? list : []).map((it) => ({ ...it, _k: keyOf(it), tags: it.tags || [], stocks: it.stocks || [] }));
          if (seen) {
            const nf = {};
            arr.forEach((it) => { if (!seen.has(it._k)) nf[it._k] = true; });
            fresh.value = nf;
          }
          seen = new Set(arr.map((it) => it._k));
          items.value = arr;
          err.value = "";
          updatedAt.value = store.clock;
        } catch (e) {
          err.value = e.detail || e.message;
        } finally {
          loading.value = false;
        }
      };
      onMounted(load);
      usePoll(load, 60000, { live: false });
      watch([tag, kw, onlyImportant, onlyStocks], () => { shown.value = PAGE; });

      const allTags = computed(() => {
        const cnt = {};
        items.value.forEach((it) => it.tags.forEach((t) => { cnt[t] = (cnt[t] || 0) + 1; }));
        const order = Object.keys(TAG_CLS);
        return Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a] || order.indexOf(a) - order.indexOf(b)).map((t) => ({ t, n: cnt[t] }));
      });
      const tagMax = computed(() => Math.max(1, ...allTags.value.map((x) => x.n)));
      const hasImportant = computed(() => items.value.some((it) => it.important));
      const filtered = computed(() => {
        const k = kw.value.trim().toLowerCase();
        return items.value.filter((it) => {
          if (tag.value && !it.tags.includes(tag.value)) return false;
          if (onlyImportant.value && !it.important) return false;
          if (onlyStocks.value && !it.stocks.length) return false;
          if (k) {
            const hay = ((it.title || "") + " " + (it.content || "") + " " + it.stocks.map((s) => s.name + s.code).join(" ")).toLowerCase();
            if (!hay.includes(k)) return false;
          }
          return true;
        });
      });
      const dayLabel = (t) => {
        const d = String(t || "").slice(0, 10);
        if (!d) return "";
        if (d === store.today) return "今天";
        const y = new Date(Date.parse(store.today) - 86400000).toISOString().slice(0, 10);
        return d === y ? "昨天" : fmt.cnDate(d);
      };
      const groups = computed(() => {
        const out = [];
        filtered.value.slice(0, shown.value).forEach((it) => {
          const lab = dayLabel(it.time) || "更早";
          if (!out.length || out[out.length - 1].label !== lab) out.push({ label: lab, items: [] });
          out[out.length - 1].items.push(it);
        });
        return out;
      });
      const hotStocks = computed(() => {
        const cnt = {};
        items.value.forEach((it) => it.stocks.forEach((s) => {
          const c = cnt[s.code] || (cnt[s.code] = { code: s.code, name: s.name, n: 0 });
          c.n += 1;
        }));
        return Object.values(cnt).sort((a, b) => b.n - a.n).slice(0, 12);
      });
      const bodyOf = (it) => {
        const c = String(it.content || "").trim();
        const t = String(it.title || "").trim();
        if (!c || c === t) return "";
        const strip = c.replace(/^【[^】]*】/, "").trim();
        return strip.startsWith(t) && strip.length - t.length < 6 ? "" : c;
      };
      const titleOf = (it) => String(it.title || "").trim() || String(it.content || "").slice(0, 48);
      const toggle = (it) => { open.value = { ...open.value, [it._k]: !open.value[it._k] }; };
      const setTag = (t) => { tag.value = tag.value === t ? "" : t; };
      const clearAll = () => { tag.value = ""; kw.value = ""; onlyImportant.value = false; onlyStocks.value = false; };
      const policyCount = computed(() => items.value.filter((it) => it.tags.some((t) => t !== "产业热点" && t !== "国际")).length);

      return {
        store, fmt, TAG_CLS, TAG_HELP, items, loading, err, load, updatedAt, tag, kw, onlyImportant, onlyStocks, open, shown, fresh, PAGE,
        allTags, tagMax, hasImportant, filtered, groups, hotStocks, bodyOf, titleOf, toggle, setTag, clearAll, policyCount, hl, isNum,
      };
    },
    template: `<div class="news-page">
      <div class="news-main stack">
        <div class="news-toolbar">
          <label class="news-search"><qw-icon name="search" :size="16"/><input v-model="kw" type="search" placeholder="搜索关键词，例如 芯片、降准、机器人" aria-label="搜索快讯"></label>
          <div class="news-meta">
            <span class="qw-tag"><i class="live-dot"></i>每 60 秒自动刷新</span>
            <span v-if="updatedAt" class="muted num">更新于 {{ updatedAt.slice(0, 5) }}</span>
            <button class="btn sm" @click="load"><qw-icon name="refresh" :size="14"/>刷新</button>
          </div>
        </div>
        <div class="filter-chips news-tags">
          <button class="chip" :class="{active: !tag}" @click="tag = ''">全部 <span class="muted num">{{ items.length }}</span></button>
          <button v-for="x in allTags" :key="x.t" class="chip" :class="[{active: tag === x.t}]" @click="setTag(x.t)" v-tip="TAG_HELP[x.t] || ''"><i class="tag-dot" :class="TAG_CLS[x.t] || 't-gray'"></i>{{ x.t }} <span class="muted num">{{ x.n }}</span></button>
          <span class="grow"></span>
          <span v-if="tag || kw.trim() || onlyImportant || onlyStocks" class="muted news-found">找到 <b class="num">{{ filtered.length }}</b> 条<button class="linkbtn" style="margin-left:6px" @click="clearAll">清除</button></span>
          <button v-if="hasImportant" class="chip" :class="{active: onlyImportant}" @click="onlyImportant = !onlyImportant"><qw-icon name="flame" :size="13"/>只看重要</button>
          <button class="chip" :class="{active: onlyStocks}" @click="onlyStocks = !onlyStocks"><qw-icon name="candle" :size="13"/>有相关股票</button>
        </div>

        <p class="news-note-m muted"><qw-icon name="info" :size="13"/>消息面只有实时数据，不参与模型训练和回测，只作为今天名单的参考加分。</p>
        <qw-card v-if="loading"><qw-skeleton :rows="10"/></qw-card>
        <qw-card v-else-if="err && !items.length"><qw-empty icon="alert" title="快讯暂时拿不到" :desc="err" action-text="重新加载" @action="load"/></qw-card>
        <qw-card v-else-if="!items.length"><qw-empty icon="news" title="暂时没有快讯" desc="新闻源可能暂时没有更新，一分钟后会自动再试。" action-text="立即刷新" @action="load"/></qw-card>
        <qw-card v-else-if="!filtered.length"><qw-empty icon="filter" title="没有符合条件的快讯" desc="换个关键词，或者取消筛选看看。" action-text="清除筛选" @action="clearAll"/></qw-card>

        <div v-else class="news-stream">
          <template v-for="g in groups" :key="g.label">
            <div class="news-day">{{ g.label }}</div>
            <article v-for="it in g.items" :key="it._k" class="news-item" :class="{imp: it.important, fresh: fresh[it._k]}">
              <div class="ni-time num">{{ $fmt.time(it.time) }}</div>
              <div class="ni-body">
                <div class="ni-title" @click="bodyOf(it) && toggle(it)" :class="{clickable: !!bodyOf(it)}">
                  <span v-if="fresh[it._k]" class="qw-tag red">新</span>
                  <span v-html="hl(titleOf(it), kw.trim())"></span>
                </div>
                <div v-if="bodyOf(it)" class="ni-content" :class="{open: open[it._k]}" v-html="hl(bodyOf(it), kw.trim())" @click="toggle(it)"></div>
                <div class="ni-foot">
                  <span v-for="t in it.tags" :key="t" class="ntag" :class="TAG_CLS[t] || 't-gray'" @click="setTag(t)" v-tip="TAG_HELP[t] || ''">{{ t }}</span>
                  <a v-for="s in it.stocks" :key="s.code" class="chip ni-stock" :href="'#/watch/' + s.code" v-tip="'看盘：' + s.name + ' ' + s.code"><qw-icon name="candle" :size="12"/>{{ s.name || s.code }}</a>
                  <span class="grow"></span>
                  <button v-if="bodyOf(it)" class="linkbtn ni-more" @click="toggle(it)">{{ open[it._k] ? '收起' : '展开全文' }}</button>
                  <span v-if="it.source" class="muted ni-src">{{ it.source }}</span>
                  <a v-if="it.url" class="linkbtn ni-src" :href="it.url" target="_blank" rel="noopener noreferrer">原文<qw-icon name="external" :size="12"/></a>
                </div>
              </div>
            </article>
          </template>
          <div v-if="filtered.length > shown" class="news-more"><button class="btn" @click="shown += PAGE">再看 {{ Math.min(PAGE, filtered.length - shown) }} 条（还有 {{ filtered.length - shown }} 条）</button></div>
          <p v-else class="muted news-end">没有更多了，只显示最近 {{ items.length }} 条快讯</p>
        </div>
      </div>

      <aside class="news-side stack">
        <qw-card title="政策热度" icon="flame" :sub="'最近 ' + items.length + ' 条快讯里'">
          <p class="side-big"><b class="num">{{ policyCount }}</b> 条和政策相关</p>
          <ul class="tag-bars">
            <li v-for="x in allTags" :key="x.t" :class="{active: tag === x.t}" @click="setTag(x.t)">
              <span class="tb-name"><i class="tag-dot" :class="TAG_CLS[x.t] || 't-gray'"></i>{{ x.t }}</span>
              <span class="tb-bar"><i :class="TAG_CLS[x.t] || 't-gray'" :style="{width: (x.n / tagMax * 100) + '%'}"></i></span>
              <span class="num tb-n">{{ x.n }}</span>
            </li>
          </ul>
          <p v-if="!allTags.length" class="muted">暂时没有带标签的快讯。</p>
        </qw-card>
        <qw-card title="被提到最多的股票" icon="candle">
          <div v-if="hotStocks.length" class="chips-wrap"><a v-for="s in hotStocks" :key="s.code" class="chip" :href="'#/watch/' + s.code">{{ s.name || s.code }} <span class="muted num">{{ s.n }}次</span></a></div>
          <p v-else class="muted">快讯里暂时没有点名具体股票。</p>
        </qw-card>
        <div class="qw-banner info news-note"><qw-icon name="info" :size="18"/><div class="qw-banner-body">消息面只有最近的实时数据，没有历史，所以<b>不参与模型训练和回测</b>。消息热度≠利好（新闻多也可能是利空），所以<b>默认不参与名单排序</b>（权重 0）；想试试的话，可以在<a href="#/settings">预测设置</a>里调高权重。</div></div>
      </aside>
    </div>`,
  });
})();
