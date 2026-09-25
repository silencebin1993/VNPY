/* 数据源 #/sources：每种数据的数据源顺序（可调整）与健康状况、测试连接、扩展数据存档与更新、可选数据源配置 */
(function () {
  "use strict";
  const { ref, computed, onMounted } = Vue;
  const { api, fmt, toast, jobs } = QW;

  QW.page("sources", {
    props: ["params", "query"],
    setup() {
      const loading = ref(true);
      const err = ref("");
      const data = ref({ capabilities: [], providers: [], archives: [] });
      const saving = ref("");
      const probing = ref("");
      const probeOpen = ref(false);
      const probeRes = ref(null);
      const jobId = ref("");
      const tushareToken = ref("");
      const qmtPath = ref("");

      const load = async () => {
        try {
          data.value = await api.get("/api/providers", null, { silent: true });
          err.value = "";
        } catch (e) { err.value = e.detail || e.message; } finally { loading.value = false; }
      };
      const loadSettings = async () => {
        try {
          const s = await api.get("/api/settings", null, { silent: true });
          tushareToken.value = (s.providers || {}).tushare_token || "";
          qmtPath.value = (s.providers || {}).qmt_path || "";
        } catch (e) { /* 忽略 */ }
      };
      onMounted(() => { load(); loadSettings(); });

      const provLabel = (name) => {
        const p = data.value.providers.find((x) => x.name === name);
        return p ? p.label : name;
      };
      const move = async (cap, i, d) => {
        const chain = cap.chain.slice();
        const j = i + d;
        if (j < 0 || j >= chain.length) return;
        [chain[i], chain[j]] = [chain[j], chain[i]];
        await saveChain(cap, chain);
      };
      const toggle = async (cap, name) => {
        let chain = cap.chain.slice();
        if (chain.includes(name)) {
          if (chain.length === 1) { toast.warn("至少要保留一个数据源"); return; }
          chain = chain.filter((n) => n !== name);
        } else chain.push(name);
        await saveChain(cap, chain);
      };
      const saveChain = async (cap, chain) => {
        saving.value = cap.capability;
        try {
          data.value = await api.put("/api/providers/chain", { capability: cap.capability, chain });
          toast.success(`已更新「${cap.label}」的数据源顺序`);
        } catch (e) { /* 已提示 */ } finally { saving.value = ""; }
      };
      const reset = (cap) => saveChain(cap, []);
      const probe = async (cap, name) => {
        probing.value = cap.capability + ":" + name;
        try {
          probeRes.value = { cap, name, ...(await api.post("/api/providers/probe", { capability: cap.capability, provider: name })) };
          probeOpen.value = true;
          load();
        } catch (e) { /* 已提示 */ } finally { probing.value = ""; }
      };
      const updateExt = async () => {
        try {
          jobId.value = await jobs.start("/api/jobs/run", { name: "ext_update" }, "更新扩展数据");
        } catch (e) { /* 已提示 */ }
      };
      const saveOptional = async () => {
        try {
          await api.put("/api/settings", { providers: { tushare_token: tushareToken.value.trim(), qmt_path: qmtPath.value.trim() } });
          toast.success("已保存");
          load();
        } catch (e) { /* 已提示 */ }
      };
      const sampleCols = computed(() => {
        const s = probeRes.value && probeRes.value.sample && probeRes.value.sample[0];
        return s ? Object.keys(s).slice(0, 8) : [];
      });
      const health = (src) => {
        if (!src.available) return { cls: "gray", text: "不可用" };
        if (src.fail && !src.ok) return { cls: "red", text: "最近失败" };
        if (src.ok) return { cls: "green", text: "正常" };
        return { cls: "", text: "未使用" };
      };
      const fmtCell = (v) => (v == null ? "—" : typeof v === "number" ? fmt.num(v, Math.abs(v) >= 100 ? 0 : 2) : String(v));

      return {
        fmt, loading, err, data, saving, probing, probeOpen, probeRes, jobId, tushareToken, qmtPath, load, provLabel, move,
        toggle, reset, probe, updateExt, saveOptional, sampleCols, health, fmtCell,
      };
    },
    template: `<div class="stack">
      <qw-card v-if="loading"><qw-skeleton :rows="8"/></qw-card>
      <qw-card v-else-if="err"><qw-empty icon="alert" title="数据源信息读取不到" :desc="err" action-text="重试" @action="load"/></qw-card>
      <template v-else>
        <div class="gd-note"><qw-icon name="info" :size="16"/><span>每种数据都有一串数据源，按顺序尝试：前一个取不到就自动换下一个。“本机”表示已经下载到电脑里的数据，最快也最稳定。一般不需要改顺序；某个网站长期连不上时，可以把它往后挪或关掉。</span></div>

        <qw-card title="扩展数据" icon="database" sub="资金流、融资融券、股东户数、解禁、质押、业绩预告、增减持、指数（排雷和主力分析会用到）">
          <template #extra><button class="btn sm primary" @click="updateExt"><qw-icon name="download" :size="14"/>现在更新</button></template>
          <qw-job v-if="jobId" :job-id="jobId" title="更新扩展数据" @done="load" @failed="load"/>
          <qw-table :columns="[{key:'label',label:'数据'},{key:'rows',label:'行数',align:'right'},{key:'codes',label:'股票数',align:'right'},{key:'range',label:'日期范围'},{key:'updated',label:'更新时间'}]" :rows="data.archives" row-key="name" dense>
            <template #cell-rows="{row}"><span class="num">{{ row.exists ? fmt.int(row.rows) : '—' }}</span></template>
            <template #cell-codes="{row}"><span class="num">{{ row.codes ? fmt.int(row.codes) : '—' }}</span></template>
            <template #cell-range="{row}"><span class="num text-2">{{ row.start ? row.start + ' ~ ' + row.end : (row.exists ? '—' : '还没有下载') }}</span></template>
            <template #cell-updated="{row}"><span class="num text-2">{{ row.updated || '—' }}</span></template>
          </qw-table>
        </qw-card>

        <qw-card title="每种数据的数据源" icon="plug" :pad="false">
          <div class="src-list">
            <div v-for="cap in data.capabilities" :key="cap.capability" class="src-row">
              <div class="src-cap">
                <b>{{ cap.label }}</b><span v-if="cap.customized" class="qw-tag blue">已自定义</span>
                <div class="muted src-desc">{{ cap.description }}</div>
              </div>
              <div class="src-chain">
                <div v-if="!cap.sources.length" class="muted">暂无数据源</div>
                <div v-for="src in cap.sources" :key="src.name" class="src-item" :class="{off: !cap.chain.includes(src.name)}">
                  <span class="src-order num">{{ cap.chain.includes(src.name) ? cap.chain.indexOf(src.name) + 1 : '—' }}</span>
                  <span class="src-name">{{ src.label }}<span v-if="src.optional" class="muted">（可选）</span></span>
                  <span class="qw-tag" :class="health(src).cls" v-tip="src.available ? (src.last_error ? '最近失败：' + src.last_error + '（' + src.last_error_at + '）' : (src.last_ok ? '最近成功：' + src.last_ok : '')) : src.reason">{{ health(src).text }}</span>
                  <span v-if="src.last_ms != null" class="muted num src-ms">{{ Math.round(src.last_ms) }}ms</span>
                  <span class="src-acts">
                    <button v-if="cap.chain.includes(src.name)" class="qw-iconbtn sm" :disabled="saving === cap.capability || cap.chain.indexOf(src.name) === 0" @click="move(cap, cap.chain.indexOf(src.name), -1)" v-tip="'往前挪'" aria-label="往前挪"><qw-icon name="chevronUp" :size="14"/></button>
                    <button v-if="cap.chain.includes(src.name)" class="qw-iconbtn sm" :disabled="saving === cap.capability || cap.chain.indexOf(src.name) === cap.chain.length - 1" @click="move(cap, cap.chain.indexOf(src.name), 1)" v-tip="'往后挪'" aria-label="往后挪"><qw-icon name="chevronDown" :size="14"/></button>
                    <button class="btn sm ghost" :disabled="saving === cap.capability" @click="toggle(cap, src.name)">{{ cap.chain.includes(src.name) ? '停用' : '启用' }}</button>
                    <button class="btn sm" :disabled="!!probing || !src.available" @click="probe(cap, src.name)">{{ probing === cap.capability + ':' + src.name ? '测试中…' : '测试' }}</button>
                  </span>
                </div>
                <button v-if="cap.customized" class="btn sm ghost src-reset" @click="reset(cap)">恢复默认顺序</button>
              </div>
            </div>
          </div>
        </qw-card>

        <qw-card title="可选数据源" icon="sliders" sub="不填也能正常使用；填了可以多一个备用来源">
          <div class="form-grid">
            <label><span>tushare token</span><input v-model="tushareToken" class="qw-input" placeholder="在 tushare.pro 注册后获取（还需要 pip 安装 tushare）"></label>
            <label><span>券商 QMT 安装目录</span><input v-model="qmtPath" class="qw-input" placeholder="例如 D:\\国金QMT交易端\\bin.x64（开通 QMT 后才需要）"></label>
          </div>
          <template #footer><button class="btn primary" @click="saveOptional"><qw-icon name="save" :size="15"/>保存</button></template>
        </qw-card>
      </template>

      <qw-modal v-model="probeOpen" :title="probeRes ? '测试：' + probeRes.cap.label + ' · ' + provLabel(probeRes.name) : '测试'" width="720px">
        <template v-if="probeRes">
          <div class="row" style="gap:12px;margin-bottom:10px">
            <span class="qw-tag" :class="probeRes.ok ? 'green' : 'red'">{{ probeRes.ok ? '成功' : '失败' }}</span>
            <span class="muted num">{{ probeRes.ms }} 毫秒 · {{ probeRes.rows }} 行</span>
          </div>
          <p v-if="probeRes.error" class="text-2" style="white-space:pre-wrap">{{ probeRes.error }}</p>
          <div v-if="sampleCols.length" class="hs-box"><div style="overflow-x:auto" v-hscroll>
            <table class="mini-table"><thead><tr><th v-for="c in sampleCols" :key="c">{{ c }}</th></tr></thead>
              <tbody><tr v-for="(r, i) in probeRes.sample" :key="i"><td v-for="c in sampleCols" :key="c" class="num">{{ fmtCell(r[c]) }}</td></tr></tbody></table>
          </div></div>
        </template>
      </qw-modal>
    </div>`,
  });
})();
