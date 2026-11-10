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
        hit=hit.filter(function(s){return m.stocks[s.code]});
        if(hit.length===1){ location.href=root+m.stocks[hit[0].code].replace(/index\.html$/,''); return; }
        if(!hit.length){ msg(sf,'查無「'+q+'」：公開靜態版只收錄追蹤範圍內的股票。'); return; }
        var ul=document.createElement('ul'); ul.className='static-search-list';
        hit.slice(0,30).forEach(function(s){var li=document.createElement('li'),a2=document.createElement('a');a2.href=root+m.stocks[s.code].replace(/index\.html$/,'');a2.textContent=s.code+' '+s.name;li.appendChild(a2);ul.appendChild(li)});
        sf.appendChild(ul);
      }).catch(function(){ msg(sf,'讀取股票清單失敗，請重新整理頁面。'); });
    });
  }
})();
