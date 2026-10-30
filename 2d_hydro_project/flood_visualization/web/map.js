/* 广西久隆·宁明·贵港 二维洪涝可视化 —— 地图与图层管理（MapLibre GL JS 5.24，本地 vendor）
 *
 * 图层顺序（catalog.layer_order 缺省时使用）：
 *   basemap → dem → optical → sar → flood（逐帧 parts）→ max（包络 parts）→ boundary → sites
 *
 * 关键实现：
 *   - 模型水深/包络/ DEM / SAR / 光学全部用 `type:'image'` + 四角坐标贴图（无 XYZ 瓦片）。
 *   - 一帧 = 一组 parts；用 A/B 两套 image source 做双缓冲，全部 part 就绪后一次性翻转可见性，
 *     不会出现半旧半新。
 *   - 切换 run 时 parts 数量/尺寸可能变化 → 整体 removeLayer/removeSource 后重建。
 *   - 在线底图仅使用 catalog.basemaps 给出的 Esri 地址；失败保留本地图层并回调，不自动换图。
 */
(function (global) {
  'use strict';

  var LAYER_ORDER = ['basemap', 'terrain_source', 'terrain_edit', 'dem', 'optical', 'sar', 'flood', 'max', 'boundary', 'sites'];
  var IMAGE_GROUPS = ['terrain_source', 'terrain_edit', 'dem', 'optical', 'sar'];
  // 原生数值/类别层保持像元边界，禁止插值造出假数值；只有光学背景用 linear。
  var NEAREST_GROUPS = ['terrain_source', 'terrain_edit', 'dem', 'sar', 'flood', 'max'];
  var VECTOR_GROUPS = ['boundary', 'sites'];

  var TRANSPARENT_PX =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

  function FVMap(opts) {
    this.opts = opts || {};
    this.map = null;
    this.catalog = null;
    this.caseObj = null;
    this.runObj = null;
    this.images = null;
    this.cache = this.opts.cache || null;
    this.onNotice = this.opts.onNotice || function () {};
    this.onError = this.opts.onError || function () {};
    this.onClick = this.opts.onClick || function () {};
    this.onBasemapState = this.opts.onBasemapState || function () {};

    this.groups = {}; // group -> {layers:Set, sources:Set, visible, opacity}
    LAYER_ORDER.forEach(function (g) {
      this.groups[g] = { layers: [], sources: [], visible: false, opacity: 1 };
    }, this);

    this.activeBuffer = 'a';
    this.buffers = { a: { parts: [], layers: [], sources: [] }, b: { parts: [], layers: [], sources: [] } };
    this.maxBuffer = { parts: [], layers: [], sources: [] };
    this.commitToken = 0;
    this.maxCommitToken = 0;
    this.basemap = { enabled: false, id: null, failing: false, errorCount: 0, firstErrorAt: 0 };
    this._sourceErrors = new Map();
    this._ready = false;
    this._mapLoaded = null;
  }

  FVMap.prototype.init = function (container) {
    var self = this;
    this.map = new maplibregl.Map({
      container: container,
      style: {
        version: 8,
        sources: {},
        layers: [
          { id: 'fv-background', type: 'background', paint: { 'background-color': '#f8fafc' } }
        ]
      },
      center: [108.8, 22.1],
      zoom: 8,
      attributionControl: false,
      preserveDrawingBuffer: false,
      fadeDuration: 0,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      maxPitch: 0
    });
    this.map.dragRotate.disable();
    this.map.touchZoomRotate.disableRotation();
    this.map.keyboard.disable(); // 键盘由 app.js 统一处理（空格/左右键）

    this.map.on('error', function (e) {
      self._handleMapError(e);
    });

    this._mapLoaded = new Promise(function (resolve) {
      self.map.on('load', function () {
        self._ready = true;
        resolve();
      });
    });

    this.map.on('click', function (e) {
      var feats = [];
      try {
        if (self.map.getLayer('fv-sites-points')) {
          feats = self.map.queryRenderedFeatures(e.point, { layers: ['fv-sites-points'] });
        }
      } catch (err) {
        feats = [];
      }
      if (feats && feats.length) {
        var f = feats[0];
        var props = f.properties || {};
        var html = '<strong>' + escapeHtml(props.name || '站点') + '</strong>' +
          (props.label ? '<br><span style="color:#64748b">' + escapeHtml(props.label) + '</span>' : '');
        new maplibregl.Popup({ closeButton: true, maxWidth: '280px' })
          .setLngLat(f.geometry.coordinates)
          .setHTML(html)
          .addTo(self.map);
        return;
      }
      self.onClick(e.lngLat.lng, e.lngLat.lat);
    });

    this.map.on('mousemove', function (e) {
      var overSite = false;
      try {
        overSite = self.map.getLayer('fv-sites-points') &&
          self.map.queryRenderedFeatures(e.point, { layers: ['fv-sites-points'] }).length > 0;
      } catch (err) {
        overSite = false;
      }
      self.map.getCanvas().style.cursor = overSite ? 'pointer' : 'crosshair';
    });

    this.map.on('moveend', function () {
      self._reportScale();
    });

    return this._mapLoaded;
  };

  FVMap.prototype._handleMapError = function (e) {
    var err = e && e.error ? e.error : e;
    var msg = (err && err.message) || String(err);
    var sourceId = e && e.sourceId ? e.sourceId : null;
    if (sourceId === 'fv-basemap' || /arcgisonline|basemap/i.test(msg)) {
      this._noteBasemapError(msg);
      return;
    }
    if (sourceId) this._sourceErrors.set(sourceId, msg);
    // 本地图层加载失败：交给 app 决定（暂停播放 + 提示）
    if (this.opts.onLayerError) this.opts.onLayerError(sourceId, msg);
  };

  FVMap.prototype._reportScale = function () {
    if (!this.map) return;
    var zoom = this.map.getZoom();
    var lat = this.map.getCenter().lat;
    // Web Mercator：分辨率 = 156543.03 * cos(lat) / 2^zoom （m/px @256px tile）
    var mPerPx = (156543.03392 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, zoom);
    if (this.opts.onScale) this.opts.onScale(mPerPx, zoom);
  };

  /* ------------------------------------------------------------ 图层工具 */

  FVMap.prototype._group = function (name) {
    return this.groups[name];
  };

  FVMap.prototype._beforeId = function (groupName) {
    var start = LAYER_ORDER.indexOf(groupName);
    if (start < 0) return undefined;
    for (var i = start + 1; i < LAYER_ORDER.length; i += 1) {
      var ids = this.groups[LAYER_ORDER[i]].layers;
      if (ids.length) return ids[0];
    }
    return undefined;
  };

  FVMap.prototype._removeGroup = function (name) {
    var g = this.groups[name];
    var self = this;
    g.layers.slice().forEach(function (id) {
      try {
        if (self.map.getLayer(id)) self.map.removeLayer(id);
      } catch (err) {
        /* 忽略已移除 */
      }
    });
    g.sources.slice().forEach(function (id) {
      try {
        if (self.map.getSource(id)) self.map.removeSource(id);
      } catch (err) {
        /* 忽略已移除 */
      }
    });
    g.layers = [];
    g.sources = [];
  };

  FVMap.prototype._addRasterLayer = function (groupId, layerId, sourceId, opts) {
    var g = this.groups[groupId];
    this.map.addLayer(
      {
        id: layerId,
        type: 'raster',
        source: sourceId,
        paint: {
          'raster-opacity': opts.opacity,
          'raster-fade-duration': 0,
          'raster-resampling': opts.resampling || 'linear'
        },
        layout: { visibility: g.visible ? 'visible' : 'none' }
      },
      this._beforeId(groupId)
    );
    g.layers.push(layerId);
  };

  FVMap.prototype._addImageSource = function (sourceId, url, coordinates) {
    if (this.map.getSource(sourceId)) this.map.removeSource(sourceId);
    this.map.addSource(sourceId, {
      type: 'image',
      url: url || TRANSPARENT_PX,
      coordinates: coordinates
    });
  };

  /* ------------------------------------------------------------ 案例/run */

  /**
   * 切换案例与 run：清空全部本地图层，按新 run 的 grid / parts 重建。
   * @param {object} payload {caseObj, runObj, images, imagesUrl}
   */
  FVMap.prototype.setCaseRun = function (payload) {
    var self = this;
    var caseObj = payload.caseObj;
    var images = payload.images || null;
    this.caseObj = caseObj;
    this.runObj = payload.runObj || null;
    this.images = images;
    this.commitToken += 1;
    if (!this._ready) return Promise.resolve();

    LAYER_ORDER.forEach(function (g) {
      if (g !== 'basemap') self._removeGroup(g);
    });
    this.buffers = { a: { parts: [], layers: [], sources: [] }, b: { parts: [], layers: [], sources: [] } };
    this.maxBuffer = { parts: [], layers: [], sources: [] };
    this.activeBuffer = 'a';
    this._sourceErrors.clear();

    var fitPromise = this.fitCase(caseObj);
    var layers = (caseObj && caseObj.layers) || {};

    // ---- DEM / SAR / 光学：单图或 parts 索引 ----
    var staticJobs = [];
    IMAGE_GROUPS.forEach(function (name) {
      var layer = layers[name];
      if (!layer) return;
      var url = layer.images || layer.image; // images = parts 索引 JSON（可能多子图）
      if (!url || url === 'undefined') return;
      var geom = layer.geometry || (caseObj.grid ? caseObj.grid : null);
      var coords = (geom && geom.coordinates) || (layer.parts && layer.parts[0] && layer.parts[0].coordinates) ||
        null;
      var isIndex = /\.json(\?|$)/i.test(url);
      if (!coords && !isIndex) return;
      staticJobs.push(self._prepareStaticImage(name, url, coords, layer));
    });

    // ---- 边界 / 站点：GeoJSON ----
    VECTOR_GROUPS.forEach(function (name) {
      var layer = layers[name];
      if (!layer || !layer.geojson) return;
      staticJobs.push(self._prepareVector(name, layer.geojson));
    });

    // ---- 水深帧 parts（双缓冲）与包络 parts ----
    if (images && images.frames && images.frames.length) {
      var frame0 = images.frames[0];
      self._buildFrameBuffers(frame0.parts);
      var maxParts = (images.max && images.max.parts) || (layers.max && layers.max.parts) || null;
      if (maxParts && maxParts.length) self._buildMaxBuffer(maxParts);
    } else if (layers.max && layers.max.parts && layers.max.parts.length) {
      // 帧索引缺失时至少把包络建好（避免整页空白）
      self._buildMaxBuffer(layers.max.parts);
    }

    return Promise.all(staticJobs).then(function () {
      return fitPromise;
    });
  };

  FVMap.prototype.fitCase = function (caseObj) {
    if (!caseObj || !caseObj.model_bbox) return Promise.resolve();
    var bbox = caseObj.model_bbox;
    return new Promise(function (resolve) {
      this.map.fitBounds(
        [
          [bbox[0], bbox[1]],
          [bbox[2], bbox[3]]
        ],
        { padding: 40, duration: 0, maxZoom: 15 }
      );
      this.map.once('idle', function () {
        resolve();
      });
      setTimeout(resolve, 900);
    }.bind(this));
  };

  FVMap.prototype._prepareStaticImage = function (groupName, url, coordinates, layerDef) {
    var self = this;
    // 契约允许两种形式：
    //   a) 单张 PNG + geometry.coordinates（如 SAR）
    //   b) parts 索引 JSON（如 DEM：frames/<case>/dem_images.json，含 parts[] 与 color_scale）
    if (/\.json(\?|$)/i.test(url)) return this._prepareStaticParts(groupName, url, layerDef);
    // 静态层整场只加载一次；不进逐帧 LRU，避免挤占逐帧缓存预算
    var preload = this.cache ? this.cache.loadOnce(url) : Promise.resolve(null);
    return preload.then(
      function () {
        var sourceId = 'fv-' + groupName;
        var opacity = self.groups[groupName].opacity;
        self._addImageSource(sourceId, url, coordinates);
        var g = self.groups[groupName];
        g.sources.push(sourceId);
        if (layerDef && typeof layerDef.opacity === 'number') g.opacity = layerDef.opacity;
        self._addRasterLayer(groupName, 'fv-' + groupName + '-raster', sourceId, {
          opacity: g.opacity,
          // 原生数值层保持像元边界；光学背景使用线性重采样。
          resampling: NEAREST_GROUPS.indexOf(groupName) >= 0 ? 'nearest' : 'linear'
        });
        return null;
      },
      function (err) {
        // 静态图层不可用：不亮开关，记录原因，由 app 显示
        self._sourceErrors.set('fv-' + groupName, (err && err.message) || '加载失败');
        if (self.opts.onLayerError) self.opts.onLayerError('fv-' + groupName, (err && err.message) || '图层加载失败');
        return null;
      }
    );
  };

  /** parts 索引形式的静态图层（DEM/光学/SAR 可多子图）。 */
  FVMap.prototype._prepareStaticParts = function (groupName, indexUrl, layerDef) {
    var self = this;
    return fetch(indexUrl, { cache: 'no-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + '：' + indexUrl);
        return r.json();
      })
      .then(function (index) {
        var parts = index.parts || null;
        if (!parts && index.frames && index.frames.length === 1 && index.frames[0].parts) {
          parts = index.frames[0].parts;
        }
        if (!parts || !parts.length) throw new Error('parts 索引缺少 parts：' + indexUrl);
        self.groups[groupName].colorScale = index.color_scale || null;
        self.groups[groupName].grid = index.grid || null;
        var urls = parts.map(function (p) {
          return p.path;
        });
        // 静态层整场只加载一次；不进逐帧 LRU，避免挤占逐帧缓存预算
        return Promise.all(urls.map(function (u) { return self.cache.loadOnce(u); })).then(function () {
          var g = self.groups[groupName];
          parts.forEach(function (part, i) {
            var sourceId = 'fv-' + groupName + '-' + i;
            self._addImageSource(sourceId, part.path, part.coordinates);
            g.sources.push(sourceId);
            self._addRasterLayer(groupName, 'fv-' + groupName + '-' + i + '-raster', sourceId, {
              opacity: g.opacity,
              // 原生数值层保持像元边界；光学背景使用线性重采样。
              resampling: NEAREST_GROUPS.indexOf(groupName) >= 0 ? 'nearest' : 'linear'
            });
          });
          return null;
        });
      })
      .catch(function (err) {
        self._sourceErrors.set('fv-' + groupName, (err && err.message) || '图层索引加载失败');
        if (self.opts.onLayerError) self.opts.onLayerError('fv-' + groupName, (err && err.message) || '图层加载失败');
        return null;
      });
  };

  FVMap.prototype._prepareVector = function (groupName, url) {
    var self = this;
    return fetch(url, { cache: 'no-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url);
        return r.json();
      })
      .then(function (geojson) {
        var sourceId = 'fv-' + groupName;
        if (self.map.getSource(sourceId)) self.map.removeSource(sourceId);
        self.map.addSource(sourceId, { type: 'geojson', data: geojson });
        var g = self.groups[groupName];
        g.sources.push(sourceId);
        var beforeId = self._beforeId(groupName);
        if (groupName === 'boundary') {
          self.map.addLayer(
            {
              id: 'fv-boundary-line',
              type: 'line',
              source: sourceId,
              paint: {
                'line-color': '#d97706',
                'line-width': 1.6,
                'line-dasharray': [4, 3],
                'line-opacity': g.visible ? 1 : 0
              },
              layout: { visibility: g.visible ? 'visible' : 'none' }
            },
            beforeId
          );
          g.layers.push('fv-boundary-line');
        } else if (groupName === 'sites') {
          self.map.addLayer(
            {
              id: 'fv-sites-points',
              type: 'circle',
              source: sourceId,
              paint: {
                'circle-radius': 5,
                'circle-color': '#0284c7',
                'circle-stroke-color': '#ffffff',
                'circle-stroke-width': 1.6,
                'circle-opacity': g.visible ? 1 : 0
              },
              layout: { visibility: g.visible ? 'visible' : 'none' }
            },
            beforeId
          );
          g.layers.push('fv-sites-points');
          self.map.addLayer(
            {
              id: 'fv-sites-labels',
              type: 'symbol',
              source: sourceId,
              layout: {
                'text-field': ['get', 'name'],
                'text-size': 11,
                'text-offset': [0, 1.1],
                'text-anchor': 'top',
                'text-allow-overlap': false,
                visibility: g.visible ? 'visible' : 'none'
              },
              paint: {
                'text-color': '#0f172a',
                'text-halo-color': '#ffffff',
                'text-halo-width': 1.8,
                'text-opacity': g.visible ? 1 : 0
              }
            },
            beforeId
          );
          g.layers.push('fv-sites-labels');
        }
        return null;
      })
      .catch(function (err) {
        self._sourceErrors.set('fv-' + groupName, (err && err.message) || 'GeoJSON 加载失败');
        if (self.opts.onLayerError) self.opts.onLayerError('fv-' + groupName, (err && err.message) || 'GeoJSON 加载失败');
        return null;
      });
  };

  /* ---------------------------------------------------- 帧 parts 双缓冲 */

  FVMap.prototype._buildFrameBuffers = function (parts) {
    if (!parts || !parts.length) return;
    var self = this;
    ['a', 'b'].forEach(function (bufName) {
      var buf = self.buffers[bufName];
      buf.parts = parts;
      buf.sources = [];
      buf.layers = [];
      parts.forEach(function (part, i) {
        var sourceId = 'fv-flood-' + bufName + '-' + i;
        self._addImageSource(sourceId, TRANSPARENT_PX, part.coordinates);
        var layerId = 'fv-flood-' + bufName + '-' + i + '-raster';
        self.map.addLayer(
          {
            id: layerId,
            type: 'raster',
            source: sourceId,
            paint: {
              'raster-opacity': self.groups.flood.opacity,
              'raster-fade-duration': 0,
              'raster-resampling': 'nearest'
            },
            layout: { visibility: 'none' }
          },
          self._beforeId('flood')
        );
        buf.sources.push(sourceId);
        buf.layers.push(layerId);
        self.groups.flood.layers.push(layerId);
        self.groups.flood.sources.push(sourceId);
      });
    });
    // 默认显示 A 缓冲（由 commitFrame 填充），此处保持隐藏
    this._flipFrameBuffer('a');
  };

  FVMap.prototype._buildMaxBuffer = function (parts) {
    var self = this;
    var buf = this.maxBuffer;
    buf.parts = parts;
    parts.forEach(function (part, i) {
      var sourceId = 'fv-max-' + i;
      self._addImageSource(sourceId, TRANSPARENT_PX, part.coordinates);
      var layerId = 'fv-max-' + i + '-raster';
      self.map.addLayer(
        {
          id: layerId,
          type: 'raster',
          source: sourceId,
          paint: {
            'raster-opacity': self.groups.max.opacity,
            'raster-fade-duration': 0,
            'raster-resampling': 'nearest'
          },
          layout: { visibility: 'none' }
        },
        self._beforeId('max')
      );
      buf.sources.push(sourceId);
      buf.layers.push(layerId);
      self.groups.max.layers.push(layerId);
      self.groups.max.sources.push(sourceId);
    });
  };

  FVMap.prototype._flipFrameBuffer = function (name) {
    var self = this;
    var other = name === 'a' ? 'b' : 'a';
    var showMax = this.groups.max.visible && this.maxModeOn;
    this.buffers.other = this.buffers[other];
    ['a', 'b'].forEach(function (buf) {
      self.buffers[buf].layers.forEach(function (id) {
        if (!self.map.getLayer(id)) return;
        var visible = buf === name && !showMax && self.groups.flood.visible;
        self.map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
      });
    });
    this.activeBuffer = name;
  };

  FVMap.prototype._flipMaxLayers = function (show) {
    var self = this;
    this.maxBuffer.layers.forEach(function (id) {
      if (!self.map.getLayer(id)) return;
      self.map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none');
    });
    if (show) {
      ['a', 'b'].forEach(function (buf) {
        self.buffers[buf].layers.forEach(function (id) {
          if (self.map.getLayer(id)) self.map.setLayoutProperty(id, 'visibility', 'none');
        });
      });
    } else if (this._hasFrame) {
      this._flipFrameBuffer(this.activeBuffer);
    }
  };

  FVMap.prototype._waitIdle = function (timeoutMs) {
    var self = this;
    return new Promise(function (resolve) {
      var done = false;
      var finish = function () {
        if (done) return;
        done = true;
        self.map.off('idle', onIdle);
        resolve();
      };
      var onIdle = function () {
        finish();
      };
      self.map.on('idle', onIdle);
      setTimeout(finish, timeoutMs || 4000);
    });
  };

  /**
   * 等指定 image source 的图片真正加载完成（MapLibre 在 ImageSource 载入后
   * 触发 data 事件，sourceDataType 为 metadata/content）。这比等全局 idle 更精确：
   * idle 还会被其它图层/请求拖住，导致切帧无谓变慢。
   */
  FVMap.prototype._waitSources = function (sourceIds, timeoutMs) {
    var self = this;
    if (!sourceIds.length) return Promise.resolve();
    return new Promise(function (resolve) {
      var pending = new Set(sourceIds);
      var done = false;
      var finish = function () {
        if (done) return;
        done = true;
        self.map.off('data', onData);
        // 再等一帧渲染，确保纹理已上传
        if (global.requestAnimationFrame) global.requestAnimationFrame(function () { resolve(); });
        else setTimeout(resolve, 16);
      };
      var onData = function (e) {
        if (e && e.sourceId && pending.has(e.sourceId)) {
          var dt = e.sourceDataType;
          if (dt === 'content' || dt === 'metadata' || dt === 'idle') {
            pending.delete(e.sourceId);
            if (!pending.size) finish();
          }
        }
      };
      self.map.on('data', onData);
      setTimeout(finish, timeoutMs || 2500);
    });
  };

  /**
   * 提交一帧：先在缓存预加载全部 part（任一失败即抛错），再写入非活动缓冲，
   * 等地图空闲后整体翻转可见性。
   * @returns {Promise<void>} 失败时 reject（含 part 路径）
   */
  FVMap.prototype.commitFrame = function (frameEntry, token) {
    var self = this;
    if (!frameEntry || !frameEntry.parts || !frameEntry.parts.length) {
      return Promise.reject(new Error('帧缺少图片 parts（images.json）'));
    }
    if (!this._ready) return Promise.resolve();
    this.commitToken = token;
    var inactive = this.activeBuffer === 'a' ? 'b' : 'a';
    var buf = this.buffers[inactive];
    if (buf.parts.length !== frameEntry.parts.length) {
      return Promise.reject(new Error('parts 数量与已建图层不一致，需要重建 source'));
    }
    var urls = frameEntry.parts.map(function (p) {
      return p.path;
    });
    return this.cache.preloadAll(urls).then(
      function () {
        if (self.commitToken !== token) return null; // 过期提交，丢弃
        for (var i = 0; i < frameEntry.parts.length; i += 1) {
          var part = frameEntry.parts[i];
          var src = self.map.getSource(buf.sources[i]);
          if (!src) throw new Error('图层 source 丢失：' + buf.sources[i]);
          src.updateImage({ url: part.path, coordinates: part.coordinates });
        }
        return self._waitSources(buf.sources, 2500).then(function () {
          if (self.commitToken !== token) return null;
          self._hasFrame = true;
          self._flipFrameBuffer(inactive);
          return null;
        });
      },
      function (err) {
        throw new Error('帧图片加载失败：' + ((err && err.message) || err));
      }
    );
  };

  /** 提交最大包络（一次性，之后由 showMax 控制可见性）。 */
  FVMap.prototype.commitMax = function () {
    var self = this;
    var parts = null;
    if (this.images && this.images.max && this.images.max.parts) parts = this.images.max.parts;
    else if (this.caseObj && this.caseObj.layers && this.caseObj.layers.max && this.caseObj.layers.max.parts) {
      parts = this.caseObj.layers.max.parts;
    }
    if (!parts || !parts.length) {
      // 兼容 split=1×1 时的 layers.max.image + geometry 形式
      var def = this.caseObj && this.caseObj.layers ? this.caseObj.layers.max : null;
      if (def && def.image && def.image !== 'undefined' && !/\.json$/i.test(def.image) && def.geometry && def.geometry.coordinates) {
        return this._commitMaxSingle(def.image, def.geometry.coordinates);
      }
      return Promise.reject(new Error('缺少包络 parts'));
    }
    if (this.maxBuffer.sources.length !== parts.length) {
      return Promise.reject(new Error('包络 parts 数量与已建图层不一致（需要重建 source）'));
    }
    var maxToken = ++this.maxCommitToken;
    var urls = parts.map(function (p) {
      return p.path;
    });
    return this.cache.preloadAll(urls).then(
      function () {
        if (self.maxCommitToken !== maxToken) return null;
        for (var i = 0; i < parts.length; i += 1) {
          var src = self.map.getSource(self.maxBuffer.sources[i]);
          if (!src) throw new Error('包络 source 丢失');
          src.updateImage({ url: parts[i].path, coordinates: parts[i].coordinates });
        }
        return self._waitSources(self.maxBuffer.sources, 2500).then(function () {
          self._maxReady = true;
          return null;
        });
      },
      function (err) {
        throw new Error('包络图片加载失败：' + ((err && err.message) || err));
      }
    );
  };

  /** 单图包络（旧/简化形式）：直接更新一个 image source。 */
  FVMap.prototype._commitMaxSingle = function (url, coordinates) {
    var self = this;
    return this.cache.preload(url).then(
      function () {
        var src = self.map.getSource('fv-max-single');
        if (!src) {
          self.map.addSource('fv-max-single', { type: 'image', url: url, coordinates: coordinates });
          self.map.addLayer(
            {
              id: 'fv-max-single-raster',
              type: 'raster',
              source: 'fv-max-single',
              paint: { 'raster-opacity': self.groups.max.opacity, 'raster-fade-duration': 0 },
              layout: { visibility: 'none' }
            },
            self._beforeId('max')
          );
          self.maxBuffer.layers.push('fv-max-single-raster');
        } else {
          src.updateImage({ url: url, coordinates: coordinates });
        }
        return self._waitSources(['fv-max-single'], 2500).then(function () {
          self._maxReady = true;
          return null;
        });
      },
      function (err) {
        throw new Error('包络图片加载失败：' + ((err && err.message) || err));
      }
    );
  };

  FVMap.prototype.setMaxMode = function (on) {
    this.maxModeOn = !!on;
    this._flipMaxLayers(!!on);
    if (!on && this._hasFrame) this._flipFrameBuffer(this.activeBuffer);
  };

  /* ------------------------------------------------------------ 可见性/透明度 */

  FVMap.prototype.setGroupVisible = function (groupName, visible) {
    var g = this.groups[groupName];
    if (!g) return;
    g.visible = !!visible;
    if (groupName === 'flood') {
      this._flipFrameBuffer(this.activeBuffer);
      return;
    }
    if (groupName === 'max') {
      this._flipMaxLayers(!!visible);
      return;
    }
    g.layers.forEach(function (id) {
      if (!this.map.getLayer(id)) return;
      this.map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
    }, this);
  };

  FVMap.prototype.setGroupOpacity = function (groupName, opacity) {
    var g = this.groups[groupName];
    if (!g) return;
    g.opacity = opacity;
    var self = this;
    g.layers.forEach(function (id) {
      if (!self.map.getLayer(id)) return;
      try {
        if (groupName === 'boundary') {
          self.map.setPaintProperty(id, 'line-opacity', opacity);
        } else if (groupName === 'sites') {
          if (id.indexOf('labels') >= 0) self.map.setPaintProperty(id, 'text-opacity', opacity);
          else self.map.setPaintProperty(id, 'circle-opacity', opacity);
        } else {
          self.map.setPaintProperty(id, 'raster-opacity', opacity);
        }
      } catch (err) {
        /* 图层类型不支持时忽略 */
      }
    });
  };

  /* ------------------------------------------------------------ 在线底图 */

  FVMap.prototype.setBasemap = function (enabled, basemapId) {
    var self = this;
    this.basemap.enabled = !!enabled;
    this.basemap.id = basemapId || this.basemap.id;
    this._removeBasemapLayers();
    if (!enabled) {
      this.basemap.failing = false;
      this.onBasemapState({ enabled: false, failing: false, attribution: '' });
      return;
    }
    var def = (this.catalog && this.catalog.basemaps ? this.catalog.basemaps : []).filter(function (b) {
      return b.id === basemapId;
    })[0];
    if (!def || !def.tiles || !def.tiles.length) {
      this.basemap.failing = true;
      this.onBasemapState({
        enabled: true,
        failing: true,
        message: '未找到可用的在线底图定义（catalog.basemaps）',
        attribution: ''
      });
      return;
    }
    if (global.navigator && global.navigator.onLine === false) {
      this.basemap.failing = true;
      this.onBasemapState({ enabled: true, failing: true, message: '当前网络离线', attribution: def.attribution || '' });
      return;
    }
    this.basemap.errorCount = 0;
    this.basemap.firstErrorAt = 0;
    this.basemap.failing = false;
    this.map.addSource('fv-basemap', {
      type: 'raster',
      tiles: def.tiles,
      tileSize: def.tile_size || 256,
      attribution: def.attribution || '',
      maxzoom: 19
    });
    this.groups.basemap.sources.push('fv-basemap');
    this.map.addLayer(
      {
        id: 'fv-basemap-raster',
        type: 'raster',
        source: 'fv-basemap',
        paint: { 'raster-opacity': this.groups.basemap.opacity, 'raster-fade-duration': 0 }
      },
      this._beforeId('basemap')
    );
    this.groups.basemap.layers.push('fv-basemap-raster');
    this.groups.basemap.visible = true;
    this.onBasemapState({ enabled: true, failing: false, attribution: def.attribution || '', label: def.label || '' });
    // 主动探测一张瓦片：不依赖地图内部错误事件，断网/403/404 都能确定性触发降级提示
    this._probeBasemap(def, this.caseObj && this.caseObj.model_bbox).catch(function (err) {
      if (!self.basemap.enabled || self.basemap.failing) return;
      self.basemap.failing = true;
      self.onBasemapState({
        enabled: true,
        failing: true,
        message: (err && err.message) || '在线底图瓦片探测失败',
        attribution: def.attribution || ''
      });
    });
  };

  /**
   * 探测在线底图是否真的可用：取当前案例 bbox 中心、zoom 10 的一张瓦片，
   * 用 Image 加载；失败或 8 s 超时即判定不可用。逻辑与地图内部事件解耦，行为确定。
   * 模板占位符按 Esri 的 {z}/{y}/{x} 顺序替换。
   */
  FVMap.prototype._probeBasemap = function (def, bbox) {
    var tiles = def.tiles || [];
    var tpl = tiles[0];
    if (!tpl) return Promise.reject(new Error('底图缺少瓦片模板'));
    var z = Number(def.probe_zoom) || 10;
    var lon;
    var lat;
    if (bbox && bbox.length === 4) {
      lon = (bbox[0] + bbox[2]) / 2;
      lat = (bbox[1] + bbox[3]) / 2;
    } else {
      var c = this.map.getCenter();
      lon = c.lng;
      lat = c.lat;
    }
    var n = Math.pow(2, z);
    var x = Math.floor(((lon + 180) / 360) * n);
    var latRad = (lat * Math.PI) / 180;
    var y = Math.floor(((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n);
    var url = tpl
      .replace('{z}', String(z))
      .replace('{x}', String(x))
      .replace('{y}', String(y));
    return new Promise(function (resolve, reject) {
      var img = new Image();
      var done = false;
      var timer = setTimeout(function () {
        if (done) return;
        done = true;
        reject(new Error('在线底图探测超时（8 s）：' + url));
      }, 8000);
      img.onload = function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(url);
      };
      img.onerror = function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        reject(new Error('在线底图瓦片不可用（网络失败/403/404）：' + url));
      };
      img.src = url;
    });
  };

  FVMap.prototype._removeBasemapLayers = function () {
    var self = this;
    this.groups.basemap.layers.forEach(function (id) {
      try {
        if (self.map.getLayer(id)) self.map.removeLayer(id);
      } catch (err) {
        /* ignore */
      }
    });
    this.groups.basemap.layers = [];
    this.groups.basemap.sources.forEach(function (id) {
      try {
        if (self.map.getSource(id)) self.map.removeSource(id);
      } catch (err) {
        /* ignore */
      }
    });
    this.groups.basemap.sources = [];
    this.groups.basemap.visible = false;
  };

  FVMap.prototype._noteBasemapError = function (message) {
    if (!this.basemap.enabled) return;
    var now = Date.now();
    if (!this.basemap.firstErrorAt || now - this.basemap.firstErrorAt > 12000) {
      this.basemap.firstErrorAt = now;
      this.basemap.errorCount = 0;
    }
    this.basemap.errorCount += 1;
    if (this.basemap.failing) return;
    if (this.basemap.errorCount >= 3) {
      this.basemap.failing = true;
      var def = (this.catalog.basemaps || []).filter(function (b) {
        return b.id === this.basemap.id;
      }, this)[0];
      // 保留图层与本地数据，仅提示；不自动换其它底图
      this.onBasemapState({
        enabled: true,
        failing: true,
        message: message || '在线底图瓦片加载失败',
        attribution: def ? def.attribution || '' : ''
      });
    }
  };

  FVMap.prototype.basemapAttribution = function () {
    if (!this.basemap.id || !this.catalog) return '';
    var def = (this.catalog.basemaps || []).filter(function (b) {
      return b.id === this.basemap.id;
    })[0];
    return def ? def.attribution || '' : '';
  };

  FVMap.prototype.retryBasemap = function () {
    if (!this.basemap.enabled) return;
    var id = this.basemap.id;
    this._removeBasemapLayers();
    this.basemap.failing = false;
    this.basemap.errorCount = 0;
    this.basemap.firstErrorAt = 0;
    this.setBasemap(true, id);
  };

  FVMap.prototype.setCatalog = function (catalog) {
    this.catalog = catalog;
  };

  FVMap.prototype.hasLayerError = function (sourceId) {
    return this._sourceErrors.get(sourceId) || null;
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  global.FVMap = FVMap;
})(window);
