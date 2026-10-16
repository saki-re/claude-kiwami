/* 「取り込む」の中身。みんかぶのページの表を写す読み方は、ここ1か所。使い道は2つ：
   1) ブックマークに入れる仕掛け：app.py が1行に詰めて javascript: の形にする。いま開いているページを写して、このアプリの画面（__ORIGIN__/torikomi）へ移る。
   2) 貼り付けて取り込む：このアプリの画面が /extract.js として読み、貼られた HTML を同じ読み方で写す（W.__TOUSHIN_PAGE__ が立っているとき）。
   通信する部品・script の読み込み・自動で次へ進む仕掛けは入れない（確かめ役が数える）。
   表の形が前と違うときは、写さずに止めます（黙って違う数字を入れないため）。
   行の途中で改行しないこと・スラッシュ2本の注釈を書かないこと（1行に詰めたとき壊れるため）。 */
(function(W){
/* V＝中身の版。読めるページを足したら1つ上げる（ブックマークは入れたときの中身のままなので、古いものはこのアプリの画面が見分けて「入れ直して」と出す） */
var O='__ORIGIN__',V=6;
function T(e){return e?e.textContent.replace(/\s+/g,' ').trim():'';}
function N(s){var m=String(s).replace(/,/g,'').replace('−','-').match(/[-+]?\d+\.?\d*/);return m?parseFloat(m[0]):null;}
/* D＝読むページ、L＝その住所、P＝貼り付けで渡されたとき true。返すのは {d:写した中身} か {a:入れない理由} */
function X(D,L,P){
if(L.hostname!=='itf.minkabu.jp'){return {a:'みんかぶ投資信託のページで押してください'};}
var d={v:V},B=T(D.body),fm=L.pathname.match(/^\/fund\/([0-9A-Za-z]{8})\/return\/?$/);
if(fm){
d.kind='fund';d.id=fm[1];d.name=T(D.querySelector('h1')).replace(/のリターン$/,'');
var nm=B.match(/基準価額\s*(\d\d\/\d\d)\s*([\d,]+)\s*円\s*([-+−]?[\d,]+)?/);
if(nm){d.md=nm[1];d.nav=N(nm[2]);d.nd=nm[3]?N(nm[3]):null;}
d.r={};d.k={};d.s={};d.sk={};d.p={};d.pk={};
var ts=D.querySelectorAll('table'),found=false;
for(var a=0;a<ts.length&&!found;a++){
var hd=null,rws={},rs=ts[a].rows;
for(var i=0;i<rs.length;i++){var c0=T(rs[i].cells[0]);if(c0==='期間'){hd=rs[i];}if(c0==='リターン'||c0==='標準偏差'||c0==='シャープレシオ'){rws[c0]=rs[i];}}
if(!hd||!rws['リターン']){continue;}
found=true;
/* リターン＝r/k（値と全ファンド中の順位）、標準偏差（値動きの荒さ）＝s/sk、シャープレシオ（荒さの割の増え方）＝p/pk */
var kinds=[['リターン','r','k'],['標準偏差','s','sk'],['シャープレシオ','p','pk']];
for(var q=0;q<kinds.length;q++){
var vl=rws[kinds[q][0]];
if(!vl){continue;}
if(hd.cells.length!==vl.cells.length){return {a:'リターンの表の形が、前と変わっています。作った人に知らせてください'};}
for(var j=1;j<hd.cells.length;j++){
var lm=T(hd.cells[j]).match(/^(\d+)\s*(ヶ月|ケ月|カ月|か月|年)/),c=T(vl.cells[j]),v=c.match(/^([-+−]?[\d,]*\.?\d+)/);
if(!lm||!v||(q===0&&!/%/.test(c))){continue;}
var mo=lm[2]==='年'?lm[1]*12:+lm[1];
d[kinds[q][1]][mo]=N(v[1]);
var km=c.match(/\(\s*(\d+)\s*位/);
if(km){d[kinds[q][2]][mo]=+km[1];}
}
}
}
if(!found){return {a:'リターンの表が見つかりませんでした'};}
/* NISA の印：名前の上の帯（bg-MK-fund-info）にある「NISA成長投資枠」などのリンク。帯が見つからなければ分からない（null） */
var info=D.querySelector('.bg-MK-fund-info');
if(info){d.nisa=[];var nb=info.querySelectorAll('a[href*="nisa"]');for(var b=0;b<nb.length;b++){d.nisa.push(T(nb[b]));}}
return {d:d};
}
if(/^\/fund\/compare\/?$/.test(L.pathname)){
/* 比較のページ：列ごとに1本のファンド。名前のリンク（/fund/番号）から番号、「リターン」「シャープ レシオ」の行から1年・3年・5年。
   版6から、なりたちの行も写す：手数料（上限・税込）・分配金（前回・1年累計）・決算回数・設定日・信託期間・RR分類（無い行は写さない） */
var ct=null,tbs=D.querySelectorAll('table');
for(var e=0;e<tbs.length&&!ct;e++){var labels={};for(var g=0;g<tbs[e].rows.length;g++){labels[T(tbs[e].rows[g].cells[0]).replace(/\s/g,'')]=tbs[e].rows[g];}if(labels['ファンド']&&labels['リターン']&&labels['シャープレシオ']){ct=labels;}}
if(!ct){return {a:'比較の表が見つかりませんでした'};}
var nr=ct['ファンド'].cells,n2=nr.length;
var fk=null;
for(var x in ct){if(ct[x].cells.length!==n2){return {a:'比較の表の形が、前と変わっています。作った人に知らせてください'};}if(/^手数料/.test(x)){fk=x;}}
function CV(k,i){var t=k&&ct[k]?T(ct[k].cells[i]):'';return /^-+$/.test(t)?'':t;}
function DT(t){var m=t.match(/^(\d{4})\/(\d\d)\/(\d\d)$/);return m?m[1]+'-'+m[2]+'-'+m[3]:null;}
function YRS(cell){var o={},sp=cell.querySelectorAll('span'),tx=[];for(var w=0;w<sp.length;w++){var tw=T(sp[w]);if(tw){tx.push(tw);}}for(var y=0;y+1<tx.length;y++){var ym=tx[y].match(/^(\d+)年$/),yv=tx[y+1].match(/^([-+−]?[\d,]*\.?\d+)%?$/);if(ym&&yv){o[ym[1]*12]=N(yv[1]);}}return o;}
d.kind='compare';d.rows=[];
for(var c2=1;c2<n2;c2++){
var la=nr[c2].querySelector('a[href*="/fund/"]'),im=la?la.getAttribute('href').match(/\/fund\/([0-9A-Za-z]{8})$/):null;
if(!im){continue;}
var dv=CV('分配金',c2),dl=dv.match(/前回\s*([\d,]+)\s*円/),dy=dv.match(/累計\s*([\d,]+)\s*円/),ud=CV('信託期間',c2);
d.rows.push({id:im[1],name:T(la),fee:ct['実質信託報酬']?N(T(ct['実質信託報酬'].cells[c2])):null,r:YRS(ct['リターン'].cells[c2]),p:YRS(ct['シャープレシオ'].cells[c2]),bf:fk?N(CV(fk,c2)):null,dl:dl?N(dl[1]):null,dy:dy?N(dy[1]):null,st:CV('決算回数',c2)||null,sd:DT(CV('設定日',c2)),ud:/無期限/.test(ud)?'none':DT(ud),rr:CV('RR分類',c2)||null});
}
var qa=L.search.match(/fund_ids=([^&]*)/);d.asked=qa?decodeURIComponent(qa[1]).split(',').filter(function(z){return z;}).length:null;
if(!d.rows.length){return {a:'比較の表にファンドが見つかりませんでした'};}
return {d:d};
}
var nk=L.pathname.match(/^\/ranking\/(yield_nisa_growth|yield_nisa)\/?$/);
if(nk){
/* NISA のランキング（NISA成長投資枠 利回り・つみたてNISA利回り）：並んでいるファンドに NISA の印を付けるだけ。番号と名前しか読まない */
d.kind='nisa';d.which=nk[1];d.rows=[];
var qp=L.search.match(/[?&]page=(\d+)/);d.page=qp?+qp[1]:1;
var ni=D.querySelectorAll('input[id="fund_ids"]');
for(var z2=0;z2<ni.length;z2++){
var tz=ni[z2].closest('tr'),iz=ni[z2].value;
if(!tz||!/^[0-9A-Za-z]{8}$/.test(iz)||!/^\d+$/.test(T(tz.querySelector('span.text-lg')))){continue;}
d.rows.push([iz,T(tz.querySelector('a[href$="/fund/'+iz+'"]'))]);
}
if(!d.rows.length){return {a:'NISA のランキングの表が見つかりませんでした。作った人に知らせてください'};}
return {d:d};
}
if(!/^\/ranking\/return\/?$/.test(L.pathname)){return {a:'ランキングか、ファンドの「リターン」のページか、比較のページで押してください',u:L.pathname};}
var ex=D.querySelector('select[name="exchange"]'),cats=D.querySelectorAll('a[href*="fund_type="]'),narrowed=false;
for(var q=0;q<cats.length;q++){if(/bg-blue-500/.test(cats[q].className)&&/fund_type=[^&]/.test(cats[q].getAttribute('href'))){narrowed=true;}}
if(narrowed||/[?&]fund_type=[^&]/.test(L.search)||/[?&]exchange=[12]/.test(L.search)||(ex&&ex.value!=='0')){return {a:'絞り込みなし（すべて）のランキングで押してください'};}
var s=D.querySelector('select[name="term"]'),qt=L.search.match(/[?&]term=(\d+)/),u=B.match(/更新[：:]\s*(\d{4})\/(\d\d)\/(\d\d)\s*(\d\d?:\d\d)?/),sp=B.match(/(\d{4})\/(\d\d)\s*~\s*(\d{4})\/(\d\d)/);
var ms=sp?(sp[3]-sp[1])*12+(sp[4]-sp[2])+1:0,tm=s?+s.value:(qt?+qt[1]:(P?ms:12));
if((s&&qt&&s.value!==qt[1])||(ms&&tm!==ms)){return {a:'期間の表示が食い違っています。ページを開き直してから押してください'};}
if(!tm){return {a:'どの期間のランキングかが読めませんでした'};}
d.kind='rank';d.term=tm;d.date=u?u[1]+'-'+u[2]+'-'+u[3]:null;d.time=u&&u[4]?u[4]:null;d.span=sp?sp[1]+'/'+sp[2]+'~'+sp[3]+'/'+sp[4]:null;d.rows=[];
var ins=D.querySelectorAll('input[id="fund_ids"]'),tb=ins.length?ins[0].closest('table'):null,th=tb?tb.querySelectorAll('th'):[];
if(!tb){return {a:'ランキングの表が見つかりませんでした'};}
if(th.length<5||!/基準価額/.test(T(th[1]))||!/利回り.*1年/.test(T(th[2]))||!/信託報酬/.test(T(th[3]))||!/レーティング/.test(T(th[4]))){return {a:'ランキングの表の形が、前と変わっています。作った人に知らせてください'};}
for(var n=0;n<ins.length;n++){
var tr=ins[n].closest('tr');
if(!tr){continue;}
var id=ins[n].value,td=tr.children,ps=td[1]?td[1].querySelectorAll('p'):[],rk=T(tr.querySelector('span.text-lg'));
if(!/^\d+$/.test(rk)||td.length<5){continue;}
var nd=ps[1]?N(T(ps[1])):null;
if(nd!==null&&/negative/.test(ps[1].className)){nd=-Math.abs(nd);}
d.rows.push([+rk,id,T(tr.querySelector('a[href$="/fund/'+id+'"]')),T(tr.querySelector('div.text-itf-xs')).replace(/\s/g,''),ps[0]?N(T(ps[0])):null,nd,N(T(td[2])),N(T(td[3])),(T(td[4]).match(/★/g)||[]).length]);
}
if(!d.rows.length){return {a:'ランキングの表が見つかりませんでした'};}
return {d:d};
}
if(W.__TOUSHIN_PAGE__){W.toushinExtract=X;return;}
var r=X(document,location,false);
/* 読めないみんかぶのページでは、止まらずにこのアプリの画面へ移って、版と住所だけを渡す（ブックマークが古いのか、本当に読めないページなのかを、このアプリの画面が言い分ける） */
if(r.u){location.href=O+'/torikomi#'+encodeURIComponent(JSON.stringify({v:V,kind:'unknown',path:r.u}));return;}
if(r.a){alert(r.a);return;}
location.href=O+'/torikomi#'+encodeURIComponent(JSON.stringify(r.d));
})(window);
