/* MetalLomPro 2.0 — фронтенд */
'use strict';

const $=id=>document.getElementById(id);
const fmt=n=>n==null?'—':Math.round(n).toLocaleString('ru-RU');
const charts={};

async function api(path,opts={}){
  const r=await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...opts});
  if(r.status===401){showLogin();throw new Error('auth');}
  if(!r.ok){let d;try{d=await r.json()}catch{d={}};throw new Error(d.detail||('HTTP '+r.status));}
  return r.json();
}

/* ── Бейджи качества данных ── */
const QUALITY_LABEL={live:'live',cache:'кеш',manual:'обзвон',calc:'расчёт'};
function badge(q){
  if(!q)return '<span class="badge b-na">нет данных</span>';
  const cls={live:'b-live',cache:'b-cache',manual:'b-manual',calc:'b-calc'}[q.quality]||'b-na';
  const when=q.collected_at?new Date(q.collected_at).toLocaleDateString('ru-RU'):'';
  return `<span class="badge ${cls}" title="${q.source||''}">${QUALITY_LABEL[q.quality]||q.quality} · ${when}</span>`;
}

/* ── Авторизация ── */
function showLogin(){$('login-screen').style.display='flex';$('app').style.display='none';}
async function doLogin(){
  $('li-err').textContent='';
  try{
    await api('/login',{method:'POST',body:JSON.stringify({username:$('li-user').value.trim(),password:$('li-pass').value})});
    location.reload();
  }catch(e){$('li-err').textContent=e.message;}
}
async function doLogout(){await api('/logout',{method:'POST'});location.reload();}
$('li-pass')?.addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});

/* ── Навигация ── */
const loaded={};
function show(s){
  document.querySelectorAll('.screen').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.s===s));
  $('s-'+s).classList.add('on');
  if(!loaded[s]){loaded[s]=true;LOADERS[s]&&LOADERS[s]().catch(console.warn);}
}

function mkChart(id,cfg){
  if(charts[id])charts[id].destroy();
  charts[id]=new Chart($(id).getContext('2d'),cfg);
}
/* Цвета графиков — фирменная палитра МетОптТорг (как на портале) */
const C={steel:'#485E88',steel2:'#8390B1',navy:'#142248',ok:'#177A50',warn:'#9A6A0B',
  crit:'#B23A2F',line:'#D6DEEA',t3:'#5F6B85'};
const gridOpts={color:'rgba(214,222,234,.7)'};
const baseScales={x:{grid:gridOpts,ticks:{color:C.t3,font:{size:10}}},y:{grid:gridOpts,ticks:{color:C.t3,font:{size:10}}}};

/* ═══ Сводка ═══ */
async function loadSummary(){
  const d=await api('/summary');
  const sig=d.signals;
  const sigTile=(name,metal,note)=>{
    const v=sig[metal], o=(d.outlook||{})[metal];
    const cls=v>0.15?'up':(v<-0.1?'dn':'acc');
    const word=v>0.15?'Ожидаем рост':(v<-0.1?'Ожидаем снижение':'Ожидаем стабильность');
    const arrow=v>0.15?'▲':(v<-0.1?'▼':'→');
    let big, detail='';
    if(o){
      const sgn=o.pct_change>0?'+':'';
      big=`${arrow} ${sgn}${o.pct_change}% за 6 мес`;
      const absAbs=Math.abs(Math.round(o.abs_change));
      detail=`${o.abs_change<0?'−':'+'}${fmt(absAbs)} ₽/т: с ${fmt(o.base)} до ${fmt(o.m6)} (${o.series_name})`;
    }else{
      big=`${arrow} ${v>0?'+':''}${v.toFixed(2)}`;
    }
    return `<div class="kpi"><div class="kpi-l">${name} — ${word.toLowerCase()}</div>
      <div class="sig ${cls}">${big}</div>
      ${detail?`<div class="kpi-u" style="font-size:12px;color:var(--t2)">${detail}</div>`:''}
      <div class="kpi-u">${note}</div>
      <div class="kpi-u" style="opacity:.65">сигнал модели ${v>0?'+':''}${v.toFixed(2)} (от −1 до +1)</div></div>`;
  };
  $('sig-tiles').innerHTML=
    sigTile('Чёрный лом','black',d.strategy.black)+
    sigTile('Медь','copper',d.strategy.copper)+
    sigTile('Алюминий','alum',d.strategy.alum);

  const km=d.key_metrics;
  const kpi=(label,q,unit)=>`<div class="kpi"><div class="kpi-l">${label}</div>
    <div class="kpi-v">${q?fmt(q.value):'—'}</div>
    <div class="kpi-u">${unit||q?.unit||''} ${badge(q)}</div></div>`;
  $('kpi-tiles').innerHTML=
    kpi('3А CPT ж/д Урал',km.lom3a_ural)+
    kpi('3А FCA Екатеринбург',km.lom3a_ural_fca)+
    kpi('Прайс ММК',km.plant_MMK)+
    kpi('HMS CFR Турция',km.hms_turkey)+
    kpi('USD/RUB',km.usd_rub)+
    kpi('Ставка ЦБ',km.key_rate)+
    kpi('Медь LME',km.copper_lme)+
    kpi('Алюминий LME',km.alum_lme);

  const regions={PERM:'Пермь',KOMI_PERM:'Коми',HMAO:'ХМАО',SOUTH:'Юг'};
  let rows='<tr><th>Регион</th><th>FCA (забирают)</th><th>С доставкой (мы везём)</th></tr>';
  for(const[code,name]of Object.entries(regions)){
    const f=d.survey_prices[code+'_FCA'],c=d.survey_prices[code+'_CPT_AUTO'];
    rows+=`<tr><td>${name}</td>
      <td>${f?fmt(f.price)+' <span class="muted">('+f.date+')</span>':'—'}</td>
      <td>${c?fmt(c.price)+' <span class="muted">('+c.date+')</span>':'—'}</td></tr>`;
  }
  $('survey-tbl').innerHTML=rows;
  // предупреждение, если обзвон старше 21 дня — прогноз уже питается рынком
  try{
    const ds=Object.values(d.survey_prices).map(x=>new Date(x.date));
    if(ds.length){
      const age=Math.round((Date.now()-Math.max(...ds))/864e5);
      if(age>21)$('survey-tbl').insertAdjacentHTML('afterend',
        `<div class="note" style="color:var(--crit)">⚠ Обзвону ${age} дней — рынок с тех пор сильно сдвинулся. `+
        `Прогноз сейчас считается от живых котировок (MMI/Транслом). Внеси свежий обзвон на экране «Обзвон» — `+
        `он снова станет главным источником.</div>`);
    }
  }catch(e){}

  $('strategy-box').innerHTML=Object.entries({'Чёрный':d.strategy.black,'Медь':d.strategy.copper,'Алюминий':d.strategy.alum})
    .map(([k,v])=>`<div style="margin-bottom:8px"><b class="acc">${k}:</b> ${v}</div>`).join('');
  $('two-phase-note').textContent='Фаза 1: '+d.two_phase.phase1+' · Фаза 2: '+d.two_phase.phase2;

  const i3=d.indices3||{};
  const idxTile=(label,q,how)=>`<div class="kpi"><div class="kpi-l">${label}</div>
    <div class="kpi-v">${q?fmt(q.value):'—'}</div>
    <div class="kpi-u">${q?badge(q):'<span class="badge b-na">нет данных</span>'} <span class="muted">${how}</span></div></div>`;
  $('indices3-tiles').innerHTML=
    idxTile('RusLOM',i3.ruslom,'ручной ввод')+
    idxTile('MMI (Пермь, без ЖДТ)',i3.mmi,'из еженедельника')+
    idxTile('MetallPlace',i3.metallplace,'ручной ввод')+
    idxTile('Транслом (Урал CPT)',i3.translom,'скрапер');
  const oi=d.our_index||{};
  const OIN={PERM:'Пермский край',KOMI_NORTH:'Коми',HMAO:'ХМАО',SOUTH:'Юг (Волгоград)'};
  $('ourindex-tiles').innerHTML=Object.entries(OIN).map(([code,name])=>{
    const q=oi[code];
    return `<div class="kpi"><div class="kpi-l">${name}</div>
      <div class="kpi-v">${q?fmt(q.value):'—'}</div>
      <div class="kpi-u">${q?badge(q):'<span class="badge b-na">мало данных ЖД</span>'}</div></div>`;
  }).join('');
  api('/ai/briefing').then(b=>{
    $('briefing-box').innerHTML=b.content?md(b.content)+`<div class="note">${b.model} · ${new Date(b.created_at).toLocaleString('ru-RU')}</div>`
      :'<span class="muted">Брифинг ещё не генерировался — нажми на экране «ИИ-аналитик» или дождись утра (генерация в 7:30)</span>';
  }).catch(()=>{});
  const fr=d.company_freshness;
  $('foot-status').innerHTML=`1С: продажи до ${fr.sales||'—'}<br>рейсы до ${fr.trips||'—'}`;
}

/* ═══ Маржа ═══ */
let mgPrefilled=false;
async function prefillMargin(){
  // автозаполнение тоннажа из соседнего сервиса «Остатки» (:8090)
  try{
    const n=await api('/stock_neighbor');
    if(n&&n.regions){
      const map={PERM:'perm',KOMI_PERM:'komi',KOMI_NORTH:'komi_north',HMAO:'hmao',SOUTH:'south'};
      for(const[reg,k]of Object.entries(map)){
        const el=$('mg-'+k);
        if(el.value==='0'||el.value==='')el.value=Math.round(n.regions[reg]||0);
      }
      $('margin-stock-note').innerHTML=`Тоннаж подставлен из <b>сервиса «Остатки»</b> (сверенные данные на ${n.actual_date}, всего ${fmt(n.total_t)} т лома`+
        (Object.keys(n.unmapped||{}).length?`; вне регионов расчёта: ${Object.entries(n.unmapped).map(([b,t])=>b+' '+fmt(t)+' т').join(', ')}`:'')+
        `). Можно поправить вручную.`;
      mgPrefilled=true;
    }
  }catch(e){
    $('margin-stock-note').innerHTML='<span class="muted">Сервис «Остатки» недоступен — введи тоннаж вручную</span>';
  }
}
async function loadMargin(){
  if(!mgPrefilled)await prefillMargin();
  for(const k of['cost']){
    const el=$('mg-'+k);
    if(el.value==='0'&&localStorage['mg-'+k])el.value=localStorage['mg-'+k];
    localStorage['mg-'+k]=el.value;
  }
  const p=new URLSearchParams({perm:$('mg-perm').value||0,komi:$('mg-komi').value||0,
    komi_north:$('mg-komi_north').value||0,
    hmao:$('mg-hmao').value||0,south:$('mg-south').value||0,cost:$('mg-cost').value||0});
  const d=await api('/margin_today?'+p);
  $('margin-cost-note').innerHTML=d.cost_source?
    `Себестоимость: <b>${fmt(d.cost_source.cost_per_t)} ₽/т</b> (${d.cost_source.source}, ${fmt(d.cost_source.qty_t)} т за ${d.cost_source.months} мес)`:
    '<span class="dn">Нет данных себестоимости — загрузите выгрузку продаж 1С</span>';
  $('margin-kpi').innerHTML=
    `<div class="kpi"><div class="kpi-l">Маржа при продаже сейчас</div><div class="kpi-v ${d.total_margin_rub>=0?'up':'dn'}">${fmt(d.total_margin_rub)} ₽</div><div class="kpi-u">${fmt(d.total_qty_t)} т в лучшие точки</div></div>`+
    `<div class="kpi"><div class="kpi-l">Маржа через 3 мес</div><div class="kpi-v">${fmt(d.total_margin_3m_rub)} ₽</div><div class="kpi-u">прогноз модели: цена ${d.price_change_3m_pct>0?'+':''}${d.price_change_3m_pct}%</div></div>`+
    `<div class="kpi"><div class="kpi-l">Цена ожидания</div><div class="kpi-v ${d.wait_cost_rub>0?'dn':'up'}">${d.wait_cost_rub>0?'−':'+'}${fmt(Math.abs(d.wait_cost_rub))} ₽</div><div class="kpi-u">${d.wait_cost_rub>0?'столько потеряем, если ждать 3 мес':'ожидание в плюс'}</div></div>`;
  let rows='<tr><th>Регион</th><th>Тонн</th><th>Лучший завод</th><th>Чистая ₽/т</th><th>Себест. ₽/т</th><th>Маржа сейчас</th><th>Чистая ₽/т +3м</th><th>Маржа +3м</th></tr>';
  for(const r of d.rows){
    rows+=`<tr><td>${r.region_name}</td><td>${fmt(r.qty_t)}</td><td>${r.best_plant}</td>
      <td>${fmt(r.net_per_ton)}</td><td>${fmt(r.cost_per_t)}</td>
      <td class="${(r.margin_rub||0)>=0?'up':'dn'}">${r.margin_rub!=null?fmt(r.margin_rub):'—'}</td>
      <td>${fmt(r.net_per_ton_3m)}</td>
      <td class="muted">${r.margin_rub_3m!=null?fmt(r.margin_rub_3m):'—'}</td></tr>`;
  }
  $('margin-tbl').innerHTML=d.rows.length?rows:'<tr><td class="muted">Введи тоннаж выше и нажми «Посчитать»</td></tr>';
  $('margin-note').textContent=d.note;
  loadStockTable();
}

const REGION_RU={PERM:'Пермский край',KOMI_PERM:'Коми-Пермяцкий',KOMI_NORTH:'Коми (Усинск/Ухта)',HMAO:'ХМАО',SOUTH:'Юг'};
async function loadStockTable(){
  // Сверенные остатки по базам из сервиса «Остатки»; сырой регистр — только запасной вариант
  try{
    const n=await api('/stock_neighbor');
    if(n&&n.bases&&n.bases.length){
      $('stock-tbl-title').textContent='Остатки лома по базам — сверенные';
      let h='<tr><th>База</th><th>Лом, т</th><th>Регион расчёта</th></tr>';
      for(const b of n.bases)h+=`<tr><td>${b.base}</td><td>${fmt(b.tonnes)}</td>
        <td class="${b.region?'':'muted'}">${b.region?REGION_RU[b.region]||b.region:'вне маршрутов'}</td></tr>`;
      h+=`<tr><td><b>Итого</b></td><td><b>${fmt(n.total_t)}</b></td><td></td></tr>`;
      $('stock-tbl').innerHTML=h;
      $('stock-tbl-note').innerHTML=`Источник: <span class="badge b-live">сервис «Остатки» · ${n.actual_date}</span> — данные прошли ручную сверку со складами. «Вне маршрутов» — базы, для которых в калькуляторе нет плеч доставки (в маржу не входят).`;
      return;
    }
  }catch(e){}
  // фолбэк: наш сырой регистр
  try{
    const st=await api('/stock');
    $('stock-tbl-title').textContent='Сырые остатки из регистра 1С (требуют сверки!)';
    let h='<tr><th>Склад</th><th>Приход−расход, т</th><th>Регион</th></tr>';
    for(const s of st.slice(0,12))h+=`<tr><td>${s.warehouse}</td><td>${fmt(s.qty_t)}</td><td class="muted">${s.region||'?'}</td></tr>`;
    $('stock-tbl').innerHTML=st.length?h:'<tr><td class="muted">Нет данных</td></tr>';
    $('stock-tbl-note').textContent='Сервис «Остатки» недоступен — показан сырой регистр оборотов (без начальных остатков, цифры могут быть завышены).';
  }catch(e){}
}

/* ═══ ЖД и вагоны ═══ */
const RC_PLANTS=[['SEVERSTAL_TZ','Северский ТЗ'],['MMK','ММК'],['PROMSORT_URAL','ПромСорт-Урал'],
  ['PNTZ','ПНТЗ'],['NLMK','НЛМК'],['SEVERSTAL','Северсталь'],['BMZ','БМЗ'],['OMK_STAL','ОМК (Выкса)'],
  ['UGMK_TUMEN','УГМК-Тюмень'],['EVRAZ_ZSMK','ЕВРАЗ ЗСМК'],['VOLZHSKY_TZ','Волжский ТЗ'],
  ['TAGANROG_MZ','Таганрогский МЗ'],['ABINSK_EMZ','Абинский ЭМЗ']];
async function loadRail(){
  $('rc-plant').innerHTML=RC_PLANTS.map(([id,n])=>`<option value="${id}">${n}</option>`).join('');
  const st=await api('/rail/status');
  if(st.shipments)$('rail-period').innerHTML=`Повагонная база: <b>${fmt(st.shipments)}</b> отправок лома за ${st.from} — ${st.to}. Реальные тарифы РЖД по маршрутам и вагонная экономика`;
  else $('rail-period').innerHTML='<span class="dn">База ЖД пуста — загрузите выгрузку ИВМ (CSV) на экране «Данные»</span>';
  await loadWagonCalc();
  await loadRailRoutes();
}
async function loadWagonCalc(){
  const reg=$('rc-region').value;
  const p=new URLSearchParams({plant_id:$('rc-plant').value,from_region:reg,
    cpt_price:$('rc-cpt').value||0,plant_wagon_price:$('rc-plantprice').value||0,
    private_rate:$('rc-private').value||3250,own_wagon_cost:$('rc-own').value||0});
  const d=await api('/rail/wagon_calc?'+p);
  $('rc-note').innerHTML=`CPT: <b>${fmt(d.cpt_price)} ₽/т</b> (${d.price_source}) · ЖД-тариф: <b>${d.rail?fmt(d.rail.tariff_per_t):'—'} ₽/т</b> `+
    (d.rail?`<span class="badge ${d.rail.quality==='live'?'b-live':'b-calc'}">${d.rail.source}</span>`:'<span class="badge b-na">нет данных</span>');
  $('rc-best').innerHTML=`<div class="best" style="margin-top:10px"><div class="t">${d.summary}</div></div>`;
  let h='<tr><th></th><th>Схема</th><th>Цена ₽/т</th><th>− ЖД-тариф</th><th>− вагонная ставка</th><th>= Чистыми ₽/т</th><th>Проигрыш лучшей</th></tr>';
  for(const v of d.variants){
    h+=`<tr${v.is_best?' style="background:var(--ok-tint)"':''}><td>${v.is_best?'★':''}</td>
      <td><b>${v.name}</b><div class="muted">${v.note}</div></td>
      <td>${fmt(v.price)}</td><td>−${fmt(v.rail_tariff)}</td><td>−${fmt(v.wagon_cost)}</td>
      <td class="${v.is_best?'up':''}" style="font-weight:800">${fmt(v.net_per_ton)}</td>
      <td class="${v.vs_best<0?'dn':'muted'}">${v.vs_best<0?fmt(v.vs_best):'—'}</td></tr>`;
  }
  $('rc-tbl').innerHTML=h;
  loadCompetitors(reg);
}
async function loadRailRoutes(){
  const reg=$('rr-region').value;
  const d=await api('/rail/routes'+(reg?'?from_region='+reg:''));
  let h='<tr><th>Откуда</th><th>Грузополучатель</th><th>Тонн</th><th>Тариф ₽/т</th><th>Отпр.</th><th>Ср. вагон, т</th></tr>';
  for(const r of d)h+=`<tr><td>${r.from_region}</td><td>${r.consignee.slice(0,38)}${r.plant_id?' <span class="badge b-manual">'+r.plant_id+'</span>':''}</td>
    <td>${fmt(r.tons)}</td><td><b>${fmt(r.tariff_per_t)}</b></td><td>${r.shipments}</td><td>${r.avg_wagon_t||'—'}</td></tr>`;
  $('rr-tbl').innerHTML=d.length?h:'<tr><td class="muted">Нет данных</td></tr>';
}
async function loadCompetitors(reg){
  const d=await api('/rail/competitors?from_region='+reg);
  let h='<tr><th>Грузоотправитель</th><th>Куда</th><th>Тонн</th><th>Тариф ₽/т</th></tr>';
  for(const r of d.slice(0,15))h+=`<tr><td>${r.consignor.slice(0,32)}</td><td class="muted">${r.consignee.slice(0,30)}</td><td>${fmt(r.tons)}</td><td>${fmt(r.tariff_per_t)}</td></tr>`;
  $('rcomp-tbl').innerHTML=d.length?h:'<tr><td class="muted">Нет данных по региону</td></tr>';
  const ops=await api('/rail/operators?from_region='+reg);
  let oh='<tr><th>Оператор</th><th>Вагонов</th><th>Тонн</th></tr>';
  for(const o of ops.slice(0,10))oh+=`<tr><td>${o.operator.slice(0,36)}</td><td>${o.wagons}</td><td>${fmt(o.tons)}</td></tr>`;
  $('rop-tbl').innerHTML=ops.length?oh:'<tr><td class="muted">—</td></tr>';
}

/* ═══ Матрица связей ═══ */
function mxTab(p){
  document.querySelectorAll('.mx-pane').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('#mx-tabs .subtab').forEach(x=>x.classList.toggle('active',x.dataset.p===p));
  $('mxp-'+p).classList.add('on');
}
async function loadMatrix(){
  const d=await api('/matrix');
  if(d.error){$('mx-week').innerHTML='<span class="dn">'+d.error+'</span>';return;}
  $('mx-week').innerHTML=`Командный центр рынка · неделя MMI с <b>${d.week_start}</b> · недель загружено: ${d.weeks_available.length}`;
  const RN={PERM:'Пермский край',KOMI_NORTH:'Коми',HMAO:'ХМАО',SOUTH:'Юг (Волгоград)'};
  $('mx-our').innerHTML=Object.entries(d.our_regions).map(([code,r])=>
    `<div class="kpi"><div class="kpi-l">${RN[code]||code}</div>
     <div class="kpi-v">${fmt(r.total_t)} т</div>
     <div class="kpi-u">уехало по ЖД за неделю (${r.obl})</div></div>`).join('');
  let oh='<tr><th>Регион</th><th>Завод-получатель</th><th>Тонн за неделю</th></tr>';
  for(const[code,r]of Object.entries(d.our_regions))
    for(const f of r.flows)oh+=`<tr><td>${RN[code]||code}</td><td class="pl" style="cursor:pointer;color:var(--steel)" onclick="mxTab('plants');loadDossier('${f.plant.replace(/'/g,"\\'")}')">${f.plant}</td><td>${fmt(f.tons)}</td></tr>`;
  $('mx-ourflows-tbl').innerHTML=oh;
  await loadMatrixGrid();
  loadPlantsRegistry();
  loadSuppliers();
}
async function loadPlantsRegistry(){
  const d=await api('/matrix/plants_registry');
  let h='<tr><th>Завод</th><th>CPT ₽/т</th><th>Δ нед</th><th>Закуп, т/нед</th><th>СР%</th><th>Гл. регион-поставщик</th><th>Наши продажи</th><th>Netback Пермь</th></tr>';
  for(const p of d){
    h+=`<tr><td class="pl" onclick="loadDossier('${p.plant.replace(/'/g,"\\'")}')">${p.plant}</td>
      <td><b>${p.cpt?fmt(p.cpt):'—'}</b></td>
      <td class="${(p.cpt_change_wk||0)>0?'up':(p.cpt_change_wk<0?'dn':'muted')}">${p.cpt_change_wk!=null?(p.cpt_change_wk>0?'+':'')+fmt(p.cpt_change_wk):'—'}</td>
      <td>${fmt(p.buy_week_tons)}</td>
      <td class="${(p.free_share_pct||0)>70?'up':'muted'}">${p.free_share_pct??'—'}</td>
      <td class="muted">${p.top_supplier_region||'—'}</td>
      <td>${p.our_qty_t?fmt(p.our_qty_t)+' т по '+fmt(p.our_price)+' <span class="muted">до '+p.our_last+'</span>':'<span class="muted">не продаём</span>'}</td>
      <td>${p.netback_perm?'<b>'+fmt(p.netback_perm)+'</b>':'—'}</td></tr>`;
  }
  $('mx-plants-tbl').innerHTML=h;
}
async function loadSuppliers(){
  const reg=$('mx-sup-region').value;
  const d=await api('/matrix/suppliers'+(reg?'?from_region='+reg:''));
  let h='<tr><th>Заготовитель / грузоотправитель</th><th>Тонн</th><th>Отпр.</th><th>Ср. тариф ₽/т</th><th>Откуда</th><th>Куда возит</th></tr>';
  for(const s of d){
    h+=`<tr><td>${s.consignor.slice(0,36)}${s.captive?' <span class="badge b-cache">кэптив</span>':''}</td>
      <td><b>${fmt(s.tons)}</b></td><td>${s.shipments}</td><td>${fmt(s.tariff_per_t)}</td>
      <td class="muted">${s.top_regions.join('; ')}</td>
      <td class="muted">${s.top_plants.join('; ')}</td></tr>`;
  }
  $('mx-sup-tbl').innerHTML=d.length?h:'<tr><td class="muted">Нет данных по региону</td></tr>';
}
let MXG=null;
async function loadMatrixGrid(){
  MXG=await api('/matrix/multi');
  if(MXG.error)return;
  $('mx-method').textContent=MXG.method+' Слои независимы: расхождение источников — само по себе сигнал.';
  if(MXG.rail_period)$('mxl-rail-period').textContent=MXG.rail_period[0]+' — '+MXG.rail_period[1];
  renderMatrixGrid();
  loadFactors();
  loadWebLatest();
}
function renderMatrixGrid(){
  const g=MXG; if(!g)return;
  const showM=$('mxl-mmi').checked, showR=$('mxl-rail').checked, showO=$('mxl-ours').checked;
  let h='<tr><th style="text-align:left">Завод ↓ · Область →</th><th>CPT ₽/т</th><th>т/нед</th><th>СР%</th>';
  for(const c of g.columns){
    const isOur=g.our_columns.includes(c);
    h+=`<th class="col${isOur?' ourcol':''}" title="${c}">${c.length>20?c.slice(0,19)+'…':c}</th>`;
  }
  h+='</tr>';
  for(const r of g.rows){
    h+=`<tr><td class="pl" onclick="loadDossier('${r.plant.replace(/'/g,"\\'")}')">${r.plant}</td>
      <td><b>${r.cpt?fmt(r.cpt):'—'}</b></td><td>${fmt(r.week_tons)}</td>
      <td class="${(r.free_share_pct||0)>70?'up':'muted'}">${r.free_share_pct??'—'}</td>`;
    r.cells.forEach((v,i)=>{
      const rail=r.rail[i], ours=r.ours[i];
      const alpha=(showM&&v)?Math.min(0.85,0.08+0.77*Math.sqrt(v/g.max_cell)):0;
      const isOur=g.our_columns.includes(g.columns[i]);
      let inner='';
      if(showM&&v)inner+=`<div${alpha>0.45?' style="color:#fff"':''}>${fmt(v)}</div>`;
      if(showR&&rail)inner+=`<div style="color:var(--ok);font-size:10px;font-weight:800">${fmt(rail)}</div>`;
      if(showO&&ours)inner+=`<div style="color:#E8862B;font-size:10px;font-weight:800">◆ ${fmt(ours)}</div>`;
      h+=`<td class="cell" style="background:rgba(72,94,136,${alpha})${isOur?';outline:1px solid var(--steel-3)':''}">${inner}</td>`;
    });
    h+='</tr>';
  }
  $('mx-grid').innerHTML=h;
}
async function loadFactors(){
  const fs=await api('/market/factors');
  let h='<tr><th></th><th>Фактор</th><th>Значение</th><th>Комментарий</th><th>Источник</th></tr>';
  for(const f of fs){
    const arrow=f.direction>0.1?'<span class="up">▲</span>':(f.direction<-0.1?'<span class="dn">▼</span>':'<span class="muted">→</span>');
    h+=`<tr><td>${arrow}</td><td>${f.name}</td>
      <td><b>${f.value??''}</b> <span class="muted">${f.unit||''}</span></td>
      <td class="muted">${f.comment}</td>
      <td><span class="badge ${f.quality==='live'?'b-live':(f.quality==='calc'?'b-calc':'b-na')}" title="${f.source}">${f.date||f.quality}</span></td></tr>`;
  }
  $('mx-factors-tbl').innerHTML=h;
}
/* Паутина влияний: SVG-граф в 3 слоя (события → каналы → цели) */
function drawWeb(w){
  const ev=w.events||[],ch=w.channels||[],tg=w.targets||[],links=w.links||[];
  const W=1080,rowH=44,H=Math.max(ev.length,ch.length,tg.length)*rowH+40;
  const colX=[10,430,830],colW=[350,300,240];
  const pos={};
  const place=(arr,ci)=>arr.forEach((n,i)=>{pos[n.id]={x:colX[ci],y:52+i*rowH,ci};});
  place(ev,0);place(ch,1);place(tg,2);
  let s=`<svg viewBox="0 0 ${W} ${H+26}" style="width:100%;height:auto;font-family:inherit">`;
  const heads=['СОБЫТИЯ (ВНЕШНИЕ)','КАНАЛЫ ВЛИЯНИЯ','ЦЕНЫ И МЫ'];
  heads.forEach((t,i)=>{s+=`<text x="${colX[i]}" y="16" font-size="10" font-weight="800" letter-spacing="1.5" fill="#5F6B85">${t}</text>`;});
  for(const l of links){
    const a=pos[l.from],b=pos[l.to];if(!a||!b)continue;
    const x1=a.x+colW[a.ci],y1=a.y+13,x2=b.x,y2=b.y+13;
    const col=l.sign>0?'#177A50':(l.sign<0?'#B23A2F':'#8390B1');
    const wd=(l.strength||1)*1.1;
    s+=`<path d="M${x1},${y1} C${x1+70},${y1} ${x2-70},${y2} ${x2},${y2}" fill="none" stroke="${col}" stroke-width="${wd}" opacity="0.55"><title>${l.sign>0?'+':'−'} ${l.why||''} (лаг ${l.lag||'?'})</title></path>`;
  }
  const box=(n,ci,fill,stroke)=>{
    const p=pos[n.id];
    const t=(n.title||'').slice(0,ci===0?52:40);
    return `<g>${n.src||n.title?`<title>${((n.title||'')+(n.src?' · '+n.src:'')).replace(/&/g,'&amp;').replace(/</g,'&lt;')}</title>`:''}<rect x="${p.x}" y="${p.y}" width="${colW[ci]}" height="28" rx="7" fill="${fill}" stroke="${stroke}"/>`+
      `<text x="${p.x+9}" y="${p.y+18}" font-size="11.5" fill="#141D33">${t.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</text></g>`;
  };
  for(const n of ev)s+=box(n,0,n.sign>0?'#E6F4EC':(n.sign<0?'#FBEAE7':'#F5F7FB'),'#D6DEEA');
  for(const n of ch)s+=box(n,1,'#EDF1F8','#CBD7EC');
  for(const n of tg)s+=box(n,2,'#142248','#142248').replace('fill="#141D33"','fill="#fff"');
  s+='</svg>';
  return s;
}
/* Сетевой граф (стиль карты-сети): круги = узлы (размер — важность, цвет — категория),
   свободная раскладка force-directed, связи: зелёная — вверх, красная — вниз */
const NET_COLORS={supply:'#485E88',demand:'#177A50',macro:'#9A6A0B',export:'#7A5CC4',
  regul:'#B23A2F',news:'#485E88',price:'#142248',us:'#E8862B'};
const NET_LEGEND=[['supply','предложение лома'],['demand','спрос заводов'],['macro','макро (ставка/курс)'],
  ['export','экспорт/мир'],['regul','регуляторика'],['price','цены'],['us','МетОптТорг']];
function drawNet(w){
  const nodes=(w.nodes||[]).map(n=>({...n}));
  const links=(w.links||[]).filter(l=>l.from!==l.to);
  if(!nodes.length)return '<span class="muted">нет узлов</span>';
  const idx={};nodes.forEach((n,i)=>idx[n.id]=i);
  const W=1100,H=640;
  // детерминированный «рандом», чтобы карта не прыгала при перерисовке
  let seed=42;const rnd=()=>{seed=(seed*16807)%2147483647;return seed/2147483647;};
  nodes.forEach(n=>{
    if(n.kind==='price'||n.kind==='us'){n.x=W*0.72+rnd()*W*0.2;n.y=H*0.2+rnd()*H*0.6;}
    else{n.x=W*0.08+rnd()*W*0.55;n.y=H*0.08+rnd()*H*0.84;}
    n.r=(n.kind==='price'||n.kind==='us')?26:10+ (n.size||2)*3.2;
  });
  // симуляция: отталкивание + пружины + гравитация
  for(let it=0;it<260;it++){
    const t=1-it/260;
    for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
      const a=nodes[i],b=nodes[j];
      let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy||1,d=Math.sqrt(d2);
      const rep=2600*t/d2, min=(a.r+b.r+18);
      let f=rep+(d<min?(min-d)*0.35:0);
      dx/=d;dy/=d;a.x-=dx*f;a.y-=dy*f;b.x+=dx*f;b.y+=dy*f;
    }
    for(const l of links){
      const a=nodes[idx[l.from]],b=nodes[idx[l.to]];if(!a||!b)continue;
      let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      const f=(d-150)*0.012*t;dx/=d;dy/=d;
      a.x+=dx*f;a.y+=dy*f;b.x-=dx*f;b.y-=dy*f;
    }
    for(const n of nodes){
      n.x+=(W/2-n.x)*0.004*t;n.y+=(H/2-n.y)*0.004*t;
      if(n.kind==='price'||n.kind==='us')n.x+=(W*0.85-n.x)*0.03*t; // цели тянем вправо
      n.x=Math.max(n.r+4,Math.min(W-n.r-4,n.x));
      n.y=Math.max(n.r+14,Math.min(H-n.r-16,n.y));
    }
  }
  const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  let s=`<svg viewBox="0 0 ${W} ${H+40}" style="width:100%;height:auto;font-family:inherit">`;
  for(const l of links){
    const a=nodes[idx[l.from]],b=nodes[idx[l.to]];if(!a||!b)continue;
    const col=l.sign>0?'#177A50':(l.sign<0?'#B23A2F':'#8390B1');
    const mx=(a.x+b.x)/2+(a.y-b.y)*0.12, my=(a.y+b.y)/2+(b.x-a.x)*0.12;
    s+=`<path d="M${a.x.toFixed(0)},${a.y.toFixed(0)} Q${mx.toFixed(0)},${my.toFixed(0)} ${b.x.toFixed(0)},${b.y.toFixed(0)}" fill="none" stroke="${col}" stroke-width="${(l.strength||1)*1.2}" opacity="0.5"><title>${l.sign>0?'↑':'↓'} ${esc(l.why)} (лаг ${esc(l.lag||'?')})</title></path>`;
  }
  for(const n of nodes){
    const col=NET_COLORS[n.kind]||'#8390B1';
    const label=esc((n.title||'').slice(0,34)+((n.title||'').length>34?'…':''));
    s+=`<g><title>${esc(n.title)}${n.src?' · '+esc(n.src):''}</title>`+
      `<circle cx="${n.x.toFixed(0)}" cy="${n.y.toFixed(0)}" r="${n.r.toFixed(0)}" fill="${col}" opacity="${n.kind==='price'||n.kind==='us'?1:0.85}" stroke="#fff" stroke-width="2"/>`+
      `<text x="${n.x.toFixed(0)}" y="${(n.y+n.r+12).toFixed(0)}" font-size="10.5" text-anchor="middle" fill="#141D33" style="paint-order:stroke;stroke:#EDF0F5;stroke-width:3px">${label}</text></g>`;
  }
  // легенда
  let lx=10;
  s+=`<g transform="translate(0,${H+18})">`;
  for(const[k,name]of NET_LEGEND){
    s+=`<circle cx="${lx+7}" cy="0" r="7" fill="${NET_COLORS[k]}"/><text x="${lx+19}" y="4" font-size="11" fill="#4C5871">${name}</text>`;
    lx+=19+name.length*6.6+26;
  }
  s+='</g></svg>';
  return s;
}
function renderWebData(w){ return w.nodes?drawNet(w):drawWeb(w); }
async function loadWebLatest(){
  try{
    const d=await api('/ai/influence_web');
    if(d.web){$('mx-web').innerHTML=renderWebData(d.web);
      $('mx-web-summary').innerHTML=(d.web.summary?md(d.web.summary):'')+`<div class="note">построено ${new Date(d.created_at).toLocaleString('ru-RU')} · зелёное — толкает цену вверх, красное — вниз, толщина — сила, наведи на линию — почему и лаг</div>`;}
    else $('mx-web').innerHTML='<span class="muted">Паутина ещё не строилась — нажми кнопку выше</span>';
  }catch(e){}
}
async function genWeb(btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> ИИ строит паутину…';
  try{
    const d=await api('/ai/influence_web',{method:'POST'});
    $('mx-web').innerHTML=renderWebData(d.web);
    $('mx-web-summary').innerHTML=d.web.summary?md(d.web.summary):'';
  }catch(e){$('mx-web').innerHTML=`<span class="dn">${e.message}</span>`;}
  btn.disabled=false;btn.textContent='Построить паутину по свежим событиям (до 2 мин)';
}
async function loadDossier(plant){
  $('mx-dossier-wrap').style.display='grid';
  $('mx-dossier').innerHTML='<span class="spin"></span> собираю досье…';
  $('mx-dossier').scrollIntoView({behavior:'smooth',block:'nearest'});
  let d;
  try{d=await api('/matrix/plant?name='+encodeURIComponent(plant));}
  catch(e){$('mx-dossier').innerHTML=`<span class="dn">${e.message}</span>`;return;}
  if(d.error){$('mx-dossier').innerHTML=`<span class="dn">${d.error}</span>`;return;}
  const RN={PERM:'Пермь',KOMI_NORTH:'Коми',HMAO:'ХМАО',SOUTH:'Юг'};
  let h=`<div class="dossier-head"><span class="nm">${d.plant}</span>
    ${d.kb?`<span class="muted">${d.kb.city} · ${d.kb.region} · ${d.kb.transport.join('/')}</span>`:''}
    ${d.cpt_last?`<span>CPT <b>${fmt(d.cpt_last)} ₽/т</b> <span class="badge b-live">MMI</span></span>`:''}
    <span>Спрос: <b>${fmt(d.week_tons)} т/нед</b>${d.free_share_pct!=null?`, СР ${d.free_share_pct}%`:''}</span>
    ${d.kb&&d.kb.price_page?`<a href="${d.kb.price_page}" target="_blank">прайс ↗</a>`:''}</div>`;
  if(d.kb&&d.kb.notes)h+=`<div class="muted" style="margin-bottom:10px">${d.kb.notes}</div>`;
  h+='<div class="dossier-grid">';
  // цены за 2 недели
  if(d.price_series.length){
    h+='<div><h4>Цена CPT по дням (MMI)</h4><table class="tbl">';
    for(const p of d.price_series.slice(-8))h+=`<tr><td class="muted">${p.date}</td><td><b>${fmt(p.price)}</b></td></tr>`;
    h+='</table></div>';
  }
  // откуда везут
  if(d.supply_regions.length){
    h+='<div><h4>Откуда везут (нед., MMI)</h4><table class="tbl">';
    for(const s of d.supply_regions.slice(0,8))h+=`<tr><td>${s.region}</td><td>${fmt(s.tons)} т</td></tr>`;
    h+='</table></div>';
  }
  // поставщики из ЖД
  if(d.suppliers.length){
    h+='<div><h4>Поставщики (повагонная база)</h4><table class="tbl">';
    for(const s of d.suppliers.slice(0,8))h+=`<tr><td>${s.consignor.slice(0,26)}${s.captive?' <span class="badge b-cache">кэптив</span>':''}</td><td>${fmt(s.tons)} т</td></tr>`;
    h+='</table></div>';
  }
  // netback по методике
  if(d.netbacks.length){
    h+='<div><h4>Наш netback (CPT − факт-тариф)</h4><table class="tbl">';
    for(const n of d.netbacks)h+=`<tr><td>${RN[n.region]}</td><td>−${fmt(n.tariff_per_t)}</td>
      <td><b class="up">${fmt(n.netback)}</b> <span class="badge ${n.tariff_quality==='live'?'b-live':'b-calc'}">${n.tariff_quality==='live'?'факт ЖД':'оценка'}</span></td></tr>`;
    h+='</table></div>';
  }
  // наши продажи
  if(d.our_sales){
    h+=`<div><h4>Наши продажи этому заводу</h4>
      <div>Всего <b>${fmt(d.our_sales.qty_t)} т</b> по ср. <b>${fmt(d.our_sales.avg_price)} ₽/т</b>
      <div class="muted">последняя: ${d.our_sales.last_date}</div></div><table class="tbl">`;
    for(const m of d.our_sales.months)h+=`<tr><td class="muted">${m.month}</td><td>${fmt(m.qty_t)} т</td><td>${fmt(m.avg_price)}</td></tr>`;
    h+='</table></div>';
  }else{
    h+='<div><h4>Наши продажи</h4><div class="muted">Продаж этому заводу в 1С не найдено</div></div>';
  }
  h+=`</div><div class="note">${d.docs_note}</div>`;
  $('mx-dossier').innerHTML=h;
}

/* ═══ Радар ═══ */
async function loadRadar(){
  const d=await api('/radar');
  let rows='<tr><th></th><th>Индикатор</th><th>Значение</th><th>Комментарий</th></tr>';
  for(const c of d.checks){
    rows+=`<tr><td><span class="lvl lvl-${c.level}"></span></td>
      <td>${c.indicator_n}. ${c.name}</td><td>${c.value_text}</td>
      <td class="muted">${c.message}</td></tr>`;
  }
  $('radar-tbl').innerHTML=rows;
}

/* ═══ Калькулятор ═══ */
async function loadCalc(){
  const p=new URLSearchParams({from_region:$('c-region').value,metal_code:$('c-metal').value,
    weight_ton:$('c-weight').value,transport:$('c-transport').value});
  const d=await api('/calculator?'+p);
  $('calc-rate-note').textContent=d.logistics_rate_note;
  const b=d.best_option;
  $('calc-best').innerHTML=b?`<div class="best"><div class="t">${b.plant_name} · ${b.plant_city}</div>
    <div>Чистыми <b class="up">${fmt(b.net_per_ton)} ₽/т</b> · за ${d.weight_ton} т = <b>${fmt(b.net_total_rub)} ₽</b></div>
    <div class="muted">Цена ${fmt(b.plant_price_ton)} − логистика ${fmt(b.logistics_per_ton)} (${b.distance_km} км) ·
      источник цены: ${b.price_source} <span class="badge ${b.price_quality==='live'?'b-live':'b-cache'}">${b.price_quality==='live'?'live':'справочник'}</span></div></div>`:'';
  let rows='<tr><th>Завод</th><th>Город</th><th>Км</th><th>Цена ₽/т</th><th>Логистика ₽/т</th><th>Чистыми ₽/т</th><th>Итого ₽</th><th>Цена</th></tr>';
  for(const r of d.all_options){
    rows+=`<tr><td>${r.plant_name}</td><td class="muted">${r.plant_city}</td><td>${r.distance_km}</td>
      <td>${fmt(r.plant_price_ton)}</td><td>${fmt(r.logistics_per_ton)}</td>
      <td class="${r.net_per_ton>16000?'up':(r.net_per_ton>14000?'acc':'dn')}">${fmt(r.net_per_ton)}</td>
      <td>${fmt(r.net_total_rub)}</td>
      <td><span class="badge ${r.price_quality==='live'?'b-live':'b-cache'}">${r.price_quality==='live'?'live':'спр.'}</span></td></tr>`;
  }
  $('calc-tbl').innerHTML=rows;
}

/* ═══ Прогноз ═══ */
async function initForecast(){
  const d=await api('/forecast/series');
  $('f-series').innerHTML=d.series.map(s=>`<option value="${s.id}">${s.name}</option>`).join('');
  const factorCard=(title,factors,signal)=>{
    let rows='<tr><th>Фактор</th><th>Вес</th><th>Напр.</th></tr>';
    for(const f of factors)rows+=`<tr><td title="${f.note||''}">${f.name}</td><td>${f.weight}</td>
      <td class="${f.direction>0?'up':'dn'}">${f.direction>0?'+':''}${f.direction}</td></tr>`;
    return `<div class="card"><h3>${title} · сигнал ${signal>0?'+':''}${signal}</h3>
      <div class="tbl-wrap"><table class="tbl">${rows}</table></div></div>`;
  };
  api('/ai/factors_review').then(r=>{if(r.content)$('f-ai-review').innerHTML=md(r.content)+`<div class="note">от ${new Date(r.created_at).toLocaleString('ru-RU')}</div>`;}).catch(()=>{});
  $('f-factors').innerHTML=factorCard('Чёрный',d.factors.black,d.signals.black)+
    factorCard('Медь',d.factors.copper,d.signals.copper)+
    factorCard('Алюминий',d.factors.alum,d.signals.alum);
  $('fs-series').innerHTML=d.series.map(s=>`<option value="${s.id}">${s.name}</option>`).join('');
  await loadForecast();
  loadScenarios().catch(()=>{});
}
async function loadForecast(){
  const d=await api('/forecast?series='+$('f-series').value+'&months='+$('f-months').value);
  $('f-base-note').innerHTML=`База: <b>${fmt(d.base)}</b> (${d.base_source}, ${d.base_date}) · сигнал ${d.signal>0?'+':''}${d.signal}`;
  $('f-phase').textContent=d.phase_note?('Фаза 1: '+d.phase_note.phase1+' · Фаза 2: '+d.phase_note.phase2):'';
  const labels=['сейчас',...d.points.map(p=>'+'+p.month+' мес')];
  mkChart('f-chart',{type:'line',data:{labels,datasets:[
    {label:'Прогноз',data:[d.base,...d.points.map(p=>p.price)],borderColor:C.steel,backgroundColor:'rgba(72,94,136,.08)',tension:.3,fill:false},
    {label:'Ниж. граница',data:[d.base,...d.points.map(p=>p.lower)],borderColor:'rgba(131,144,177,.55)',borderDash:[4,4],pointRadius:0,fill:false},
    {label:'Верх. граница',data:[d.base,...d.points.map(p=>p.upper)],borderColor:'rgba(131,144,177,.55)',borderDash:[4,4],pointRadius:0,fill:'-1',backgroundColor:'rgba(131,144,177,.10)'}]},
    options:{maintainAspectRatio:false,plugins:{legend:{labels:{color:C.t3,font:{size:10}}}},scales:baseScales}});
}

/* ═══ Рынок ═══ */
async function loadMarket(){
  await loadMarketChart();
  const plants=await api('/market/plants');
  let rows='<tr><th>Завод</th><th>Город</th><th>Справочник (с доставкой авто)</th><th>Live-прайс</th></tr>';
  for(const p of plants){
    rows+=`<tr><td>${p.name}</td><td class="muted">${p.city}</td>
      <td>${fmt(p.price_cpt_auto)} <span class="badge b-cache">Rusmet 06/25</span></td>
      <td>${p.live?fmt(p.live.value)+' '+badge(p.live):'<span class="muted">нет</span>'}</td></tr>`;
  }
  $('plants-tbl').innerHTML=rows;
  api('/market/local_prices').then(lp=>{
    const mname={lom3a_local:'Лом 3А',copper_local:'Медь',alum_local:'Алюминий'};
    let h='<tr><th>Приёмка</th><th>Город</th><th>Металл</th><th>Цена ₽/т</th><th>Как</th><th>Дата</th></tr>';
    for(const p of lp.slice(0,40))h+=`<tr><td>${p.source.slice(0,30)}</td><td class="muted">${p.city||p.region||'—'}</td>
      <td>${mname[p.metric]||p.metric}</td><td><b>${fmt(p.value)}</b></td>
      <td><span class="badge ${p.via==='llm'?'b-manual':'b-live'}">${p.via||'парсер'}</span></td>
      <td class="muted">${p.date}</td></tr>`;
    $('local-prices-tbl').innerHTML=lp.length?h:'<tr><td class="muted">Цены появятся после первого обхода (экран «Данные» → «Обойти сейчас»)</td></tr>';
  }).catch(()=>{});
  const news=await api('/news');
  $('news-box').innerHTML=news.length?news.map(n=>{
    const dir=n.ai_direction>0.15?'<span class="up">▲</span>':(n.ai_direction<-0.15?'<span class="dn">▼</span>':'<span class="muted">→</span>');
    return `<div style="padding:8px 0;border-bottom:1px solid rgba(28,38,56,.5)">
      ${dir} <a href="${n.url}" target="_blank">${n.title}</a>
      <div class="muted" style="font-size:11px">${n.source} · ${new Date(n.collected_at).toLocaleDateString('ru-RU')}
      ${n.ai_impact?' · '+n.ai_impact:''}</div></div>`;
  }).join(''):'<span class="muted">Новости появятся после первого сбора</span>';
}
async function loadMarketChart(){
  const metric=$('m-metric').value;
  const d=await api('/market/history?metric='+metric+'&days=365');
  mkChart('m-chart',{type:'line',data:{labels:d.points.map(p=>p.date),datasets:[
    {label:metric,data:d.points.map(p=>p.value),borderColor:C.steel,backgroundColor:'rgba(72,94,136,.08)',tension:.25,fill:true,
     pointBackgroundColor:d.points.map(p=>p.quality==='live'?C.ok:C.warn)}]},
    options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:baseScales}});
}
async function triggerCollect(btn){
  btn.disabled=true;btn.textContent='Сбор запущен…';
  try{await api('/collect',{method:'POST'});}catch(e){}
  setTimeout(()=>{btn.disabled=false;btn.textContent='Обновить данные сейчас';loadMarketChart();},90000);
}

/* ═══ Продажи vs рынок ═══ */
async function loadSales(){
  const d=await api('/sales/vs_market');
  const ours=d.our_sales;
  const idx=d.market_index;
  const ds=[{label:'Наша цена продажи 3А',data:ours.map(m=>m.avg_price),borderColor:C.steel,tension:.25}];
  if(idx)ds.push({label:'Индекс '+idx.source+' ('+idx.basis+')',data:ours.map(()=>idx.value),borderColor:C.steel2,borderDash:[6,4],pointRadius:0});
  mkChart('sv-chart',{type:'line',data:{labels:ours.map(m=>m.month),datasets:ds},
    options:{maintainAspectRatio:false,plugins:{legend:{labels:{color:C.t3,font:{size:10}}}},scales:baseScales}});
  $('sv-note').textContent=d.note+(ours.length?' · Последний месяц: '+ours[ours.length-1].month+', '+fmt(ours[ours.length-1].avg_price)+' ₽/т при '+fmt(ours[ours.length-1].qty_t)+' т':'');
  const buyers=await api('/sales/buyers');
  let rows='<tr><th>Покупатель</th><th>Объём, т</th><th>Выручка, ₽</th><th>Ср. цена ₽/т</th></tr>';
  for(const b of buyers.slice(0,15))rows+=`<tr><td>${b.buyer}</td><td>${fmt(b.qty_t)}</td><td>${fmt(b.revenue_rub)}</td><td>${fmt(b.avg_price)}</td></tr>`;
  $('buyers-tbl').innerHTML=rows||'<tr><td class="muted">Нет данных — загрузите выгрузку 1С на экране «Данные»</td></tr>';
  const vs=await api('/sales/vs_plants');
  let vh='<tr><th>Контрагент</th><th>Регион</th><th>Тонн (12м)</th><th>Наша цена ₽/т</th><th>Цена завода (рынок)</th><th>Δ ₽/т</th></tr>';
  for(const r of vs.slice(0,18)){
    const our=r.our_recent_price||r.our_price;
    vh+=`<tr><td>${r.buyer.slice(0,36)}</td><td>${REGION_RU[r.region]||r.region||'—'}</td>
      <td>${fmt(r.qty_t)}</td>
      <td>${fmt(our)}${r.our_recent_price?' <span class="muted">свежая</span>':' <span class="muted">ср. 12м</span>'}</td>
      <td>${r.market_price?fmt(r.market_price)+' <span class="badge b-live" title="'+(r.market_source||'')+'">MMI</span>':'<span class="muted">нет котировки</span>'}</td>
      <td class="${r.delta==null?'muted':(r.delta<-500?'dn':(r.delta>500?'up':''))}">${r.delta!=null?fmt(r.delta):'—'}</td></tr>`;
  }
  $('buyers-vs-tbl').innerHTML=vs.length?vh:'<tr><td class="muted">Нет данных</td></tr>';
}
async function loadScenarios(){
  const series=$('fs-series').value||$('f-series').value;
  const d=await api('/forecast/scenarios?series='+series+'&months=6');
  const labels=['сейчас','+1','+2','+3','+4','+5','+6 мес'];
  const ds=[];
  const colors={base:C.steel,opt:C.ok,pes:C.crit};
  for(const sc of d.scenarios)ds.push({label:sc.name,data:[d.base,...sc.points],
    borderColor:colors[sc.id],borderWidth:sc.id==='base'?3:2,
    borderDash:sc.id==='base'?[]:[6,4],tension:.3,pointRadius:2});
  for(const a of d.compare_bases){
    if(Math.abs(a.base-d.base)<1)continue;
    ds.push({label:a.source.slice(0,30),data:[a.base,...a.points],
      borderColor:'rgba(131,144,177,.6)',borderWidth:1.5,borderDash:[2,3],tension:.3,pointRadius:0});
  }
  mkChart('fs-chart',{type:'line',data:{labels,datasets:ds},
    options:{maintainAspectRatio:false,plugins:{legend:{labels:{color:C.t3,font:{size:10}}}},scales:baseScales}});
  let h='<tr><th>Вариант</th><th>База</th><th>+1 мес</th><th>+3 мес</th><th>+6 мес</th><th>Δ за 6м</th></tr>';
  const row=(name,base,pts,bold)=>{
    const d6=pts[5]-base;
    return `<tr${bold?' style="background:var(--steel-tint)"':''}><td>${name}</td><td>${fmt(base)}</td><td>${fmt(pts[0])}</td><td>${fmt(pts[2])}</td><td><b>${fmt(pts[5])}</b></td><td class="${d6<0?'dn':'up'}">${d6>0?'+':''}${fmt(d6)}</td></tr>`;
  };
  for(const sc of d.scenarios)h+=row(sc.name,d.base,sc.points,sc.id==='base');
  for(const a of d.compare_bases)h+=row('от базы: '+a.source.slice(0,34)+' ('+a.date+')',a.base,a.points,false);
  $('fs-tbl').innerHTML=h;
}
async function genFactorsReview(btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> ИИ анализирует рынок (до 2 мин)…';
  try{
    const d=await api('/ai/factors_review',{method:'POST'});
    $('f-ai-review').innerHTML=md(d.content);
  }catch(e){$('f-ai-review').innerHTML=`<span class="dn">${e.message}</span>`;}
  btn.disabled=false;btn.textContent='Запустить ревизию (ИИ проверит факторы по свежим данным)';
}

/* ═══ Логистика ═══ */
async function loadLogistics(){
  const d=await api('/trips/rates');
  const s=d.summary;
  $('log-kpi').innerHTML=s?
    `<div class="kpi"><div class="kpi-l">Медианный тариф авто</div><div class="kpi-v">${fmt(s.median)}</div><div class="kpi-u">₽/т/100км · ${s.trips} рейсов · ${s.source}</div></div>`+
    `<div class="kpi"><div class="kpi-l">25-й перцентиль (дёшево)</div><div class="kpi-v up">${fmt(s.p25)}</div><div class="kpi-u">₽/т/100км</div></div>`+
    `<div class="kpi"><div class="kpi-l">75-й перцентиль (дорого)</div><div class="kpi-v dn">${fmt(s.p75)}</div><div class="kpi-u">₽/т/100км — всё выше требует разбора</div></div>`
    :'<div class="card muted">Нет данных рейсов — загрузите «Отвесную» на экране «Данные»</div>';
  let rows='<tr><th>Откуда</th><th>Куда</th><th>Рейсов</th><th>Км</th><th>₽/т/100км</th></tr>';
  for(const r of d.routes)rows+=`<tr><td>${r.from||'—'}</td><td>${r.to||'—'}</td><td>${r.trips}</td><td>${fmt(r.km)}</td><td>${fmt(r.rate_per_100km)}</td></tr>`;
  $('routes-tbl').innerHTML=rows;
  const exp=await api('/trips/expensive');
  let er='<tr><th>Дата</th><th>Откуда → куда</th><th>Км</th><th>Т</th><th>₽/т/100км</th><th>Доставка</th></tr>';
  for(const t of exp)er+=`<tr><td>${t.date}</td><td>${t.from} → ${t.to}</td><td>${t.km}</td><td>${t.weight_t}</td>
    <td class="dn">${fmt(t.rate_per_100km)}</td><td class="muted">${t.delivery}</td></tr>`;
  $('exp-tbl').innerHTML=exp.length?er:'<tr><td class="muted">Аномально дорогих рейсов не найдено</td></tr>';
}

/* ═══ ИИ ═══ */
async function loadAi(){
  const st=await api('/ai/status');
  $('ai-provider').innerHTML=st.available?
    `Провайдер: <b class="up">${st.provider}</b> — ищет свежие данные в интернете и связывает их с данными платформы`:
    `<span class="dn">ИИ не подключён</span> — добавь PERPLEXITY_API_KEY в .env на сервере`;
  const b=await api('/ai/briefing');
  if(b.content)$('ai-briefing').innerHTML=md(b.content)+`<div class="note">${b.model} · ${new Date(b.created_at).toLocaleString('ru-RU')}</div>`;
  const h=await api('/ai/history');
  $('ai-chat').innerHTML=h.filter(x=>x.kind==='chat').slice(0,5).reverse()
    .map(x=>`<div class="chat-q">${x.question}</div><div class="chat-a md">${md(x.content)}</div>`).join('');
}
async function askAi(){
  const q=$('ai-q').value.trim();if(!q)return;
  $('ai-btn').disabled=true;$('ai-btn').innerHTML='<span class="spin"></span>';
  $('ai-chat').insertAdjacentHTML('beforeend',`<div class="chat-q">${q}</div><div class="chat-a" id="ai-wait"><span class="spin"></span> думаю…</div>`);
  try{
    const d=await api('/ai/chat',{method:'POST',body:JSON.stringify({question:q})});
    $('ai-wait').outerHTML=`<div class="chat-a md">${md(d.answer)}</div>`;
    $('ai-q').value='';
  }catch(e){$('ai-wait').outerHTML=`<div class="chat-a dn">${e.message}</div>`;}
  $('ai-btn').disabled=false;$('ai-btn').textContent='Спросить';
}
async function genBriefing(btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> генерирую (до минуты)…';
  try{
    const d=await api('/ai/briefing/generate',{method:'POST'});
    $('ai-briefing').innerHTML=md(d.content);
  }catch(e){$('ai-briefing').innerHTML=`<span class="dn">${e.message}</span>`;}
  btn.disabled=false;btn.textContent='Сгенерировать брифинг сейчас';
}

/* Мини-markdown (заголовки, списки, ТАБЛИЦЫ, жирный, сноски) */
function md(t){
  t=t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  // markdown-таблицы → фирменные таблицы
  const lines=t.split('\n'); const out=[]; let tbl=null;
  const flush=()=>{ if(!tbl)return;
    let h='<div class="tbl-wrap" style="margin:8px 0"><table class="tbl">';
    tbl.forEach((cells,i)=>{ const tag=i===0?'th':'td';
      h+='<tr>'+cells.map(c=>`<${tag}>${c}</${tag}>`).join('')+'</tr>'; });
    h+='</table></div>'; out.push(h); tbl=null; };
  for(const ln of lines){
    if(/^\s*\|.*\|\s*$/.test(ln)){
      const cells=ln.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      if(cells.length&&cells.every(c=>/^:?-{2,}:?$/.test(c)))continue;  // разделитель |---|
      (tbl=tbl||[]).push(cells); continue;
    }
    flush(); out.push(ln);
  }
  flush();
  return out.join('\n')
    .replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>')
    .replace(/^\s*---+\s*$/gm,'')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\[(\d+)\]/g,'<sup class="muted">[$1]</sup>')
    .replace(/^[-•*] (.*)$/gm,'<li>$1</li>').replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g,'<ul>$1</ul>')
    .replace(/\n{2,}/g,'<br>').replace(/\n(?=<)/g,'').replace(/\n/g,' ');
}

/* ═══ Обзвон ═══ */
const SV_ROWS=[['PERM','Пермь'],['KOMI_PERM','Коми-Пермяцкий'],['HMAO','ХМАО'],['SOUTH','Юг (Волгоград)']];
function initSurveyForm(){
  $('sv-date').value=new Date().toISOString().slice(0,10);
  let rows='<tr><th>Регион</th><th>FCA — завод забирает, ₽/т</th><th>С доставкой — мы везём, ₽/т</th><th>Комментарий</th></tr>';
  for(const[code,name]of SV_ROWS){
    rows+=`<tr><td>${name}</td>
      <td><input type="number" id="sv-${code}-FCA" style="width:110px" placeholder="—"></td>
      <td><input type="number" id="sv-${code}-CPT_AUTO" style="width:110px" placeholder="—"></td>
      <td><input id="sv-${code}-c" style="width:100%" placeholder=""></td></tr>`;
  }
  $('sv-form').innerHTML=rows;
  loadSurveyHistory();
}
async function saveSurvey(){
  const entries=[];
  for(const[code]of SV_ROWS){
    for(const basis of['FCA','CPT_AUTO']){
      const v=parseFloat($(`sv-${code}-${basis}`).value);
      if(v>0)entries.push({region:code,basis,price:v,comment:$(`sv-${code}-c`).value||''});
    }
  }
  if(!entries.length){$('sv-msg').textContent='Заполни хотя бы одну цену';return;}
  await api('/survey',{method:'POST',body:JSON.stringify({survey_date:$('sv-date').value,entries})});
  $('sv-msg').innerHTML='<span class="up">Сохранено ✓ — прогноз теперь считается от этих цен</span>';
  loadSurveyHistory();
}
async function saveManualQuotes(){
  let n=0;
  for(const m of['plant_MMK','wagon_rate','armatura_a500','ruslom_index','metallplace_index']){
    const v=parseFloat($('mq-'+m).value);
    if(v>0){await api('/manual_quote',{method:'POST',body:JSON.stringify({metric:m,value:v})});n++;}
  }
  $('mq-msg').innerHTML=n?'<span class="up">Записано ✓ — радар обновится при следующей проверке</span>':'Заполни хотя бы одно поле';
}
async function loadSurveyHistory(){
  const d=await api('/survey/history?region='+$('svh-region').value+'&basis='+$('svh-basis').value);
  mkChart('svh-chart',{type:'line',data:{labels:d.map(x=>x.date),datasets:[
    {label:'Цена обзвона',data:d.map(x=>x.price),borderColor:C.ok,tension:.25}]},
    options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:baseScales}});
}

/* ═══ Данные ═══ */
async function loadData(){
  await refreshImports();
  loadCrawler();
  const s=await api('/summary');
  const fr=s.company_freshness;
  $('freshness-box').innerHTML=`Продажи (1С): до <b>${fr.sales||'нет'}</b> · Рейсы: до <b>${fr.trips||'нет'}</b> · Движения ТМЦ: до <b>${fr.stock||'нет'}</b>`;
}
async function loadCrawler(){
  try{
    const st=await api('/crawler/status');
    $('cr-kpi').innerHTML=
      `<div class="kpi"><div class="kpi-l">Сайтов в реестре</div><div class="kpi-v">${st.total}</div><div class="kpi-u">обход каждые 6 ч</div></div>`+
      `<div class="kpi"><div class="kpi-l">С ценой</div><div class="kpi-v up">${st.with_price}</div><div class="kpi-u">получили прайс</div></div>`+
      `<div class="kpi"><div class="kpi-l">Через ИИ</div><div class="kpi-v">${st.via_llm}</div><div class="kpi-u">извлечено LLM</div></div>`+
      `<div class="kpi"><div class="kpi-l">Найдено разведкой</div><div class="kpi-v">${st.ai_discovered}</div><div class="kpi-u">ИИ-поиск сайтов</div></div>`;
    const rows=await api('/crawler/sources');
    let h='<tr><th>Источник</th><th>Регион/город</th><th>Цена 3А</th><th>Метод</th><th>Обновлён</th><th>Статус</th></tr>';
    for(const s of rows){
      h+=`<tr><td><a href="${s.url}" target="_blank">${s.name.slice(0,34)}</a>${s.added_by==='ai_discover'?' <span class="badge b-manual">ИИ</span>':''}</td>
        <td class="muted">${s.city||s.region||'—'}</td>
        <td>${s.last_price?'<b>'+fmt(s.last_price)+'</b>':'—'}</td>
        <td>${s.last_method?'<span class="badge '+(s.last_method==='llm'?'b-manual':'b-live')+'">'+s.last_method+'</span>':''}</td>
        <td class="muted">${s.last_ok?new Date(s.last_ok).toLocaleDateString('ru-RU'):'ещё нет'}</td>
        <td class="${s.fail_count>2?'dn':'muted'}">${s.fail_count?'сбоев: '+s.fail_count+' '+(s.note||''):'ок'}</td></tr>`;
    }
    $('cr-tbl').innerHTML=h;
  }catch(e){}
}
async function crawlRun(btn){
  btn.disabled=true;btn.textContent='Обход запущен…';
  try{const r=await api('/crawler/run',{method:'POST'});$('cr-msg').textContent=r.message;}catch(e){$('cr-msg').textContent=e.message;}
  setTimeout(()=>{btn.disabled=false;btn.textContent='Обойти сейчас';loadCrawler();},60000);
}
async function crawlDiscover(btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> ИИ ищет сайты…';
  try{
    const r=await api('/crawler/discover',{method:'POST',body:JSON.stringify({region:$('cr-region').value})});
    $('cr-msg').innerHTML=`<span class="up">Добавлено новых: ${r.added}</span> (дублей пропущено: ${r.skipped_dup})`;
    loadCrawler();
  }catch(e){$('cr-msg').textContent=e.message;}
  btn.disabled=false;btn.textContent='Найти новые сайты';
}
async function crawlAdd(){
  const url=$('cr-url').value.trim();if(!url)return;
  try{
    const r=await api('/crawler/add',{method:'POST',body:JSON.stringify({url})});
    $('cr-msg').textContent=r.ok?'Добавлен ✓ (цена появится после обхода)':r.message;
    if(r.ok){$('cr-url').value='';loadCrawler();}
  }catch(e){$('cr-msg').textContent=e.message;}
}
async function refreshImports(){
  const rows=await api('/imports');
  let h='<tr><th>Когда</th><th>Файл</th><th>Тип</th><th>Загружено</th><th>Пропущено</th><th>Статус</th></tr>';
  for(const r of rows)h+=`<tr><td class="muted">${new Date(r.at).toLocaleString('ru-RU')}</td>
    <td>${r.file}</td><td>${r.kind}</td><td>${fmt(r.loaded)}</td><td>${fmt(r.skipped)}</td>
    <td class="${r.status==='ok'?'up':(r.status==='error'?'dn':'muted')}">${r.status} ${r.message||''}</td></tr>`;
  $('imports-tbl').innerHTML=rows.length?h:'<tr><td class="muted">Импортов ещё не было</td></tr>';
}
async function uploadFiles(files){
  const fd=new FormData();
  for(const f of files)fd.append('files',f);
  $('upload-progress').innerHTML='<div class="card"><span class="spin"></span> Загрузка и импорт… (большие файлы — до нескольких минут)</div>';
  try{
    const r=await fetch('/api/upload_1c',{method:'POST',body:fd});
    const d=await r.json();
    $('upload-progress').innerHTML='<div class="card">'+d.results.map(x=>
      `<div>${x.file}: <b class="${x.status==='ok'?'up':'dn'}">${x.status}</b> · тип ${x.kind} · +${fmt(x.loaded)} строк (${fmt(x.skipped)} пропущено)</div>`).join('')+'</div>';
    refreshImports();
  }catch(e){$('upload-progress').innerHTML=`<div class="card dn">Ошибка: ${e.message}</div>`;}
}
const dz=$('dz');
if(dz){
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
  dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');uploadFiles(e.dataTransfer.files);});
  $('dz-file').addEventListener('change',e=>uploadFiles(e.target.files));
}

/* ═══ Загрузчики экранов ═══ */
const LOADERS={
  summary:loadSummary,margin:loadMargin,radar:loadRadar,
  calc:async()=>{await loadCalc();},forecast:initForecast,market:loadMarket,
  sales:loadSales,logistics:loadLogistics,ai:loadAi,rail:loadRail,matrix:loadMatrix,
  survey:async()=>initSurveyForm(),data:loadData,
};

/* ═══ Старт ═══ */
(async()=>{
  try{
    const me=await api('/me');
    if(!me.user){showLogin();return;}
    $('app').style.display='grid';
    $('foot-user').textContent='👤 '+me.user;
    loaded.summary=true;
    loadSummary().catch(console.warn);
  }catch(e){showLogin();}
})();
