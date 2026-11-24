/* 自選股清單：只寫入這個瀏覽器的 localStorage，不呼叫站外服務。 */
(function(root, factory){
  'use strict';
  var api = factory();
  if(typeof module!=='undefined' && module.exports) module.exports = api;
  root.TWSITE_WATCH = api;
  if(typeof document!=='undefined'){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', api.boot);
    else api.boot();
  }
})(typeof window!=='undefined'?window:globalThis, function(){
  'use strict';
  var KEY='twsite.watchlist.v1';
  var MINUS='\u2212';

  function norm(code){
    return String(code||'').trim().toUpperCase();
  }
  function validCode(code){
    return /^[0-9A-Z]{3,8}$/.test(code);
  }
  function parseList(text){
    var raw=String(text||'').replace(/\uFEFF/g,'').replace(/[，、；;]/g,',').replace(/\s+/g,'\n');
    var out=[], seen={};
    raw.split(/[\n,]+/).forEach(function(line){
      var tok=norm(line).split(/[\/\s|]+/)[0];
      if(!validCode(tok)||seen[tok]) return;
      seen[tok]=1; out.push(tok);
    });
    return out;
  }
  function load(){
    try{
      var raw=localStorage.getItem(KEY);
      if(!raw) return [];
      var data=JSON.parse(raw);
      if(Array.isArray(data)) return parseList(data.join('\n'));
      if(data && Array.isArray(data.codes)) return parseList(data.codes.join('\n'));
    }catch(e){}
    return [];
  }
  function save(codes){
    localStorage.setItem(KEY, JSON.stringify(parseList(codes.join('\n'))));
  }
  function has(code){
    code=norm(code);
    return load().indexOf(code)>=0;
  }
  function add(code){
    code=norm(code); if(!validCode(code)) return load();
    var cur=load();
    if(cur.indexOf(code)<0){ cur.push(code); save(cur); }
    return cur;
  }
  function remove(code){
    code=norm(code);
    var cur=load().filter(function(c){return c!==code});
    save(cur); return cur;
  }
  function toggle(code){
    return has(code)?remove(code):add(code);
  }

  function rootUrl(){
    var meta=document.querySelector('meta[name="site-root"]');
    return meta?meta.getAttribute('content')||'./':'/';
  }
  function watchApi(){
    var r=rootUrl();
    return r==='/'?'/api/watchlist':r+'api/watchlist.json';
  }
  function absHref(h){
    if(!h) return '';
    if(h.charAt(0)==='/' && h.charAt(1)!=='/') return h;
    return rootUrl()+h;
  }
  function stockUrl(code, rec){
    if(rec&&rec.href) return absHref(rec.href);
    var r=rootUrl();
    if(r==='/') return '/stock/'+encodeURIComponent(code);
    return r+'stock/?code='+encodeURIComponent(code);
  }
  function modeUrl(id, rec, bundle){
    var p=bundle&&bundle.links&&bundle.links.modes&&bundle.links.modes[id];
    if(p) return absHref(p)+'#results';
    var r=rootUrl();
    if(r==='/') return '/hub?mode='+encodeURIComponent(id)+'#results';
    return r+'hub/?mode='+encodeURIComponent(id)+'#results';
  }

  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function miss(text){
    return '<span class="muted">'+esc(text||'資料不足')+'</span>';
  }
  function fmtNum(v, digits){
    if(v==null||v==='') return '';
    var n=Number(v); if(!isFinite(n)) return '';
    return n.toLocaleString('zh-Hant-TW', {minimumFractionDigits:digits, maximumFractionDigits:digits});
  }
  function fmtChg(v, pct){
    if(v==null||v==='') return miss('資料不足');
    var n=Number(v); if(!isFinite(n)) return miss('資料不足');
    var d=n>0?'up':n<0?'down':'flat';
    var arr=n>0?'▲':n<0?'▼':'■';
    var sr=n>0?'上漲':n<0?'下跌':'平盤';
    var sign=n>0?'+':n<0?MINUS:'';
    var body=sign+fmtNum(Math.abs(n), 2)+(pct?'%':'');
    return '<span class="chg '+d+'"><span class="arr" aria-hidden="true">'+arr+'</span><span class="sr">'+sr+'</span>'+body+'</span>';
  }
  function sortVal(v){
    return (v==null||v==='')?'':String(v);
  }

  var dataCache=null, dataWait=null;
  function loadData(){
    if(dataCache) return Promise.resolve(dataCache);
    if(dataWait) return dataWait;
    dataWait=fetch(watchApi(), {cache:'no-store'}).then(function(r){
      if(!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function(d){ dataCache=d||{stocks:{}}; return dataCache; });
    return dataWait;
  }

  function bindButton(slot){
    var code=norm(slot.getAttribute('data-watch-code'));
    if(!validCode(code)) return;
    var name=slot.getAttribute('data-watch-name')||'';
    var btn=document.createElement('button');
    btn.type='button';
    btn.className='btn watch-toggle';
    btn.setAttribute('data-testid','watch-toggle');
    function paint(){
      var on=has(code);
      btn.textContent=on?'已在自選，移除':'加入自選';
      btn.setAttribute('aria-pressed', on?'true':'false');
      btn.classList.toggle('on', on);
    }
    btn.addEventListener('click', function(){
      toggle(code);
      paint();
      var ev=document.createEvent('Event'); ev.initEvent('twsite-watchlist-change', true, true);
      document.dispatchEvent(ev);
    });
    paint();
    slot.appendChild(btn);
    if(name) btn.setAttribute('title', (has(code)?'從自選移除':'加入自選')+' '+code+' '+name);
  }

  function rowHtml(code, rec, asOf, yoyMonth, bundle){
    if(!rec){
      return '<tr class="watch-missing" data-code="'+esc(code)+'">'+
        '<td data-sort="'+esc(code)+'"><span class="nmc"><strong>'+esc(code)+'</strong></span>'+
        ' <span class="pill pill-warn">未收錄</span><div class="muted">本站沒有這檔股票的資料，沒有個股頁。</div></td>'+
        '<td class="r" data-sort="">'+miss('資料不足')+'</td>'+
        '<td class="r" data-sort="">'+miss('資料不足')+'</td>'+
        '<td class="r" data-sort="">'+miss('資料不足')+'</td>'+
        '<td data-sort="">'+miss('資料不足')+'</td>'+
        '<td class="r" data-sort="">'+miss('資料不足')+'</td>'+
        '<td><button type="button" class="btn" data-watch-remove="'+esc(code)+'">移除</button></td></tr>';
    }
    var name=rec.name||code;
    var title=esc(code+' '+name);
    var head;
    if(rec.has_page){
      head='<a class="nmc" href="'+esc(stockUrl(code, rec))+'" title="'+title+'"><strong>'+esc(name)+'</strong> <span class="nc">'+esc(code)+'</span></a>';
      if(rec.market&&rec.market!=='—') head+='<div class="muted">'+esc(rec.market)+'</div>';
    }else{
      head='<span class="nmc" title="'+title+'"><strong>'+esc(name)+'</strong> <span class="nc">'+esc(code)+'</span></span>'+
        ' <span class="pill pill-warn">未收錄個股頁</span>'+
        '<div class="muted">'+(rec.market&&rec.market!=='—' ?esc(rec.market)+'｜':'')+'這一版沒有個股頁，不提供連結。</div>';
    }
    var closeTxt=rec.close==null?miss('資料不足'):esc(fmtNum(rec.close, 2));
    var chgCell, chgSort;
    if(rec.no_compare && rec.change==null){
      chgCell=miss('無比價'); chgSort='';
    }else{
      chgCell=fmtChg(rec.change, false); chgSort=sortVal(rec.change);
    }
    var pctCell=fmtChg(rec.change_pct, true);
    var modes='';
    if(rec.modes&&rec.modes.length){
      modes='<ul class="sm-list">'+rec.modes.map(function(m){
        var href=modeUrl(m.id, rec, bundle);
        return '<li><a href="'+esc(href)+'"><span class="mcode">'+esc(m.id)+'</span> '+esc(m.name)+'</a> <span class="tier '+esc(m.tier_class||'t1')+'"><span class="sr">等級：</span>'+esc(m.tier||'基本')+'</span></li>';
      }).join('')+'</ul>';
    }else{
      modes=miss('今天沒有符合的篩選模式');
    }
    var yoyCell, yoySort;
    if(rec.yoy!=null){
      yoyCell=fmtChg(rec.yoy, true);
      if(yoyMonth||rec.yoy_month) yoyCell+='<div class="muted">'+esc(rec.yoy_month||yoyMonth)+'</div>';
      yoySort=sortVal(rec.yoy);
    }else{
      yoyCell=miss(rec.yoy_text||'資料不足');
      yoySort='';
    }
    return '<tr data-code="'+esc(code)+'">'+
      '<td data-sort="'+esc(code)+'">'+head+'</td>'+
      '<td class="r" data-sort="'+esc(sortVal(rec.close))+'">'+closeTxt+'</td>'+
      '<td class="r" data-sort="'+esc(chgSort)+'">'+chgCell+'</td>'+
      '<td class="r" data-sort="'+esc(sortVal(rec.change_pct))+'">'+pctCell+'</td>'+
      '<td data-sort="'+esc((rec.modes||[]).map(function(m){return m.id}).join(','))+'">'+modes+'</td>'+
      '<td class="r" data-sort="'+esc(yoySort)+'">'+yoyCell+'</td>'+
      '<td><button type="button" class="btn" data-watch-remove="'+esc(code)+'">移除</button></td></tr>';
  }

  function labelCells(table){
    if(!table||!table.tHead) return;
    var heads=Array.prototype.map.call(table.tHead.rows[0].cells, function(th){
      return th.textContent.replace(/[↕↑↓]/g,'').trim();
    });
    Array.prototype.forEach.call(table.tBodies, function(tb){
      Array.prototype.forEach.call(tb.rows, function(tr){
        Array.prototype.forEach.call(tr.cells, function(td,i){
          if(heads[i]) td.setAttribute('data-label', heads[i]);
        });
      });
    });
  }

  function renderPage(bundle){
    var page=document.querySelector('[data-testid="watchlist-page"]');
    if(!page) return;
    var codes=load();
    var tools=page.querySelector('[data-watch-tools]');
    var empty=page.querySelector('[data-watch-empty]');
    var body=page.querySelector('[data-watch-body]');
    var rows=page.querySelector('[data-watch-rows]');
    var sum=page.querySelector('[data-watch-sum]');
    var msg=page.querySelector('[data-watch-msg]');
    if(tools) tools.hidden=false;
    var stocks=(bundle&&bundle.stocks)||{};
    if(!codes.length){
      if(empty) empty.hidden=false;
      if(body) body.hidden=true;
      if(rows) rows.innerHTML='';
      return;
    }
    if(empty) empty.hidden=true;
    if(body) body.hidden=false;
    var asOf=bundle&&bundle.as_of;
    var yoyMonth=bundle&&bundle.yoy_month;
    var missing=0, nopage=0;
    rows.innerHTML=codes.map(function(c){
      var rec=stocks[c];
      if(!rec) missing++;
      else if(!rec.has_page) nopage++;
      return rowHtml(c, rec, asOf, yoyMonth, bundle);
    }).join('');
    labelCells(page.querySelector('table.watch-table'));
    var bits=['目前 '+codes.length+' 檔'];
    if(asOf&&asOf!=='—') bits.push('行情日期 '+asOf);
    if(yoyMonth) bits.push('營收月份 '+yoyMonth);
    if(nopage) bits.push(nopage+' 檔未收錄個股頁（仍顯示已有的數字）');
    if(missing) bits.push(missing+' 檔未收錄（沒有資料、沒有連結）');
    if(sum) sum.textContent=bits.join('｜');
    if(msg) /* keep last action */;
    rows.querySelectorAll('[data-watch-remove]').forEach(function(b){
      b.addEventListener('click', function(){
        remove(b.getAttribute('data-watch-remove'));
        renderPage(bundle);
      });
    });
  }

  function bindTools(bundle){
    var page=document.querySelector('[data-testid="watchlist-page"]');
    if(!page||page.getAttribute('data-watch-bound')==='1') return;
    page.setAttribute('data-watch-bound','1');
    var msg=page.querySelector('[data-watch-msg]');
    function tell(t){ if(msg) msg.textContent=t||''; }
    var exp=page.querySelector('[data-watch-export]');
    if(exp) exp.addEventListener('click', function(){
      var codes=load();
      if(!codes.length){ tell('目前沒有自選股可匯出。'); return; }
      var blob=new Blob([codes.join('\n')+'\n'], {type:'text/plain;charset=utf-8'});
      var a=document.createElement('a');
      a.href=URL.createObjectURL(blob);
      a.download='watchlist.txt';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
      tell('已匯出 '+codes.length+' 個代號（只在這個瀏覽器下載，沒有上傳）。');
    });
    var file=page.querySelector('[data-watch-import]');
    if(file) file.addEventListener('change', function(){
      var f=file.files&&file.files[0];
      file.value='';
      if(!f) return;
      var reader=new FileReader();
      reader.onload=function(){
        var added=mergeIncoming(String(reader.result||''), bundle);
        tell(added);
      };
      reader.readAsText(f);
    });
    var pasteBtn=page.querySelector('[data-watch-import-text]');
    var dlg=page.querySelector('[data-watch-dialog]');
    var ta=page.querySelector('[data-watch-paste]');
    if(pasteBtn&&dlg){
      pasteBtn.addEventListener('click', function(){
        if(ta) ta.value=load().join('\n');
        if(dlg.showModal) dlg.showModal(); else dlg.setAttribute('open','');
      });
      dlg.addEventListener('close', function(){
        if(dlg.returnValue==='ok'){
          tell(mergeIncoming(ta?ta.value:'', bundle));
        }
      });
    }
    function mergeIncoming(text, bundleNow){
      var incoming=parseList(text);
      if(!incoming.length) return '沒有讀到有效代號。';
      var cur=load(), seen={};
      cur.forEach(function(c){ seen[c]=1; });
      var n=0;
      incoming.forEach(function(c){ if(!seen[c]){ cur.push(c); seen[c]=1; n++; } });
      save(cur);
      renderPage(bundleNow);
      return '已讀取 '+incoming.length+' 個代號，新增 '+n+' 檔（清單仍只存在這個瀏覽器）。';
    }
  }

  function boot(){
    document.querySelectorAll('.watch-slot[data-watch-code]').forEach(bindButton);
    var page=document.querySelector('[data-testid="watchlist-page"]');
    if(!page) return;
    loadData().then(function(bundle){
      bindTools(bundle);
      renderPage(bundle);
    }).catch(function(){
      bindTools({stocks:{}});
      var msg=page.querySelector('[data-watch-msg]');
      if(msg) msg.textContent='讀取行情清單失敗，仍可看到你已加入的代號；數字會顯示為資料不足。';
      renderPage({stocks:{}});
    });
  }

  return {KEY:KEY, parseList:parseList, load:load, save:save, has:has, add:add, remove:remove, toggle:toggle,
          validCode:validCode, norm:norm, boot:boot};
});
