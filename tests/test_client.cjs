const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.value = '2'; this.events = {}; this.attrs = {}; this.textContent = ''; this.disabled = false; const names = new Set(); this.classList = {add: n => names.add(n), remove: n => names.delete(n), contains: n => names.has(n), toggle: (n, force) => { const on = force ?? !names.has(n); on ? names.add(n) : names.delete(n); return on; }}; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attrs[name] = value; }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = new Element();
document.querySelector = get;
document.querySelectorAll = () => [];
document.pointerLockElement = get('#video');
const context = vm.createContext({document, window: new Element(), navigator: {}, console, setTimeout, clearTimeout, setInterval, clearInterval, localStorage: {setItem(){}}, performance, Map, Set, Uint8Array, DataView, ArrayBuffer});
vm.runInContext(fs.readFileSync('web/app.js', 'utf8').replace(/initialize\(\);\s*$/, ''), context);
vm.runInContext(`
  globalThis.requests = [];
  state.config = {dlss_target_fps:16, quality_options: Array.from({length:5}, (_,level)=>({level,name:'Preset '+level,width:1280,height:720,fps:60,bitrate_mbps:9,jitter_ms:0}))};
  api = async (path, options) => { requests.push({path, body:JSON.parse(options.body)}); return {quality:JSON.parse(options.body).quality ?? state.quality, dlss_enabled:JSON.parse(options.body).dlss_enabled ?? state.dlssEnabled}; };
  bindEvents();
`, context);
(async () => {
  const key = code => ({code, ctrlKey:true, altKey:true, repeat:false, preventDefault(){this.prevented=true;}});
  get('#sendEscape').events.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(context.requests[0].path, '/api/input/escape');
  const dlss = key('KeyD');
  document.events.keydown(dlss);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(context.requests[1].path, '/api/session/settings');
  assert.equal(context.requests[1].body.dlss_enabled, true);
  assert.equal(get('#liveDlss').attrs['aria-pressed'], 'true');
  get('#qualitySlider').events.change({target:{value:'0'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(context.requests[2].body.quality, 0);
  assert.equal(get('#qualitySlider').value, 0);
  const plainEscape = {code:'Escape',preventDefault(){throw new Error('Plain Escape was intercepted');}};
  document.events.keydown(plainEscape);
  assert.equal(context.requests.length, 3);
  get('#originalRenderer').events.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(context.requests[3].path, '/api/session/settings');
  assert.equal(context.requests[3].body.settings.dlss.temporal_stability, false);
  assert.equal(context.requests[3].body.settings.dlss.neural_before_upscale, false);
  assert.equal(context.requests[3].body.dlss_enabled, undefined);
  clearTimeout(vm.runInContext('showToast.timer', context));
  console.log('PASS: Escape button, live DLSS toggle, quality slider, browser Escape behavior, and original renderer restoration');
})().catch(error=>{console.error(error);process.exitCode=1;});
