// A folyamat-ablak fejlécének meghajtása DOM-csonkkal: a ui.html VALÓDI függvényei futnak.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

function grab(name) {            // egy top-level függvény forrása, kapcsos-zárójel számlálással
  const i = js.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('nincs: ' + name);
  let d = 0, j = js.indexOf('{', i);
  for (let k = j; k < js.length; k++) {
    if (js[k] === '{') d++;
    else if (js[k] === '}' && --d === 0) return js.slice(i, k + 1);
  }
}
const els = {};
function el(id) {
  if (!els[id]) {
    const cls = new Set(id === 'mp-ring' ? ['indet'] : []);
    els[id] = {
      id, _t: '', style: { display: '' }, children: [],
      get textContent() { return this._t; }, set textContent(v) { this._t = String(v); this._h = String(v); },
      get innerHTML() { return this._h || ''; },
      set innerHTML(v) { this._h = String(v); this._t = String(v).replace(/<[^>]*>/g, ''); this.children = []; },
      classList: { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c), toggle: () => {} },
      setAttribute() {}, appendChild(c) { this.children.push(c); },
      scrollHeight: 0, scrollTop: 0, clientHeight: 0, disabled: false,
    };
  }
  return els[id];
}
const document = { getElementById: el, createElement: () => el('tmp' + Math.random()) };
const names = ['showProgressModal', 'updateProgressModal', 'mpDedup', 'mpClassify', 'appendLog', 'mpSetHead',
               'mpEtaSecs', 'mpFmtSecs', 'onTaskComplete'];
const consts = js.match(/const MP_KIND = \[[\s\S]*?\];/)[0] + '\n' + js.match(/const MP_PLAIN_COUNT = .*;/)[0];
const src = `let progressStartTime = 0, progressTimer = null, mpProg = {}, currentView = '', SERVICE_TOOLS = [];
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const setInterval = () => 1, clearInterval = () => {}, setTimeout = () => {};
  const loadDrivers = () => {}, loadBackupView = () => {}, checkWuStatus = () => {}, refreshServiceTools = () => {};
  ${consts}\n${names.map(grab).join('\n')}
  return { showProgressModal, updateProgressModal, onTaskComplete, mpSetHead, mpDedup };`;
const api = new Function('document', src)(document);

const vis = id => el(id).style.display !== 'none';
const snap = () => ({
  pct: el('mp-pct').textContent, now: el('mp-now').textContent, sub: el('mp-sub').textContent,
  step: vis('mp-st-step') ? el('mp-st-step').textContent : null,
  eta: vis('mp-st-eta') ? el('mp-st-eta').textContent : null,
  status: vis('mp-status') ? el('mp-status').textContent : null,
  counter: vis('mp-counter') ? el('mp-counter').textContent : null,
  time: vis('mp-time'),
});
let bad = 0;
function check(name, got, want) {
  for (const k of Object.keys(want)) {
    const ok = want[k] instanceof RegExp ? want[k].test(String(got[k])) : got[k] === want[k];
    if (!ok) { bad++; console.log('  ELTÉR', name, k, '=', JSON.stringify(got[k]), ' várt:', String(want[k])); }
  }
  console.log((bad ? '' : 'ok  ') + name, JSON.stringify(got));
}

// 1) Letöltés bájtokban (a bejelentett kép): nem "lépés: 12582912 / 32272456"
api.showProgressModal('Szervíz Programok Telepítése', '');
api.updateProgressModal({ log: '🌐 Programok előkészítése (első alkalommal letöltés)...', indeterminate: true });
check('indeterminate', snap(), { pct: '•••', step: null, status: null, time: false, now: /Programok előkészítése/ });
api.updateProgressModal({ current: 12582912, total: 32272456, unit: 'bytes', status: '🌐 Letöltés: 12 / 31 MB' });
check('bytes', snap(), { pct: '39%', step: 'letöltve: 12 / 31 MB', status: null, counter: null, now: '🌐 Letöltés: 12 / 31 MB' });
api.updateProgressModal({ current: 80, total: 157, status: '📦 Kicsomagolás: 80/157 fájl' });
check('extract', snap(), { pct: '51%', step: 'lépés: 80 / 157' });
api.onTaskComplete({ task: 'x', status: '❌ Egyetlen program telepítése sem sikerült.' });
check('fail-end', snap(), { pct: '❌', step: null, eta: null, status: null, time: true });

// 2) Az 1 kattintásos fix törlési lába: számlálós sor -> utána számláló nélküli lépés
api.showProgressModal('1 Kattintásos Fix', '');
api.updateProgressModal({ log: 'Third-party driverek összegyűjtése és törlése...', indeterminate: true });
check('fix-phase', snap(), { now: 'Third-party driverek összegyűjtése és törlése...', pct: '•••' });
api.updateProgressModal({ log: '🗑 Törlés (58/58): oem57.inf', current: 58, total: 58 });
check('fix-del', snap(), { pct: '100%', step: 'lépés: 58 / 58', now: '🗑 Törlés (58/58): oem57.inf' });
api.updateProgressModal({ log: '  ✅ oem57.inf törölve' });
check('fix-result-line', snap(), { pct: '100%', now: '🗑 Törlés (58/58): oem57.inf' });
api.updateProgressModal({ log: '🔧 Wi-Fi driver újraépítése...' });
check('fix-next-step', snap(), { pct: '•••', step: null, eta: null, now: '🔧 Wi-Fi driver újraépítése...' });

// 3) WU-telepítés: a "3/13" számláló nem áll kint kétszer
api.updateProgressModal({ log: 'Összesen 13 driver telepítése...', total: 13, current: 0, counter: '0 / 13' });
api.updateProgressModal({ log: '  ✅ Intel - Net', current: 3, total: 13, counter: '3/13', ok: 3, fail: 0 });
check('wu', snap(), { pct: '23%', step: 'lépés: 3 / 13', sub: '', counter: null, now: 'Összesen 13 driver telepítése...' });

// 4) Százalékos és másodperces egység: nincs "lépés" chip
api.updateProgressModal({ current: 42, total: 100, unit: 'percent' });
check('percent', snap(), { pct: '42%', step: null });
api.updateProgressModal({ current: 300, total: 540, unit: 'seconds', status: '🧪 y-cruncher fut - 5 / 9 perc' });
check('seconds', snap(), { pct: '56%', step: null });
api.onTaskComplete({ task: 'x', status: '✅ Kész' });
check('ok-end', snap(), { pct: '100%', step: null, time: true });

console.log(bad ? ('HIBA: ' + bad) : 'MIND RENDBEN');
process.exit(bad ? 1 : 0);
