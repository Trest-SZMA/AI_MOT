/* ================================================================
   Вкладки дашборда. DATA/render/утилиты определены в шаблоне.
   ================================================================ */

/* ---------- общий раскрывающийся узел дерева ---------- */
function treeNode(label, valHtml, buildChildren, opts={}){
  const node = el('div','node');
  const row = el('div','row'+(opts.open?' open':''));
  row.innerHTML = '<span class="tw">▶</span>'+(opts.code?'<span class="code">'+esc(opts.code)+'</span>':'')
    +'<span class="lbl">'+label+'</span>'+(opts.badge||'')+'<span class="val">'+valHtml+'</span>';
  const kids = el('div','children'+(opts.open?' open':''));
  let built=opts.open;
  if(opts.open) buildChildren(kids);
  row.addEventListener('click',ev=>{ if(ev.target.closest('a'))return;
    const open=kids.classList.toggle('open'); row.classList.toggle('open',open);
    if(open&&!built){ buildChildren(kids); built=true; } });
  node.appendChild(row); node.appendChild(kids); return node;
}
function tval(t){ return fmt(t)+' <span class="unit">т</span>'; }
const REAL = r => r.series!=='БЕЗ СЕРИИ' && !String(r.series).startsWith('площадка:');

/* ================================================================
   1) ПОДРАЗДЕЛЕНИЯ: база → склад → категория → отч.группа → позиция → серии → документы
   ================================================================ */
TABS.podr = function(app){
  const rows = DATA.rows;
  app.appendChild(kpiCards(rows));
  const bar = el('div','toolbar');
  bar.innerHTML = '<input type="search" id="q" placeholder="Поиск: серия / код / номенклатура / договор…">'
    + '<select id="fbadge"><option value="">происхождение: все</option>'
    + Object.keys(BADGE_RANK).map(b=>'<option>'+b+'</option>').join('')+'</select>'
    + '<span class="right muted" id="cnt"></span>';
  app.appendChild(bar);
  const wrap = el('div','panel tree'); app.appendChild(wrap);
  function draw(){
    const q=$('#q').value.trim().toLowerCase(), fb=$('#fbadge').value;
    let data = rows;
    if(q) data = data.filter(r=>(r.series+' '+r.code+' '+r.name+' '+r.contract+' '+r.contragent).toLowerCase().includes(q));
    if(fb) data = data.filter(r=>r.badge===fb);
    wrap.innerHTML='';
    $('#cnt').textContent = fmt(sum(data,r=>r.tonnes))+' т в '+data.length+' строк';
    const byBase = groupBy(data, r=>r.base);
    [...byBase.keys()].sort(baseSort).forEach(base=>{
      const brows=byBase.get(base);
      wrap.appendChild(treeNode('<b>'+esc(base)+'</b>', tval(sum(brows,r=>r.tonnes)), kids=>{
        const bySkl=groupBy(brows,r=>r.sklad);
        [...bySkl.keys()].sort().forEach(skl=>{
          const srows=bySkl.get(skl);
          kids.appendChild(treeNode(esc(skl), tval(sum(srows,r=>r.tonnes)), k2=>{
            const byCat=groupBy(srows,r=>r.category);
            [...byCat.keys()].sort().forEach(cat=>{
              const crows=byCat.get(cat);
              k2.appendChild(treeNode('🗂 '+esc(cat), tval(sum(crows,r=>r.tonnes)), k3=>{
                const byG=groupBy(crows,r=>r.report_group);
                [...byG.keys()].sort().forEach(g=>{
                  const grows=byG.get(g);
                  k3.appendChild(treeNode(esc(g)+' <span class="tag">(итого)</span>', tval(sum(grows,r=>r.tonnes)), k4=>{
                    const byPos=groupBy(grows,r=>r.code+'|'+r.name);
                    [...byPos.keys()].forEach(pk=>{
                      const prows=byPos.get(pk), p0=prows[0];
                      k4.appendChild(treeNode(esc(p0.name), tval(sum(prows,r=>r.tonnes)), k5=>{
                        prows.sort((a,b)=>b.tonnes-a.tonnes).forEach(r=>k5.appendChild(seriesLeaf(r)));
                      },{code:p0.code}));
                    });
                  }));
                });
              }));
            });
          }));
        });
      }, {open:byBase.size<=1}));
    });
  }
  $('#q').addEventListener('input',draw); $('#fbadge').addEventListener('change',draw);
  draw();
};

function seriesLeaf(r){
  const days=daysLying(r.arrival_dt);
  const extra=[]; if(r.contragent)extra.push(esc(r.contragent));
  if(r.datavyvoza)extra.push('вывоз до '+esc(r.datavyvoza));
  if(days!=null)extra.push('лежит '+days+' дн · '+staleCat(days));
  // показываем ПОЛНУЮ строку серии (номера могут совпадать у разных серий)
  const full=r.series_full||r.series_display;
  const fullTag=(full&&full!==r.series_display)?' <span class="muted" style="font-weight:400">· '+esc(full)+'</span>':'';
  const lbl='<span class="stale" style="background:'+staleColor(days)+';display:inline-block;margin-right:6px"></span>'
    +'<b>'+esc(r.series_display)+'</b>'+fullTag+(extra.length?' <span class="tag">— '+extra.join(' · ')+'</span>':'');
  return treeNode(lbl, tval(r.tonnes), kids=>{
    const d=el('div','docs');
    if(!r.docs||!r.docs.length){ d.innerHTML='<div class="doc">нет документов-приходов (серия из строки факта)</div>'; }
    else r.docs.forEach(doc=>{
      if(doc.note){   // синтетическая пометка «пересчёт» — заполнить вручную
        d.appendChild(el('div','doc',
          '<span class="rn" style="color:var(--yellow)">⚑ ПЕРЕСЧЁТ</span>'
          +'<span class="dt">'+esc(doc.dt||'')+'</span><span>'+fmt(doc.qty)+' '+esc(r.unit||'')+'</span>'
          +'<span class="badge b-ручная" style="flex:none">заполнить вручную</span>'
          +'<span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis">'+esc(doc.partia||'')+'</span>'));
        return;
      }
      const dd=daysLying(doc.dt);
      d.appendChild(el('div','doc','<span class="rn">'+esc(doc.regnum)+'</span>'
        +'<span class="dt">'+esc(doc.dt)+'</span><span>'+fmt(doc.qty)+' '+esc(r.unit||'')+'</span>'
        +'<span class="muted">'+(dd!=null?dd+' дн':'')+'</span>'
        +'<span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis">'+esc(doc.partia||'')+'</span>')); });
    kids.appendChild(d);
  }, {badge:badge(r.badge)});
}

/* база сортировка по номеру 5.x */
function baseSort(a,b){ const na=parseFloat((a.match(/\d+/)||[999])[0]), nb=parseFloat((b.match(/\d+/)||[999])[0]);
  return na-nb || a.localeCompare(b,'ru'); }

function kpiCards(rows){
  const c=el('div','cards');
  const tot=sum(rows,r=>r.tonnes), real=sum(rows.filter(REAL),r=>r.tonnes);
  const noser=sum(rows.filter(r=>r.series==='БЕЗ СЕРИИ'),r=>r.tonnes);
  const bases=new Set(rows.map(r=>r.base)).size, series=new Set(rows.filter(REAL).map(r=>r.series)).size;
  const def=[['Всего, т',fmt(tot)],['С реальной серией, т',fmt(real)+' ('+(tot?Math.round(real/tot*100):0)+'%)'],
    ['БЕЗ СЕРИИ, т',fmt(noser)],['Баз «5»',bases],['Уникальных серий',series]];
  def.forEach(([k,v])=>{ const d=el('div','card'); d.innerHTML='<div class="k">'+k+'</div><div class="v small">'+v+'</div>'; c.appendChild(d); });
  return c;
}

/* ================================================================
   2) СЕРИИ: направление → договор → серия → карточка (базы→группы→позиции)
   ================================================================ */
TABS.series = function(app){
  const rows = DATA.rows.filter(REAL);
  const bar=el('div','toolbar'); bar.innerHTML='<input type="search" id="qs" placeholder="Поиск серии / договора…"><span class="right muted" id="cs"></span>';
  app.appendChild(bar);
  const wrap=el('div','panel tree'); app.appendChild(wrap);
  function draw(){
    const q=$('#qs').value.trim().toLowerCase(); let data=rows;
    if(q)data=data.filter(r=>(r.series+' '+r.contract+' '+r.direction).toLowerCase().includes(q));
    wrap.innerHTML=''; $('#cs').textContent=fmt(sum(data,r=>r.tonnes))+' т';
    const byDir=groupBy(data,r=>r.direction);
    [...byDir.keys()].sort((a,b)=>sum(byDir.get(b),r=>r.tonnes)-sum(byDir.get(a),r=>r.tonnes)).forEach(dir=>{
      const drows=byDir.get(dir);
      wrap.appendChild(treeNode('<b>'+esc(dir)+'</b>',tval(sum(drows,r=>r.tonnes)),kids=>{
        // Уровень «договор» убран — серии выводятся сразу под направлением
        // (номер договора уже виден в полной строке серии).
        const byS=groupBy(drows,r=>r.series);
        [...byS.keys()].sort((a,b)=>(''+a).localeCompare(''+b,'ru',{numeric:true})).forEach(s=>{
          const srows=byS.get(s), s0=srows[0];
          const ex=[]; if(s0.contragent)ex.push(esc(s0.contragent)); if(s0.datavyvoza)ex.push('вывоз до '+esc(s0.datavyvoza));
          const sfull=s0.series_full||s0.series_display;
          const sft=(sfull&&sfull!==s0.series_display)?' <span class="muted" style="font-weight:400">· '+esc(sfull)+'</span>':'';
          kids.appendChild(treeNode('<b>'+esc(s0.series_display)+'</b>'+sft+(ex.length?' <span class="tag">— '+ex.join(' · ')+'</span>':''),
            tval(sum(srows,r=>r.tonnes)),k3=>{
            const byB=groupBy(srows,r=>r.base);
            [...byB.keys()].sort(baseSort).forEach(b=>{
              const brows=byB.get(b);
              k3.appendChild(treeNode(esc(b),tval(sum(brows,r=>r.tonnes)),k4=>{
                brows.sort((a,b)=>b.tonnes-a.tonnes).forEach(r=>{
                  k4.appendChild(treeNode(esc(r.name)+' <span class="muted">('+esc(r.sklad)+')</span>',
                    tval(r.tonnes),kk=>{ const d=el('div','docs');
                      (r.docs||[]).forEach(doc=>d.appendChild(el('div','doc','<span class="rn">'+esc(doc.regnum)+'</span><span class="dt">'+esc(doc.dt)+'</span><span>'+fmt(doc.qty)+'</span>')));
                      if(!r.docs||!r.docs.length)d.innerHTML='<div class="doc">строка факта («1С»)</div>'; kk.appendChild(d);
                    },{code:r.code,badge:badge(r.badge)}));
                });
              }));
            });
          },{badge:badge(worstBadgeArr(srows))}));
        });
      }));
    });
  }
  $('#qs').addEventListener('input',draw); draw();
};
function worstBadgeArr(a){ let w=null; for(const r of a)w=worstBadge(w,r.badge); return w; }

/* ================================================================
   3) ГРУППЫ НОМЕНКЛАТУРЫ: итоги по отчётным группам, фильтр по категории
   ================================================================ */
TABS.groups = function(app){
  const rows=DATA.rows;
  const cats=[...new Set(rows.map(r=>r.category))].sort();
  const bar=el('div','toolbar'); bar.innerHTML='<select id="fc"><option value="">категория: все</option>'
    +cats.map(c=>'<option>'+esc(c)+'</option>').join('')+'</select><input type="search" id="qg" placeholder="Поиск группы…">';
  app.appendChild(bar);
  const panel=el('div','panel'); app.appendChild(panel);
  function draw(){
    const fc=$('#fc').value,q=$('#qg').value.trim().toLowerCase();
    let data=rows; if(fc)data=data.filter(r=>r.category===fc); if(q)data=data.filter(r=>r.report_group.toLowerCase().includes(q));
    const byG=groupBy(data,r=>r.report_group);
    const arr=[...byG.entries()].map(([g,rs])=>({g,cat:rs[0].category,t:sum(rs,r=>r.tonnes),
      real:sum(rs.filter(REAL),r=>r.tonnes),pos:new Set(rs.map(r=>r.code)).size,ser:new Set(rs.filter(REAL).map(r=>r.series)).size}))
      .sort((a,b)=>b.t-a.t);
    let h='<table><thead><tr><th>Отчётная группа</th><th>Категория</th><th class="num">Тонн</th><th class="num">С серией</th><th class="num">%</th><th class="num">Позиций</th><th class="num">Серий</th></tr></thead><tbody>';
    arr.forEach(x=>{ h+='<tr><td>'+esc(x.g)+'</td><td class="muted">'+esc(x.cat)+'</td><td class="num">'+fmt(x.t)
      +'</td><td class="num">'+fmt(x.real)+'</td><td class="num">'+(x.t?Math.round(x.real/x.t*100):0)+'</td><td class="num">'+x.pos+'</td><td class="num">'+x.ser+'</td></tr>'; });
    h+='<tr style="font-weight:800"><td>ИТОГО</td><td></td><td class="num">'+fmt(sum(arr,x=>x.t))+'</td><td class="num">'+fmt(sum(arr,x=>x.real))+'</td><td></td><td></td><td></td></tr>';
    h+='</tbody></table>'; panel.innerHTML=h;
  }
  $('#fc').addEventListener('change',draw); $('#qg').addEventListener('input',draw); draw();
};

/* ================================================================
   4) ПРОВЕРКА ДАННЫХ: приход vs расход по позициям; расход<30% — красным
   ================================================================ */
TABS.check = function(app){
  const bar=el('div','toolbar'); bar.innerHTML='<input type="search" id="qc" placeholder="Поиск позиции…">'
    +'<label class="muted"><input type="checkbox" id="onlyw"> только проблемные (расход &lt; 30%)</label>'
    +'<span class="right muted" id="ccnt"></span>';
  app.appendChild(bar);
  const panel=el('div','panel'); app.appendChild(panel);
  function draw(){
    const q=$('#qc').value.trim().toLowerCase(), ow=$('#onlyw').checked;
    let data=DATA.check.slice();
    if(q)data=data.filter(r=>(r.code+' '+r.name).toLowerCase().includes(q));
    if(ow)data=data.filter(r=>r.ratio!=null&&r.ratio<0.30);
    data.sort((a,b)=>b.prihod-a.prihod);
    $('#ccnt').textContent=data.length+' позиций';
    let h='<table><thead><tr><th>Код</th><th>Номенклатура</th><th class="num">Приход</th><th class="num">Расход</th><th class="num">Расход/Приход</th></tr></thead><tbody>';
    data.slice(0,3000).forEach(r=>{ const warn=r.ratio!=null&&r.ratio<0.30;
      h+='<tr><td class="muted" style="font-family:monospace">'+esc(r.code)+'</td><td>'+esc(r.name)+'</td>'
        +'<td class="num">'+fmt(r.prihod)+'</td><td class="num'+(warn?' warn':'')+'">'+fmt(r.rashod)+'</td>'
        +'<td class="num'+(warn?' warn':'')+'">'+(r.ratio!=null?Math.round(r.ratio*100)+'%':'—')+'</td></tr>'; });
    h+='</tbody></table>'; if(data.length>3000)h+='<div class="hint">показаны первые 3000</div>'; panel.innerHTML=h;
  }
  $('#qc').addEventListener('input',draw); $('#onlyw').addEventListener('change',draw); draw();
};

/* ================================================================
   5) НОВЫЕ ОСТАТКИ: загрузка CSV фактических остатков (дата из имени)
   ================================================================ */
TABS.newstock = function(app){
  const p=el('div','panel'); p.innerHTML=
    '<h3>Загрузка новых фактических остатков</h3>'
    +'<p class="hint">Файл «Остатки на складах ДД.ММ.ГГ» (.csv / .xlsx). Дата актуальности = дата из имени файла − 1 день. '
    +'Полный пересчёт выполняется конвейером (stock_report.py) либо во вкладке «Обновление данных».</p>'
    +'<input type="file" id="nf" accept=".csv,.xlsx"><div id="nout" class="hint"></div>';
  app.appendChild(p);
  $('#nf').addEventListener('change',async e=>{
    const f=e.target.files[0]; if(!f)return;
    const m=/(\d{2})\.(\d{2})\.(\d{2})/.exec(f.name);
    const actual=m?new Date(2000+ +m[3],+m[2]-1,+m[1]-1):null;
    let rows=[];
    if(f.name.toLowerCase().endsWith('.csv')){ rows=parseFactCSV(await f.text()); }
    else { $('#nout').innerHTML='XLSX-разбор в браузере: сохраните файл как CSV, либо используйте stock_report.py. Имя распознано.'; }
    const tot=sum(rows,r=>r.qty);
    $('#nout').innerHTML='Файл: <b>'+esc(f.name)+'</b><br>Дата актуальности: <b>'+(actual?actual.toLocaleDateString('ru-RU'):'—')
      +'</b><br>Строк: '+rows.length+' · суммарный «Начальный остаток»: '+fmt(tot)
      +'<br><span class="muted">Для привязки к сериям запустите пересчёт (вкладка «Обновление данных» → блок регистра).</span>';
  });
};
function parseFactCSV(text){
  const lines=text.split(/\r?\n/).filter(x=>x.trim()); if(!lines.length)return [];
  const sep=lines[0].includes(';')?';':(lines[0].includes('\t')?'\t':',');
  const hdr=splitCSV(lines[0],sep); const gi=n=>hdr.findIndex(h=>h.replace(/["']/g,'').trim().toLowerCase()===n);
  const iq=gi('начальный остаток'), ic=gi('номенклатура.код'), inm=gi('номенклатура'), isk=gi('склад');
  const out=[]; for(let i=1;i<lines.length;i++){ const c=splitCSV(lines[i],sep);
    out.push({sklad:(c[isk]||'').trim(),code:(c[ic]||'').trim(),name:(c[inm]||'').trim(),qty:parseFloat((c[iq]||'0').replace(',','.'))||0}); }
  return out;
}
function splitCSV(line,sep){ const out=[];let cur='',q=false; for(let i=0;i<line.length;i++){const ch=line[i];
  if(ch=='"'){ if(q&&line[i+1]=='"'){cur+='"';i++;} else q=!q; } else if(ch===sep&&!q){out.push(cur);cur='';} else cur+=ch; }
  out.push(cur); return out; }

/* ================================================================
   6) ВЫГРУЗКА В 1С: база «5» → склад → чекбоксы позиций → Excel (шаблон перемещения)
   ================================================================ */
TABS.export1c = function(app){
  const rows=DATA.rows;
  const bases=[...new Set(rows.map(r=>r.base))].filter(b=>b.startsWith('5')).sort(baseSort);
  const p=el('div','panel'); p.innerHTML='<h3>Выгрузка в 1С — «Товары»</h3>'
    +'<div class="toolbar"><select id="eb"><option value="">— база «5» —</option>'+bases.map(b=>'<option>'+esc(b)+'</option>').join('')+'</select>'
    +'<select id="es"><option value="">— склад —</option></select>'
    +'<select id="esort"><option value="alpha">сортировка: А→Я (номенклатура)</option><option value="tons">по тоннажу</option></select>'
    +'<select id="emode"><option value="series">с серией</option><option value="noseries">без серии</option><option value="all">все (с серией и без)</option></select>'
    +'<button class="btn" id="egen" style="background:var(--accent);border-color:var(--accent)">⬇ Скачать XLSX</button>'
    +'<span class="right muted" id="esel"></span></div>'
    +'<div class="hint">Формат «Товары» (9 колонок). Заполняются: Код номенклатуры, Номенклатура, Серия, Количество. '
    +'Пустыми остаются: Артикул, Характеристика, Назначение, Код упаковки, Упаковка. '
    +'Режим «с серией» — реальные серии (полная строка); «без серии» — позиции «БЕЗ СЕРИИ» с пустой колонкой Серия '
    +'(экономист заполнит в 1С); «все» — и те, и другие.</div>'
    +'<div id="elist" class="panel" style="max-height:52vh;overflow:auto"></div>';
  app.appendChild(p);
  const eb=$('#eb'),es=$('#es'),elist=$('#elist');
  eb.addEventListener('change',()=>{ const b=eb.value; es.innerHTML='<option value="">— склад —</option>';
    [...new Set(rows.filter(r=>r.base===b).map(r=>r.sklad))].sort().forEach(s=>es.innerHTML+='<option>'+esc(s)+'</option>'); elist.innerHTML=''; });
  es.addEventListener('change',()=>{ drawList(); });
  $('#esort',p).addEventListener('change',()=>{ drawList(); });
  $('#emode',p).addEventListener('change',()=>{ drawList(); });
  // доступна ли позиция для выбора в текущем режиме
  function selectable(r){ const m=$('#emode',p).value;
    if(m==='series') return REAL(r);
    if(m==='noseries') return !REAL(r);
    return true; }
  function drawList(){ const b=eb.value,s=es.value; if(!b||!s){elist.innerHTML='';return;}
    const sortBy=$('#esort',p).value;
    const cmp = sortBy==='tons'
      ? (a,b)=>b.tonnes-a.tonnes
      : (a,b)=>(a.name||'').localeCompare(b.name||'','ru',{numeric:true,sensitivity:'base'})
              || (a.series_display||'').localeCompare(b.series_display||'','ru',{numeric:true});
    const rs=rows.filter(r=>r.base===b&&r.sklad===s).sort(cmp);
    let h='<div class="chk"><input type="checkbox" id="eall"><b>Выбрать все доступные</b></div>';
    rs.forEach((r,i)=>{ const ok=selectable(r);
      h+='<div class="chk"><input type="checkbox" class="ec" data-i="'+i+'"'+(ok?'':' disabled')+'>'
        +'<span class="code">'+esc(r.code)+'</span><span style="flex:1">'+esc(r.name)+' — <b>'+esc(r.series_full||r.series_display)+'</b> '+badge(r.badge)+'</span>'
        +'<span class="val">'+fmt(r.tonnes)+' т</span></div>'; });
    elist.innerHTML=h; elist._rs=rs;
    $('#eall').addEventListener('change',e=>elist.querySelectorAll('.ec:not([disabled])').forEach(c=>c.checked=e.target.checked));
    elist.addEventListener('change',updSel); updSel();
  }
  function updSel(){ const n=elist.querySelectorAll('.ec:checked').length; $('#esel').textContent=n+' позиций выбрано'; }
  $('#egen').addEventListener('click',()=>{
    if(!elist._rs){alert('Выберите базу и склад');return;}
    const chosen=[...elist.querySelectorAll('.ec:checked')].map(c=>elist._rs[+c.dataset.i]);
    if(!chosen.length){alert('Не выбрано ни одной позиции');return;}
    // Формат «Товары» (9 колонок). Для позиций без реальной серии колонка Серия — пустая.
    const aoa=[['Артикул','Код номенклатуры','Номенклатура','Характеристика','Назначение','Серия','Код упаковки','Упаковка','Количество']];
    chosen.forEach(r=>aoa.push(['', r.code, r.name, '', '', (REAL(r)?(r.series_full||r.series_display):''), '', '', r.qty]));
    downloadXLSX(aoa,'Товары_'+es.value.replace(/[^\wа-яА-Я0-9]+/g,'_')+'.xlsx');
  });
};

/* ---------- Генерация .xlsx на чистом JS (zip stored) ----------
   Структура как у шаблона 1С «Товары.xlsx»: sharedStrings (t="s") + styles.xml,
   лист «Лист_1». 1С не читает inlineStr — поэтому строки только через sharedStrings. */
function _xmlClean(s){ return String(s==null?'':s).replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g,''); }
function downloadXLSX(aoa, fname){
  const colL=i=>{let s='';i++;while(i){i--;s=String.fromCharCode(65+i%26)+s;i=Math.floor(i/26);}return s;};
  const sst=[]; const smap=new Map(); let strCount=0;
  const si=s=>{ if(smap.has(s))return smap.get(s); const i=sst.length; smap.set(s,i); sst.push(s); return i; };
  let rowsXml='';
  aoa.forEach((row,r)=>{ let cells='';
    row.forEach((v,c)=>{ const ref=colL(c)+(r+1);
      if(typeof v==='number'&&isFinite(v)){ cells+='<c r="'+ref+'"><v>'+v+'</v></c>'; }
      else { const str=_xmlClean(v); if(str==='')return; strCount++; cells+='<c r="'+ref+'" t="s"><v>'+si(str)+'</v></c>'; } });
    rowsXml+='<row r="'+(r+1)+'">'+cells+'</row>'; });
  const ncol=aoa.reduce((m,r)=>Math.max(m,r.length),1), nrow=aoa.length;
  const dim='A1:'+colL(ncol-1)+nrow;
  const X='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';
  const sheet=X+'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="'+dim+'"/><sheetData>'+rowsXml+'</sheetData></worksheet>';
  const shared=X+'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="'+strCount+'" uniqueCount="'+sst.length+'">'
    +sst.map(s=>'<si><t xml:space="preserve">'+esc(s)+'</t></si>').join('')+'</sst>';
  const styles=X+'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>';
  const ct=X+'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>';
  const rels=X+'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>';
  const wb=X+'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Лист_1" sheetId="1" r:id="rId1"/></sheets></workbook>';
  const wbr=X+'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>';
  const files=[['[Content_Types].xml',ct],['_rels/.rels',rels],['xl/workbook.xml',wb],['xl/_rels/workbook.xml.rels',wbr],
    ['xl/worksheets/sheet1.xml',sheet],['xl/styles.xml',styles],['xl/sharedStrings.xml',shared]];
  const blob=zipStored(files); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=fname; a.click(); URL.revokeObjectURL(a.href);
}
const _CRC=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xEDB88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
function _crc32(buf){let c=0xFFFFFFFF;for(let i=0;i<buf.length;i++)c=_CRC[(c^buf[i])&0xFF]^(c>>>8);return (c^0xFFFFFFFF)>>>0;}
function zipStored(files){
  const enc=new TextEncoder(); const parts=[]; const central=[]; let offset=0;
  const u16=n=>[n&255,(n>>8)&255], u32=n=>[n&255,(n>>8)&255,(n>>16)&255,(n>>24)&255];
  files.forEach(([name,content])=>{
    const nameB=enc.encode(name), data=enc.encode(content), crc=_crc32(data);
    const local=[].concat([0x50,0x4b,0x03,0x04],u16(20),u16(0),u16(0),u16(0),u16(0),u32(crc),u32(data.length),u32(data.length),u16(nameB.length),u16(0));
    parts.push(new Uint8Array(local),nameB,data);
    central.push([].concat([0x50,0x4b,0x01,0x02],u16(20),u16(20),u16(0),u16(0),u16(0),u16(0),u32(crc),u32(data.length),u32(data.length),u16(nameB.length),u16(0),u16(0),u16(0),u16(0),u32(0),u32(offset)));
    central.push(nameB);
    offset+=local.length+nameB.length+data.length;
  });
  const centralParts=[]; let cs=0;
  central.forEach(c=>{ const a=Array.isArray(c)?new Uint8Array(c):c; centralParts.push(a); cs+=a.length; });
  const end=[].concat([0x50,0x4b,0x05,0x06],u16(0),u16(0),u16(files.length),u16(files.length),u32(cs),u32(offset),u16(0));
  return new Blob([...parts,...centralParts,new Uint8Array(end)],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
}

/* ---------- (Резерв) Генерация .xls (BIFF8 + OLE2/CFB) — оставлено на случай возврата к .xls ----------
   Проверено чтением через xlrd. */
function downloadXLS(aoa, fname){
  const wb=biff8Workbook(aoa);
  const blob=new Blob([cfbContainer(wb)],{type:'application/vnd.ms-excel'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=fname; a.click(); URL.revokeObjectURL(a.href);
}
function _u16(a,n){a.push(n&255,(n>>8)&255);}
function _u32(a,n){a.push(n&255,(n>>8)&255,(n>>16)&255,(n>>24)&255);}
function _f64(a,v){const dv=new DataView(new ArrayBuffer(8));dv.setFloat64(0,v,true);for(let i=0;i<8;i++)a.push(dv.getUint8(i));}
function _utf16le(a,s){for(let i=0;i<s.length;i++){const c=s.charCodeAt(i);a.push(c&255,(c>>8)&255);}}
function _su(a,s){_u16(a,s.length);a.push(0x01);_utf16le(a,s);}      // XLUnicodeString (16-bit cch)
function _ssu(a,s){a.push(s.length&255,0x01);_utf16le(a,s);}        // Short (8-bit cch)
function _rec(a,code,body){_u16(a,code);_u16(a,body.length);for(const b of body)a.push(b);}
function _bof(dt){const b=[];_u16(b,0x0600);_u16(b,dt);_u16(b,0x0DBB);_u16(b,0x07CC);_u32(b,0x41);_u32(b,0x06);return b;}
function _xf(style){const b=[];_u16(b,0);_u16(b,0);_u16(b,style?0xFFF5:0x0001);b.push(0,0,0,0);_u32(b,0);_u32(b,0);_u16(b,0);return b;}
function biff8Workbook(aoa){
  const SHEET='Перемещение';
  const g=[];
  _rec(g,0x0809,_bof(0x0005));                       // BOF globals
  const font=[];_u16(font,200);_u16(font,0);_u16(font,0x7FFF);_u16(font,400);_u16(font,0);font.push(0,0,0,0);_ssu(font,'Arial');
  _rec(g,0x0031,font);                               // FONT 0
  for(let i=0;i<15;i++)_rec(g,0x00E0,_xf(true));     // 15 стилевых XF
  _rec(g,0x00E0,_xf(false));                         // 1 ячейковый XF (индекс 15)
  // BOUNDSHEET: смещение BOF листа = итоговая длина globals (учитываем саму запись + EOF)
  const bsBody=[];const tmp=[];_ssu(tmp,SHEET);
  const bsLen=4+6+tmp.length;                        // заголовок(4)+lbPlyPos(4)+grbit(2)+имя
  const globalsLen=g.length+bsLen+4;
  _u32(bsBody,globalsLen);bsBody.push(0,0);_ssu(bsBody,SHEET);
  _rec(g,0x0085,bsBody);                             // BOUNDSHEET
  _rec(g,0x000A,[]);                                 // EOF globals
  if(g.length!==globalsLen)throw new Error('globals offset mismatch '+g.length+'/'+globalsLen);
  const s=[];
  _rec(s,0x0809,_bof(0x0010));                       // BOF worksheet
  const nrows=aoa.length,ncols=aoa.reduce((m,r)=>Math.max(m,r.length),1);
  const dim=[];_u32(dim,0);_u32(dim,nrows);_u16(dim,0);_u16(dim,ncols);_u16(dim,0);
  _rec(s,0x0200,dim);                                // DIMENSIONS
  aoa.forEach((row,r)=>row.forEach((v,c)=>{
    if(typeof v==='number'&&isFinite(v)){const b=[];_u16(b,r);_u16(b,c);_u16(b,15);_f64(b,v);_rec(s,0x0203,b);}
    else{const b=[];_u16(b,r);_u16(b,c);_u16(b,15);_su(b,v==null?'':String(v));_rec(s,0x0204,b);}
  }));
  _rec(s,0x000A,[]);                                 // EOF worksheet
  return new Uint8Array(g.concat(s));
}
function cfbContainer(workbook){
  const ENDOFCHAIN=0xFFFFFFFE,FREESECT=0xFFFFFFFF,FATSECT=0xFFFFFFFD;
  let wb=Array.from(workbook);
  while(wb.length<4096)wb.push(0);
  while(wb.length%512)wb.push(0);
  const W=wb.length/512, dirSect=W, fatSect=W+1;
  const fat=new Array(128).fill(FREESECT);
  for(let i=0;i<W-1;i++)fat[i]=i+1;
  fat[W-1]=ENDOFCHAIN;fat[dirSect]=ENDOFCHAIN;fat[fatSect]=FATSECT;
  const fatB=[];fat.forEach(x=>_u32(fatB,x>>>0));
  function de(name,etype,color,left,right,child,start,size){
    const b=[];_utf16le(b,name);while(b.length<64)b.push(0);
    _u16(b,name.length*2+2);b.push(etype,color);_u32(b,left>>>0);_u32(b,right>>>0);_u32(b,child>>>0);
    for(let i=0;i<16;i++)b.push(0);_u32(b,0);_u32(b,0);_u32(b,0);_u32(b,0);_u32(b,0);
    _u32(b,start>>>0);_u32(b,size>>>0);_u32(b,0);
    return b; // 128 байт
  }
  let dir=de('Root Entry',5,1,-1,-1,1,ENDOFCHAIN,0).concat(de('Workbook',2,1,-1,-1,-1,0,wb.length));
  while(dir.length<512)dir.push(0);
  const h=[];
  [0xD0,0xCF,0x11,0xE0,0xA1,0xB1,0x1A,0xE1].forEach(x=>h.push(x));
  for(let i=0;i<16;i++)h.push(0);
  _u16(h,0x003E);_u16(h,0x0003);_u16(h,0xFFFE);_u16(h,9);_u16(h,6);
  for(let i=0;i<6;i++)h.push(0);
  _u32(h,0);_u32(h,1);_u32(h,dirSect);_u32(h,0);_u32(h,4096);
  _u32(h,ENDOFCHAIN);_u32(h,0);_u32(h,ENDOFCHAIN);_u32(h,0);
  _u32(h,fatSect);for(let i=0;i<108;i++)_u32(h,FREESECT);
  while(fatB.length<512)fatB.push(0);
  return new Uint8Array(h.concat(wb).concat(dir).concat(fatB));
}

/* ================================================================
   7) ОБНОВЛЕНИЕ ДАННЫХ: снимок (скачать/загрузить), автодетект справочников
   ================================================================ */
TABS.update = function(app){
  const served = location.protocol.indexOf('http')===0;   // на сервере, а не file://
  const api=(m,u,b)=>fetch(u,{method:m,headers:b?{'Content-Type':'application/json'}:undefined,body:b?JSON.stringify(b):undefined}).then(r=>r.json());
  const BP='background:var(--accent);color:#fff;border:none;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer;font-size:13px';
  const BS='background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px';
  const INP='background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--text);font-size:13px;width:100%';

  // --- шапка + снимок ---
  const intro=el('div','panel');
  intro.innerHTML='<h3>Обновление данных</h3>'
    +'<p class="hint">Два источника: <b>SQL «Extractor»</b> (движения + справочники напрямую из базы) и '
    +'<b>Excel-остатки</b> локально. Кнопка «Пересобрать» тянет включённые SQL-источники и запускает расчёт на сервере.</p>'
    +'<button id="u_snap" style="'+BP+'">⬇ Скачать снимок (JSON)</button> '
    +'<span class="muted">снимок = данные + дата + правки (для архива)</span>';
  app.appendChild(intro);
  $('#u_snap').addEventListener('click',()=>{ const blob=new Blob([JSON.stringify(DATA)],{type:'application/json'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='snapshot_'+(ACTUAL||'').replace(/\./g,'-')+'.json'; a.click(); URL.revokeObjectURL(a.href); });
  if(!served){
    const w=el('div','panel'); w.innerHTML='<p class="hint">Загрузка данных и пересборка работают только на сервере '
      +'(откройте дашборд по адресу http://…:8090/). В офлайн-версии доступен только снимок.</p>'; app.appendChild(w); return; }

  // --- Блок 1: SQL «Extractor» ---
  const s1=el('div','panel');
  s1.innerHTML='<h3>1 · Источник «из экстрактора» — напрямую из SQL</h3>'
    +'<p class="hint">Сервис забирает таблицы из базы «Extractor», куда 1С складывает данные. Движения и справочники берутся из SQL; '
    +'фактические остатки грузятся Excel-файлом (блок 2). Пароль — только в окружении службы (METOPTORG_SQL_PASSWORD в /etc/metoptorg-ostatki.env).</p>'
    +'<div id="u_na" class="hint" style="color:var(--red);display:none">Модуль SQL недоступен (нет python-tds на сервере).</div>'
    +'<div id="u_sqlbox">'
    +'<div class="toolbar">'
    +'<div style="flex:1;min-width:120px"><label class="muted">сервер</label><input id="u_host" type="text" style="'+INP+'"></div>'
    +'<div style="width:90px"><label class="muted">порт</label><input id="u_port" type="number" style="'+INP+'"></div>'
    +'<div style="flex:1;min-width:120px"><label class="muted">база</label><input id="u_db" type="text" style="'+INP+'"></div>'
    +'<div style="flex:1;min-width:120px"><label class="muted">пользователь</label><input id="u_user" type="text" style="'+INP+'"></div>'
    +'<button id="u_test" style="'+BS+';align-self:end">Проверить подключение</button>'
    +'<button id="u_tabs" style="'+BS+';align-self:end">Показать таблицы</button>'
    +'</div>'
    +'<label style="display:inline-flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="u_en"> <span>брать данные из SQL при пересборке</span></label>'
    +'<div id="u_drv" class="hint"></div><div id="u_msg" class="hint"></div>'
    +'<table style="margin-top:8px"><thead><tr><th>вкл</th><th>что это</th><th>таблица в базе</th><th></th></tr></thead><tbody id="u_src"></tbody></table>'
    +'<datalist id="u_tl"></datalist>'
    +'<div style="margin-top:12px"><button id="u_save" style="'+BP+'">Сохранить источники</button></div>'
    +'</div>';
  app.appendChild(s1);

  // --- Блок 2: Excel локально ---
  const s2=el('div','panel');
  s2.innerHTML='<h3>2 · Фактические остатки — Excel локально</h3>'
    +'<p class="hint">Файл «Остатки на складах ДД.ММ.ГГ.xlsx» (дата отчёта — из имени, минус 1 день). '
    +'Можно догрузить и любую выгрузку 1С (обороты/справочники) в csv, если не берёте её из SQL.</p>'
    +'<input type="file" id="u_file" accept=".xlsx,.xls,.csv" multiple style="display:none">'
    +'<button id="u_pick" style="'+BS+'">Выбрать файл(ы)…</button> <span id="u_up" class="hint"></span>';
  app.appendChild(s2);

  // --- Блок 3: пересборка ---
  const s3=el('div','panel');
  s3.innerHTML='<h3>3 · Пересборка</h3>'
    +'<p class="hint">Тянет включённые SQL-источники и запускает расчёт (stock_report → make_dash → make_excel). Фоново, с живым логом.</p>'
    +'<button id="u_reb" style="'+BP+'">▶ Пересобрать сейчас</button> <span class="status" id="u_stat" style="font-weight:700;margin-left:10px">—</span>'
    +'<div id="u_log" style="background:#0b1120;color:#cfe;border-radius:10px;padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:12px;height:280px;overflow:auto;white-space:pre-wrap;margin-top:10px"></div>';
  app.appendChild(s3);

  // --- логика ---
  let SRC=[];
  const cfgForm=()=>{ const sources={};
    document.querySelectorAll('#u_src tr').forEach(tr=>{ sources[tr.dataset.k]={enabled:tr.querySelector('.se').checked, table:tr.querySelector('.tb').value.trim()}; });
    return {enabled:$('#u_en').checked, host:$('#u_host').value.trim(), port:+$('#u_port').value||1433,
      database:$('#u_db').value.trim(), user:$('#u_user').value.trim(), timeout:60, sources}; };
  async function loadSql(){ const d=await api('GET','/api/sql/config');
    if(!d.available){ $('#u_na').style.display='block'; $('#u_sqlbox').style.display='none'; return; }
    const c=d.config; SRC=d.sources;
    $('#u_host').value=c.host||''; $('#u_port').value=c.port||1433; $('#u_db').value=c.database||''; $('#u_user').value=c.user||''; $('#u_en').checked=!!c.enabled;
    $('#u_drv').innerHTML='драйвер: '+esc(d.driver)+' · пароль '+(d.password_set?'<b style="color:var(--green)">задан</b>':'<b style="color:var(--red)">НЕ задан</b> (добавьте METOPTORG_SQL_PASSWORD в /etc/metoptorg-ostatki.env)');
    $('#u_src').innerHTML=SRC.map(s=>{ const cs=c.sources[s.key]||{};
      return '<tr data-k="'+s.key+'"><td><input type="checkbox" class="se" '+(cs.enabled?'checked':'')+'></td><td>'+esc(s.label)+'</td>'
        +'<td><input type="text" class="tb" list="u_tl" value="'+esc(cs.table||'')+'" placeholder="dbo.Имя таблицы" style="'+INP+'"></td>'
        +'<td><button class="bp" data-k="'+s.key+'" style="'+BS+'">проверить</button></td></tr>'; }).join('');
    document.querySelectorAll('#u_src .bp').forEach(b=>b.onclick=()=>probe(b.dataset.k));
  }
  $('#u_test').onclick=async()=>{ $('#u_msg').textContent='проверяю…'; const d=await api('POST','/api/sql/test',cfgForm());
    $('#u_msg').innerHTML='<b style="color:var(--'+(d.ok?'green':'red')+')">'+(d.ok?'✓ ':'✗ ')+esc(d.message)+'</b>'; };
  $('#u_tabs').onclick=async()=>{ $('#u_msg').textContent='читаю таблицы…'; const d=await api('POST','/api/sql/tables',cfgForm());
    if(!d.ok){ $('#u_msg').innerHTML='<b style="color:var(--red)">'+esc(d.error)+'</b>'; return; }
    $('#u_tl').innerHTML=d.tables.map(t=>'<option value="'+esc(t.schema+'.'+t.name)+'">'+t.rows+' строк</option>').join('');
    $('#u_msg').innerHTML='<b style="color:var(--green)">таблиц: '+d.tables.length+' (подсказки в полях)</b>'; };
  async function probe(k){ const tr=document.querySelector('#u_src tr[data-k="'+k+'"]'); const table=tr.querySelector('.tb').value.trim();
    if(!table){alert('укажите таблицу');return;} const b=tr.querySelector('.bp'); b.textContent='…';
    const d=await api('POST','/api/sql/probe',{table,cfg:cfgForm()}); b.textContent='проверить';
    if(!d.ok){alert('Ошибка: '+d.error);return;}
    alert('Таблица '+table+'\nСтрок: '+d.probe.rows+'\nКолонки: '+d.probe.columns.join(', ')); }
  $('#u_save').onclick=async()=>{ const d=await api('POST','/api/sql/config',cfgForm());
    $('#u_msg').innerHTML=d.ok?'<b style="color:var(--green)">✓ источники сохранены</b>':'<b style="color:var(--red)">'+esc(d.error)+'</b>'; };

  $('#u_pick').onclick=()=>$('#u_file').click();
  $('#u_file').onchange=async e=>{ for(const f of e.target.files){ $('#u_up').textContent='загружаю '+f.name+'…';
    const r=await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name),'Content-Type':'application/octet-stream'},body:f});
    let res; try{res=await r.json();}catch{res={ok:false,error:'нет ответа'};}
    $('#u_up').innerHTML=res.ok?'<b style="color:var(--green)">✓ '+esc(res.name)+' — '+(res.bytes/1048576).toFixed(1)+' МБ</b>':'<b style="color:var(--red)">✗ '+esc(res.error||'ошибка')+'</b>'; }
    e.target.value=''; };

  $('#u_reb').onclick=async()=>{ const d=await api('POST','/api/rebuild'); if(!d.ok){alert(d.error||'занято');return;} poll(); };
  async function poll(){ if(!document.getElementById('u_log'))return;   // ушли с вкладки
    const s=await api('GET','/api/state');
    if(!document.getElementById('u_log'))return;
    $('#u_stat').innerHTML=s.building?'<span style="color:var(--yellow)">● идёт пересборка…</span>'
      :(s.ok===true?'<span style="color:var(--green)">✓ готово ('+esc(s.finished)+')</span>'
       :s.ok===false?'<span style="color:var(--red)">✗ ошибка ('+esc(s.finished)+')</span>':'—');
    const lg=$('#u_log'); lg.textContent=(s.tail||[]).join('\n'); lg.scrollTop=lg.scrollHeight;
    if(s.building) setTimeout(poll,1500);
  }
  loadSql(); poll();
};
