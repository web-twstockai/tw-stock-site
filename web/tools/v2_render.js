// 匯出 v2 的 node 端呈現（比對工具 tools/compare_export.py 用）：讀匯出目錄的資料檔，用 static_assets/v2.js 同一份程式組頁，
// 並檢查組出來的頁面裡每個站內連結都指到存在的檔案或殼頁可呈現的網址。
// 用法：node tools/v2_render.js <v2 匯出目錄> < requests.json > results.json
'use strict';
const fs=require('fs'),path=require('path'),zlib=require('zlib');
const TW2=require(path.join(__dirname,'static_assets','v2.js'));
const dir=process.argv[2];
const cache={};
function load(f){
  if(f in cache) return cache[f];
  const p=path.join(dir,f);
  if(!fs.existsSync(p)) return cache[f]=null;
  let b=fs.readFileSync(p);if(/\.gz$/.test(f)) b=zlib.gunzipSync(b);
  return cache[f]=JSON.parse(b.toString('utf8'));
}
const M=load('manifest.json');
function view(kind,search,csv){
  const pairs=TW2.parse(search);
  if(kind==='stock'){const code=TW2.parse(search).filter(p=>p[0]==='code').map(p=>p[1])[0]||'';const d=load('data/stock/'+code+'.json.gz');return d?TW2.stockMain(d,pairs):null}
  if(kind==='source'){const sh=load('data/source/'+TW2.sourceShard(pairs,M.g_shards)+'.json.gz');return sh?TW2.sourceMain(sh,pairs):null}
  if(kind==='screener'){const D=load('data/screener.json.gz');const st=TW2.screenerState(D,pairs);return csv?TW2.screenerCsv(D,st):TW2.screenerMain(D,st,'../')}
  if(kind==='revenue'){const st=TW2.revenueState(M,pairs);if(M.months.indexOf(st.month)<0) return null;const e=load('data/revenue/'+st.month+'.json.gz');return e?(csv?TW2.revenueCsv(e,st):TW2.revenueMain(e,st,'../')):null}
  return null;
}
const SHELLS={stock:1,source:1,screener:1,revenue:1};
function checkLink(fromFile,href){
  if(/^(https?:|mailto:|data:|javascript:|#|\/\/)/.test(href)||href==='') return null;
  const h=href.replace(/&amp;/g,'&').replace(/&#34;/g,'"').replace(/&#39;/g,"'");
  const [pq]=h.split('#');const [p,q]=pq.split('?');
  const target=path.posix.normalize(path.posix.join(path.posix.dirname(fromFile),p||path.posix.basename(fromFile)));
  if(target.startsWith('..')) return 'outside:'+href;
  const parts=target.replace(/\/$/,'').split('/');
  if(parts.length===1&&SHELLS[parts[0]]&&(p.endsWith('/')||p==='')){
    const v=view(parts[0],'?'+(q||''),/(^|&)csv=1(&|$)/.test(q||''));
    return v?null:'state:'+href;
  }
  let f=path.join(dir,target);
  if(fs.existsSync(f)&&fs.statSync(f).isDirectory()) f=path.join(f,'index.html');
  return fs.existsSync(f)?null:'missing:'+href;
}
function links(fromFile,html){
  const bad=[];const re=/\s(?:href|src)="([^"]*)"/g;let m;
  while((m=re.exec(html))){const r=checkLink(fromFile,m[1]);if(r) bad.push(r)}
  return bad;
}
const reqs=JSON.parse(fs.readFileSync(0,'utf8'));
const out=reqs.map(r=>{
  if(r.kind==='file'){const html=fs.readFileSync(path.join(dir,r.file),'utf8');return {id:r.id,bad:links(r.file,html)}}
  const v=view(r.kind,r.search,r.csv);
  if(!v) return {id:r.id,missing:true};
  if(r.csv) return {id:r.id,name:v.name,text:v.text};
  return {id:r.id,title:v.title,html:v.html,bad:r.links?links(r.kind+'/index.html',v.html):[]};
});
process.stdout.write(JSON.stringify(out));
