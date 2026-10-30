/* 广西久隆·宁明·贵港 二维洪涝可视化 —— 时间轴、帧调度、图片缓存
 *
 * 职责（无 DOM 依赖，供 app.js 调用）：
 *   1. ImageCache：预加载 part 图片 + LRU（默认上限 256 MB，按解码后字节估算）。
 *   2. FramePlayer：真实帧索引、播放/暂停、逐帧、速度（帧/秒）、双缓冲提交、请求令牌、
 *      预取前后 1 帧；缺帧时停止前进并回报错误，不用旧帧或 0 值冒充。
 *   3. 时间与文本格式化：T+ 小时、绝对时间（timestamp 为 null 时明确不可用）。
 *
 * 约定：一帧 = 一组 parts（images.json 的 frames[i].parts）；切帧时整组一起提交。
 */
(function (global) {
  'use strict';

  var MB = 1024 * 1024;

  /* ------------------------------------------------------------ 时间格式 */

  /** T+ 小时文本，例如 T+12.0 h。sim_seconds 为模型真值。 */
  function formatTPlus(simSeconds) {
    var s = Number(simSeconds);
    if (!isFinite(s)) return 'T+— h';
    var hours = s / 3600;
    var sign = hours < 0 ? '-' : '+';
    return 'T' + sign + Math.abs(hours).toFixed(1) + ' h';
  }

  /** 帧内时间（分钟）文本，用于提示条，例如「T+12.0 h（6 h/帧）」。 */
  function formatFrameTime(simSeconds) {
    return formatTPlus(simSeconds);
  }

  /**
   * 绝对时间文本。timestamp 为 ISO8601 且带偏移（如 +08:00）时按该偏移的墙钟时间显示；
   * timestamp 为 null → 返回 null，表示「绝对时间不可用」，调用方不得伪造日期。
   */
  function formatAbsoluteTime(timestamp) {
    if (!timestamp || typeof timestamp !== 'string') return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?([+-]\d{2}:\d{2}|Z)?$/.exec(timestamp);
    if (!m) {
      var d = new Date(timestamp);
      if (isNaN(d.getTime())) return null;
      return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
        ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }
    var zone = m[6];
    var zoneText = '';
    if (zone === 'Z') zoneText = '（UTC）';
    else if (zone === '+08:00') zoneText = '（北京时间）';
    else if (zone) zoneText = '（UTC' + zone + '）';
    return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + zoneText;
  }

  function pad2(v) {
    return v < 10 ? '0' + v : '' + v;
  }

  /** origin_status → 中文说明（用于时间轴下方注释）。 */
  function describeOrigin(timeInfo) {
    if (!timeInfo) return '';
    var status = timeInfo.origin_status || '';
    var origin = timeInfo.time_origin;
    if (status === 'model_schedule_confirmed') {
      var abs = formatAbsoluteTime(origin);
      return '时间起点：模型排程确认' + (abs ? '（' + abs + '）' : '') + '；仅贵港有确认的绝对时间';
    }
    if (status === 'relative_only_candidate') {
      return '时间起点为候选依据（旧 meta 无规范 t0）；只显示相对 T+ 小时，不显示绝对日期';
    }
    if (status === 'relative_only') {
      return '时间起点未确认（relative_only）；只显示相对 T+ 小时';
    }
    return origin ? '时间起点：' + origin : '时间起点未提供；只显示相对 T+ 小时';
  }

  /* ------------------------------------------------------------ 图片缓存 */

  /**
   * 预加载图片并按 LRU 淘汰；字节数优先取 Resource Timing（encodedBodySize），
   * 拿不到则按解码后 RGBA 估算。
   */
  function ImageCache(maxBytes) {
    this.maxBytes = maxBytes || 256 * MB;
    this.entries = new Map(); // url -> {img, bytes}
    this.totalBytes = 0;
    this.hits = 0;
    this.misses = 0;
    this.evictions = 0;
    this.failures = 0;
  }

  ImageCache.prototype.has = function (url) {
    return this.entries.has(url);
  };

  ImageCache.prototype.touch = function (url) {
    var e = this.entries.get(url);
    if (!e) return null;
    this.entries.delete(url);
    this.entries.set(url, e);
    return e.img;
  };

  ImageCache.prototype._measure = function (img, url) {
    var bytes = 0;
    try {
      var entries = global.performance && performance.getEntriesByName ? performance.getEntriesByName(url) : null;
      if (entries && entries.length) {
        bytes = entries[entries.length - 1].decodedBodySize || entries[entries.length - 1].encodedBodySize || 0;
      }
    } catch (err) {
      bytes = 0;
    }
    if (!bytes) bytes = (img.naturalWidth || img.width || 0) * (img.naturalHeight || img.height || 0) * 4;
    return bytes || 4096;
  };

  ImageCache.prototype.put = function (url, img) {
    var bytes = this._measure(img, url);
    var prev = this.entries.get(url);
    if (prev) {
      this.totalBytes -= prev.bytes;
      this.entries.delete(url);
    }
    this.entries.set(url, { img: img, bytes: bytes });
    this.totalBytes += bytes;
    this._evict();
  };

  ImageCache.prototype._evict = function () {
    // 硬上限：超过 maxBytes 时一直淘汰到回落线（maxBytes × 0.85），
    // 避免"刚好卡在上限附近反复进出"造成缓存抖动与实测超限。
    var target = this.totalBytes > this.maxBytes ? Math.floor(this.maxBytes * 0.85) : this.maxBytes;
    while (this.totalBytes > target && this.entries.size > 1) {
      var oldestKey = this.entries.keys().next().value;
      var e = this.entries.get(oldestKey);
      this.entries.delete(oldestKey);
      this.totalBytes -= e.bytes;
      this.evictions += 1;
    }
    // 单条目超过上限的极端情况：保留当前帧必需图片，但记录标记供自检。
    if (this.totalBytes > this.maxBytes) this.overflow = true;
  };

  /** 预加载单张图片：命中缓存直接返回；失败 reject（调用方必须当成错误，不得静默）。 */
  ImageCache.prototype.preload = function (url) {
    var self = this;
    var hit = this.touch(url);
    if (hit) {
      this.hits += 1;
      return Promise.resolve(hit);
    }
    this.misses += 1;
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.decoding = 'async';
      img.onload = function () {
        self.put(url, img);
        resolve(img);
      };
      img.onerror = function () {
        self.failures += 1;
        reject(new Error('图片加载失败：' + url));
      };
      img.src = url;
    });
  };

  /** 预加载一组 URL；任一失败 reject（不返回半成功结果）。 */
  ImageCache.prototype.preloadAll = function (urls) {
    return Promise.all(urls.map(this.preload.bind(this)));
  };

  /**
   * 加载静态图层图片，但不占用逐帧 LRU 预算。
   *
   * 静态图层（源地形、地形修改掩膜、SAR、光学）整场只加载一次，
   * 由地图 source 自己持有引用；若也计入 LRU，会挤掉逐帧缓存。
   * 这里只用浏览器 HTTP 缓存，不计入 totalBytes。
   */
  ImageCache.prototype.loadOnce = function (url) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.decoding = 'async';
      img.onload = function () {
        resolve(img);
      };
      img.onerror = function () {
        reject(new Error('图片加载失败：' + url));
      };
      img.src = url;
    });
  };

  ImageCache.prototype.stats = function () {
    return {
      maxBytes: this.maxBytes,
      bytes: Math.round(this.totalBytes),
      count: this.entries.size,
      hits: this.hits,
      misses: this.misses,
      evictions: this.evictions,
      failures: this.failures,
      overflow: !!this.overflow
    };
  };

  ImageCache.prototype.clear = function () {
    this.entries.clear();
    this.totalBytes = 0;
  };

  /* ------------------------------------------------------------ 帧调度器 */

  /**
   * FramePlayer 管理「当前真实帧」。切帧流程：
   *   request(index) → token++ → onCommit(index, frame, images)（调用方负责预加载 + 贴图）
   *   → 成功后才更新 index 并回调 onCommitted；失败停下并回调 onCommitError。
   * 播放使用 setTimeout 链（提交完成后再排下一帧），因此慢帧只会降速，不会叠帧。
   */
  function FramePlayer(opts) {
    this.opts = opts || {};
    this.onCommit = this.opts.onCommit; // async (index, frame, images, token) => void
    this.onCommitted = this.opts.onCommitted || function () {};
    this.onCommitError = this.opts.onCommitError || function () {};
    this.onStateChange = this.opts.onStateChange || function () {};
    this.cache = this.opts.cache || new ImageCache();

    this.timeline = null;
    this.images = null;
    this.caseId = null;
    this.runId = null;
    this.index = 0;
    this.token = 0;
    this.playing = false;
    this.speed = 1; // 帧/秒
    this.maxMode = false;
    this.timer = null;
    this.busy = false;
    this.lastError = null;
    this.prefetchScheduled = false;
  }

  FramePlayer.prototype.frameCount = function () {
    return this.timeline && this.timeline.frames ? this.timeline.frames.length : 0;
  };

  FramePlayer.prototype.frame = function (i) {
    if (!this.timeline || !this.timeline.frames) return null;
    var idx = i == null ? this.index : i;
    return this.timeline.frames[idx] || null;
  };

  /** 当前帧对应的 images.json 帧对象（含 parts）。按 frame_id 对齐，缺失时返回 null。 */
  FramePlayer.prototype.frameImage = function (i) {
    if (!this.images || !this.images.frames) return null;
    var frame = this.frame(i);
    if (!frame) return null;
    for (var k = 0; k < this.images.frames.length; k += 1) {
      if (this.images.frames[k].frame_id === frame.frame_id) return this.images.frames[k];
    }
    return this.images.frames[i] || null;
  };

  FramePlayer.prototype.setData = function (params) {
    this.pause();
    this.timeline = params.timeline;
    this.images = params.images;
    this.caseId = params.caseId;
    this.runId = params.runId;
    this.index = 0;
    this.lastError = null;
    this.token += 1;
  };

  FramePlayer.prototype.setSpeed = function (speed) {
    this.speed = speed > 0 ? speed : 1;
    if (this.playing) this._schedule();
    this.onStateChange(this.state());
  };

  FramePlayer.prototype.state = function () {
    return {
      index: this.index,
      frameCount: this.frameCount(),
      playing: this.playing,
      speed: this.speed,
      maxMode: this.maxMode,
      busy: this.busy,
      lastError: this.lastError
    };
  };

  /** 请求切到指定帧（越界自动夹到有效范围；max 模式下拒绝）。 */
  FramePlayer.prototype.goTo = function (target, meta) {
    var self = this;
    var count = this.frameCount();
    if (!count) return Promise.resolve();
    if (this.maxMode) return Promise.resolve();
    var idx = Math.max(0, Math.min(count - 1, Math.round(Number(target) || 0)));
    var myToken = ++this.token;
    var frame = this.frame(idx);
    var imageEntry = this.frameImage(idx);
    this.busy = true;
    this.onStateChange(this.state());
    return Promise.resolve()
      .then(function () {
        return self.onCommit(idx, frame, imageEntry, myToken, meta || {});
      })
      .then(function () {
        if (myToken !== self.token) return null; // 过期响应，丢弃
        self.index = idx;
        self.busy = false;
        self.lastError = null;
        self.onCommitted(idx, frame, imageEntry);
        self.onStateChange(self.state());
        self._prefetchAround(idx);
        return null;
      })
      .catch(function (err) {
        if (myToken !== self.token) return null; // 已被更新的请求取代
        self.busy = false;
        self.lastError = { index: idx, frame: frame, message: (err && err.message) || String(err) };
        self.pause();
        self.onCommitError(idx, frame, err);
        self.onStateChange(self.state());
        return null;
      });
  };

  FramePlayer.prototype.next = function () {
    if (this.index >= this.frameCount() - 1) return Promise.resolve();
    return this.goTo(this.index + 1);
  };

  FramePlayer.prototype.prev = function () {
    if (this.index <= 0) return Promise.resolve();
    return this.goTo(this.index - 1);
  };

  FramePlayer.prototype.play = function () {
    if (this.playing || this.maxMode || this.frameCount() < 2) return;
    if (this.index >= this.frameCount() - 1) {
      // 从末帧播放时回到首帧再开始，避免看起来没反应
      this.goTo(0, { reason: 'replay' }).then(this._startTimingIfIdle.bind(this));
      this.playing = true;
      this.onStateChange(this.state());
      return;
    }
    this.playing = true;
    this.onStateChange(this.state());
    this._schedule();
  };

  FramePlayer.prototype._startTimingIfIdle = function () {
    if (this.playing && !this.timer) this._schedule();
  };

  FramePlayer.prototype.pause = function () {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.playing) {
      this.playing = false;
      this.onStateChange(this.state());
    }
  };

  FramePlayer.prototype.toggle = function () {
    if (this.playing) this.pause();
    else this.play();
  };

  FramePlayer.prototype._schedule = function () {
    var self = this;
    if (this.timer) clearTimeout(this.timer);
    if (!this.playing) return;
    var interval = 1000 / (this.speed > 0 ? this.speed : 1);
    this.timer = setTimeout(function () {
      self.timer = null;
      self._tick();
    }, Math.max(60, interval));
  };

  FramePlayer.prototype._tick = function () {
    var self = this;
    if (!this.playing || this.maxMode) return;
    if (this.index >= this.frameCount() - 1) {
      this.pause();
      return;
    }
    this.goTo(this.index + 1, { reason: 'play' }).then(function () {
      if (self.playing) self._schedule();
    });
  };

  /** 播放模式下不允许切换到包络视图：调用方在开启 max 前先 pause。 */
  FramePlayer.prototype.setMaxMode = function (on) {
    this.maxMode = !!on;
    if (this.maxMode) this.pause();
    this.onStateChange(this.state());
  };

  /* ---------------------------------------------------------------- 预取 */

  FramePlayer.prototype._urlsOf = function (index) {
    var entry = this.frameImage(index);
    if (!entry || !entry.parts) return [];
    return entry.parts.map(function (p) {
      return p.path;
    });
  };

  FramePlayer.prototype._prefetchAround = function (idx) {
    var self = this;
    if (this.prefetchScheduled) return;
    this.prefetchScheduled = true;
    var run = function () {
      self.prefetchScheduled = false;
      if (self.maxMode) return;
      var targets = [idx + 1, idx - 1];
      for (var i = 0; i < targets.length; i += 1) {
        var t = targets[i];
        if (t < 0 || t >= self.frameCount()) continue;
        self._urlsOf(t).forEach(function (url) {
          self.cache.preload(url).catch(function () {
            /* 预取失败静默：真正切到该帧时会显式报错 */
          });
        });
      }
    };
    if (global.requestIdleCallback) global.requestIdleCallback(run, { timeout: 400 });
    else setTimeout(run, 60);
  };

  /** 预取指定帧（供外部在空闲时调用）。 */
  FramePlayer.prototype.prefetch = function (index) {
    var self = this;
    return this._urlsOf(index).map(function (url) {
      return self.cache.preload(url).catch(function () {
        return null;
      });
    });
  };

  /** 最大包络的 part 列表（images.max.parts）。 */
  FramePlayer.prototype.maxParts = function () {
    if (!this.images || !this.images.max || !this.images.max.parts) return [];
    return this.images.max.parts;
  };

  global.FVTimeline = {
    ImageCache: ImageCache,
    FramePlayer: FramePlayer,
    formatTPlus: formatTPlus,
    formatFrameTime: formatFrameTime,
    formatAbsoluteTime: formatAbsoluteTime,
    describeOrigin: describeOrigin
  };
})(window);
