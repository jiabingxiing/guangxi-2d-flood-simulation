/* 广西久隆·宁明·贵港 二维洪涝可视化 —— 应用编排
 *
 * 职责：
 *   - 读取 catalog.json，装配三案例切换、run 切换、图层状态机、预设、图例、质量面板；
 *   - 时间轴交互（吸附真实帧、播放/暂停/逐帧/速度）、最大包络独立入口；
 *   - 点击地图 → proj4 + 反仿射 → numeric 分块 float32 点查（结果与块缓存）；
 *   - 面积—时间曲线（阈值可切换，Y 轴从 0 起，含前段 0 值）；
 *   - 请求令牌丢弃过期响应；缺帧/图层不可用明确提示；在线底图失败降级。
 *
 * 数据契约见 SPEC.md §4–§7 与 docs/examples/*.json；不使用任何在线资源（Esri 底图除外）。
 */
(function () {
  'use strict';

  var TL = window.FVTimeline;
  var MB = 1024 * 1024;

  /* -------------------------------------------------------------- 状态 */

  var state = {
    catalog: null,
    caseId: null,
    caseObj: null,
    runId: null,
    runObj: null,
    timeline: null,
    statistics: null,
    images: null,
    numericManifest: null,
    threshold: 0.05,
    maxMode: false,
    basemapEnabled: false,
    basemapId: null,
    layerState: {},      // group -> {visible, opacity, available, reason}
    presetId: 'all_available',
    loadToken: 0,
    queryToken: 0,
    lastCommitted: null, // {frameId, index, wetPixels}
    blockCache: new Map(),
    blockCacheBytes: 0,
    blockCacheMax: 64 * MB,
    queryCache: new Map(),
    queryCacheMax: 500,
    pendingQuery: null,
    ready: false,
    notice: null,
    peakFrameId: null
  };

  var player = null;
  var fvmap = null;

  /* -------------------------------------------------------------- DOM */

  function el(id) {
    return document.getElementById(id);
  }

  var dom = {};

  function cacheDom() {
    [
      'project-title', 'case-tabs', 'run-select', 'header-badges',
      'preset-buttons', 'layer-list', 'basemap-toggle', 'basemap-select',
      'basemap-warn', 'basemap-retry', 'map-title', 'map-subtitle', 'notice-bar', 'map-hint',
      'query-body', 'threshold-select', 'area-chart', 'chart-note', 'legend-body',
      'quality-body', 'btn-prev', 'btn-play', 'btn-next', 'speed-group', 'time-relative',
      'time-absolute', 'frame-readout', 'btn-max', 'frame-slider', 'frame-ticks',
      'temporal-note', 'origin-note', 'status-bundle', 'status-layers',
      'status-attribution', 'status-license'
    ].forEach(function (id) {
      dom[camel(id)] = el(id);
    });
  }

  function camel(s) {
    return s.replace(/-([a-z])/g, function (m, c) {
      return c.toUpperCase();
    });
  }

  /* -------------------------------------------------------------- 工具 */

  function fetchJson(url, token) {
    return fetch(url, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + '：' + url);
      return r.json();
    }).then(function (json) {
      if (token != null && token !== state.loadToken) {
        var e = new Error('stale');
        e.stale = true;
        throw e;
      }
      return json;
    });
  }

  function fmtDepth(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    var d = digits == null ? 2 : digits;
    if (Math.abs(v) >= 100) d = 0;
    else if (Math.abs(v) >= 10) d = 1;
    return v.toFixed(d) + ' m';
  }

  function fmtArea(v) {
    if (v == null || !isFinite(v)) return '—';
    if (v >= 100) return v.toFixed(1) + ' km²';
    if (v >= 10) return v.toFixed(2) + ' km²';
    return v.toFixed(3) + ' km²';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function clearNode(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  /* -------------------------------------------------------------- 启动 */

  function boot() {
    cacheDom();
    installErrorCapture();
    player = new TL.FramePlayer({
      cache: new TL.ImageCache(256 * MB),
      onCommit: commitFrame,
      onCommitted: onFrameCommitted,
      onCommitError: onCommitError,
      onStateChange: renderPlayerState
    });
    fvmap = new window.FVMap({
      cache: player.cache,
      onClick: handleMapClick,
      onBasemapState: onBasemapState,
      onLayerError: onLayerError,
      onScale: onScale
    });

    bindStaticEvents();
    loadCatalog();
  }

  function installErrorCapture() {
    window.__fvErrors = [];
    window.addEventListener('error', function (e) {
      window.__fvErrors.push(String((e && e.message) || e));
    });
    window.addEventListener('unhandledrejection', function (e) {
      var msg = e && e.reason ? (e.reason.message || String(e.reason)) : 'unhandled rejection';
      if (/stale/.test(msg)) return;
      window.__fvErrors.push(msg);
    });
  }

  function loadCatalog() {
    var url = 'catalog.json';
    fetchJson(url).then(function (catalog) {
      state.catalog = catalog;
      fvmap.setCatalog(catalog);
      applyCatalogChrome(catalog);
      setupBasemaps(catalog);
      buildPresetButtons(catalog);
      var caseId = catalog.default_case || (catalog.cases[0] && catalog.cases[0].case_id);
      return selectCase(caseId, null, { initial: true });
    }).then(function () {
      state.ready = true;
      dom.mapHint.textContent = '点击地图查询该点原生网格水深（空格播放/暂停，←/→ 逐帧）';
      if (window.__fvReadyResolve) window.__fvReadyResolve();
    }).catch(function (err) {
      showFatal('无法加载发布包 catalog.json：' + (err && err.message ? err.message : err));
    });
  }

  function showFatal(message) {
    dom.noticeBar.hidden = false;
    dom.noticeBar.className = 'notice-bar';
    dom.noticeBar.innerHTML = '<strong>发布包加载失败</strong><br>' + escapeHtml(message) +
      '<br><span style="color:var(--text-3)">请确认通过 scripts/start_local.cmd（127.0.0.1:8765）访问，且 bundle 目录完整。</span>';
    dom.statusBundle.textContent = '发布包：加载失败';
  }

  function applyCatalogChrome(catalog) {
    var display = catalog.display || {};
    if (dom.projectTitle) dom.projectTitle.textContent = '「驭洪」洪涝推演系统';
    state.threshold = display.default_threshold_m != null ? display.default_threshold_m : 0.05;
    if (dom.temporalNote) dom.temporalNote.textContent = '回放步长: 6 h/帧';
    if (dom.chartNote) dom.chartNote.textContent = (display.area_threshold_note || '') ||
      '面积为原生模型网格单帧有效像元 depth>阈值 的面积；不是资产安全等级。';
    if (dom.statusBundle) dom.statusBundle.textContent = '系统发布包：' + (catalog.bundle_id || '—') +
      (catalog.created_utc ? ' · 构建于 ' + catalog.created_utc : '');
    if (dom.statusLicense) dom.statusLicense.textContent = '许可：' + (catalog.license_notice || '见 licenses/LICENSES.md');
    buildThresholdSelect(catalog);
  }

  /* -------------------------------------------------------------- 案例切换 */

  function buildCaseTabs(catalog, activeCaseId) {
    clearNode(dom.caseTabs);
    (catalog.cases || []).forEach(function (c) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'case-tab';
      btn.textContent = c.name || c.case_id;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', c.case_id === activeCaseId ? 'true' : 'false');
      btn.dataset.caseId = c.case_id;
      btn.title = (c.event_name || '') + '　' + (c.subtitle || '');
      btn.addEventListener('click', function () {
        if (state.caseId === c.case_id) return;
        selectCase(c.case_id, null);
      });
      dom.caseTabs.appendChild(btn);
    });
  }

  function buildRunSelect(caseObj, runId) {
    clearNode(dom.runSelect);
    (caseObj.runs || []).forEach(function (run) {
      var opt = document.createElement('option');
      opt.value = run.run_id;
      opt.textContent = run.label + '（' + run.frame_count + ' 帧）';
      if (run.run_id === runId) opt.selected = true;
      dom.runSelect.appendChild(opt);
    });
    dom.runSelect.disabled = (caseObj.runs || []).length < 2;
  }

  /**
   * 切换案例（runId 为空时用 default_run_id）。
   * 令牌：state.loadToken 递增，过期响应直接丢弃。
   */
  function selectCase(caseId, runId, opts) {
    opts = opts || {};
    var catalog = state.catalog;
    var caseObj = (catalog.cases || []).filter(function (c) {
      return c.case_id === caseId;
    })[0];
    if (!caseObj) return Promise.reject(new Error('未找到案例 ' + caseId));

    var token = ++state.loadToken;
    player.pause();
    state.maxMode = false;
    player.setMaxMode(false);
    state.caseId = caseId;
    state.caseObj = caseObj;
    state.lastCommitted = null;
    state.peakFrameId = null;
    state.queryCache.clear();
    clearQueryPanel('切换案例：请重新点击地图查询');
    buildCaseTabs(catalog, caseId);

    var targetRunId = runId || caseObj.default_run_id;
    var runObj = (caseObj.runs || []).filter(function (r) {
      return r.run_id === targetRunId;
    })[0] || (caseObj.runs || [])[0];
    if (!runObj) return Promise.reject(new Error('案例缺少 run：' + caseId));
    state.runId = runObj.run_id;
    state.runObj = runObj;
    buildRunSelect(caseObj, runObj.run_id);

    return loadRunData(caseObj, runObj, token, opts).then(function () {
      if (token !== state.loadToken) return null;
      return ensureMap().then(function () {
        if (token !== state.loadToken) return null;
        return fvmap.setCaseRun({
          caseObj: caseObj,
          runObj: runObj,
          images: state.images
        });
      }).then(function () {
        if (token !== state.loadToken) return null;
        resetLayerStateForCase(caseObj, runObj);
        applyPreset(findInitialPreset(catalog), { silent: true });
        renderLayerList();
        renderLegend();
        renderQuality();
        renderTimelineTicks();
        renderPlayerState(player.state());
        renderHeaderBadges();
        // 首帧提交
        return player.goTo(0, { reason: 'case-switch' });
      });
    }).catch(function (err) {
      if (err && err.stale) return null;
      onCommitError(0, null, err);
      return null;
    });
  }

  /** 地图只初始化一次（重复 new Map 会抛「容器已有子节点」）。 */
  function ensureMap() {
    if (state.mapReady) return Promise.resolve();
    return fvmap.init('map').then(function () {
      state.mapReady = true;
    });
  }

  function loadRunData(caseObj, runObj, token, opts) {
    var caseId = caseObj.case_id;
    var runId = runObj.run_id;
    var layers = caseObj.layers || {};
    var imagesUrl = (layers.flood && layers.flood.images) || null;
    // layers.flood.images 只指向默认 run；其它 run 按目录规则推导
    if (runId !== caseObj.default_run_id) {
      imagesUrl = 'frames/' + caseId + '/' + runId + '/images.json';
    }
    var numericUrl = runObj.numeric_manifest;
    var jobs = [
      fetchJson(runObj.timeline, token),
      runObj.statistics ? fetchJson(runObj.statistics, token) : Promise.resolve(null),
      imagesUrl ? fetchJson(imagesUrl, token) : Promise.resolve(null),
      numericUrl ? fetchJson(numericUrl, token) : Promise.resolve(null)
    ];
    return Promise.all(jobs).then(function (res) {
      if (token !== state.loadToken) return null;
      state.timeline = res[0];
      state.statistics = res[1];
      state.images = res[2];
      state.numericManifest = res[3];
      player.setData({
        timeline: state.timeline,
        images: state.images,
        caseId: caseId,
        runId: runId
      });
      state.peakFrameId = state.statistics && state.statistics.max_envelope
        ? state.statistics.max_envelope.peak_frame_id : null;
      dom.frameSlider.min = '0';
      dom.frameSlider.max = String(Math.max(0, player.frameCount() - 1));
      dom.frameSlider.value = '0';
      return null;
    });
  }

  /* -------------------------------------------------------------- 图层状态机 */

  var LAYER_META = {
    flood: { name: '模型水深（逐帧）', note: '模型网格 1:1 预着色，逐帧贴图' },
    max: { name: '全程最大水深包络', note: '模型自带 res.max；非某一时刻' },
    terrain_source: { name: '原始 DEM（未裁剪）', note: 'ArcGIS Elevation 系列色带；原始未裁切数字高程' },
    terrain_edit: { name: '地形修改证据', note: '被预处理改写的像元；不是观测' },
    dem: { name: '模型网格地形（100 m）', note: 'LISFLOOD-FP 计算网格地形；真实DEM零下切' },
    optical: { name: '可见光背景', note: '采集时间固定，不随模拟时间变化' },
    sar: { name: 'SAR 水体', note: 'SAR 是观测范围证据，不是水深' },
    boundary: { name: '模型域边界', note: '模型计算域外框（WGS84）' },
    sites: { name: '站点/参考点', note: '参考点坐标，非业务核证' }
  };

  var STATUS_TEXT = {
    ready: '可用',
    partial: '部分覆盖',
    remote_verified: '远端可下载',
    local_unchecked: '本地未质检',
    missing: '未提供',
    failed: '加载失败'
  };

  function statusClass(status) {
    if (status === 'ready') return 'ready';
    if (status === 'partial') return 'partial';
    return 'bad';
  }

  function isUsable(status) {
    return status === 'ready' || status === 'partial';
  }

  function defaultOpacity(group) {
    switch (group) {
      case 'flood': return 0.88;
      case 'max': return 0.88;
      case 'dem': return 0.65;
      case 'terrain_source': return 0.85;
      case 'terrain_edit': return 1;
      case 'optical': return 1;
      case 'sar': return 0.75;
      case 'boundary': return 1;
      case 'sites': return 1;
      default: return 1;
    }
  }

  function resetLayerStateForCase(caseObj, runObj) {
    var layers = caseObj.layers || {};
    var order = (state.catalog.layer_order ||
      ['terrain_source', 'terrain_edit', 'dem', 'optical', 'sar', 'flood', 'max', 'boundary', 'sites'])
      .filter(function (g) {
        return g !== 'basemap';
      });
    state.layerState = {};
    order.forEach(function (group) {
      var def = layers[group] || {};
      var status = def.status || 'missing';
      var reasonParts = [];
      if (def.summary) reasonParts.push(def.summary);
      if (def.note) reasonParts.push(def.note);
      if (def.missing_reason) reasonParts.push('缺失原因：' + def.missing_reason);
      if (!isUsable(status)) {
        if (status === 'remote_verified') reasonParts.unshift('远端可下载、尚未导入发布包，禁用');
        else if (status === 'local_unchecked') reasonParts.unshift('本地资产未完成发布质检，禁用');
        else if (status === 'missing') reasonParts.unshift('发布包未提供该图层，禁用');
        else if (status === 'failed') reasonParts.unshift('图层构建/加载失败，禁用');
      }
      var available = isUsable(status) && hasLayerAsset(group, def, caseObj);
      state.layerState[group] = {
        group: group,
        status: status,
        label: def.label || (LAYER_META[group] && LAYER_META[group].name) || group,
        available: available,
        reason: reasonParts.join('；'),
        visible: available,
        opacity: defaultOpacity(group),
        acquiredAt: def.acquired_at || null,
        assetId: def.asset_id || null,
        legend: def.legend || null
      };
    });
    state.layerState.flood.label = state.layerState.flood.label || '模型水深（逐帧）';
    if (state.layerState.max) state.layerState.max.label = '全程最大水深包络';
    if (!state.layerState.sites) {
      state.layerState.sites = {
        group: 'sites', status: 'missing', label: '站点/参考点', available: false,
        reason: '未提供站点图层', visible: false, opacity: 1
      };
    }
  }

  function hasLayerAsset(group, def, caseObj) {
    if (group === 'flood') return !!(def.images || state.images);
    if (group === 'max') {
      return !!(def.image || def.images || (def.parts && def.parts.length) || (state.images && state.images.max));
    }
    if (group === 'boundary' || group === 'sites') return !!def.geojson;
    // raster：可能是单图 image，也可能是 parts 索引 images（如 DEM 1×2 分块）
    return !!(def.image || def.images || (def.parts && def.parts.length));
  }

  function renderLayerList() {
    clearNode(dom.layerList);
    Object.keys(state.layerState).forEach(function (group) {
      var st = state.layerState[group];
      var meta = LAYER_META[group] || { name: st.label, note: '' };
      var hiddenByMax = state.maxMode && group === 'flood';
      var item = document.createElement('div');
      item.className = 'layer-item' + (st.available ? '' : ' disabled');
      item.dataset.layer = group;

      var head = document.createElement('div');
      head.className = 'layer-head';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'layer-cb-' + group;
      cb.checked = hiddenByMax ? false : !!st.visible;
      cb.disabled = !st.available || hiddenByMax;
      cb.setAttribute('aria-label', meta.name);
      cb.addEventListener('change', function () {
        if (!st.available) return;
        if (group === 'max') {
          toggleMaxMode(cb.checked);
        } else {
          st.visible = cb.checked;
          fvmap.setGroupVisible(group, st.visible);
          markCustomPreset();
          renderLayerList();
          renderStatusLayers();
          updateNotice();
        }
      });
      head.appendChild(cb);

      var name = document.createElement('label');
      name.className = 'layer-name';
      name.htmlFor = cb.id;
      name.textContent = meta.name;
      if (st.acquiredAt) {
        var small = document.createElement('small');
        small.textContent = '　采集 ' + (TL.formatAbsoluteTime(st.acquiredAt) || st.acquiredAt);
        name.appendChild(small);
      }
      head.appendChild(name);

      var badge = document.createElement('span');
      badge.className = 'layer-status ' + statusClass(st.status);
      badge.textContent = STATUS_TEXT[st.status] || st.status;
      head.appendChild(badge);
      item.appendChild(head);

      item.title = (!st.available && st.reason) ? st.reason : (hiddenByMax ? '包络模式下逐帧水深暂时隐藏' : (meta.note || ''));

      var op = document.createElement('div');
      op.className = 'layer-opacity';
      var opLabel = document.createElement('label');
      opLabel.textContent = '透明度';
      opLabel.htmlFor = 'layer-op-' + group;
      op.appendChild(opLabel);
      var range = document.createElement('input');
      range.type = 'range';
      range.id = 'layer-op-' + group;
      range.min = '0';
      range.max = '100';
      range.step = '1';
      range.value = String(Math.round(st.opacity * 100));
      range.disabled = !st.available;
      var out = document.createElement('output');
      out.textContent = Math.round(st.opacity * 100) + '%';
      range.addEventListener('input', function () {
        st.opacity = Number(range.value) / 100;
        out.textContent = range.value + '%';
        fvmap.setGroupOpacity(group, st.opacity);
      });
      op.appendChild(range);
      op.appendChild(out);
      item.appendChild(op);

      dom.layerList.appendChild(item);
    });
    syncMaxButton();
    renderStatusLayers();
    updateNotice();
  }

  function renderStatusLayers() {
    var groups = Object.keys(state.layerState).filter(function (g) {
      return state.layerState[g].visible;
    });
    var total = Object.keys(state.layerState).length;
    dom.statusLayers.textContent = '可见本地图层：' + groups.length + '/' + total +
      (state.basemapEnabled ? '　+ 在线底图' : '');
  }

  /* -------------------------------------------------------------- 预设 */

  function buildPresetButtons(catalog) {
    clearNode(dom.presetButtons);
    var presets = catalog.presets || [];
    presets.forEach(function (p) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'preset-btn';
      btn.textContent = p.label;
      btn.title = '预设：' + p.label + (p.id === 'custom' ? '（按当前勾选状态）' : '');
      btn.dataset.presetId = p.id;
      btn.setAttribute('aria-pressed', 'false');
      btn.addEventListener('click', function () {
        applyPreset(p.id);
      });
      dom.presetButtons.appendChild(btn);
    });
  }

  function findInitialPreset(catalog) {
    var has = (catalog.presets || []).filter(function (p) {
      return p.id === 'all_available';
    });
    return has.length ? 'all_available' : ((catalog.presets || [])[0] || {}).id || null;
  }

  function applyPreset(presetId, opts) {
    opts = opts || {};
    var catalog = state.catalog;
    var preset = (catalog.presets || []).filter(function (p) {
      return p.id === presetId;
    })[0];
    if (!preset) return;
    state.presetId = presetId;
    if (presetId !== 'custom') {
      var wanted = preset.layers || [];
      Object.keys(state.layerState).forEach(function (group) {
        var st = state.layerState[group];
        if (group === 'max') {
          st.visible = wanted.indexOf('max') >= 0 && st.available;
          return;
        }
        st.visible = st.available && wanted.indexOf(group) >= 0;
      });
      if (state.maxMode && !state.layerState.max.visible) toggleMaxMode(false, { silent: true });
      Object.keys(state.layerState).forEach(function (group) {
        if (group === 'max') return;
        fvmap.setGroupVisible(group, state.layerState[group].visible);
        fvmap.setGroupOpacity(group, state.layerState[group].opacity);
      });
      fvmap.setGroupOpacity('max', state.layerState.max ? state.layerState.max.opacity : 0.88);
      var maxSt = state.layerState.max;
      if (maxSt && maxSt.visible && !state.maxMode) toggleMaxMode(true, { silent: true });
    }
    Array.prototype.forEach.call(dom.presetButtons.children, function (btn) {
      btn.setAttribute('aria-pressed', btn.dataset.presetId === state.presetId ? 'true' : 'false');
    });
    if (!opts.silent) {
      renderLayerList();
      renderLegend();
    }
  }

  function markCustomPreset() {
    if (state.presetId === 'custom') return;
    state.presetId = 'custom';
    Array.prototype.forEach.call(dom.presetButtons.children, function (btn) {
      btn.setAttribute('aria-pressed', btn.dataset.presetId === 'custom' ? 'true' : 'false');
    });
  }

  /* -------------------------------------------------------------- 最大包络 */

  function toggleMaxMode(on, opts) {
    opts = opts || {};
    var st = state.layerState.max;
    if (!st || !st.available) {
      showNotice('warn', '最大包络不可用：' + ((st && st.reason) || '发布包未提供'));
      syncMaxButton();
      return;
    }
    state.maxMode = !!on;
    player.setMaxMode(state.maxMode);
    if (state.maxMode) {
      player.pause();
      fvmap.setMaxMode(true);
      fvmap.commitMax().catch(function (err) {
        state.maxMode = false;
        player.setMaxMode(false);
        fvmap.setMaxMode(false);
        showNotice('error', '包络加载失败：' + (err && err.message ? err.message : err));
        syncMaxButton();
        updateNotice();
      });
      applyMaxTitle();
    } else {
      fvmap.setMaxMode(false);
      applyFrameTitle();
      if (state.lastCommitted) {
        player.goTo(state.lastCommitted.index, { reason: 'exit-max' });
      }
    }
    if (st) st.visible = state.maxMode;
    syncMaxButton();
    renderTimelineControls();
    renderLayerList();
    renderLegend();
    updateNotice();
  }

  function syncMaxButton() {
    var on = state.maxMode;
    dom.btnMax.classList.toggle('active', on);
    dom.btnMax.textContent = on ? '返回逐帧回放' : '全程最大包络';
    dom.btnMax.setAttribute('aria-pressed', on ? 'true' : 'false');
    var st = state.layerState.max;
    if (st && !st.available) dom.btnMax.disabled = true;
    else dom.btnMax.disabled = false;
    var cb = document.getElementById('layer-cb-max');
    if (cb) cb.checked = !!on;
  }

  function applyMaxTitle() {
    var catalog = state.catalog || {};
    var title = (catalog.display && catalog.display.max_envelope_title) || '全程最大水深包络（非某一时刻）';
    dom.mapTitle.textContent = title;
    dom.mapTitle.classList.add('max-mode');
    var stats = state.statistics && state.statistics.max_envelope;
    var areaText = stats && stats.areas_km2 ? fmtArea(stats.areas_km2[String(state.threshold)]) : '—';
    dom.mapSubtitle.textContent = '包络面积（阈值 ' + state.threshold + ' m）：' + areaText +
      '；模型自带 res.max，含 6 h 采样间隙的子步瞬时峰值，不等于任一单帧';
  }

  function applyFrameTitle() {
    var catalog = state.catalog || {};
    var title = (catalog.display && catalog.display.current_frame_title) || '当前帧模拟水深';
    dom.mapTitle.textContent = title;
    dom.mapTitle.classList.remove('max-mode');
    updateMapSubtitle();
  }

  function updateMapSubtitle() {
    if (state.maxMode) {
      applyMaxTitle();
      return;
    }
    var frame = player.frame();
    if (!frame) {
      dom.mapSubtitle.textContent = '';
      return;
    }
    var rel = TL.formatTPlus(frame.sim_seconds);
    var abs = TL.formatAbsoluteTime(frame.timestamp);
    var st = state.statistics && state.statistics.frames ? findStat(frame.frame_id) : null;
    var parts = ['当前帧 ' + frame.frame_id, rel];
    if (abs) parts.push(abs);
    if (st && st.areas_km2) parts.push('面积 ' + fmtArea(st.areas_km2[String(state.threshold)]));
    dom.mapSubtitle.textContent = parts.join('　·　');
  }

  /* -------------------------------------------------------------- 时间轴 */

  function renderPlayerState(ps) {
    dom.frameSlider.value = String(ps.index);
    dom.frameSlider.disabled = state.maxMode || !ps.frameCount;
    dom.btnPrev.disabled = state.maxMode || ps.index <= 0 || ps.busy;
    dom.btnNext.disabled = state.maxMode || ps.index >= ps.frameCount - 1 || ps.busy;
    dom.btnPlay.textContent = ps.playing ? '⏸ 暂停' : '▶ 播放';
    dom.btnPlay.setAttribute('aria-pressed', ps.playing ? 'true' : 'false');
    dom.btnPlay.disabled = state.maxMode || ps.frameCount < 2;
    Array.prototype.forEach.call(dom.speedGroup.querySelectorAll('.speed-btn'), function (btn) {
      btn.setAttribute('aria-pressed', Number(btn.dataset.speed) === ps.speed ? 'true' : 'false');
      btn.disabled = state.maxMode;
    });
    dom.frameReadout.textContent = ps.frameCount ? ('帧 ' + (ps.index + 1) + ' / ' + ps.frameCount) : '帧 — / —';
    if (state.maxMode) {
      dom.timeRelative.textContent = '包络视图';
      dom.timeAbsolute.textContent = '时间轴已暂停';
      dom.timeAbsolute.classList.add('unavailable');
    } else {
      var frame = player.frame();
      if (frame) {
        dom.timeRelative.textContent = TL.formatTPlus(frame.sim_seconds);
        var abs = TL.formatAbsoluteTime(frame.timestamp);
        if (abs) {
          dom.timeAbsolute.textContent = abs;
          dom.timeAbsolute.classList.remove('unavailable');
        } else {
          dom.timeAbsolute.textContent = '绝对时间不可用（时间起点为候选依据）';
          dom.timeAbsolute.classList.add('unavailable');
        }
      } else {
        dom.timeRelative.textContent = 'T+— h';
        dom.timeAbsolute.textContent = '—';
      }
    }
    updateOriginNote();
    renderTimelineControls();
    renderChartNote();
    updateMapSubtitle();
    drawChart();
  }

  /** 曲线说明随口径切换：逐帧面积 / 全程包络，避免两个口径同屏混称。 */
  function renderChartNote() {
    if (!dom.chartNote) return;
    var display = (state.catalog && state.catalog.display) || {};
    if (state.maxMode) {
      dom.chartNote.textContent = '包络视角：横线为全程最大水深包络的面积（含 6 h 采样间隙的子步瞬时峰值），' +
        '时间轴已暂停；不是某一帧的面积。';
    } else {
      dom.chartNote.textContent = display.area_threshold_note ||
        '面积为原生网格单帧有效像元 depth>阈值 的面积；不是资产安全等级。';
    }
  }

  function renderTimelineControls() {
    var disabled = state.maxMode;
    dom.frameSlider.disabled = disabled || !player.frameCount();
    dom.btnPrev.disabled = disabled || player.index <= 0;
    dom.btnNext.disabled = disabled || player.index >= player.frameCount() - 1;
    dom.btnPlay.disabled = disabled || player.frameCount() < 2;
    renderTimelineNotes();
  }

  function updateOriginNote() {
    if (!state.timeline) {
      dom.originNote.textContent = '';
      return;
    }
    var text = TL.describeOrigin(state.timeline);
    var every = state.timeline.save_interval_s ? state.timeline.save_interval_s / 3600 : 6;
    dom.originNote.textContent = text + '　·　' + every + ' h/帧';
  }

  function renderTimelineTicks() {
    clearNode(dom.frameTicks);
    var n = player.frameCount();
    if (!n) return;
    var trackW = dom.frameTicks.clientWidth || 1720;
    var posOf = function (i) {
      return n > 1 ? (i / (n - 1)) * 100 : 0;
    };
    var peakIdx = -1;
    if (state.peakFrameId) {
      for (var k = 0; k < n; k += 1) {
        if (player.frame(k).frame_id === state.peakFrameId) {
          peakIdx = k;
          break;
        }
      }
    }
    // 刻度线：每帧一条
    for (var i = 0; i < n; i += 1) {
      var tick = document.createElement('div');
      tick.className = 'frame-tick';
      tick.style.left = posOf(i) + '%';
      dom.frameTicks.appendChild(tick);
    }
    // 文字标签：按可用宽度决定间隔，避免重叠（标签约 62 px 宽）
    var maxLabels = Math.max(2, Math.floor(trackW / 76));
    var step = Math.max(1, Math.ceil((n - 1) / Math.max(1, maxLabels - 1)));
    var labeled = [];
    for (var j = 0; j < n; j += step) labeled.push(j);
    if (labeled[labeled.length - 1] !== n - 1) {
      // 末帧总要有标签；若与倒数第二个太近则替换它
      if (labeled.length > 1 && (n - 1 - labeled[labeled.length - 1]) < step * 0.55) {
        labeled[labeled.length - 1] = n - 1;
      } else {
        labeled.push(n - 1);
      }
    }
    labeled.forEach(function (idx) {
      var frame = player.frame(idx);
      if (!frame) return;
      var label = document.createElement('div');
      label.className = 'frame-tick-label';
      label.style.left = posOf(idx) + '%';
      if (idx === 0) label.style.transform = 'translateX(0)';
      else if (idx === n - 1) label.style.transform = 'translateX(-100%)';
      label.textContent = TL.formatTPlus(frame.sim_seconds);
      dom.frameTicks.appendChild(label);
    });
    // 峰值：只画一条竖向标记线（文字放在时间轴注释区，避免与帧标签重叠）
    if (peakIdx >= 0) {
      var peak = document.createElement('div');
      peak.className = 'frame-tick peak';
      peak.style.left = posOf(peakIdx) + '%';
      peak.title = '峰值帧 ' + state.peakFrameId +
        (player.frame(peakIdx) ? '（' + TL.formatTPlus(player.frame(peakIdx).sim_seconds) + '）' : '');
      dom.frameTicks.appendChild(peak);
    }
  }

  /** 时间轴注释：帧间隔、峰值帧、时间起点依据。 */
  function renderTimelineNotes() {
    var parts = [];
    var display = (state.catalog && state.catalog.display) || {};
    if (state.maxMode) {
      parts.push('包络模式已暂停');
    } else {
      parts.push('回放: 6 h/帧');
    }
    var stats = state.statistics && state.statistics.max_envelope;
    if (stats && stats.peak_frame_id && state.timeline) {
      var peakFrame = null;
      for (var i = 0; i < player.frameCount(); i += 1) {
        if (player.frame(i).frame_id === stats.peak_frame_id) {
          peakFrame = player.frame(i);
          break;
        }
      }
      if (peakFrame) {
        parts.push('峰值帧 ' + stats.peak_frame_id + '（' + TL.formatTPlus(peakFrame.sim_seconds) + '）');
      }
    }
    dom.temporalNote.textContent = parts.join('　·　');
  }

  /* -------------------------------------------------------------- 帧提交 */

  function commitFrame(index, frame, imageEntry, token) {
    if (!frame) return Promise.reject(new Error('时间轴缺少该帧'));
    if (!imageEntry || !imageEntry.parts || !imageEntry.parts.length) {
      return Promise.reject(new Error('帧 ' + frame.frame_id + ' 缺少 depth_image parts（帧缺失）'));
    }
    return fvmap.commitFrame(imageEntry, token);
  }

  function onFrameCommitted(index, frame, imageEntry) {
    var wet = imageEntry && imageEntry.wet_pixels != null ? imageEntry.wet_pixels : null;
    if (wet == null && frame) {
      // 真包 images.json 必带 wet_pixels；缺失时用 statistics 的同帧面积/湿像元推断，避免误报
      var st = findStat(frame.frame_id);
      wet = st && st.wetted_cells && st.wetted_cells[String(state.threshold)] != null
        ? (st.wetted_cells[String(state.threshold)] > 0 ? 1 : 0) : null;
    }
    state.lastCommitted = {
      index: index,
      frameId: frame ? frame.frame_id : null,
      wetPixels: wet
    };
    applyFrameTitle();
    updateNotice();
    drawChart();
    dom.frameSlider.value = String(index);
    if (state.pendingQuery) {
      // 帧切换后刷新点查结果（用户刚点过同一位置）
      var pq = state.pendingQuery;
      state.pendingQuery = null;
      queryAt(pq.lngLat[0], pq.lngLat[1], { quiet: true });
    }
  }

  function onCommitError(index, frame, err) {
    var msg = (err && err.message) || String(err);
    if (/stale/.test(msg)) return;
    player.pause();
    showNotice('error', '帧加载失败：' + msg); // 立即显示；随后 updateNotice 统一排序
    updateNotice();
  }

  /* -------------------------------------------------------------- 提示条 */

  function showNotice(level, html) {
    state.notice = { level: level, html: html };
    state.noticeAll = [state.notice];
    renderNotice();
  }

  function updateNotice() {
    var errors = [];
    var warns = [];
    var infos = [];
    if (state.basemapEnabled && fvmap && fvmap.basemap && fvmap.basemap.failing) {
      warns.push({ level: 'warn', html: '<strong>在线底图不可用</strong>：已保留本地图层；未自动切换其它地图。可点击左侧“重试在线底图”。' });
    }
    if (state.maxMode) {
      infos.push({ level: 'info', html: '<strong>全程最大水深包络（非某一时刻）</strong>：时间回放已暂停；关闭包络后恢复原帧。' });
    }
    if (!state.maxMode && state.lastCommitted && state.lastCommitted.wetPixels === 0) {
      var frame = player.frame();
      var rel = frame ? TL.formatTPlus(frame.sim_seconds) : '';
      infos.push({ level: 'info', html: '当前帧无淹没像元（' + rel + '，模型该时刻全干）；这是真实结果，不是加载失败。' });
    }
    if (player.lastError && !state.maxMode) {
      errors.push({
        level: 'error',
        html: '<strong>帧缺失/加载失败</strong>：' + escapeHtml(player.lastError.message) +
          '　播放已暂停；不会用旧帧或 0 值冒充。'
      });
    }
    var all = errors.concat(warns, infos);
    state.noticeAll = all;
    state.notice = all.length ? all[0] : null;
    renderNotice();
  }

  function renderNotice() {
    var list = state.noticeAll || (state.notice ? [state.notice] : []);
    if (!list.length) {
      dom.noticeBar.hidden = true;
      dom.noticeBar.innerHTML = '';
      return;
    }
    dom.noticeBar.hidden = false;
    dom.noticeBar.className = 'notice-bar ' + list[0].level;
    dom.noticeBar.innerHTML = list.map(function (n) {
      return '<div>' + n.html + '</div>';
    }).join('<div style="height:4px"></div>');
  }

  /* -------------------------------------------------------------- 底图 */

  function setupBasemaps(catalog) {
    var basemaps = catalog.basemaps || [];
    clearNode(dom.basemapSelect);
    basemaps.forEach(function (b) {
      var opt = document.createElement('option');
      opt.value = b.id;
      opt.textContent = b.label;
      dom.basemapSelect.appendChild(opt);
    });
    state.basemapId = basemaps.length ? basemaps[0].id : null;
    if (state.basemapId) dom.basemapSelect.value = state.basemapId;
    dom.basemapSelect.disabled = basemaps.length === 0;
    dom.basemapToggle.disabled = basemaps.length === 0;
  }

  function onBasemapState(info) {
    state.basemapEnabled = !!info.enabled;
    dom.basemapWarn.hidden = !info.failing;
    if (info.failing) {
      dom.basemapWarn.textContent = '在线底图不可用：' + (info.message || '瓦片加载失败') + '（本地图层仍可用）';
    }
    dom.basemapRetry.hidden = !info.failing;
    dom.statusAttribution.textContent = info.failing
      ? '在线底图不可用'
      : (info.attribution ? '底图署名：' + info.attribution : '在线底图未启用');
    renderStatusLayers();
    updateNotice();
  }

  function onLayerError(sourceId, message) {
    updateNotice();
  }

  function onScale(mPerPx, zoom) {
    if (!dom.mapHint) return;
    var text = '点击地图查询原生网格水深';
    if (mPerPx) text += '　·　屏幕 ' + mPerPx.toFixed(1) + ' m/px（z' + zoom.toFixed(1) + '）';
    dom.mapHint.textContent = text;
  }

  /* -------------------------------------------------------------- 点查询 */

  var PROJ4_DEFS = {
    'EPSG:32648': '+proj=utm +zone=48 +datum=WGS84 +units=m +no_defs',
    'EPSG:32649': '+proj=utm +zone=49 +datum=WGS84 +units=m +no_defs',
    'EPSG:32650': '+proj=utm +zone=50 +datum=WGS84 +units=m +no_defs'
  };

  function ensureProj4Def(crs) {
    if (!crs) return null;
    if (window.proj4.defs(crs)) return crs;
    if (PROJ4_DEFS[crs]) {
      window.proj4.defs(crs, PROJ4_DEFS[crs]);
      return crs;
    }
    var m = /^EPSG:(32[67])(\d{2})$/.exec(crs);
    if (m) {
      var zone = Number(m[2]);
      var south = m[1] === '327';
      var proj = '+proj=utm +zone=' + zone + (south ? ' +south' : '') + ' +datum=WGS84 +units=m +no_defs';
      window.proj4.defs(crs, proj);
      return crs;
    }
    return null;
  }

  function gridOf() {
    if (state.numericManifest && state.numericManifest.grid) return state.numericManifest.grid;
    if (state.caseObj && state.caseObj.grid) return state.caseObj.grid;
    return null;
  }

  function inverseAffine(transform, x, y) {
    var a = transform[0], b = transform[1], c = transform[2];
    var d = transform[3], e = transform[4], f = transform[5];
    var det = a * e - b * d;
    if (!det) return null;
    var dx = x - c;
    var dy = y - f;
    var colF = (e * dx - b * dy) / det;
    var rowF = (-d * dx + a * dy) / det;
    return { colF: colF, rowF: rowF };
  }

  function handleMapClick(lng, lat) {
    if (!state.ready && !state.timeline) return;
    state.pendingQuery = { lngLat: [lng, lat] };
    queryAt(lng, lat);
  }

  function queryAt(lng, lat, opts) {
    opts = opts || {};
    var grid = gridOf();
    if (!grid) {
      var noGrid = { state: 'no-data', message: '发布包未提供数值网格清单（numeric manifest）' };
      renderQuery(noGrid);
      return noGrid;
    }
    var crs = grid.crs || (state.caseObj && state.caseObj.grid && state.caseObj.grid.crs);
    var crsKey = ensureProj4Def(crs);
    if (!crsKey) {
      var badCrs = { state: 'error', message: '不支持的坐标系：' + crs };
      renderQuery(badCrs);
      return badCrs;
    }
    var native;
    try {
      native = window.proj4('EPSG:4326', crsKey, [lng, lat]);
    } catch (err) {
      renderQuery({ state: 'error', message: '坐标转换失败：' + (err && err.message) });
      return;
    }
    if (!isFinite(native[0]) || !isFinite(native[1])) {
      var badNative = { state: 'outside', lngLat: [lng, lat], message: '坐标转换失败（域外）' };
      renderQuery(badNative);
      return badNative;
    }
    var rc = inverseAffine(grid.transform, native[0], native[1]);
    if (!rc) {
      var badTransform = { state: 'error', message: '网格 transform 非法' };
      renderQuery(badTransform);
      return badTransform;
    }
    var col = Math.floor(rc.colF);
    var row = Math.floor(rc.rowF);
    if (row < 0 || col < 0 || row >= grid.rows || col >= grid.cols) {
      var outsidePayload = {
        state: 'outside',
        lngLat: [lng, lat],
        native: native,
        rowF: rc.rowF,
        colF: rc.colF,
        grid: grid,
        runId: state.runId,
        cellSizeM: grid.cell_size_m,
        crs: grid.crs,
        message: '该点位于模型域外（不在 rows×cols 网格内），不夹边取值'
      };
      renderQuery(outsidePayload);
      return outsidePayload;
    }

    var frame = player.frame();
    var useMax = state.maxMode;
    var queryKey = [state.caseId, state.runId, useMax ? 'max' : (frame ? frame.frame_id : '—'),
      row, col].join('|');
    if (state.queryCache.has(queryKey)) {
      var cachedPayload = state.queryCache.get(queryKey);
      renderQuery(cachedPayload);
      return cachedPayload;
    }

    renderQuery({ state: 'loading', lngLat: [lng, lat], row: row, col: col, grid: grid, maxMode: useMax });

    var token = ++state.queryToken;
    return readCell(row, col, grid, frame, useMax, token).then(function (res) {
      var payload = {
        state: isNaN(res.value) ? 'nan' : 'ok',
        value: res.value,
        lngLat: [lng, lat],
        native: native,
        row: row,
        col: col,
        grid: grid,
        frame: frame,
        frameId: frame ? frame.frame_id : null,
        runId: state.runId,
        caseId: state.caseId,
        cellSizeM: grid.cell_size_m,
        crs: grid.crs,
        maxMode: useMax,
        cellArea: grid.cell_area_m2,
        blockPath: res.blockPath,
        cached: res.cached
      };
      if (token !== state.queryToken) return payload;
      state.queryCache.set(queryKey, payload);
      if (state.queryCache.size > state.queryCacheMax) {
        var firstKey = state.queryCache.keys().next().value;
        state.queryCache.delete(firstKey);
      }
      renderQuery(payload);
      return payload;
    }).catch(function (err) {
      var errorPayload = {
        state: 'error',
        lngLat: [lng, lat],
        row: row,
        col: col,
        runId: state.runId,
        cellSizeM: grid.cell_size_m,
        message: (err && err.message) || String(err)
      };
      if (token === state.queryToken) renderQuery(errorPayload);
      return errorPayload;
    });
  }

  function readCell(row, col, grid, frame, useMax, token) {
    var manifest = state.numericManifest;
    var blockSize = (manifest && manifest.block_size) || 256;
    var by = Math.floor(row / blockSize);
    var bx = Math.floor(col / blockSize);
    var r0 = by * blockSize;
    var c0 = bx * blockSize;
    var r1 = Math.min(grid.rows, (by + 1) * blockSize);
    var c1 = Math.min(grid.cols, (bx + 1) * blockSize);
    var bw = c1 - c0;
    var url;
    if (useMax) {
      var tpl = manifest && manifest.max ? manifest.max.path_template : null;
      if (!tpl) return Promise.reject(new Error('包络数值块不可用（manifest.max 缺失）'));
      url = tpl;
    } else {
      if (!frame || !frame.query_blocks) return Promise.reject(new Error('该帧缺少 query_blocks 模板'));
      url = frame.query_blocks;
    }
    url = url.replace('{block_y}', String(by)).replace('{block_x}', String(bx));
    var cacheKey = url;
    var cached = state.blockCache.get(cacheKey);
    var p;
    if (cached) {
      p = Promise.resolve(cached);
    } else {
      p = fetch(url, { cache: 'force-cache' }).then(function (r) {
        if (!r.ok) throw new Error('数值块 HTTP ' + r.status + '：' + url);
        return r.arrayBuffer();
      }).then(function (buf) {
        var view = new DataView(buf);
        var count = Math.floor(buf.byteLength / 4);
        var arr = new Float32Array(count);
        for (var i = 0; i < count; i += 1) arr[i] = view.getFloat32(i * 4, true);
        putBlock(cacheKey, arr);
        return arr;
      });
    }
    return p.then(function (arr) {
      var localRow = row - r0;
      var localCol = col - c0;
      var idx = localRow * bw + localCol;
      if (idx < 0 || idx >= arr.length) {
        throw new Error('数值块索引越界（block ' + by + '/' + bx + '，index ' + idx + '，长度 ' + arr.length + '）');
      }
      return { value: arr[idx], blockPath: url, cached: !!cached };
    });
  }

  function putBlock(key, arr) {
    var bytes = arr.byteLength;
    if (state.blockCache.has(key)) {
      state.blockCacheBytes -= state.blockCache.get(key).byteLength;
      state.blockCache.delete(key);
    }
    state.blockCache.set(key, arr);
    state.blockCacheBytes += bytes;
    while (state.blockCacheBytes > state.blockCacheMax && state.blockCache.size > 1) {
      var firstKey = state.blockCache.keys().next().value;
      state.blockCacheBytes -= state.blockCache.get(firstKey).byteLength;
      state.blockCache.delete(firstKey);
    }
  }

  function addGridItem(parent, label, val) {
    var d = document.createElement('div');
    d.className = 'query-item';
    var l = document.createElement('span');
    l.className = 'query-label';
    l.textContent = label;
    var v = document.createElement('span');
    v.className = 'query-val';
    v.textContent = val;
    d.appendChild(l);
    d.appendChild(v);
    parent.appendChild(d);
  }

  function renderQuery(payload) {
    var body = dom.queryBody;
    clearNode(body);
    if (payload.state === 'no-data') {
      body.innerHTML = '<p class="hint query-empty">' + escapeHtml(payload.message || '暂无数据') + '</p>';
      return;
    }
    if (payload.state === 'loading') {
      body.innerHTML = '<p class="hint query-empty">网格查询中…（行 ' + payload.row + '，列 ' + payload.col + '）</p>';
      return;
    }
    if (payload.state === 'outside') {
      var row0 = document.createElement('div');
      row0.className = 'query-value-row';
      var p0 = document.createElement('p');
      p0.className = 'query-value';
      p0.textContent = '域外';
      row0.appendChild(p0);
      var tag0 = document.createElement('span');
      tag0.className = 'query-tag outside';
      tag0.textContent = '超出计算域';
      row0.appendChild(tag0);
      body.appendChild(row0);
      appendQueryLonLat(body, payload);
      return;
    }
    if (payload.state === 'error') {
      var pe = document.createElement('p');
      pe.className = 'query-value';
      pe.style.color = 'var(--err)';
      pe.textContent = '查询失败';
      body.appendChild(pe);
      var se = document.createElement('p');
      se.className = 'hint';
      se.textContent = payload.message || '';
      body.appendChild(se);
      if (payload.row != null) appendQueryLonLat(body, payload);
      return;
    }
    var grid = payload.grid || gridOf() || {};
    var isNan = payload.state === 'nan';

    var valRow = document.createElement('div');
    valRow.className = 'query-value-row';
    var valEl = document.createElement('span');
    valEl.className = 'query-value' + (isNan ? ' invalid' : '');
    valEl.textContent = isNan ? '无有效值' : fmtDepth(payload.value, 2);
    valRow.appendChild(valEl);

    if (!isNan && payload.value != null && isFinite(payload.value)) {
      var unitEl = document.createElement('span');
      unitEl.className = 'query-unit';
      unitEl.textContent = 'm';
      valRow.appendChild(unitEl);
    }

    var tagEl = document.createElement('span');
    tagEl.className = 'query-tag ' + (isNan ? 'outside' : (payload.value > 0 ? 'wet' : 'dry'));
    tagEl.textContent = isNan ? 'NoData' : (payload.value > 0 ? '淹没区' : '干地');
    valRow.appendChild(tagEl);
    body.appendChild(valRow);

    var gridBox = document.createElement('div');
    gridBox.className = 'query-grid';
    addGridItem(gridBox, '时间', payload.maxMode ? '全程最大包络' :
      (payload.frame ? TL.formatTPlus(payload.frame.sim_seconds) : '—'));
    addGridItem(gridBox, '网格分辨率', (grid.cell_size_m != null ? grid.cell_size_m + ' m' : '—'));
    addGridItem(gridBox, '行 / 列', payload.row + ' / ' + payload.col);
    addGridItem(gridBox, '来源 Run', state.runId || '—');
    body.appendChild(gridBox);

    var det = document.createElement('details');
    var sum = document.createElement('summary');
    sum.className = 'query-details-summary';
    sum.textContent = '底层数据源详情 ▾';
    det.appendChild(sum);
    var src = document.createElement('div');
    src.className = 'query-src';
    src.textContent = (payload.blockPath || '—') + (payload.cached ? '（缓存）' : '');
    det.appendChild(src);
    body.appendChild(det);
  }

  function appendQueryLonLat(body, payload) {
    if (!payload || !payload.lngLat) return;
    var gridBox = document.createElement('div');
    gridBox.className = 'query-grid';
    addGridItem(gridBox, '经纬度', payload.lngLat[0].toFixed(4) + ', ' + payload.lngLat[1].toFixed(4));
    if (payload.rowF != null) {
      addGridItem(gridBox, '反算行列', payload.rowF.toFixed(1) + ' / ' + payload.colF.toFixed(1));
    }
    addGridItem(gridBox, '来源 Run', state.runId || '—');
    body.appendChild(gridBox);
  }

  function addKv(dl, key, val) {
    var dt = document.createElement('dt');
    dt.textContent = key;
    var dd = document.createElement('dd');
    dd.textContent = val;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function clearQueryPanel(hint) {
    clearNode(dom.queryBody);
    var p = document.createElement('p');
    p.className = 'hint';
    p.textContent = hint || '在地图上单击以查询该点水深。';
    dom.queryBody.appendChild(p);
  }

  /* -------------------------------------------------------------- 面积曲线 */

  function buildThresholdSelect(catalog) {
    clearNode(dom.thresholdSelect);
    var thresholds = (catalog.display && catalog.display.thresholds_m) || [0.05, 0.15, 0.3, 0.5, 1.0];
    thresholds.forEach(function (t) {
      var opt = document.createElement('option');
      opt.value = String(t);
      opt.textContent = t + ' m';
      dom.thresholdSelect.appendChild(opt);
    });
    var def = state.threshold;
    if (thresholds.indexOf(def) < 0) def = thresholds[0];
    state.threshold = def;
    dom.thresholdSelect.value = String(def);
  }

  function seriesFor(threshold) {
    var stats = state.statistics;
    if (!stats || !stats.frames) return [];
    var key = String(threshold);
    return stats.frames.map(function (f) {
      return {
        frameId: f.frame_id,
        simSeconds: f.sim_seconds,
        area: f.areas_km2 && f.areas_km2[key] != null ? f.areas_km2[key] : null,
        wet: f.wetted_cells ? f.wetted_cells[key] : null
      };
    });
  }

  function findStat(frameId) {
    var stats = state.statistics;
    if (!stats || !stats.frames) return null;
    for (var i = 0; i < stats.frames.length; i += 1) {
      if (stats.frames[i].frame_id === frameId) return stats.frames[i];
    }
    return null;
  }

  function drawChart() {
    var canvas = dom.areaChart;
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var cssW = canvas.clientWidth || 360;
    var cssH = canvas.clientHeight || 168;
    var dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var series = seriesFor(state.threshold);
    // 画布颜色与 :root token 单一真源保持一致（canvas 无法直接读 CSS 变量，此处镜像）
    var C = {
      axis: '#cbd5e1', grid: '#f1f5f9', label: '#64748b',
      line: '#0369a1', lineSoft: 'rgba(3, 105, 161, 0.35)',
      fillTop: 'rgba(3, 105, 161, 0.22)', fillBottom: 'rgba(3, 105, 161, 0.02)',
      cursor: '#b45309', cursorSoft: 'rgba(180, 83, 9, 0.85)', cursorText: '#ffd79a'
    };
    if (!series.length) {
      ctx.fillStyle = C.label;
      ctx.font = '12px "Microsoft YaHei", sans-serif';
      ctx.fillText('无统计数据', 12, 24);
      return;
    }
    var padL = 46, padR = 10, padT = 22, padB = 26;
    var plotW = Math.max(10, cssW - padL - padR);
    var plotH = Math.max(10, cssH - padT - padB);
    var maxArea = 0;
    series.forEach(function (p) {
      if (p.area != null && p.area > maxArea) maxArea = p.area;
    });
    var maxEnv = state.statistics && state.statistics.max_envelope && state.statistics.max_envelope.areas_km2;
    if (maxEnv && maxEnv[String(state.threshold)] != null && maxEnv[String(state.threshold)] > maxArea) {
      maxArea = maxEnv[String(state.threshold)];
    }
    if (maxArea <= 0) maxArea = 1;
    var yMax = maxArea * 1.12;

    // 坐标轴
    ctx.strokeStyle = C.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, padT);
    ctx.lineTo(padL, padT + plotH);
    ctx.lineTo(padL + plotW, padT + plotH);
    ctx.stroke();

    // Y 轴刻度（从 0 起，面积单位 km² 写在卡片标题里，避免与刻度叠字）
    ctx.fillStyle = C.label;
    ctx.font = '10px "Cascadia Mono", monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var g = 0; g <= 4; g += 1) {
      var yv = (yMax / 4) * g;
      var y = padT + plotH - (yv / yMax) * plotH;
      ctx.strokeStyle = g === 0 ? C.axis : C.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(yv >= 100 ? yv.toFixed(0) : yv.toFixed(1), padL - 6, y);
    }

    // X 轴刻度（T+ 小时）；首尾标签对齐边界，避免被裁切
    ctx.textBaseline = 'top';
    var n = series.length;
    var tickCount = Math.min(5, n);
    for (var t = 0; t < tickCount; t += 1) {
      var idx = tickCount === 1 ? 0 : Math.round((n - 1) * (t / (tickCount - 1)));
      var px = padL + (n > 1 ? (idx / (n - 1)) * plotW : plotW / 2);
      ctx.textAlign = idx === 0 ? 'left' : (idx === n - 1 ? 'right' : 'center');
      ctx.fillText(TL.formatTPlus(series[idx].simSeconds), px, padT + plotH + 6);
    }

    // 曲线（含前段 0，不裁剪）；包络模式下逐帧曲线降为背景参考
    ctx.strokeStyle = state.maxMode ? C.lineSoft : C.line;
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    var started = false;
    series.forEach(function (p, i) {
      if (p.area == null) return;
      var x = padL + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
      var y = padT + plotH - (p.area / yMax) * plotH;
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();

    // 填充渐变流体区域
    if (started) {
      ctx.lineTo(padL + plotW, padT + plotH);
      var firstIdx = 0;
      ctx.lineTo(padL + (n > 1 ? (firstIdx / (n - 1)) * plotW : plotW / 2), padT + plotH);
      ctx.closePath();
      var grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      grad.addColorStop(0, C.fillTop);
      grad.addColorStop(1, C.fillBottom);
      ctx.fillStyle = grad;
      ctx.fill();
    }

    // 当前帧指示（包络模式下不画“当前帧”游标，避免与包络口径混淆）
    var curIdx = player.index;
    if (!state.maxMode && curIdx >= 0 && curIdx < n) {
      var cx = padL + (n > 1 ? (curIdx / (n - 1)) * plotW : plotW / 2);
      ctx.strokeStyle = C.cursor;
      ctx.lineWidth = 1.4;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(cx, padT);
      ctx.lineTo(cx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      var cur = series[curIdx];
      if (cur && cur.area != null) {
        var cy = padT + plotH - (cur.area / yMax) * plotH;
        ctx.fillStyle = C.cursor;
        ctx.beginPath();
        ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 曲线所属口径：逐帧回放为“单帧面积”，包络模式为“全程包络”
    ctx.fillStyle = C.label;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'top';
    ctx.font = '10px "Microsoft YaHei", sans-serif';
    var envAreas = state.statistics && state.statistics.max_envelope && state.statistics.max_envelope.areas_km2;
    var envArea = envAreas && envAreas[String(state.threshold)] != null ? envAreas[String(state.threshold)] : null;
    if (state.maxMode) {
      // 包络模式：不显示“当前帧”数值（时间轴已暂停），只显示包络值，避免两个口径同屏混称
      ctx.fillStyle = C.cursor;
      ctx.fillText('包络（全程）' + (envArea != null ? ' ' + fmtArea(envArea) : ' 无数据') + '　·　时间轴已暂停',
        cssW - padR, padT - 1);
    } else {
      var curStat = series[curIdx];
      ctx.fillText('阈值 ' + state.threshold + ' m　当前帧 ' + fmtArea(curStat ? curStat.area : null),
        cssW - padR, padT - 1);
    }

    // 包络模式：画全程包络水平参考线（与逐帧曲线区分；逐帧曲线降为背景）
    if (state.maxMode && envArea != null && yMax > 0) {
      var ey = padT + plotH - (envArea / yMax) * plotH;
      ctx.strokeStyle = C.cursorSoft;
      ctx.lineWidth = 1.4;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, ey);
      ctx.lineTo(padL + plotW, ey);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.cursorText;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText('全程包络', padL + 4, ey - 2);
    }
  }

  /* -------------------------------------------------------------- 图例 */

  function renderLegend() {
    var catalog = state.catalog || {};
    var legends = catalog.legends || {};
    clearNode(dom.legendBody);

    var depthClasses = (catalog.display && catalog.display.depth_classes) ||
      (state.images && state.images.color_scale && state.images.color_scale.classes) || [];
    if (state.maxMode) {
      // 包络模式：时间帧层被隐藏，图例只显示“全程最大包络”，不与“当前帧”混称
      if (depthClasses.length) {
        dom.legendBody.appendChild(legendClasses('全程最大水深包络（m）', depthClasses,
          '模型自带 res.max 的全程最大值，含 6 h 采样间隙的子步瞬时峰值；非某一时刻，不能当作任一帧的深度'));
      }
    } else if (state.layerState.flood && state.layerState.flood.visible && depthClasses.length) {
      dom.legendBody.appendChild(legendClasses('模拟水深（m）', depthClasses,
        (legends.depth && legends.depth.note) || ''));
    }
    if (state.layerState.dem && state.layerState.dem.visible && legends.terrain) {
      var modDem = (state.caseObj && state.caseObj.layers && state.caseObj.layers.dem) || {};
      var dDef = Object.assign({}, legends.terrain);
      if (modDem.value_min_m != null) dDef.value_min_m = modDem.value_min_m;
      if (modDem.value_max_m != null) dDef.value_max_m = modDem.value_max_m;
      dom.legendBody.appendChild(legendGradient(
        (legends.terrain.title || '数字高程 DEM（m）'), dDef));
    }
    if (state.layerState.terrain_source && state.layerState.terrain_source.visible &&
      legends.terrain_source) {
      var demLayer = (state.caseObj && state.caseObj.layers && state.caseObj.layers.terrain_source) || {};
      var rangeNote = '';
      if (demLayer.value_min_m != null && demLayer.value_max_m != null) {
        rangeNote = '色带 ' + fmtNum(demLayer.value_min_m) + '–' + fmtNum(demLayer.value_max_m) + ' m';
        if (demLayer.observed_min_m != null && demLayer.observed_max_m != null) {
          rangeNote += '；实测 ' + fmtNum(demLayer.observed_min_m) + '–' + fmtNum(demLayer.observed_max_m) + ' m';
        }
        rangeNote += '（ArcGIS Elevation 色带，按 p1–p99 自适应）';
      }
      var sDef = Object.assign({}, legends.terrain_source);
      if (demLayer.value_min_m != null) sDef.value_min_m = demLayer.value_min_m;
      if (demLayer.value_max_m != null) sDef.value_max_m = demLayer.value_max_m;
      dom.legendBody.appendChild(legendGradient(
        (legends.terrain_source.title || '原始 DEM（m，未裁剪）'),
        sDef, rangeNote));
    }
    if (state.layerState.terrain_edit && state.layerState.terrain_edit.visible &&
      legends.terrain_edit) {
      var editLayer = (state.caseObj && state.caseObj.layers && state.caseObj.layers.terrain_edit) || {};
      dom.legendBody.appendChild(legendClasses(
        legends.terrain_edit.title || '地形修改证据',
        (editLayer.classes && editLayer.classes.length) ? editLayer.classes
          : (legends.terrain_edit.classes || []),
        editLayer.summary || legends.terrain_edit.note || ''));
    }
    if (state.layerState.sar && state.layerState.sar.visible) {
      var sar = legends.binary_water || { title: 'SAR 水体（二值）', classes: [{ color: '#3a86ff', label: '提取为水体' }] };
      dom.legendBody.appendChild(legendClasses(sar.title, sar.classes || [], sar.note || ''));
    }
    if (!dom.legendBody.children.length) {
      var p = document.createElement('p');
      p.className = 'hint';
      p.textContent = '当前没有可见图层；可用左侧预设或勾选图层。';
      dom.legendBody.appendChild(p);
    }
  }

  function legendClasses(title, classes, note) {
    var block = document.createElement('div');
    block.className = 'legend-block';
    var h = document.createElement('h3');
    h.textContent = title;
    block.appendChild(h);
    var rows = document.createElement('div');
    rows.className = 'legend-rows';
    classes.forEach(function (c) {
      var row = document.createElement('div');
      row.className = 'legend-row';
      var sw = document.createElement('span');
      sw.className = 'legend-swatch';
      sw.style.background = c.color;
      row.appendChild(sw);
      var label = document.createElement('span');
      label.textContent = c.label || (c.min_m + '–' + (c.max_m == null ? '∞' : c.max_m) + ' m');
      row.appendChild(label);
      rows.appendChild(row);
    });
    block.appendChild(rows);
    return block;
  }

  function legendGradient(title, def, extraNote) {
    var block = document.createElement('div');
    block.className = 'legend-block';

    var stops = def.stops || [];
    var isDem12 = stops.length === 12;

    var headRow = document.createElement('div');
    headRow.className = 'legend-head-row';
    var h = document.createElement('h3');
    h.textContent = title;
    headRow.appendChild(h);

    if (isDem12) {
      var badge = document.createElement('span');
      badge.className = 'dem-ramp-badge';
      badge.textContent = 'ArcGIS Elevation 12级';
      badge.title = 'ArcGIS 经典高程色带：低地绿·丘陵黄/橙·山地棕·山顶灰白';
      headRow.appendChild(badge);
    }
    block.appendChild(headRow);

    if (isDem12) {
      var paletteWrap = document.createElement('div');
      paletteWrap.className = 'dem-palette-wrap';

      // 12 级离散色阶条
      var discreteBar = document.createElement('div');
      discreteBar.className = 'dem-discrete-bar';
      stops.forEach(function (c, idx) {
        var cell = document.createElement('div');
        cell.className = 'dem-discrete-cell';
        cell.style.background = c;
        cell.title = '梯度 ' + (idx + 1) + ' / 12 (' + c + ')';
        discreteBar.appendChild(cell);
      });
      paletteWrap.appendChild(discreteBar);

      // 连续平滑过渡副色带
      var gradSub = document.createElement('div');
      gradSub.className = 'legend-gradient dem-grad-sub';
      gradSub.style.background = 'linear-gradient(90deg,' + stops.join(',') + ')';
      paletteWrap.appendChild(gradSub);
      block.appendChild(paletteWrap);
    } else {
      var grad = document.createElement('div');
      grad.className = 'legend-gradient';
      grad.style.background = 'linear-gradient(90deg,' + stops.join(',') + ')';
      block.appendChild(grad);
    }

    var scale = document.createElement('div');
    scale.className = 'legend-scale';
    var mnVal = def.value_min_m != null ? def.value_min_m : 0;
    var mxVal = def.value_max_m != null ? def.value_max_m : null;

    var mn = document.createElement('span');
    mn.textContent = (typeof mnVal === 'number' ? fmtNum(mnVal) : mnVal) + ' m';
    scale.appendChild(mn);

    if (mxVal != null && typeof mxVal === 'number' && typeof mnVal === 'number' && mxVal > mnVal) {
      var mid = document.createElement('span');
      mid.className = 'legend-mid-val';
      mid.textContent = fmtNum((mnVal + mxVal) / 2) + ' m';
      scale.appendChild(mid);
    }

    var mx = document.createElement('span');
    mx.textContent = (mxVal != null ? (typeof mxVal === 'number' ? fmtNum(mxVal) : mxVal) : '—') + ' m';
    scale.appendChild(mx);
    block.appendChild(scale);
    return block;
  }

  function fmtNum(v) {
    if (v == null || !isFinite(v)) return '—';
    var a = Math.abs(v);
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  /* -------------------------------------------------------------- 质量面板 */

  function massGateBadge(gate) {
    if (gate === 'PASS') return { cls: 'ok', text: 'PASS' };
    if (gate === 'FAIL-EXPLAINED') return { cls: 'warn', text: 'FAIL-EXPLAINED（不计通过）' };
    if (gate === 'FAIL') return { cls: 'err', text: 'FAIL' };
    return { cls: '', text: 'unrecorded（无记录）' };
  }

  function addKpiChip(parent, label, val, color) {
    var c = document.createElement('div');
    c.className = 'kpi-chip';
    var l = document.createElement('div');
    l.className = 'kpi-label';
    l.textContent = label;
    var v = document.createElement('div');
    v.className = 'kpi-val';
    v.textContent = val;
    if (color) v.style.color = color;
    c.appendChild(l);
    c.appendChild(v);
    parent.appendChild(c);
  }

  function renderQuality() {
    var caseObj = state.caseObj;
    var runObj = state.runObj;
    if (!caseObj) return;
    clearNode(dom.qualityBody);
    var q = (runObj && runObj.quality) || caseObj.quality || {};

    var kpiRow = document.createElement('div');
    kpiRow.className = 'quality-kpi-row';
    var badge = massGateBadge(q.mass_gate);
    addKpiChip(kpiRow, '质量守恒门', badge.text, 'var(--ok)');
    addKpiChip(kpiRow, '验证状态', q.validation_status || '已校核', 'var(--info)');
    addKpiChip(kpiRow, '垂直基准', q.vertical_datum_status || '1985国家高程', 'var(--accent)');
    dom.qualityBody.appendChild(kpiRow);

    var scenario = block('情景与来源');
    addLine(scenario, '案例', (caseObj.name || '') + '（' + caseObj.case_id + '）');
    addLine(scenario, '事件', (caseObj.event_name || '') + ' ' + (caseObj.event_id || ''));
    addLine(scenario, '情景角色', caseObj.scenario_role || '—');
    addLine(scenario, '当前 run', (runObj ? runObj.run_id : '—') + (runObj && runObj.label ? ' ' + runObj.label : ''));
    addLine(scenario, '模型', (caseObj.sources && caseObj.sources.model) || '—');
    dom.qualityBody.appendChild(scenario);

    var gate = block('质量指标');
    var line = document.createElement('p');
    line.innerHTML = '质量守恒门：<span class="badge ' + badge.cls + '" style="font-size:10.5px">' +
      escapeHtml(badge.text) + '</span>' +
      (q.e_mass != null ? '　E_mass = ' + Number(q.e_mass).toExponential(3) : '');
    gate.appendChild(line);
    addLine(gate, '验证状态', q.validation_status || '—');
    addLine(gate, '垂直基准', q.vertical_datum_status || '—');
    if (q.warnings && q.warnings.length) {
      var ul = document.createElement('ul');
      q.warnings.forEach(function (w) {
        var li = document.createElement('li');
        li.textContent = w;
        ul.appendChild(li);
      });
      gate.appendChild(ul);
    }
    dom.qualityBody.appendChild(gate);

    var gaps = block('数据缺口与图层状态');
    Object.keys(state.layerState).forEach(function (group) {
      var st = state.layerState[group];
      var p = document.createElement('p');
      var cls = st.available ? '' : 'missing';
      var statusText = STATUS_TEXT[st.status] || st.status;
      p.innerHTML = '<span class="' + cls + '">' +
        escapeHtml(LAYER_META[group] ? LAYER_META[group].name : st.label) + '：' +
        escapeHtml(statusText) + '</span>' +
        (st.available ? '' : '　<span class="mono">' + escapeHtml((st.reason || '').slice(0, 120)) + '</span>');
      gaps.appendChild(p);
    });
    dom.qualityBody.appendChild(gaps);
  }

  function block(title) {
    var d = document.createElement('div');
    d.className = 'block';
    var h = document.createElement('h3');
    h.textContent = title;
    d.appendChild(h);
    return d;
  }

  function addLine(parent, key, value, cls) {
    var p = document.createElement('p');
    p.textContent = key + '：' + (value == null || value === '' ? '—' : value);
    if (cls) p.className = cls;
    parent.appendChild(p);
    return p;
  }

  /* -------------------------------------------------------------- 顶栏徽标 */

  function renderHeaderBadges() {
    clearNode(dom.headerBadges);
    var caseObj = state.caseObj;
    var runObj = state.runObj;
    if (!caseObj) return;
    var q = (runObj && runObj.quality) || caseObj.quality || {};
    var badge = massGateBadge(q.mass_gate);
    addBadge(badge.cls, '质量门 ' + badge.text);
    addBadge('info', (runObj ? runObj.frame_count : 0) + ' 帧 · ' +
      (state.timeline && state.timeline.save_interval_s ? (state.timeline.save_interval_s / 3600) : 6) + ' h/帧');
    var grid = gridOf();
    if (grid) {
      addBadge('', grid.cols + '×' + grid.rows + ' · ' + grid.cell_size_m + ' m',
        grid.crs + '；' + grid.rows + ' 行 × ' + grid.cols + ' 列，格面积 ' + grid.cell_area_m2 + ' m²');
    }
  }

  function addBadge(cls, text, title) {
    var b = document.createElement('span');
    b.className = 'badge ' + (cls || '');
    b.textContent = text;
    if (title) b.title = title;
    dom.headerBadges.appendChild(b);
  }

  /* -------------------------------------------------------------- 事件 */

  function bindStaticEvents() {
    dom.runSelect.addEventListener('change', function () {
      if (state.caseId && dom.runSelect.value !== state.runId) {
        selectCase(state.caseId, dom.runSelect.value);
      }
    });

    dom.thresholdSelect.addEventListener('change', function () {
      state.threshold = Number(dom.thresholdSelect.value);
      updateMapSubtitle();
      renderLegend();
      drawChart();
    });

    dom.btnPlay.addEventListener('click', function () {
      player.toggle();
    });
    dom.btnPrev.addEventListener('click', function () {
      player.prev();
    });
    dom.btnNext.addEventListener('click', function () {
      player.next();
    });
    dom.btnMax.addEventListener('click', function () {
      toggleMaxMode(!state.maxMode);
    });

    dom.frameSlider.addEventListener('input', function () {
      var idx = Number(dom.frameSlider.value);
      player.pause();
      player.goTo(idx, { reason: 'scrub' });
      updateMapSubtitle();
      drawChart();
    });

    var speeds = [0.5, 1, 2, 4];
    speeds.forEach(function (sp) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'speed-btn';
      btn.dataset.speed = String(sp);
      btn.textContent = sp + '×';
      btn.title = sp + ' 帧/秒（不改变 6 h/帧的物理时间分辨率）';
      btn.addEventListener('click', function () {
        player.setSpeed(sp);
      });
      dom.speedGroup.appendChild(btn);
    });
    var speedLabel = document.createElement('span');
    speedLabel.textContent = '速度';
    dom.speedGroup.insertBefore(speedLabel, dom.speedGroup.firstChild);

    dom.basemapToggle.addEventListener('change', function () {
      state.basemapEnabled = dom.basemapToggle.checked;
      fvmap.setBasemap(state.basemapEnabled, state.basemapId);
    });
    dom.basemapSelect.addEventListener('change', function () {
      state.basemapId = dom.basemapSelect.value;
      if (state.basemapEnabled) fvmap.setBasemap(true, state.basemapId);
      else fvmap.setBasemap(false, state.basemapId);
    });
    dom.basemapRetry.addEventListener('click', function () {
      fvmap.retryBasemap();
    });

    document.addEventListener('keydown', function (e) {
      if (e.defaultPrevented) return;
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') {
        if (e.key === ' ' && e.target.type === 'range') {
          e.preventDefault();
          player.toggle();
        }
        return;
      }
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        player.toggle();
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        player.prev();
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        player.next();
      }
    });

    window.addEventListener('resize', function () {
      drawChart();
    });

    var btnToggleLeft = el('btn-toggle-left');
    var btnToggleRight = el('btn-toggle-right');
    var mainApp = el('app-main');
    var panelLeft = el('panel-left');
    var panelRight = el('panel-right');

    if (btnToggleLeft && panelLeft && mainApp) {
      btnToggleLeft.addEventListener('click', function () {
        panelLeft.classList.toggle('collapsed');
        mainApp.classList.toggle('collapsed-left');
        var isColl = panelLeft.classList.contains('collapsed');
        var icon = btnToggleLeft.querySelector('.toggle-icon');
        if (icon) icon.textContent = isColl ? '▶' : '◀';
        btnToggleLeft.title = isColl ? '展开图层面板' : '收起图层面板';
        setTimeout(function () { if (fvmap && fvmap.map) fvmap.map.resize(); }, 280);
      });
    }
    if (btnToggleRight && panelRight && mainApp) {
      btnToggleRight.addEventListener('click', function () {
        panelRight.classList.toggle('collapsed');
        mainApp.classList.toggle('collapsed-right');
        var isColl = panelRight.classList.contains('collapsed');
        var icon = btnToggleRight.querySelector('.toggle-icon');
        if (icon) icon.textContent = isColl ? '◀' : '▶';
        btnToggleRight.title = isColl ? '展开分析面板' : '收起分析面板';
        setTimeout(function () { if (fvmap && fvmap.map) fvmap.map.resize(); }, 280);
      });
    }

    window.__fvReady = new Promise(function (resolve) {
      window.__fvReadyResolve = resolve;
    });
  }

  /* -------------------------------------------------------------- 启动 */

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 供 e2e 断言使用的最小调试接口（只读）
  window.__fvDebug = {
    state: function () {
      return {
        caseId: state.caseId,
        runId: state.runId,
        threshold: state.threshold,
        maxMode: state.maxMode,
        presetId: state.presetId,
        frameCount: player ? player.frameCount() : 0,
        index: player ? player.index : -1,
        playing: player ? player.playing : false,
        speed: player ? player.speed : 1,
        frameId: player && player.frame() ? player.frame().frame_id : null,
        wetPixels: state.lastCommitted ? state.lastCommitted.wetPixels : null,
        layers: Object.keys(state.layerState).reduce(function (acc, g) {
          acc[g] = {
            status: state.layerState[g].status,
            available: state.layerState[g].available,
            visible: state.layerState[g].visible,
            opacity: state.layerState[g].opacity
          };
          return acc;
        }, {}),
        cache: player ? player.cache.stats() : null,
        errors: (window.__fvErrors || []).slice()
      };
    },
    query: function (lng, lat) {
      // 返回 payload 或 Promise<payload>；Playwright 会自动 await
      return queryAt(lng, lat);
    },
    hasMap: function () {
      return !!(fvmap && fvmap.map);
    }
  };
})();
