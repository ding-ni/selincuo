window.MonitoringWorkspace = {
  props: { objects: { type: Array, required: true }, initialObject: { type: String, default: '' } },
  emits: ['locate', 'summary'],
  setup(props, { emit }) {
    const { ref, computed, onMounted, onUpdated, onUnmounted, watch } = Vue;
    const variables = ref([]), products = ref([]), loading = ref(false), error = ref('');
    const objectId = ref(props.initialObject), variable = ref(''), channel = ref(''), productId = ref('');
    const start = ref(''), end = ref(''), quality = ref('all'), rows = ref([]), total = ref(0), offset = ref(0);
    const seriesLoading = ref(false), seriesError = ref(''), registered = ref(0), applied = ref(null);
    const pageSize = 50;
    let catalogSequence = 0, seriesSequence = 0;
    const channels = { ground: '地面监测', satellite: '卫星反演', uav: '无人机', other: '其他 / 待核实' };
    const qualities = { unreviewed: '未审核', accepted: '供数方审核通过', suspect: '可疑', missing: '缺测', rejected: '剔除' };
    const active = computed(() => products.value.find(p => p.product_id === productId.value));
    const selectedObject = computed(() => props.objects.find(o => o.object_id === objectId.value));
    const variableName = id => variables.value.find(v => v.variable === id)?.name || id;
    const objectName = id => props.objects.find(o => o.object_id === id)?.name || id;
    const displayTime = time => time ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(time)) : '未声明';
    const icons = () => window.lucide?.createIcons({ attrs: { 'aria-hidden': 'true' } });
    async function get(path, params = {}) {
      const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== '' && value != null));
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(`/api/v1${path}?${query}`, { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
        return data;
      } catch (e) { throw new Error(e.name === 'AbortError' ? '请求超时，请重试' : e.message); }
      finally { clearTimeout(timer); }
    }
    function clearSeries() {
      ++seriesSequence; rows.value = []; total.value = 0; offset.value = 0; applied.value = null;
      seriesLoading.value = false; seriesError.value = '';
    }
    async function refresh() {
      const sequence = ++catalogSequence;
      clearSeries(); loading.value = true; error.value = ''; products.value = []; productId.value = ''; registered.value = 0;
      emit('summary', { status: 'loading' });
      try {
        const [dictionary, result] = await Promise.all([get('/monitoring/variables'), get('/monitoring/products', { object_id: objectId.value, variable: variable.value, channel: channel.value })]);
        if (sequence !== catalogSequence) return;
        variables.value = dictionary.items; products.value = result.items; registered.value = result.registered_total;
        emit('summary', { status: result.status, count: result.registered_total });
        if (result.items.length) { productId.value = result.items[0].product_id; await chooseProduct(); }
      } catch (e) { if (sequence === catalogSequence) { error.value = e.message; emit('summary', { status: 'error' }); } }
      finally { if (sequence === catalogSequence) loading.value = false; }
    }
    async function chooseProduct() {
      clearSeries(); start.value = ''; end.value = ''; quality.value = 'all';
      if (productId.value) await querySeries(0, true);
    }
    async function querySeries(nextOffset = 0, useForm = true) {
      const sequence = ++seriesSequence;
      seriesLoading.value = true; seriesError.value = ''; rows.value = []; total.value = 0;
      const filters = useForm ? { start: start.value ? `${start.value}:00+08:00` : '', end: end.value ? `${end.value}:00+08:00` : '', quality: quality.value === 'all' ? '' : quality.value } : applied.value;
      if (!filters || !productId.value) { seriesLoading.value = false; return; }
      try {
        const result = await get('/observations', { product_id: productId.value, ...filters, offset: nextOffset, limit: pageSize });
        if (sequence !== seriesSequence) return;
        rows.value = result.items; total.value = result.total; offset.value = result.offset; applied.value = { ...filters };
      } catch (e) { if (sequence === seriesSequence) { seriesError.value = e.message; applied.value = null; } }
      finally { if (sequence === seriesSequence) seriesLoading.value = false; }
    }
    function reset() { objectId.value = ''; variable.value = ''; channel.value = ''; refresh(); }
    watch(() => props.initialObject, id => { if (id !== objectId.value) { objectId.value = id; refresh(); } });
    onUnmounted(() => { ++catalogSequence; ++seriesSequence; });
    onMounted(refresh); onUpdated(icons);
    return { variables, products, loading, error, objectId, variable, channel, productId, start, end, quality, rows, total, offset,
      seriesLoading, seriesError, registered, applied, pageSize, channels, qualities, active, selectedObject, variableName, objectName,
      displayTime, refresh, chooseProduct, querySeries, reset, locate: id => emit('locate', id) };
  },
  template: `
    <section class="monitor-workspace" aria-label="监测资料工作台">
      <div class="monitor-heading"><div><h2>监测资料</h2><p>地面观测 / 卫星反演 / 无人机成果</p></div><span class="quality-badge">{{ loading ? '正在查询' : error ? '服务异常' : registered ? registered + ' 个产品版本' : '真实时序待接入' }}</span></div>
      <form class="monitor-filters" @submit.prevent="refresh">
        <label>空间对象<select v-model="objectId" aria-label="监测空间对象"><option :value="''">全部对象</option><option v-for="o in objects" :key="o.object_id" :value="o.object_id">{{o.name}} · {{o.object_id}}</option></select></label>
        <label>监测变量<select v-model="variable" aria-label="监测变量"><option :value="''">全部变量</option><option v-for="v in variables" :value="v.variable">{{v.name}} · {{v.unit}}</option></select></label>
        <label>观测平台<select v-model="channel" aria-label="观测平台"><option :value="''">全部平台</option><option v-for="(name,id) in channels" :value="id">{{name}}</option></select></label>
        <button type="submit" class="primary-button" :disabled="loading"><i data-lucide="search"></i>查询资料</button><button type="button" class="icon-button" @click="reset" title="重置筛选" aria-label="重置筛选"><i data-lucide="rotate-ccw"></i></button>
      </form>
      <div v-if="error" class="error-banner" role="alert">{{error}}<button @click="refresh">重试监测查询</button></div>
      <p v-else-if="loading" class="empty" role="status">正在读取资料版本与质量信息…</p>
      <template v-else-if="!products.length">
        <div class="monitor-empty"><i data-lucide="chart-no-axes-combined"></i><h3>{{registered ? '当前筛选下没有监测产品' : '尚未接入监测时序'}}</h3><p>{{registered ? '调整对象、变量或观测平台后重新查询。' : '现有资料为静态边界与站点位置，尚无可核验的观测记录。'}}</p><button v-if="selectedObject" class="text-button" @click="locate(selectedObject.object_id)"><i data-lucide="map-pin"></i>查看{{selectedObject.name}}空间位置</button></div>
        <div v-if="!registered" class="monitor-readiness"><div><i data-lucide="radio-tower"></i><h3>地面监测</h3><p>湖位、流量、降水、气温</p><span>设备在线状态未核实</span></div><div><i data-lucide="satellite"></i><h3>卫星反演</h3><p>水位、水域面积及质量标志</p><span>产品时间与来源待接入</span></div><div><i data-lucide="scan-line"></i><h3>无人机成果</h3><p>按对象归档的监测产品</p><span>测次与成果版本待接入</span></div></div>
      </template>
      <template v-else>
        <div class="product-selection"><label>资料版本<select v-model="productId" @change="chooseProduct" aria-label="监测资料版本"><option v-for="p in products" :key="p.product_id" :value="p.product_id">{{p.name}} · {{p.version}} · {{p.source_type}}</option></select></label><button v-if="active" class="text-button" @click="locate(active.object_id)"><i data-lucide="map-pin"></i>空间定位</button></div>
        <template v-if="active">
          <div class="monitor-metadata"><div><span>对象 / 变量</span><strong>{{objectName(active.object_id)}} / {{variableName(active.variable)}}</strong><small>{{active.unit}}<template v-if="active.vertical_datum"> · {{active.vertical_datum}}</template></small></div><div><span>数据来源</span><strong>{{active.source_type}}</strong><small>{{channels[active.channel]}} · {{active.source_version}}</small></div><div><span>记录覆盖 · 北京时间</span><strong>{{displayTime(active.time_range[0])}}</strong><small>至 {{displayTime(active.time_range[1])}}</small></div><div><span>记录 / 时间缺口</span><strong>{{active.record_count}} 条 / {{active.sampling_interval_seconds ? active.gap_count + ' 处' : '待判断'}}</strong><small>{{active.accumulation_seconds ? '累计时长 ' + active.accumulation_seconds + ' 秒' : active.coverage_status}}</small></div></div>
          <div class="quality-counts"><span v-for="(name,id) in qualities" :key="id" :class="['quality-'+id]">{{name}} <strong>{{active.quality_counts[id] || 0}}</strong></span></div>
          <form class="series-filters" @submit.prevent="querySeries(0,true)"><label>开始时间 · 北京时间<input type="datetime-local" v-model="start" aria-label="监测开始时间"></label><label>结束时间 · 北京时间<input type="datetime-local" v-model="end" aria-label="监测结束时间"></label><label>质量状态<select v-model="quality" aria-label="质量状态"><option value="all">全部状态</option><option v-for="(name,id) in qualities" :value="id">{{name}}</option></select></label><button class="primary-button" :disabled="seriesLoading"><i data-lucide="list-filter"></i>查询记录</button></form>
          <div v-if="seriesError" class="error-banner" role="alert">{{seriesError}}<button @click="querySeries(0,true)">重试记录查询</button></div>
          <p v-else-if="seriesLoading" class="empty" role="status">正在读取记录…</p>
          <template v-else><div class="table-scroll monitor-records"><table><thead><tr><th>时间 · 北京时间（UTC+8）</th><th>{{variableName(active.variable)}} / {{active.unit}}</th><th>质量状态</th><th>质量依据</th></tr></thead><tbody><tr v-for="row in rows" :key="row.time"><td>{{displayTime(row.time)}}</td><td :class="{'missing-value': row.value === null}">{{row.value === null ? '缺测' : row.value}}</td><td><span :class="['record-quality','quality-'+row.quality]">{{qualities[row.quality]}}</span></td><td>{{row.quality_note || '未提供审核依据'}}</td></tr></tbody></table><p v-if="!rows.length" class="empty">当前时段与质量条件下没有记录</p></div><div class="pagination"><span>共 {{total}} 条<template v-if="total"> · {{offset + 1}}–{{Math.min(offset + pageSize,total)}}</template></span><button class="icon-button" title="上一页" aria-label="上一页" :disabled="offset === 0 || !applied" @click="querySeries(offset-pageSize,false)"><i data-lucide="chevron-left"></i></button><button class="icon-button" title="下一页" aria-label="下一页" :disabled="offset+pageSize >= total || !applied" @click="querySeries(offset+pageSize,false)"><i data-lucide="chevron-right"></i></button></div></template>
          <details class="monitor-provenance"><summary>来源、版本与质量依据</summary><div class="dataset-detail"><div><h3>{{active.name}}</h3><p>{{active.source_description}}</p><dl><dt>产品版本</dt><dd>{{active.product_id}}</dd><dt>使用范围</dt><dd>{{active.license}}</dd><dt>接入时间 · 北京时间</dt><dd>{{displayTime(active.imported_at)}}</dd><dt>采样间隔</dt><dd>{{active.sampling_interval_seconds ? active.sampling_interval_seconds + ' 秒' : '未声明'}}</dd></dl><p class="quality-note">{{active.quality_scope}}</p><p v-if="active.source_type === '合成演示'" class="quality-note">合成演示，仅用于隔离测试，不构成真实监测或预警依据。</p></div><div class="source-list"><h3>来源文件与 SHA-256</h3><div><span>{{active.source_file}}</span><code>{{active.source_sha256}}</code></div><div><span>标准版本 SHA-256</span><code>{{active.standard_sha256}}</code></div><h3>时间缺口</h3><p v-if="!active.sampling_interval_seconds">未声明采样间隔，不能判断缺口。</p><p v-else-if="!active.gap_count">记录跨度内未发现超过声明间隔的缺口；不代表时窗外完整。</p><p v-for="gap in active.gaps">{{displayTime(gap.start)}} 至 {{displayTime(gap.end)}}（{{gap.seconds}} 秒）</p><p v-if="active.gap_list_truncated">仅展示前50处，共{{active.gap_count}}处。</p></div></div></details>
        </template>
      </template>
    </section>`
};
