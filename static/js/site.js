// 本站介面腳本：只呼叫本站 /api/stocks，不呼叫外部服務。
(function(){
  'use strict';
  // 1) 搜尋框自動完成
  var dl=document.getElementById('stocklist');
  if(dl){
    var rootMeta=document.querySelector('meta[name="site-root"]');
    fetch(rootMeta?rootMeta.getAttribute('content')+'api/stocks.json':'/api/stocks').then(function(r){return r.json()}).then(function(list){
      var frag=document.createDocumentFragment();
      list.forEach(function(s){var o=document.createElement('option');o.value=s.code;o.textContent=s.code+' '+s.name;frag.appendChild(o)});
      dl.appendChild(frag);
    }).catch(function(){});
  }

  // 2) 按「/」聚焦搜尋（輸入框內不攔截）
  var q=document.getElementById('site-q');
  document.addEventListener('keydown',function(e){
    if(e.key!=='/'||e.ctrlKey||e.metaKey||e.altKey||!q) return;
    var t=e.target, tag=(t&&t.tagName||'').toLowerCase();
    if(tag==='input'||tag==='textarea'||tag==='select'||(t&&t.isContentEditable)) return;
    e.preventDefault(); q.focus(); q.select();
  });

  // 3) 日期加星期（只是顯示，不改資料）
  var WD='日一二三四五六';
  document.querySelectorAll('[data-date]').forEach(function(el){
    var m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(el.getAttribute('data-date')||''); if(!m) return;
    var d=new Date(Date.UTC(+m[1],+m[2]-1,+m[3]));
    if(el.textContent.indexOf('（')<0) el.textContent=el.textContent+'（'+WD[d.getUTCDay()]+'）';
  });

  // 4) 表格排序：table[data-sortable]，欄位 th[data-key]，儲存格 td[data-sort]
  document.querySelectorAll('table[data-sortable]').forEach(function(tbl){
    var tbody=tbl.tBodies[0]; if(!tbody) return;
    var ths=Array.prototype.slice.call(tbl.tHead.rows[0].cells);
    var live=document.createElement('p'); live.className='sr'; live.setAttribute('aria-live','polite');
    tbl.parentNode.insertBefore(live,tbl.nextSibling);
    ths.forEach(function(th,idx){
      if(!th.hasAttribute('data-key')) return;
      var label=th.textContent.trim();
      var b=document.createElement('button'); b.type='button'; b.className='sort';
      while(th.firstChild) b.appendChild(th.firstChild);
      th.appendChild(b);
      b.addEventListener('click',function(){
        var cur=th.getAttribute('aria-sort');
        var text=th.getAttribute('data-type')==='text';
        var dir=cur==='descending'?'ascending':cur==='ascending'?'descending':(text?'ascending':'descending');
        ths.forEach(function(o){o.removeAttribute('aria-sort')});
        th.setAttribute('aria-sort',dir);
        var rows=Array.prototype.slice.call(tbody.rows);
        rows.sort(function(a,b2){
          var x=a.cells[idx].getAttribute('data-sort'), y=b2.cells[idx].getAttribute('data-sort');
          var xe=(x===null||x===''), ye=(y===null||y==='');
          if(xe||ye) return xe===ye?0:(xe?1:-1);   // 缺資料永遠排最後
          var r=text?x.localeCompare(y,'zh-Hant'):(parseFloat(x)-parseFloat(y));
          return dir==='ascending'?r:-r;
        });
        rows.forEach(function(r){tbody.appendChild(r)});
        live.textContent='已依「'+label+'」'+(dir==='ascending'?'由小到大':'由大到小')+'排序';
      });
    });
  });
  // 5) 手機堆疊表格：把欄名複製到每格的 data-label（HTML 不變；只影響 ≤640px 的顯示）
  document.querySelectorAll('table.stack, table.result-cards').forEach(function(tbl){
    if(!tbl.tHead) return;
    var heads=Array.prototype.map.call(tbl.tHead.rows[0].cells,function(th){return th.textContent.replace(/[↕↑↓]/g,'').trim()});
    Array.prototype.forEach.call(tbl.tBodies,function(tb){
      Array.prototype.forEach.call(tb.rows,function(tr){
        Array.prototype.forEach.call(tr.cells,function(td,i){ if(heads[i] && !td.hasAttribute('data-label')) td.setAttribute('data-label',heads[i]); });
      });
    });
  });

  // 5b) 年增率附註（當月營收為負）：按鈕展開／收合說明；Enter／Space／點按皆可（設計 §19）
  document.querySelectorAll('.neg-note .neg-btn').forEach(function(b){
    var box=document.getElementById(b.getAttribute('aria-controls')); if(!box) return;
    box.hidden=true; b.setAttribute('aria-expanded','false');
    b.addEventListener('click',function(){
      var open=b.getAttribute('aria-expanded')!=='true';
      b.setAttribute('aria-expanded',open?'true':'false'); box.hidden=!open;
    });
  });

  // 6) 選股結果卡片：「更多欄位」展開／收合（按鈕，鍵盤 Enter／Space 可操作）
  document.querySelectorAll('button[data-more]').forEach(function(b){
    b.addEventListener('click',function(){
      var tr=b.closest('tr'); var open=!tr.classList.contains('open');
      tr.classList.toggle('open',open); b.setAttribute('aria-expanded',open?'true':'false');
      b.textContent=open?'收合欄位':'更多欄位';
    });
  });
})();

/* R4：選股參數改動後提示「說明仍是已套用參數」——不在前端產生第二份白話說明 */
(function(){
  var f=document.querySelector('form.params'); if(!f) return;
  var out=f.querySelector('[data-testid="params-dirty"]'); if(!out) return;
  var init=new URLSearchParams(new FormData(f)).toString();
  function chk(){
    var now=new URLSearchParams(new FormData(f)).toString();
    var dirty=now!==init;
    out.textContent=dirty?'參數已修改：上方條件說明仍是目前已套用的參數，按「重新篩選」後更新。':'';
    f.classList.toggle('is-dirty',dirty);
  }
  f.addEventListener('input',chk); f.addEventListener('change',chk);
})();
