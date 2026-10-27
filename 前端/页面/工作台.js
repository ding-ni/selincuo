const { createApp, ref, computed, nextTick, onMounted, onUpdated } = Vue;
createApp({
  components: { MonitoringWorkspace: window.MonitoringWorkspace },
  setup() {
    const navigation = [
      { id: 'map', name: '流域一张图', icon: 'map', ready: true },
      { id: 'monitor', name: '天空地监测', icon: 'satellite', ready: true },
      { id: 'forecast', name: '模型预报', icon: 'workflow' },
      { id: 'risk', name: '漫溢风险', icon: 'waves' },
      { id: 'warning', name: '预警管理', icon: 'bell' },
      { id: 'catalog', name: '成果与运行', icon: 'folders', ready: true },
    ];
    const pending = {
      monitor: { title: '监测资料尚未接入', description: '现有站点资料仅包含位置。观测时间、设备运行状态和监测值尚无有效记录。', inputs: ['地面湖位、流量与气象观测', '卫星与无人机反演产品及质量记录', '变量单位、时间基准与有效覆盖'] },
      forecast: { title: '尚无可运行的预报方案', description: '模型、驱动资料和初始边界尚未注册，当前无法提交历史回放、研究情景或业务预报。', inputs: ['模型版本与独立验证记录', '气象驱动与湖泊水位—库容关系', '参数版本、初始状态与边界条件'] },
      risk: { title: '漫溢风险待研判', description: '当前没有经审查的淹没成果或风险产品。风险状态未知。', inputs: ['有效模型运行与淹没演进结果', '承灾体、暴露度与易损性资料', '风险判据及集合成员信息'] },
      warning: { title: '暂无业务预警依据', description: '预报验证、阈值依据与复核发布配置尚未接入，当前没有可发布的业务预警。', inputs: ['经验证的业务预报成果', '预警阈值版本与有效期', '复核角色及真实发布渠道'] },
    };
    const typeNames = { basin: '候选总边界', subbasin: '入湖子流域', lake: '湖泊边界', station: '降水位置' };
    const colors = { basin: '#76877b', subbasin: '#5b927b', lake: '#379fb7', station: '#bd8940' };
    const view = ref('map'), objects = ref([]), datasets = ref([]), query = ref(''), type = ref('');
    const selected = ref(null), selectedDatasets = ref([]), datasetQuery = ref(''), focusedDataset = ref(null);
    const loading = ref(false), error = ref(''), mapError = ref(''), detailError = ref(''), detailLoading = ref(false);
    const dataVersion = ref(''), coordinates = ref('经纬度 / WGS 84');
    const monitorObject = ref('');
    function openMonitoring(id) { monitorObject.value = id; setView('monitor'); }
    async function locateMonitoring(id) { await setView('map'); await selectObject(id); }
    const visible = ref({ basin: true, subbasin: true, lake: true, station: true });
    let map = null, mapLayers = [], markers = [], loadSequence = 0, detailSequence = 0;
    const currentTitle = computed(() => navigation.find(n => n.id === view.value).name);
    const filteredObjects = computed(() => objects.value.filter(o => (!type.value || o.type === type.value) && o.name.toLowerCase().includes(query.value.trim().toLowerCase())));
    const filteredDatasets = computed(() => datasets.value.filter(d => `${d.name} ${d.dataset_id}`.toLowerCase().includes(datasetQuery.value.toLowerCase())));
    const activeDataset = computed(() => datasets.value.find(d => d.dataset_id === focusedDataset.value));
    const icons = () => window.lucide?.createIcons({ attrs: { 'aria-hidden': 'true' } });
    async function request(path) {
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(`/api/v1${path}`, { signal: controller.signal });
        if (!response.ok) {
          const value = await response.json().catch(() => ({}));
          throw new Error(typeof value.detail === 'string' ? value.detail : `资料请求失败（${response.status}）`);
        }
        return await response.json();
      } catch (e) {
        if (e.name === 'AbortError') throw new Error('资料请求超时，请重试');
        throw e;
      } finally { clearTimeout(timer); }
    }
    async function setView(id) { view.value = id; await nextTick(); if (id === 'map') map?.resize(); icons(); }
    function openDataset(id) { focusedDataset.value = id; datasetQuery.value = ''; setView('catalog'); }
    function fitAll() {
      if (!map || !objects.value.length) return;
      const bounds = new maplibregl.LngLatBounds();
      objects.value.forEach(o => { bounds.extend(o.bbox.slice(0, 2)); bounds.extend(o.bbox.slice(2)); });
      map.fitBounds(bounds, { padding: 35, duration: 450, maxZoom: 8 });
    }
    function highlight() {
      if (map?.getLayer('selected-boundary')) map.setFilter('selected-boundary', ['==', ['get', 'object_id'], selected.value?.object_id || '']);
    }
    async function selectObject(id, focus = true) {
      const sequence = ++detailSequence;
      detailLoading.value = true; detailError.value = ''; selected.value = null; selectedDatasets.value = []; highlight();
      try {
        const [object, related] = await Promise.all([request(`/objects/${encodeURIComponent(id)}`), request(`/datasets?object_id=${encodeURIComponent(id)}`)]);
        if (sequence !== detailSequence) return;
        selected.value = object; selectedDatasets.value = related.items; visible.value[object.type] = true;
        updateVisibility(); highlight();
        if (focus && map) {
          const b = object.bbox;
          if (object.type === 'station') map.easeTo({ center: b.slice(0, 2), zoom: 8, duration: 500 });
          else map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 50, maxZoom: 9, duration: 500 });
        }
      } catch (e) { if (sequence === detailSequence) detailError.value = `对象详情读取失败：${e.message}`; }
      finally { if (sequence === detailSequence) detailLoading.value = false; }
    }
    function updateVisibility() {
      if (!map) return;
      for (const item of mapLayers) if (map.getLayer(item.id)) map.setLayoutProperty(item.id, 'visibility', visible.value[item.type] ? 'visible' : 'none');
      markers.forEach(({ marker, type: kind }) => { marker.getElement().style.display = visible.value[kind] ? '' : 'none'; });
      if (map.getLayer('selected-boundary')) map.setLayoutProperty('selected-boundary', 'visibility', selected.value && visible.value[selected.value.type] ? 'visible' : 'none');
    }
    async function drawMap(collections, sequence) {
      if (map) { map.remove(); map = null; }
      markers = []; mapLayers = []; await nextTick();
      if (sequence !== loadSequence) return;
      const instance = new maplibregl.Map({ container: 'map', center: [89.8, 31.8], zoom: 6, maxZoom: 13, attributionControl: false,
        style: { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#edf1ee' } }] } });
      map = instance;
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
      map.addControl(new maplibregl.ScaleControl({ maxWidth: 90, unit: 'metric' }), 'bottom-right');
      map.on('error', () => { if (map === instance) mapError.value = '地图渲染异常，资料目录仍可查询'; });
      map.getCanvas().addEventListener('webglcontextlost', () => { mapError.value = '地图绘图环境已中断，请重新加载'; });
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('地图初始化超时')), 15000);
        instance.once('load', () => { clearTimeout(timer); resolve(); });
      });
      if (sequence !== loadSequence) return;
      map.addSource('objects', { type: 'geojson', data: { type: 'FeatureCollection', features: collections.flatMap(c => c.features) } });
      for (const kind of Object.keys(typeNames)) {
        const filter = ['==', ['get', 'type'], kind], id = `${kind}-fill`;
        if (kind === 'station') map.addLayer({ id, source: 'objects', type: 'circle', filter, paint: { 'circle-radius': 5, 'circle-color': colors[kind], 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
        else {
          map.addLayer({ id, source: 'objects', type: 'fill', filter, paint: { 'fill-color': colors[kind], 'fill-opacity': kind === 'lake' ? 0.82 : kind === 'basin' ? 0.04 : 0.16 } });
          map.addLayer({ id: `${kind}-line`, source: 'objects', type: 'line', filter, paint: { 'line-color': colors[kind], 'line-width': kind === 'basin' ? 1.4 : 1.1 } });
          mapLayers.push({ id: `${kind}-line`, type: kind });
        }
        mapLayers.push({ id, type: kind });
      }
      map.addLayer({ id: 'selected-boundary', source: 'objects', type: 'line', filter: ['==', ['get', 'object_id'], ''], paint: { 'line-color': '#135b48', 'line-width': 2.4 } });
      map.on('click', e => {
        const features = map.queryRenderedFeatures(e.point, { layers: mapLayers.map(l => l.id).filter(id => id.endsWith('-fill')) });
        if (features.length) selectObject(features[0].properties.object_id, false);
      });
      map.on('mousemove', e => {
        coordinates.value = `${e.lngLat.lng.toFixed(4)}° E / ${e.lngLat.lat.toFixed(4)}° N`;
        map.getCanvas().style.cursor = map.queryRenderedFeatures(e.point, { layers: mapLayers.map(l => l.id) }).length ? 'pointer' : '';
      });
      // 标签来自目录对象，地图不请求在线字形或底图。
      objects.value.filter(o => ['subbasin', 'lake'].includes(o.type)).forEach(o => {
        const label = document.createElement('button'); label.className = 'geo-label'; label.textContent = o.name; label.title = `查看${o.name}`;
        label.addEventListener('click', () => selectObject(o.object_id, false));
        const b = o.bbox, marker = new maplibregl.Marker({ element: label }).setLngLat([(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]).addTo(map);
        markers.push({ marker, type: o.type });
      });
      fitAll(); updateVisibility(); highlight();
    }
    async function load() {
      const sequence = ++loadSequence; ++detailSequence;
      loading.value = true; error.value = ''; mapError.value = ''; detailError.value = ''; detailLoading.value = false;
      objects.value = []; datasets.value = []; selected.value = null; selectedDatasets.value = []; dataVersion.value = '';
      if (map) { map.remove(); map = null; }
      try {
        const [health, objectList, datasetList] = await Promise.all([request('/health'), request('/objects'), request('/datasets')]);
        if (sequence !== loadSequence) return;
        objects.value = objectList.items; datasets.value = datasetList.items; dataVersion.value = health.data_version;
        try {
          const collections = await Promise.all(datasetList.items.map(d => request(`/layers/${encodeURIComponent(d.dataset_id)}`)));
          if (sequence !== loadSequence) return;
          await drawMap(collections, sequence);
        } catch (e) { if (sequence === loadSequence) mapError.value = `空间图层加载失败：${e.message}`; }
        if (sequence === loadSequence && objects.value.length) await selectObject(objects.value.find(o => o.type === 'lake')?.object_id || objects.value[0].object_id, false);
      } catch (e) { if (sequence === loadSequence) error.value = `空间资料服务不可用：${e.message}`; }
      finally { if (sequence === loadSequence) loading.value = false; }
    }
    onMounted(() => { icons(); load(); }); onUpdated(icons);
    return { navigation, pending, typeNames, colors, view, objects, datasets, query, type, selected, selectedDatasets,
      datasetQuery, focusedDataset, loading, error, mapError, detailError, detailLoading, dataVersion, coordinates, visible,
      currentTitle, filteredObjects, filteredDatasets, activeDataset, setView, openDataset, fitAll, selectObject, updateVisibility, load,
      monitorObject, openMonitoring, locateMonitoring,
      formatArea: value => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) };
  },
}).mount('#app');
