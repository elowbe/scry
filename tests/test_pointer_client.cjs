const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() {
    this.style = {}; this.attrs = {}; this.events = {}; this.videoWidth = 1920; this.videoHeight = 1080;
    const classes = new Set();
    this.classList = {add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),toggle:(n,v)=>v?classes.add(n):classes.delete(n)};
  }
  getBoundingClientRect() { return {left:100,top:0,width:1000,height:1000}; }
  getAttribute(n) { return this.attrs[n]; }
  setAttribute(n,v) { this.attrs[n]=v; }
  addEventListener(n,f) { this.events[n]=f; }
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const document=new Element(); document.querySelector=get; document.querySelectorAll=()=>[]; document.pointerLockElement=get('#video');
const context=vm.createContext({document,window:new Element(),navigator:{},console,setInterval,clearInterval,setTimeout,clearTimeout,performance,DataView,ArrayBuffer,Uint8Array,Map,Set});
vm.runInContext(fs.readFileSync('web/app.js','utf8').replace(/initialize\(\);\s*$/,''),context);
vm.runInContext(`
  globalThis.sent=[];
  state.pointerChannel={readyState:'open',bufferedAmount:0,send:data=>sent.push(new Uint8Array(data))};
  state.config={width:1920,height:1080};
  receiveCursor({type:'cursor',epoch:1,warp:true,x:.5,y:.5,width:1920,height:1080,visible:true});
`,context);
const run=s=>vm.runInContext(s,context);
run('sendMouseMove(100,0)');
assert.equal(run('cursor.x'),.6); // immediate, before any packet or host response
assert.equal(context.sent.length,0);
run('flushPointer()');
let packet=context.sent.at(-1);
assert.equal(packet[0],5);
assert.equal(new DataView(packet.buffer).getUint16(5,true),39321);
assert.equal(get('#cursorLayer').style.top,'218.75px'); // letterboxing excluded
run("receiveCursor({type:'cursor',epoch:1,warp:false,x:.1,y:.1,width:1920,height:1080,visible:true})");
assert.equal(run('cursor.x'),.6); // delayed image metadata cannot drag local cursor back
run('sendMouseButton(0,true); sendMouseMove(100,0); sendMouseButton(0,false)');
assert.deepEqual(Array.from(context.sent,p=>p[0]),[5,3,5,3]);
assert.equal(context.sent.at(-1)[2],0);
run("receiveCursor({type:'cursor',epoch:2,warp:true,x:.5,y:.5,width:1920,height:1080,visible:false})");
assert.equal(run('cursor.x'),.5);
assert.equal(get('#cursorLayer').classList.contains('hidden'),true);
run('sendMouseMove(10000,-50); flushPointer()');
assert.equal(context.sent.at(-1)[0],2); // hidden FPS cursor still delivers camera movement
assert.equal(new DataView(context.sent.at(-1).buffer).getInt16(1,true),10000);
run("receiveCursor({type:'cursor',epoch:3,warp:true,x:.4,y:.4,width:1920,height:1080,visible:true})");
run('sendMouseMove(10000,10000); flushPointer()');
assert.equal(run('cursor.x'),1); assert.equal(run('cursor.y'),1);
run('state.pointerChannel.bufferedAmount=9000; sendMouseMove(-100,0); flushPointer()');
const count=context.sent.length;
run('sendMouseMove(-100,0); flushPointer()');
assert.equal(context.sent.length,count);
run('state.pointerChannel.bufferedAmount=0; flushPointer()');
assert.equal(new DataView(context.sent.at(-1).buffer).getUint16(5,true),Math.round(.8*65535));
run('sendMouseMove(10,0); releaseAll(); flushPointer()');
assert.equal(context.sent.at(-1)[0],127); // no delayed move after unlocking
run('resetCursor()');
assert.equal(run('cursor.epoch'),0);
assert.equal(run('cursor.pending'),false);
run("receiveCursor({type:'cursor',epoch:1,warp:true,x:.8,y:.8,width:1920,height:1080,visible:true})");
run("receiveCursor({type:'cursor',epoch:2,warp:true,x:.25,y:.25,width:1920,height:1080,visible:true})");
assert.equal(run('cursor.x'),.8); // old host's unverified quadrant reset
assert.equal(run('cursor.y'),.8);
assert.equal(run('cursor.pending'),true);
run('flushPointer()');
assert.equal(new DataView(context.sent.at(-1).buffer).getUint32(1,true),2);
assert.equal(new DataView(context.sent.at(-1).buffer).getUint16(5,true),52428);
run("receiveCursor({type:'cursor',epoch:3,warp:true,warp_reason:'external',x:.5,y:.5,width:1920,height:1080,visible:true})");
assert.equal(run('cursor.x'),.5); // verified game recenter still works
run('resetCursor()');
run(`
  cursor.x=.8; cursor.y=.7; cursor.pending=false;
  globalThis.lockRequests=0;
  $('#video').requestPointerLock=()=>{ lockRequests++; };
  sendMouseButton(0,true);
  sendMouseButton(0,false);
  capturePointer({target:$('#video'),clientX:350,clientY:300,button:0});
`);
assert.equal(run('cursor.x'),.8);
assert.equal(run('cursor.y'),.7);
assert.equal(run('cursor.pending'),false);
assert.equal(run('lockRequests'),0);
assert.equal(context.sent.at(-2)[0],3);
assert.equal(context.sent.at(-2)[2],1);
assert.equal(context.sent.at(-1)[0],3);
assert.equal(context.sent.at(-1)[2],0);
run(`
  document.pointerLockElement=null;
  capturePointer({target:$('#video'),clientX:350,clientY:500,button:0});
`);
assert.ok(Math.abs(run('cursor.x')-.25)<1e-12);
assert.equal(run('cursor.y'),.5);
assert.equal(run('lockRequests'),1);
assert.equal(run('cursor.pending'),true);
console.log('PASS: local absolute cursor, letterboxing, image-only updates, drag order, FPS/recenter, edge clamp, coalescing, release and reconnect');
