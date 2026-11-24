// 公開靜態版（GitHub Pages）專用：只讀本站匯出的 manifest.json／api/stocks.json，不呼叫外部服務。
(function(){
  'use strict';
  var meta=document.querySelector('meta[name="site-root"]'); if(!meta) return;
  var root=meta.getAttribute('content')||'./';
  var manifest=null;
  function load(){
    if(manifest) return Promise.resolve(manifest);
    return fetch(root+'manifest.json').then(function(r){if(!r.ok) throw new Error(r.status);return r.json()}).then(function(m){manifest=m;return m});
  }
  function msg(form,text){
    var p=form.querySelector('.static-msg');
    if(!p){p=document.createElement('p');p.className='static-msg';p.setAttribute('role','status');form.appendChild(p)}
    p.textContent=text;
  }
  function key(path,fd){
    var pairs=[];
    fd.forEach(function(v,k){ if(v!=='' && typeof v==='string') pairs.push([k,v]); });
    pairs.sort(function(a,b){return a[0]<b[0]?-1:a[0]>b[0]?1:(a[1]<b[1]?-1:a[1]>b[1]?1:0)});
    var q=pairs.map(function(p){return encodeURIComponent(p[0])+'='+encodeURIComponent(p[1]).replace(/%20/g,'+')}).join('&');
    return path+(q?'?'+q:'');
  }
  // 選股／營收表單：查預先算好的頁面
  document.querySelectorAll('form[data-static-path]').forEach(function(f){
    var path=f.getAttribute('data-static-path'); if(path==='/search') return;
    f.addEventListener('submit',function(e){
      e.preventDefault();
      var k=key(path,new FormData(f));
      load().then(function(m){
        var file=m.routes[k];
        if(file){ location.href=root+file; }
        else msg(f,'公開靜態版沒有預先產生這組條件的結果；請改用其他組合，或回到預設。');
      }).catch(function(){ msg(f,'讀取靜態版索引失敗，請重新整理頁面。'); });
    });
  });
  // 搜尋：代號完全相符或名稱唯一相符時直接前往個股頁；多筆時列出
  var sf=document.querySelector('form.search');
  if(sf){
    sf.addEventListener('submit',function(e){
      e.preventDefault();
      var q=(sf.querySelector('input[name="q"]').value||'').trim();
      var old=sf.querySelector('.static-search-list'); if(old) old.remove();
      if(!q){ msg(sf,'請輸入代號或名稱。'); return; }
      Promise.all([load(),fetch(root+'api/stocks.json').then(function(r){return r.json()})]).then(function(a){
        var m=a[0], list=a[1];
        var hit=list.filter(function(s){return s.code===q});
        if(!hit.length) hit=list.filter(function(s){return s.code.indexOf(q)===0||s.name.indexOf(q)>=0});
        var np=m.nopage||{};
        function url(c){return m.stocks[c]?root+m.stocks[c].replace(/index\.html$/,''):root+'stock/?code='+encodeURIComponent(c)}
        hit=hit.filter(function(s){return m.stocks[s.code]||Object.prototype.hasOwnProperty.call(np,s.code)});
        if(hit.length===1&&m.stocks[hit[0].code]){ location.href=url(hit[0].code); return; }
        if(!hit.length){ msg(sf,'查無「'+q+'」：公開靜態版只收錄追蹤範圍內的股票。'); return; }
        var ul=document.createElement('ul'); ul.className='static-search-list';
        hit.slice(0,30).forEach(function(s){var li=document.createElement('li'),a2=document.createElement('a');a2.href=url(s.code);a2.textContent=s.code+' '+s.name;
          if(!m.stocks[s.code]){a2.setAttribute('data-nopage','1');a2.title='在追蹤範圍內，這一版未收錄個股頁'}
          li.appendChild(a2);ul.appendChild(li)});
        sf.appendChild(ul);
      }).catch(function(){ msg(sf,'讀取股票清單失敗，請重新整理頁面。'); });
    });
  }
})();
// 本版是否落後：依台北時間與 build-info.json 的交易日清單（與網站 freshness.expected_for 同規則：交易日 18:30 後才算當日）
(function(){
  'use strict';
  var meta=document.querySelector('meta[name="site-root"]'); if(!meta) return;
  var root=meta.getAttribute('content')||'./';
  function expected(days, ready, nowMs){
    var t=new Date(nowMs+8*3600e3), today=t.toISOString().slice(0,10), hm=t.getUTCHours()*60+t.getUTCMinutes();
    var r=(ready||'18:30').split(':'), rm=(+r[0])*60+(+r[1]);
    if(!days||!days.length||days[days.length-1]<today) return null;   // 交易日清單不夠新：不判斷
    var past=days.filter(function(d){return d<=today}).sort();
    if(past.length&&past[past.length-1]===today&&hm<rm) past.pop();
    return past.length?past[past.length-1]:null;
  }
  window.__twsiteExpected=expected;
  fetch(root+'build-info.json',{cache:'no-store'}).then(function(r){return r.json()}).then(function(b){
    var g=document.querySelector('[data-build-generated]'); if(g) g.textContent=b.generated_at_taipei||'—';
    var exp=expected(b.trade_days,b.quotes_ready,Date.now());
    if(!exp||!b.data_date||b.data_date>=exp) return;
    var msg='本版資料日期 '+b.data_date+' 落後預期資料日 '+exp+'：最近一次發布可能未通過自檢或尚未完成，公開站停在上一版；請以官方資料為準。';
    var ban=document.querySelector('.static-banner');
    if(ban){var s=document.createElement('span');s.className='pill pill-warn static-stale';s.setAttribute('data-state','static-stale');s.setAttribute('role','alert');s.textContent='⚠ '+msg;ban.appendChild(s)}
    var line=document.querySelector('[data-build-stale-line]');
    if(line){line.hidden=false;line.setAttribute('data-state','static-stale');line.textContent='⚠ '+msg}
  }).catch(function(){});
})();
