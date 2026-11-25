// Az 1 kattintásos fix előre megnyomható „Igen” gombja (2026-11-25): a ui.html VALÓDI
// mcFixReady / mcSyncYes / mcYesClick függvényei futnak DOM-csonkkal.
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
const yes = { disabled: true, style: {}, textContent: '' };
const document = { getElementById: id => (id === 'mc-yes' ? yes : null) };
const resolved = [];
const resolveConfirm = v => resolved.push(v);
const api = () => ({ js_log: () => {} });
const src = `let delLoading = false, mcFixActive = false, mcArmed = false, mcNetPending = false;
  ${['mcFixReady', 'mcSyncYes', 'mcYesClick'].map(grab).join('\n')}
  return { mcSyncYes, mcYesClick,
           open: () => { mcFixActive = true; mcArmed = false; mcNetPending = true; delLoading = true; mcSyncYes(); },
           previewDone: () => { delLoading = false; mcSyncYes(); },
           netDone: () => { mcNetPending = false; mcSyncYes(); },
           close: () => { mcFixActive = mcArmed = false; } };`;
const ui = new Function('document', 'resolveConfirm', 'api', src)(document, resolveConfirm, api);

let fails = 0;
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { fails++; console.log('HIBA:', name, '\n  kapott:', g, '\n  várt:  ', w); }
  else console.log('ok:', name);
}

// 1) Elemzés közben megnyomva: megjegyzi, és CSAK akkor indít, ha az elemzés ÉS a hálózat-felismerés is kész.
ui.open();
eq('elemzés alatt kattintható, szürke', [yes.disabled, yes.style.opacity], [false, '0.5']);
ui.mcYesClick();
eq('megjegyezve, még nem indult', [resolved.length, yes.textContent.startsWith('✅')], [0, true]);
ui.previewDone();
eq('az elemzés kész, a hálózat még nem: vár', resolved, []);
ui.netDone();
eq('minden kész: egyszer, Igen-nel indult', resolved, [true]);
ui.close();

// 2) Második kattintás visszavonja: betöltés után nem indul magától, a gomb sima Igen.
resolved.length = 0;
ui.open();
ui.mcYesClick();
ui.mcYesClick();
ui.netDone();
ui.previewDone();
eq('visszavonva: nem indult', resolved, []);
eq('a gomb kész állapotban', [yes.textContent, yes.style.opacity], ['Igen', '']);
ui.mcYesClick();
eq('kész állapotban a kattintás azonnal indít', resolved, [true]);
ui.close();

// 3) Mégse után egy késve érkező eredmény nem indít semmit.
resolved.length = 0;
ui.open();
ui.mcYesClick();
ui.close();
ui.previewDone();
ui.netDone();
eq('Mégse után nem indul', resolved, []);

// 4) Más dialógus (nem a fix): a gomb a régi módon azonnal válaszol.
resolved.length = 0;
ui.mcYesClick();
eq('sima megerősítő: azonnal', resolved, [true]);

console.log(fails ? 'HIBÁK: ' + fails : 'MIND RENDBEN');
process.exit(fails ? 1 : 0);
