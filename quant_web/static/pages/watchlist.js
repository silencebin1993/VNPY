/* 自选股 #/watchlist：搜索添加、实时行情表（交易时间自动刷新）、备注、移除、点击进入看盘 */
(function () {
  "use strict";
  const { ref, computed, onMounted, onBeforeUnmount } = Vue;
  const { api, fmt, store, bus, toast, go, usePoll, isNum, recent } = QW;

  const HELP = {
    turn: "换手率：今天成交的股数占流通股的比例，越高说明买卖越活跃。",
    fcap: "流通市值：能在市场上自由买卖的股票总价值。",
    pe: "市盈率：股价 ÷ 每股收益，大致表示“按现在的盈利多少年回本”。负数表示公司在亏损。",
    amount: "今天一共成交了多少钱。",
  };
  const EPS = 0.005;

  QW.page("watchlist", {
    props: ["params", "query"],
    setup() {
      const rows = ref(store.watchList && store.watchList.length ? store.watchList.slice() : []);
      const loading = ref(!rows.value.length);
      const err = ref("");
      const updatedAt = ref("");
      const busy = ref({});
      const noteOpen = ref(false);
      const noteRow = ref(null);
      const noteText = ref("");
      const recents = ref([]);

      const setRows = (list) => {
        rows.value = Array.isArray(list) ? list : [];
        store.watchList = rows.value.slice();
        store.watchCodes = rows.value.map((x) => x.code);
        updatedAt.value = store.clock;
      };
      const load = async () => {
        try {
          setRows(await api.get("/api/watchlist", null, { silent: true }));
          err.value = "";
        } catch (e) {
          err.value = e.detail || e.message;
        } finally {
          loading.value = false;
        }
      };
      onMounted(() => { load(); recents.value = recent.list(); });
      usePoll(load, 5000);
      const off = bus.on("watch-changed", () => load());
      onBeforeUnmount(off);

      const add = async (it) => {
        if (!it || !it.code) return;
        const label = it.name ? `${it.name}（${it.code}）` : it.code;
        if (rows.value.some((r) => r.code === it.code)) { toast.info(`${label} 已经在自选里了`); return; }
        try {
          setRows(await api.post("/api/watchlist", { code: it.code }));
          toast.success(`已加入自选：${label}`);
        } catch (e) { /* 已提示 */ }
      };
      const remove = async (r) => {
        busy.value = { ...busy.value, [r.code]: true };
        try {
          setRows(await api.del("/api/watchlist", { code: r.code }));
          toast.success(`已从自选移除：${r.name || r.code}`);
        } catch (e) { /* 已提示 */ } finally {
          busy.value = { ...busy.value, [r.code]: false };
        }
      };
      const editNote = (r) => { QW.tip.hide(); noteRow.value = r; noteText.value = r.note || ""; noteOpen.value = true; };
      const saveNote = async () => {
        const r = noteRow.value;
        if (!r) return;
        try {
          setRows(await api.post("/api/watchlist", { code: r.code, note: noteText.value.trim() }));
          noteOpen.value = false;
          toast.success("备注已保存");
        } catch (e) { /* 已提示 */ }
      };

      const status = (r) => {
        if (!isNum(r.price)) return "";
        if (isNum(r.limit_up) && r.price >= r.limit_up - EPS) return "lu";
        if (isNum(r.limit_down) && r.price <= r.limit_down + EPS) return "ld";
        return "";
      };
      const stats = computed(() => {
        const list = rows.value.filter((r) => isNum(r.pct));
        const up = list.filter((r) => r.pct > 0).length;
        const down = list.filter((r) => r.pct < 0).length;
        const avg = list.length ? list.reduce((a, r) => a + r.pct, 0) / list.length : null;
        const best = list.slice().sort((a, b) => b.pct - a.pct)[0] || null;
        return { n: rows.value.length, up, down, flat: list.length - up - down, avg, lu: rows.value.filter((r) => status(r) === "lu").length, best };
      });
      const quoteTime = computed(() => {
        const t = rows.value.map((r) => r.time).filter(Boolean).sort();
        return t.length ? t[t.length - 1] : "";
      });
      const suggestions = computed(() => recents.value.filter((r) => !rows.value.some((x) => x.code === r.code)).slice(0, 8));

      const columns = [
        { key: "name", label: "名称 / 代码", minWidth: "110px", sortable: true, sortBy: (r) => r.name || r.code, firstOrder: "asc" },
        { key: "price", label: "现价", align: "right", sortable: true },
        { key: "pct", label: "涨跌幅", align: "right", sortable: true },
        { key: "change", label: "涨跌额", align: "right", sortable: true },
        { key: "range", label: "今开 / 最高 / 最低", align: "right" },
        { key: "amount", label: "成交额", align: "right", sortable: true, help: HELP.amount },
        { key: "turnover", label: "换手", align: "right", sortable: true, help: HELP.turn },
        { key: "float_cap", label: "流通市值", align: "right", sortable: true, help: HELP.fcap },
        { key: "pe", label: "市盈率", align: "right", sortable: true, help: HELP.pe },
        { key: "note", label: "备注", minWidth: "90px" },
        { key: "actions", label: "", align: "right", width: "96px" },
      ];

      return {
        store, fmt, rows, loading, err, load, updatedAt, quoteTime, busy, add, remove, editNote, saveNote, noteOpen, noteRow, noteText,
        status, stats, suggestions, columns, go,
      };
    },
    template: `<div class="stack">
      <div class="wl-top">
        <div class="wl-add">
          <qw-stock-search :navigate="false" placeholder="输入代码、名称或拼音，回车加入自选" @select="add"/>
          <span class="muted wl-tip">选中后直接加入自选</span>
        </div>
        <div class="wl-meta">
          <span v-if="store.phase === '交易中'" class="qw-tag green"><i class="live-dot"></i>每 5 秒自动刷新</span>
          <span v-else class="muted">{{ store.phase ? store.phase + '，' : '' }}显示最近收盘行情</span>
          <span v-if="quoteTime" class="muted num">行情时间 {{ $fmt.time(quoteTime) }}</span>
          <button class="btn sm" @click="load"><qw-icon name="refresh" :size="14"/>刷新</button>
        </div>
      </div>

      <template v-if="loading">
        <qw-skeleton type="tiles" :count="4"/>
        <qw-card :pad="false"><div style="padding:16px"><qw-skeleton :rows="6"/></div></qw-card>
      </template>
      <qw-card v-else-if="err && !rows.length"><qw-empty icon="alert" title="自选股暂时读取不到" :desc="err" action-text="重新加载" @action="load"/></qw-card>

      <qw-card v-else-if="!rows.length">
        <qw-empty icon="star" title="还没有自选股" desc="在上面的搜索框输入股票代码、名称或拼音首字母，选中就能加进来。交易时间里，自选股的行情每 5 秒自动刷新。">
          <a class="btn" href="#/predict"><qw-icon name="rocket" :size="15"/>看看今天的短线预测</a>
          <a class="btn" href="#/"><qw-icon name="pulse" :size="15"/>看今天的涨停股</a>
        </qw-empty>
        <div v-if="suggestions.length" class="wl-suggest">
          <div class="muted">最近看过，点一下加入自选：</div>
          <div class="chips-wrap"><button v-for="s in suggestions" :key="s.code" class="chip" @click="add(s)"><qw-icon name="plus" :size="13"/>{{ s.name }} <span class="muted">{{ s.code }}</span></button></div>
        </div>
      </qw-card>

      <template v-else>
        <div class="wl-stats">
          <qw-stat label="自选股" :value="stats.n" unit="只" flat/>
          <qw-stat label="今天涨 / 跌" :value="stats.up + ' / ' + stats.down" :sub="stats.flat ? '平盘 ' + stats.flat + ' 只' : ''" flat/>
          <qw-stat label="平均涨跌幅" :value="$fmt.pct(stats.avg, 2)" :tone="$fmt.dir(stats.avg)" flat/>
          <qw-stat label="涨得最多" :value="stats.best ? stats.best.name : '—'" :sub="stats.best ? $fmt.pct(stats.best.pct, 2) : ''" :to="stats.best ? '/watch/' + stats.best.code : ''" flat size="sm"/>
        </div>

        <qw-card :pad="false" title="我的自选" icon="star" :sub="'点击一行进入看盘' + (stats.lu ? '，今天有 ' + stats.lu + ' 只涨停' : '')">
          <div v-if="store.isPhone" class="wl-cards">
            <div v-for="r in rows" :key="r.code" class="wl-card" @click="go('/watch/' + r.code)">
              <div class="r1"><qw-stock :code="r.code" :name="r.name"/><span v-if="status(r) === 'lu'" class="qw-tag red">涨停</span><span v-else-if="status(r) === 'ld'" class="qw-tag green">跌停</span>
                <span class="wl-px"><qw-price :value="r.price" :ref-value="r.prev_close"/><qw-price :value="r.pct" pct arrow/></span></div>
              <div class="r2 muted"><span>成交 {{ $fmt.money(r.amount) }}</span><span>换手 {{ r.turnover == null ? '—' : $fmt.num(r.turnover, 1) + '%' }}</span><span>流通 {{ $fmt.money(r.float_cap) }}</span></div>
              <div class="r3"><span class="ellipsis muted" @click.stop="editNote(r)">{{ r.note ? '备注：' + r.note : '+ 添加备注' }}</span>
                <button class="btn sm ghost" :disabled="busy[r.code]" @click.stop="remove(r)">移除</button></div>
            </div>
          </div>
          <qw-table v-else :columns="columns" :rows="rows" row-key="code" clickable :default-sort="null" @row-click="(r) => go('/watch/' + r.code)">
            <template #cell-name="{row}"><span class="row" style="flex-wrap:nowrap;gap:6px"><qw-stock :code="row.code" :name="row.name"/><span v-if="status(row) === 'lu'" class="qw-tag red">涨停</span><span v-else-if="status(row) === 'ld'" class="qw-tag green">跌停</span></span></template>
            <template #cell-price="{row}"><span v-if="row.error && row.price == null" class="muted" v-tip="row.error">暂无</span><qw-price v-else :value="row.price" :ref-value="row.prev_close"/></template>
            <template #cell-pct="{row}"><qw-price :value="row.pct" pct arrow/></template>
            <template #cell-change="{row}"><qw-price :value="row.change" change/></template>
            <template #cell-range="{row}"><span class="num text-2 nowrap">{{ $fmt.price(row.open) }} / <span class="up">{{ $fmt.price(row.high) }}</span> / <span class="down">{{ $fmt.price(row.low) }}</span></span></template>
            <template #cell-amount="{row}"><span class="num">{{ $fmt.money(row.amount) }}</span></template>
            <template #cell-turnover="{row}"><span class="num">{{ row.turnover == null ? '—' : $fmt.num(row.turnover, 2) + '%' }}</span></template>
            <template #cell-float_cap="{row}"><span class="num">{{ $fmt.money(row.float_cap) }}</span></template>
            <template #cell-pe="{row}"><span class="num" :class="row.pe < 0 ? 'muted' : ''">{{ row.pe == null ? '—' : row.pe < 0 ? '亏损' : $fmt.num(row.pe, 1) }}</span></template>
            <template #cell-note="{row}"><button class="note-cell ellipsis" @click.stop="editNote(row)" v-tip="row.note ? '点击修改备注' : '点击添加备注'">{{ row.note || '＋备注' }}</button></template>
            <template #cell-actions="{row}">
              <span class="row" style="justify-content:flex-end;flex-wrap:nowrap;gap:4px">
                <a class="qw-iconbtn" style="width:30px;height:30px" :href="'#/watch/' + row.code" @click.stop v-tip="'看盘'" aria-label="看盘"><qw-icon name="candle" :size="15"/></a>
                <button class="qw-iconbtn" style="width:30px;height:30px" :disabled="busy[row.code]" @click.stop="remove(row)" v-tip="'从自选移除'" aria-label="从自选移除"><qw-icon name="x" :size="15"/></button>
              </span>
            </template>
          </qw-table>
        </qw-card>
      </template>

      <qw-modal v-model="noteOpen" :title="noteRow ? '备注：' + (noteRow.name || noteRow.code) : '备注'" width="440px">
        <p class="muted" style="font-size:13px;margin-bottom:8px">记下你为什么关注它，比如“等回调到 20 元再看”。最多 200 字。</p>
        <textarea v-model="noteText" class="qw-input" rows="3" maxlength="200" placeholder="写点备注…" @keydown.ctrl.enter="saveNote"></textarea>
        <template #footer><button class="btn ghost" @click="noteOpen = false">取消</button><button class="btn primary" @click="saveNote">保存备注</button></template>
      </qw-modal>
    </div>`,
  });
})();
