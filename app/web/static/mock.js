/* Ported from the judge mockup (07b-hero1.html, 08-script.html). */

/* scene 1 hero: AI orbits outside, human holds the $; only the human releases the refund. 6s seamless loop, time-driven (seek) */
(()=>{
const svg=document.getElementById('hero1');if(!svg)return;
const NS='http://www.w3.org/2000/svg',D=6000,CX=304,CY=318,RO=255,RS=104,TAU=Math.PI*2;
const g=id=>document.getElementById(id);
const E={bot:g('h1bot'),botIn:g('h1botIn'),eyes:g('h1eyes'),ant:g('h1ant'),aiA:g('h1aiArrow'),aiL:g('h1aiLine'),aiH:g('h1aiHead'),blk:g('h1block'),rip:g('h1ripple'),
 dA:g('h1doorA'),dB:g('h1doorB'),lock:g('h1lock'),shk:g('h1shackle'),lockBox:g('h1lockBox'),lockBody:g('h1lockBody'),shkP:g('h1shackP'),
 ref:g('h1refund'),revp:g('h1revp'),refLbl:g('h1refLbl'),coin:g('h1coin'),head:g('h1head'),ok:g('h1ok'),pok:g('h1pok'),pulse:g('h1pulse'),pring:g('h1pring')};
const cp=document.createElementNS(NS,'path');cp.setAttribute('d','M250 290 C 300 262 330 235 362 235 L 631 235');cp.setAttribute('fill','none');cp.setAttribute('visibility','hidden');g('h1world').appendChild(cp);
const CL=cp.getTotalLength(),RL=E.revp.getTotalLength();E.revp.setAttribute('stroke-dasharray',RL+' '+RL);
const cl=x=>x<0?0:x>1?1:x,seg=(t,a,b)=>cl((t-a)/(b-a));
const eo=x=>1-Math.pow(1-x,3),eio=x=>x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2;
const back=x=>{const c=1.9;return 1+(c+1)*Math.pow(x-1,3)+c*Math.pow(x-1,2)};
const mix=(a,b,k)=>a+(b-a)*k;
const hex=(a,b,k)=>{const p=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));const A=p(a),B=p(b);return'#'+A.map((v,i)=>Math.round(mix(v,B[i],k)).toString(16).padStart(2,'0')).join('')};
const TH0=-77*Math.PI/180;
/* two AI attempts: [launch, hit, retractEnd] */
const ATT=[[250,750,1350],[1450,1950,2550]];
function render(t){
 t=((t%D)+D)%D;
 /* bot orbit: exactly one revolution per loop + 3 bobs */
 const th=TH0+TAU*t/D,bx=CX+RO*Math.cos(th),by=CY+RO*Math.sin(th)+3*Math.sin(TAU*3*t/D);
 let shake=0,sad=0,red=0;
 /* AI arrow */
 let aOp=0;
 for(const[a,h,r]of ATT){
  if(t<a-1||t>r+1)continue;
  const ux=(CX-bx),uy=(CY-by),L=Math.hypot(ux,uy),nx=ux/L,ny=uy/L;
  const sx=bx+nx*32,sy=by+ny*32,hx=CX-nx*(RS+9),hy=CY-ny*(RS+9);
  const full=Math.hypot(hx-sx,hy-sy);
  let p;
  if(t<h)p=eio(seg(t,a,h));else p=1-back(seg(t,h+120,r))*1; /* bounce back */
  if(t>=h&&t<h+120)p=1;
  p=Math.max(0,Math.min(1,p));
  aOp=Math.min(seg(t,a,a+120),1-seg(t,r-150,r));
  const tx=sx+(hx-sx)*p,ty=sy+(hy-sy)*p;
  E.aiL.setAttribute('x1',sx.toFixed(1));E.aiL.setAttribute('y1',sy.toFixed(1));E.aiL.setAttribute('x2',(tx-nx*8).toFixed(1));E.aiL.setAttribute('y2',(ty-ny*8).toFixed(1));
  E.aiL.setAttribute('stroke-dashoffset',(-(t/D)*14*30).toFixed(1));
  const hit=t>=h&&t<r?1:0;
  const col=hit?'#F87171':'#A78BFA';
  E.aiL.setAttribute('stroke',col);E.aiH.setAttribute('fill',col);
  const px=-ny,py=nx,hs=15;
  E.aiH.setAttribute('points',`${(tx+nx*4).toFixed(1)},${(ty+ny*4).toFixed(1)} ${(tx-nx*hs+px*hs*.7).toFixed(1)},${(ty-ny*hs+py*hs*.7).toFixed(1)} ${(tx-nx*hs-px*hs*.7).toFixed(1)},${(ty-ny*hs-py*hs*.7).toFixed(1)}`);
  /* block mark at the ring */
  const bxp=CX-nx*RS,byp=CY-ny*RS;
  const bk=seg(t,h,h+180),bo=Math.min(1,bk*3)*(1-seg(t,r-120,r));
  E.blk.setAttribute('transform',`translate(${bxp.toFixed(1)} ${byp.toFixed(1)}) scale(${(0.4+0.6*back(bk)).toFixed(3)})`);
  E.blk.setAttribute('opacity',(t>=h?Math.max(0,bo):0).toFixed(3));
  const rp=seg(t,h,h+500);
  E.rip.setAttribute('cx',bxp.toFixed(1));E.rip.setAttribute('cy',byp.toFixed(1));E.rip.setAttribute('r',(12+30*eo(rp)).toFixed(1));E.rip.setAttribute('opacity',(t>=h&&rp<1?(1-rp)*.9:0).toFixed(3));
  if(t>=h&&t<h+380){shake=Math.sin((t-h)/380*TAU*3)*4*(1-seg(t,h,h+380));sad=1;red=1}
 }
 /* block mark may linger just after retract */
 if(!ATT.some(([a,h,r])=>t>=a-1&&t<=r+1)){E.blk.setAttribute('opacity',0);E.rip.setAttribute('opacity',0)}
 E.aiA.setAttribute('opacity',aOp.toFixed(3));
 E.bot.setAttribute('transform',`translate(${(bx+shake).toFixed(1)} ${by.toFixed(1)})`);
 E.eyes.setAttribute('transform',`translate(0 ${sad?1.5:0}) scale(1 ${sad?.35:1})`);
 /* blink once per loop */
 if(t>4700&&t<4820)E.eyes.setAttribute('transform','scale(1 .15)');
 E.ant.setAttribute('fill',red?'#F87171':'#A78BFA');
 /* human nods + OK badge */
 const nod=seg(t,2700,3250);
 E.head.setAttribute('transform',`translate(0 ${(Math.sin(nod*TAU*2)*3.2*(nod>0&&nod<1?1:0)).toFixed(2)})`);
 const okIn=seg(t,2850,3150),okOut=seg(t,5250,5600);
 E.ok.setAttribute('opacity',(okIn*(1-okOut)).toFixed(3));
 E.ok.setAttribute('transform',`translate(290 176) scale(${(0.3+0.7*back(okIn)).toFixed(3)})`);
 /* gate opens (3150-3500), closes (5350-5800) */
 const op=eo(seg(t,3150,3500))*(1-eio(seg(t,5350,5800)));
 E.dA.setAttribute('transform',`rotate(${(-26*op).toFixed(2)} ${CX} ${CY})`);
 E.dB.setAttribute('transform',`rotate(${(26*op).toFixed(2)} ${CX} ${CY})`);
 const gc=hex('#FBBF24','#34D399',cl(op*1.4));
 E.dA.firstElementChild.setAttribute('stroke',gc);E.dB.firstElementChild.setAttribute('stroke',gc);
 /* lock: shackle pops up, then lock fades out of the gateway */
 const un=seg(t,3000,3250),lf=op;
 E.shk.setAttribute('transform',`translate(0 ${(-5*eo(un)*(1-seg(t,5600,5900))).toFixed(2)})`);
 const lc=hex('#FBBF24','#34D399',un*(1-seg(t,5600,5900)));
 E.lockBox.setAttribute('stroke',lc);E.lockBody.setAttribute('fill',lc);E.shkP.setAttribute('stroke',lc);
 E.lock.setAttribute('transform',`translate(362 ${(235-46*lf).toFixed(1)}) scale(${(1-0.25*lf).toFixed(3)})`);
 E.lock.setAttribute('opacity',(1-cl(lf*1.6)).toFixed(3));
 /* refund arrow reveal 3350-3900; fade 5100-5500 */
 const rv=eio(seg(t,3350,3900));
 E.revp.setAttribute('stroke-dashoffset',(RL*(1-rv)).toFixed(1));
 const rfo=rv>0?1-seg(t,5100,5500):0;
 E.ref.setAttribute('opacity',rfo.toFixed(3));
 E.ref.firstElementChild.setAttribute('stroke-dashoffset',(-(t/D)*17*24).toFixed(1));
 E.refLbl.setAttribute('opacity',(seg(t,3700,3950)*(1-seg(t,5100,5500))).toFixed(3));
 /* coin travels 3500-4500, fades in patient, returns to hands 5500-5950 */
 const cm=eio(seg(t,3500,4500));
 let cpnt=cp.getPointAtLength(CL*cm),cs=1,co=1;
 if(t>=4500){const f=seg(t,4500,4800);cs=1-f*.7;co=1-f}
 if(t>=5500){cpnt={x:250,y:290};const f=eo(seg(t,5500,5950));cs=.4+.6*f;co=f}
 E.coin.setAttribute('transform',`translate(${cpnt.x.toFixed(1)} ${cpnt.y.toFixed(1)}) scale(${cs.toFixed(3)})`);
 E.coin.setAttribute('opacity',co.toFixed(3));
 /* patient receives */
 const pr=seg(t,4500,5100);
 E.pulse.setAttribute('r',(36+28*eo(pr)).toFixed(1));E.pulse.setAttribute('opacity',(pr>0&&pr<1?(1-pr):0).toFixed(3));
 const pk=seg(t,4550,4800),pkOut=seg(t,5500,5800);
 E.pok.setAttribute('opacity',(pk*(1-pkOut)).toFixed(3));
 E.pok.setAttribute('transform',`translate(27 -27) scale(${(0.3+0.7*back(pk)).toFixed(3)})`);
 E.pring.setAttribute('fill-opacity',(0.12+0.18*pk*(1-pkOut)).toFixed(3));
}
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
let raf=0,t0=performance.now(),paused=false,vis=true;
const tick=n=>{render(n-t0);raf=requestAnimationFrame(tick)};
const start=()=>{if(!raf&&!paused&&!reduce&&vis)raf=requestAnimationFrame(tick)};
const stop=()=>{cancelAnimationFrame(raf);raf=0};
window.__hero1={D,render,seek(ms){paused=true;stop();render(ms)},play(){paused=false;t0=performance.now();start()}};
if(reduce){render(4950);return}
render(0);
new IntersectionObserver(es=>es.forEach(e=>{vis=e.isIntersecting;vis?start():stop()})).observe(svg);
})();


(()=>{
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
const $=s=>document.querySelector(s);
const scenes=[...document.querySelectorAll('.scene')];
let cur=-1;
window.__mock={get cur(){return cur}};

/* one navigation system: header progress dots + "n / 8" */
const box=$('#progdots');
const dots=scenes.map((s,i)=>{const b=document.createElement('button');b.type='button';b.setAttribute('aria-label',`Scene ${i+1} of ${scenes.length}`);b.addEventListener('click',()=>go(i));box.appendChild(b);return b});
function go(i){i=Math.max(0,Math.min(scenes.length-1,i));scenes[i].scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'})}
window.__mock.go=go;
document.querySelectorAll('[data-go]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();const g=a.dataset.go;if(g==='team'){$('#team').scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'});return}go(+g-1)}));

/* camera: outgoing scales to .92 and fades; incoming starts at 1.12 and settles (0.8s) */
function activate(i){
 if(i===cur||i<0)return;
 const prev=scenes[cur];
 if(prev){prev.classList.remove('active');prev.classList.add('out');leave(prev.id)}
 const s=scenes[i];
 s.classList.remove('out');s.classList.add('pre');void s.offsetWidth;s.classList.remove('pre');s.classList.add('active');
 cur=i;
 dots.forEach((b,j)=>j===i?b.setAttribute('aria-current','step'):b.removeAttribute('aria-current'));
 $('#nowN').textContent=i+1;
 enter(s.id);
}
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)activate(scenes.indexOf(e.target))}),{rootMargin:'-49% 0px -49% 0px'});
scenes.forEach(s=>io.observe(s));

/* keyboard + swipe */
addEventListener('keydown',e=>{
 if(e.target.closest&&e.target.closest('input,textarea,select,summary,button,a'))return;
 if(e.altKey||e.ctrlKey||e.metaKey)return;
 const k=e.key;let j=null;
 if(k==='ArrowDown'||k==='PageDown'||k==='ArrowRight'||(k===' '&&!e.shiftKey))j=cur+1;
 else if(k==='ArrowUp'||k==='PageUp'||k==='ArrowLeft'||(k===' '&&e.shiftKey))j=cur-1;
 else if(k==='Home')j=0;else if(k==='End')j=scenes.length-1;
 if(j!==null){e.preventDefault();go(j)}
});
let tx=0,ty=0;
addEventListener('touchstart',e=>{const t=e.changedTouches[0];tx=t.clientX;ty=t.clientY},{passive:true});
addEventListener('touchend',e=>{const t=e.changedTouches[0],dx=t.clientX-tx,dy=t.clientY-ty;
 if(Math.abs(dx)>60&&Math.abs(dx)>1.5*Math.abs(dy))go(cur+(dx<0?1:-1))},{passive:true});

/* timers */
const timers={};
const clearT=k=>{(timers[k]||[]).forEach(clearTimeout);timers[k]=[]};
const later=(k,ms,f)=>(timers[k]=timers[k]||[]).push(setTimeout(f,ms));

/* scene 1: 8s two-beat loop. Beat 1 (0-4s): ask -> $450 from DB -> PayPal/Venmo. Beat 2 (4-8s): attack -> real reply -> human approval, $0 moved */
const loop1=$('#loop1');
const K=['k1','k2','k3'];
function beat(n){loop1.classList.remove(...K);loop1.dataset.beat=n}
function runLoop1(){clearT('l1');
 if(reduce){loop1.classList.add('static',...K);return}
 const cyc=()=>{beat(1);
  later('l1',200,()=>loop1.classList.add('k1'));
  later('l1',1100,()=>loop1.classList.add('k2'));
  later('l1',1900,()=>loop1.classList.add('k3'));
  later('l1',4000,()=>beat(2));
  later('l1',4350,()=>loop1.classList.add('k1'));
  later('l1',5150,()=>loop1.classList.add('k2'));
  later('l1',5950,()=>loop1.classList.add('k3'));
  later('l1',8000,cyc)};
 cyc()}

/* scene 5: Ask -> Verified price -> Choose payment (6s loop) */
const f5=$('#flow5');
function runFlow5(){clearT('f5');const set=n=>{f5.classList.remove('ph1','ph2','ph3');if(n)f5.classList.add('ph'+n)};
 if(reduce){set(3);return}
 const cyc=()=>{set(1);later('f5',1800,()=>set(2));later('f5',3600,()=>set(3));later('f5',6400,cyc)};cyc()}

/* scene 6: row 3 edited $85 -> $45. Hashes are real SHA-256 prefixes from the app's own audit digest for these sample rows.
   The verifier flags the edited row; row 4 still points at the old row-3 hash, so that link no longer matches. */
const chain=$('#chain'),rows=[['$450','3610'],['$125','0a25'],['$85','9bca'],['$59','1849'],['$49','d0dd'],['$450','54db']],EDIT=2,NEWH='58b3';
const CK='<svg fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><use href="#i-check"/></svg>';
chain.innerHTML=rows.map((r,i)=>(i?'<span class="lnk" aria-hidden="true"></span>':'')+`<div class="blk"><span class="ed">Edited</span><span class="n">Row ${i+1}</span><span class="v">${r[0]}</span><span class="h">#${r[1]}</span><span class="st">${CK}</span></div>`).join('');
chain.setAttribute('aria-label','Six audit rows linked by hashes. Row 3 is edited from $85 to $45, its hash no longer matches, and its link to row 4 breaks. Verify reports: Tampering detected in rows 3.');
chain.setAttribute('role','img');
const blks=[...chain.querySelectorAll('.blk')],lnks=[...chain.querySelectorAll('.lnk')],cursor=$('#cursor'),s6=$('#s6');
const setBad=(i,bad)=>{const b=blks[i];b.classList.toggle('bad',bad);b.querySelector('.st use').setAttribute('href',bad?'#i-x':'#i-check')};
function reset6(){blks.forEach((b,i)=>{setBad(i,false);b.classList.remove('edit','edited');b.querySelector('.v').textContent=rows[i][0];b.querySelector('.h').innerHTML='#'+rows[i][1]});lnks.forEach(l=>l.classList.remove('bad'));s6.classList.remove('shown')}
function broken(){const b=blks[EDIT];b.classList.add('edited');b.querySelector('.v').textContent='$45';b.querySelector('.h').innerHTML='#'+NEWH+' ≠ <s>#'+rows[EDIT][1]+'</s>';setBad(EDIT,true)}
function runChain(){clearT('ch');reset6();
 if(reduce){broken();lnks[EDIT].classList.add('bad');s6.classList.add('shown');return}
 cursor.style.transition='none';cursor.style.opacity=0;cursor.style.transform='translate(30px,160px)';void cursor.offsetWidth;cursor.style.transition='';
 later('ch',250,()=>{const w=$('#chainwrap').getBoundingClientRect(),b=blks[EDIT].getBoundingClientRect(),z=w.width/$('#chainwrap').offsetWidth||1;
  cursor.style.opacity=1;cursor.style.transform=`translate(${(b.left-w.left)/z+b.width/z*.55}px,${(b.top-w.top)/z+b.height/z*.45}px)`});
 later('ch',1350,()=>{blks[EDIT].classList.add('edit');broken()});
 later('ch',1900,()=>{lnks[EDIT].classList.add('bad');blks[EDIT].classList.remove('edit');cursor.style.opacity=0});
 later('ch',2500,()=>s6.classList.add('shown'));
}

/* team: email shown on click */
document.querySelectorAll('.em').forEach(b=>b.addEventListener('click',()=>{
 if(b.classList.contains('open'))return;
 const e=b.dataset.email;b.classList.add('open');b.setAttribute('aria-expanded','true');
 b.querySelector('span').innerHTML=`<a href="mailto:${e}">${e}</a>`;
}));

function enter(id){if(id==='s1')runLoop1();if(id==='s5')runFlow5();if(id==='s6')runChain()}
function leave(id){if(id==='s1')clearT('l1');if(id==='s5')clearT('f5');if(id==='s6')clearT('ch')}

/* PayPal JS SDK (sandbox, client-id=test) in scene 5: official buttons render; every click is rejected, so no order is created */
const pp=$('#pp-sdk');
const fallback=()=>{if(pp.dataset.mode==='sdk')return;pp.dataset.mode='fallback';pp.innerHTML='<div class="paytiles"><span class="ppbtn"><img src="/static/mock/paypal-blue.svg" alt="PayPal"></span><span class="vmbtn"><img src="/static/mock/venmo-white.svg" alt="Venmo"></span></div>'};
fallback();pp.dataset.mode='';
const ft=setTimeout(()=>{pp.dataset.mode='fallback'},7000);
const sdk=document.createElement('script');
sdk.src='https://www.paypal.com/sdk/js?client-id=test&components=buttons&currency=USD&enable-funding=venmo&buyer-country=US';
sdk.onerror=()=>{clearTimeout(ft);pp.dataset.mode='fallback'};
sdk.onload=()=>{try{
  const opts=f=>({fundingSource:f,style:{layout:'horizontal',shape:'rect',height:40,label:'pay',tagline:false},onClick:(d,a)=>a.reject(),createOrder:()=>Promise.reject(new Error('mockup: no orders'))});
  const els=[paypal.FUNDING.PAYPAL,paypal.FUNDING.VENMO].map(f=>paypal.Buttons(opts(f))).filter(b=>b.isEligible());
  if(!els.length||pp.dataset.mode==='fallback')return;
  clearTimeout(ft);pp.innerHTML='';pp.dataset.mode='sdk';
  els.forEach(b=>{const d=document.createElement('div');d.style.marginBottom='6px';pp.appendChild(d);b.render(d)});
 }catch(e){pp.dataset.mode='fallback'}};
document.head.appendChild(sdk);
})();
