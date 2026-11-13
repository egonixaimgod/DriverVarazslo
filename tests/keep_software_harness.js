// Az 1 kattintásos fix "Szoftveres driverek megtartása" kapcsolója (2026-11-13): a ui.html
// VALÓDI delRowForce / delMachineInfo / delKeepPackages függvényei futnak DOM-csonkkal.
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
const boxes = { 'mc-wifi-mode': false, 'mc-rebuild-wifi': true, 'mc-skip-printers': true, 'mc-keep-software': false };
const document = { getElementById: id => ({ get checked() { return !!boxes[id]; } }) };
const src = `let delPreview = [], delUnchecked = new Set(), delMachine = null, delBootDetected = true;
  ${['delRowForce', 'delMachineInfo', 'delRowOn', 'delKeepPackages'].map(grab).join('\n')}
  return { delRowForce, delKeepPackages,
           set: (p, m, u) => { delPreview = p; delMachine = m; delUnchecked = new Set(u || []); } };`;
const ui = new Function('document', src)(document);

let fails = 0;
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { fails++; console.log('HIBA:', name, '\n  kapott:', g, '\n  várt:  ', w); }
  else console.log('ok:', name);
}

const rows = [
  { published: 'oem5.inf', original: 'teamviewervpn.inf', group: 'normal' },
  { published: 'oem6.inf', original: 'nvhda.inf', group: 'normal' },
  { published: 'oem7.inf', original: 'brscan.inf', group: 'printer' },
  { published: 'oem8.inf', original: 'storahci.inf', group: 'boot' },
  { published: 'oem9.inf', original: 'wireguard.inf', group: 'normal' },
];
const mm = { packages: {
  'oem5.inf': { slot: 'software' }, 'oem6.inf': { slot: 'gpu:pci\\x' }, 'oem7.inf': { slot: 'software' },
  'oem8.inf': { slot: 'board:storage' }, 'oem9.inf': { slot: 'software' } } };

// 1) Kapcsoló KI: csak a kézi kivétel megy át.
ui.set(rows, mm, ['oem6.inf']);
eq('KI: nincs szoftver-zárolás', ui.delRowForce(rows[0]), null);
eq('KI: csak a kézi kivétel', ui.delKeepPackages(),
   [{ published: 'oem6.inf', original: 'nvhda.inf', reason: 'manual' }]);

// 2) Kapcsoló BE: a 'software' kategória (csak a normal csoport) zárolt, és átmegy reason='software'-rel.
boxes['mc-keep-software'] = true;
ui.set(rows, mm, ['oem6.inf']);
eq('BE: TeamViewer zárolt, megmarad', [ui.delRowForce(rows[0]).on, ui.delRowForce(rows[0]).keep], [false, true]);
eq('BE: a nyomtató-csoportról a nyomtató-kapcsoló dönt', !!ui.delRowForce(rows[2]).keep, false);
eq('BE: a boot-zárolás érintetlen', ui.delRowForce(rows[3]).keep, undefined);
eq('BE: kivételek', ui.delKeepPackages(), [
  { published: 'oem5.inf', original: 'teamviewervpn.inf', reason: 'software' },
  { published: 'oem6.inf', original: 'nvhda.inf', reason: 'manual' },
  { published: 'oem9.inf', original: 'wireguard.inf', reason: 'software' }]);

// 3) Nyomtató-kapcsoló KI + szoftver BE: a szoftveres slotú nyomtató-csomag NEM marad meg.
boxes['mc-skip-printers'] = false;
ui.set(rows, mm, []);
eq('nyomtató-kapcsoló KI: a szkenner-csomag törlődik', ui.delRowForce(rows[2]), null);

// 4) Térkép nélkül a kapcsoló nem tud mit megtartani (a felület ki is mondja).
ui.set(rows, null, []);
eq('térkép nélkül nincs szoftver-zárolás', ui.delKeepPackages(), []);

console.log(fails ? 'HIBÁK: ' + fails : 'MIND RENDBEN');
process.exit(fails ? 1 : 0);
