// Offline browser regression. Uses Chrome's anonymous debugging pipe.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';
import {setTimeout as delay} from 'node:timers/promises';
const dir=mkdtempSync(join(tmpdir(),'survivex-review-'));
const chrome=process.env.CHROME_BIN||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const child=spawn(chrome,['--headless','--disable-background-networking','--disable-component-update',
  '--no-first-run','--no-default-browser-check','--remote-debugging-pipe','--user-data-dir='+dir],
  {stdio:['ignore','ignore','ignore','pipe','pipe']});
const exited=once(child,'exit');
const timer=setTimeout(()=>child.kill('SIGKILL'),25000);
let id=0,buf='',sessionId;
const waiting=new Map();
child.stdio[4].on('data',b=>{
  buf+=b.toString();
  let end;
  while((end=buf.indexOf('\0'))>=0){
    const message=JSON.parse(buf.slice(0,end));buf=buf.slice(end+1);
    if(waiting.has(message.id)){
      const [resolve,reject]=waiting.get(message.id);waiting.delete(message.id);
      if(message.error)reject(Error(message.error.message));else resolve(message.result);
    }
  }
});
child.once('exit',()=>{for(const [,reject] of waiting.values())reject(Error('Chrome exited'));});
function call(method,params={},session=sessionId){
  const msg={id:++id,method,params,sessionId:session};
  return new Promise((resolve,reject)=>{waiting.set(msg.id,[resolve,reject]);child.stdio[3].write(JSON.stringify(msg)+'\0')});
}
async function evaluate(expression){
  const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  assert(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;
}
try{
  const good=join(dir,'trek.json');
  execFileSync(process.env.PYTHON||'python3',['journey.py','demo',good]);
  const {targetId}=await call('Target.createTarget',{url:new URL('./rescue.html',import.meta.url).href});
  ({sessionId}=await call('Target.attachToTarget',{targetId,flatten:true}));
  await call('Network.enable');
  await call('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
  for(let i=0;i<100&&!await evaluate("typeof load==='function'");i++)await delay(30);
  const {root}=await call('DOM.getDocument');
  const {nodeId}=await call('DOM.querySelector',{nodeId:root.nodeId,selector:'#file'});
  async function open(file){
    await call('DOM.setFileInputFiles',{nodeId,files:[file]});
    for(let i=0;i<150;i++){
      const msg=await evaluate("$('status').textContent");
      if(msg.includes('verified')||msg.includes('rejected'))return msg;
      await delay(20);
    }
    throw Error('Import timed out');
  }
  assert.match(await open(good),/SYNTHETIC TEST DATA.*verified/);
  assert.deepEqual(await evaluate('segments.map(s=>s.length)'),[3,2,1]);
  const summary=await evaluate('report');
  for(const label of ['120 s','No journey end','Ridge shelter','Trail hut','12.95560']) {
    assert(summary.includes(label),summary);
  }
  await call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:dir});
  await evaluate("$('save').click()");
  const saved=join(dir,'incident-summary.txt');
  for(let i=0;i<100&&!existsSync(saved);i++)await delay(30);
  assert.equal(readFileSync(saved,'utf8'),summary);
  assert.equal(await evaluate("$('map').querySelectorAll('circle').length"),6);
  await evaluate("$('plus').click();$('map').querySelector('circle').dispatchEvent(new MouseEvent('click'));");
  assert.match(await evaluate("$('detail').textContent"),/accuracy_m = 6/);
  const doc=JSON.parse(readFileSync(good,'utf8'));doc.events[0][0]+=' ';
  const bad=join(dir,'bad.json');writeFileSync(bad,JSON.stringify(doc));
  assert.match(await open(bad),/Integrity failure/);
  assert(await evaluate("$('review').hidden"));
  const broken=join(dir,'malformed.json');writeFileSync(broken,'{');
  assert.match(await open(broken),/rejected/);
  process.stdout.write('PASS offline Chrome file import, source labels, route segments, gap, summary, zoom, sample inspection, damaged and malformed files\n');
}finally{
  child.kill();await exited;clearTimeout(timer);rmSync(dir,{recursive:true,force:true});
}
