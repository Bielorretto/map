/* Accès Urgences — front branché sur l'API (temps de route réels, simulation, optimisation). */
const DUMMY = document.createElement('div');
const $ = s => document.querySelector(s) || DUMMY, $$ = s => document.querySelectorAll(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fr = n => Math.round(n).toLocaleString('fr-FR').replace(/\u202f/g, '\u00a0'); // espace fine absente de la police : espace insécable
const fmtM = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace('.', ',') + ' M' : n >= 1e4 ? fr(n / 1000) + ' k' : fr(n);
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const api = (p, body) => fetch(p, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : undefined)
  .then(r => { if (!r.ok) throw new Error(p + ' : ' + r.status); return r.json(); });
const getJ = p => fetch(p).then(r => r.json());

let INIT, zonesAll, mask, land;
try {
  [INIT, zonesAll, mask, land] = await Promise.all([api('/api/init'), getJ('data/zones.json'), getJ('data/mask.json'), getJ('data/france.json')]);
} catch (e) {
  $('#loading').textContent = 'API indisponible — lancez ./lancer.sh puis rechargez la page.';
  throw e;
}

/* ---------- palettes ---------- */
const P = {
  day: { glowc: '#5B6CFF', bg: '#9EBDFF', fr: '#F8F9FD', sky: '#BFD3F4', horizon: '#F3E9FF', fog: '#DDE6F7', urg: '#E8365B', smur: '#3A74F0', stroke: '#FFFFFF' }
};
const mode = 'day'; document.body.dataset.mode = mode;
const hex2rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
function rampRGB(v, R) { if (v <= R[0][0]) return hex2rgb(R[0][1]); for (let i = 1; i < R.length; i++) { if (v <= R[i][0]) { const t = (v - R[i - 1][0]) / (R[i][0] - R[i - 1][0]), a = hex2rgb(R[i - 1][1]), b = hex2rgb(R[i][1]); return a.map((c, k) => c + (b[k] - c) * t); } } return hex2rgb(R[R.length - 1][1]); }
const ZR = [[0, '#1A9850'], [10, '#4FB45A'], [15, '#8FCF63'], [20, '#D9EF8B'], [24, '#FEE08B'], [28, '#FDAE61'], [32, '#F46D43'], [38, '#D73027'], [45, '#A50026']];
const LUT = new Uint8Array(1201 * 3); for (let i = 0; i <= 1200; i++) { const c = rampRGB(i / 10, ZR); LUT[i * 3] = c[0]; LUT[i * 3 + 1] = c[1]; LUT[i * 3 + 2] = c[2]; }
const zcol = m => { const i = Math.max(0, Math.min(1200, Math.round((m ?? 0) * 10))) * 3; return '#' + [LUT[i], LUT[i + 1], LUT[i + 2]].map(v => v.toString(16).padStart(2, '0')).join(''); };
const fmin = m => m == null ? '–' : m > 60 ? '> 60 min' : Math.round(m) + ' min';
const SANS_ROUTE = [74, 13, 43];
// Couleur de chaque code raster : minutes × 2 (0..250), 254 = hors terre, 255 = sans accès routier.
const CL = new Uint8Array(256 * 4);
for (let c = 0; c < 256; c++) {
  const o = c * 4;
  if (c <= 250) { const i = Math.min(1200, c * 5) * 3; CL[o] = LUT[i]; CL[o + 1] = LUT[i + 1]; CL[o + 2] = LUT[i + 2]; CL[o + 3] = 255; }
  else if (c === 255) { CL[o] = SANS_ROUTE[0]; CL[o + 1] = SANS_ROUTE[1]; CL[o + 2] = SANS_ROUTE[2]; CL[o + 3] = 235; }
}

/* ---------- territoires & zones ---------- */
const SEUIL = INIT.seuil;
const AV = Object.keys(INIT.territoires);
const TINFO = { FR: ['France métropolitaine', '96 dép.'], GP: ['Guadeloupe', '971'], MQ: ['Martinique', '972'], GF: ['Guyane', '973'], RE: ['La Réunion', '974'], YT: ['Mayotte', '976'] };
const TFULL = { FR: 'France métropolitaine', GP: 'Guadeloupe', MQ: 'Martinique', GF: 'Guyane', RE: 'La Réunion', YT: 'Mayotte' };
const TDEP = { GP: '971', MQ: '972', GF: '973', RE: '974', YT: '976' };
const zones = { type: 'FeatureCollection', features: zonesAll.features.filter(f => AV.includes(f.properties.t)) };
const state = { d3: true, terr: null, reg: null, optN: 2, avant: false }; // avant = situation réelle affichée, simulation gardée de côté
const STATS = {}, STATS_SIM = {}; // STATS = affiché ; STATS_SIM = avec les hôpitaux simulés
function applyStats(code, st) {
  zones.features.forEach(f => {
    const p = f.properties; if (p.t !== code) return;
    if (code === 'FR') { const d = st.deps[p.c]; if (d) { p.m = +d.moy.toFixed(1); p.s = +d.part_loin.toFixed(3); p.pl = d.pop_loin; p.p = d.pop; } }
    else { const c = st.communes && st.communes[p.c]; if (c) { p.m = +c.moy.toFixed(1); p.s = c.loin ? 1 : 0; p.pl = c.loin ? c.pop : 0; p.p = c.pop; } }
  });
}
for (const c of AV) { STATS[c] = INIT.territoires[c].stats; applyStats(c, STATS[c]); }
zones.features.forEach(f => { f.properties.m0 = f.properties.m; });

const myR = l => Math.log(Math.tan(Math.PI / 4 + l * Math.PI / 360));
const polysOf = g => g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
const TERR = Object.keys(TINFO).filter(t => AV.includes(t)).map(t => {
  const fs = zones.features.filter(f => f.properties.t === t);
  let a = [180, 90, -180, -90]; fs.forEach(f => polysOf(f.geometry).forEach(p => p[0].forEach(([x, y]) => { a = [Math.min(a[0], x), Math.min(a[1], y), Math.max(a[2], x), Math.max(a[3], y)]; })));
  const px = (a[2] - a[0]) * .06 + .01, py = (a[3] - a[1]) * .06 + .01; a = [a[0] - px, a[1] - py, a[2] + px, a[3] + py];
  return { t, name: TINFO[t][0], code: TINFO[t][1], fs, bbox: a, y0: myR(a[3]), y1: myR(a[1]), hs: INIT.urgences.filter(h => h.terr === t) };
});
function fpath(ctx, g, X, Y) { ctx.beginPath(); polysOf(g).forEach(p => p.forEach(r => { r.forEach(([x, y], j) => { j ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)); }); ctx.closePath(); })); }
$('#omg').innerHTML = TERR.map(t => `<button class="omt${t.t === 'FR' ? ' wide metro' : ''}" data-t="${t.t}" aria-label="Voir ${esc(TFULL[t.t])}"><canvas></canvas><span><b>${esc(t.name)}</b><em>${t.code}</em></span></button>`).join('');

/* ---------- rasters de temps (calculés par l'API sur le réseau routier) ---------- */
const HEATS = await Promise.all(AV.map(async key => {
  const t = INIT.territoires[key], b = t.bbox, W = t.W, H = t.H;
  const codes0 = new Uint8Array(await (await fetch(`/api/raster/${key}.bin`)).arrayBuffer());
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const dv = document.createElement('canvas'); dv.width = W; dv.height = H;
  const y0 = myR(b[3]), y1 = myR(b[1]);
  const Hh = { key, b, W, H, y0, y1, codes0, codes: codes0.slice(), cv, dv, img: cv.getContext('2d').createImageData(W, H),
    X: lon => (lon - b[0]) / (b[2] - b[0]) * W, Y: lat => (y0 - myR(lat)) / (y0 - y1) * H,
    coords: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]], dirty: false };
  return Hh;
}));
const HEAT = Object.fromEntries(HEATS.map(H => [H.key, H]));
const SIMS = [];

function paint(H, x0, y0, x1, y1) {
  x0 = Math.max(0, Math.floor(x0)); y0 = Math.max(0, Math.floor(y0)); x1 = Math.min(H.W, Math.ceil(x1)); y1 = Math.min(H.H, Math.ceil(y1));
  if (x1 <= x0 || y1 <= y0) return;
  const d = H.img.data, W = H.W, sims = state.avant ? [] : SIMS.filter(s => s.patch && s.patch.terr === H.key);
  for (let r = y0; r < y1; r++) for (let c = x0; c < x1; c++) {
    const i = r * W + c; let v = H.codes0[i];
    if (v !== 254) for (const s of sims) {
      const p = s.patch; if (c < p.x || c >= p.x + p.w || r < p.y || r >= p.y + p.h) continue;
      if (s.rev != null) { const dx = c - s.px, dy = r - s.py; if (dx * dx + dy * dy > s.rev * s.rev) continue; }
      const pv = p.data[(r - p.y) * p.w + (c - p.x)]; if (pv < v) v = pv;
    }
    H.codes[i] = v; const o = i * 4, k = v * 4; d[o] = CL[k]; d[o + 1] = CL[k + 1]; d[o + 2] = CL[k + 2]; d[o + 3] = CL[k + 3];
  }
  H.cv.getContext('2d').putImageData(H.img, 0, 0, x0, y0, x1 - x0, y1 - y0);
  dispRect(H, x0, y0, x1, y1);
}
function regionPath(H, ctx) {
  const fs = state.reg ? zones.features.filter(z => z.properties.r === state.reg && z.properties.t === H.key) : [];
  ctx.beginPath(); fs.forEach(f => polysOf(f.geometry).forEach(p => p.forEach(r => { r.forEach(([a, b], j) => j ? ctx.lineTo(H.X(a), H.Y(b)) : ctx.moveTo(H.X(a), H.Y(b))); ctx.closePath(); }))); return fs.length;
}
function dispRect(H, x0, y0, x1, y1) {
  const g = H.dv.getContext('2d'); g.save(); g.beginPath(); g.rect(x0, y0, x1 - x0, y1 - y0); g.clip(); g.clearRect(x0, y0, x1 - x0, y1 - y0);
  if (!state.reg || regionPath(H, g)) { g.drawImage(H.cv, 0, 0); if (state.reg) { g.globalCompositeOperation = 'destination-in'; regionPath(H, g); g.fill('nonzero'); } }
  g.restore(); H.dirty = true;
}
let pauseT = 0;
function pushHeat() {
  if (!window.__mapReady) return;
  HEATS.forEach(H => { if (!H.dirty) return; const s = map.getSource('heat-' + H.key); if (s && s.play) s.play(); });
  clearTimeout(pauseT); pauseT = setTimeout(() => HEATS.forEach(H => { const s = map.getSource('heat-' + H.key); if (H.dirty && s && s.pause) s.pause(); H.dirty = false; }), 300);
}
HEATS.forEach(H => paint(H, 0, 0, H.W, H.H));
function gainSimule(lng, lat) { // minutes avec la simulation, seulement si un hôpital simulé améliore ce point
  for (const H of HEATS) {
    const c = Math.floor(H.X(lng)), r = Math.floor(H.Y(lat)); if (c < 0 || r < 0 || c >= H.W || r >= H.H) continue;
    const i = r * H.W + c; if (H.codes0[i] === 254) continue; return H.codes[i] < H.codes0[i] ? H.codes[i] / 2 : null;
  }
  return null;
}
function minutesAt(lng, lat) {
  for (const H of HEATS) {
    const c = Math.floor(H.X(lng)), r = Math.floor(H.Y(lat)); if (c < 0 || r < 0 || c >= H.W || r >= H.H) continue;
    const v = H.codes[r * H.W + c]; if (v === 254) continue; return v === 255 ? Infinity : v / 2;
  }
  return null;
}

/* ---------- indicateurs ---------- */
function totals() { let pop = 0, loin = 0; for (const c of AV) { pop += STATS[c].pop; loin += STATS[c].pop_loin; } return { pop, loin }; }
function refreshPanels() {
  const { pop, loin } = totals();
  $('#kCov').textContent = ((1 - loin / pop) * 100).toFixed(1).replace('.', ',') + ' %'; $('#kOut').textContent = fmtM(loin); $('#kS').textContent = SEUIL;
  $('#kSites').textContent = INIT.urgences.length; // services réels FINESS seulement : les hôpitaux simulés n'y sont jamais ajoutés $('#kSmur').textContent = INIT.smur.length;
  $('#legBar').style.background = `linear-gradient(90deg,${ZR.map(r => r[1] + ' ' + (r[0] / 45 * 100).toFixed(1) + '%').join(',')})`;
  $('#legTitle').textContent = 'TEMPS D’ACCÈS AUX URGENCES · MIN';
  $('#legTicks').innerHTML = '<span>0</span><span>15</span><span>30</span><span>45+</span>';
}
let commitSeq = 0;
async function commit() {
  const seq = ++commitSeq;
  const sites = SIMS.map(s => { const ll = s.mk.getLngLat(); return { lon: ll.lng, lat: ll.lat }; });
  let r = { territoires: {}, gain: 0 };
  if (sites.length) { try { r = await api('/api/simulation', { sites }); } catch (e) { console.error(e); return; } }
  if (seq !== commitSeq) return;
  for (const c of AV) { STATS_SIM[c] = r.territoires[c] || INIT.territoires[c].stats; if (!state.avant) { STATS[c] = STATS_SIM[c]; applyStats(c, STATS[c]); } }
  lastGain = r.gain;
  if (map.getSource('zones')) map.getSource('zones').setData(zones);
  refreshPanels(); drawTiles(); if (state.reg) selectRegion(state.reg, null, true); showToast(r.gain);
}
let lastGain = 0;
const aaHTML = () => `<div class="seg aa" role="group" aria-label="Comparer avant et après la simulation"><button data-v="avant" class="${state.avant ? 'on' : ''}" aria-pressed="${state.avant}">Avant</button><button data-v="apres" class="${state.avant ? '' : 'on'}" aria-pressed="${!state.avant}">Après</button></div>`;
function showToast(g = lastGain) { // reste affiché tant qu'il y a des hôpitaux simulés : c'est là qu'on compare avant / après
  const t = $('#toast'); document.body.classList.toggle('sim-on', SIMS.length > 0); if (!SIMS.length) { t.classList.remove('show'); return; }
  const n = SIMS.length, h = `${n} hôpita${n > 1 ? 'ux' : 'l'} simulé${n > 1 ? 's' : ''}`;
  t.innerHTML = (state.avant
    ? `<b class="big av">Avant</b><span>situation réelle, sans les ${h}<br>(+${fr(g)} habitants sous ${SEUIL} min après)</span>`
    : `<b class="big">+${fr(g)}</b><span>habitants passent sous ${SEUIL} min de route<br>grâce à ${h}</span>`) + aaHTML() + '<button id="undo">Tout retirer</button>';
  t.classList.add('show'); $('#undo').onclick = removeAll;
}
/* avant / après : bascule l'affichage (carte, indicateurs, zones) entre la situation réelle et la simulation */
function setAvant(on) {
  on = !!on && SIMS.length > 0;
  if (state.avant !== on) {
    state.avant = on;
    SIMS.forEach(s => { s.mk.getElement().style.display = on ? 'none' : ''; const p = s.mk.getPopup(); if (p && p.isOpen()) p.remove(); });
    repaintRects(...SIMS.map(s => rectOf(s.patch)));
    for (const c of AV) { STATS[c] = on ? INIT.territoires[c].stats : (STATS_SIM[c] || INIT.territoires[c].stats); applyStats(c, STATS[c]); }
    if (map.getSource('zones')) map.getSource('zones').setData(zones);
    refreshPanels(); drawTiles(); if (state.reg) selectRegion(state.reg, null, true);
  }
  showToast();
  $$('.ba .aa').forEach(el => { el.outerHTML = aaHTML(); });
}
document.addEventListener('click', e => { const b = e.target.closest('.aa button'); if (b) setAvant(b.dataset.v === 'avant'); });
function drawTiles() {
  const p = P[mode], dpr = Math.min(2, devicePixelRatio || 1);
  $$('.omt').forEach(btn => {
    const t = TERR.find(x => x.t === btn.dataset.t), cv = btn.querySelector('canvas'), w = cv.clientWidth || 100, h = cv.clientHeight || 44;
    cv.width = w * dpr; cv.height = h * dpr; const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const mw = (t.bbox[2] - t.bbox[0]) * Math.PI / 180, mh = t.y0 - t.y1, k = Math.min((w - 6) / mw, (h - 6) / mh), ox = (w - mw * k) / 2, oy = (h - mh * k) / 2;
    const X = x => ox + (x - t.bbox[0]) * Math.PI / 180 * k, Y = y => oy + (t.y0 - myR(y)) * k;
    ctx.fillStyle = p.fr; t.fs.forEach(f => { fpath(ctx, f.geometry, X, Y); ctx.fill('evenodd'); });
    const H = HEAT[t.t]; if (H) { ctx.save(); ctx.globalCompositeOperation = 'source-atop'; ctx.imageSmoothingQuality = 'high'; ctx.drawImage(H.cv, X(H.b[0]), Y(H.b[3]), X(H.b[2]) - X(H.b[0]), Y(H.b[1]) - Y(H.b[3])); ctx.restore(); }
    ctx.lineWidth = t.t === 'FR' ? .3 : .55; ctx.strokeStyle = 'rgba(255,255,255,.9)'; t.fs.forEach(f => { fpath(ctx, f.geometry, X, Y); ctx.stroke(); });
    if (t.t !== 'FR') t.hs.forEach(hh => { ctx.beginPath(); ctx.arc(X(hh.lon), Y(hh.lat), 2.8, 0, 7); ctx.fillStyle = p.urg; ctx.fill(); ctx.lineWidth = 1.2; ctx.strokeStyle = '#fff'; ctx.stroke(); });
    SIMS.forEach(s => { const ll = s.mk.getLngLat(); if (ll.lng < t.bbox[0] || ll.lng > t.bbox[2] || ll.lat < t.bbox[1] || ll.lat > t.bbox[3]) return; ctx.beginPath(); ctx.arc(X(ll.lng), Y(ll.lat), 3.4, 0, 7); ctx.fillStyle = '#1D4A7C'; ctx.fill(); ctx.lineWidth = 1.4; ctx.strokeStyle = '#fff'; ctx.stroke(); });
  });
}
function flyTerr(t, d) {
  if (t.t === 'FR') { map.flyTo({ ...homeCam(), duration: d || 2200, essential: true }); return; }
  const c = camFor(t.bbox, { top: 120, bottom: 110, left: desk() ? 300 : 30, right: desk() ? padR(90) : 30 });
  map.flyTo({ ...c, zoom: Math.min(c.zoom, 11) - (state.d3 ? .15 : 0), pitch: state.d3 ? 50 : 0, bearing: 0, duration: d || 2200, essential: true });
}
function selectTerr(t) {
  if (t && t.t === 'FR') t = null; state.terr = t; document.body.classList.toggle('terr', !!t);
  $$('.omt').forEach(b => b.classList.toggle('on', t ? b.dataset.t === t.t : b.dataset.t === 'FR')); $('#where').textContent = t ? TFULL[t.t] + ' · ' + t.code : 'France métropolitaine et outre-mer';
}
$('#omg').addEventListener('click', e => {
  const b = e.target.closest('.omt'); if (!b) return; clearRegion(); const t = TERR.find(x => x.t === b.dataset.t);
  if (state.terr === t) { selectTerr(null); map.flyTo({ ...homeCam(), duration: 2200 }); return; } selectTerr(t); flyTerr(t);
});
$('#back').onclick = () => { clearRegion(); selectTerr(null); map.flyTo({ ...homeCam(), duration: 2200, essential: true }); };
$('#omCollapse').onclick = () => { const c = $('#om').classList.toggle('closed'); $('#omCollapse').setAttribute('aria-expanded', String(!c)); if (!c) drawTiles(); };

/* ---------- carte ---------- */
$('#loading').remove();
if ((INIT.en_cours || []).includes('FR')) {
  const pill = document.createElement('div'); pill.className = 'fl glass hint show'; pill.id = 'pending'; pill.style.top = '150px';
  pill.textContent = 'Métropole : temps de route en cours de calcul sur le réseau OSM — les DROM sont prêts (tuiles Outre-mer)';
  $('.stage').appendChild(pill);
}
const box = $('#map');
const fitZoom = () => { const w = box.clientWidth, h = box.clientHeight; return Math.max(4.1, Math.min(6.3, Math.min(Math.log2(0.66 * w * 360 / (15 * 512)), Math.log2(0.95 * h * 360 / (14 * 512))))); };
const desk = () => box.clientWidth > 900;
const padR = b => b + (desk() && document.body.classList.contains('chat-on') ? 390 : 0); // panneau de l'assistant ouvert
const homeCam = () => ({ center: [3.0, state.d3 ? 45.55 : 46.2], zoom: fitZoom() + (state.d3 ? 0.12 : 0.06), pitch: state.d3 ? 48 : 0, bearing: state.d3 ? -12 : 0, padding: { left: desk() ? 270 : 0, top: desk() ? 70 : 0, right: desk() ? padR(60) : 0, bottom: desk() ? 80 : 0 } });
const map = new maplibregl.Map({ container: 'map', attributionControl: false, maxPitch: 70, antialias: true,
  style: 'https://tiles.openfreemap.org/styles/liberty', center: [2.5, 46.5], zoom: fitZoom() - 0.5, pitch: 0, bearing: 0, maxZoom: 17, minZoom: 3.6 });
window.__map = map;
const TYPES = { SU: 'Structure des urgences', AMU: 'Antenne de médecine d’urgence', SUP: 'Urgences pédiatriques', SMUR: 'SMUR', SMURP: 'SMUR pédiatrique', ASMUR: 'Antenne SMUR', SAMU: 'SAMU' };
const ptFC = (list, dx = 0, dy = 0) => ({ type: 'FeatureCollection', features: list.map(s => ({ type: 'Feature', properties: { name: s.nom, ville: s.ville, types: s.types.join(','), finess: s.finess, pos: s.position || '' }, geometry: { type: 'Point', coordinates: [s.lon + dx, s.lat + dy] } })) });
function pinImg(fill) {
  const s = 64, c = document.createElement('canvas'); c.width = s; c.height = s * 1.3; const x = c.getContext('2d');
  x.translate(s / 2, s * .46); x.beginPath(); x.arc(0, 0, s * .36, Math.PI * .8, Math.PI * .2); x.lineTo(0, s * .8); x.closePath();
  x.shadowColor = 'rgba(0,0,0,.4)'; x.shadowBlur = 8; x.shadowOffsetY = 3; x.fillStyle = fill; x.fill(); x.shadowColor = 'transparent';
  x.lineWidth = 4; x.strokeStyle = '#fff'; x.stroke(); x.lineCap = 'round'; x.lineWidth = 6; x.beginPath(); x.moveTo(0, -10); x.lineTo(0, 10); x.moveTo(-10, 0); x.lineTo(10, 0); x.stroke();
  return x.getImageData(0, 0, c.width, c.height);
}
function addOverlays() {
  const p = P[mode], L = map.getStyle().layers;
  const before = (L.find(l => /^(waterway|tunnel|road|highway|bridge|building|aeroway|railway)/.test(l.id)) || L.find(l => l.type === 'symbol') || {}).id;
  L.forEach(l => { if (l.type === 'symbol' && l.layout && l.layout['text-field'] && JSON.stringify(l.layout['text-field']).includes('name')) map.setLayoutProperty(l.id, 'text-field', ['coalesce', ['get', 'name:fr'], ['get', 'name']]); });
  if (!map.hasImage('pin-urg')) map.addImage('pin-urg', pinImg(p.urg), { pixelRatio: 2 });
  map.addSource('land', { type: 'geojson', data: land });
  map.addSource('mask', { type: 'geojson', data: mask });
  map.addSource('zones', { type: 'geojson', data: zones, promoteId: 'c' });
  map.addSource('urg', { type: 'geojson', data: ptFC(INIT.urgences) });
  map.addSource('smur', { type: 'geojson', data: ptFC(INIT.smur, 0.012, -0.008) });
  HEATS.forEach(H => { map.addSource('heat-' + H.key, { type: 'canvas', canvas: H.dv, coordinates: H.coords, animate: false }); map.addLayer({ id: 'heat-' + H.key, type: 'raster', source: 'heat-' + H.key, paint: { 'raster-opacity': ['interpolate', ['linear'], ['zoom'], 4, .85, 9, .66, 13, .4, 16, .25], 'raster-fade-duration': 0, 'raster-resampling': 'linear' } }, before); });
  map.addLayer({ id: 'zones', type: 'fill', source: 'zones', paint: { 'fill-color': '#000000', 'fill-opacity': 0 } }, before);
  map.addLayer({ id: 'zones-dim', type: 'fill', source: 'zones', filter: ['==', ['get', 'c'], '__'], paint: { 'fill-color': '#FFFFFF', 'fill-opacity': .45 } }, before);
  map.addLayer({ id: 'zones-line', type: 'line', source: 'zones', paint: { 'line-color': '#FFFFFF', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, .7, 9, 1.8], 'line-opacity': .9 } }, before);
  map.addLayer({ id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': p.bg, 'fill-opacity': 1 } });
  map.addLayer({ id: 'land-glow', type: 'line', source: 'land', paint: { 'line-color': p.glowc, 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 10, 9, 22], 'line-blur': ['interpolate', ['linear'], ['zoom'], 4, 9, 9, 18], 'line-opacity': ['interpolate', ['linear'], ['zoom'], 4, .55, 7, .3, 9, 0] } });
  map.addLayer({ id: 'zones-hl', type: 'line', source: 'zones', filter: ['==', ['get', 'c'], ''], paint: { 'line-color': '#1A2047', 'line-width': 2.4 } });
  map.addLayer({ id: 'zones-sel-glow', type: 'line', source: 'zones', filter: ['==', ['get', 'c'], ''], paint: { 'line-color': p.glowc, 'line-width': 12, 'line-blur': 8, 'line-opacity': .7 } });
  map.addLayer({ id: 'zones-sel', type: 'line', source: 'zones', filter: ['==', ['get', 'c'], ''], paint: { 'line-color': p.glowc, 'line-width': 3.2 } });
  map.addLayer({ id: 'zones-focus', type: 'line', source: 'zones', filter: ['==', ['get', 'c'], ''], paint: { 'line-color': '#1A2047', 'line-width': 3.4 } });
  map.addLayer({ id: 'smur', type: 'circle', source: 'smur', paint: { 'circle-color': p.smur, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 2, 8, 4.5, 11, 7], 'circle-stroke-color': p.stroke, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 4, .8, 9, 1.8], 'circle-pitch-alignment': 'viewport' } });
  map.addLayer({ id: 'urg-halo', type: 'circle', source: 'urg', paint: { 'circle-color': p.urg, 'circle-radius': 6, 'circle-opacity': .3, 'circle-blur': .4, 'circle-pitch-alignment': 'viewport' } });
  map.addLayer({ id: 'urg', type: 'circle', source: 'urg', paint: { 'circle-color': p.urg, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 2.6, 8, 5.5, 11, 8], 'circle-stroke-color': p.stroke, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 4, 1, 9, 2], 'circle-pitch-alignment': 'viewport' } });
  map.addLayer({ id: 'urg-pin', type: 'symbol', source: 'urg', minzoom: 8.5, layout: { 'icon-image': 'pin-urg', 'icon-size': ['interpolate', ['linear'], ['zoom'], 8.5, .6, 11, .9], 'icon-anchor': 'bottom', 'icon-allow-overlap': true } });
  try { map.setLight({ anchor: 'viewport', color: '#ffffff', intensity: .38, position: [1.3, 200, 38] }); } catch (e) { }
  try { if (map.setSky) map.setSky({ 'sky-color': p.sky, 'horizon-color': p.horizon, 'fog-color': p.fog, 'sky-horizon-blend': .7, 'horizon-fog-blend': .6, 'fog-ground-blend': .35, 'atmosphere-blend': 0 }); } catch (e) { }
  window.__mapReady = true;
}
map.on('sourcedata', e => { if (e.isSourceLoaded && e.tile) map.triggerRepaint(); });
map.on('style.load', () => {
  addOverlays(); bindMap(); refreshPanels(); drawTiles(); selectTerr(null);
  if (reduce) map.jumpTo(homeCam()); else { map.easeTo({ ...homeCam(), duration: 2600, easing: t => 1 - Math.pow(1 - t, 3) }); requestAnimationFrame(pulse); }
});

/* ---------- région isolée ---------- */
function zCenter(f) { let sx = 0, sy = 0, n = 0, big = null, bl = 0; polysOf(f.geometry).forEach(p => { if (p[0].length > bl) { bl = p[0].length; big = p[0]; } }); big.forEach(([x, y]) => { sx += x; sy += y; n++; }); return [sx / n, sy / n]; }
function zBox(f) { let a = [180, 90, -180, -90]; polysOf(f.geometry).forEach(p => p[0].forEach(([x, y]) => { a = [Math.min(a[0], x), Math.min(a[1], y), Math.max(a[2], x), Math.max(a[3], y)]; })); return a; }
function boxOf(fs) { let a = [180, 90, -180, -90]; fs.forEach(f => { const b = zBox(f); a = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]; }); return a; }
function camFor(bb, P) { // cadrage calculé à la main : cameraForBounds ajoute la marge courante de la carte à P
  const W = Math.max(160, box.clientWidth - P.left - P.right), H = Math.max(160, box.clientHeight - P.top - P.bottom), my = l => myR(l) / (2 * Math.PI);
  const dx = Math.max(1e-6, (bb[2] - bb[0]) / 360), dy = Math.max(1e-6, my(bb[3]) - my(bb[1])), cy = (my(bb[1]) + my(bb[3])) / 2;
  return { center: [(bb[0] + bb[2]) / 2, (2 * Math.atan(Math.exp(cy * 2 * Math.PI)) - Math.PI / 2) * 180 / Math.PI], zoom: Math.log2(Math.min(W / (512 * dx), H / (512 * dy))), padding: P };
}
function flyBox(bb, maxZ) {
  const c = camFor(bb, { top: 100, bottom: 40, left: desk() ? 280 : 30, right: desk() ? padR(340) : 30 });
  map.flyTo({ ...c, zoom: Math.min(c.zoom, maxZ || 11), pitch: state.d3 ? 40 : 0, bearing: state.d3 ? -6 : 0, duration: 1800, essential: true });
}
let regMarkers = [];
function regFilters() {
  const r = state.reg, F = r ? ['==', ['get', 'r'], r] : null;
  if (map.getLayer('zones')) map.setFilter('zones', F);
  if (map.getLayer('zones-dim')) map.setFilter('zones-dim', r ? ['!=', ['get', 'r'], r] : ['==', ['get', 'c'], '__']);
  ['zones-sel', 'zones-sel-glow'].forEach(id => map.getLayer(id) && map.setFilter(id, r ? F : ['==', ['get', 'c'], '']));
  if (!r && map.getLayer('zones-focus')) map.setFilter('zones-focus', ['==', ['get', 'c'], '']);
  HEATS.forEach(H => dispRect(H, 0, 0, H.W, H.H)); pushHeat();
}
function clearRegion() { state.reg = null; regFilters(); regMarkers.forEach(m => m.remove()); regMarkers = []; $('#zcard').classList.remove('show'); }
const regionDeps = fs => fs[0].properties.t === 'FR' ? fs.map(f => f.properties.c) : [TDEP[fs[0].properties.t]];
function selectRegion(r, focus, still) {
  const fs = zones.features.filter(z => z.properties.r === r); if (!fs.length) return;
  const prevRes = still ? $('#zRes').innerHTML : '';
  state.reg = r; regFilters();
  regMarkers.forEach(m => m.remove()); const compact = fs.length > 14;
  regMarkers = fs.map(f => {
    const pr = f.properties, el = document.createElement('div'); el.className = 'zlab';
    el.innerHTML = `<i style="background:${zcol(pr.m)}"></i><b>${fmin(pr.m)}</b>${compact ? '' : `<span>${esc(pr.n)}</span>`}`;
    return new maplibregl.Marker({ element: el, anchor: 'center' }).setLngLat(zCenter(f)).addTo(map);
  });
  const pop = fs.reduce((s, z) => s + (z.properties.p || 0), 0), loin = fs.reduce((s, z) => s + (z.properties.pl || 0), 0);
  const mean = pop ? fs.reduce((s, z) => s + (z.properties.p || 0) * z.properties.m, 0) / pop : 0;
  const sorted = fs.slice().sort((a, b) => b.properties.m - a.properties.m);
  const z = $('#zcard'), N = state.optN;
  z.innerHTML = `<div class="zh"><div><b>${esc(r)}</b><span>${fs.length} zone${fs.length > 1 ? 's' : ''} · cliquez une zone pour la détailler</span></div><button class="ib" id="zX" aria-label="Fermer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <div class="zt"><i style="background:${zcol(mean)}"></i>${fmin(mean)}</div>
    <p>temps moyen de route jusqu’aux urgences (pondéré par la population)${loin ? ` · <b style="color:var(--ink)">${fr(loin)}</b> hab. à ${SEUIL} min ou plus` : ''}</p>
    <h3>Optimiser les implantations</h3>
    <div class="zopt"><div class="stp"><button id="zM" aria-label="Un hôpital de moins">−</button><b id="zN">${N}</b><button id="zP" aria-label="Un hôpital de plus">+</button></div><button class="go" id="zGo">Placer au mieux</button></div>
    <button class="zq" id="zLs">Combien d’hôpitaux pour passer toute la zone sous ${SEUIL} min ?</button>
    <div class="zres" id="zRes">${prevRes}</div>
    <h3>Temps d’accès par zone</h3>
    ${sorted.map(f => `<button class="prow" data-c="${esc(f.properties.c)}"><span class="rk" style="background:${zcol(f.properties.m)}"></span><span class="pn">${esc(f.properties.n)} <span style="color:var(--muted)">${esc(f.properties.c)}</span></span><span class="pv" style="width:auto;white-space:nowrap">${fmin(f.properties.m)}</span></button>`).join('')}
    <div class="row"><button id="zAll">Voir toute la France</button></div>`;
  if (!still) { z.classList.remove('show'); void z.offsetWidth; z.scrollTop = 0; } z.classList.add('show');
  $('#zX').onclick = clearRegion;
  $('#zAll').onclick = () => { clearRegion(); selectTerr(null); map.flyTo({ ...homeCam(), duration: 1800, essential: true }); };
  $$('#zcard .prow').forEach(b => b.onclick = () => focusZone(b.dataset.c));
  const setN = n => { state.optN = Math.max(1, Math.min(10, n)); $('#zN').textContent = state.optN;  };
  $('#zM').onclick = () => setN(state.optN - 1); $('#zP').onclick = () => setN(state.optN + 1);
  $('#zGo').onclick = () => optimiser(regionDeps(fs), 'mclp');
  $('#zLs').onclick = () => optimiser(regionDeps(fs), 'lscp');
  if (still) { } else if (focus) focusZone(focus); else flyBox(boxOf(fs), fs.length > 1 ? 10 : 11);
}
function focusZone(c, fly = true) {
  const f = zones.features.find(z => z.properties.c === c); if (!f) return;
  map.getLayer('zones-focus') && map.setFilter('zones-focus', ['==', ['get', 'c'], c]);
  $$('#zcard .prow').forEach(b => b.classList.toggle('on', b.dataset.c === c));
  const row = document.querySelector(`#zcard .prow[data-c="${CSS.escape(c)}"]`); if (row) row.scrollIntoView({ block: 'nearest' });
  if (fly) flyBox(zBox(f), 11.5);
}
function pickZone(pr) { if (state.reg === pr.r) focusZone(pr.c); else { selectTerr(null); selectRegion(pr.r); } }

/* ---------- classement des zones prioritaires ---------- */
function priorites() {
  clearRegion();
  const L = zones.features.filter(f => f.properties.t === 'FR').map(f => ({ n: f.properties.n, c: f.properties.c, r: f.properties.r, pl: f.properties.pl || 0, p: f.properties.p || 1, m: f.properties.m }));
  TERR.filter(t => t.t !== 'FR').forEach(t => { const pl = t.fs.reduce((s, f) => s + (f.properties.pl || 0), 0), p = t.fs.reduce((s, f) => s + (f.properties.p || 0), 0); L.push({ n: TFULL[t.t], c: t.code, r: TFULL[t.t], terr: t, pl, p: p || 1, m: rmean(TFULL[t.t]) }); });
  const top = L.filter(z => z.pl > 0).sort((a, b) => b.pl - a.pl).slice(0, 20), max = top.length ? top[0].pl : 1;
  const z = $('#zcard');
  z.innerHTML = `<div class="zh"><div><b>Zones prioritaires</b><span>habitants à ${SEUIL} min ou plus des urgences</span></div><button class="ib" id="zX" aria-label="Fermer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <p style="margin-top:8px">Classement par population concernée. Cliquez une zone pour l’isoler et optimiser ses implantations.</p>
    ${top.map((x, i) => `<button class="prow" data-i="${i}"><span class="rk n" style="background:${zcol(Math.max(30, 45 - i))}">${i + 1}</span><span class="pn">${esc(x.n)} <span style="color:var(--muted)">${Math.round(x.pl / x.p * 100)} %</span></span><span class="pb"><span style="width:${(x.pl / max * 100).toFixed(0)}%;background:${zcol(Math.max(30, 45 - i))}"></span></span><span class="pv">${fmtM(x.pl)}</span></button>`).join('')}`;
  z.classList.remove('show'); void z.offsetWidth; z.scrollTop = 0; z.classList.add('show');
  $('#zX').onclick = () => z.classList.remove('show');
  $$('#zcard .prow').forEach(b => b.onclick = () => { const x = top[+b.dataset.i]; if (x.terr) { selectTerr(x.terr); selectRegion(x.r); } else { selectTerr(null); selectRegion(x.r, x.c); } });
}
{ const k = $('#kOut').closest('.kpi'); k.setAttribute('role', 'button'); k.tabIndex = 0; k.title = 'Voir les zones prioritaires'; k.style.cursor = 'pointer';
  k.onclick = priorites; k.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); priorites(); } }; }

/* ---------- optimisation (MCLP / LSCP sur le réseau routier) ---------- */
async function optimiser(deps, modeOpt) {
  const res = $('#zRes'), btns = [$('#zGo'), $('#zLs')];
  btns.forEach(b => b.disabled = true);
  res.innerHTML = 'Calcul des trajets réels depuis les sites candidats…';
  const sites = SIMS.map(s => { const ll = s.mk.getLngLat(); return { lon: ll.lng, lat: ll.lat }; });
  let r;
  try { r = await api('/api/optimisation', { deps, mode: modeOpt, p: state.optN, sites }); }
  catch (e) { res.innerHTML = 'Erreur de calcul : ' + esc(e.message); btns.forEach(b => b.disabled = false); return; }
  btns.forEach(b => b.disabled = false);
  const n = r.sites.length, gain = r.sites.reduce((s, x) => s + x.gain, 0), horsPortee = Math.max(0, r.pop_loin - r.pop_couvrable);
  const meta = `<br><span>${r.statut === 'optimal' ? 'Solution optimale' : 'Meilleure solution trouvée'} · ${r.candidats || 0} sites candidats · ${fr(r.ms)} ms</span>`;
  if (!n) { res.innerHTML = r.pop_loin ? `Aucun site ne peut ramener ces ${fr(r.pop_loin)} habitants sous ${SEUIL} min (zones sans route).` : `Toute la population est déjà à moins de ${SEUIL} min.`; return; }
  if (modeOpt === 'lscp') {
    res.innerHTML = `Il faut <b class="big">${n}</b> nouvel${n > 1 ? 's' : ''} hôpita${n > 1 ? 'ux' : 'l'} pour ramener sous ${SEUIL} min les <b>${fr(r.pop_couvrable)}</b> habitants qui peuvent l’être.` +
      (horsPortee > 0 ? ` <b>${fr(horsPortee)}</b> restent hors de portée (aucun site possible à moins de ${SEUIL} min par la route).` : '') + meta + `<br><button id="zPlace">Les placer sur la carte</button>`;
    $('#zPlace').onclick = () => placer(r.sites);
  } else {
    res.innerHTML = `<b class="big">+${fr(gain)}</b> habitants sous ${SEUIL} min avec ${n} hôpita${n > 1 ? 'ux' : 'l'} bien placé${n > 1 ? 's' : ''}` +
      (r.pop_loin - gain > 0 ? ` · ${fr(r.pop_loin - gain)} restent au-delà` : '') + meta;
    placer(r.sites);
  }
}
async function placer(list) {
  const sims = list.map(s => dropHospital({ lng: s.lon, lat: s.lat }, { noCommit: true, label: s.gain != null ? '+' + fmtM(s.gain) : null }));
  await Promise.all(sims.map(s => s.ready));
  commit();
  if (list.length) {
    let a = [180, 90, -180, -90]; list.forEach(s => { a = [Math.min(a[0], s.lon), Math.min(a[1], s.lat), Math.max(a[2], s.lon), Math.max(a[3], s.lat)]; });
    if (!state.reg) flyBox([a[0] - .3, a[1] - .2, a[2] + .3, a[3] + .2], 9);
  }
}

/* ---------- hôpital à glisser : impact calculé par l'API en temps réel ---------- */
const HICON = document.querySelector('#hdrag .hi').innerHTML;
let ghost = null;
function moveGhost(e) { ghost.style.left = e.clientX + 'px'; ghost.style.top = e.clientY + 'px'; }
const b64 = s => { const raw = atob(s), u = new Uint8Array(raw.length); for (let i = 0; i < raw.length; i++) u[i] = raw.charCodeAt(i); return u; };
function rectOf(p) { return p ? [p.terr, p.x, p.y, p.x + p.w, p.y + p.h] : null; }
function repaintRects(...rects) {
  rects.filter(Boolean).forEach(([t, x0, y0, x1, y1]) => HEAT[t] && paint(HEAT[t], x0, y0, x1, y1)); pushHeat();
}
async function fetchImpact(s) {
  if (s.busy) { s.again = true; return; }
  s.busy = true;
  try {
    const ll = s.mk.getLngLat();
    const r = await api('/api/impact', { lon: ll.lng, lat: ll.lat });
    if (!SIMS.includes(s)) return;
    const old = rectOf(s.patch);
    s.patch = r.terr && r.data ? { terr: r.terr, x: r.x, y: r.y, w: r.w, h: r.h, data: b64(r.data) } : null;
    if (s.patch) { const H = HEAT[r.terr]; s.px = H.X(ll.lng); s.py = H.Y(ll.lat); }
    s.offroad = !r.terr;
    s.mk.getElement().style.opacity = s.offroad ? .55 : 1;
    if (s.rev == null) repaintRects(old, rectOf(s.patch));
  } catch (e) { console.error(e); }
  finally { s.busy = false; if (s.again) { s.again = false; await fetchImpact(s); } }
}
function wave(s) {
  return new Promise(res => {
    const p = s.patch; if (!p || reduce) { s.rev = null; repaintRects(rectOf(p)); res(); return; }
    const R = Math.hypot(Math.max(s.px - p.x, p.x + p.w - s.px), Math.max(s.py - p.y, p.y + p.h - s.py)), t0 = performance.now(), dur = 1100;
    const step = now => {
      if (!SIMS.includes(s)) { res(); return; }
      const u = Math.min(1, (now - t0) / dur); s.rev = u < 1 ? R * (1 - Math.pow(1 - u, 3)) : null;
      repaintRects(rectOf(p)); if (u < 1) requestAnimationFrame(step); else res();
    };
    requestAnimationFrame(step);
  });
}
function removeSim(s) { setAvant(false); const i = SIMS.indexOf(s); if (i < 0) return; SIMS.splice(i, 1); s.mk.remove(); repaintRects(rectOf(s.patch)); commit(); }
function removeAll() { setAvant(false); const old = SIMS.splice(0); old.forEach(s => s.mk.remove()); repaintRects(...old.map(s => rectOf(s.patch))); commit(); }
function removeIdx(ix) { setAvant(false); const old = SIMS.filter((s, i) => ix.includes(i)); old.forEach(s => { SIMS.splice(SIMS.indexOf(s), 1); s.mk.remove(); }); repaintRects(...old.map(s => rectOf(s.patch))); commit(); }
function dropHospital(ll, opt = {}) {
  setAvant(false);
  const el = document.createElement('div'); el.className = 'hmk'; el.innerHTML = '<i></i><i></i><b>' + HICON + '</b>' + (opt.label ? `<em>${esc(opt.label)}</em>` : '');
  const m = new maplibregl.Marker({ element: el, draggable: true, anchor: 'bottom' }).setLngLat(ll).addTo(map);
  const s = { mk: m, patch: null, rev: 0 }; SIMS.push(s);
  const pop = new maplibregl.Popup({ closeButton: false, offset: 52, maxWidth: '240px' }).setHTML('<b>Nouvel hôpital</b><br><span style="color:var(--muted)">Déplacez-le : les temps de route sont recalculés en direct.</span><br><button class="hdel">Retirer</button>');
  m.setPopup(pop);
  pop.on('open', () => { const b = pop.getElement() && pop.getElement().querySelector('.hdel'); if (b) b.onclick = () => removeSim(s); });
  m.on('dragstart', () => { if (pop.isOpen()) pop.remove(); s.rev = null; const em = el.querySelector('em'); if (em) em.remove(); });
  m.on('drag', () => fetchImpact(s));
  m.on('dragend', async () => { await fetchImpact(s); commit(); });
  s.ready = (async () => { s.rev = 0; await fetchImpact(s); await wave(s); s.rev = null; if (!opt.noCommit) commit(); })();
  return s;
}
$('#hdrag').addEventListener('pointerdown', e => {
  e.preventDefault(); ghost = document.createElement('div'); ghost.className = 'hghost'; ghost.innerHTML = HICON; document.body.appendChild(ghost); moveGhost(e);
  $('#hint').classList.add('show'); document.body.classList.add('dragging'); tip.style.display = 'none';
});
addEventListener('pointermove', e => { if (ghost) moveGhost(e); });
addEventListener('pointerup', e => {
  if (!ghost) return; ghost.remove(); ghost = null; $('#hint').classList.remove('show'); document.body.classList.remove('dragging');
  const t = document.elementFromPoint(e.clientX, e.clientY); if (t && t.closest('#map')) { const r = box.getBoundingClientRect(); dropHospital(map.unproject([e.clientX - r.left, e.clientY - r.top])); }
});
$('#hdrag').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dropHospital(map.getCenter()); } });

/* pulsation des urgences */
let lastP = 0;
function pulse(now) {
  if (!document.hidden && now - lastP > 40 && map.getLayer('urg-halo')) {
    lastP = now; const ph = (now % 2600) / 2600;
    map.setPaintProperty('urg-halo', 'circle-radius', ['interpolate', ['linear'], ['zoom'], 4, 3 + ph * 8, 9, 6 + ph * 20]);
    map.setPaintProperty('urg-halo', 'circle-opacity', .42 * (1 - ph));
  }
  requestAnimationFrame(pulse);
}

/* ---------- interactions carte ---------- */
const tip = $('#tip');
function bindMap() {
  let hz = null;
  map.on('mousemove', 'zones', e => {
    const f = e.features[0]; if (!f) return; const pr = f.properties;
    if (hz !== pr.c) { hz = pr.c; map.setFilter('zones-hl', ['==', ['get', 'c'], hz]); }
    const here = minutesAt(e.lngLat.lng, e.lngLat.lat);
    const top = here == null ? '' : here === Infinity ? `<div class="t"><i style="background:rgb(${SANS_ROUTE})"></i>sans route</div><div class="s">pas d’accès routier à un service d’urgences</div>`
      : `<div class="t"><i style="background:${zcol(here)}"></i>${fmin(here)}</div><div class="s">ici, en voiture jusqu’aux urgences les plus proches</div>`;
    tip.innerHTML = top + `<div class="s"><b style="color:var(--ink)">${esc(pr.n)}</b> (${esc(pr.c)}) · moyenne ${fmin(pr.m)}</div>`;
    tip.style.display = 'block';
    tip.style.left = Math.min(e.point.x + 16, box.clientWidth - 250) + 'px'; tip.style.top = Math.max(8, e.point.y - 96) + 'px';
  });
  map.on('mouseenter', 'zones', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'zones', () => { map.getCanvas().style.cursor = ''; hz = null; map.setFilter('zones-hl', ['==', ['get', 'c'], '']); tip.style.display = 'none'; });
  ['urg', 'smur'].forEach(l => {
    map.on('mouseenter', l, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', l, () => { map.getCanvas().style.cursor = ''; });
    map.on('click', l, e => {
      const f = e.features[0], pr = f.properties, ty = String(pr.types).split(',').map(t => TYPES[t] || t).join(' · ');
      new maplibregl.Popup({ closeButton: false, offset: 10 }).setLngLat(f.geometry.coordinates)
        .setHTML(`<b>${esc(pr.name)}</b><br>${esc(pr.ville)}<br><span style="color:var(--muted)">${esc(ty)}<br>FINESS ${esc(pr.finess)}${pr.pos ? '<br>Position : ' + esc(pr.pos) : ''}</span>`).addTo(map);
      e.preventDefault();
    });
  });
  map.on('click', 'zones', e => { if (e.defaultPrevented) return; if (map.queryRenderedFeatures(e.point, { layers: ['urg', 'smur'].filter(l => map.getLayer(l)) }).length) return; const f = e.features[0]; if (f) pickZone(f.properties); });
  map.on('click', 'zones-dim', e => { const f = e.features[0]; if (f) { selectTerr(null); selectRegion(f.properties.r); } });
  map.on('mouseenter', 'zones-dim', () => { map.getCanvas().style.cursor = 'pointer'; }); map.on('mouseleave', 'zones-dim', () => { map.getCanvas().style.cursor = ''; });
}

/* ---------- contrôles ---------- */
$('#collapse').onclick = () => { const c = $('#layers').classList.toggle('closed'); $('#collapse').setAttribute('aria-expanded', String(!c)); };
$('#zin').onclick = () => map.zoomIn(); $('#zout').onclick = () => map.zoomOut();
$('#home').onclick = () => { clearRegion(); selectTerr(null); map.easeTo({ ...homeCam(), duration: 1400 }); };
function setView(d3) {
  state.d3 = d3; $('#v3').classList.toggle('on', d3); $('#v2').classList.toggle('on', !d3); $('#v3').setAttribute('aria-pressed', String(d3)); $('#v2').setAttribute('aria-pressed', String(!d3));
  map.easeTo({ pitch: d3 ? 50 : 0, bearing: d3 ? -10 : 0, duration: 1000, essential: true });
}
$('#v3').onclick = () => setView(true); $('#v2').onclick = () => setView(false);

/* ---------- recherche de territoire ---------- */
const norm = x => ' ' + x.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/ ste? (?=\S)/g, m => m === ' st ' ? ' saint ' : ' sainte ').trim();
const nc = x => norm(x).replace(/ /g, ''); // forme compacte : « loire atlantique » = « Loire-Atlantique »
const REGS = [...new Set(zones.features.filter(z => z.properties.t === 'FR').map(z => z.properties.r))].sort((a, b) => a.localeCompare(b, 'fr'));
const rmean = r => { const fs = zones.features.filter(z => z.properties.r === r), p = fs.reduce((s, z) => s + (z.properties.p || 0), 0); return p ? fs.reduce((s, z) => s + (z.properties.p || 0) * z.properties.m, 0) / p : null; };
const PICK = () => [{ g: 'France', k: 'France entière', s: 'Métropole et outre-mer', m: null, go: () => { clearRegion(); selectTerr(null); map.flyTo({ ...homeCam(), duration: 1800, essential: true }); } }]
  .concat(REGS.map(r => ({ g: 'Régions', k: r, s: zones.features.filter(z => z.properties.r === r).length + ' départements', m: rmean(r), go: () => { selectTerr(null); selectRegion(r); } })))
  .concat(TERR.filter(t => t.t !== 'FR').map(t => ({ g: 'Outre-mer', k: TFULL[t.t], s: 'Code ' + t.code, m: rmean(TFULL[t.t]), go: () => { clearRegion(); selectTerr(t); flyTerr(t); } })))
  .concat(zones.features.filter(z => z.properties.t === 'FR').map(z => ({ g: 'Départements', k: z.properties.n, s: z.properties.c + ' · ' + z.properties.r, m: z.properties.m, hide: true, go: () => { selectTerr(null); selectRegion(z.properties.r, z.properties.c); } })))
  .concat(zones.features.filter(z => z.properties.t !== 'FR').map(z => ({ g: 'Communes d’outre-mer', k: z.properties.n, s: z.properties.r, m: z.properties.m, hide: true, go: () => { selectTerr(TERR.find(t => t.t === z.properties.t)); selectRegion(z.properties.r, z.properties.c); } })));
let qRes = [], qOn = 0;
/* lieux trouvés par le serveur : communes (INSEE), urgences/SMUR (FINESS), adresses et lieux-dits (BAN) */
const QSRV = { q: '', lieux: [], attente: null, entree: false }, QG = { commune: 'Communes', etablissement: 'Urgences et SMUR', adresse: 'Adresses et lieux-dits' };
let qPop = null, qMk = null;
function allerA(l) {
  clearRegion();
  const t = l.terr && l.terr !== 'FR' ? TERR.find(x => x.t === l.terr) : null; selectTerr(t);
  map.flyTo({ center: [l.lon, l.lat], zoom: l.zoom, pitch: 0, bearing: 0, duration: 1800, essential: true });
  if (qPop) qPop.remove(); if (qMk) qMk.remove();
  const el = document.createElement('div'); el.className = 'qpin'; qMk = new maplibregl.Marker({ element: el }).setLngLat([l.lon, l.lat]).addTo(map);
  const sim = SIMS.length && !state.avant ? gainSimule(l.lon, l.lat) : null;
  const temps = l.type === 'etablissement' ? `<div class="qt"><span>Service ${l.service === 'SMUR' ? 'SMUR' : 'd’urgences'} (FINESS)</span></div>`
    : l.minutes == null ? '<div class="qt"><span>Hors réseau routier</span></div>'
    : `<div class="qt"><i style="background:${zcol(l.minutes)}"></i><b>${fmin(l.minutes)}</b><span>de route jusqu’aux urgences</span></div><div class="qs">${l.via ? esc(l.via) + ' · ' : ''}temps actuel, sans simulation</div>`
      + (sim != null ? `<div class="qs">Avec les hôpitaux simulés : <b>${fmin(sim)}</b></div>` : '');
  qPop = new maplibregl.Popup({ offset: 14, maxWidth: '280px', className: 'qpop' }).setLngLat([l.lon, l.lat])
    .setHTML(`<b>${esc(l.nom)}</b><div class="qs">${esc(l.detail)}</div>${temps}`).addTo(map);
  qPop.on('close', () => { if (qMk) { qMk.remove(); qMk = null; } });
}
function chercherServeur(q) {
  clearTimeout(QSRV.attente); if (nc(q).length < 2) { QSRV.q = q; QSRV.lieux = []; return; }
  QSRV.attente = setTimeout(async () => {
    try {
      const r = await (await fetch('/api/recherche?q=' + encodeURIComponent(q))).json();
      if ($('#q').value.trim() !== q) return;
      QSRV.q = q; QSRV.lieux = r.lieux || []; renderQ(true);
      if (QSRV.entree) { QSRV.entree = false; choose(0); }
    } catch (e) { console.error(e); }
  }, 140);
}
function renderQ(depuisServeur) {
  const brut = $('#q').value.trim(), v = nc(brut), all = PICK();
  if (!depuisServeur) chercherServeur(brut);
  const loc = v ? all.filter(p => nc(p.k).includes(v) || nc(p.s).includes(v)).slice(0, 8) : all.filter(p => !p.hide);
  const dejaOM = new Set(loc.filter(p => p.g === 'Communes d’outre-mer').map(p => nc(p.k)));
  const srv = v && QSRV.q === brut ? QSRV.lieux.filter(l => !(l.type === 'commune' && dejaOM.has(nc(l.nom))))
    .map(l => ({ g: QG[l.type], k: l.nom, s: l.detail, m: l.type === 'etablissement' ? null : l.minutes, srv: true, go: () => allerA(l) })) : [];
  qRes = loc.concat(srv); qOn = 0;
  let g = '', h = ''; qRes.forEach((p, i) => {
    if (p.g !== g) { g = p.g; h += `<div class="qh">${esc(g)}</div>`; }
    h += `<button class="qi${i === 0 ? ' on' : ''}" role="option" data-i="${i}"><i style="background:${p.m == null ? 'linear-gradient(135deg,#7C8CFF,#5CF2C8)' : zcol(p.m)}"></i><div><b>${esc(p.k)}</b><span>${esc(p.s)}</span></div>${p.m == null ? '' : `<em>${fmin(p.m)}</em>`}</button>`;
  });
  $('#qList').innerHTML = h || `<div class="qh">${v && QSRV.q !== brut ? 'Recherche…' : 'Aucun lieu trouvé'}</div>`;
  $$('#qList .qi').forEach(b => b.onmousedown = e => { e.preventDefault(); choose(+b.dataset.i); });
}
function openQ() { renderQ(); $('#qList').classList.add('show'); $('#q').setAttribute('aria-expanded', 'true'); }
function closeQ() { $('#qList').classList.remove('show'); $('#q').setAttribute('aria-expanded', 'false'); }
function choose(i) { const p = qRes[i]; if (!p) { if (nc($('#q').value).length >= 2) QSRV.entree = true; return; } closeQ(); $('#q').value = p.k === 'France entière' ? '' : p.k; $('#q').blur(); p.go(); }
$('#q').addEventListener('focus', openQ); $('#q').addEventListener('input', openQ); $('#q').addEventListener('blur', () => setTimeout(closeQ, 120));
$('#q').addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeQ(); $('#q').blur(); }
  else if (e.key === 'Enter') { e.preventDefault(); choose(qOn); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); qOn = Math.max(0, Math.min(qRes.length - 1, qOn + (e.key === 'ArrowDown' ? 1 : -1))); $$('#qList .qi').forEach(b => b.classList.toggle('on', +b.dataset.i === qOn)); const b = document.querySelector(`#qList .qi[data-i="${qOn}"]`); if (b) b.scrollIntoView({ block: 'nearest' }); }
});
addEventListener('resize', () => { clearTimeout(window.__rt); window.__rt = setTimeout(drawTiles, 150); });
/* ---------- assistant (cœur) : question en français → calculs du moteur → réponse + carte ---------- */
const chat = { hist: [], busy: false };
const SUGG = ['Quels départements sont prioritaires ?', 'Combien d’hôpitaux pour mettre la Lozère au vert ?', 'Où placer 3 hôpitaux en Bretagne ?',
  'Et si on ouvrait des urgences à Langogne ?', 'Quel est l’hôpital le plus proche de Corte ?'];
const cm = $('#chatMsgs');
const SRC = {}; // sources reçues des outils : id → { court, label, url }
const tags = s => s.replace(/\s?\[([A-Za-z]+(?:\s*[·,+/]\s*[A-Za-z]+)*)\]/g, (m, g) => {
  const ids = g.split(/\s*[·,+/]\s*/).map(x => x.toUpperCase());
  return ids.every(i => /^(FINESS|INSEE|OSM|OPTIM|COMMUNES)$/.test(i)) ? ids.map(i => `<sup class="st" title="${esc(SRC[i] ? SRC[i].label : i)}">${i}</sup>`).join('') : m;
});
function md(t) { // mini-markdown sûr (texte échappé) : **gras**, listes « - » ou « 1. », paragraphes, [SOURCE]
  const inl = s => tags(s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'));
  let html = '', list = null;
  for (const raw of esc(t).split('\n')) {
    const l = raw.trim(), m = l.match(/^(?:[-•*]|(\d+)[.)])\s+(.*)$/);
    if (m) { const tag = m[1] ? 'ol' : 'ul'; if (list !== tag) { if (list) html += `</${list}>`; html += `<${tag}>`; list = tag; } html += `<li>${inl(m[2])}</li>`; continue; }
    if (list) { html += `</${list}>`; list = null; }
    if (l) html += `<p>${inl(l)}</p>`;
  }
  return list ? html + `</${list}>` : html;
}
function chatHello() {
  cm.innerHTML = `<div class="ba"><p><b>Bonjour !</b> Posez-moi vos questions sur l’accès aux urgences. Je réponds avec le moteur de la carte (temps de route réels, simulations, implantations optimales) et j’agis directement sur la carte.</p>` +
    (INIT.assistant ? '' : '<p class="err">Clé d’API absente : renseignez-la dans le fichier .env du projet (voir .env.example), puis relancez le serveur.</p>') +
    `</div><div class="sugg">${SUGG.map(s => `<button type="button">${esc(s)}</button>`).join('')}</div>`;
  cm.querySelectorAll('.sugg button').forEach(b => b.onclick = () => ask(b.textContent));
}
function openChat(on) {
  const c = $('#chat'), show = on ?? !c.classList.contains('show');
  c.classList.toggle('show', show); document.body.classList.toggle('chat-on', show);
  $('#heart').classList.toggle('on', show); $('#heart').setAttribute('aria-expanded', String(show));
  if (show) { if (!cm.childElementCount) chatHello(); setTimeout(() => $('#chatIn').focus(), 60); }
}
$('#heart').addEventListener('click', () => openChat());
$('#chatX').onclick = () => { openChat(false); $('#heart').focus(); };
$('#chatNew').onclick = () => { if (!chat.busy) { chat.hist = []; chatHello(); } };
$('#chat').addEventListener('keydown', e => { if (e.key === 'Escape') { openChat(false); $('#heart').focus(); } });
const chatIn = $('#chatIn');
const grow = () => { chatIn.style.height = '38px'; chatIn.style.height = Math.min(110, chatIn.scrollHeight + 2) + 'px'; };
chatIn.addEventListener('input', grow);
chatIn.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(chatIn.value); } });
$('#chatForm').addEventListener('submit', e => { e.preventDefault(); ask(chatIn.value); });

const fmtV = (v, f) => v == null ? '–' : f === 'min' ? (v >= 120 ? 'sans route' : (Math.round(v * 10) / 10).toLocaleString('fr-FR') + ' min')
  : f === 'pct' ? v.toLocaleString('fr-FR', { maximumFractionDigits: 1 }) + ' %' : f === 'hab' ? fmtM(v) : fr(v);
function carte(c) { // petits graphiques sous la réponse de l'assistant
  if (c.k === 'chiffres') return `<div class="vz vzc">${c.items.map(i => `<div><b class="big ${i.ton || ''}">${i.signe && i.v > 0 ? '+' : ''}${fr(i.v)}</b><span>${esc(i.lib)}</span></div>`).join('')}</div>`;
  if (c.k === 'tuiles') return `<div class="vz">${c.titre ? `<h4>${esc(c.titre)}</h4>` : ''}<div class="tl">${c.tuiles.map(t => `<div><b>${t.min != null ? `<i style="background:${zcol(t.min)}"></i>` : ''}${fmtV(t.v, t.f)}</b><span>${esc(t.lib)}</span></div>`).join('')}</div></div>`;
  if (c.k === 'barres') {
    const max = Math.max(1, ...c.lignes.map(l => l.v || 0));
    return `<div class="vz"><h4>${esc(c.titre)}</h4>${c.lignes.map((l, i) => `<div class="br">${c.rang ? `<em>${i + 1}</em>` : ''}<span class="n">${esc(l.nom)}${l.sous ? `<span>${esc(l.sous)}</span>` : ''}</span><span class="bar"><span style="width:${Math.max(4, (l.v || 0) / max * 100).toFixed(0)}%;background:${c.bon ? 'var(--good)' : l.min != null ? zcol(l.min) : 'var(--accent)'}"></span></span><span class="v">${c.signe && l.v > 0 ? c.signe : ''}${fmtV(l.v, c.f)}</span></div>`).join('')}` +
      (c.reste ? `<div class="more">… et ${c.reste} autre${c.reste > 1 ? 's' : ''} (posés sur la carte)</div>` : '') + (c.note ? `<div class="more">${esc(c.note)}</div>` : '') + '</div>';
  }
  if (c.k === 'temps') return `<div class="vz tps"><b class="pill" style="background:${zcol(c.min)};color:${c.min >= 36 ? '#fff' : '#0A1030'}">${fmtV(c.min, 'min')}</b><div><span>de</span> <b>${esc(c.de)}</b><br><span>vers</span> ${esc(c.vers)}</div></div>`;
  if (c.k === 'liste') return `<div class="vz"><h4>${esc(c.titre)}</h4>${c.lignes.map(l => `<div class="li"><b>${esc(l.nom)}</b><span>${esc(l.sous || '')}</span></div>`).join('')}</div>`;
  return '';
}
function piedSources(el, ids) {
  el.innerHTML = ids.length ? '<span>Sources</span>' + ids.map(i => { const x = SRC[i]; return x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener" title="${esc(x.label)}">${esc(x.court)}</a>` : `<b title="${esc(x.label)}">${esc(x.court)}</b>`; }).join('') : '';
}
async function agir(a) { // actions décidées par l'assistant, appliquées dans l'ordre
  if (a.type === 'placer') { if (a.remplacer && SIMS.length) removeAll(); await placer(a.sites); }
  else if (a.type === 'retirer') removeIdx(a.indices);
  else if (a.type === 'zone') {
    if (a.terr === 'FR') { selectTerr(null); selectRegion(a.region, a.dep || undefined); }
    else { const t = TERR.find(x => x.t === a.terr); if (t) { clearRegion(); selectTerr(t); selectRegion(TFULL[a.terr]); } }
  }
  else if (a.type === 'classement') priorites();
  else if (a.type === 'centrer') map.flyTo({ center: [a.lon, a.lat], zoom: a.zoom || 10, duration: 1600, essential: true });
  else if (a.type === 'accueil') { clearRegion(); selectTerr(null); map.flyTo({ ...homeCam(), duration: 1800, essential: true }); }
}
async function ask(q) {
  q = (q || '').trim(); if (!q || chat.busy) return;
  chat.busy = true; $('#chatSend').disabled = true; chatIn.value = ''; grow();
  const sg = cm.querySelector('.sugg'); if (sg) sg.remove();
  cm.insertAdjacentHTML('beforeend', `<div class="bu">${esc(q)}</div>`);
  const box = document.createElement('div'); box.className = 'ba';
  box.innerHTML = '<div class="stps"></div><div class="tx"><span class="dots" aria-label="Réflexion en cours"><i></i><i></i><i></i></span></div><div class="vzs"></div><div class="srcs"></div>';
  cm.appendChild(box);
  const stps = box.querySelector('.stps'), tx = box.querySelector('.tx'), vzs = box.querySelector('.vzs'), srcs = box.querySelector('.srcs'), down = () => { cm.scrollTop = cm.scrollHeight; };
  const vus = []; let simule = false;
  down();
  const etat = { sites: SIMS.map(s => { const ll = s.mk.getLngLat(); return { lon: ll.lng, lat: ll.lat }; }), region: state.reg, terr: state.terr ? TFULL[state.terr.t] : null };
  const hist = chat.hist.slice();
  let texte = '', coupe = false, erreur = false, file = Promise.resolve();
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ question: q, historique: hist, etat }) });
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await rd.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const ln = buf.slice(0, k); buf = buf.slice(k + 2);
        if (!ln.startsWith('data: ')) continue;
        const ev = JSON.parse(ln.slice(6));
        if (ev.t === 'texte') { if (coupe && texte) texte += '\n\n'; coupe = false; texte += ev.v; tx.innerHTML = md(texte); }
        else if (ev.t === 'etape') { coupe = true; stps.insertAdjacentHTML('beforeend', `<div class="stp1" data-id="${esc(ev.id)}"><i></i><span>${esc(ev.v)}…</span></div>`); }
        else if (ev.t === 'etape_ok') { const e = stps.querySelector(`[data-id="${CSS.escape(ev.id)}"]`); if (e) { e.classList.add(ev.echec ? 'ko' : 'ok'); e.querySelector('span').textContent = e.querySelector('span').textContent.replace(/…$/, ''); } }
        else if (ev.t === 'action') { if (ev.v.type === 'placer') simule = true; file = file.then(() => agir(ev.v)).catch(console.error); }
        else if (ev.t === 'visuel') vzs.insertAdjacentHTML('beforeend', carte(ev.v));
        else if (ev.t === 'sources') { ev.v.forEach(x => { SRC[x.id] = x; if (!vus.includes(x.id)) vus.push(x.id); }); piedSources(srcs, vus); if (texte) tx.innerHTML = md(texte); }
        else if (ev.t === 'erreur') { erreur = true; tx.innerHTML = (texte ? md(texte) : '') + `<p class="err">${esc(ev.v)}</p>`; }
        down();
      }
    }
  } catch (e) { erreur = true; tx.innerHTML = `<p class="err">Connexion au serveur perdue (${esc(e.message)}).</p>`; }
  await file;
  if (!texte && !erreur) tx.innerHTML = '<p class="muted">(pas de réponse)</p>';
  if (simule && SIMS.length) { srcs.insertAdjacentHTML('beforebegin', aaHTML()); down(); }
  if (texte) chat.hist.push({ role: 'user', texte: q }, { role: 'assistant', texte });
  chat.busy = false; $('#chatSend').disabled = false;
  const bu = box.previousElementSibling; // remonte au début de la réponse si elle dépasse la hauteur du panneau
  if (bu && box.offsetHeight + bu.offsetHeight > cm.clientHeight - 20) cm.scrollTop += bu.getBoundingClientRect().top - cm.getBoundingClientRect().top - 8; else down();
  chatIn.focus();
}

window.__app = { dropHospital, selectRegion, optimiser, priorites, SIMS, HEAT, ask, openChat, agir };

// ---------- Liquid glass : réfraction des bords des panneaux ----------
// Chaque panneau .glass reçoit un filtre SVG (feDisplacementMap) dont la carte de déplacement suit sa forme
// (rectangle arrondi) : au centre rien ne bouge, sur une bande de BORD px le fond est « tiré » vers l'intérieur,
// comme sous le bord épais d'une lentille. Seul Chromium sait appliquer un filtre SVG en backdrop-filter ;
// ailleurs le verre garde son flou et ses reflets (CSS seul).
(() => {
  const chromium = navigator.userAgentData?.brands?.some(b => /Chromium/.test(b.brand));
  if (!chromium || matchMedia('(prefers-reduced-transparency: reduce)').matches) return;
  const NS = 'http://www.w3.org/2000/svg', BORD = 18, FORCE = 30;
  const defs = document.createElementNS(NS, 'svg');
  defs.setAttribute('aria-hidden', 'true'); defs.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;pointer-events:none';
  document.body.appendChild(defs);
  const etat = new WeakMap(); let n = 0;
  const carte = (w, h, r) => { // R = décalage x, G = décalage y (128 = neutre)
    const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
    const g = cv.getContext('2d'), im = g.createImageData(w, h), d = im.data, hw = w / 2, hh = h / 2;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const px = x + .5 - hw, py = y + .5 - hh, qx = Math.abs(px) - (hw - r), qy = Math.abs(py) - (hh - r);
      let dist, nx = 0, ny = 0; // distance au bord, normale sortante
      if (qx > 0 && qy > 0) { const l = Math.hypot(qx, qy); dist = r - l; nx = qx / l; ny = qy / l; }
      else if (qx > qy) { dist = r - qx; nx = 1; } else { dist = r - qy; ny = 1; }
      const t = Math.min(Math.max(dist / BORD, 0), 1), a = (1 - t) ** 2.4; // profil de lentille : fort au bord, nul au centre
      const i = (y * w + x) * 4;
      d[i] = 128 - 127 * a * nx * Math.sign(px); d[i + 1] = 128 - 127 * a * ny * Math.sign(py); d[i + 2] = 128; d[i + 3] = 255;
    }
    g.putImageData(im, 0, 0); return cv.toDataURL();
  };
  const appliquer = el => {
    const w = Math.round(el.offsetWidth), h = Math.round(el.offsetHeight);
    if (w < 24 || h < 24) return;
    const r = Math.min(parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0, w / 2, h / 2), cle = `${w}x${h}r${r}`;
    let s = etat.get(el);
    if (s?.cle === cle) return;
    if (!s) {
      const id = `lg${++n}`, f = document.createElementNS(NS, 'filter');
      f.id = id; f.setAttribute('filterUnits', 'userSpaceOnUse'); f.setAttribute('primitiveUnits', 'userSpaceOnUse'); f.setAttribute('color-interpolation-filters', 'sRGB');
      f.innerHTML = `<feImage x="0" y="0" preserveAspectRatio="none" result="m"/><feDisplacementMap in="SourceGraphic" in2="m" scale="${FORCE}" xChannelSelector="R" yChannelSelector="G"/>`;
      defs.appendChild(f); s = { f }; etat.set(el, s);
      el.style.setProperty('--lgf', `url(#${id})`);
    }
    s.cle = cle;
    for (const [k, v] of [['x', 0], ['y', 0], ['width', w], ['height', h]]) s.f.setAttribute(k, v);
    const img = s.f.firstChild; img.setAttribute('width', w); img.setAttribute('height', h); img.setAttribute('href', carte(w, h, r));
  };
  const attente = new Set(); let prevu = 0;
  const ro = new ResizeObserver(es => { es.forEach(e => attente.add(e.target)); clearTimeout(prevu); prevu = setTimeout(() => { attente.forEach(appliquer); attente.clear(); }, 90); });
  const suivre = el => { if (!etat.has(el) && !el.classList.contains('tip') && !el.classList.contains('hlab')) ro.observe(el); };
  document.querySelectorAll('.glass').forEach(suivre);
  new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(x => { if (x.nodeType === 1 && x.classList.contains('glass')) suivre(x); })))
    .observe(document.querySelector('.stage'), { childList: true });
})();
