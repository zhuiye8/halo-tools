/* halobar console frontend (zero-dependency ES2020) */
'use strict';

const token = document.querySelector('meta[name="halo-token"]').content;
const $ = (id) => document.getElementById(id);

const api = async (path, options = {}) => {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Halo-Token': token,
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
  return data;
};

const fmt = (s) => {
  if (s === null || s === undefined || Number.isNaN(s)) return '0:00';
  const v = Math.max(0, Math.floor(s));
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, '0')}`;
};

const rgbToHex = (rgb) => {
  if (!Array.isArray(rgb) || rgb.length !== 3) return '#ffffff';
  return '#' + rgb.map((n) => Math.max(0, Math.min(255, Number(n) || 0)).toString(16).padStart(2, '0')).join('');
};

const hexToRgb = (hex) => {
  const clean = String(hex || '').replace('#', '');
  if (clean.length !== 6) return [255, 255, 255];
  return [
    parseInt(clean.slice(0, 2), 16),
    parseInt(clean.slice(2, 4), 16),
    parseInt(clean.slice(4, 6), 16),
  ];
};

function paintSlider(el) {
  if (!el) return;
  const min = Number(el.min || 0);
  const max = Number(el.max || 100);
  const val = Number(el.value || 0);
  const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
  el.style.setProperty('--fill', `${Math.max(0, Math.min(100, pct))}%`);
}

function paintAllSliders() {
  document.querySelectorAll('input[type="range"]').forEach(paintSlider);
}

// ------------------------------------------------------------ custom selects
const customSelects = new Map();

function closeAllSelects(exceptWrap = null) {
  customSelects.forEach(({ wrap, btn, menu }) => {
    if (wrap !== exceptWrap) {
      wrap.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
      menu.hidden = true;
    }
  });
}

function syncSelectUI(sel) {
  const entry = customSelects.get(sel);
  if (!entry) return;
  const opt = sel.options[sel.selectedIndex] || sel.options[0];
  entry.valEl.textContent = opt ? opt.textContent : '';
  entry.menu.querySelectorAll('.cselect-opt').forEach((item) => {
    item.setAttribute('aria-selected', String(item.dataset.val === sel.value));
  });
}

function syncAllSelects() {
  customSelects.forEach((_, sel) => syncSelectUI(sel));
}

function enhanceSelect(sel) {
  if (!sel || sel.hidden || customSelects.has(sel)) return;
  const wrap = document.createElement('div');
  wrap.className = 'cselect';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cselect-btn';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  if (sel.getAttribute('aria-label')) btn.setAttribute('aria-label', sel.getAttribute('aria-label'));
  btn.innerHTML = '<span class="cselect-val"></span>'
    + '<svg class="cselect-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';

  const menu = document.createElement('div');
  menu.className = 'cselect-menu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;

  Array.from(sel.options).forEach((opt) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'cselect-opt';
    item.dataset.val = opt.value;
    item.setAttribute('role', 'option');
    item.innerHTML = '<span></span>'
      + '<svg class="cselect-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    item.querySelector('span').textContent = opt.textContent;
    item.onclick = () => {
      sel.value = opt.value;
      syncSelectUI(sel);
      closeAllSelects();
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    };
    menu.appendChild(item);
  });

  btn.onclick = (e) => {
    e.stopPropagation();
    const opening = menu.hidden;
    closeAllSelects(wrap);
    if (opening) {
      const rect = btn.getBoundingClientRect();
      menu.classList.toggle('up', window.innerHeight - rect.bottom < 240 && rect.top > 240);
      menu.hidden = false;
      wrap.classList.add('open');
      btn.setAttribute('aria-expanded', 'true');
    } else {
      closeAllSelects();
    }
  };

  wrap.appendChild(btn);
  wrap.appendChild(menu);
  customSelects.set(sel, { wrap, btn, menu, valEl: btn.querySelector('.cselect-val') });
  syncSelectUI(sel);
}

function initCustomSelects() {
  document.querySelectorAll('select:not([hidden])').forEach(enhanceSelect);
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.cselect')) closeAllSelects();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllSelects();
  });
}

// ---------------------------------------------------------------------- toast
let toastTimer = null;
function showToast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, isError ? 3200 : 1800);
}

// --------------------------------------------------------------- modal dialog
let activeModalResolve = null;

function closeModal(result = false) {
  const backdrop = $('modal-backdrop');
  if (!backdrop || backdrop.hidden) return;
  backdrop.hidden = true;
  const resolve = activeModalResolve;
  activeModalResolve = null;
  if (resolve) resolve(result);
}

function showConfirm({ title, body, notes = '', confirmText = '确认', cancelText = '取消' }) {
  return new Promise((resolve) => {
    if (activeModalResolve) closeModal(false);
    activeModalResolve = resolve;
    $('modal-title').textContent = title || '确认操作';
    $('modal-body').textContent = body || '';
    const notesEl = $('modal-notes');
    const trimmed = String(notes || '').trim();
    if (trimmed) {
      notesEl.textContent = trimmed;
      notesEl.hidden = false;
    } else {
      notesEl.hidden = true;
    }
    $('modal-confirm').textContent = confirmText;
    $('modal-cancel').textContent = cancelText;
    $('modal-backdrop').hidden = false;
    $('modal-confirm').focus();
  });
}

$('modal-cancel').onclick = () => closeModal(false);
$('modal-confirm').onclick = () => closeModal(true);
$('modal-backdrop').addEventListener('click', (e) => {
  if (e.target === $('modal-backdrop')) closeModal(false);
});
document.addEventListener('keydown', (e) => {
  if ($('modal-backdrop').hidden) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeModal(false);
  }
});

// ---------------------------------------------------------------------- theme
const mediaDark = window.matchMedia('(prefers-color-scheme: dark)');
let currentThemePref = 'system';

function applyTheme(pref) {
  currentThemePref = pref || 'system';
  const resolved = currentThemePref === 'system'
    ? (mediaDark.matches ? 'dark' : 'light')
    : currentThemePref;
  document.documentElement.setAttribute('data-theme', resolved);
  $('theme-seg').querySelectorAll('button').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.theme === currentThemePref));
  });
}

mediaDark.addEventListener('change', () => {
  if (currentThemePref === 'system') applyTheme('system');
});
applyTheme('system');

// --------------------------------------------------------------------- state
const STATE_TEXT = {
  idle: '空闲',
  following: '同步中',
  paused: '已暂停',
  restored: '已恢复屏幕',
  'waiting-device': '等待设备',
  error: '错误',
  connecting: '连接中',
};

const SCENE_NAMES = {
  0: '时钟', 1: '游戏', 2: '工作', 3: '阅读 / 自定义静图',
  4: '猫咪', 5: '小狗', 6: '梗图 / 自定义动画', 7: '赛博', 8: '声波',
};

let lastState = '';
let lastTrackKey = '';
let lastLyricSource = '';
let lastActiveIndex = -1;
let lastMirrorKey = '';
let activeColorHex = '#ffffff';
let refreshInFlight = false;
let logsInFlight = false;
let lyricsTimeline = null;
let lastAsset = null;

// ----------------------------------------------------------- 256x32 LED mirror
const mirrorCanvas = $('mirror-canvas');
const mirrorCtx = mirrorCanvas.getContext('2d');
const mirrorOffscreen = document.createElement('canvas');
mirrorOffscreen.width = 256;
mirrorOffscreen.height = 32;
const mirrorOffCtx = mirrorOffscreen.getContext('2d', { willReadFrequently: true });

function renderMirror(status) {
  const mode = status.display_mode || 'idle';
  const text = status.display_text || '';
  const scene = status.display_scene;
  const colorHex = rgbToHex(status.color);
  activeColorHex = colorHex;

  const key = `${mode}|${text}|${scene}|${colorHex}|${status.connected}`;
  if (key === lastMirrorKey) return;
  lastMirrorKey = key;

  $('mirror-color').textContent = colorHex.toUpperCase();
  if (!status.connected) {
    $('mirror-mode').textContent = '设备未连接';
  } else if (mode === 'text' && text) {
    $('mirror-mode').textContent = 'PIXELBAR 256×32 · 歌词输出';
  } else if (mode === 'standby' || mode === 'scene') {
    $('mirror-mode').textContent = `PIXELBAR 256×32 · 场景「${SCENE_NAMES[scene] ?? scene ?? '-'}」`;
  } else if (mode === 'restored') {
    $('mirror-mode').textContent = 'PIXELBAR 256×32 · 已归还控制权';
  } else {
    $('mirror-mode').textContent = 'PIXELBAR 256×32 · 待命';
  }

  // Draw crisp 1-bit pixel matrix onto 256x32 offscreen canvas
  mirrorOffCtx.fillStyle = '#000';
  mirrorOffCtx.fillRect(0, 0, 256, 32);

  if (mode === 'text' && text) {
    mirrorOffCtx.fillStyle = '#fff';
    mirrorOffCtx.font = '700 20px "Microsoft YaHei UI", "Segoe UI", sans-serif';
    mirrorOffCtx.textAlign = 'center';
    mirrorOffCtx.textBaseline = 'middle';
    mirrorOffCtx.fillText(text, 128, 17, 248);
  } else if (mode === 'standby' || mode === 'scene') {
    // Draw clean dot-matrix equalizer / wave representation
    mirrorOffCtx.fillStyle = '#fff';
    const bars = 28;
    for (let i = 0; i < bars; i++) {
      const h = Math.round(5 + 10 * (0.5 + 0.5 * Math.sin(i * 0.55)));
      const x = 14 + i * 8;
      mirrorOffCtx.fillRect(x, 16 - Math.floor(h / 2), 4, h);
    }
  } else if (!status.connected) {
    mirrorOffCtx.fillStyle = '#555';
    mirrorOffCtx.font = '12px "Cascadia Mono", monospace';
    mirrorOffCtx.textAlign = 'center';
    mirrorOffCtx.textBaseline = 'middle';
    mirrorOffCtx.fillText('NO DEVICE', 128, 16);
  } else {
    mirrorOffCtx.fillStyle = '#666';
    mirrorOffCtx.font = '12px "Cascadia Mono", monospace';
    mirrorOffCtx.textAlign = 'center';
    mirrorOffCtx.textBaseline = 'middle';
    mirrorOffCtx.fillText(mode === 'restored' ? 'RESTORED' : 'HALO PIXELBAR', 128, 16);
  }

  // Render onto visible LED matrix with subtle pixel grid spacing
  const src = mirrorOffCtx.getImageData(0, 0, 256, 32).data;
  mirrorCtx.fillStyle = '#08070d';
  mirrorCtx.fillRect(0, 0, 512, 64);
  const [r, g, b] = hexToRgb(colorHex);
  const dim = `rgba(${r},${g},${b},0.07)`;
  const lit = `rgb(${r},${g},${b})`;

  // Sample every 2px horizontally/vertically for crisp 128x16 visible LED feel or 256x32 2x2 dots
  for (let y = 0; y < 32; y++) {
    for (let x = 0; x < 256; x++) {
      const idx = (y * 256 + x) * 4;
      const on = src[idx] >= 72;
      if (on) {
        mirrorCtx.fillStyle = lit;
        mirrorCtx.fillRect(x * 2, y * 2, 1.85, 1.85);
      } else if ((x & 1) === 0 && (y & 1) === 0) {
        mirrorCtx.fillStyle = dim;
        mirrorCtx.fillRect(x * 2, y * 2, 1, 1);
      }
    }
  }
}

// ------------------------------------------------------------- lyric timeline
async function fetchLyrics() {
  try {
    const data = await api('/api/lyrics');
    lyricsTimeline = data;
    renderTimeline(data);
  } catch (e) { /* ignore */ }
}

function renderTimeline(data) {
  const box = $('timeline');
  const lines = (data && data.lines) || [];
  if (!lines.length) {
    box.innerHTML = `<div class="timeline-empty">${
      data && data.title ? '暂未匹配到滚动歌词（可点右上角「换词源」手动选择）' : '播放歌曲后这里会显示实时歌词时间轴'
    }</div>`;
    lastActiveIndex = -1;
    return;
  }
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lyric-item' + (line.index === data.active_index ? ' active' : '');
    btn.dataset.index = String(line.index);
    btn.setAttribute('role', 'option');
    btn.setAttribute('aria-selected', String(line.index === data.active_index));

    const ts = document.createElement('span');
    ts.className = 'ts mono';
    ts.textContent = fmt(line.start);

    const body = document.createElement('span');
    body.className = 'body';
    body.textContent = line.text;
    if (line.translation && line.translation !== line.text) {
      const tr = document.createElement('span');
      tr.className = 'trans';
      tr.textContent = line.translation;
      body.appendChild(tr);
    }

    btn.appendChild(ts);
    btn.appendChild(body);
    btn.onclick = () => jumpToLine(line.index);
    frag.appendChild(btn);
  }
  box.innerHTML = '';
  box.appendChild(frag);
  lastActiveIndex = -1;
  highlightTimelineIndex(data.active_index, true);
}

function highlightTimelineIndex(index, immediate = false) {
  if (index === lastActiveIndex) return;
  lastActiveIndex = index;
  const box = $('timeline');
  const items = box.querySelectorAll('.lyric-item');
  let activeEl = null;
  items.forEach((el) => {
    const isCurrent = Number(el.dataset.index) === index;
    el.classList.toggle('active', isCurrent);
    el.setAttribute('aria-selected', String(isCurrent));
    if (isCurrent) activeEl = el;
  });
  if (activeEl) {
    const targetTop = activeEl.offsetTop - box.clientHeight / 2 + activeEl.clientHeight / 2;
    box.scrollTo({ top: Math.max(0, targetTop), behavior: immediate ? 'auto' : 'smooth' });
  }
}

async function jumpToLine(index) {
  const res = await api('/api/command', {
    method: 'POST',
    body: JSON.stringify({ action: 'jump_line', value: index }),
  });
  if (res.error) showToast(res.error, true);
  else showToast('已对齐到该句');
  refresh();
}

// ------------------------------------------------------------- candidates
$('btn-candidates').onclick = async () => {
  const panel = $('candidate-panel');
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  $('candidate-list').innerHTML = '<div class="hint" style="margin:6px">正在搜索网易云候选歌词…</div>';
  const res = await api('/api/lyrics/candidates');
  if (res.error) {
    $('candidate-list').innerHTML = `<div class="hint bad" style="margin:6px">${res.error}</div>`;
    return;
  }
  const items = res.candidates || [];
  if (!items.length) {
    $('candidate-list').innerHTML = '<div class="hint" style="margin:6px">没有找到候选歌曲（确认当前正在播放音乐）</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const item of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'candidate-item';
    btn.innerHTML = `<span class="c-main"><span class="c-title"></span><span class="c-sub mono"></span></span><span class="chip no-dot mono">${Math.round((item.score || 0) * 100)}%</span>`;
    btn.querySelector('.c-title').textContent = item.title;
    btn.querySelector('.c-sub').textContent = `${item.artist || '未知歌手'} · ${fmt(item.duration)} · ID ${item.song_id}`;
    btn.onclick = async () => {
      const sel = await api('/api/lyrics/select', {
        method: 'POST',
        body: JSON.stringify({ song_id: item.song_id }),
      });
      if (sel.error) {
        showToast(sel.error, true);
        return;
      }
      panel.hidden = true;
      showToast(`已切换歌词：${item.title}`);
      if (sel.lyrics) renderTimeline(sel.lyrics);
      refresh();
    };
    frag.appendChild(btn);
  }
  $('candidate-list').innerHTML = '';
  $('candidate-list').appendChild(frag);
};

$('btn-close-candidates').onclick = () => { $('candidate-panel').hidden = true; };

// --------------------------------------------------------------- live status
function setSegValue(segId, val) {
  const seg = $(segId);
  if (!seg) return;
  seg.querySelectorAll('button').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.val === String(val)));
  });
}

function syncSwatches(rgb) {
  const key = Array.isArray(rgb) ? rgb.join(',') : '';
  $('color-swatches').querySelectorAll('.swatch').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.rgb === key);
  });
  const hex = rgbToHex(rgb);
  if (document.activeElement !== $('color-picker')) {
    $('color-picker').value = hex;
  }
}

function syncAmbientUI(amb) {
  if (!amb) return;
  const enabled = amb.enabled !== false;
  const effect = Number(amb.effect || 1);
  $('ambient-power').checked = enabled;
  $('ambient-controls').style.opacity = enabled ? '1' : '0.45';
  if (document.activeElement !== $('ambient-effect')) {
    $('ambient-effect').value = String(effect);
    syncSelectUI($('ambient-effect'));
  }
  const rgb = amb.color || [195, 185, 255];
  const key = Array.isArray(rgb) ? rgb.join(',') : '';
  $('ambient-swatches').querySelectorAll('.swatch').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.rgb === key);
  });
  if (document.activeElement !== $('ambient-color-picker')) {
    $('ambient-color-picker').value = rgbToHex(rgb);
  }
  if (document.activeElement !== $('ambient-brightness')) {
    $('ambient-brightness').value = amb.brightness ?? 100;
    $('ambient-brightness-val').textContent = `${amb.brightness ?? 100}%`;
    paintSlider($('ambient-brightness'));
  }
  if (document.activeElement !== $('ambient-speed')) {
    $('ambient-speed').value = amb.speed ?? 8;
    $('ambient-speed-val').textContent = String(amb.speed ?? 8);
    paintSlider($('ambient-speed'));
  }
  // RGB applies to Breathing(1) and Static(3); Speed applies to Breathing(1) and Tide(2).
  $('ambient-color-row').hidden = ![1, 3].includes(effect);
  $('ambient-speed-row').hidden = ![1, 2].includes(effect);
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const s = await api('/api/status');
    const prevState = lastState;
    lastState = s.state || '';

    const stateEl = $('state');
    stateEl.textContent = STATE_TEXT[s.state] || s.state || '-';
    stateEl.className = 'chip ' + (s.connected ? (s.state === 'following' ? 'ok' : 'warn') : 'err');
    if (prevState && prevState !== lastState) {
      stateEl.classList.remove('pulse');
      void stateEl.offsetWidth;
      stateEl.classList.add('pulse');
    }

    $('source').textContent = s.source === 'none' ? '无进度源' : s.source;
    $('source').className = 'chip ' + (s.source === '6k-labs' ? 'ok' : 'warn');

    const healthEl = $('health-chip');
    if (s.smtc_health === 'backoff' || s.smtc_health === 'circuit-open') {
      healthEl.hidden = false;
      healthEl.textContent = s.smtc_health === 'circuit-open' ? 'SMTC 熔断保护中' : 'SMTC 退避中';
      healthEl.className = 'chip ' + (s.smtc_health === 'circuit-open' ? 'err' : 'warn');
    } else {
      healthEl.hidden = true;
    }

    $('ver').textContent = 'v' + (s.version || '-');
    const build = s.build || {};
    $('buildline').textContent = build.commit
      ? `v${s.version} · ${build.commit} · ${(build.built_at || '').slice(0, 16).replace('T', ' ')}`
      : `v${s.version || 'dev'} · source checkout`;

    renderMirror(s);
    renderUpdate(s);

    $('track').textContent = s.track || '未检测到播放';
    $('artist').textContent = s.artist || '—';
    $('pos').textContent = fmt(s.position);
    $('dur').textContent = fmt(s.duration);
    const pct = (s.position && s.duration) ? Math.min(100, s.position / s.duration * 100) : 0;
    $('bar').style.width = pct + '%';

    const offset = Number(s.clock_offset || 0);
    if (Math.abs(offset) >= 0.05) {
      $('offset-wrap').hidden = false;
      $('offset-val').textContent = `${offset > 0 ? '+' : ''}${offset.toFixed(1)}s`;
    } else {
      $('offset-wrap').hidden = true;
    }

    // Reload full timeline only when track or lyric source changes
    if (s.track_key !== lastTrackKey || s.lyrics_source !== lastLyricSource) {
      lastTrackKey = s.track_key || '';
      lastLyricSource = s.lyrics_source || '';
      fetchLyrics();
    } else {
      highlightTimelineIndex(s.lyric_index ?? -1);
    }

    $('conn').textContent = s.connected ? '已连接' : '未连接';
    $('conn').className = s.connected ? 'ok' : 'bad';
    $('fw').textContent = s.firmware || '-';
    $('src2').textContent = s.source || '-';
    $('lsrc').textContent = s.lyrics_source || '-';

    const canToggle = ['following', 'paused', 'restored'].includes(s.state);
    $('btn-toggle').disabled = !canToggle;
    $('btn-toggle').textContent = (s.state === 'paused' || s.state === 'restored') ? '继续同步' : '暂停同步';

    // Sync controls when user is not actively typing/selecting
    if (s.settings && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'SELECT') {
      setSegValue('lang-seg', s.settings.lyric_lang || 'original');
      setSegValue('linemode-seg', s.settings.line_mode || 'page');
      setSegValue('align-seg', s.settings.align || 'center');
      $('animation').value = s.settings.animation === null ? '' : String(s.settings.animation);
      $('standby-scene').value = s.settings.standby_scene === null ? '' : String(s.settings.standby_scene);
      $('standby').value = s.settings.standby_after;
      $('auto-update').checked = s.settings.auto_check_updates !== false;
      $('update-proxy').value = s.settings.update_proxy || 'auto';
      syncAllSelects();
      $('autostart').checked = !!s.settings.autostart;
      $('color-calibration').checked = s.settings.color_calibration !== false;
      $('ambient-sync').checked = !!s.settings.ambient_sync_color;
      if (s.settings.theme && s.settings.theme !== currentThemePref) {
        applyTheme(s.settings.theme);
      }
      syncSwatches(s.settings.color || s.color);
      syncAmbientUI(s.ambient);
    }

    const uploadPct = Math.round((s.upload_progress || 0) * 100);
    $('asset-bar').style.width = uploadPct + '%';
    if (s.upload_state === 'uploading' || s.upload_state === 'queued') {
      $('asset-progress').textContent = `上传中 ${uploadPct}%`;
    } else if (s.upload_state === 'error') {
      $('asset-progress').textContent = '上传失败：' + (s.last_error || '');
    } else if (s.upload_state === 'done' && s.asset_name) {
      $('asset-info').textContent = `${s.asset_name} · ${s.asset_frames} 帧 · 场景 ${s.asset_category}/${s.asset_option}`;
      $('asset-progress').textContent = '上传完成';
    }

    renderEnvironment(s.environment);
  } catch (e) {
    $('state').textContent = '控制台离线';
    $('state').className = 'chip err';
  } finally {
    refreshInFlight = false;
  }
}

async function command(action, extra = {}) {
  const res = await api('/api/command', {
    method: 'POST',
    body: JSON.stringify({ action, ...extra }),
  });
  if (res.error) showToast(res.error, true);
  refresh();
  return res;
}

async function patchSettings(patch, toastText = '已应用') {
  const res = await api('/api/settings', {
    method: 'POST',
    body: JSON.stringify(patch),
  });
  if (res.error) {
    showToast(res.error, true);
  } else if (toastText) {
    showToast(toastText);
  }
  refresh();
}

// ------------------------------------------------------ instant control wiring
$('btn-toggle').onclick = () => command(lastState === 'paused' || lastState === 'restored' ? 'resume' : 'pause');
$('btn-restore').onclick = () => command('restore');
$('btn-minus').onclick = () => command('nudge', { value: -0.5 });
$('btn-plus').onclick = () => command('nudge', { value: 0.5 });
$('btn-next').onclick = () => command('next_line');
$('btn-reset-offset').onclick = async () => {
  await command('reset_offset');
  showToast('偏移已归零');
};

// Keyboard shortcuts when not focused on an input or modal
document.addEventListener('keydown', (e) => {
  if (!$('modal-backdrop').hidden) return;
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return;
  if (e.code === 'Space' && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    $('btn-toggle').click();
  } else if (e.key === '[') {
    command('nudge', { value: -0.5 });
    showToast('−0.5s');
  } else if (e.key === ']') {
    command('nudge', { value: 0.5 });
    showToast('+0.5s');
  }
});

// Segmented controls
$('theme-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-theme]');
  if (!btn) return;
  applyTheme(btn.dataset.theme);
  patchSettings({ theme: btn.dataset.theme }, '');
});

$('lang-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-val]');
  if (!btn) return;
  setSegValue('lang-seg', btn.dataset.val);
  patchSettings({ lyric_lang: btn.dataset.val });
});

$('linemode-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-val]');
  if (!btn) return;
  setSegValue('linemode-seg', btn.dataset.val);
  patchSettings({ line_mode: btn.dataset.val });
});

$('align-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-val]');
  if (!btn) return;
  setSegValue('align-seg', btn.dataset.val);
  patchSettings({ align: btn.dataset.val });
});

$('animation').onchange = () => {
  const val = $('animation').value;
  patchSettings({ animation: val === '' ? null : Number(val) });
};

$('standby-scene').onchange = () => {
  const val = $('standby-scene').value;
  const sceneId = val === '' ? null : Number(val);
  const option = sceneId === 3 ? 6 : (sceneId === 6 ? 52 : 0);
  patchSettings({ standby_scene: sceneId, standby_scene_option: option });
};

$('btn-preview-scene').onclick = async () => {
  const val = $('standby-scene').value;
  if (val === '') { showToast('请先选择一个场景', true); return; }
  const category = Number(val);
  const option = category === 3 ? 6 : (category === 6 ? 52 : 0);
  await command('scene', { category, option });
  showToast(`已切换至「${SCENE_NAMES[category] || category}」`);
};

$('standby').onchange = () => {
  patchSettings({ standby_after: Number($('standby').value) });
};

$('auto-update').onchange = () => patchSettings({ auto_check_updates: $('auto-update').checked });
$('update-proxy').onchange = () => patchSettings({ update_proxy: $('update-proxy').value });
$('autostart').onchange = () => patchSettings(
  { autostart: $('autostart').checked },
  $('autostart').checked ? '已开启开机自启' : '已关闭开机自启',
);

$('color-calibration').onchange = () => patchSettings(
  { color_calibration: $('color-calibration').checked },
  $('color-calibration').checked ? '已开启 LED 色彩校正' : '已切换为原始 RGB 输出',
);

// Colour swatches + picker
$('color-swatches').addEventListener('click', (e) => {
  const btn = e.target.closest('.swatch');
  if (!btn) return;
  const rgb = btn.dataset.rgb.split(',').map(Number);
  syncSwatches(rgb);
  command('color', { rgb }).then(() => showToast(`颜色已设为 ${rgbToHex(rgb).toUpperCase()}`));
});

let colorDebounce = null;
$('color-picker').addEventListener('input', () => {
  const rgb = hexToRgb($('color-picker').value);
  syncSwatches(rgb);
  clearTimeout(colorDebounce);
  colorDebounce = setTimeout(() => command('color', { rgb }), 140);
});

// Surround ambient light strip controls
async function updateAmbient(patch, toastText = '') {
  const res = await api('/api/ambient', {
    method: 'POST',
    body: JSON.stringify(patch),
  });
  if (res.error) showToast(res.error, true);
  else if (toastText) showToast(toastText);
  if (res.status && res.status.ambient) syncAmbientUI(res.status.ambient);
  else refresh();
}

$('ambient-power').onchange = () => {
  const on = $('ambient-power').checked;
  updateAmbient({ enabled: on }, on ? '氛围灯已开启' : '氛围灯已关闭');
};

$('ambient-sync').onchange = () => patchSettings(
  { ambient_sync_color: $('ambient-sync').checked },
  $('ambient-sync').checked ? '氛围灯将跟随屏幕颜色' : '已解除氛围灯颜色同步',
);

$('ambient-effect').onchange = () => {
  const effect = Number($('ambient-effect').value);
  const label = $('ambient-effect').selectedOptions[0]?.textContent || '';
  updateAmbient({ enabled: true, effect }, `氛围灯：${label}`);
};

$('ambient-swatches').addEventListener('click', (e) => {
  const btn = e.target.closest('.swatch');
  if (!btn) return;
  const rgb = btn.dataset.rgb.split(',').map(Number);
  updateAmbient({ enabled: true, rgb }, `灯带颜色 ${rgbToHex(rgb).toUpperCase()}`);
});

let ambColorDebounce = null;
$('ambient-color-picker').addEventListener('input', () => {
  const rgb = hexToRgb($('ambient-color-picker').value);
  clearTimeout(ambColorDebounce);
  ambColorDebounce = setTimeout(() => updateAmbient({ enabled: true, rgb }), 160);
});

let ambBrightDebounce = null;
$('ambient-brightness').addEventListener('input', () => {
  const el = $('ambient-brightness');
  paintSlider(el);
  const val = Number(el.value);
  $('ambient-brightness-val').textContent = `${val}%`;
  clearTimeout(ambBrightDebounce);
  ambBrightDebounce = setTimeout(() => updateAmbient({ enabled: true, brightness: val }), 160);
});

let ambSpeedDebounce = null;
$('ambient-speed').addEventListener('input', () => {
  const el = $('ambient-speed');
  paintSlider(el);
  const val = Number(el.value);
  $('ambient-speed-val').textContent = String(val);
  clearTimeout(ambSpeedDebounce);
  ambSpeedDebounce = setTimeout(() => updateAmbient({ enabled: true, speed: val }), 160);
});

// ------------------------------------------------------------- env & updates
function renderEnvironment(env) {
  if (!env || !env.links) return;
  const links = env.links;
  $('env-netease').innerHTML = env.netease_exe
    ? `<span class="ok">已检测</span> <span style="color:var(--ink-3);font-size:12px">${env.netease_path}</span>`
    : `<span class="bad">未检测到官网版</span> · <a class="link" href="${links.netease}" target="_blank">下载</a>`;
  $('env-bncm').innerHTML = env.betterncm
    ? '<span class="ok">已安装</span>'
    : `<span class="no">未安装</span> · <a class="link" href="${links.betterncm}" target="_blank">BetterNCM</a> → <a class="link" href="${links.inflink}" target="_blank">InfLink-rs</a> → <a class="link" href="${links.sixk}" target="_blank">6K Labs</a>`;
  $('env-sixk').innerHTML = env.sixk
    ? '<span class="ok">在线（真实进度）</span>'
    : (env.betterncm
      ? `<span class="no">未连接（网易云未开或插件未加载）</span>`
      : `<span class="no">未安装（当前为 SMTC 事件 + 时钟估算）</span>`);
  $('env-tempo').innerHTML = env.tempo_hub
    ? '<span class="no">正在运行，可能抢占设备，建议退出</span>'
    : '<span class="ok">未运行</span>';

  const banner = $('env-banner');
  if (!env.sixk) {
    banner.hidden = false;
    banner.innerHTML =
      '<b>想要拖动进度条也精准同步？</b>给官网版网易云装上 6K Labs 插件链（约 5 分钟）：' +
      `<ol>
         <li>使用 <a class="link" href="${links.netease}" target="_blank">官网版网易云</a>（Store 版不可注入）</li>
         <li>安装 <a class="link" href="${links.betterncm}" target="_blank">BetterNCM</a> 并放入 <a class="link" href="${links.inflink}" target="_blank">InfLink-rs</a> 与 <a class="link" href="${links.sixk}" target="_blank">6K Labs</a></li>
       </ol>`;
  } else {
    banner.hidden = true;
  }
}

$('btn-shortcut').onclick = async () => {
  const res = await api('/api/shortcut', { method: 'POST', body: '{}' });
  if (res.error) showToast(`创建失败：${res.error}`, true);
  else showToast('已创建桌面快捷方式');
};

let latestUpdateMeta = { version: '', notes: '' };

function renderUpdate(s) {
  const state = s.update_state || 'idle';
  const version = s.update_version || '';
  const progress = Math.round((s.update_progress || 0) * 100);
  const current = 'v' + (s.version || '-');
  latestUpdateMeta = { version, notes: s.update_notes || '' };
  const line = $('update-line');
  const button = $('btn-update');
  const bar = $('update-progress');
  const notes = $('update-notes');

  if (['available', 'downloading', 'ready', 'applying'].includes(state)) {
    line.innerHTML = `当前 ${current} · 发现新版本 <b style="color:var(--accent)">v${version}</b>`
      + (s.update_url ? ` · <a class="link" href="${s.update_url}" target="_blank">查看</a>` : '');
    notes.hidden = false;
    $('update-notes-body').textContent = s.update_notes || '（无更新说明）';
    button.hidden = state === 'applying';
    button.textContent = state === 'downloading' ? `下载中 ${progress}%` : (state === 'ready' ? '重启并安装' : '立即更新');
    button.disabled = state === 'downloading';
    bar.hidden = state !== 'downloading';
    $('update-bar').style.width = progress + '%';
  } else if (state === 'checking') {
    line.textContent = `当前 ${current} · 正在检查…`;
    button.hidden = true;
    bar.hidden = true;
  } else if (state === 'none') {
    line.textContent = `当前 ${current} · 已是最新版本`;
    button.hidden = true;
    bar.hidden = true;
    notes.hidden = true;
  } else if (state === 'error') {
    line.innerHTML = `当前 ${current} · 检查失败：${s.update_error || '未知错误'}`
      + ' · <a class="link" href="https://github.com/zhuiye8/halo-tools/releases/latest" target="_blank">手动下载</a>';
    button.hidden = true;
    bar.hidden = true;
  } else {
    line.textContent = `当前 ${current}`;
    button.hidden = true;
    bar.hidden = true;
  }
}

$('btn-check-update').onclick = async () => {
  $('update-line').textContent = '正在检查…';
  await api('/api/update/check', { method: 'POST', body: '{}' });
  setTimeout(refresh, 1500);
};

$('btn-update').onclick = async () => {
  const verLabel = latestUpdateMeta.version ? ` v${latestUpdateMeta.version}` : '';
  const ok = await showConfirm({
    title: `更新到${verLabel || '新版本'}`,
    body: '将自动下载安装包、替换程序文件并重启 halobar，本地设置与歌词记忆会完整保留。',
    notes: latestUpdateMeta.notes,
    confirmText: '开始更新',
    cancelText: '稍后再说',
  });
  if (!ok) return;
  await api('/api/update/apply', { method: 'POST', body: '{}' });
  showToast('开始下载更新…');
  setTimeout(refresh, 800);
};

// ------------------------------------------------------------ logs & health
async function refreshLogs() {
  if (logsInFlight) return;
  logsInFlight = true;
  try {
    const data = await api('/api/logs');
    $('logs').textContent = (data.lines || []).slice(-120).join('\n') || '（暂无日志）';
    $('logs').scrollTop = $('logs').scrollHeight;
  } catch (e) { /* ignore */ } finally {
    logsInFlight = false;
  }
}

$('btn-diag').onclick = async () => {
  const box = $('diag-box');
  if (!box.hidden) {
    box.hidden = true;
    return;
  }
  const d = await api('/api/debug/threads');
  const smtc = (d.media && d.media.smtc) || {};
  const sixk = (d.media && d.media.sixk) || {};
  box.hidden = false;
  box.textContent =
    `threads=${d.threads} handles=${d.handles} | `
    + `6k=${sixk.online ? 'online' : 'offline'}(reads=${sixk.reads || 0}) | `
    + `smtc[${smtc.mode || '-'}] scans=${smtc.session_scans || 0} meta=${smtc.metadata_reads || 0} `
    + `state=${smtc.state_reads || 0} evt=${smtc.events_received || 0} to=${smtc.timeouts || 0}`;
};

// ------------------------------------------------------------ pixel studio
const SCREEN_W = 256;
const SCREEN_H = 32;
let sourceImage = null;
let selectedFile = null;
const view = { zoom: 1, offsetX: 0, offsetY: 0, dragging: false, startX: 0, startY: 0 };

const fittedCanvas = document.createElement('canvas');
fittedCanvas.width = SCREEN_W;
fittedCanvas.height = SCREEN_H;
const finalCanvas = document.createElement('canvas');
finalCanvas.width = SCREEN_W;
finalCanvas.height = SCREEN_H;
const tmpCanvas = document.createElement('canvas');
tmpCanvas.width = SCREEN_W;
tmpCanvas.height = SCREEN_H;
const tmpOutCanvas = document.createElement('canvas');
tmpOutCanvas.width = SCREEN_W;
tmpOutCanvas.height = SCREEN_H;

function setSourceImage(bitmap) {
  if (sourceImage && typeof sourceImage.close === 'function') {
    sourceImage.close();
  }
  sourceImage = bitmap;
}

function currentOptions() {
  return {
    fit: $('asset-fit').value,
    zoom: Number($('asset-zoom').value),
    offset_x: view.offsetX,
    offset_y: view.offsetY,
    threshold: Number($('asset-threshold').value),
    brightness: Number($('asset-brightness').value),
    contrast: Number($('asset-contrast').value),
    invert: $('asset-invert').checked,
    dither: $('asset-dither').checked,
  };
}

function fitSource(image) {
  const fctx = fittedCanvas.getContext('2d', { willReadFrequently: true });
  fctx.fillStyle = '#000';
  fctx.fillRect(0, 0, SCREEN_W, SCREEN_H);
  const fit = $('asset-fit').value;
  let w; let h;
  if (fit === 'cover') {
    const scale = Math.max(SCREEN_W / image.width, SCREEN_H / image.height);
    w = image.width * scale; h = image.height * scale;
  } else if (fit === 'stretch') {
    w = SCREEN_W; h = SCREEN_H;
  } else {
    const scale = Math.min(SCREEN_W / image.width, SCREEN_H / image.height);
    w = image.width * scale; h = image.height * scale;
  }
  fctx.imageSmoothingEnabled = true;
  fctx.drawImage(image, (SCREEN_W - w) / 2, (SCREEN_H - h) / 2, w, h);
  return fctx.getImageData(0, 0, SCREEN_W, SCREEN_H);
}

function floydSteinberg(values, threshold) {
  const w = SCREEN_W; const h = SCREEN_H;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      const old = values[i];
      const nw = old >= threshold ? 255 : 0;
      const err = old - nw;
      values[i] = nw;
      if (x + 1 < w) values[i + 1] += err * 7 / 16;
      if (y + 1 < h) {
        if (x > 0) values[i + w - 1] += err * 3 / 16;
        values[i + w] += err * 5 / 16;
        if (x + 1 < w) values[i + w + 1] += err / 16;
      }
    }
  }
}

function renderPreview() {
  if (!sourceImage) return;
  const opts = currentOptions();
  const base = fitSource(sourceImage);

  const drawW = SCREEN_W * opts.zoom;
  const drawH = SCREEN_H * opts.zoom;
  const x = (SCREEN_W - drawW) / 2 + opts.offset_x * SCREEN_W;
  const y = (SCREEN_H - drawH) / 2 + opts.offset_y * SCREEN_H;

  tmpCanvas.getContext('2d').putImageData(base, 0, 0);
  const fctx = finalCanvas.getContext('2d', { willReadFrequently: true });
  fctx.fillStyle = '#000';
  fctx.fillRect(0, 0, SCREEN_W, SCREEN_H);
  fctx.imageSmoothingEnabled = true;
  fctx.drawImage(tmpCanvas, x, y, drawW, drawH);
  const image = fctx.getImageData(0, 0, SCREEN_W, SCREEN_H);

  const gray = new Float32Array(SCREEN_W * SCREEN_H);
  for (let i = 0; i < gray.length; i++) {
    const p = i * 4;
    gray[i] = 0.299 * image.data[p] + 0.587 * image.data[p + 1] + 0.114 * image.data[p + 2];
  }
  for (let i = 0; i < gray.length; i++) {
    let v = (gray[i] - 128) * opts.contrast + 128 + opts.brightness;
    if (opts.invert) v = 255 - v;
    gray[i] = Math.max(0, Math.min(255, v));
  }
  const [lr, lg, lb] = hexToRgb(activeColorHex);
  const out = new Uint8ClampedArray(SCREEN_W * SCREEN_H * 4);
  if (opts.dither) floydSteinberg(gray, opts.threshold);
  for (let i = 0; i < gray.length; i++) {
    const on = opts.dither ? gray[i] >= 128 : gray[i] >= opts.threshold;
    const p = i * 4;
    out[p] = on ? lr : 0;
    out[p + 1] = on ? lg : 0;
    out[p + 2] = on ? lb : 0;
    out[p + 3] = 255;
  }
  const outImage = new ImageData(out, SCREEN_W, SCREEN_H);
  const viewCanvas = $('asset-canvas');
  const vctx = viewCanvas.getContext('2d');
  vctx.imageSmoothingEnabled = false;
  tmpOutCanvas.getContext('2d').putImageData(outImage, 0, 0);
  vctx.drawImage(tmpOutCanvas, 0, 0, viewCanvas.width, viewCanvas.height);
  $('asset-empty').style.display = 'none';
}

function resetView() {
  view.zoom = 1; view.offsetX = 0; view.offsetY = 0;
  $('asset-zoom').value = 1;
  $('asset-zoom-val').textContent = '100%';
  paintSlider($('asset-zoom'));
  renderPreview();
}

async function loadImageFile(file) {
  if (!file) return;
  selectedFile = file;
  try {
    setSourceImage(await createImageBitmap(file));
  } catch (e) {
    showToast('无法读取这张图片', true);
    return;
  }
  const label = `${file.name || '剪贴板图片'} · ${sourceImage.width}×${sourceImage.height}`;
  $('asset-info').textContent = label;
  if ($('file-drop-label')) $('file-drop-label').textContent = label;
  $('asset-progress').textContent = '';
  resetView();
}

$('asset-file').onchange = () => loadImageFile($('asset-file').files[0]);
$('asset-empty').onclick = () => $('asset-file').click();

$('asset-fit-seg').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-val]');
  if (!btn) return;
  setSegValue('asset-fit-seg', btn.dataset.val);
  $('asset-fit').value = btn.dataset.val;
  renderPreview();
});

// Drag & drop + clipboard paste
const dropzone = $('asset-dropzone');
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('dragover');
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) loadImageFile(file);
});

window.addEventListener('paste', (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) {
        loadImageFile(file);
        showToast('已从剪贴板载入图片');
        break;
      }
    }
  }
});

['asset-fit', 'asset-threshold', 'asset-brightness', 'asset-contrast', 'asset-invert', 'asset-dither']
  .forEach((id) => {
    const el = $(id);
    el.addEventListener('input', () => {
      if (id === 'asset-threshold') $('asset-threshold-val').textContent = el.value;
      if (id === 'asset-brightness') $('asset-brightness-val').textContent = el.value;
      if (id === 'asset-contrast') $('asset-contrast-val').textContent = Number(el.value).toFixed(1);
      if (el.type === 'range') paintSlider(el);
      renderPreview();
    });
    el.addEventListener('change', renderPreview);
  });

$('asset-zoom').addEventListener('input', () => {
  const el = $('asset-zoom');
  view.zoom = Number(el.value);
  $('asset-zoom-val').textContent = Math.round(view.zoom * 100) + '%';
  paintSlider(el);
  renderPreview();
});

const viewCanvasEl = $('asset-canvas');
viewCanvasEl.addEventListener('pointerdown', (e) => {
  if (!sourceImage) return;
  view.dragging = true;
  view.startX = e.clientX; view.startY = e.clientY;
  viewCanvasEl.classList.add('dragging');
  viewCanvasEl.setPointerCapture(e.pointerId);
});
viewCanvasEl.addEventListener('pointermove', (e) => {
  if (!view.dragging) return;
  const rect = viewCanvasEl.getBoundingClientRect();
  view.offsetX = Math.max(-1, Math.min(1, view.offsetX + (e.clientX - view.startX) / rect.width));
  view.offsetY = Math.max(-1, Math.min(1, view.offsetY + (e.clientY - view.startY) / rect.height));
  view.startX = e.clientX; view.startY = e.clientY;
  renderPreview();
});
viewCanvasEl.addEventListener('pointerup', (e) => {
  view.dragging = false;
  viewCanvasEl.classList.remove('dragging');
  viewCanvasEl.releasePointerCapture(e.pointerId);
});
viewCanvasEl.addEventListener('wheel', (e) => {
  if (!sourceImage) return;
  e.preventDefault();
  view.zoom = Math.max(0.4, Math.min(3.5, view.zoom + (e.deltaY < 0 ? 0.1 : -0.1)));
  $('asset-zoom').value = view.zoom;
  $('asset-zoom-val').textContent = Math.round(view.zoom * 100) + '%';
  paintSlider($('asset-zoom'));
  renderPreview();
}, { passive: false });

$('btn-reset-view').onclick = resetView;

function loadSample() {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 32;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, 256, 32);
  ctx.strokeStyle = '#fff';
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, 255, 31);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 22px "Microsoft YaHei UI", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('HALO', 128, 16);
  c.toBlob((blob) => {
    if (blob) selectedFile = new File([blob], 'halo-sample.png', { type: 'image/png' });
  });
  createImageBitmap(c).then((bitmap) => {
    setSourceImage(bitmap);
    $('asset-info').textContent = '示例图 · 256×32';
    if ($('file-drop-label')) $('file-drop-label').textContent = 'halo-sample.png · 256×32';
    $('asset-progress').textContent = '';
    resetView();
  });
}

$('btn-sample').onclick = loadSample;

$('btn-upload').onclick = async () => {
  const file = selectedFile || $('asset-file').files[0];
  if (!file) { showToast('先选择、拖拽或粘贴一张图片', true); return; }
  const buffer = await file.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  const opts = currentOptions();
  $('btn-upload').disabled = true;
  $('asset-progress').textContent = '处理中…';
  try {
    const result = await api('/api/asset/upload', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name || 'clipboard.png',
        data: btoa(binary),
        fit: opts.fit,
        threshold: opts.threshold,
        invert: opts.invert,
        dither: opts.dither,
        zoom: opts.zoom,
        offset_x: opts.offset_x,
        offset_y: opts.offset_y,
        brightness: opts.brightness,
        contrast: opts.contrast,
        as_standby: $('asset-standby').checked,
        pause_after: $('asset-pause').checked,
      }),
    });
    if (result.error) {
      $('asset-progress').textContent = '失败：' + result.error;
      showToast(result.error, true);
    } else {
      lastAsset = result;
      $('asset-info').textContent = `${file.name || '图片'} · ${result.frames} 帧 · ${(result.bytes / 1024).toFixed(1)} KB`;
      $('asset-progress').textContent = result.animated ? '已提交（动画）' : '已提交';
      showToast('素材已提交上传');
    }
  } catch (e) {
    $('asset-progress').textContent = '上传请求失败';
    showToast('上传请求失败', true);
  } finally {
    $('btn-upload').disabled = false;
    refresh();
  }
};

$('btn-showasset').onclick = async () => {
  const body = lastAsset ? { category: lastAsset.category, option: lastAsset.option } : {};
  const result = await api('/api/asset/display', { method: 'POST', body: JSON.stringify(body) });
  if (result.error) showToast(result.error, true);
  else showToast('已切换到自定义素材');
  refresh();
};

// --------------------------------------------------- visibility-aware polling
let statusTimer = null;
let logsTimer = null;

function startPolling() {
  if (!statusTimer) statusTimer = setInterval(refresh, 700);
  if (!logsTimer) logsTimer = setInterval(refreshLogs, 2500);
}

function stopPolling() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else {
    refresh();
    refreshLogs();
    startPolling();
  }
});

initCustomSelects();
paintAllSliders();
refresh();
refreshLogs();
if (!document.hidden) startPolling();
