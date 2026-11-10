// 個股 K 線＋MA20/MA60＋成交量（lightweight-charts 4.2.3，本地檔案）
// 色彩依 docs/design/TOKENS.md：漲 #c4261d、跌 #167a46（白底皆 ≥ 4.5:1）；日期軸顯示交易日，價格單位元、成交量單位股。
(function(){
  var el=document.getElementById('chart'), raw=document.getElementById('chart-data');
  if(!el||!raw) return;
  var msg=el.querySelector('.chart-msg');
  function fail(t){ if(msg){msg.setAttribute('data-state','error');msg.className='chart-msg is-error';msg.textContent=t;} }
  if(!window.LightweightCharts){ fail('圖表元件無法載入（本地檔案 lightweight-charts），下方月營收與數值不受影響。'); return; }
  var d;
  try{ d=JSON.parse(raw.textContent); }catch(e){ fail('圖表資料格式錯誤，無法繪製。'); return; }
  if(!d.candles||!d.candles.length){ fail('沒有可繪製的行情資料。'); return; }
  if(msg) msg.remove();
  var UP='#c4261d', DOWN='#167a46', WD='日一二三四五六';
  function bd(t){ // time 可能是 'YYYY-MM-DD' 或 {year,month,day}
    if(typeof t==='string'){var m=t.split('-');return {y:+m[0],m:+m[1],d:+m[2]}}
    if(t&&t.year) return {y:t.year,m:t.month,d:t.day};
    var x=new Date(t*1000); return {y:x.getUTCFullYear(),m:x.getUTCMonth()+1,d:x.getUTCDate()};
  }
  function pad(n){return (n<10?'0':'')+n}
  function vol(v){ // 股 → 萬股／億股（只是顯示換算）
    var a=Math.abs(v);
    if(a>=1e8) return (v/1e8).toFixed(2)+'億';
    if(a>=1e4) return Math.round(v/1e4).toLocaleString('zh-TW')+'萬';
    return Math.round(v).toLocaleString('zh-TW');
  }
  var chart=LightweightCharts.createChart(el,{autoSize:true,
    layout:{background:{color:'#ffffff'},textColor:'#3a404c',fontFamily:getComputedStyle(document.body).fontFamily},
    grid:{vertLines:{color:'#f1ede4'},horzLines:{color:'#f1ede4'}},
    rightPriceScale:{scaleMargins:{top:0.06,bottom:0.3},borderColor:'#e2dccf'},
    timeScale:{borderColor:'#e2dccf',
      tickMarkFormatter:function(t,type){var b=bd(t);
        if(type===0) return b.y+'年';
        if(type===1) return b.m+'月';
        return b.m+'/'+b.d;}},
    localization:{locale:'zh-TW',
      timeFormatter:function(t){var b=bd(t),w=new Date(Date.UTC(b.y,b.m-1,b.d)).getUTCDay();
        return b.y+'-'+pad(b.m)+'-'+pad(b.d)+'（'+WD[w]+'）';},
      priceFormatter:function(p){return p.toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2})}}});
  // 台股慣例：漲紅跌綠
  var c=chart.addCandlestickSeries({upColor:UP,downColor:DOWN,borderUpColor:UP,borderDownColor:DOWN,wickUpColor:UP,wickDownColor:DOWN});
  c.setData(d.candles);
  // 除權息／減資日標記（後端已依時間排序；格式見 HANDOFF §13）
  // 設計 §19：圖上只畫形狀、不畫文字（文字會壓到相鄰 K 棒）——已還原 ▼ arrowDown #5a6170；「（未還原）」● circle #7a4a00。
  // 類型文字放在圖下方的「事件列」按鈕（▼ 06/11 除息）；滑過／聚焦／點按時十字線移到該日，並在說明列顯示完整日期與類型。
  var mk=(d.markers||[]).map(function(m){
    var na=/未還原/.test(m.text||'');
    return {time:m.time,position:'aboveBar',shape:na?'circle':'arrowDown',color:na?'#7a4a00':'#5a6170',size:1,_text:m.text||'',_na:na};
  });
  // 標記放在圖頂的獨立「事件軌」（隱形序列、自己的價格軸），永遠在 K 棒上方的留白裡，不會壓到 K 棒或均線
  if(mk.length){
    chart.priceScale('right').applyOptions({scaleMargins:{top:0.12,bottom:0.3}});
    var evtS=chart.addLineSeries({priceScaleId:'evt',lineVisible:false,color:'rgba(0,0,0,0)',lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,pointMarkersVisible:false});
    chart.priceScale('evt').applyOptions({visible:false,scaleMargins:{top:0.02,bottom:0.93}});
    evtS.setData(mk.map(function(m){return {time:m.time,value:1}}));
    if(evtS.setMarkers) evtS.setMarkers(mk.map(function(m){return {time:m.time,position:'inBar',shape:m.shape,color:m.color,size:2}}));
  }
  chart.addLineSeries({color:'#d97706',lineWidth:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}).setData(d.ma20);
  chart.addLineSeries({color:'#24477a',lineWidth:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}).setData(d.ma60);
  var v=chart.addHistogramSeries({priceScaleId:'vol',priceFormat:{type:'custom',formatter:vol,minMove:1},color:'#b9b3a6',lastValueVisible:false,priceLineVisible:false});
  chart.priceScale('vol').applyOptions({scaleMargins:{top:0.76,bottom:0}});
  v.setData(d.volume);
  chart.timeScale().fitContent();
  // ---- 事件列（圖下方）：鍵盤／觸控可用的標記說明 ----
  if(mk.length){
    var close={}; d.candles.forEach(function(k){close[typeof k.time==='string'?k.time:JSON.stringify(k.time)]=k.close});
    var fig=el.closest('figure')||el.parentNode;
    var lane=document.createElement('div'); lane.className='kline-lane'; lane.setAttribute('role','group'); lane.setAttribute('aria-label','圖上除權息／減資標記');
    var info=document.createElement('p'); info.className='kline-lane-info'; info.setAttribute('aria-live','polite');
    var lab=document.createElement('span'); lab.className='kline-lane-l'; lab.textContent='圖上標記'; lane.appendChild(lab);
    function full(t){var b=bd(t),w=new Date(Date.UTC(b.y,b.m-1,b.d)).getUTCDay();return b.y+'-'+pad(b.m)+'-'+pad(b.d)+'（'+WD[w]+'）'}
    function show(m,btn){
      if(chart.setCrosshairPosition) chart.setCrosshairPosition(close[m.time],m.time,c);
      info.textContent=(m._na?'● ':'▼ ')+full(m.time)+' '+m._text+(m._na?'':'（已用於還原）');
      lane.querySelectorAll('.evt-chip').forEach(function(x){x.setAttribute('aria-pressed',x===btn?'true':'false')});
    }
    function hide(){ if(chart.clearCrosshairPosition) chart.clearCrosshairPosition(); }
    mk.forEach(function(m){
      var b=document.createElement('button'); b.type='button'; b.className='evt-chip'+(m._na?' is-na':''); b.setAttribute('aria-pressed','false');
      var x=bd(m.time);
      b.innerHTML='<span aria-hidden="true">'+(m._na?'●':'▼')+'</span> '+pad(x.m)+'/'+pad(x.d)+' ';
      b.appendChild(document.createTextNode(m._text.replace('（未還原）','')));
      if(m._na){var t=document.createElement('small');t.textContent='未還原';b.appendChild(t)}
      b.addEventListener('mouseenter',function(){show(m,b)}); b.addEventListener('focus',function(){show(m,b)});
      b.addEventListener('click',function(){show(m,b)}); b.addEventListener('blur',hide); b.addEventListener('mouseleave',function(){ if(document.activeElement!==b) hide(); });
      lane.appendChild(b);
    });
    // 滑鼠移到圖上標記那天時也顯示說明
    var byT={}; mk.forEach(function(m){byT[m.time]=m});
    chart.subscribeCrosshairMove(function(p){
      if(!p||!p.time) return; var b=bd(p.time), k=b.y+'-'+pad(b.m)+'-'+pad(b.d);
      if(byT[k]) info.textContent=(byT[k]._na?'● ':'▼ ')+full(k)+' '+byT[k]._text+(byT[k]._na?'':'（已用於還原）');
    });
    info.textContent='點按或用 Tab 選擇標記，可在圖上定位該日。';
    var cap=fig.querySelector('figcaption');
    fig.insertBefore(lane,cap); fig.insertBefore(info,cap);
  }
})();
