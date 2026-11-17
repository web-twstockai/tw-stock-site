// 公開靜態版 v2（共用殼頁＋資料檔）：依網址讀本站匯出的 data/*.json.gz，組出與伺服器同一份模板輸出的頁面。
// 純函式（stockMain／sourceMain／screenerMain／revenueMain／*Csv）也給 node 比對工具用；瀏覽器端只負責讀檔與插入。
(function(root){
  'use strict';
  var TW2={};
  function enc(s){return encodeURIComponent(s).replace(/[!'()*]/g,function(c){return '%'+c.charCodeAt(0).toString(16).toUpperCase()}).replace(/%20/g,'+')}
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&#34;').replace(/'/g,'&#39;')}
  function cmp(a,b){return a[0]<b[0]?-1:a[0]>b[0]?1:(a[1]<b[1]?-1:a[1]>b[1]?1:0)}
  // 與 tools/export_v2.py qkey() 同規則：去掉空值、依鍵與值排序、不編碼
  function qkey(pairs){return pairs.filter(function(p){return p[1]!==''}).slice().sort(cmp).map(function(p){return p[0]+'='+p[1]}).join('&')}
  function qs(pairs){return pairs.filter(function(p){return p[1]!==''&&p[1]!=null}).slice().sort(cmp).map(function(p){return enc(p[0])+'='+enc(String(p[1]))}).join('&')}
  function fnv(s){var b=unescape(encodeURIComponent(s)),h=0x811c9dc5;for(var i=0;i<b.length;i++){h^=b.charCodeAt(i);h=Math.imul(h,0x01000193)>>>0}return h>>>0}
  function parse(search){var out=[];(search||'').replace(/^\?/,'').split('&').forEach(function(x){if(!x)return;var i=x.indexOf('=');var k=i<0?x:x.slice(0,i),v=i<0?'':x.slice(i+1);try{k=decodeURIComponent(k.replace(/\+/g,' '));v=decodeURIComponent(v.replace(/\+/g,' '))}catch(e){}out.push([k,v])});return out}
  function get(pairs,k){for(var i=pairs.length-1;i>=0;i--) if(pairs[i][0]===k) return pairs[i][1];return null}
  function csvCell(v){v=String(v);return /[",\r\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v}
  TW2.qkey=qkey;TW2.qs=qs;TW2.fnv=fnv;TW2.parse=parse;

  // ---- 個股 ----
  var GROUP={price:'q',period:'f',scope:'f',basis:'f',cat:'e'};
  TW2.stockKeys=function(pairs){
    var k={q:[],f:[],e:[]};
    pairs.forEach(function(p){var g=GROUP[p[0]];if(!g||p[1]==='') return;if(p[0]==='price'&&p[1]==='adj') return;k[g].push(p)});
    return {q:qkey(k.q),f:qkey(k.f),e:qkey(k.e)};
  };
  TW2.stockMain=function(d,pairs){
    var k=TW2.stockKeys(pairs);
    if(d.q[k.q]==null||d.f[k.f]==null||d.e[k.e]==null) return null;
    return {title:d.t,html:d.pre+d.q[k.q]+d.r+d.f[k.f]+d.e[k.e]};
  };
  // ---- 來源明細 ----
  TW2.sourceShard=function(pairs,n){
    var sym=get(pairs,'symbol');
    return (sym&&/^[A-Za-z0-9]+$/.test(sym))?'s/'+sym:'g/'+(fnv(qkey(pairs))%(n||16));
  };
  TW2.sourceMain=function(shard,pairs){var x=shard[qkey(pairs)];return x?{title:x[0],html:x[1]}:null};

  // ---- 選股 ----
  var TOG=['enable_trend','enable_breakout','enable_volume','enable_turnover'];
  TW2.screenerState=function(D,pairs){
    var f=get(pairs,'f'),mask='d';
    if(f==='1'){mask=0;TOG.forEach(function(t,i){if(get(pairs,t)==='1') mask|=1<<i});mask=String(mask)}
    var sort=get(pairs,'sort');if(!(sort in D.sorts)) sort='turnover';
    var order=get(pairs,'order')==='asc'?'asc':'desc';
    var page=parseInt(get(pairs,'page')||'1',10);if(!(page>=1)) page=1;
    return {mask:mask,sort:sort,order:order,page:page};
  };
  function scrPairs(st,over){
    var m=st.mask==='d'?15:parseInt(st.mask,10),p=[['f','1']];
    TOG.forEach(function(t,i){if(m>>i&1) p.push([t,'1'])});
    var o={sort:st.sort,order:st.order};for(var k in over) o[k]=over[k];
    for(var k2 in o) p.push([k2,String(o[k2])]);
    return p;
  }
  function patchSelect(h,name,val){
    var i=h.indexOf('<select name="'+name+'">');if(i<0) return h;
    var j=h.indexOf('</select>',i);var s=h.slice(i,j);
    s=s.replace(/ selected>/g,' >').replace('value="'+val+'" >','value="'+val+'" selected>');
    return h.slice(0,i)+s+h.slice(j);
  }
  TW2.screenerOrder=function(m,st){return m.ord?m.ord[st.sort+':'+st.order]:[]};
  TW2.screenerMain=function(D,st,pre){
    pre=pre||'../';
    var m=D.masks[st.mask];if(!m) return null;
    var PS=D.page_size,n=m.n,pages=Math.max(1,Math.ceil(n/PS)),page=Math.min(Math.max(1,st.page),pages);
    var form=patchSelect(patchSelect(m.form,'sort',st.sort),'order',st.order);
    var h='<section class="card" id="results">\n  <h2>結果 <small>'+n+' 檔／檢查 '+esc(m.U)+' 檔｜交易日 '+esc(m.td)+
      (pages>1?'｜第 '+page+'／'+pages+' 頁（每頁 '+PS+' 檔）':'')+'</small></h2>\n  ';
    if(n){
      h+='<p class="sortbar">排序：';
      Object.keys(D.sorts).forEach(function(k){
        var on=st.sort===k,o=(on&&st.order==='desc')?'asc':'desc';
        h+='<a href="'+esc(pre+'screener/?'+qs(scrPairs(st,{sort:k,order:o})))+'" class="'+(on?'on':'')+'">'+esc(D.sorts[k])+(on?(st.order==='desc'?'↓':'↑'):'')+'</a> ';
      });
      h+='</p>\n  ';
      var idx=TW2.screenerOrder(m,st).slice((page-1)*PS,page*PS);
      h+=m.topen+idx.map(function(i){return m.rows[i]}).join('\n  ')+m.tclose;
    } else h+=m.empty;
    h+='\n  ';
    if(pages>1){
      h+='<nav class="pager" aria-label="分頁" data-testid="screener-pager">';
      for(var i=1;i<=pages;i++) h+=(i===page?'<b>'+i+'</b>':'<a href="'+esc(pre+'screener/?'+qs(scrPairs(st,{page:i}))+'#results')+'">'+i+'</a>')+' ';
      h+='</nav>';
    }
    h+='\n  '+m.src+'</section>';
    return {title:'選股｜台股盤後研究',html:m.a+form+m.mid+h+m.z};
  };
  TW2.screenerCsv=function(D,st){
    var m=D.masks[st.mask];if(!m) return null;
    var rows=TW2.screenerOrder(m,st).map(function(i){return m.csv.rows[i]}).join('');
    return {name:m.csv.name,text:'\ufeff'+m.csv.head+rows+m.csv.tail};
  };

  // ---- 月營收 ----
  TW2.revenueState=function(M,pairs){
    var month=get(pairs,'month')||M.default_month;
    var market=get(pairs,'market');if(market!=='TWSE'&&market!=='TPEx') market='all';
    return {month:month,market:market,high:get(pairs,'high')==='1',sort:get(pairs,'sort')||'symbol',desc:get(pairs,'desc')==='1',
            page:Math.max(1,parseInt(get(pairs,'page')||'1',10)||1)};
  };
  function revPairs(e,st,over){
    var o={month:e.month,market:st.market,high:st.high?'1':'',sort:st.sort,desc:st.desc?'1':'',page:''};
    for(var k in over) o[k]=over[k]==null?'':String(over[k]);
    var p=[];for(var k2 in o) if(o[k2]!=='') p.push([k2,o[k2]]);return p;
  }
  TW2.revenueSelect=function(e,st){
    if(!e.rows) return [];
    var ord=e.ord[st.sort+':'+(st.desc?1:0)]||[];
    return ord.filter(function(i){return (st.market==='all'||e.mk[i]===st.market)&&(!st.high||e.hi[i]==='true')});
  };
  TW2.revenueMain=function(e,st,pre){
    pre=pre||'../';
    if(!(st.sort in e.labels)) st.sort='symbol';
    var form=e.form;
    var i=form.indexOf('<select name="market">'),j=form.indexOf('</select>',i);
    if(i>=0){var s=form.slice(i,j).replace(/ selected>/g,'>').replace('value="'+st.market+'">','value="'+st.market+'" selected>');form=form.slice(0,i)+s+form.slice(j)}
    if(st.high) form=form.replace('name="high" value="1">','name="high" value="1" checked>');
    form=form.replace('<input type="hidden" name="sort" value="symbol">','<input type="hidden" name="sort" value="'+esc(st.sort)+'">'+(st.desc?'<input type="hidden" name="desc" value="1">':''));
    if(e.status==='error') return {title:'月營收追蹤｜台股盤後研究',html:e.a+form+e.b};
    var sel=TW2.revenueSelect(e,st),total=sel.length,PS=e.page_size,pages=Math.max(1,Math.ceil(total/PS)),page=Math.min(Math.max(1,st.page),pages);
    var href=function(over){return esc(pre+'revenue/?'+qs(revPairs(e,st,over)))};
    var cnt=e.cnt.replace(/(<span class="nw">近 12 個月新高 <a href=")[^"]*(")/,'$1'+href({high:'1'})+'$2');
    var note='';
    if(st.high||st.market!=='all'){
      note='<p class="state state-nondefault rev-filtered" data-state="revenue-filtered"><span aria-hidden="true">⚑</span> 篩選中：'+
        (st.market!=='all'?'市場＝'+(st.market==='TWSE'?'上市':'上櫃'):'')+(st.market!=='all'&&st.high?'、':'')+(st.high?'只看近 12 個月新高':'')+
        '，共 '+total+' 列。<a href="'+esc(pre+'revenue/?'+qs([['month',e.month]]))+'">清除篩選</a></p>\n';
    }
    var h;
    if(e.have===0){h=e.have_static}
    else{
      h='<section class="card" id="have" aria-labelledby="have-h">\n  <h2 id="have-h">已有營收（'+total+' 列'+(pages>1?'，第 '+page+'／'+pages+' 頁':'')+'）</h2>\n  ';
      if(!total) h+='<div class="empty" data-state="revenue-filtered-empty">目前篩選條件下 0 列。</div>\n  ';
      else{
        var th=function(k,cls){var on=st.sort===k;
          return '<th'+(cls?' class="'+cls+'"':'')+(on?' aria-sort="'+(st.desc?'descending':'ascending')+'"':'')+'><a href="'+
            href({sort:k,desc:(on&&st.desc)?'':(on?'1':'')})+'">'+esc(e.labels[k])+(on?(st.desc?' ↓':' ↑'):'')+'</a></th>'};
        h+='<div class="table-wrap sticky-first rev-wrap" tabindex="0" role="region" aria-label="已有營收表，可左右捲動"><table class="data rev-table" data-testid="revenue-table">\n    <thead><tr>\n      '+
          th('symbol')+'<th>名稱</th>'+th('market')+th('revenue','r')+th('prev_year','r')+th('mom','r')+th('yoy','r')+th('cum','r')+th('cum_yoy','r')+th('high')+'<th>公司差異說明</th>\n    </tr></thead>\n    <tbody>\n    '+
          sel.slice((page-1)*PS,page*PS).map(function(x){return e.rows[x]}).join('\n    ')+'\n    </tbody>\n  </table></div>\n  <p class="scroll-hint" aria-hidden="true">← 左右滑動看更多欄 →</p>\n  ';
        if(pages>1){h+='<nav class="pager" aria-label="分頁" data-testid="revenue-pager">';for(var p=1;p<=pages;p++) h+=(p===page?'<b>'+p+'</b>':'<a href="'+href({page:p})+'">'+p+'</a>')+' ';h+='</nav>\n  '}
      }
      h+='</section>';
    }
    return {title:'月營收追蹤｜台股盤後研究',html:e.a+form+e.gap+cnt+'\n\n'+note+e.mid+h+e.z};
  };
  TW2.revenueCsv=function(e,st){
    if(!(st.sort in e.labels)) st.sort='symbol';
    var first='# 營收月份 '+e.month+'｜'+e.universe_text+'｜市場 '+({TWSE:'上市',TPEx:'上櫃'}[st.market]||'全部')+
      '｜只看近 12 個月新高 '+(st.high?'是':'否')+'｜排序 '+e.labels[st.sort]+(st.desc?'（降冪）':'');
    var rows=TW2.revenueSelect(e,st).map(function(i){return e.csvrows[i]}).join('');
    return {name:e.csvname,text:'\ufeff'+csvCell(first)+'\r\n'+e.csvhead+rows};
  };

  if(typeof module!=='undefined'&&module.exports){module.exports=TW2}
  root.TW2=TW2;

  // ---------------- 瀏覽器端 ----------------
  if(typeof document==='undefined') return;
  var box=document.getElementById('v2-root');if(!box) return;
  var meta=document.querySelector('meta[name="site-root"]');var base=meta?meta.getAttribute('content'):'../';
  var kind=box.getAttribute('data-v2'),pairs=parse(location.search);
  function load(url){
    return fetch(base+url).then(function(r){
      if(!r.ok) throw new Error('HTTP '+r.status);
      if(/\.gz$/.test(url)){
        if(typeof DecompressionStream==='undefined') throw new Error('瀏覽器不支援解壓縮（DecompressionStream），請更新瀏覽器');
        return new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).text().then(JSON.parse);
      }
      return r.json();
    });
  }
  function fail(t){if(!box.parentNode) return;box.innerHTML='<div class="empty" data-state="v2-error" role="alert">'+esc(t)+' <a href="'+esc(base)+'">回首頁</a></div>'}
  var scr=(box.getAttribute('data-v2-scripts')||'').split(' ').filter(Boolean);
  function scripts(){
    return scr.reduce(function(p,s){return p.then(function(){return new Promise(function(ok){var e=document.createElement('script');e.src=base+s;e.onload=e.onerror=function(){ok()};document.body.appendChild(e)})})},Promise.resolve());
  }
  function download(c){
    if(!c){return}
    var b=new Blob([c.text],{type:'text/csv;charset=utf-8'}),a=document.createElement('a');
    a.href=URL.createObjectURL(b);a.download=c.name;document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},1000);
  }
  function show(v,csv){
    if(!v){fail('公開靜態版沒有這個網址的內容（可能是未匯出的組合）。');return}
    document.title=v.title;
    var tmp=document.createElement('div');tmp.innerHTML=v.html;
    var frag=document.createDocumentFragment();while(tmp.firstChild) frag.appendChild(tmp.firstChild);
    box.parentNode.insertBefore(frag,box);box.parentNode.removeChild(box);
    document.querySelectorAll('a[data-v2-csv]').forEach(function(a){
      if(!csv) return;
      a.addEventListener('click',function(ev){ev.preventDefault();download(csv())});
    });
    document.querySelectorAll('form[data-v2-form]').forEach(function(f){
      f.addEventListener('submit',function(ev){
        ev.preventDefault();var p=[];
        new FormData(f).forEach(function(v,k){if(typeof v==='string'&&v!==''&&k!=='adv') p.push([k,v])});
        location.href=f.getAttribute('action')+(p.length?'?'+qs(p):'');
      });
    });
    scripts().then(function(){
      if(location.hash){var t=document.getElementById(location.hash.slice(1));if(t) t.scrollIntoView()}
      if(get(pairs,'csv')==='1'&&csv) download(csv());
    });
  }
  var p;
  if(kind==='stock'){
    var code=get(pairs,'code')||'';
    if(!/^[A-Za-z0-9]+$/.test(code)){fail('網址缺少股票代號。');p=null}
    else p=load('data/stock/'+code+'.json.gz').then(function(d){show(TW2.stockMain(d,pairs))},function(){fail('公開靜態版沒有 '+code+' 的個股頁（不在追蹤範圍或尚未匯出）。')});
  } else if(kind==='source'){
    p=load('manifest.json').then(function(M){return load('data/source/'+TW2.sourceShard(pairs,M.g_shards)+'.json.gz')})
      .then(function(sh){show(TW2.sourceMain(sh,pairs))},function(){fail('公開靜態版沒有這筆來源明細。')});
  } else if(kind==='screener'){
    p=load('data/screener.json.gz').then(function(D){var st=TW2.screenerState(D,pairs);show(TW2.screenerMain(D,st,base),function(){return TW2.screenerCsv(D,st)})},function(e){fail('選股資料讀取失敗（'+e.message+'）。')});
  } else if(kind==='revenue'){
    p=load('manifest.json').then(function(M){var st=TW2.revenueState(M,pairs);
      if(M.months.indexOf(st.month)<0){fail('公開靜態版沒有 '+st.month+' 的月營收。');return}
      return load('data/revenue/'+st.month+'.json.gz').then(function(e){show(TW2.revenueMain(e,st,base),function(){return TW2.revenueCsv(e,st)})})}).catch(function(e){fail('月營收資料讀取失敗（'+e.message+'）。')});
  }
})(typeof window!=='undefined'?window:globalThis);
