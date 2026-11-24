/* 手機導覽（HUB_HANDOFF §1、§8-9；移植原型 hub-A/nav.js）：右緣漸層提示還有項目（捲到底移除），
   進頁把目前頁面項目捲到導覽列中間（只捲導覽列，不捲整頁）。
   篩選中心：手機（≤640）把結果區搬到選中的卡片下方（原型 place()）；沒有 JS 時結果留在原位。 */
(function(){
  'use strict';
  var n=document.querySelector('.mainnav');
  if(n){
    var edge=function(){n.classList.toggle('more',n.scrollWidth-n.clientWidth-n.scrollLeft>2);};
    var cur=n.querySelector('[aria-current=page]');
    if(cur&&n.scrollWidth>n.clientWidth){
      var want=cur.offsetLeft-n.offsetLeft-(n.clientWidth-cur.offsetWidth)/2;
      n.scrollLeft=Math.max(0,Math.min(want,n.scrollWidth-n.clientWidth));
    }
    edge();n.addEventListener('scroll',edge,{passive:true});window.addEventListener('resize',edge);
  }
  var r=document.getElementById('results'),home=document.querySelector('.lower-main');
  if(r&&home&&window.matchMedia){
    var mq=window.matchMedia('(max-width:640px)');
    var place=function(){
      var id=r.getAttribute('data-mode'),cell=id&&mq.matches?document.querySelector('[data-cell="'+id+'"]'):null;
      (cell||home).appendChild(r);
    };
    place();
    if(mq.addEventListener)mq.addEventListener('change',place);
  }
})();
