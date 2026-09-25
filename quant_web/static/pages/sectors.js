/* 板块强弱 #/sectors：按证监会行业看最近 5/20/60 天的中位涨幅、站上 20 日线的比例、成交额和龙头股 */
(function () {
  "use strict";
  const { ref, computed, onMounted } = Vue;
  const { api, fmt, isNum } = QW;

  QW.page("sectors", {
    props: ["params", "query"],
    setup() {
      const data = ref(null);
      const err = ref("");
      const loading = ref(true);
      const q = ref("");
      const load = async () => {
        try { data.value = await api.get("/api/market/sectors", null, { silent: true }); err.value = ""; }
        catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
      };
      onMounted(load);
      const rows = computed(() => {
        const list = (data.value && data.value.rows) || [];
        const kw = q.value.trim();
        return kw ? list.filter((r) => r.industry.includes(kw)) : list;
      });
      const columns = [
        { key: "rank", label: "#", width: "44px" },
        { key: "industry", label: "行业", minWidth: "140px" },
        { key: "strength", label: "强度", align: "right", sortable: true, help: "20 日和 60 日涨幅在所有行业里排名的平均（0~100，越高越强）" },
        { key: "r5", label: "5 日", align: "right", sortable: true, help: "行业内股票 5 天涨幅的中位数" },
        { key: "r20", label: "20 日", align: "right", sortable: true },
        { key: "r60", label: "60 日", align: "right", sortable: true },
        { key: "above20", label: "站上 20 日线", align: "right", sortable: true, help: "行业里股价在 20 日线上方的股票比例" },
        { key: "amount", label: "成交额", align: "right", sortable: true },
        { key: "n", label: "股票数", align: "right", sortable: true },
        { key: "leaders", label: "20 日涨得最多（成交额 ≥5000 万）", minWidth: "220px" },
      ];
      const pctCell = (v) => (isNum(v) ? fmt.ratio(v, 1, true) : "—");
      return { data, err, loading, q, rows, columns, pctCell, fmt, load };
    },
    template: `<div class="stack">
      <div class="gd-note"><qw-icon name="info" :size="16"/><span>按行业把全市场股票分组，看最近谁强谁弱（中位数，不受个别大涨股影响）。强势行业里的股票更容易上涨，但强势不代表还会继续强——买之前仍要看个股的量价、主力阶段和排雷。</span></div>
      <qw-card :title="data ? '行业强弱（' + data.date + '）' : '行业强弱'" icon="layers" :pad="false">
        <template #extra><input v-model="q" class="qw-input" placeholder="找行业" style="width:140px;height:30px"></template>
        <qw-skeleton v-if="loading" :rows="10" style="padding:16px"/>
        <qw-empty v-else-if="err" icon="alert" title="行业数据暂时算不出来" :desc="err" action-text="重试" @action="load"/>
        <qw-table v-else :columns="columns" :rows="rows" row-key="industry" dense :page-size="40" max-height="calc(100vh - 260px)">
          <template #cell-rank="{index}"><span class="muted num">{{ index + 1 }}</span></template>
          <template #cell-industry="{row}"><b v-tip="row.industry">{{ $fmt.industry(row.industry) }}</b></template>
          <template #cell-strength="{row}"><span class="num">{{ row.strength.toFixed(0) }}</span></template>
          <template #cell-r5="{row}"><span class="num" :class="$fmt.dir(row.r5)">{{ pctCell(row.r5) }}</span></template>
          <template #cell-r20="{row}"><span class="num" :class="$fmt.dir(row.r20)">{{ pctCell(row.r20) }}</span></template>
          <template #cell-r60="{row}"><span class="num" :class="$fmt.dir(row.r60)">{{ pctCell(row.r60) }}</span></template>
          <template #cell-above20="{row}"><span class="num">{{ $fmt.ratio(row.above20, 0) }}</span></template>
          <template #cell-amount="{row}"><span class="num">{{ $fmt.money(row.amount) }}</span></template>
          <template #cell-leaders="{row}"><span class="sec-leaders"><a v-for="l in row.leaders" :key="l.code" class="chip" :href="'#/watch/' + l.code">{{ l.name }} <span :class="$fmt.dir(l.r20)">{{ pctCell(l.r20) }}</span></a></span></template>
        </qw-table>
        <div v-if="data" class="muted" style="font-size:12px;padding:8px 16px">{{ data.note }}</div>
      </qw-card>
    </div>`,
  });
})();
