/* ================================================================
   Вкладки дашборда реализации. DATA/render/утилиты — в шаблоне.
   ================================================================ */

/* ---------- общий раскрывающийся узел дерева ---------- */
/* Полный текст подписи в подсказке: даже в компактном режиме, где строка
   обрезается многоточием, наведение показывает её целиком. */
const plain = html => String(html).replace(/<[^>]*>/g,'').replace(/\s+/g,' ').trim();

/* Деньги — ровно два знака и разряды: в отличие от тонн (до 3 знаков без
   округления) рубли всегда с копейками, иначе сумма платежа выглядит округлённой. */
const money = n => (n==null||isNaN(n)) ? ''
  : Number(n).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
/* ISO-дата -> привычная 05.12.2025 (в подсказках, не в таблицах) */
const ruDate = d => (d && d.length>=10)
  ? d.slice(8,10)+'.'+d.slice(5,7)+'.'+d.slice(0,4) : (d||'');

/* ---------- ширина колонки подписи в режиме «подписи целиком» ----------
   Строки растут в ШИРИНУ, но числовые колонки обязаны стоять на одной
   вертикали. Поэтому колонке подписи задаём одну ширину для всех строк —
   по самой длинной. Меряем в два прохода: сперва отпускаем ширину по
   содержимому, потом фиксируем максимум в пикселях. */
let _fitId = 0;
function fitLabels(){
  if(!document.documentElement.classList.contains('wide')) return;
  /* именно таймер, а не requestAnimationFrame: в неактивной вкладке кадры не
     приходят, и однажды взведённый флаг «уже запланировано» залипал навсегда */
  clearTimeout(_fitId);
  _fitId = setTimeout(()=>{
    document.querySelectorAll('.tree').forEach(tree=>{
      const labels = tree.querySelectorAll('.row>.hd, .dl>.hd');
      if(!labels.length) return;
      tree.style.setProperty('--lblw','max-content');   // проход 1: по содержимому
      let w = 0;
      labels.forEach(l=>{ const x = l.getBoundingClientRect().width; if(x>w) w = x; });
      // проход 2: фиксируем максимум; потолок — чтобы одна аномальная подпись
      // не увела таблицу на километр вправо
      tree.style.setProperty('--lblw', Math.min(1500, Math.max(260, Math.ceil(w)+6))+'px');
    });
  }, 30);
}
window.fitLabels = fitLabels;

function treeNode(label, valHtml, buildChildren, opts={}){
  const node = el('div','node');
  const row = el('div','row'+(opts.open?' open':''));
  const wide = opts.wide ? ' wide' : '';
  /* код, подпись и бейдж живут в ОДНОЙ колонке: иначе значки разной ширины
     сдвигают блок чисел, и колонки перестают совпадать между строками */
  row.innerHTML = '<span class="tw">▶</span><span class="hd">'
    +(opts.code?'<span class="code">'+esc(opts.code)+'</span>':'')
    +'<span class="lbl" title="'+esc(plain(label))+'">'+label+'</span>'+(opts.badge||'')
    +'</span><span class="val'+wide+'">'+valHtml+'</span>';
  const kids = el('div','children'+(opts.open?' open':''));
  /* ⚠️ ЕДИНИЦА, В КОТОРОЙ УЗЕЛ РОДИЛСЯ, ЖИВЁТ С НИМ ДО САМОГО НИЗА.
     Дети строятся ЛЕНИВО, по клику, — а к тому времени MEASURE_FORCED уже
     вернулся к режиму вкладки. На «Весь обороте» из-за этого разрез по единице
     держался ровно один уровень: под «штуками» шли серии в штуках, но склады,
     номенклатура и документы под ними снова показывали ВСЕ единицы разом.
     Запоминаем режим на момент создания узла и восстанавливаем его вокруг
     построения детей — на любой глубине. Узел, которому нужна ДРУГАЯ единица
     (уровень единицы в unitNodes), выставляет её внутри своего колбэка, и она
     побеждает, потому что применяется позже. */
  const bornMeasure = MEASURE_FORCED, bornUnitLvl = UNIT_LEVEL;
  const build = k=>{
    const p = MEASURE_FORCED, pu = UNIT_LEVEL;
    MEASURE_FORCED = bornMeasure; UNIT_LEVEL = bornUnitLvl;
    try{ buildChildren(k); } finally{ MEASURE_FORCED = p; UNIT_LEVEL = pu; }
  };
  let built=opts.open;
  if(opts.open) build(kids);
  row.addEventListener('click',ev=>{ if(ev.target.closest('a'))return;
    const open=kids.classList.toggle('open'); row.classList.toggle('open',open);
    if(open&&!built){ build(kids); built=true; }
    fitLabels();                     // раскрыли уровень — подписи могли стать длиннее
  });
  node.appendChild(row); node.appendChild(kids); return node;
}
function tval(t){ return fmt(t)+' <span class="unit">т</span>'; }

/* ---------- пояснение «как читать» ----------
   Инструкцию читают один раз, а место она занимала всегда: на Ганта это было
   шесть строк сплошного текста над диаграммой. Сворачиваем — содержимое
   остаётся целиком, по клику. Предупреждения (.note warn/bad) не сворачиваем:
   это находка по данным, а не справка. */
function howto(title, html, cls){
  const d = el('details','note'+(cls?' '+cls:''));
  d.innerHTML = '<summary>'+esc(title)+'</summary><div class="nb">'+html+'</div>';
  return d;
}

/* ---------- ВСЕ ПОТОКИ НА КАЖДОМ УРОВНЕ ДЕРЕВА ----------
   Раньше «куплено» и «продано» стояли только на уровне проекта, а перемещения и
   резка были видны отдельным блоком. Теперь любая строка дерева — направление,
   бизнес-план, серия, склад, номенклатура и даже отдельный документ — заполняет
   ОДНИ И ТЕ ЖЕ колонки: купили · уехало · приехало · в производство · из производства ·
   ПРОДАЛИ · возврат · недостачи · на затраты. Источник один — построчные
   движения регистра (DATA.moves), поэтому суммы складываются снизу вверх и
   сходятся с 1С на каждом уровне. */
const SEP = '\u0001';
/* ---------- РАЗРЕЗ ПО ЕДИНИЦЕ ИЗМЕРЕНИЯ ----------
   Невесовая номенклатура давала 0 т и была невидима: 793 630 штук, 3 034 887
   литров и 312 737 метров лежали с нулями, а 17 бизнес-планов не имели ни одной
   тоннажной строки вообще. Теперь единица — такой же разрез, как группа учёта:
   логика расчёта та же (каскад серий и лоты работают со строками, единица на
   них не влияет), но показываются величины выбранной единицы и только те планы,
   где она есть.

   ⚠️ Разрезы между собой НЕ складываются: строка попадает в «тонны», если у неё
   есть тоннаж, и в разрез своей единицы, если та не весовая. Обычно это одно и
   то же, но «Труба НКТ 60 мм б/у (м)», у которой 1С заполнила и единицу для
   отчётов, и коэффициент, честно видна и как 32 м, и как 0,218 т.
   Классы считает Python (config.MEASURE_GROUPS) и кладёт в строку движения (mc)
   и в позицию (mc) — оболочка их только читает, чтобы правило жило в одном месте. */
/* Вкладка «Серии (насквозь)» показывает ВСЁ сразу, как 1С: количества в родных
   единицах, без разделения и без пересчёта. Это не выбор пользователя, а режим
   вкладки, поэтому он не попадает в список разрезов и не пишется в localStorage. */
const MEASURE_ALL = 'все';
let MEASURE_FORCED = null;              /* вкладка фиксирует режим на своё время */
/* Единица, выбранная УРОВНЕМ ДЕРЕВА на «Весь обороте» (unitNodes). Пока она
   стоит, всё ниже — одна эта единица, и сводку «также: … в других единицах»
   показывать незачем: другие единицы лежат на соседних узлах того же проекта, а
   не спрятаны. У строки самого проекта флаг пуст — там сводка нужна. */
let UNIT_LEVEL = null;
const curMeasure = () => MEASURE_FORCED || MEASURE;
const MMAIN = 'т';
/* ---------- ВЕС ВСЕГДА В ТОННАХ ----------
   В 1С часть номенклатуры записана килограммами (медь, стружка, обмотка) — в
   расчёте они сведены в тонны ещё в `qty_to_tonnes` (кг = 0,001 т). Отдельного
   разреза «килограммы» в отчёте НЕТ: два способа показать одну величину только
   плодят путаницу, а сверка с 1С идёт в тоннах. Заглушки ниже оставлены, чтобы
   места показа веса читались одинаково и не зависели от того, что выбрано. */
const wScale = () => 1;
const wLabel = () => MMAIN;
const tShow  = t => (t || 0);
const measBase = m => m;
const MEASURES = (META.measures && META.measures.length) ? META.measures
                 : [{key:MMAIN, title:'тонны', rows:0, qty:0, bps:0}];
const MKEYS = MEASURES.map(m=>m.key);
const MEAS_KEY = 'metoptorg.measure';
let MEASURE = (()=>{ try{ const v = localStorage.getItem(MEAS_KEY);
                          return MKEYS.indexOf(v)>=0 ? v : MMAIN; }
                     catch(e){ return MMAIN; } })();
const mTitle = k => (k===MEASURE_ALL ? 'все единицы'
                     : ((MEASURES.find(m=>m.key===k)||{}).title || k));
/* подпись единицы рядом с числом: в режиме «насквозь» единицы разные, поэтому
   пишем «ед.» — складывать их в одну колонку это и есть поведение 1С */
const unitLabel = () => curMeasure()===MEASURE_ALL ? 'ед.' : curMeasure();
function setMeasure(v){
  if(MKEYS.indexOf(v) < 0) return;
  MEASURE = v;
  try{ localStorage.setItem(MEAS_KEY, v); }catch(e){}
  /* кэш держится по каждой единице отдельно, чистить его не нужно */
  /* _UAGG не сбрасываем: он считает ВСЕ единицы сразу и от выбора не зависит */
}
/* строка движения: видна ли в разрезе и каким числом. Для веса отбираем по
   наличию тоннажа, а не по классу единицы: килограммы — тоже тонны. */
const mIn  = r => { const M0=measBase(curMeasure());
  return M0===MEASURE_ALL ? true
       : M0===MMAIN ? Math.abs(r[MC.tonnes])>1e-9 : mst(r,'mc')===M0; };
const mVal = r => { const M0=measBase(curMeasure());
  /* ⚠️ Не r[MC.qty]: это количество КАК В ДОКУМЕНТЕ (метры, граммы). Величина
     разреза лежит в mq — там метры уже переведены в километры. */
  return M0===MMAIN ? tShow(r[MC.tonnes]) : r[MC.mq]; };
/* позиция продажи — то же правило */
const iIn  = r => { const M0=measBase(curMeasure());
  return M0===MEASURE_ALL ? true
       : M0===MMAIN ? Math.abs(r.tonnes)>1e-9 : r.mc===M0; };
const iVal = r => { const M0=measBase(curMeasure());
  return M0===MMAIN ? tShow(r.tonnes) : (r.mq||0); };
/* Смена единицы меняет не только цифры в дереве, но и карточки, и списки
   фильтров, поэтому вкладка перерисовывается целиком, а не только таблица. */
function rerenderTab(){
  try{
    const b = document.querySelector('nav.tabs button.active');
    if(b && typeof render === 'function'){ render(b.dataset.t); return; }
  }catch(e){}
}
/** Выпадающий список разрезов — строится из снимка, а не зашит. */
function measureSelect(id){
  return '<select id="'+id+'" class="msel" title="В какой единице смотрим отчёт. '
    + 'Показываются только бизнес-планы, где эта единица есть; логика расчёта '
    + 'одна и та же, меняется только величина. Суммы разных единиц между собой '
    + 'не складываются. В скобках — сколько бизнес-планов В ЭТОЙ ЕДИНИЦЕ ПРОДАВАЛИ: '
    + 'ровно столько строк и будет в дереве.">'
    + MEASURES.map(m=>'<option value="'+esc(m.key)+'"'
        +(m.key===MEASURE?' selected':'')+'>'+esc(m.title)
        +' ('+cnt(measBps(m.key))+' БП)</option>').join('')+'</select>';
}

/* Кэш агрегатов ПО КАЖДОЙ единице: на вкладке «Серии (продано)» разные проекты
   смотрят в разных единицах, и пересчитывать 300 тыс. строк на каждое раскрытие
   нельзя. Держим не больше четырёх — больше в работе и не бывает. */
const _AGG_CACHE = new Map();
let _AGG = null, _AGG_M = null;
const NEG_IN   = META.negative_in || {};
/* ПРИХОД ЧЕРЕЗ РАЗБОРКУ: выпуск переработки, у которого в том же документе нет
   тоннажного сырья той же серии, — это появление тонн из штук (разобрали
   ёмкость → получили лом), а не внутреннее перекладывание. Такой выпуск идёт
   и в свою колонку «из производства», и в «купили».
   ⚠️ В ОСТАТКЕ он уже посчитан как «переработка_вернули», поэтому его сумма
   копится отдельным ключом _born, и balanceOf вычитает её ровно один раз —
   иначе остаток вырос бы на все 1 805,610 т. */
const BIRTH_IN = META.birth_in || {};
function newAgg(){
  return {S:new Map(),      // проект
          V:new Map(),      // проект + вариант серии (точная строка 1С)
          W:new Map(),      // + склад, на котором прошло движение
          C:new Map(),      // + код номенклатуры
          ROWS:new Map(),   // + код -> сами строки движений (документы листа)
          W_OF:new Map(),   // (проект+вариант) -> Map(склад -> суммы потоков)
          CODES:new Map(),  // (проект+вариант+склад) -> Set(код номенклатуры)
          UNITS:new Map(),  // (проект+вариант) -> Set(единица движений)
          VARS:new Map()};  // проект -> Set(вариант серии по ДВИЖЕНИЯМ)
}
const _aggAdd = (m, k, fl, t) => { let o = m.get(k); if(!o){ o = {}; m.set(k, o); }
                                   o[fl] = (o[fl] || 0) + t; };
/** Одна строка движения в агрегат: t — величина в единице ЭТОГО агрегата. */
/* ⚠️ РАЗБИЕНИЕ, А НЕ ДУБЛЬ: строка «Производства без заказа» с видом работ
   «сортировка» считается в СВОИ колонки и одновременно исключается из «в производство /
   из производства». Сумма двух пар равна прежнему итогу переработки, поэтому ОСТАТОК и
   баланс не меняются (обе пары стоят в BAL_IN / BAL_OUT). Делаем это в
   ОБОЛОЧКЕ: расчёт, Excel и выгрузка в 1С работают с прежними потоками, и ни
   одна опорная цифра не двигается. */
const SORT_FLOW = {'переработка_забрали':'сортировка_забрали',
                   'переработка_вернули':'сортировка_вернули'};
function aggRow(B, r, t, weight){
  let fl = mfl(r);
  if(SORT_FLOW[fl] && mst(r,'wk')==='сортировка') fl = SORT_FLOW[fl];
  const s = mst(r,'series'), v = mst(r,'variant'),
        w = mst(r,'sklad'),  c = mst(r,'code');
  const kv = s+SEP+v, kw = kv+SEP+w, kc = kw+SEP+c;
  _aggAdd(B.S, s, fl, t); _aggAdd(B.V, kv, fl, t);
  _aggAdd(B.W, kw, fl, t); _aggAdd(B.C, kc, fl, t);
  /* возврат поставщику — отрицательный ПРИХОД, как в 1С: свою колонку он
     сохраняет, но из «купили» вычитается, иначе колонка брутто и не сходится
     с отчётом 1С. В ОСТАТКЕ он по-прежнему расход (BAL_OUT) — вычесть можно
     только один раз. */
  const ng = NEG_IN[fl];
  if(ng){ _aggAdd(B.S, s, ng, -t); _aggAdd(B.V, kv, ng, -t);
          _aggAdd(B.W, kw, ng, -t); _aggAdd(B.C, kc, ng, -t); }
  const bi = (weight && r[MC.born]) ? BIRTH_IN[fl] : null;
  if(bi){ _aggAdd(B.S, s, bi, t);  _aggAdd(B.V, kv, bi, t);
          _aggAdd(B.W, kw, bi, t); _aggAdd(B.C, kc, bi, t);
          _aggAdd(B.S, s, '_born', t);  _aggAdd(B.V, kv, '_born', t);
          _aggAdd(B.W, kw, '_born', t); _aggAdd(B.C, kc, '_born', t); }
  let a = B.ROWS.get(kc); if(!a){ a = []; B.ROWS.set(kc, a); } a.push(r);
  let wm = B.W_OF.get(kv); if(!wm){ wm = new Map(); B.W_OF.set(kv, wm); }
  let wo = wm.get(w); if(!wo){ wo = {}; wm.set(w, wo); }
  wo[fl] = (wo[fl] || 0) + t;
  if(ng) wo[ng] = (wo[ng] || 0) - t;
  if(bi){ wo[bi] = (wo[bi] || 0) + t; wo['_born'] = (wo['_born'] || 0) + t; }
  let cs = B.CODES.get(kw); if(!cs){ cs = new Set(); B.CODES.set(kw, cs); }
  cs.add(c);
  let us = B.UNITS.get(kv); if(!us){ us = new Set(); B.UNITS.set(kv, us); }
  us.add(Math.abs(r[MC.tonnes])>1e-9 ? MMAIN : (mst(r,'mc')||'ед.'));
  /* варианты серии ПО ДВИЖЕНИЯМ: уровень варианта в дереве строился только по
     продажам, и вариант без единой продажи пропадал целиком (см. seriesNodes) */
  let vs = B.VARS.get(s); if(!vs){ vs = new Set(); B.VARS.set(s, vs); }
  vs.add(v);
}
/** Единицы, в которых видна строка движения: вес и/или своя невесовая. */
function rowUnits(r){
  const out = [];
  if(Math.abs(r[MC.tonnes])>1e-9) out.push(MMAIN);
  const mc = mst(r,'mc');
  if(mc && mc!==MMAIN) out.push(mc);
  return out;
}
/** ⚠️ ВСЕ единицы — ОДНИМ проходом. «Весь оборот» рисует полосу на каждую
    величину, и одиннадцать отдельных проходов по 304 тыс. строк давали 700 мс
    на открытие вкладки. Здесь строки раскладываются по своим агрегатам за раз. */
function warmUnits(){
  const need = MEASURES.map(m=>m.key).filter(k=>!_AGG_CACHE.has(k));
  if(!need.length) return;
  const B = {};
  need.forEach(k=>{ B[k] = newAgg(); });
  for(const r of MOVES){
    const us = rowUnits(r);
    for(const u of us){
      const b = B[u];
      if(!b) continue;
      aggRow(b, r, u===MMAIN ? tShow(r[MC.tonnes]) : r[MC.mq], u===MMAIN);
    }
  }
  need.forEach(k=>_AGG_CACHE.set(k, B[k]));
}
function flowAgg(){
  const _M0 = curMeasure();
  const _hit = _AGG_CACHE.get(_M0);
  if(_hit) return _hit;
  if(_AGG && _AGG_M === _M0) return _AGG;
  const B = newAgg();
  const weight = measBase(_M0)===MMAIN;
  for(const r of MOVES){
    if(!mIn(r)) continue;
    aggRow(B, r, mVal(r), weight);
  }
  _AGG = B; _AGG_M = _M0;
  _AGG_CACHE.set(_M0, B);
  return B;
}
function addInto(acc, src){ if(src) for(const k in src) acc[k] = (acc[k]||0) + src[k]; return acc; }
const keyOf = {
  s: r => r.series,
  v: r => r.series+SEP+r.series_variant,
  w: r => r.series+SEP+r.series_variant+SEP+r.sklad,
  c: r => r.series+SEP+r.series_variant+SEP+r.sklad+SEP+r.code,
};
/** Суммы потоков для уровня дерева. Один и тот же ключ не складываем дважды. */
function aggOf(rows, lvl){
  const A = flowAgg(), src = {s:A.S, v:A.V, w:A.W, c:A.C}[lvl];
  const acc = {}, seen = new Set(), kf = keyOf[lvl];
  for(const r of rows){
    const k = kf(r);
    if(seen.has(k)) continue;
    seen.add(k);
    addInto(acc, src.get(k));
  }
  return acc;
}
/* ОСТАТОК: сколько ещё лежит на наших складах и базах и может быть продано.
   Приход минус расход по тому же ключу, поэтому колонка складывается снизу
   вверх наравне с остальными. Внутренние потоки входят обеими сторонами:
   «приехало» кладёт металл на склад, «уехало» снимает. */
const BAL_IN  = ['куплено','приехало','переработка_вернули','сортировка_вернули',
                 'излишки','ввод_остатков','пересортица',
                 /* найдены сверкой 12.08.2026: 209 строк регистра не попадали
                    ни в один поток, и остаток расходился с 1С на 136 ключах */
                 'возврат_от_клиента','корректировки',
                 /* односторонний приход переработки — см. ⚠️ ниже (14.09.2026) */
                 'переработка_приход_без_пары'];
/* ⚠️ «возврат» в этом списке НЕТ, и это не описка. К моменту подсчёта остатка
   возврат поставщику УЖЕ вычтен из «купили» (см. NEG в flowAgg — там колонка
   прихода приведена к виду 1С). Оставить его ещё и здесь значит вычесть дважды:
   у БП 1563 остаток показывал −111,503 вместо +54,430 — ровно на 165,933 т
   возврата. В Python сальдо считается от СЫРЫХ потоков, поэтому там возврат в
   BALANCE_OUT остаётся — вычесть надо ровно один раз, но в каждом месте свой. */
const BAL_OUT = ['уехало','переработка_забрали','сортировка_забрали','продано','списано',
                 'списано_на_затраты','внутренний_оборот',
                 /* односторонний расход переработки — см. ⚠️ ниже (14.09.2026) */
                 'переработка_расход_без_пары'];
/* ⚠️ ОДНОСТОРОННИЕ ПЕРЕРАБОТКИ ВХОДЯТ В ОСТАТОК — решение заказчика 14.09.2026
   («делай вариант 1»: разницы с 1С быть не должно). Тонны, ставшие штуками
   (20,66 т трубы → 34 шт ёмкостей), из тоннажного остатка уходят, лом от
   разборки штучной единицы — приходит: ровно так считает 1С. Прежнее правило
   11.08.2026 (БП 1586: односторонние — диагностика, остаток не меняют)
   отменено — с ним остаток расходился с 1С у 237 БП. Списки здесь и
   BALANCE_IN/OUT в src/config.py обязаны совпадать по смыслу. */
function balanceOf(v){
  let s = 0;
  for(const k of BAL_IN)  s += v[k] || 0;
  for(const k of BAL_OUT) s -= v[k] || 0;
  /* приход через разборку сидит и в «купили», и в «переработка_вернули» —
     в остатке он должен быть посчитан один раз, поэтому дубль снимаем */
  s -= v['_born'] || 0;
  return s;
}
/* ВТОРОЙ ОСТАТОК — «КАК СЧИТАЕТ 1С» (решение заказчика 12.08.2026).
   1С считает остаток буквально: приход минус расход по каждой строке регистра.
   Наш ОСТАТОК — модель ПРОЕКТА, из неё намеренно выброшены односторонние
   переработки: диагностический расход без весовой пары не должен уменьшать
   остаток бизнес-плана потерей, которой не было (БП 1586, §5г). Для СКЛАДА это
   правило неверно — если 1С оприходовала металл на склад, он там лежит.
   Поэтому в дереве стоят ОБА, рядом. Разница между ними — ровно односторонние
   переработки, и она видна глазом, а не выводится вычитанием.
   ⚠️ Обе формулы линейны по потокам, поэтому обе складываются снизу вверх:
   инвариант дерева не нарушен ни для одной. Никакой логики «на складе одна
   формула, на проекте другая» нет и быть не должно. */
/* с 14.09.2026 формула одна: односторонние уже в BAL_IN / BAL_OUT */
function balanceOf1c(v){ return balanceOf(v); }

/** Строка дерева: движения из регистра + «ПРОДАЛИ» по ОТФИЛЬТРОВАННЫМ позициям
    (фильтры дерева обязаны влиять на проданное, а движения показывают весь путь). */
function lcVals(rows, lvl){
  const v = aggOf(rows, lvl);
  /* остаток считаем по ПОЛНОМУ движению, до подмены «продано» фильтром дерева */
  v['остаток'] = balanceOf(v);
  /* ⚠️ Отбираем строки ПО ЕДИНИЦЕ: остальные колонки приходят из flowAgg, где
     отбор уже сделан, а «продано» считается прямо по позициям. Без фильтра в
     разрезе «штуки» складывались тонны с штуками — у БП 1124 «продали»
     показывало 2 485,26 вместо 9 шт. */
  v['продано'] = sum(rows.filter(iIn), r=>iVal(r));
  return v;
}
/** Какие единицы есть у этого набора строк — по позициям и по движениям. */
function unitsOf(rows){
  const A = flowAgg(), u = new Set();
  rows.forEach(r=>{ if(Math.abs(r.tonnes)>1e-9) u.add(MMAIN);
                    if(r.mc && r.mc!==MMAIN) u.add(r.mc); });
  const seen = new Set();
  for(const r of rows){
    const kv = r.series+SEP+r.series_variant;
    if(seen.has(kv)) continue;
    seen.add(kv);
    (A.UNITS.get(kv)||[]).forEach(x=>u.add(x));
  }
  return [...u].sort((a,b)=> a===MMAIN ? -1 : b===MMAIN ? 1 : a.localeCompare(b,'ru'));
}
/** Полоса жизненного цикла для строки дерева.
    ⚠️ В режиме «все единицы» РИСУЕМ ПО ПОЛОСЕ НА КАЖДУЮ ЕДИНИЦУ. Складывать
    тонны с километрами и штуками нельзя ни в одной колонке — у «БЕЗ СЕРИИ»
    так получался остаток «−12 343», собранный из тонн, штук и литров разом. */
function lcNode(rows, lvl){
  /* ⚠️ Под уровнем единицы полоса ОДНА, но ведущую колонку с подписью единицы
     (`.lc.lcu i.uu`) она обязана сохранить: у строки проекта эта колонка есть, и
     без неё цифры нижних строк уезжали на 42 px влево относительно шапки и
     собственного родителя. Та же ловушка, из-за которой колонку заводили. */
  if(curMeasure()!==MEASURE_ALL) return lcStrip(lcVals(rows, lvl), UNIT_LEVEL || '');
  warmUnits();                     /* агрегаты всех единиц — одним проходом */
  const us = unitsOf(rows);
  if(us.length <= 1){
    const keep = MEASURE_FORCED; MEASURE_FORCED = us[0] || MMAIN;
    const html = lcStrip(lcVals(rows, lvl), MEASURE_FORCED);
    MEASURE_FORCED = keep;
    return html;
  }
  const keep = MEASURE_FORCED;
  const out = us.map(u=>{ MEASURE_FORCED = u; return lcStrip(lcVals(rows, lvl), u); });
  MEASURE_FORCED = keep;
  return '<span class="lcmulti">'+out.join('')+'</span>';
}

/* ---------- «откуда продано»: тип подразделения → склад ----------
   Один и тот же проект обычно отгружается с нескольких мест: часть с базы,
   часть с цеха, часть с договорной площадки. Этот уровень показывает разбивку. */
const SITE_ICON = {'Цех':'🏭','База':'🏢','Заготовка':'⛏','Площадка':'📍',
                   'Демонтаж':'🔧','АУП':'🏛',
                   'Ответхранение':'📦','Собственные склады':'🏬'};
const SITE_RANK = {'Цех':0,'Заготовка':1,'Площадка':2,'База':3,
                   'Ответхранение':4,'Собственные склады':5,'Демонтаж':6,'АУП':7};

/** Уровень «откуда продано»: тип подразделения → склад → номенклатура.
    Показываем не только склады отгрузки, но и те, через которые металл прошёл
    (купили в Когалыме → уехало → приехало на Осенцы → продали): иначе в таблице
    не видно, откуда товар взялся. У таких складов «ПРОДАЛИ» пусто, стоит
    пометка «только движения». */
function whereNodes(parent, rows, buildLeaf, movesOnly){
  const A = flowAgg();
  /* movesOnly — у варианта продаж нет вовсе, и строки сюда пришли-заготовки без
     склада. Тогда ВСЕ склады берутся из индекса движений (ветка extra ниже),
     и каждый честно помечается «только движения». */
  const bySkl = movesOnly ? new Map() : groupBy(rows, r=>r.sklad);
  const site0 = new Map();                       // склад -> тип подразделения
  rows.forEach(r=>site0.set(r.sklad, r.site||'—'));
  const SK_SITE = DATA.sk_site || {};
  const SK_UNIT = DATA.sk_unit || {};
  /* склады без продаж, но с движениями по этим сериям/вариантам */
  const extra = new Map();
  const seenV = new Set();
  for(const r of rows){
    const kv = r.series+SEP+r.series_variant;
    if(seenV.has(kv)) continue;
    seenV.add(kv);
    const wm = A.W_OF.get(kv);
    if(!wm) continue;
    wm.forEach((ag, w)=>{
      if(bySkl.has(w) || !w) return;
      let cur = extra.get(w);
      if(!cur){ cur = {agg:{}, keys:[]}; extra.set(w, cur); }
      addInto(cur.agg, ag);
      cur.keys.push({series:r.series, series_variant:r.series_variant, sklad:w,
                     site:SK_SITE[w]||'—', base:'', division:SK_UNIT[w]||'',
                     tonnes:0, docs:[], badge:r.badge, code:null, name:'',
                     last_dt:'', first_dt:''});
    });
  }
  /* единый список: тип подразделения -> склады (с продажами и без) */
  const bySite = new Map();
  const push = (site, sklad, entry)=>{
    let m = bySite.get(site); if(!m){ m = new Map(); bySite.set(site, m); }
    m.set(sklad, entry);
  };
  bySkl.forEach((wrows, skl)=> push(site0.get(skl)||'—', skl, {rows:wrows, only:false}));
  extra.forEach((cur, w)=> push(SK_SITE[w]||'—', w, {rows:cur.keys, only:true, agg:cur.agg}));

  [...bySite.keys()].sort((a,b)=>(SITE_RANK[a]??9)-(SITE_RANK[b]??9)).forEach(site=>{
    const m = bySite.get(site);
    const srows = [].concat(...[...m.values()].filter(e=>!e.only).map(e=>e.rows));
    const allRows = [].concat(...[...m.values()].map(e=>e.rows));
    /* Подпись — ПОДРАЗДЕЛЕНИЕ из справочника производственных подразделений,
       а не база. «Когалым КНПО» — заготовительная площадка Западной Сибири;
       приписка «База Когалым» бралась из справочника складов (ВысшийРодитель
       «5. База Когалым») и была неверна по существу: у заготовки базы нет. */
    const units=[...new Set(allRows.map(r=>r.division||r.base).filter(b=>b&&b!=='—'))];
    const lbl=(SITE_ICON[site]||'•')+' <b>'+esc(site)+'</b>'
      + (units.length?' <span class="tag" title="Подразделения из справочника производственных подразделений: '
          +esc(units.join(', '))+'">'
          +esc(units.slice(0,3).join(', '))
          +(units.length>3?' +'+(units.length-3):'')+'</span>':'');
    parent.appendChild(treeNode(lbl, lcNode(allRows, 'w'), k=>{
      const order = [...m.keys()].sort((a,b)=>{
        const A1=m.get(a), B1=m.get(b);
        if(A1.only!==B1.only) return A1.only?1:-1;          // сперва склады с продажами
        return sum(B1.rows,r=>iVal(r))-sum(A1.rows,r=>iVal(r));
      });
      order.forEach(skl=>{
        const e = m.get(skl);
        const tag = e.only ? ' <span class="tag">только движения — продаж отсюда не было</span>' : '';
        const r0 = e.rows[0];
        const ou = r0 ? otherUnits(r0.series+SEP+r0.series_variant+SEP+skl, 'w') : '';
        k.appendChild(treeNode(esc(skl)+tag+ou+dtTag(e.rows), lcNode(e.rows,'w'),
          k2=>buildLeaf(k2, e.rows, e.only), {wide:true}));
      });
    }, {wide:true}));
  });
}
/** Шапка «куплено · продано» над деревом. */
function colHead(){
  const box=el('div','colhead-box');
  /* ⚠️ В режиме «все единицы» у строк появляется ведущая колонка с подписью
     единицы — шапка обязана получить такую же, иначе числа уезжают вправо
     относительно заголовков. Ширина колонки задана переменной, одной на всех. */
  const U = curMeasure()===MEASURE_ALL;
  const lcCls = 'lc'+(U?' lcu':'');
  const uCell = U ? '<i class="uu">ед.</i>' : '';
  // ряд 1 — группы: сразу видно, что перемещения и резка это внутренние движения
  const g=el('div','colhead grp');
  /* ⚠️ ШИРИНЫ ГРУПП СЧИТАЮТСЯ ПО САМИМ КОЛОНКАМ, а не берутся из расчёта:
     META.flow_group_head не знает ни про сортировку (выделена в оболочке), ни
     про «вернул клиент» и «корректировки». Любая пропущенная колонка сдвигает
     заголовок группы относительно чисел под ним. Порядок групп в расчёте:
     куплено · внутри компании · выбыло · осталось. */
  const heads=(META.flow_group_head||[]).map(([n,sp],i)=>
      [n, i===0 ? LC_N.lead : i===1 ? LC_N.inn : i===2 ? 1+LC_N.out
                : i===3 ? LC_N.bal : sp]);
  g.innerHTML='<span class="tw" style="width:16px"></span><span class="hd"><span class="lbl"></span></span>'
    +'<span class="'+lcCls+'">'+(U?'<u></u>':'')+heads.map(([name,span],idx)=>
        '<u class="'+(idx?'gs':'')+'" style="grid-column:span '+span+'">'+esc(name)+'</u>'
      ).join('')+'</span>';
  // ряд 2 — сами колонки
  const h=el('div','colhead');
  h.innerHTML='<span class="tw" style="width:16px"></span><span class="hd"><span class="lbl">'
    +(window.__COLHEAD||'проект · серия · откуда продано · склад · номенклатура')+'</span></span>'
    +'<span class="'+lcCls+'">'+uCell+LC_COLS.map(([k,l,kind,sep])=>
        '<i class="'+((kind==='sold'?'s':'aux')+(sep?' gs':''))+'" title="'
        +esc(BADGE_TITLE_FLOW[k]||'')+'">'+esc(l)+'</i>').join('')+'</span>';
  box.appendChild(g); box.appendChild(h);
  return box;
}

/* ---------- порядок узлов дерева: по тоннажу или по дате ---------- */
function sortMode(){ const e=$('#fsort'); return e?e.value:'t'; }
const maxDt = rows => rows.reduce((m,r)=> r.last_dt>m?r.last_dt:m, '');
const minDt = rows => rows.reduce((m,r)=> (m===''||(r.first_dt&&r.first_dt<m))?r.first_dt:m, '');
/** Ключи Map(ключ->строки) в выбранном порядке. */
function orderedKeys(m, cmpTonnes){
  const ks=[...m.keys()], mode=sortMode();
  if(mode==='new') return ks.sort((a,b)=> maxDt(m.get(b)).localeCompare(maxDt(m.get(a))));
  if(mode==='old') return ks.sort((a,b)=> minDt(m.get(a)).localeCompare(minDt(m.get(b))));
  if(mode==='az')  return ks.sort((a,b)=> String(a).localeCompare(String(b),'ru'));
  return ks.sort(cmpTonnes || ((a,b)=> sum(m.get(b),r=>iVal(r))-sum(m.get(a),r=>iVal(r))));
}
/** Позиции внутри листа в выбранном порядке. */
function orderedRows(rows){
  const mode=sortMode();
  if(mode==='new') return rows.slice().sort((a,b)=> String(b.last_dt).localeCompare(String(a.last_dt)));
  if(mode==='old') return rows.slice().sort((a,b)=> String(a.first_dt).localeCompare(String(b.first_dt)));
  return rows.slice().sort((a,b)=> iVal(b)-iVal(a));
}
/** Подпись с датой последней продажи — видно, свежая позиция или старая. */
function dtTag(rows){
  const d=maxDt(rows); return d?' <span class="tag">посл. '+esc(d)+'</span>':'';
}


/* ---------- уровень «серия» под проектом ----------
   Проект обобщает варианты написания серии: «…(ЗС-УВМ) ДС35»,
   «…(ЗС-УВМ-Армада) ДС35», «… ДС35 V» — это один проект, но три РАЗНЫЕ строки
   серии в документах 1С. Показываем их отдельно, чтобы цифры сходились с
   выгрузкой 1С, где группируют по серии, а не по проекту. */
/* Настоящая строка серии 1С или наша заглушка? Заглушка — это имя ПРОЕКТА плюс
   пометка (META.var_synth_mark), её ставит var_label, когда точной строки серии у
   движения нет. Экономисты читали её как реальную серию и шли сверять с 1С, где
   такой серии не существует, — тем более что в дереве подпись обрезается и
   пометка не видна. Поэтому подпись чистим, а признак выносим в бейдж. */
const V_SYNTH = META.var_synth_mark || '';
const V_UNKNOWN = META.var_unknown || '';
const isSynthVar = v => !!V_SYNTH && String(v).indexOf(V_SYNTH) >= 0;
function varLabel(v){
  return isSynthVar(v) ? String(v).split(V_SYNTH)[0] : v;
}
function seriesNodes(parent, rows){
  const byVar = groupBy(rows, r=>r.series_variant||'—');
  /* ⚠️ УРОВЕНЬ ВАРИАНТА СТРОИЛСЯ ТОЛЬКО ПО ПРОДАЖАМ — и вариант, по которому
     продаж ещё не было, пропадал ЦЕЛИКОМ, на обеих вкладках. У БП 1491/1493 так
     не было видно серии «… (Лукойл Коми)) VS»: 16,529 т куплено и перевезено на
     Склад МАТ Б.Усинск, продаж ноль. В итог проекта эти тонны входили (куплено
     5 625,699 сходилось с 1С), а раскрыть их было негде.
     На уровне СКЛАДА и НОМЕНКЛАТУРЫ это уже решено (whereNodes / posNodes
     добавляют «только движения»), на уровне варианта — не было.
     Достраиваем из индекса движений: строка-заготовка несёт только проект и
     вариант, всё остальное берётся из flowAgg по этому ключу. */
  const _A = flowAgg();
  const _pk = rows.length ? rows[0].series : null;
  if(_pk){
    (_A.VARS.get(_pk) || []).forEach(v=>{
      const key = v || '—';
      if(byVar.has(key)) return;
      byVar.set(key, [{series:_pk, series_variant:v, sklad:'', site:'—', base:'',
                       division:'', tonnes:0, docs:[], badge:'нет', code:null,
                       name:'', last_dt:'', first_dt:'', _movesOnly:true}]);
    });
  }
  const keys = orderedKeys(byVar);
  keys.forEach(v=>{
    const vrows=byVar.get(v);
    const movesOnly = vrows.every(r=>r._movesOnly);
    const synth = isSynthVar(v) || v===V_UNKNOWN;
    const lbl = (keys.length>1?'🔖 ':'')+esc(varLabel(v))
      + (synth ? '<span class="tag nosr" title="'+esc('Точной строки серии 1С у этих '
          + 'движений нет — показано имя проекта. Сверять эту строку с отчётом 1С '
          + 'по серии нельзя: такой серии в 1С не существует. Серия восстановлена '
          + 'по лоту, документу или наследованием при переработке — см. бейдж '
          + 'происхождения справа.')+'">'+esc(META.var_synth_badge||'серия не указана')
          +'</span>' : '')
      + (movesOnly ? '<span class="tag" title="'+esc('По этой строке серии продаж '
          + 'ещё не было: металл куплен и/или перевезён, но не продан. Раньше '
          + 'такой вариант в дереве не показывался вовсе, хотя его тонны входили '
          + 'в итог проекта.')+'">только движения — продаж не было</span>' : '')
      + otherUnits(vrows[0].series+SEP+v, 'v')
      + dtTag(vrows);
    parent.appendChild(treeNode(lbl, lcNode(vrows,'v'), k=>{
      whereNodes(k, vrows, (k2, wrows, onlyMoves)=>{
        posNodes(k2, wrows, onlyMoves);
      }, movesOnly);
    }, {badge:badge(worstOf(vrows)), wide:true}));
  });
}

/** Уровень номенклатуры внутри склада + документы отдельными строками.
    Коды берутся и из продаж, и из движений: под складом видно всё, что через
    него прошло, а не только проданное. */
function posNodes(parent, wrows, onlyMoves){
  const A = flowAgg();
  const byPos = groupBy(wrows.filter(r=>r.code), r=>r.code+'|'+r.name);
  /* коды, по которым на этом складе были движения, но продаж не было */
  const soldCodes = new Set(wrows.map(r=>r.code).filter(Boolean));
  const extra = new Map();
  const seen = new Set();
  for(const r of wrows){
    const kw = r.series+SEP+r.series_variant+SEP+r.sklad;
    if(seen.has(kw)) continue;
    seen.add(kw);
    const codes = A.CODES.get(kw);
    if(!codes) continue;
    codes.forEach(c=>{
      if(soldCodes.has(c) || extra.has(c)) return;
      extra.set(c, {series:r.series, series_variant:r.series_variant,
                    sklad:r.sklad, site:r.site, base:r.base, code:c,
                    name:(DATA.nm||{})[c]||c, tonnes:0, docs:[], badge:r.badge,
                    last_dt:'', first_dt:''});
    });
  }
  const nodes = [];
  orderedKeys(byPos).forEach(pk=>{
    const prows=byPos.get(pk), p0=prows[0];
    nodes.push({rows:prows, p0, only:false});
  });
  extra.forEach(er=> nodes.push({rows:[er], p0:er, only:true}));
  nodes.forEach(n=>{
    const p0=n.p0, prows=n.rows;
    const tag = n.only ? ' <span class="tag">только движения</span>' : '';
    parent.appendChild(treeNode(esc(p0.name)+tag+dtTag(prows), lcNode(prows,'c'),
      k3=>{ k3.appendChild(docLines(prows)); },
      /* __NOM_CM — значок комментариев по запасам на строке номенклатуры.
         Ставится ТОЛЬКО из разбора Ганты (leafTree выставляет хук на время
         вызова): в остальных деревьях комментариев нет, и пустой вызов не
         должен ничего менять. */
      {code:p0.code, badge:(n.only?'':badge(worstOf(prows)))
         + (window.__NOM_CM ? window.__NOM_CM(p0) : ''), wide:true}));
  });
}

/** Документы отдельными СТРОКАМИ в тех же колонках: каждый документ ставит своё
    количество в свою ячейку (купили / уехало / приехало / в производство / из производства /
    ПРОДАЛИ / возврат / недостачи / на затраты). Строки одного лота идут подряд —
    цепочка металла читается сверху вниз. */
function docLines(prows){
  const A = flowAgg();
  const box = el('div','doclines');
  const rows = [];
  const seen = new Set();
  for(const r of prows){
    const kc = r.series+SEP+r.series_variant+SEP+r.sklad+SEP+r.code;
    if(seen.has(kc)) continue;
    seen.add(kc);
    for(const m of (A.ROWS.get(kc)||[])) rows.push(m);
  }
  if(!rows.length){
    box.appendChild(el('div','hint','Движений по этой позиции в выгрузке нет.'));
    return box;
  }
  /* группируем по лоту, лоты — по свежести, внутри лота — по дате (путь металла) */
  const byLot = groupBy(rows, r=>mlotk(r)||'— без лота —');
  const lastOf = a => a.reduce((m,r)=> mst(r,'dt')>m?mst(r,'dt'):m, '');
  const desc = sortMode()!=='old';
  const lots = [...byLot.keys()].sort((a,b)=> desc
    ? lastOf(byLot.get(b)).localeCompare(lastOf(byLot.get(a)))
    : lastOf(byLot.get(a)).localeCompare(lastOf(byLot.get(b))));
  lots.slice(0, 60).forEach(lot=>{
    const rs = byDate(byLot.get(lot));
    const tot = {}; rs.forEach(r=> tot[mfl(r)] = (tot[mfl(r)]||0) + mVal(r));
    tot['остаток'] = balanceOf(tot);      // сколько от лота ещё лежит на складе
    box.appendChild(el('div','dl lot',
      '<span class="tw"></span><span class="hd"><span class="lbl">лот <b>'+esc(lotLabel(lot)||lot)+'</b>'
      +' <span class="tag">'+rs.length+' движений</span></span></span>'
      +'<span class="val wide">'+lcStrip(tot)+'</span>'));
    rs.forEach(r=>{
      const phrase = movePhrase(r);
      /* пара документа: ушло в производство / уехало — и что по нему вернулось */
      const pr = docPair(r);
      const line = el('div','dl',
        '<span class="tw"></span><span class="hd"><span class="lbl" title="'
        +esc(mst(r,'dt')+' · документ '+mst(r,'doc')+' · '+phrase
             +' · '+(NM[mst(r,'code')]||mst(r,'code')))+'">'
        +'<span class="dt">'+esc(mst(r,'dt'))+'</span>'
        +'<span class="rn">'+esc(mst(r,'doc'))+'</span>'
        /* значок расхождения — сразу за номером документа, а не в конце строки:
           в режиме «подписи целиком» конец подписи уезжает за правый край, и
           единственное, ради чего строку красили, приходилось искать прокруткой */
        +gapChip(pr)
        +'<span class="ph">'+esc(phrase)+'</span>'
        +badge(mst(r,'badge'))
        +'<button class="whysr" title="Показать, откуда взялась серия у этой '
          +'строки: лот, документ-родитель лота и что написала в нём сама 1С">'
          +'откуда серия</button>'
        +'</span></span><span class="val wide">'
        +lcStrip({[mfl(r)]: mVal(r)})+'</span>');
      box.appendChild(line);
      attachFate(line, r, pr);
      let panel = null;
      line.querySelector('.whysr').addEventListener('click', ev=>{
        ev.stopPropagation();
        if(panel){ panel.remove(); panel=null; return; }
        panel = whySeries(r);
        line.after(panel);
      });
    });
  });
  if(byLot.size>60)
    box.appendChild(el('div','hint','Показаны 60 лотов из '+byLot.size+'.'));
  return box;
}

/* ---------- «ОТКУДА СЕРИЯ»: цепочка восстановления на одной строке ----------
   Серия записана в самой строке 1С только у 27 % тоннажа; остальное мы
   ВОССТАНАВЛИВАЕМ. Экономист видел цифру и не видел оснований — разбор одного
   спорного случая (БП 1124) занимал полдня переписки. Здесь та же цепочка
   собирается за секунду: строка -> её лот -> документ, создавший лот -> что
   написала в нём сама 1С -> на какой ступени каскада мы взяли серию.
   Всё считается из DATA.moves, дополнительных данных не нужно. */
const BADGE_WHY = {
  '1С': 'серия записана в самой строке 1С — это не расчёт, а факт из выгрузки',
  'ручная': 'ручная привязка из data/overrides.json — решение заказчика',
  'донор': 'серия поправлена по остатку склада (правило заказчика 14.09.2026): '
    + 'под серией, записанной в 1С, со склада списали больше, чем на нём было, '
    + 'а металл этой серии приехал сюда перемещением и остался — расход '
    + 'переписан на неё. Первыми переписываются самые поздние списания: свой '
    + 'металл уходил раньше, перебор — в конце',
  'док': 'серия взята с ЛОТА: у прихода этого лота она известна',
  'переработка': 'унаследована при переработке: сырьё этой серии доминировало в '
    + 'документе. Оттуда же взята и сама строка серии — если у сырья этого '
    + 'проекта в документе она одна; если серий было несколько, подпись '
    + 'остаётся именем проекта с пометкой «серия не указана»',
  'доля': 'лот смешанный, строка поделена между сериями пропорционально составу',
  'вероятно': 'на этот склад под этим кодом приходила только одна серия',
  'площадка': 'серия ВЫВЕДЕНА из имени договорного склада — настоящей серии 1С нет',
  'склад': 'склад закреплён за единственной серией 1С',
  'ввод': 'пометка «ввод остатков» / «излишки»',
  'нет': 'серию восстановить не удалось — показано «БЕЗ СЕРИИ»',
};
const IN_FLOWS = ['куплено','переработка_вернули','приехало','излишки','ввод_остатков'];
function whySeries(r){
  const box = el('div','whybox');
  const lot = mlotk(r), bg = mst(r,'badge');
  const rows = lot ? MOVES.filter(x=>mlotk(x)===lot && IN_FLOWS.indexOf(mfl(x))>=0) : [];
  const esc2 = x => esc(String(x==null?'':x));
  let h = '<div class="wh1">Откуда серия у этой строки</div>'
    + '<div class="whrow"><i>ступень каскада</i><b>'+esc2(bg)+'</b> — '
      + esc2(BADGE_WHY[bg]||'') + '</div>'
    + '<div class="whrow"><i>серия в строке 1С</i>'
      + (bg==='1С' ? '<b>'+esc2(mst(r,'variant'))+'</b> (записана в документе)'
                   : '<b class="muted">пусто</b> — в выгрузке серии на этой строке нет')
      + '</div>'
    + '<div class="whrow"><i>лот (партия)</i><b>'+(lot?esc2(lot):'<span class="muted">нет</span>')+'</b></div>';
  if(rows.length){
    h += '<div class="wh1">Приход этого лота — отсюда и берётся серия</div>'
      + '<table class="whtab"><thead><tr><th>дата</th><th>документ</th><th>поток</th>'
      + '<th>номенклатура</th><th>серия, записанная в 1С</th><th class="num">кол-во</th>'
      + '</tr></thead><tbody>'
      + rows.slice(0,12).map(x=>'<tr><td>'+esc2(mst(x,'dt'))+'</td><td>'+esc2(mst(x,'doc'))
        +'</td><td>'+esc2(MSHORT[mfl(x)]||mfl(x))+'</td><td>'
        +esc2(NM[mst(x,'code')]||mst(x,'code'))+'</td><td>'
        +(mst(x,'badge')==='1С' ? '<b>'+esc2(mst(x,'variant'))+'</b>'
            : '<span class="muted">'+esc2(mst(x,'variant'))+' (тоже восстановлена)</span>')
        +'</td><td class="num">'+fmt3(mVal(x))+'</td></tr>').join('')
      + '</tbody></table>'
      + (rows.length>12 ? '<div class="hint">Показаны 12 приходов из '+rows.length+'.</div>' : '');
  } else if(lot){
    h += '<div class="hint">Прихода по этому лоту в выгрузке нет — металл пришёл '
       + 'до начала периода либо под другим лотом.</div>';
  }
  /* готовая строка для ручной привязки: ключ включает ЛОТ, иначе правка
     накроет соседние строки того же документа с другими сериями */
  /* ⚠️ В ключ ручной привязки идёт НОМЕР лота (mst 'lot'), а НЕ полный ключ:
     формат overrides.json менять нельзя — прежние правки заказчика должны
     работать без переделки (§6). Группировка и подпись в дереве при этом
     живут на полном ключе `lotk`, это разные вещи. */
  const ov = JSON.stringify({regnum: mst(r,'doc'), lot: mst(r,'lot'), code: mst(r,'code'),
     series: mst(r,'variant'), author: '', note: ''}, null, 1);
  h += '<div class="wh1">Если серия неверна — привязать вручную</div>'
    + '<div class="hint">Добавьте это в <b>data/overrides.json</b>, поправив '
    + '<b>series</b>, и запустите пересборку. Ключ включает лот, поэтому '
    + 'соседние строки того же документа с другими сериями не пострадают.</div>'
    + '<pre class="whjson">'+esc(ov)+'</pre>'
    + '<button class="btn3 whcopy">Скопировать</button>';
  box.innerHTML = h;
  const cp = box.querySelector('.whcopy');
  if(cp) cp.addEventListener('click', ev=>{
    ev.stopPropagation();
    try{ navigator.clipboard.writeText(ov); cp.textContent='Скопировано'; }
    catch(e){ cp.textContent='Выделите текст выше и скопируйте'; }
  });
  return box;
}

/* ---------- полоса жизненного цикла в строке дерева ----------
   купили | уехало | приехало | в сортировку | из сортировки | в производство |
   из производства | продали | возврат | недостачи | на затраты | ОСТАТОК.
   Все колонки заполняются на КАЖДОМ уровне дерева — от направления до
   отдельного документа — из построчных движений. */
/* Колонки четырёх групп. Внутренние движения (уехало↔приехало, забрали↔вернули)
   помечены курсивом и отделены линией: они перекладывают тот же металл, а не
   являются приходом/расходом компании. Складывать их во «поступило» нельзя —
   у 1578 это давало 5 041 т при закупке 2 382 т. */
const BADGE_TITLE_FLOW = META.flow_title || {};
/* ⚠️ В ПОЛОСЕ ПОДПИСИ КОРОЧЕ, ЧЕМ В РАСЧЁТЕ. Колонка шириной 76 px, шрифт
   микро: «ПРОИЗВОДСТВО» в неё не помещается ни при каком переносе — слово
   шире самой колонки. Полные названия остаются в `C.FLOW_LABEL` (их берут
   Excel и «Закупки / продажи», где место есть) и в подсказке колонки. */
const LC_LABEL = Object.assign({}, META.flow_label || {},
  {'сортировка_забрали':'− в сортировку', 'сортировка_вернули':'+ из сортировки',
   'переработка_забрали':'− в произв.',    'переработка_вернули':'+ из произв.',
  });
const LC_COLS=[
  ['куплено',null,'ext'],
  /* ⚠️ «ВЕРНУЛ КЛИЕНТ» И «КОРРЕКТИРОВКИ» В ПОЛОСЕ НЕТ, И ВОЗВРАЩАТЬ ИХ НЕЛЬЗЯ
     (решение заказчика 12.08.2026). Они были добавлены днём ранее, когда
     нашлись 209 потерянных строк регистра, — и сломали строку: полоса
     рассчитана на 12 колонок, с четырнадцатью подпись сжималась, заголовки
     наезжали на числа («упили» вместо «купили»), а на вложенных уровнях текст
     ложился поверх цифр. Читать таблицу стало нельзя.
     Металл при этом НЕ ПОТЕРЯН: оба потока стоят в BAL_IN и BALANCE_1C_IN,
     то есть в ОСТАТОК входят; их суммы видны на «Закупках / продажах» — там
     таблица, а не полоса фиксированной ширины, и место есть.
     Вместе они дают 744,337 т на 209 строк из 306 817 — почти всегда пусто,
     и держать под них две колонки в каждой строке дерева незачем. */
  ['уехало',null,'inn','gs'],
  ['приехало',null,'inn'],
  /* ⚠️ ПОРЯДОК КОЛОНОК — РЕШЕНИЕ ЗАКАЗЧИКА: уехало · приехало, затем
     СОРТИРОВКА, и только потом производство. Так читается путь металла: сначала
     логистика, потом отбор нужного из массы, потом сам передел.
     Сортировка — ОТДЕЛЬНЫЕ колонки: «Производство без заказа» с выгрузки
     11.08.2026 несёт ВИД РАБОТ, и сортировка — не передел (из массы отобрали
     позицию, металл не менял форму). Разделение — РАЗБИЕНИЕ, а не дубль:
     строки сортировки уходят ИЗ пары переработки в свои колонки, сумма
     прежняя, остаток не меняется (обе пары стоят в BAL_IN / BAL_OUT).
     ⚠️ ВТОРАЯ ПАРА НАЗЫВАЕТСЯ «в производство / из производства» (решение
     заказчика 12.08.2026), а НЕ «в резку / из резки»: кроме резки (13 470
     строк) в ней лежат разборка (20 151) и прочее производство (90) —
     пакетирование, обвязка вагонов, демонтаж. Имя «резка» описывало меньшую
     часть содержимого. Переименована ТОЛЬКО ПОДПИСЬ (`FLOW_LABEL` в
     `src/config.py`), ключи потоков прежние — ни одна цифра не сдвинулась.
     Какой именно вид работ был в строке, видно по подписи потока (`WFLOW`) и
     по формулировке движения (`WORK_PHRASE`). */
  ['сортировка_забрали',null,'inn'],
  ['сортировка_вернули',null,'inn'],
  ['переработка_забрали',null,'inn'],
  ['переработка_вернули',null,'inn'],
  ['продано',null,'sold','gs'],
  ['возврат',null,'ext'],
  ['списано',null,'ext'],
  ['списано_на_затраты',null,'ext'],
  /* ⚠️ КОЛОНКИ «ОСТАТОК (1С)» ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО (решение заказчика
     12.08.2026). Она была заведена днём ранее как вторая колонка баланса, и
     оказалась лишней: в дереве нужен ОДИН остаток — проектный, а сверка со
     складским живёт на вкладке «Проверка данных», где для неё есть место и
     разбор по подразделениям. Пятнадцатая колонка вдобавок дожимала подпись
     строки до нуля на обычном мониторе. */
  ['остаток',null,'bal','gs']]
  .map(([k,_l,kind,sep])=>[k, LC_LABEL[k]||k.replace(/_/g,' '), kind, sep]);
/* Ширина сетки полосы — из самого списка колонок: добавили колонку, сетка
   узнала об этом сама. Иначе лишние ячейки переносятся на второй ряд.
   ⚠️ СЧЁТЧИКОВ ТРИ, А НЕ ОДИН. Раньше JS задавал только число внутренних
   колонок, а головная и хвостовая группы были зашиты в CSS как
   `var(--lc-col)` и `repeat(3,var(--lc-col))`. Стоило добавить во внешний
   приход «вернул клиент» и «корректировки» — и полоса снова уехала на ВТОРОЙ
   РЯД, ровно как в прошлый раз с сортировкой. Теперь из LC_COLS считаются все
   три: головные внешние (до первой внутренней), внутренние и хвостовые
   внешние (после ПРОДАЛИ). */
const LC_N = (()=>{ const first=LC_COLS.findIndex(c=>c[2]==='inn');
  const sold=LC_COLS.findIndex(c=>c[2]==='sold');
  return {lead:first<0?1:first,
          inn:LC_COLS.filter(c=>c[2]==='inn').length,
          out:LC_COLS.slice(sold+1).filter(c=>c[2]==='ext').length,
          bal:LC_COLS.filter(c=>c[2]==='bal').length}; })();
try{ const st=document.documentElement.style;
     st.setProperty('--lc-lead-n', String(LC_N.lead));
     st.setProperty('--lc-inn-n',  String(LC_N.inn));
     st.setProperty('--lc-out-n',  String(LC_N.out));
     st.setProperty('--lc-bal-n',  String(LC_N.bal)); }catch(e){}
/* ⚠️ ОДНОСТОРОННЯЯ ПЕРЕРАБОТКА ДОЛЖНА БЫТЬ ВИДНА (заказчик 14.09.2026, БП 1812:
   «нет расходов по производству без заказа, хотя в данных всё есть»). У этих
   потоков нет своей колонки (полоса рассчитана на 12, см. решение 12.08), и
   строка документа выходила ПУСТОЙ — 11,745 т расхода, а в полосе ни одной
   цифры. Правило БП 1586 (§ REWORK_UNPAIRED) не тронуто: остаток они
   по-прежнему не меняют. Показываем их в колонках производства особым
   начертанием, а если в той же ячейке есть парная величина — в подсказке. */
const LC_UNPAIRED = {'переработка_расход_без_пары':'переработка_забрали',
                     'переработка_приход_без_пары':'переработка_вернули'};
const LC_UNP_TIP = 'без весовой пары: сырьё ушло в штучный выпуск (или пришло из '
  + 'него) без веса на другой стороне документа. В ОСТАТОК бизнес-плана не '
  + 'входит (правило БП 1586), в складской остаток на «Проверке данных» — входит.';
function lcStrip(vals, unit){
  const unp = {};
  for(const k in LC_UNPAIRED){
    const v = vals[k];
    if(v!=null && Math.abs(v)>=0.0005) unp[LC_UNPAIRED[k]] = (unp[LC_UNPAIRED[k]]||0) + v;
  }
  return '<span class="lc'+(unit?' lcu':'')+'">'
    + (unit ? '<i class="uu" title="'+esc('Всё в этой строке — в единице «'+unit
        + '». Разные единицы не складываются, поэтому у строки своя полоса на '
        + 'каждую.')+'">'+esc(unit)+'</i>' : '')
    + LC_COLS.map(([k,,kind,sep])=>{
    const v=vals[k];
    let cls=(kind==='sold'?'s':kind==='bal'?'bal':kind==='inn'?'aux inn':'aux')+(sep?' gs':'');
    // отрицательный остаток = по этому ключу расход больше прихода: в регистре
    // не хватает поступления. Прячем такое нельзя, помечаем цветом.
    if(kind==='bal' && v!=null && v < -0.0005) cls += ' neg';
    // ноль не печатаем — иначе строка забита нулями и цифры не видно
    /* ⚠️ ОТ 100 ТЫСЯЧ — БЕЗ ДРОБНОЙ ЧАСТИ (заказчик 21.08.2026: «слегка
       съедаются строки»). «125 503,814» — 11 знаков, а колонка полосы 78–88 px:
       на Windows-рендеринге шрифта такое число заполняло ячейку целиком и
       наползало на соседнюю — числа слипались. Тысячные у сотен тысяч всё
       равно не читаются; точное значение — в подсказке ячейки. */
    const big = v!=null && Math.abs(v) >= 99999.9995;
    let txt=(v==null||Math.abs(v)<0.0005)?'':(big?(v<0?'-':'')+cnt(v):fmt(v));
    let tip=(BADGE_TITLE_FLOW[k]||'')+(big?' — точно: '+fmt(v):'');
    const u = unp[k];
    if(u!=null){
      if(!txt){ txt = fmt(u); cls += ' unp'; tip = LC_UNP_TIP; }
      else tip += ' · ещё '+fmt(u)+' '+LC_UNP_TIP;
    }
    return '<i class="'+cls+'" title="'+esc(tip)+'">'+txt+'</i>';
  }).join('')+'</span>';
}
/* ================================================================
   ПОСТРОЧНЫЕ ДВИЖЕНИЯ (§5 ТЗ)
   Одна строка регистра 1С = одна строка здесь. Никаких агрегатов:
   документ → номенклатура → количество → откуда → куда. Так цифры
   сверяются с 1С один в один, а цепочка лота читается подряд.
   ================================================================ */
/* регистр выручки загружен? (META.revenue пишет расчёт, 14.09.2026) */
const HAS_RUB = !!(META.revenue && META.revenue.linked > 0.5);
const MOVES  = DATA.moves || [];
const MC     = (()=>{const o={}; (META.move_cols||[]).forEach((c,i)=>o[c]=i); return o;})();
const MFLOWS = META.move_flows || [];
const MPHRASE= META.move_phrase || {};
const MSHORT = META.move_short || {};
const NM     = DATA.nm || {};
const mfl = r => MFLOWS[r[MC.flow]];
const mst = (r,c) => str(r[MC[c]]);
/* ---------- ЛОТ: полный ключ вместо голого номера ----------
   ⚠️ Под ОДНИМ номером в 1С лежат РАЗНЫЕ документы: у РУ00-000481 их четыре —
   «Приобретение товаров и услуг» от 04.02.2025 (4 879,640 т трубы на Головных
   сооружениях), два «Производство без заказа» (22.05.2024, Лом 3А на Усинске и
   17.02.2025, станция управления на Майском) и «Сборка (разборка)» от
   06.02.2025 (Труба 325 на Ухте). В расчёте они РАЗДЕЛЕНЫ (ключ = тип + номер
   + дата), а в дереве группировались по номеру и склеивались в один лот с
   чужой номенклатурой и чужими складами — ровно об это и споткнулись.
   Группируем по `lotk`, подписываем так же, как пишет сама 1С.
   Колонка `lot` остаётся НОМЕРОМ: на ней держится ключ overrides.json. */
const LOT_TYPES = META.lot_types || {};
const mlotk = r => mst(r,'lotk') || mst(r,'lot');
/** «прио:РУ00-000481@2025-02-04» -> «Приобретение товаров и услуг РУ00-000481 от 04.02.2025» */
function lotLabel(k){
  if(!k) return '';
  const i = k.indexOf(':'), t = i<0 ? '' : k.slice(0,i);
  let rest = i<0 ? k : k.slice(i+1);
  const j = rest.indexOf('@'), dt = j<0 ? '' : rest.slice(j+1);
  if(j>=0) rest = rest.slice(0,j);
  return (LOT_TYPES[t] ? LOT_TYPES[t]+' ' : '') + rest + (dt ? ' от '+ruDate(dt) : '');
}
/* формулировка строится из того же шаблона, что и в Excel (META.move_phrase) */
const WPHRASE = META.work_phrase || {};
/* ⚠️ «Производство без заказа» — не только резка. С выгрузки 11.08.2026 у него
   есть ВИД РАБОТ (колонка «РегистраторВидРаботОбщий»): резка, сортировка,
   разборка, производство. Раньше всё это читалось как «в резку / из резки», и
   по строке было не понять, порезали трубу или отобрали из неё позицию.
   Если вид работ известен — формулировка своя, иначе прежняя. */
function movePhrase(r){
  const fl = mfl(r), wk = mst(r,'wk');
  const t = (wk && WPHRASE[fl+'|'+wk]) || MPHRASE[fl];
  if(!t) return fl;
  return t.replace('{from}', mst(r,'from')||'—').replace('{to}', mst(r,'to')||'—');
}
/* Короткая подпись потока с учётом вида работ: чип не должен говорить «из
   резки», когда рядом написано «из сортировки». */
const WFLOW = {
  /* ⚠️ «резка» здесь ОБЯЗАТЕЛЬНА. Колонка называется «в производство», и без
     своей записи резка получала бы подпись колонки — вид работ из 1С исчезал
     бы из строки, хотя ради него колонку и завели. */
  'переработка_забрали': {'сортировка':'в сортировку','разборка':'в разборку',
                          'резка':'в резку','производство':'в производство'},
  'переработка_вернули': {'сортировка':'из сортировки','разборка':'из разборки',
                          'резка':'из резки','производство':'из производства'},
};
const workShort = (fl, wk) => (WFLOW[fl] || {})[wk] || '';
/* внешний приход | внутри компании | внешнее выбытие — раскраска потока */
const FLOW_KIND = {'куплено':'in','излишки':'in','ввод_остатков':'in',
  'возврат_от_клиента':'in','корректировки':'in',
  'уехало':'mv','приехало':'mv','переработка_забрали':'mv','переработка_вернули':'mv',
  'пересортица':'mv','продано':'out','возврат':'out','списано':'out',
  'списано_на_затраты':'out','внутренний_оборот':'out'};
function flowChip(fl, wk){
  const w = workShort(fl, wk);
  return '<span class="fchip f-'+(FLOW_KIND[fl]||'mv')+'" title="'
    +esc((BADGE_TITLE_FLOW[fl]||fl) + (wk ? '\nВид работ из 1С: '+wk
        + '. Заполняется у документов «Производство без заказа».' : ''))+'">'
    +esc(w || MSHORT[fl] || fl.replace(/_/g,' '))+'</span>';
}

/* Индексы по 294 тыс. строк строим лениво и один раз. */
let _MIX = null;
function movesIndex(){
  if(_MIX) return _MIX;
  const byVar=new Map(), bySer=new Map(), byLot=new Map(), byDoc=new Map();
  const push=(m,k,r)=>{ let a=m.get(k); if(!a){a=[]; m.set(k,a);} a.push(r); };
  for(const r of MOVES){
    push(byVar, mst(r,'variant'), r);
    push(bySer, mst(r,'series'),  r);
    push(byLot, mlotk(r),        r);
    push(byDoc, mst(r,'doc'),     r);
  }
  _MIX = {byVar, bySer, byLot, byDoc};
  return _MIX;
}
const byDate = rows => rows.slice().sort((a,b)=>{
  const x=mst(a,'dt'), y=mst(b,'dt'); return x<y?-1:x>y?1:0; });

/* ================================================================
   ЧТО ВЫШЛО ПО ДОКУМЕНТУ И ЧТО БЫЛО ДАЛЬШЕ (наведение на строку)
   ================================================================
   Экономист видел «в производство ушло 350» и не видел, чем это кончилось: вернулось
   ли из производства, каким документом оформили выпуск и куда металл уехал потом.
   Собиралось это руками по вкладке «Движения» — на один спорный документ
   уходил час. Теперь тот же путь собирается при наведении: пара документа
   (ушло → пришло), расхождение цветом и дальнейшие документы по лоту.

   ⚠️ Пара считается по ключу «номер документа + ДАТА». Номера в 1С повторяются
   из года в год (РУ00-006027 есть и в 2022, и в 2025), и по одному номеру
   склеились бы чужие движения: у 34 тыс. документов переработки сошлось бы
   что угодно. По ключу с датой из 34 820 документов резки 97 % сходятся
   в ноль — оставшаяся тысяча и есть находка, ради которой всё это.
   Расхождение показываем ПО ДОКУМЕНТУ ЦЕЛИКОМ, а не по одной серии: внутри
   документа сырьё одной серии превращается в выпуск, унаследовавший её,
   и разрез по серии дал бы 3 978 ложных «выпуск без сырья». */
const PAIR_OUT = {'переработка_забрали':'переработка_вернули', 'уехало':'приехало'};
const PAIR_IN  = {'переработка_вернули':'переработка_забрали', 'приехало':'уехало'};
const GAP_EPS  = 0.0005;   /* ниже этого расхождения нет: столько же знаков в выводе */
const GAP_WARN = 0.02;     /* до 2 % — жёлтый (угар, обрезь), выше — красный */
const dkey = r => mst(r,'doc')+SEP+mst(r,'dt');
let _DPX = null;
function docPairIndex(){
  if(_DPX) return _DPX;
  const m = new Map();
  for(const r of MOVES){
    const fl = mfl(r);
    if(!PAIR_OUT[fl] && !PAIR_IN[fl]) continue;
    const k = dkey(r);
    let a = m.get(k); if(!a){ a = []; m.set(k, a); }
    a.push(r);
  }
  _DPX = m; return m;
}
/** Пара документа для строки: сколько по нему УШЛО и сколько ПРИШЛО обратно.
    Возвращает null, если поток не парный (купили, продали, списали). */
function docPair(r){
  const fl0 = mfl(r);
  const inFl = PAIR_IN[fl0] || fl0;
  if(!PAIR_OUT[inFl]) return null;
  const outFl = PAIR_OUT[inFl];
  const rows = docPairIndex().get(dkey(r)) || [];
  const src = rows.filter(x=>mfl(x)===inFl), dst = rows.filter(x=>mfl(x)===outFl);
  if(!src.length && !dst.length) return null;
  const sT = a=>sum(a,x=>x[MC.tonnes]), sQ = a=>sum(a,x=>x[MC.mq]);
  const it = sT(src), ot = sT(dst), iq = sQ(src), oq = sQ(dst);
  /* Каждую сторону меряем в ЕЁ единице: тоннажную — в тоннах, штучную — в
     количестве 1С. Общий разрез отчёта здесь не годится — в тоннах штучное
     оборудование даёт ноль, и «в производство ушло 4,9 т, вернулось 0» читалось бы
     как пропажа, хотя вернулось 8 противовесов. */
  const tIn = Math.abs(it)>GAP_EPS, tOut = Math.abs(ot)>GAP_EPS;
  const qIn = Math.abs(iq)>GAP_EPS, qOut = Math.abs(oq)>GAP_EPS;
  const uOf = a => mst(a[0]||r,'mc') || unitLabel();
  /* вес показываем так же, как везде: тонны или килограммы. Порог «расхождения
     нет» едет вместе с величиной, иначе в килограммах полграмма стало бы
     расхождением у каждого второго документа. */
  const iv = tIn ? tShow(it) : iq, ov = tOut ? tShow(ot) : oq;
  const iu = tIn ? wLabel() : uOf(src), ou = tOut ? wLabel() : uOf(dst);
  const eps = (tIn || tOut) ? GAP_EPS * wScale() : GAP_EPS;
  let kind = 'pair', st = 'ok', d = 0, rel = 0;
  if(!tIn && !qIn){
    /* пары нет вовсе: приход без отправки — вопрос, выпуск без сырья — обычная
       разборка (её отдельно считает и Python, см. META.birth_in) */
    kind = 'noin'; st = (inFl==='уехало') ? 'warn' : 'mix';
  } else if(!tOut && !qOut){
    kind = 'noout'; st = 'bad'; d = -iv; rel = -1;
  } else if(iu !== ou){
    /* единица сменилась: разобрали ёмкость (штуки → тонны) или наоборот.
       Это не потеря и не находка, а смена номенклатуры — красным не красим. */
    kind = 'mix'; st = 'mix';
  } else {
    d = ov - iv;
    rel = Math.abs(iv)>eps ? d/iv : (Math.abs(d)>eps ? 1 : 0);
    st = Math.abs(d)<=eps ? 'ok' : (Math.abs(rel)<=GAP_WARN ? 'warn' : 'bad');
  }
  return {inFl, outFl, src, dst, iv, ov, iu, ou, d, rel, unit: iu, kind, st,
          docs: new Set(dst.map(x=>mst(x,'doc'))).size};
}
/* доля в процентах — с запятой, как все остальные числа в отчёте */
/* доля — один знак: три здесь только шумят («0,600 %») */
const pct = x => (Math.abs(x)*100).toFixed(1).replace('.',',')+' %';
/* «2 документа», а не «2 документов»: подсказку читают глазами, а не парсером */
const plural = (n,a,b,c)=>{ const n10=n%10, n100=n%100;
  return (n10===1&&n100!==11) ? a
       : (n10>=2&&n10<=4&&(n100<10||n100>=20)) ? b : c; };
const nOf = (n,a,b,c)=> n+' '+plural(n,a,b,c);
const vOf = (v,u) => fmt(v)+' '+u;                      /* число с единицей */
/** Значок расхождения в самой строке: «−2,000 т · −0,6 %». Сошлось — молчим,
    иначе значки стоят у 92 % строк и перестают что-либо значить. */
function gapChip(p){
  if(!p || p.st==='ok') return '';
  const io = MSHORT[p.inFl]||p.inFl, oo = MSHORT[p.outFl]||p.outFl;
  let txt, ttl;
  if(p.kind==='mix'){
    txt = vOf(p.iv,p.iu)+' → '+vOf(p.ov,p.ou);
    ttl = io+' '+vOf(p.iv,p.iu)+', '+oo+' '+vOf(p.ov,p.ou)+'. Единица сменилась: '
        + 'разобрали штучное оборудование в тоннажный лом (или наоборот). Это не '
        + 'потеря и не находка — сверять такие числа между собой нельзя.';
  } else if(p.kind==='noin'){
    txt = 'без пары';
    ttl = 'В этом документе есть только «'+oo+'», парного «'+io+'» нет. '
        + (p.inFl==='уехало'
           ? 'Приход без отправки: отправку оформили другим документом или датой.'
           : 'Выпуск без сырья в документе — обычно разборка или ввод готовой продукции.');
  } else {
    const sgn = p.d>0 ? '+' : '−';
    txt = sgn+vOf(Math.abs(p.d),p.unit)
        + (p.kind==='noout' ? ' · ' + (p.inFl==='уехало' ? 'не приехало' : 'не вернулось')
           : ' · '+sgn+pct(p.rel));
    ttl = io+' '+fmt3(p.iv)+' '+p.iu+' → '+oo+' '+fmt3(p.ov)+' '+p.ou+'. '
        + (p.d<0 ? 'Не вернулось '+fmt3(-p.d)+' '+p.unit+': угар и обрезь при резке, '
                 + 'либо выпуск оформлен другим документом.'
                 : 'Вернулось больше, чем ушло: часть выпуска оформлена без сырья '
                 + 'в этом документе.');
  }
  return '<span class="dgap g-'+p.st+'" title="'+esc(ttl)+'">'+esc(txt)+'</span>';
}
/* ---------- карточка при наведении ---------- */
function fateLine(dt, doc, fl, val, unit, to, cls){
  return '<div class="fl'+(cls?' '+cls:'')+'"><span class="dt">'+esc(ruDate(dt))+'</span>'
    + '<span class="rn">'+esc(doc)+'</span>'+flowChip(fl)
    + '<span class="q">'+fmt(val)+' <i>'+esc(unit)+'</i></span>'
    + '<span class="to">'+esc(to||'')+'</span></div>';
}
/** Судьба лота: что было до этой строки и — главное — что сделали после неё.
    compact — для второго лота в карточке (у сырья это лот-источник): его
    история интересна одной строкой, а не сорока, иначе карточка не читается. */
function fateLot(lot, r, compact){
  const all = byDate(movesIndex().byLot.get(lot) || []);
  if(!all.length) return '';
  const me = dkey(r), dt = mst(r,'dt');
  const before = all.filter(x=> mst(x,'dt') < dt);
  const after  = all.filter(x=> mst(x,'dt') > dt
                            || (mst(x,'dt')===dt && dkey(x)!==me));
  /* Итог по лоту — ОТДЕЛЬНОЙ строкой на каждую единицу. Складывать тонны со
     штуками нельзя (в отчёте это правило сквозное), а показывать «0» у штучного
     лота — врать: 8 противовесов в тоннах дают ноль. */
  const byU = new Map();
  all.forEach(x=>{ const f = mfl(x), t = Math.abs(x[MC.tonnes])>GAP_EPS;
    const u = t ? wLabel() : (mst(x,'mc')||unitLabel());
    let o = byU.get(u); if(!o){ o = {}; byU.set(u, o); }
    o[f] = (o[f]||0) + (t ? tShow(x[MC.tonnes]) : x[MC.mq]); });
  const totLine = u => {
    const v = byU.get(u); v['остаток'] = balanceOf(v);
    const parts = [];
    LC_COLS.forEach(([k,lbl])=>{ if(Math.abs(v[k]||0)>GAP_EPS)
      parts.push(esc(String(lbl).replace(/^[+−-]\s*/,''))+' <b>'+fmt(v[k])+'</b>'); });
    return parts.length ? '<div class="fate-tot">по лоту, '+esc(u)+': '
      + parts.join(' · ')+'</div>' : '';
  };
  /* Документы одного дня и потока — одной строкой: выпуск из 24 строк иначе
     выдавливает из карточки то, ради чего её открыли. */
  const pack = (rows, cls, limit, tail) => {
    const g = groupBy(rows, x=> mst(x,'dt')+SEP+mst(x,'doc')+SEP+mfl(x));
    const keys = [...g.keys()];
    /* «до этого» показываем ПОСЛЕДНИЕ документы перед строкой, а не первые:
       важно то, что случилось непосредственно перед ней */
    const take = cls==='past' ? keys.slice(-limit) : keys.slice(0, limit);
    const out = take.map(k=>{
      const rs = g.get(k), x0 = rs[0];
      const v = sum(rs, x=>x[MC.tonnes]), useT = Math.abs(v)>GAP_EPS;
      const to = mst(x0,'to') || mst(x0,'from');
      return fateLine(mst(x0,'dt'), mst(x0,'doc'), mfl(x0),
                      useT ? tShow(v) : sum(rs, x=>x[MC.mq]),
                      useT ? wLabel() : (mst(x0,'mc')||unitLabel()), to, cls);
    }).join('');
    const hid = keys.length - take.length;
    return (cls==='past' && hid
        ? '<div class="hint">до этого — ещё '
          + nOf(hid,'документ','документа','документов')+' по лоту</div>' : '')
      + out
      + (cls!=='past' && hid
        ? '<div class="hint">…и ещё '+nOf(hid,'документ','документа','документов')
          + ' по этому лоту</div>' : '');
  };
  const meT = Math.abs(r[MC.tonnes])>GAP_EPS;
  let h = '<div class="wh1">Что было дальше · лот '+esc(lotLabel(lot)||lot)
        + (compact ? ' <span class="tag">лот-источник</span>' : '')+'</div>';
  if(before.length && !compact) h += pack(before, 'past', 2);
  h += '<div class="fl me"><span class="dt">'+esc(ruDate(dt))+'</span>'
     + '<span class="rn">'+esc(mst(r,'doc'))+'</span>'+flowChip(mfl(r), mst(r,'wk'))
     + '<span class="q">'+fmt(meT ? tShow(r[MC.tonnes]) : r[MC.mq])
     + ' <i>'+esc(meT ? wLabel() : (mst(r,'mc')||unitLabel()))+'</i></span>'
     + '<span class="to">эта строка</span></div>';
  h += after.length ? pack(after, '', compact ? 3 : 6)
     : '<div class="hint">После этого документа движений по лоту нет — металл лежит '
       + 'на складе либо ушёл под другим лотом.</div>';
  /* тонны первой строкой, остальные единицы за ней */
  [...byU.keys()].sort((a,b)=> a===wLabel() ? -1 : b===wLabel() ? 1 : a.localeCompare(b,'ru'))
    .forEach(u=>{ h += totLine(u); });
  return h;
}
/** Состав одной стороны документа: номенклатура · сколько · откуда/куда · лот.
    Заголовок пишем словами документа: «в производство» → «что забрали»,
    «из производства» → «что получили», перемещение → «что уехало / что приехало».
    ⚠️ Раньше стояло «Что порезали»: за парой стоит не только резка, но и
    разборка (20 151 строка) и прочее производство — заголовок врал. */
const SIDE_TITLE = {
  'переработка_забрали': 'Что забрали в производство',
  'переработка_вернули': 'Что получили',
  'уехало':              'Что уехало',
  'приехало':            'Что приехало',
};
function fateItems(rows, flow, _lab, dir){
  if(!rows || !rows.length) return '';
  const g = groupBy(rows, x=> mst(x,'code')+SEP+mst(x,dir==='from'?'from':'to')+SEP+mlotk(x));
  const keys = [...g.keys()].slice(0, 5);
  const arrow = dir==='from' ? '←' : '→';
  return '<div class="wh1">'+esc(SIDE_TITLE[flow] || (MSHORT[flow]||flow))+'</div>'
    + keys.map(k=>{ const rs=g.get(k), x0=rs[0];
        const v = sum(rs,x=>x[MC.tonnes]), q = sum(rs,x=>x[MC.mq]);
        const useT = Math.abs(v)>GAP_EPS;
        const place = mst(x0, dir==='from' ? 'from' : 'to');
        return '<div class="fl"><span class="nmn">'
          + esc(NM[mst(x0,'code')]||mst(x0,'code'))+'</span>'
          + '<span class="q">'+fmt(useT?tShow(v):q)+' <i>'
          + esc(useT?wLabel():(mst(x0,'mc')||'ед.'))+'</i></span>'
          + '<span class="to">'+arrow+' '+esc(place)
          + (mlotk(x0) ? ' · лот '+esc(lotLabel(mlotk(x0))) : '')+'</span></div>';
      }).join('')
    + (g.size>5 ? '<div class="hint">…ещё '
        + nOf(g.size-5,'позиция','позиции','позиций')+'</div>' : '');
}
/** Всё вместе: пара документа, что получилось и судьба лота. */
function fateCard(r, p){
  const box = el('div','fate');
  const code = mst(r,'code');
  let h = '<div class="fate-h"><span class="dt">'+esc(ruDate(mst(r,'dt')))+'</span>'
    + '<span class="rn">'+esc(mst(r,'doc'))+'</span>'+flowChip(mfl(r), mst(r,'wk'))
    + '<span class="nmn">'+esc(NM[code]||code)+'</span></div>';
  if(p){
    const io = MSHORT[p.inFl]||p.inFl, oo = MSHORT[p.outFl]||p.outFl;
    const num = (v,u) => fmt3(v)+' <i>'+esc(u)+'</i>';
    const nRow = n => nOf(n,'строка','строки','строк');
    h += '<div class="wh1">Что стало по этому документу</div>'
      + '<div class="fp"><span>'+esc(io)+'</span><b>'+num(p.iv,p.iu)+'</b>'
        + '<span class="hint">'+nRow(p.src.length)+'</span></div>'
      + '<div class="fp"><span>'+esc(oo)+'</span><b>'+num(p.ov,p.ou)+'</b>'
        + '<span class="hint">'+nRow(p.dst.length)
        + (p.docs>1 ? ' · '+nOf(p.docs,'документ','документа','документов') : '')
        + '</span></div>'
      + '<div class="fp gap g-'+p.st+'"><span>расхождение</span><b>'
        + (p.kind==='mix' ? 'единица сменилась'
           : p.kind==='noin' ? 'пары в документе нет'
           /* «−0,000 т · −0,0 %» — это ноль, отражённый в трёх знаках вывода;
              пишем словом, иначе сошедшийся документ выглядит подозрительным */
           : p.st==='ok' ? 'сошлось в ноль'
           : (p.d>0?'+':'−')+fmt3(Math.abs(p.d))+' <i>'+esc(p.unit)+'</i>'
             + (Math.abs(p.iv)>GAP_EPS ? ' · '+(p.d>0?'+':'−')+pct(p.rel) : ''))
        + '</b></div>'
      + '<div class="hint">'+esc(fateWhy(p))+'</div>';
    /* ⚠️ ПОКАЗЫВАЕМ ОБЕ СТОРОНЫ: что порезали и во что превратили. Раньше состав
       выпуска был виден только когда наводишь на строку сырья, а с самого выпуска
       не было видно, из чего он сделан — приходилось искать документ руками. */
    h += fateItems(p.src, p.inFl,  'из чего',  'from')
       + fateItems(p.dst, p.outFl, 'во что',   'to');
  }
  /* судьба металла: у сырья — лот выпуска (новый металл), иначе свой лот */
  const lots = [];
  if(p && mfl(r)===p.inFl)
    p.dst.forEach(x=>{ const l=mlotk(x); if(l && lots.indexOf(l)<0) lots.push(l); });
  const own = mlotk(r);
  if(own && lots.indexOf(own)<0) lots.push(own);
  lots.slice(0,2).forEach((l,i)=>{ h += fateLot(l, r, i>0); });
  if(!lots.length)
    h += '<div class="hint">Лота у этой строки в выгрузке нет — проследить путь '
       + 'дальше не по чему.</div>';
  box.innerHTML = h;
  return box;
}
function fateWhy(p){
  const io = MSHORT[p.inFl]||p.inFl;
  if(p.kind==='mix')
    return 'Единица сменилась: с одной стороны документа тонны, с другой — штучная '
         + 'номенклатура. Разобрали оборудование в лом или, наоборот, собрали из '
         + 'лома изделие. Сверять эти числа между собой нельзя, и расхождения тут нет.';
  if(p.kind==='noin')
    return p.inFl==='уехало'
      ? 'Приход без отправки: «уехало» по этому документу нет — отправку оформили '
      + 'другим документом или другой датой.'
      : 'Выпуск без сырья в документе: «'+io+'» здесь нет. Обычно так выглядит '
      + 'разборка или ввод готовой продукции.';
  /* у перемещения и у передела разная физика: там «приехало на другой склад»,
     здесь «вернулось из производства другой номенклатурой» — и объяснения разные */
  const move = p.inFl==='уехало';
  if(p.st==='ok')
    return move
      ? 'Сошлось: сколько уехало, столько и приехало — металл только сменил склад.'
      : 'Сошлось: сколько ушло в производство, столько и вернулось — металл сменил '
      + 'номенклатуру и лот.';
  if(p.kind==='noout')
    return 'Обратной стороны у документа нет вовсе: ушло '+fmt3(p.iv)+' '+p.iu
         + (move ? ', приехало ноль. Груз ещё в пути либо приход оформлен другим '
                 + 'документом или датой.'
                 : ', вернулось ноль. Выпуск оформлен другим документом или датой.');
  if(p.d < 0)
    return (move ? 'Не приехало ' : 'Не вернулось ')+fmt3(-p.d)+' '+p.unit+' из '
         + fmt3(p.iv)+'. '
         + (move ? 'Обычно это груз в пути или приход, оформленный другим документом.'
                 : 'При резке это угар и обрезь; если доля велика — выпуск оформлен '
                 + 'другим документом или датой.');
  return move
    ? 'Приехало на '+fmt3(p.d)+' '+p.unit+' больше, чем уехало: часть прихода '
    + 'пришла по этому документу без своей отправки.'
    : 'Вернулось на '+fmt3(p.d)+' '+p.unit+' больше, чем ушло: часть выпуска '
    + 'оформлена без сырья в этом документе.';
}
/* ---------- ФОКУС НА ДОКУМЕНТЕ ----------
   Один документ 1С встречается в отчёте много раз: строкой в дереве под каждой
   номенклатурой, строкой в цепочке лота, строкой в таблице движений, продолжением
   в карточке. Сверяя спорный случай, экономист искал эти вхождения глазами.
   Клик по номеру включает подсветку: все строки с этим номером помечаются на
   ЛЮБОЙ вкладке, пока фокус не снят. Это выбор пользователя, а не статус, поэтому
   цвет — «взаимодействие» (--accent), а не шкала ok/warn/bad. */
const DOCF_KEY = 'metoptorg.docfocus';
let DOC_FOCUS = (()=>{ try{ return localStorage.getItem(DOCF_KEY)||''; }catch(e){ return ''; } })();
function setDocFocus(v){
  DOC_FOCUS = (v==null ? '' : String(v).trim());
  try{ localStorage.setItem(DOCF_KEY, DOC_FOCUS); }catch(e){}
  applyDocFocus();
}
/** Пометить строки с этим номером и показать плашку с кнопками. */
function applyDocFocus(){
  docFocusBind();
  document.querySelectorAll('.dochit').forEach(e=>e.classList.remove('dochit'));
  const bar = $('#docbar');
  if(bar) bar.remove();
  if(!DOC_FOCUS) return;
  let n = 0;
  document.querySelectorAll('.rn').forEach(e=>{
    if(e.textContent.trim() !== DOC_FOCUS) return;
    const row = e.closest('.dl, .cline, .fl, tr, .doc');
    if(row){ row.classList.add('dochit'); n++; }
  });
  const b = el('div','docbar');
  b.id = 'docbar';
  b.innerHTML = '<b>документ '+esc(DOC_FOCUS)+'</b>'
    + '<span class="hint">'+(n ? 'подсвечено '+nOf(n,'строка','строки','строк')+' на этой вкладке'
                               : 'на этой вкладке его нет')+'</span>'
    + '<button class="btn3" id="docall">все движения</button>'
    + '<button class="btn3" id="docoff">снять</button>';
  document.body.appendChild(b);
  $('#docall').addEventListener('click', ()=>{
    /* показать документ целиком: вкладка «Движения», поиск по его номеру */
    setBpFocus(DOC_FOCUS);
    if(typeof render === 'function') render('moves');
  });
  $('#docoff').addEventListener('click', ()=> setDocFocus(''));
}
/* Клик по номеру документа где угодно — включить/снять фокус.
   ⚠️ Слушатель вешаем ЛЕНИВО, а не на верхнем уровне файла: tabs.js целиком
   прогоняется в node-обвязке tools/check_1c_xlsx.py, где document — заглушка
   без addEventListener (на этом уже спотыкались с карточкой документа). */
let _DOCF_BOUND = false;
function docFocusBind(){
  if(_DOCF_BOUND) return;
  _DOCF_BOUND = true;
  document.addEventListener('click', ev=>{
    const rn = ev.target.closest('.rn');
    if(!rn || !rn.textContent.trim()) return;
    const num = rn.textContent.trim();
    ev.stopPropagation(); ev.preventDefault();
    setDocFocus(DOC_FOCUS === num ? '' : num);
  });
}

/* ---------- поповер: показываем по наведению, держим по клику ----------
   Карточка одна на страницу: строк документов на экране бывает несколько сотен,
   и держать по узлу на каждую нельзя. */
let _FT = {box:null, host:null, tmr:0, pin:false};
function fateHide(force){
  if(_FT.pin && !force) return;
  clearTimeout(_FT.tmr);
  if(_FT.box){ _FT.box.remove(); _FT.box = null; }
  if(_FT.host){ _FT.host.classList.remove('fate-on'); _FT.host = null; }
  _FT.pin = false;
}
function fateShow(node, r, p, pin){
  fateHide(true);
  const box = fateCard(r, p);
  box.classList.add('fatepop');
  if(pin) box.classList.add('pin');
  document.body.appendChild(box);
  const rc = node.getBoundingClientRect(), bw = box.offsetWidth, bh = box.offsetHeight;
  let x = Math.min(rc.left + 24, window.innerWidth - bw - 12);
  let y = rc.bottom + 6;
  if(y + bh > window.innerHeight - 8) y = Math.max(8, rc.top - bh - 6);
  box.style.left = Math.max(8, x)+'px';
  box.style.top  = y+'px';
  box.addEventListener('mouseenter', ()=> clearTimeout(_FT.tmr));
  box.addEventListener('mouseleave', ()=>{ if(!_FT.pin) fateHide(); });
  node.classList.add('fate-on');
  _FT.box = box; _FT.host = node; _FT.pin = !!pin;
}
/* Общие слушатели вешаем ЛЕНИВО, при первой строке документа, а не на верхнем
   уровне файла: tabs.js целиком прогоняется в node-обвязке tools/check_1c_xlsx.py
   (там сборщик Excel), а в ней document — заглушка без addEventListener. */
let _FT_BOUND = false;
function fateBind(){
  if(_FT_BOUND) return;
  _FT_BOUND = true;
  document.addEventListener('click', ev=>{
    if(_FT.pin && !ev.target.closest('.fatepop') && !ev.target.closest('.hasfate'))
      fateHide(true);
  });
  /* карточка стоит на месте (position:fixed), а строка под ней уезжает —
     при прокрутке закрываем, иначе подсказка врёт про чужую строку */
  window.addEventListener('scroll', ()=> fateHide(true), true);
}
/** Вешаем поведение на строку документа. Пара считается сразу — её значок
    стоит в самой строке, — а карточка собирается только при наведении. */
function attachFate(node, r, p){
  if(p && p.st!=='ok') node.classList.add('gap-'+p.st);
  if(!p && !mlotk(r)) return;         /* проследить нечего — и подсказки нет */
  fateBind();
  node.classList.add('hasfate');
  node.addEventListener('mouseenter', ()=>{
    clearTimeout(_FT.tmr);
    if(_FT.pin) return;
    _FT.tmr = setTimeout(()=> fateShow(node, r, p, false), 180);
  });
  node.addEventListener('mouseleave', ()=>{
    clearTimeout(_FT.tmr);
    if(!_FT.pin) _FT.tmr = setTimeout(()=> fateHide(), 160);
  });
  /* клик закрепляет карточку: на планшете наведения нет, да и читать длинную
     цепочку удобнее, когда она не исчезает от движения мыши */
  node.addEventListener('click', ev=>{
    if(ev.target.closest('.whysr')) return;
    ev.stopPropagation();
    if(_FT.pin && _FT.host===node){ fateHide(true); return; }
    fateShow(node, r, p, true);
  });
}

/** Таблица движений построчно. limit — сколько строк рисовать. */
function movesTable(rows, limit){
  limit = limit || 500;
  const shown = byDate(rows).slice(0, limit);
  const box = el('div','scroll');
  const t = el('table','mv');
  t.innerHTML = '<thead><tr>'
    + '<th title="Период регистра 1С — дата движения">дата</th>'
    + '<th title="Номер документа 1С, по которому прошло движение">документ</th>'
    + '<th title="Что произошло по этому документу 1С — формулировка потока">что произошло</th>'
    + '<th title="Номенклатура 1С: код и наименование">номенклатура</th>'
    + '<th class="num" title="Количество В ЕДИНИЦАХ ДОКУМЕНТА 1С: если там метры '
      + 'или килограммы, здесь они и стоят. В разрезах отчёта то же количество '
      + 'приведено к единице группы — километры и тонны.">кол-во</th>'
    + '<th class="num" title="То же в весе. шт / компл / л / м² в вес не переводятся — там 0">'
      + esc(wLabel())+'</th>'
    + '<th title="Откуда: склад-отправитель, поставщик, «резка» или инвентаризация">откуда</th>'
    + '<th title="Куда: склад-получатель (с типом подразделения), покупатель, затраты, «резка»">куда</th>'
    + '<th title="Лот-партия 1С. По нему цепочка движений читается подряд">лот</th>'
    + '<th title="Серия (полная строка 1С) и происхождение привязки">серия</th>'
    + '</tr></thead><tbody>'
    + shown.map((r,i)=>{
        const code=mst(r,'code');
        return '<tr data-i="'+i+'"><td class="nw">'+esc(mst(r,'dt'))+'</td>'
        +'<td class="rn">'+esc(mst(r,'doc'))+'</td>'
        +'<td>'+flowChip(mfl(r), mst(r,'wk'))+' <span class="muted">'+esc(movePhrase(r))+'</span>'
        +gapChip(docPair(r))+'</td>'
        +'<td><span class="code">'+esc(code)+'</span> '+esc(NM[code]||'')+'</td>'
        +'<td class="num">'+fmt3(r[MC.qty])+'</td>'
        +'<td class="num">'+(Math.abs(tShow(r[MC.tonnes]))>0.0005
             ? fmt3(tShow(r[MC.tonnes])) : '<span class="muted">0</span>')+'</td>'
        +'<td>'+esc(mst(r,'from'))+'</td><td>'+esc(mst(r,'to'))+'</td>'
        +'<td class="lot" title="'+esc(lotLabel(mlotk(r)))+'">'+esc(mst(r,'lot')||'—')+'</td>'
        +'<td>'+esc((mst(r,'variant')||'—').slice(0,54))+' '+badge(mst(r,'badge'))+'</td></tr>';
      }).join('')
    + '</tbody>';
  box.appendChild(t);
  /* строку таблицы связываем с движением через data-i: таблица собирается одной
     строкой HTML, ссылку на объект в ней не передать */
  t.querySelectorAll('tbody tr').forEach(tr=>{
    const r = shown[+tr.dataset.i];
    if(r) attachFate(tr, r, docPair(r));
  });
  if(rows.length>limit)
    box.appendChild(el('div','hint','Показаны первые '+cnt(limit)+' движений из '+cnt(rows.length)
      +' (по дате). Уточните фильтр, чтобы увидеть остальные.'));
  return box;
}

/** Цепочка по каждому лоту подряд: купили → уехало → приехало → продали. */
function lotChains(rows, limit){
  const wrap = el('div');
  const byLot = groupBy(rows, r=>mlotk(r)||'— без лота —');
  const lots = [...byLot.keys()].sort((a,b)=>{
    const A=byDate(byLot.get(a))[0], B=byDate(byLot.get(b))[0];
    return mst(B,'dt').localeCompare(mst(A,'dt'));      // свежие лоты сверху
  }).slice(0, limit||40);
  lots.forEach(lot=>{
    const rs = byDate(byLot.get(lot));
    const sold = sum(rs.filter(r=>mfl(r)==='продано'), r=>mVal(r));
    const box = el('div','chain');
    box.innerHTML = '<div class="chain-h"><b>лот '+esc(lotLabel(lot)||lot)+'</b>'
      + '<span class="tag">' + cnt(rs.length) + ' движений'
      + (Math.abs(sold)>0.0005 ? ' · продано ' + fmt3(sold) + ' ' + unitLabel() : '') + '</span></div>';
    rs.forEach(r=>{
      const code=mst(r,'code'), pr=docPair(r);
      const line = el('div','cline',
        '<span class="dt">'+esc(mst(r,'dt'))+'</span>'
        +'<span class="rn">'+esc(mst(r,'doc'))+'</span>'
        +flowChip(mfl(r), mst(r,'wk'))
        +'<span class="q">'+fmt3(r[MC.qty])+'</span>'
        +gapChip(pr)
        +'<span class="ph">'+esc(movePhrase(r))+'</span>'
        +'<span class="nmn">'+esc(NM[code]||code)+'</span>'
        +badge(mst(r,'badge')));
      box.appendChild(line);
      attachFate(line, r, pr);
    });
    wrap.appendChild(box);
  });
  if(byLot.size > lots.length)
    wrap.appendChild(el('div','hint','Показаны '+lots.length+' лотов из '+byLot.size+' (свежие сверху).'));
  return wrap;
}

/** Движения для набора позиций дерева: те же вариант серии + код. */
function movesForRows(prows){
  const IX = movesIndex();
  const vars = new Set(prows.map(r=>r.series_variant));
  const sers = new Set(prows.map(r=>r.series));
  const codes = new Set(prows.map(r=>r.code));
  const out = [], seen = new Set();
  const take = arr => { for(const r of (arr||[])) {
      if(!codes.has(mst(r,'code'))) continue;
      if(seen.has(r)) continue; seen.add(r); out.push(r); } };
  vars.forEach(v=>take(IX.byVar.get(v)));
  /* если у позиции серия восстановлена и варианта нет — берём по проекту */
  if(!out.length) sers.forEach(s=>take(IX.bySer.get(s)));
  return out;
}

/* ---------- KPI-карточки ----------
   ⚠️ ЕДИНИЦЫ НЕ СКЛАДЫВАЮТСЯ НИ ПРИ КАКОМ РЕЖИМЕ. В режиме «все единицы»
   («Серии насквозь») карточки показывали одно число «4 704 004,776 ед.» —
   тонны, штуки, литры и метры в одной сумме, и «88 % тоннажа» считались от неё
   же. Такого количества не существует: это разные величины. Теперь ведущее
   число — ВЕС (тонны или килограммы, как выбрано), а остальные единицы стоят
   отдельной строкой «также: 40 279,845 шт · 3 040 927,3 л». Ровно так же
   устроены подписи в дереве. */
function unitTotals(rows, pred){
  const o = {};
  for(const r of rows){
    if(pred && !pred(r)) continue;
    const w = Math.abs(r.tonnes) > 1e-9;
    /* ⚠️ Позиция с единицей «т», у которой тоннаж 0 (1С не дала коэффициента),
       в вес не идёт: её «количество» — те же тонны, и сложение задвоило бы вес.
       Так в режиме «все единицы» набегало лишних 0,574 т. */
    if(!w && (!r.mc || r.mc===MMAIN)) continue;
    const u = w ? wLabel() : r.mc;
    o[u] = (o[u]||0) + (w ? tShow(r.tonnes) : (r.mq||0));
  }
  return o;
}
/** «также: 40 279,845 шт · 12 471,082 м» — всё, кроме ведущей единицы.
    Больше четырёх единиц в подпись карточки не влезает — хвост сворачиваем. */
function otherUnitsLine(o, lead){
  const keys = Object.keys(o)
    .filter(u => u!==lead && Math.abs(o[u])>0.0005)
    .sort((a,b)=>Math.abs(o[b])-Math.abs(o[a]));
  if(!keys.length) return '';
  const shown = keys.slice(0,4).map(u => fmt(o[u])+' '+u);
  return 'также: '+shown.join(' · ')
    + (keys.length>4 ? ' · и ещё '+nOf(keys.length-4,'единица','единицы','единиц') : '');
}
function kpiCards(rows){
  /* карточки — в том же разрезе, что и дерево под ними: иначе «Бизнес-планов»
     показывал бы 837 при пустом дереве в разрезе штук */
  rows = rows.filter(iIn);
  const ALLU = curMeasure()===MEASURE_ALL;
  /* В режиме «все единицы» ведущее число — ВЕС, прочие единицы уходят в подпись.
     На время счёта фиксируем весовой разрез, иначе подпись, множитель и закупка
     разъезжаются: число получалось в килограммах, а подпись — «т». */
  const keepM0 = MEASURE_FORCED;
  if(ALLU) MEASURE_FORCED = MMAIN;
  const uAll  = ALLU ? unitTotals(rows) : null;
  const uReal = ALLU ? unitTotals(rows, REAL) : null;
  const t    = ALLU ? (uAll[wLabel()]||0)  : sum(rows,r=>iVal(r));
  const real = ALLU ? (uReal[wLabel()]||0) : sum(rows.filter(REAL),r=>iVal(r));
  const unitTxt = ALLU ? MMAIN : unitLabel();
  const c=el('div','cards');
  /* ⚠️ В карточке — ВСЕ бизнес-планы выборки, столько же, сколько строк в дереве
     под ней. Раньше здесь стоял фильтр REAL, и счётчик показывал 523 при 837
     строках: из него молча выпадали 313 псевдо-серий договорных площадок (это
     четверть тоннажа, 131 200,612 т) и «БЕЗ СЕРИИ». Подпись «= проектов из
     справочника серий» формально верна — площадок в справочнике нет, — но
     читается как «всего бизнес-планов», и цифра не сходилась с деревом.
     Качество привязки никуда не делось: оно ушло во вторую строку карточки. */
  const bp=new Set(rows.map(r=>r.series));                   // БП = проект
  const bpReal=new Set(rows.filter(REAL).map(r=>r.series));
  const bpSite=new Set(rows.filter(r=>String(r.series).startsWith('площадка:'))
                           .map(r=>r.series));
  const bpNone=new Set(rows.filter(r=>r.series==='БЕЗ СЕРИИ').map(r=>r.series));
  /* ⚠️ ПРОЕКТЫ БЕЗ ЕДИНОЙ ПРОДАЖИ. Вкладка «Весь оборот по сериям» подписана
     «включая непроданное», и дерево там достраивается проектами, которых нет в
     ITEMS: металл куплен и/или перевезён, но не продан (см. seriesTree). А
     карточка считалась ТОЛЬКО по ITEMS и показывала 936 при 1 079 строках в
     дереве — ровно те 143 проекта, что добавили 12.08. Подсказка карточки при
     этом обещает «столько же строк в дереве под карточкой», и обещание надо
     держать: цифру, которая не сходится с тем, что под ней, экономист считает
     ошибкой расчёта.
     Набор и фильтр по поиску — те же, что в дереве, иначе они опять разойдутся.
     ⚠️ ТОЛЬКО на «Весь оборот»: на «Продажах по сериям» таким планам не место. */
  let bpNoSale = 0;
  if(ALLU){
    /* ⚠️ НА ВРЕМЯ СЧЁТА ВЕРНУТЬ РАЗРЕЗ ВКЛАДКИ. Выше MEASURE_FORCED переключён
       на вес ради ведущего числа карточки, а flowAgg() кэширует агрегат ПО
       РАЗРЕЗУ: в весовом разрезе проектов без продаж 138, а дерево строится в
       разрезе «все единицы», где их 143. Без этой оговорки карточка снова
       разошлась бы с деревом — теперь уже на пять строк. */
    const keepM1 = MEASURE_FORCED; MEASURE_FORCED = keepM0;
    try {
      const A0 = flowAgg();
      const full = {};
      (DATA.flows || []).forEach(r=>{ full[r.series] = r.series_full || r.series_display; });
      const q0 = ($('#q') && $('#q').value || '').trim().toLowerCase();
      const qm0 = qMatcher(q0);
      for(const pk of A0.S.keys()){
        if(bp.has(pk)) continue;
        const nm = full[pk] || String(pk);
        if(qm0 && !qm0(String(nm).toLowerCase())) continue;
        bpNoSale++;
      }
    } finally { MEASURE_FORCED = keepM1; }
  }
  const bpParts=[];
  if(bpReal.size) bpParts.push(bpReal.size+' с серией 1С');
  if(bpSite.size) bpParts.push(bpSite.size+' — площадки');
  if(bpNone.size) bpParts.push('без серии');
  if(bpNoSale) bpParts.push(bpNoSale+' без продаж');
  const dg=new Set(rows.filter(r=>r.contract).map(r=>r.contract));
  /* ФАКТ ВЫРУЧКИ по выборке: рубли лежат на позициях (it.rub) и суммируются
     по тому же набору строк, что и тоннаж, — карточка и дерево не разойдутся */
  const rub = HAS_RUB ? sum(rows, r=>r.rub||0) : 0;
  /* «Куплено» в режиме «все единицы» тоже весовое: aggOf сложил бы разные
     единицы в одно число, а сравнивать продажу с закупкой можно только в одной */
  const bought = aggOf(rows,'s')['куплено'] || 0;
  /* считается закупка только по сериям, попавшим в выборку; полная закупка
     (включая серии, которые ещё не продавались) — в META.flow_totals */
  /* ⚠️ flow_totals лежат в ТОННАХ. Сравнивать их с закупкой выборки можно
     только в весовом разрезе: в штуках это вычло бы тонны из штук. */
  const boughtAll = tShow((META.flow_totals||{})['куплено']||0);
  const weight = ALLU || measBase(curMeasure())===MMAIN;
  const rest = weight ? boughtAll - bought : 0;
  const alsoSold = ALLU ? otherUnitsLine(uAll,  wLabel()) : '';
  const alsoReal = ALLU ? otherUnitsLine(uReal, wLabel()) : '';
  c.innerHTML =
    /* ведущая метрика вкладки — проданный тоннаж: именно его сверяют с 1С,
       остальные четыре к нему подчинены */
    '<div class="card lead"><div class="k">Продано всего</div><div class="v">'+fmt(t)+' <span class="unit">'+esc(unitTxt)+'</span></div>'
    +'<div class="s">'+(bought?(100*t/bought).toFixed(1)+' % от купленного':'только «Реализация товаров и услуг»')
      +(alsoSold?' · '+esc(alsoSold):'')+'</div></div>'
    +(HAS_RUB ? '<div class="card"><div class="k">Выручка без НДС</div><div class="v">'+money(rub)+' <span class="unit">₽</span></div>'
      +'<div class="s">'+(t>0.0005 && rub>0.5 ? money(rub/t)+' ₽ за '+esc(unitTxt)+' · ' : '')
      +'регистр «Выручка и себестоимость продаж» по '+esc(META.revenue.max_date||'—')+'</div></div>' : '')
    +'<div class="card"><div class="k">Куплено</div><div class="v">'+fmt(bought)+' <span class="unit">'+esc(unitTxt)+'</span></div>'
    +'<div class="s">по сериям из выборки'
      +(rest>0.001?'; ещё '+fmt(rest)+' '+esc(unitTxt)+' — серии без продаж':'')+'</div></div>'
    +'<div class="card"><div class="k">С реальной серией</div><div class="v">'+fmt(real)+' <span class="unit">'+esc(unitTxt)+'</span></div>'
    +'<div class="s">'+(t? (100*real/t).toFixed(2):0)+' % веса'
      +(alsoReal?' · '+esc(alsoReal):'')+'</div></div>'
    +'<div class="card" title="'+esc('Столько же строк в дереве под карточкой. '
      +'«Площадка» — бизнес-план, чей номер выведен из имени договорного склада '
      +'(«22ESP0397К БП 437»): настоящей серии в 1С ему не завели, но это '
      +'полноценный план, и тоннаж по нему считается наравне с остальными.')
      +(bpNoSale?' Плюс планы, по которым продаж ещё не было: металл куплен '
        +'и/или перевезён, но не продан — они есть в дереве этой вкладки '
        +'(«включая непроданное»), значит должны быть и здесь.':'')
      +'"><div class="k">Бизнес-планов</div><div class="v">'+(bp.size+bpNoSale)+'</div>'
    +'<div class="s">'+(bpParts.join(' · ') || '= проектов из справочника серий')
      +'</div></div>'
    +'<div class="card"><div class="k">Договоров</div><div class="v">'+dg.size+'</div>'
    /* ⚠️ Счётчики зависят от РАЗРЕЗА, и это надо говорить вслух: в тоннах видны
       только планы, где продавали тоннажное, в штуках — только штучное. Между
       тоннами и килограммами счётчики совпадают: это один и тот же вес. */
    +'<div class="s">позиций: '+cnt(rows.length)+' · '
      +(ALLU ? 'все единицы сразу'
             : 'только с продажами в «'+esc(unitLabel())+'»')+'</div></div>';
  if(ALLU) MEASURE_FORCED = keepM0;          /* режим вкладки вернули на место */
  return c;
}

/* ---------- панель фильтров (общая) ---------- */
/* ---------- что внутри бизнес-плана ----------
   bp_nm[проект] = {g: {группа аналитического учёта: тонны},
                    t: {номенклатура отчёта: тонны}}
   g — для фильтра: показываем БП, где такая номенклатура есть хоть в одной
   позиции (фильтруем БИЗНЕС-ПЛАНЫ, а не позиции — иначе от плана осталась бы
   одна строка и он перестал бы сходиться сам с собой).
   t — для значков рядом с названием: «Лом 5А», «Труба б/у НКТ 73». */
/* ⚠️ Внутри плана состав разложен ПО РАЗРЕЗАМ: bp_nm[проект][класс] = {g,t,d},
   а контрагенты (c) лежат рядом с классами — они от единицы не зависят.
   У плана, который весь в штуках, в разрезе тонн состава нет вовсе, и это
   правильно: показывать там чужие числа нельзя. Списки для выпадающих меню
   собираются по ВСЕМ разрезам, иначе при переключении единицы фильтр обнулялся
   бы вместе со списком. */
const BPNM = DATA.bp_nm || {};
/* состав плана лежит по КЛАССАМ данных, поэтому «кг» смотрит в тот же «т» */
const bpM = series => (BPNM[series] && BPNM[series][measBase(MEASURE)]) || null;
/* …а величина показывается так, как выбрано наверху: тонны или килограммы */
const bpVal  = v => (v || 0);
const bpUnit = () => (MEASURE===MEASURE_ALL ? 'ед.' : MEASURE);
const bpAllM = k => { const o=BPNM[k]||{}, out=[];
  for(const mk in o) if(mk!=='c' && o[mk] && typeof o[mk]==='object') out.push(o[mk]);
  return out; };
const BP_GROUPS = (()=>{ const s=new Set();
  for(const k in BPNM) bpAllM(k).forEach(m=>{ for(const g in (m.g||{})) s.add(g); });
  return [...s].sort((a,b)=>a.localeCompare(b,'ru')); })();
const bpHasGroup = (series, g) => { if(!g) return true;
  const m = bpM(series); return !!(m && m.g && m.g[g]); };
function groupSelect(id){
  return '<select id="'+id+'" title="Показать бизнес-планы, в которых есть '
    +'номенклатура этой группы аналитического учёта. Фильтруются планы целиком, '
    +'а не отдельные позиции."><option value="">группа учёта: все</option>'
    + BP_GROUPS.map(g=>'<option>'+esc(g)+'</option>').join('')+'</select>';
}

/* ---------- ФИЛЬТРЫ ПО ДИВИЗИОНУ И КОНТРАГЕНТУ ----------
   Та же логика, что у группы учёта, и это принципиально: фильтруется
   БИЗНЕС-ПЛАН ЦЕЛИКОМ. Если у плана есть хоть одна закупка или продажа в
   Западной Сибири — план показывается со всеми своими позициями, включая
   пермские. Иначе от плана осталась бы часть, и он перестал бы сходиться сам с
   собой: итог строки не равнялся бы сумме её разбора.

   Дивизион берётся из тех же потоков, что группа учёта (куплено + продано):
   это «где план закупали и откуда продавали», а не «через что металл проезжал».
   Контрагент — из справочника серий, все контрагенты проекта, а не только
   первый: у проекта бывает несколько серий с разными контрагентами. Это тот же
   контрагент, что показан в строке; ПОКУПАТЕЛИ документов сюда не входят — их
   тысяча с лишним, выпадающим списком их не выбрать, они ищутся строкой поиска. */
const NO_CA = '∅';                 /* «контрагент не указан» — не пустая строка,
                                           иначе значение неотличимо от «все» */
const BP_DIVS = (()=>{ const s=new Set();
  for(const k in BPNM) bpAllM(k).forEach(m=>{ for(const d in (m.d||{})) s.add(d); });
  return [...s].sort((a,b)=>a.localeCompare(b,'ru')); })();
const BP_CAS = (()=>{ const s=new Set();
  for(const k in BPNM) (BPNM[k].c||[]).forEach(c=>s.add(c));
  return [...s].sort((a,b)=>a.localeCompare(b,'ru')); })();
const bpHasDiv = (series, d) => { if(!d) return true;
  const m = bpM(series); return !!(m && m.d && m.d[d]); };
const bpHasCa = (series, c) => {
  if(!c) return true;
  const list = (BPNM[series] && BPNM[series].c) || [];
  return c===NO_CA ? !list.length : list.indexOf(c)>=0;
};
function divSelect(id){
  return '<select id="'+id+'" title="Показать бизнес-планы, у которых есть закупка '
    +'или продажа в этом дивизионе. Фильтруются планы целиком, а не отдельные '
    +'позиции: у плана остаются все его склады."><option value="">дивизион: все</option>'
    + BP_DIVS.map(d=>'<option>'+esc(d)+'</option>').join('')+'</select>';
}
function caSelect(id){
  return '<select id="'+id+'" title="Контрагент бизнес-плана из справочника серий — '
    +'тот же, что показан в строке. Покупатели из документов сюда не входят, их '
    +'больше тысячи; их ищет строка поиска."><option value="">контрагент: все</option>'
    + BP_CAS.map(c=>'<option>'+esc(c)+'</option>').join('')
    + '<option value="'+NO_CA+'">— контрагент не указан —</option></select>';
}
/* ---------- ПИКТОГРАММЫ СОСТАВА ПЛАНА ----------
   Текстовая строчка «Труба 720 (т) +1» серым 10 px в ряду таких же серых подписей
   не читалась: чтобы понять, что внутри плана, приходилось вчитываться в каждую
   строку. Категорию материала показываем ЗНАЧКОМ и ставим его первым в подписи —
   на одной вертикали во всех строках, поэтому список сканируется сверху вниз так
   же, как по цветному статусному ранту.

   Категория берётся из ГРУППЫ аналитического учёта (bp_nm[…].g), а не из
   номенклатуры отчёта (.t): групп 15, они заданы самой 1С и покрывают ВЕСЬ
   тоннаж плана, тогда как .t — только восемь верхних позиций. Точное название
   («Лом 5А», «Труба 720») остаётся текстовым значком рядом — значок отвечает на
   «что это вообще», текст на «что именно».

   Значки монохромные и различаются формой: цвет в этом интерфейсе занят статусом
   (--ok/--warn/--bad) и взаимодействием (--accent), покрасить ими материал —
   значит поставить в строке два разных смысла одного цвета.
   Символы лежат спрайтом в <defs> в начале <body> шаблона. */
const MAT = {
  nkt:   {n:'труба НКТ',             d:'Труба НКТ'},
  pipeL: {n:'труба ⌀ более 159 мм',  d:'Труба больших диаметров (более 159 мм)'},
  pipeS: {n:'труба ⌀ до 159 мм',     d:'Труба малых диаметров (до 159 мм)'},
  pipe:  {n:'труба и штанга',        d:'Труба и штанга'},
  rod:   {n:'штанга',                d:'Штанга'},
  scrap: {n:'лом чёрных металлов',   d:'Лом чёрных металлов'},
  color: {n:'лом цветных металлов',  d:'Лом цветных металлов (медь, алюминий, латунь)'},
  alloy: {n:'лом легированной стали',d:'Лом легированной стали'},
  cable: {n:'кабель',                d:'Кабель'},
  trafo: {n:'трансформаторы',        d:'Трансформаторы'},
  parts: {n:'комплектующие',         d:'Комплектующие с разбора'},
  dhno:  {n:'ДХНО',                  d:'ДХНО (отработанные катализаторы, масла)'},
  other: {n:'прочее',                d:'Прочая номенклатура'},
  none:  {n:'группа не заполнена',   d:'Группа учёта в 1С не заполнена'},
};
const MAT_ORDER = Object.keys(MAT);
/* группа учёта 1С -> значок. Набор групп задаёт 1С и он может пополниться со
   следующей выгрузкой, поэтому у незнакомой группы категория выводится по
   ключевым словам, а не теряется в «прочем». */
const MAT_EXACT = {
  'Труба НКТ':'nkt',
  'Труба больших диаметров (более 159 мм)':'pipeL',
  'Труба малых диаметров (до 159 мм)':'pipeS',
  'Труба и штанга':'pipe',
  'Штанга':'rod',
  'Лом черных металлов':'scrap',
  'Готовая продукция Лом черных металлов':'scrap',
  'Лом цветных металлов':'color',
  'Лом легированной стали':'alloy',
  'Кабель':'cable',
  'Трансформаторы':'trafo',
  'Комплектующие с разбора':'parts',
  'ДХНО':'dhno',
  'Основная номенклатурная группа':'other',
};
function matOf(g){
  if(MAT_EXACT[g]) return MAT_EXACT[g];
  const s = String(g||'').toLowerCase();
  if(!s || s.indexOf('без группы')>=0) return 'none';
  if(s.indexOf('нкт')>=0) return 'nkt';
  if(s.indexOf('кабел')>=0) return 'cable';
  if(s.indexOf('трансформ')>=0) return 'trafo';
  if(s.indexOf('штанг')>=0) return s.indexOf('труб')>=0 ? 'pipe' : 'rod';
  if(s.indexOf('труб')>=0) return 'pipe';
  if(s.indexOf('цветн')>=0) return 'color';
  if(s.indexOf('легиров')>=0 || s.indexOf('нержав')>=0) return 'alloy';
  if(s.indexOf('лом')>=0) return 'scrap';
  return 'other';
}
const matIcon = k => '<svg aria-hidden="true"><use href="#mi-'+k+'"></use></svg>';
/** Состав плана по категориям: [{k, t, pct}] по убыванию тоннажа. */
/* ---------- ОДИН МАТЕРИАЛ — ДВА ЗНАЧКА ----------
   ⚠️ «Нирезист» (Ni-Resist, высоконикелевый чугун) лежит в 1С СРАЗУ В ДВУХ
   группах учёта, и разница между ними — только скобки в имени номенклатуры:
     «Лом (нирезист)» 69,769 т -> группа «Лом легированной стали»  -> значок alloy
     «Лом нирезист»   53,476 т -> группа «Лом цветных металлов»    -> значок color
   Один и тот же материал получал разные значки: у БП 1834 — цветмет, у соседей
   — легированный. Переносим такой тоннаж туда, где ему место.

   ⚠️ ПРАВИЛО НАРОЧНО УЗКОЕ — «из группы X в группу Y, и только для этого имени».
   Соблазн выводить категорию из ИМЕНИ номенклатуры вообще проверен и отвергнут:
   он меняет ведущий значок у 63 БП и при этом ВРЁТ — «Барабан для кабеля» (это
   ДХНО, оснастка, а не кабель) становится кабелем, «Статор ПЭД б/у» — тоже не
   то, чем кажется. Имя решает только там, где оно однозначно, и каждый такой
   случай заводится сюда руками, с доказательством в комментарии. */
const NM_FIX = [
  {re: /нирезист/i, from: 'color', to: 'alloy',
   why: 'нирезист — высоконикелевый чугун; в 1С он есть и в легированной стали'},
];
function bpMat(series){
  const m = bpM(series), g = m && m.g;
  if(!g) return null;
  const by = {}; let tot = 0;
  for(const name in g){
    const t = +g[name] || 0;
    if(t <= 0) continue;
    const k = matOf(name);
    if(!by[k]) by[k] = {k:k, t:0};
    by[k].t += t; tot += t;
  }
  /* Перенос ВНУТРИ плана: общий тоннаж не меняется, доли остаются верными. */
  const nm = m.t, fixes = [];
  if(nm) for(const fx of NM_FIX){
    if(!by[fx.from]) continue;
    let moved = 0;
    for(const k in nm) if(fx.re.test(k)) moved += +nm[k] || 0;
    if(moved <= 0.0005) continue;
    const q = Math.min(moved, by[fx.from].t);
    by[fx.from].t -= q;
    if(by[fx.from].t <= 0.0005) delete by[fx.from];
    if(!by[fx.to]) by[fx.to] = {k: fx.to, t: 0};
    by[fx.to].t += q;
    /* подсказка обязана сказать, что тоннаж переставлен: иначе она уверяет,
       будто так и записано в 1С, а там он лежит в другой группе */
    fixes.push('⚠️ ' + fmt(bpVal(q)) + ' ' + bpUnit() + ' учтено как «' + MAT[fx.to].d
               + '», хотя в 1С это группа «' + MAT[fx.from].d + '»: ' + fx.why);
  }
  const list = Object.keys(by).map(k=>by[k]).sort((a,b)=>b.t-a.t);
  if(!list.length || tot <= 0) return null;
  list.forEach(x=>{ x.pct = x.t*100/tot; });
  return {list:list, tot:tot, fixes:fixes};
}
/** Значки состава. max — сколько клеток занять, остальное свернётся в «+N».
    Категории легче 1,5 % клетку не занимают: у плана с хвостом из пяти групп по
    двадцать килограммов значок кабеля читался бы наравне со значком трубы.
    Первая категория показывается всегда, даже если план из одной мелочи. */
function matSet(series, max, inline){
  const m = bpMat(series);
  if(!m) return '';
  const big = m.list.filter((x,i)=>i===0 || x.pct>=1.5);
  /* клеток ровно cap, ни одной сверх: «+N» — это тоже клетка, и без уступки ей
     места он переносился на третью строку сетки и вылезал из-под подписи */
  const cap = max||3;
  let shown = big.slice(0, cap);
  let rest = m.list.length - shown.length;
  if(rest>0 && shown.length===cap){ shown = shown.slice(0, cap-1); rest = m.list.length - shown.length; }
  const full = 'Из чего состоит план — группы учёта 1С:\n'
    + m.list.map(x=>'• '+MAT[x.k].d+' — '+fmt(bpVal(x.t))+' '+bpUnit()
        +' ('+x.pct.toFixed(1).replace('.',',')+' %)').join('\n')
    + ((m.fixes && m.fixes.length) ? '\n\n' + m.fixes.join('\n') : '');
  return '<span class="matset'+(inline?' mline':'')+'" title="'+esc(full)+'">'
    + shown.map((x,i)=>'<b class="mc'+(i?'':' lead')+'" title="'+esc(MAT[x.k].d+' — '
        + fmt(bpVal(x.t))+' '+bpUnit()+' ('
        + x.pct.toFixed(1).replace('.',',')+' %)')+'">'+matIcon(x.k)+'</b>').join('')
    + (rest>0 ? '<b class="mc more" title="'+esc(full)+'">+'+rest+'</b>' : '')
    + '</span>';
}
/** Пустое место значков — чтобы названия строк без состава не съезжали влево. */
const MAT_GAP = '<span class="matset mline"></span>';
/* ---------- «ТАКЖЕ В ЭТОМ ПЛАНЕ»: итоги в ДРУГИХ единицах ----------
   Переключатель разрезает мир по единицам, и в тоннах не видно, что приход
   плана — 15 штук ёмкостей. Экономист читает пустую ячейку «купили» как потерю
   данных. Поэтому в шапке плана показываем итоги по ОСТАЛЬНЫМ единицам, а если
   приход и расход разошлись по единицам — ставим бейдж: это не дырка, это
   разборка, где штуки превращаются в тонны. */
/* Итоги по ВСЕМ единицам сразу — отдельным проходом по движениям, потому что
   flowAgg считает только выбранный разрез. Ключи те же, что у дерева, поэтому
   сводка работает на любом уровне: проект, вариант серии, склад. */
let _UAGG = null;
function unitAgg(){
  if(_UAGG) return _UAGG;
  const BS=new Map(), BV=new Map(), BW=new Map();
  const add=(m,k,mc,fl,v)=>{ let o=m.get(k); if(!o){ o={}; m.set(k,o); }
                             let c=o[mc]; if(!c){ c={}; o[mc]=c; }
                             c[fl]=(c[fl]||0)+v; };
  for(const r of MOVES){
    const fl = mfl(r);
    if(fl!=='куплено' && fl!=='продано') continue;
    const mc = mst(r,'mc');
    const v = (mc===MMAIN) ? r[MC.tonnes] : r[MC.mq];
    if(Math.abs(v)<=0.0005) continue;
    const se=mst(r,'series'), va=mst(r,'variant'), w=mst(r,'sklad');
    add(BS, se, mc, fl, v);
    add(BV, se+SEP+va, mc, fl, v);
    add(BW, se+SEP+va+SEP+w, mc, fl, v);
  }
  _UAGG={s:BS, v:BV, w:BW};
  return _UAGG;
}
/** Сводка «также: … в других единицах» + бейдж смены единицы. lvl: s|v|w. */
function otherUnits(key, lvl){
  /* под уровнем единицы сводку не показываем: там уже выбрана одна единица, и
     «также: 15 шт» рядом с тоннами читалось бы как ещё одна цифра этой строки */
  if(UNIT_LEVEL) return '';
  const src = unitAgg()[lvl]; if(!src) return '';
  const o = src.get(key); if(!o) return '';
  /* сводка лежит по КЛАССАМ данных: «кг» — тот же весовой класс, что «т» */
  const M0 = measBase(MEASURE);
  const parts=[], mixed=[];
  for(const mk in o){
    if(mk===M0) continue;
    const t = o[mk] || {};
    const bits=[];
    if(t['куплено']) bits.push('куплено '+fmt(t['куплено']));
    if(t['продано']) bits.push('продано '+fmt(t['продано']));
    if(bits.length) parts.push(bits.join(' · ')+' '+mk);
  }
  /* единица прихода и единица расхода разные -> в цепочке была разборка */
  const cur = o[M0] || {};
  if(!cur['куплено'] && cur['продано']){
    for(const mk in o){
      if(mk===M0) continue;
      if((o[mk]||{})['куплено']) mixed.push(mk);
    }
  }
  let out='';
  if(mixed.length) out += '<span class="tag unitmix" title="'+esc('Приход этого плана '
    + 'записан в 1С в других единицах ('+mixed.join(', ')+'), а расход — в '+bpUnit()+'. '
    + 'Так бывает, когда единица меняется прямо в переработке: разобрали ёмкость '
    + '(1 шт) — получили лом (40 т). Пустая ячейка «купили» здесь не потеря данных. '
    + 'Переключите единицу вверху, чтобы увидеть приход.')+'">единица меняется в переработке</span>';
  if(parts.length) out += '<span class="tag" title="'+esc('Итоги этого плана в других '
    + 'единицах. Суммы разных единиц между собой не складываются.')+'"> · также: '
    + esc(parts.join(' · '))+'</span>';
  return out;
}
/** Легенда значков — читается один раз, дальше значки узнаются сами. */
function matLegendNote(){
  return howto('Значки: из чего состоит бизнес-план',
      '<b>Значок в начале строки — группа аналитического учёта 1С</b> '
    + '(<i>НоменклатурнаяГруппаГрузов</i>): видно, лом это, труба, штанга или '
    + 'кабель, не раскрывая план. Значков до трёх, по убыванию тоннажа; «+N» — '
    + 'сколько групп ещё. Полный состав с тоннами и долями — в подсказке при '
    + 'наведении. Различает форма, а не цвет: цветом в отчёте показан только '
    + 'статус вывоза.'
    + '<div class="matleg">'
    + MAT_ORDER.map(k=>'<span><b class="mc">'+matIcon(k)+'</b>'+esc(MAT[k].n)+'</span>').join('')
    + '</div>');
}

/** Что даёт наведение на строку документа — объясняем один раз, наверху дерева:
    сам по себе значок расхождения не рассказывает, что под ним есть карточка. */
function docFateNote(){
  return howto('Строки документов: что вышло по документу и что было дальше',
      '<b>Наведите курсор на строку документа</b> в самом низу дерева — карточка '
    + 'покажет пару этого документа (<i>в производство ушло 350 → из '
    + 'производства вернулось 348</i>), что именно получили и какими документами '
    + 'металл жил дальше по лоту: продали, уехали, снова в производство, остаток. '
    + 'Клик закрепляет карточку, '
    + 'клик мимо — закрывает.<br>'
    + '<b>Расхождение стоит прямо в строке</b> и отбито цветом слева: '
    + '<span class="dgap g-warn">до 2 %</span> — обычный угар и обрезь; '
    + '<span class="dgap g-bad">больше 2 % или ничего не вернулось</span> — '
    + 'повод разобраться; <span class="dgap g-mix">1 шт → 0,097 т</span> — '
    + 'сменилась единица (разобрали оборудование в лом), такие числа между собой '
    + 'не сверяются. Где сошлось в ноль — значка нет: это 92 % строк.<br>'
    + 'Пара считается по ключу «номер документа + дата»: номера 1С повторяются '
    + 'из года в год, и по одному номеру склеились бы чужие движения. Сравнение '
    + 'идёт по документу целиком, а не по одной серии внутри него.');
}

/** Значки «что внутри»: топ по тоннажу, остальное свёрнуто в +N. */
function bpTags(series, top){
  const mm = bpM(series), t = mm && mm.t;
  if(!t) return '';
  const ks = Object.keys(t);
  if(!ks.length) return '';
  const shown = ks.slice(0, top||3);
  const rest  = ks.slice(top||3);
  const all = ks.map(k=>k+' — '+fmt(bpVal(t[k]))+' '+bpUnit()).join('\n');
  return '<span class="nmtags" title="'+esc('Что внутри плана:\n'+all)+'">'
    + shown.map(k=>'<i>'+esc(k)+'</i>').join('')
    + (rest.length?'<i class="more">+'+rest.length+'</i>':'')+'</span>';
}

/* ---------- ПОИСК: число ищется ЦЕЛИКОМ ----------
   Поиск идёт по многим полям сразу (серия, код, номенклатура, договор,
   покупатель, номер документа) — это удобно, но набранное «1578» цеплялось как
   ПОДСТРОКА: находились БП «2023001578 БП 522», документы «РУМЛ-001578» у чужих
   планов и коды номенклатуры «00000015782». В выдаче по 1578 оказывалось шесть
   бизнес-планов вместо одного.

   Правило: если запрос — только цифры, число должно стоять ОТДЕЛЬНО (слева и
   справа не цифра). Тогда «1578» не цепляет ни «2023001578», ни «РУМЛ-001578».
   Любой другой запрос («РУМЛ-001578», «ГЕРМЕС», «Лом 5А») ищется как раньше —
   подстрокой, поэтому найти документ по полному номеру по-прежнему можно. */
function qMatcher(q){
  q = (q||'').trim().toLowerCase();
  if(!q) return null;
  if(/^\d+$/.test(q)){
    const re = new RegExp('(^|[^0-9])' + q + '($|[^0-9])');
    return hay => re.test(hay);
  }
  return hay => hay.indexOf(q) >= 0;
}

/* ---------- выбранный бизнес-план держится на всех вкладках ----------
   Экономист смотрит один план в нескольких разрезах: набирать его номер заново
   на каждой вкладке — лишняя работа. Строка поиска общая. */
const BPF_KEY = 'metoptorg.bpfocus';
let BP_FOCUS = (()=>{ try{ return localStorage.getItem(BPF_KEY)||''; }catch(e){ return ''; } })();
/* ⚠️ Значение НЕ обрезается по краям и НЕ переписывается в поле, где печатают.
   Раньше здесь стоял .trim(), а результат тут же раздавался во ВСЕ поля с
   классом bpq — включая то, в котором пользователь набирает. Из-за этого пробел
   было физически не поставить: «лом » тут же превращалось обратно в «лом», и
   до второго слова дело не доходило. Пробелы по краям снимаются там, где строка
   применяется к данным (applyFilters и фильтр Ганты), — это разные вещи. */
function setBpFocus(v, src){
  BP_FOCUS = (v==null ? '' : String(v));
  try{ localStorage.setItem(BPF_KEY, BP_FOCUS); }catch(e){}
  document.querySelectorAll('input.bpq').forEach(i=>{
    if(i===src || (typeof document!=='undefined' && i===document.activeElement)) return;
    if(i.value!==BP_FOCUS) i.value=BP_FOCUS;
  });
}

function filterBar(app, onDraw, extra, noMeasure){
  const bar = el('div','toolbar');
  bar.innerHTML = '<input type="search" id="q" class="bpq" value="'+esc(BP_FOCUS)+'" placeholder="Поиск: серия / код / номенклатура / договор / покупатель…" title="Число ищется целиком: «1578» найдёт БП 1578 и не зацепит «2023001578» или документ «РУМЛ-001578». Текст ищется как часть строки.">'
    + (noMeasure ? '' : measureSelect('fmeas'))
    + groupSelect('fgrp')
    + divSelect('fdiv')
    + caSelect('fca')
    + '<select id="fbadge"><option value="">происхождение: все</option>'
    + Object.keys(BADGE_RANK).map(b=>'<option>'+b+'</option>').join('')+'</select>'
    + '<select id="fsite"><option value="">подразделение: все</option><option>Цех</option>'
      +'<option>База</option><option>Заготовка</option><option>Площадка</option>'
      +'<option>Демонтаж</option><option>АУП</option></select>'
    + '<select id="fsort"><option value="new">сортировка: новые сверху</option>'
      +'<option value="t">по тоннажу</option>'
      +'<option value="old">старые сверху</option>'
      +'<option value="az">по алфавиту</option></select>'
    + (extra||'')
    + '<span class="right muted" id="cnt"></span>';
  app.appendChild(bar);
  ['#q','#fgrp','#fdiv','#fca','#fbadge','#fsite','#fsort'].forEach(s=>{
    const e=$(s); if(e) e.addEventListener(s==='#q'?'input':'change',onDraw); });
  /* смена единицы — не обычный фильтр: она сбрасывает кэш потоков, поэтому
     сначала setMeasure, и только потом перерисовка */
  const fm=$('#fmeas');
  if(fm) fm.addEventListener('change', ()=>{ setMeasure(fm.value); rerenderTab(); });
  const q=$('#q');
  if(q) q.addEventListener('input', ()=>setBpFocus(q.value, q));
  return bar;
}
function applyFilters(rows){
  const q=($('#q')&&$('#q').value||'').trim().toLowerCase();
  const fb=$('#fbadge')?$('#fbadge').value:'';
  const fs=$('#fsite')?$('#fsite').value:'';
  const fg=$('#fgrp')?$('#fgrp').value:'';
  const fdv=$('#fdiv')?$('#fdiv').value:'';
  const fca=$('#fca')?$('#fca').value:'';
  /* РАЗРЕЗ ПО ЕДИНИЦЕ — первым: в отчёте остаются только позиции выбранной
     единицы, поэтому и планы остаются только те, где она есть. Складывать
     штуки с тоннами в одной колонке нельзя ни на каком уровне дерева. */
  let d=rows.filter(iIn);
  /* группа учёта, дивизион и контрагент фильтруют БИЗНЕС-ПЛАНЫ целиком: у плана
     остаются все его позиции, иначе он перестал бы сходиться сам с собой */
  if(fg) d=d.filter(r=>bpHasGroup(r.series, fg));
  if(fdv) d=d.filter(r=>bpHasDiv(r.series, fdv));
  if(fca) d=d.filter(r=>bpHasCa(r.series, fca));
  const qm = qMatcher(q);
  if(qm) d=d.filter(r=>qm((r.series_full+' '+r.code+' '+r.name+' '+r.contract+' '+r.contragent+' '+r.sklad
      +' '+r.docs.map(x=>str(x.buyer)+' '+x.regnum).join(' ')).toLowerCase()));
  if(fb) d=d.filter(r=>r.badge===fb);
  if(fs) d=d.filter(r=>r.site===fs);
  const c=$('#cnt');
  if(c) c.textContent = curMeasure()===MEASURE_ALL
    ? cnt(d.length)+' позиций · единицы разные, общей суммы нет'
    : fmt(sum(d,r=>iVal(r)))+' '+unitLabel()+' в '+cnt(d.length)+' позициях';
  return d;
}

/* ================================================================
   1) БИЗНЕС-ПЛАНЫ: направление → БП (договор) → серия → номенклатура → документы
   ================================================================ */
TABS.bp = function(app){
  app.appendChild(kpiCards(ITEMS));
  app.appendChild(matLegendNote());
  filterBar(app, draw);
  const wrap = el('div','panel tree'); app.appendChild(wrap);
  function draw(){
    const data = applyFilters(ITEMS);
    window.__COLHEAD='направление · бизнес-план (проект) · серия · откуда продано · склад · номенклатура';
    wrap.innerHTML=''; wrap.appendChild(colHead());
    /* Бизнес-план = ПРОЕКТ из справочника серий (их столько же, сколько проектов).
       Серии объединяются проектом, а не номером договора: под одним договором
       бывают десятки спецификаций, и каждая — свой проект. Номер договора
       остаётся подписью на узле. */
    const byDir = groupBy(data, r=>r.direction||'Прочее');
    orderedKeys(byDir).forEach(dir=>{
      const drows=byDir.get(dir);
      wrap.appendChild(treeNode('<b>'+esc(dir)+'</b>', lcNode(drows,'s'), k1=>{
        const byProj = groupBy(drows, r=>r.series);
        orderedKeys(byProj).forEach(pk2=>{
          const brows=byProj.get(pk2), s0=brows[0];
          const extra=[];
          if(s0.contract) extra.push('договор '+esc(s0.contract));
          if(s0.contragent) extra.push(esc(s0.contragent));
          if(s0.datavyvoza) extra.push('вывоз до '+esc(s0.datavyvoza));
          /* значки состава заняли место значка 📄: в ведущей позиции строки
             теперь стоит смысл, а не украшение. Нет состава — пустая клетка той
             же ширины, иначе название этой строки уедет левее всех остальных */
          const lbl=(matSet(pk2, 3, true)||MAT_GAP)+'<b>'+esc(s0.series_full)+'</b>' + bpTags(pk2, 3)
            + otherUnits(pk2,'s')
            + (extra.length?' <span class="tag">· '+extra.join(' · ')+'</span>':'')
            + dtTag(brows);
          k1.appendChild(treeNode(lbl, lcNode(brows,'s'), k2=>{
            seriesNodes(k2, brows);
          }, {badge:badge(worstOf(brows))}));
        });
      }, {open:byDir.size<=1, wide:true}));
    });
    fitLabels();
  }
  draw();
};

/* ================================================================
   2) СЕРИИ (продано): серия (полная строка) → базы → номенклатура → документы
   Уровень «договор» отдельным узлом НЕ выделяем — номер уже в полной строке.
   ================================================================ */
/* ---------- ЕДИНИЦА ВЫБИРАЕТСЯ У ПРОЕКТА, А НЕ СВЕРХУ ----------
   Глобальный фильтр прятал целые проекты: выбрал «штуки» — и планы без штук
   исчезли из списка. Правило заказчика: список проектов показывается целиком, а
   единица выбирается на самом проекте и применяется ко всему его разбору.
   По умолчанию — тонны, если у проекта они есть; иначе самая крупная единица. */
const PROJ_MEAS = new Map();
function projUnits(sk){
  const o = unitAgg().s.get(sk) || {};
  return Object.keys(o).sort((a,b)=>{
    if(a===MMAIN) return -1; if(b===MMAIN) return 1;
    const v=k=>((o[k]||{})['куплено']||0)+((o[k]||{})['продано']||0);
    return v(b)-v(a);
  });
}
function projMeasure(sk){
  if(PROJ_MEAS.has(sk)) return PROJ_MEAS.get(sk);
  const u = projUnits(sk);
  return u.length ? u[0] : MMAIN;
}
/* ---------- ЕДИНИЦА СРАЗУ ДЛЯ ВСЕХ ПРОЕКТОВ ----------
   Единица у каждого проекта своя — это правильно, когда смотрят один план. Но
   когда нужен срез «покажи всё в тоннах», щёлкать по 146 проектам нельзя.
   Поэтому наверху есть общий переключатель: выбрали единицу — весь список
   считается в ней, а проекты, у которых её нет, из списка уходят (иначе в
   колонке стояли бы нули, которые читаются как «продаж не было»).
   Пустое значение — прежнее поведение: у каждого проекта своя единица. */
const ALLM_KEY = 'metoptorg.allmeasure';
let ALL_MEASURE = (()=>{ try{ const v = localStorage.getItem(ALLM_KEY)||'';
                              return MKEYS.indexOf(v)>=0 ? v : ''; }
                         catch(e){ return ''; } })();
function setAllMeasure(v){
  ALL_MEASURE = MKEYS.indexOf(v)>=0 ? v : '';
  try{ localStorage.setItem(ALLM_KEY, ALL_MEASURE); }catch(e){}
  /* выбранная «для всех» единица становится единицей отчёта: карточки, состав
     плана и остальные вкладки обязаны показывать то же, что дерево */
  if(ALL_MEASURE) setMeasure(ALL_MEASURE);
}
/* ⚠️ Счётчик «(N БП)» в списке разрезов брался из movements: там 1 061 план,
   а в дереве продаж их 868 — цифра рядом с выбором не сходилась с тем, что
   получаешь после выбора. Считаем по ПОЗИЦИЯМ ПРОДАЖ, то есть ровно по тому,
   что вкладка покажет. */
let _MBPS = null;
function measBps(key){
  if(!_MBPS){
    _MBPS = {};
    const by = {};
    for(const r of ITEMS){
      const w = Math.abs(r.tonnes)>1e-9;
      /* позиция с единицей «т» и нулевым тоннажом не считается ни весовой,
         ни штучной — иначе счётчик показывал 869 при 868 строках в дереве */
      if(!w && (!r.mc || r.mc===MMAIN)) continue;
      const u = w ? MMAIN : r.mc;
      (by[u] || (by[u] = new Set())).add(r.series);
    }
    for(const u in by) _MBPS[u] = by[u].size;
  }
  return _MBPS[key] || 0;
}
function allMeasSelect(){
  return '<select id="fallmeas" class="msel" title="Единица сразу для всех '
    + 'бизнес-планов. «У каждого своя» — прежний режим: единица выбирается в '
    + 'строке проекта. Если выбрать конкретную, планы без неё из списка уйдут: '
    + 'показывать их нулями значит врать, что продаж не было. Килограммы — тот '
    + 'же вес, что тонны, только ×1000.">'
    + '<option value="">единица: у каждого проекта своя</option>'
    + MEASURES.map(m=>'<option value="'+esc(m.key)+'"'
        +(m.key===ALL_MEASURE?' selected':'')+'>для всех: '+esc(m.title)
        +' ('+cnt(measBps(m.key))+' БП)</option>').join('')
    + '</select>';
}
/** Переключатель единицы в подписи проекта. Одна единица — просто подпись. */
function projMeasChips(sk){
  const u = projUnits(sk);
  if(!u.length) return '';
  /* единица задана сверху для всех — выбирать в строке нечего */
  if(ALL_MEASURE) return '<span class="tag umark" title="Единица выбрана сверху '
    + 'сразу для всех проектов">'+esc(ALL_MEASURE)+'</span>';
  const cur = projMeasure(sk);
  if(u.length === 1)
    return '<span class="tag umark" title="Весь проект записан в этой единице">'
      + esc(u[0])+'</span>';
  return '<span class="umeas" data-sk="'+esc(sk)+'" title="В какой единице '
    + 'смотреть этот проект. Единицы не складываются между собой, поэтому разбор '
    + 'показывается в одной выбранной.">'
    + u.map(k=>'<b'+(k===cur?' class="on"':'')+' data-u="'+esc(k)+'">'+esc(k)+'</b>').join('')
    + '</span>';
}

/* ---------- УРОВЕНЬ «ЕДИНИЦА ИЗМЕРЕНИЯ» («Серии насквозь») ----------
   Раньше эта вкладка складывала в одну колонку тонны, штуки, литры и метры —
   «как в 1С». Читать это нельзя: 3 040 927 литров дизтоплива забивали собой
   тоннаж, а «Лом меди эмаль, кг» вставал числом 5 450 рядом с тоннами.
   Теперь под проектом идёт УРОВЕНЬ ЕДИНИЦЫ, и всё, что ниже, считается в ней
   одной. Килограммы отдельным уровнем не выделяем: это тот же вес, что тонны,
   и в 1С он сведён к тоннам ещё в расчёте. */
function unitNodes(parent, sk, srows){
  /* единицы берём и из сводки закупок-продаж, и из самих позиций: у проекта,
     где в этой единице только двигали (не продавали), уровень тоже нужен */
  const seen = new Set(projUnits(sk));
  srows.forEach(r=>{ if(r.mc) seen.add(r.mc);
                     if(Math.abs(r.tonnes)>1e-9) seen.add(MMAIN); });
  const units = [...seen].sort((a,b)=> a===MMAIN ? -1 : b===MMAIN ? 1
                                     : a.localeCompare(b,'ru'));
  if(!units.length){ seriesNodes(parent, srows); return; }
  units.forEach(u=>{
    /* вес показываем так, как выбрано в отчёте: тонны или килограммы */
    const key = u;
    /* ⚠️ Узел единицы создаём УЖЕ в этой единице: treeNode запоминает режим на
       момент рождения и возвращает его перед построением детей, поэтому разрез
       держится до самого низа — серии, склады, номенклатура, документы. Полос
       со всеми единицами сразу ниже этого уровня не остаётся: их место —
       строка проекта, где ещё не выбрано, о какой единице речь. */
    const prev = MEASURE_FORCED, prevU = UNIT_LEVEL;
    MEASURE_FORCED = key; UNIT_LEVEL = key;
    const val = lcNode(srows,'s');
    /* подпись короткая: «что всё ниже считается в этой единице» сказано один раз
       в плашке наверху, повторять это в каждой строке — шум */
    const lbl = '<b>'+esc(mTitle(key))+'</b> <span class="tag umark" title="'
      + esc('Всё ниже — серии, склады, номенклатура и документы — считается '
          + 'в этой единице. С другими единицами эти суммы не складываются.')
      + '">'+esc(key)+'</span>';
    parent.appendChild(treeNode(lbl, val, k=>seriesNodes(k, srows), {}));
    MEASURE_FORCED = prev; UNIT_LEVEL = prevU;
  });
}

/* ДВЕ ВКЛАДКИ «СЕРИИ» — одно дерево, два режима единиц:
     • «Серии (продано)»  — с переключателем единицы: тонны / штуки / метры…
     • «Серии (насквозь)» — всё сразу, количества в РОДНЫХ единицах, как в 1С:
       ничего не пересчитывается и не отфильтровывается, разные единицы лежат в
       одной колонке. Так их складывает и сам 1С в отчёте «Остатки и движение».
   Вкладка «Направление» убрана (06.08.2026): то же дерево третьей копией только
   путало, а разрез «направление» остался фильтром. */
function seriesTab(app, allUnits){
  MEASURE_FORCED = allUnits ? MEASURE_ALL : (ALL_MEASURE || null);
  /* чем эта вкладка отличается от соседней — прямо в заголовке, а не в скобках */
  app.appendChild(el('div','tabhead', allUnits
    ? '<h2>Весь оборот по сериям</h2><p>Всё движение металла по бизнес-планам, '
      + '<b>включая непроданное</b>: купили · уехало · приехало · в сортировку · '
      + 'в производство · продали · остаток. Единица здесь — <b>уровень дерева</b>: под '
      + 'проектом идут тонны, штуки, метры, литры, и уже под ними серии, склады и '
      + 'документы. Суммы разных единиц не складываются, поэтому у строки проекта '
      + 'столько полос с цифрами, сколько у него единиц.</p>'
    : '<h2>Продажи по сериям</h2><p>Что <b>продано</b> по каждому бизнес-плану и '
      + 'откуда. Отчёт считается в <b>одной</b> единице — она выбирается вверху '
      + '(«для всех») или у самого проекта; планы, где в этой единице не '
      + 'продавали, из списка уходят. Весь оборот, включая непроданное и другие '
      + 'единицы, — на вкладке «Весь оборот по сериям».</p>'));
  app.appendChild(kpiCards(ITEMS));
  if(allUnits){
    app.appendChild(el('div','measbar',
      'Вес всегда в <b>тоннах</b>: в 1С часть номенклатуры записана килограммами '
      + '(«Лом меди эмаль, кг»), в расчёте они переведены в тонны — это одна и та '
      + 'же величина, и отдельного разреза «килограммы» в отчёте нет.'));
  }
  app.appendChild(matLegendNote());
  app.appendChild(docFateNote());
  /* на «продано» единица выбирается сверху для всех или в строке проекта;
     на «насквозь» она стала уровнем дерева, и выбирать нечего */
  filterBar(app, draw, allUnits ? '' : allMeasSelect(), true);
  const fam = $('#fallmeas');
  if(fam) fam.addEventListener('change', ()=>{ setAllMeasure(fam.value); rerenderTab(); });
  const wrap = el('div','panel tree'); app.appendChild(wrap);
  function draw(){
    /* СПИСОК ПРОЕКТОВ строится БЕЗ отбора по единице: иначе проект без тонн
       исчезал бы из списка целиком. Единица применяется НИЖЕ, к каждому проекту
       отдельно (projMeasure). Исключение — когда единица задана СВЕРХУ ДЛЯ ВСЕХ:
       тогда отбор по ней и есть смысл режима, и планы без неё уходят. */
    const keep = MEASURE_FORCED;
    MEASURE_FORCED = (!allUnits && ALL_MEASURE) ? ALL_MEASURE : MEASURE_ALL;
    const data = applyFilters(ITEMS);
    MEASURE_FORCED = keep;
    window.__COLHEAD = allUnits
      ? 'проект · единица · серия · откуда продано · склад · номенклатура'
      : 'проект · серия · откуда продано · склад · номенклатура';
    wrap.innerHTML=''; wrap.appendChild(colHead());
    const bySer = groupBy(data, r=>r.series);
    /* ⚠️ ПРОЕКТ БЕЗ ЕДИНОЙ ПРОДАЖИ ПРОПАДАЛ ИЗ ДЕРЕВА ЦЕЛИКОМ. Список строится
       из ITEMS — а это ПОЗИЦИИ ПРОДАЖ. Для вкладки «Продажи по сериям» так и
       надо, но «Весь оборот по сериям» подписана «включая непроданное», и там
       это ошибка: из отчёта выпадало 143 проекта из 1 079, по ним куплено
       21 515,290 т. Причём выпадало самое интересное — свежие бизнес-планы, где
       закупка идёт, а продаж ещё не было: 1893 (3 530,673 т), 1874 (3 063,506),
       1742 (1 924,327), 1929 (1 152,371). Пример заказчика — БП 1777: куплено
       169,310, перевезено на базу, 0,979 списано на затраты, продаж ноль.
       Достраиваем ровно тем же приёмом, что уже применён уровнем ниже для
       вариантов серии (см. seriesNodes): строка-заготовка несёт только проект и
       вариант, остальное берётся из flowAgg по этому ключу.
       ⚠️ ТОЛЬКО на «Весь оборот» (allUnits): на «Продажах» таким планам не место. */
    if(allUnits){
      const A0 = flowAgg();
      const full = {};
      (DATA.flows || []).forEach(r=>{ full[r.series] = r.series_full || r.series_display; });
      const q0 = ($('#q') && $('#q').value || '').trim().toLowerCase();
      const qm0 = qMatcher(q0);
      for(const pk of A0.S.keys()){
        if(bySer.has(pk)) continue;
        const nm = full[pk] || String(pk);   // в DATA.flows есть все проекты с движениями
        if(qm0 && !qm0(String(nm).toLowerCase())) continue;
        const vars = A0.VARS.get(pk);
        const list = (vars && vars.size ? [...vars] : ['']).map(v=>({
          series:pk, series_variant:v, series_full:nm, contragent:'', datavyvoza:'',
          sklad:'', site:'—', base:'', division:'', tonnes:0, docs:[], badge:'нет',
          code:null, name:'', last_dt:'', first_dt:'', _movesOnly:true}));
        bySer.set(pk, list);
      }
    }
    orderedKeys(bySer).forEach(sk=>{
      const srows=bySer.get(sk), s0=srows[0];
      /* весь проект — и его строка, и разбор — считается в ЕГО единице */
      const pm = allUnits ? MEASURE_ALL : (ALL_MEASURE || projMeasure(sk));
      /* строка ПРОЕКТА — единственное место, где единица ещё не выбрана: здесь
         и стоят все полосы сразу и сводка «также». Ниже единицу выбирает
         уровень дерева (unitNodes), и она держится до документов. */
      const prev = MEASURE_FORCED; MEASURE_FORCED = pm; UNIT_LEVEL = null;
      const extra=[];
      if(s0.contragent) extra.push(esc(s0.contragent));
      if(s0.datavyvoza) extra.push('вывоз до '+esc(s0.datavyvoza));
      const noSale = srows.every(r=>r._movesOnly);
      const lbl=(matSet(sk, 3, true)||MAT_GAP)+'<b>'+esc(s0.series_full)+'</b>' + bpTags(sk, 3)
        + (noSale ? '<span class="tag" title="'+esc('По этому бизнес-плану продаж '
            + 'ещё не было: металл куплен и/или перевезён, но не продан. Раньше '
            + 'такой план в дереве не показывался вовсе, хотя его закупка и '
            + 'остаток — настоящие.')+'">только движения — продаж не было</span>' : '')
        + (allUnits ? '' : projMeasChips(sk))
        + otherUnits(sk,'s')
        + (extra.length?' <span class="tag">· '+extra.join(' · ')+'</span>':'')
        + dtTag(srows);
      const node = treeNode(lbl, lcNode(srows,'s'), k1=>{
        /* разбор строится в единице проекта, а не в той, что осталась от
           предыдущего раскрытия: дети создаются лениво, по клику */
        const p2 = MEASURE_FORCED; MEASURE_FORCED = pm;
        try{ if(allUnits) unitNodes(k1, sk, srows); else seriesNodes(k1, srows); }
        finally{ MEASURE_FORCED = p2; }
      }, {badge:badge(worstOf(srows))});
      MEASURE_FORCED = prev;
      wrap.appendChild(node);
    });
    /* клик по единице в подписи проекта — не раскрытие узла, а смена разреза */
    wrap.querySelectorAll('.umeas b').forEach(b=>b.addEventListener('click', ev=>{
      ev.stopPropagation(); ev.preventDefault();
      PROJ_MEAS.set(b.parentElement.dataset.sk, b.dataset.u);
      draw();
    }));
    fitLabels();
  }
  draw();
}
TABS.series     = app => seriesTab(app, false);
TABS.series_all = app => seriesTab(app, true);


/* ================================================================
   4) ПЕРИОДЫ: месяц продажи → БП / серии, динамика
   ================================================================ */
TABS.period = function(app){
  /* разворачиваем позиции в помесячные срезы */
  const per = new Map();   /* месяц -> Map(ключ -> {t, item}) */
  const monthTotal = new Map();
  for(const r of ITEMS) for(const d of r.docs){
    const m=d.dt.slice(0,7);
    if(!per.has(m)) per.set(m,[]);
    per.get(m).push({m, t:d.tonnes, r, d});
    monthTotal.set(m,(monthTotal.get(m)||0)+d.tonnes);
  }
  const months=[...per.keys()].sort();
  const maxT=Math.max(...monthTotal.values(),1);

  const c=el('div','cards');
  const best=[...monthTotal.entries()].sort((a,b)=>b[1]-a[1])[0]||['—',0];
  const last12=months.slice(-12);
  c.innerHTML='<div class="card"><div class="k">Месяцев с продажами</div><div class="v">'+months.length+'</div>'
    +'<div class="s">'+(months[0]||'')+' … '+(months[months.length-1]||'')+'</div></div>'
    +'<div class="card"><div class="k">Лучший месяц</div><div class="v small">'+esc(best[0])+'</div>'
    +'<div class="s">'+fmt(tShow(best[1]))+' '+esc(wLabel())+'</div></div>'
    +'<div class="card"><div class="k">Средний месяц</div><div class="v">'
      +fmt(tShow(sum([...monthTotal.values()],x=>x)/Math.max(months.length,1)))+' <span class="unit">'+esc(wLabel())+'</span></div></div>'
    +'<div class="card"><div class="k">Последние 12 мес</div><div class="v">'
      +fmt(tShow(sum(last12,m=>monthTotal.get(m))))+' <span class="unit">'+esc(wLabel())+'</span></div></div>';
  app.appendChild(c);

  const dyn=el('div','panel');
  dyn.innerHTML='<h3>Динамика реализации по месяцам</h3>';
  const inner=el('div','scroll');
  months.slice().reverse().forEach(m=>{
    const t=monthTotal.get(m);
    const row=el('div','mrow');
    row.innerHTML='<span class="m">'+esc(m)+'</span>'
      +'<span class="bar"><i style="width:'+(100*t/maxT).toFixed(1)+'%"></i></span>'
      +'<span class="v">'+fmt(t)+' т</span>';
    inner.appendChild(row);
  });
  dyn.appendChild(inner); app.appendChild(dyn);

  const bar=el('div','toolbar');
  bar.innerHTML='<select id="grp"><option value="bp">группировать: по бизнес-планам (проектам)</option>'
    +'<option value="dg">группировать: по договорам</option></select>'
    +'<span class="right muted" id="cnt2"></span>';
  app.appendChild(bar);
  const wrap=el('div','panel tree'); app.appendChild(wrap);
  function draw(){
    const mode=$('#grp').value;
    wrap.innerHTML='';
    $('#cnt2').textContent=fmt(sum([...monthTotal.values()],x=>x))+' т всего';
    months.slice().reverse().forEach(m=>{
      const recs=per.get(m);
      wrap.appendChild(treeNode('<b>'+esc(m)+'</b>', tval(monthTotal.get(m)), k1=>{
        const g=groupBy(recs, x=> mode==='bp' ? x.r.series : (x.r.contract||'— договор не распознан —'));
        [...g.keys()].sort((a,b)=>sum(g.get(b),x=>x.t)-sum(g.get(a),x=>x.t)).forEach(key=>{
          const rs=g.get(key), r0=rs[0].r;
          const lbl = mode==='bp' ? '📄 '+esc(r0.series_full) : '📄 '+esc(key);
          k1.appendChild(treeNode(lbl, tval(sum(rs,x=>x.t)), k2=>{
            const byPos=groupBy(rs,x=>x.r.code+'|'+x.r.name);
            [...byPos.keys()].sort((a,b)=>sum(byPos.get(b),x=>x.t)-sum(byPos.get(a),x=>x.t)).forEach(pk=>{
              const ps=byPos.get(pk), p0=ps[0].r;
              const box=el('div','docs');
              box.appendChild(el('div','doc','<span class="rn">документ</span><span class="dt">дата</span>'
                +'<span class="lot">лот</span><span class="by">покупатель</span><span class="q">т</span>'));
              ps.sort((a,b)=>a.d.dt<b.d.dt?1:-1).forEach(x=>box.appendChild(el('div','doc',
                '<span class="rn">'+esc(x.d.regnum)+'</span><span class="dt">'+esc(x.d.dt)+'</span>'
                +'<span class="lot">'+esc(x.d.lot||'—')+'</span>'
                +'<span class="by">'+esc(str(x.d.buyer)||'—')+'</span>'
                +'<span class="q">'+fmt3(x.d.tonnes)+'</span>')));
              k2.appendChild(treeNode(esc(p0.name), tval(sum(ps,x=>x.t)),
                k3=>k3.appendChild(box), {code:p0.code}));
            });
          }, {badge: mode==='bp'?badge(worstOf(rs.map(x=>x.r))):''}));
        });
      }));
    });
  }
  $('#grp').addEventListener('change',draw);
  draw();
};

/* ================================================================
   4b) ЗАКУПКИ/ПРОДАЖИ — жизненный цикл партии по серии
       «купили 1000 т -> продали / вернули / списали / осталось»
   ================================================================ */
TABS.flows = function(app){
  const F = DATA.flows||[];
  const ORDER = META.flow_order||[];
  const TITLE = META.flow_title||{};
  const T = META.flow_totals||{};

  const c=el('div','cards');
  c.innerHTML=
    '<div class="card lead"><div class="k">Продано</div><div class="v">'+fmt(tShow(T['продано']))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">'+(T['куплено']?(100*T['продано']/T['куплено']).toFixed(1):0)+' % от купленного</div></div>'
    +'<div class="card"><div class="k">Куплено</div><div class="v">'+fmt(tShow(T['куплено']))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">Приобретение товаров и услуг</div></div>'
    +'<div class="card"><div class="k">Возврат + списано</div><div class="v small">'
      +fmt(tShow((T['возврат']||0)+(T['списано']||0)))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">возврат '+fmt(tShow(T['возврат']))+' · списано '+fmt(tShow(T['списано']))+'</div></div>'
    +'<div class="card"><div class="k">Остаток (факт 1С)</div><div class="v">'+fmt(tShow(T['остаток']))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">на '+esc(META.stock_date)+'</div></div>'
    /* ДВА НАШИХ ОСТАТКА РЯДОМ С ФАКТОМ. Складской считается как в 1С — приход
       минус расход, включая односторонние переработки; проектный (то, что в
       дереве) их намеренно исключает, иначе диагностический расход без весовой
       пары уменьшал бы остаток БП потерей, которой не было (БП 1586). */
    +'<div class="card"><div class="k">Наш складской остаток</div><div class="v">'
      +fmt(tShow(META.balance_moves_1c))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s" title="'+esc('Приход минус расход по движениям, включая '
        + 'односторонние переработки — та же арифметика, что у 1С. Это и есть '
        + 'величина, сопоставимая с фактом.')+'">так же считает 1С · Δ '
      +fmt(tShow((META.balance_moves_1c||0)-(T['остаток']||0)))+'</div></div>'
    +'<div class="card"><div class="k">Проектный остаток</div><div class="v">'
      +fmt(tShow(META.balance_moves))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s" title="'+esc('То, что показывает колонка ОСТАТОК в дереве. '
        + 'Односторонние переработки из него выброшены намеренно: нельзя вешать '
        + 'на бизнес-план потерю, которой не было. С 1С не сверяется — в снимке '
        + 'треть тоннажа лежит вообще без серии.')+'">колонка ОСТАТОК в дереве</div></div>';
  app.appendChild(c);

  const n=howto('Как читать: внешние потоки и внутренние — разные вещи','');
  n.querySelector('.nb').innerHTML='<b>Внешние потоки и внутренние — разные вещи.</b> Купили / продали / '
    +'возврат / списали — это приход и выбытие компании. «Уехало ↔ приехало» и '
    +'«забрали ↔ вернули ГП» — <i>внутренние</i> перекладывания того же металла '
    +'(в таблице курсивом и отделены линией); в приход их складывать нельзя — '
    +'у БП 1578 это давало «поступило 5 041 т» при закупке 2 382 т. '
    +'В баланс от них идёт только НЕТТО: потери переработки и недоезд.<br>'
    +'<b>Колонки:</b> купили → переместили '
    +'(<b>уехало</b> — расход со склада-отправителя, <b>приехало</b> — приход на склад-получатель) → '
    +'переработка (<b>забрали</b> — сырьё, <b>вернули ГП</b> — готовая продукция) → продали → '
    +'списали на затраты. Тождество: <code>поступило − выбыло − остаток = 0</code>, '
    +'где поступило = куплено + излишки + ввод + вернули ГП + приехало, '
    +'выбыло = продано + возврат + списано + на затраты + забрали + уехало + внутренний оборот. '
    +'«Уехало» и «приехало» в сумме по компании гасятся — разница между ними видна по серии '
    +'и показывает недоехавшее/потерянное в пути.';
  app.appendChild(n);

  /* сводка по потокам */
  const p0=el('div','panel');
  p0.innerHTML='<h3>Итог по всем сериям</h3>';
  const t0=el('table');
  const LBL = META.flow_label||{};
  const rowsHtml = ORDER.filter(k=>Math.abs(T[k]||0)>0.001).map(k=>
      '<tr><td><b>'+esc(LBL[k]||k.replace(/_/g,' '))+'</b></td><td class="num">'+fmt(T[k])+'</td>'
      +'<td class="muted">'+esc(TITLE[k]||'')+'</td></tr>').join('');
  t0.innerHTML='<thead><tr><th>Поток</th><th class="num">Тонн</th><th>Что это</th></tr></thead><tbody>'
    + rowsHtml
    + '<tr><td>внутренний оборот</td><td class="num">'+fmt(T['внутренний_оборот'])+'</td>'
      +'<td class="muted">перепродажа своей организации «проект Базы» — не продажа</td></tr>'
    + '<tr style="border-top:1px solid var(--line-2)"><td><b>ВНЕШНИЙ ПРИХОД</b></td>'
      +'<td class="num"><b>'+fmt(T['поступило'])+'</b></td>'
      +'<td class="muted">куплено + излишки + ввод остатков + пересортица</td></tr>'
    + '<tr><td><b>ВНЕШНЕЕ ВЫБЫТИЕ</b></td><td class="num"><b>'+fmt(T['выбыло'])+'</b></td>'
      +'<td class="muted">продали + возврат + недостачи + списание на затраты + внутр. оборот</td></tr>'
    + '<tr><td>односторонний расход переработки</td><td class="num">'+fmt(T['переработка_расход_без_пары'])+'</td>'
      +'<td class="muted">в документе есть тоннаж сырья, но нет весового выхода</td></tr>'
    + '<tr><td>односторонний приход от разборки</td><td class="num">'+fmt(T['переработка_приход_без_пары'])+'</td>'
      +'<td class="muted">в документе есть тоннаж выпуска, но нет весового входа</td></tr>'
    + '<tr><td>нетто односторонней переработки</td><td class="num">'+fmt(T['потери_переработки'])+'</td>'
      +'<td class="muted">односторонний расход − односторонний приход; парная резка сходится</td></tr>'
    + '<tr><td>недоезд при перемещении</td><td class="num">'+fmt(T['недоезд'])+'</td>'
      +'<td class="muted">уехало − приехало (внутри компании)</td></tr>'
    + '<tr style="border-top:1px solid var(--line-2)"><td><b>ОСТАТОК: наш складской</b></td>'
      +'<td class="num"><b>'+fmt(META.balance_moves_1c)+'</b></td>'
      +'<td class="muted">приход − расход по движениям, ВКЛЮЧАЯ односторонние '
      +'переработки — та же арифметика, что у 1С</td></tr>'
    + '<tr><td><b>ОСТАТОК: факт 1С</b></td><td class="num"><b>'+fmt(T['остаток'])+'</b></td>'
      +'<td class="muted">снимок остатков на '+esc(META.stock_date)+' — '
      +'сравнивать надо со строкой выше</td></tr>'
    + '<tr><td>ОСТАТОК: проектный (дерево)</td><td class="num">'+fmt(META.balance_moves)+'</td>'
      +'<td class="muted">без односторонних переработок: нельзя вешать на БП '
      +'потерю, которой не было. С 1С не сверяется</td></tr>'
    + '<tr><td><b>НЕВЯЗКА</b></td><td class="num warn"><b>'+fmt(T['невязка'])+'</b></td>'
      +'<td class="muted">приход − выбытие − потери − недоезд − остаток</td></tr></tbody>';
  p0.appendChild(t0); app.appendChild(p0);

  const n2=el('div','note warn');
  n2.innerHTML='<b>Два наших остатка, и сверяется с 1С только один.</b> '
    +'<i>Складской</i> считается ровно как в 1С — приход минус расход по всем строкам, '
    +'включая односторонние переработки; его и надо сравнивать с фактом. '
    +'<i>Проектный</i> — это колонка ОСТАТОК в дереве: из него односторонние выброшены '
    +'намеренно, иначе диагностический расход без весовой пары уменьшал бы остаток '
    +'бизнес-плана потерей, которой не было. Сверять проектный с 1С нельзя в принципе: '
    +'в снимке остатков треть тоннажа лежит вообще <b>без серии</b> — 1С не знает, чей это металл, '
    +'ради этого сервис и сделан.<br>'
    +'<b>Почему невязка не ноль.</b> Эталон покрывает '
    +fmt(META.stock_sklady)+' складов из '+fmt(META.move_sklady)+', у многих строк не заполнена серия, '
    +'а серия для части движений восстановлена каскадом. Невязка видна построчно в таблице ниже.';
  app.appendChild(n2);

  /* построчно по сериям */
  const bar=el('div','toolbar');
  bar.innerHTML='<input type="search" id="fq" placeholder="Поиск: серия / бизнес-план / направление…">'
    +'<select id="fs2"><option value="buy">сортировка: по купленному</option>'
      +'<option value="sale">по проданному</option>'
      +'<option value="nev">по невязке</option>'
      +'<option value="ost">по остатку</option></select>'
    +'<label class="chip" style="cursor:pointer"><input type="checkbox" id="fonly"> только с покупкой</label>'
    +'<span class="right muted" id="fcnt"></span>';
  app.appendChild(bar);
  const wrap=el('div','panel scroll'); app.appendChild(wrap);
  function draw(){
    const q=$('#fq').value.trim().toLowerCase(), s=$('#fs2').value;
    let d=F.slice();
    const qm1=qMatcher(q);
    if(qm1) d=d.filter(r=>qm1((r.series_full+' '+r.contract+' '+r.direction).toLowerCase()));
    if($('#fonly').checked) d=d.filter(r=>Math.abs(r['куплено'])>0.001);
    const key={buy:'куплено',sale:'продано',nev:'невязка',ost:'остаток'}[s];
    d.sort((a,b)=> (s==='nev'? Math.abs(b[key])-Math.abs(a[key]) : b[key]-a[key]));
    $('#fcnt').textContent=cnt(d.length)+' серий';
    d=d.slice(0,2000);
    const cols=ORDER.filter(k=>Math.abs(T[k]||0)>0.001);
    wrap.innerHTML='<table><thead><tr><th>Серия</th><th>Бизнес-план</th>'
      + cols.map(k=>'<th class="num" title="'+esc(TITLE[k]||'')+'">'+esc(LBL[k]||k.replace(/_/g,' '))+'</th>').join('')
      /* ⚠️ ЭТО ДРУГОЙ ОСТАТОК, чем в дереве, и путать их нельзя.
         Здесь — ФАКТ из файла остатков 1С по серии; в дереве — приход минус
         расход по движениям. У БП 1491/1493 это 389,738 против 803,568, и
         разница (413,830) и есть невязка: файл остатков покрывает только
         91 склад из 1 852 и только строки с заполненной серией. */
      + '<th class="num" title="'+esc('ФАКТ из файла остатков 1С по этой серии на '
          + (META.stock_date||'дату файла') + '. Это НЕ то же, что колонка ОСТАТОК '
          + 'в дереве: там приход минус расход по движениям. Файл остатков '
          + 'покрывает ' + META.stock_sklady + ' складов из ' + META.move_sklady
          + ' и только строки с заполненной серией, поэтому цифры расходятся — '
          + 'разница и стоит в колонке «невязка».')
      /* ⚠️ Колонки «остаток (наш, как в 1С)» здесь НЕТ (решение заказчика
         12.08.2026, «не нужен совсем»). Сверка складского остатка с фактом
         живёт на вкладке «Проверка данных» — там она с разбором по
         подразделениям и по роду расхождения, а не одним числом в строке. */
      + '">остаток (факт 1С)</th><th class="num" title="'
      + esc('поступило − выбыло − потери переработки − недоезд − остаток по факту 1С')
      + '">невязка</th>'
      /* ФАКТ ВЫРУЧКИ (14.09.2026): регистр «Выручка и себестоимость продаж»,
         рубли легли на те же серии, что и тонны продаж. Колонка есть только
         когда регистр загружен — без него столбец пустых ячеек ни к чему. */
      + (HAS_RUB ? '<th class="num rub" title="'+esc('Выручка без НДС по регистру '
          + '«Выручка и себестоимость продаж» (по '+(META.revenue.max_date||'—')
          + '), разнесена по сериям так же, как тонны продаж')+'">выручка, ₽</th>'
          + '<th class="num rub" title="Себестоимость продаж без НДС из того же регистра">себестоимость, ₽</th>'
          + '<th class="num rub" title="выручка − себестоимость продаж; в скобках — доля от выручки">маржа, ₽</th>' : '')
      + '</tr></thead><tbody>'
      + d.map(r=>'<tr><td>'+esc(r.series_full)+'</td><td>'+esc(r.contract||'—')+'</td>'
          + cols.map(k=>'<td class="num">'+(Math.abs(r[k])>0.001?fmt(r[k]):'')+'</td>').join('')
          + '<td class="num">'+(Math.abs(r['остаток'])>0.001?fmt(r['остаток']):'')+'</td>'
          + '<td class="num'+(Math.abs(r['невязка'])>0.001?' warn':'')+'">'+fmt(r['невязка'])+'</td>'
          + (HAS_RUB ? '<td class="num rub">'+(Math.abs(r['выручка']||0)>0.5?money(r['выручка']):'')+'</td>'
              + '<td class="num rub">'+(Math.abs(r['себестоимость']||0)>0.5?money(r['себестоимость']):'')+'</td>'
              + '<td class="num rub'+(((r['выручка']||0)-(r['себестоимость']||0))<-0.5?' warn':'')+'">'
                + (Math.abs(r['выручка']||0)>0.5 ? money((r['выручка']||0)-(r['себестоимость']||0))
                    + ' <span class="muted">('+(100*((r['выручка']||0)-(r['себестоимость']||0))/r['выручка']).toFixed(0)+' %)</span>' : '')+'</td>' : '')
          + '</tr>').join('')
      + '</tbody></table>';
  }
  $('#fq').addEventListener('input',draw);
  $('#fs2').addEventListener('change',draw);
  $('#fonly').addEventListener('change',draw);
  draw();
};

/* ================================================================
   4c) ДВИЖЕНИЯ — построчно по документам (§5 ТЗ)
   Без агрегатов: документ → номенклатура → количество → откуда → куда.
   ================================================================ */
TABS.moves = function(app){
  const T = META.flow_totals||{};
  const c = el('div','cards');
  c.innerHTML =
    '<div class="card lead"><div class="k">ПРОДАЛИ покупателю</div><div class="v">'+fmt(tShow(T['продано']))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">внешнее выбытие</div></div>'
    +'<div class="card"><div class="k">Купили у поставщика</div><div class="v">'+fmt(tShow(T['куплено']))+' <span class="unit">'+esc(wLabel())+'</span></div>'
      +'<div class="s">внешний приход</div></div>'
    +'<div class="card"><div class="k">Движений в выгрузке</div><div class="v">'+cnt(MOVES.length)+'</div>'
      +'<div class="s">строк регистра, каждая показана отдельно</div></div>'
    +'<div class="card"><div class="k">Уехало ↔ приехало</div><div class="v small">'
      +fmt(tShow(T['уехало']))+' / '+fmt(tShow(T['приехало']))+'</div>'
      +'<div class="s">внутри компании; недоезд '+fmt(tShow(T['недоезд']))+' '+esc(wLabel())+'</div></div>';
  app.appendChild(c);

  const n = howto('Как читать построчные движения','');
  n.querySelector('.nb').innerHTML = '<b>Как читать.</b> Одна строка здесь = одна строка регистра 1С, '
    +'поэтому цифры сверяются с 1С один в один. Формулировки однозначные: '
    +'<i>купили у поставщика</i> · <i>уехало со склада X</i> · <i>приехало на склад Y</i> '
    +'(на базу / на цех — по типу подразделения) · <i>в производство</i> (сырьё забрали '
    +'в передел: резка, разборка, пакетирование) · <i>из производства</i> '
    +'(вернули готовую продукцию) · <i>ПРОДАЛИ покупателю</i> · '
    +'<i>возврат</i> · <i>недостачи</i> (инвентаризация) · <i>списание на затраты</i>. '
    +'Наведите курсор на подпись потока — там название документа 1С. '
    +'<b>Наведите на саму строку</b> — карточка покажет, что вышло по этому '
    +'документу (в производство ушло 350 → из производства вернулось 348) и какими документами '
    +'металл жил дальше. Расхождение стоит прямо в строке и отбито цветом: '
    +'<span class="dgap g-warn">до 2 %</span> — угар и обрезь, '
    +'<span class="dgap g-bad">больше</span> — разбираться, '
    +'<span class="dgap g-mix">шт → т</span> — сменилась единица, сверять нельзя. '
    +'Режим «цепочки по лотам» выстраивает путь металла подряд: '
    +'купили 1.406 т трубы НКТ в Когалыме → уехало 0.756 → приехало на База Осенцы → '
    +'продали 0.312 + 0.444 с Осенцов.';
  app.appendChild(n);

  const flows = [...new Set(MOVES.map(mfl))];
  const months = [...new Set(MOVES.map(r=>mst(r,'dt').slice(0,7)))].sort();
  const bar = el('div','toolbar');
  bar.innerHTML = '<input type="search" id="vq" placeholder="Поиск: серия / БП / документ / лот / склад / код / номенклатура…">'
    + measureSelect('vmeas')
    + '<select id="vf"><option value="">поток: все</option>'
      + flows.map(f=>'<option value="'+esc(f)+'">'+esc(MSHORT[f]||f)+'</option>').join('')+'</select>'
    + '<select id="vm1"><option value="">период с: начало</option>'+months.map(m=>'<option>'+m+'</option>').join('')+'</select>'
    + '<select id="vm2"><option value="">по: конец</option>'+months.map(m=>'<option>'+m+'</option>').join('')+'</select>'
    + '<select id="vmode"><option value="rows">вид: построчно</option>'
      +'<option value="lots">вид: цепочки по лотам</option></select>'
    + '<span class="right muted" id="vcnt"></span>';
  app.appendChild(bar);
  const wrap = el('div','panel'); app.appendChild(wrap);

  function draw(){
    const q=($('#vq').value||'').trim().toLowerCase();
    const fl=$('#vf').value, m1=$('#vm1').value, m2=$('#vm2').value, mode=$('#vmode').value;
    /* разрез по единице — первым, как в дереве */
    let d = MOVES.filter(mIn);
    if(fl) d = d.filter(r=>mfl(r)===fl);
    if(m1||m2) d = d.filter(r=>{const m=mst(r,'dt').slice(0,7);
      return (!m1||m>=m1)&&(!m2||m<=m2);});
    const qmv = qMatcher(q);
    if(qmv) d = d.filter(r=>{
      const code=mst(r,'code');
      return qmv((mst(r,'variant')+' '+mst(r,'doc')+' '+mst(r,'lot')+' '+lotLabel(mlotk(r))+' '+code+' '
             +(NM[code]||'')+' '+mst(r,'from')+' '+mst(r,'to')).toLowerCase());});
    const tt = sum(d, r=>mVal(r));
    $('#vcnt').textContent = cnt(d.length)+' движений · '+fmt(tt)+' '+unitLabel();
    wrap.innerHTML='';
    if(!d.length){ wrap.appendChild(el('div','hint','Под фильтры не попало ни одного движения.')); return; }
    wrap.appendChild(mode==='lots' ? lotChains(d, 40) : movesTable(d, 600));
  }
  ['#vf','#vm1','#vm2','#vmode'].forEach(s=>$(s).addEventListener('change',draw));
  $('#vmeas').addEventListener('change',()=>{ setMeasure($('#vmeas').value); rerenderTab(); });
  $('#vq').addEventListener('input',draw);
  draw();
};

/* ================================================================
   4d) ПЛАН-ФАКТ (ДИАГРАММА ГАНТА) — дискретность месяц
   План: от месяца, когда запас фактически поступил, до «ДатаВывоза» из
   справочника серий. Факт: от того же месяца до месяца последней продажи;
   если вывезено не всё — линия открыта до конца выгрузки.
   Инструмент отвечает на один вопрос: что вывезено в срок, что просрочено
   и сколько тонн ещё висит.
   ================================================================ */
const G_STATUS = {
  'в срок':        {c:'ok',   t:'весь металл вывезен не позже плановой даты'},
  'с опозданием':  {c:'late', t:'вывезен полностью, но позже плановой даты'},
  'просрочен':     {c:'bad',  t:'плановая дата прошла, а металл ещё не вывезен'},
  'в работе':      {c:'work', t:'плановая дата ещё не наступила, вывоз идёт'},
  'без плана':     {c:'none', t:'у серий проекта не заполнена «ДатаВывоза» в 1С'},
  'битая дата вывоза': {c:'none', t:'дата вывоза в 1С за пределами разумного (например 0204-09) — плана нет'},
};
/* ---------- «ПРОСРОЧЕН» — В РАЗНЫХ ВАРИАНТАХ ПРО РАЗНОЕ ----------
   Решение заказчика 13.08.2026, и оно ложится ровно на определения вариантов:
     вариант 1 «заготовка»    — расход это ВЫВОЗ за пределы заготовки
                                («уехало» + продажи + списания);
     вариант 2 «вся компания» — расход это ПРОДАЖА (перемещения внутри компании
                                расходом не считаются, они гасят друг друга).
   Значит одна и та же просрочка означает «не вывезли» в первом варианте и
   «не продали» во втором — так и подписываем.
   ⚠️ МЕНЯЕТСЯ ТОЛЬКО НАДПИСЬ. Значение статуса остаётся 'просрочен': по нему
   считаются карточки, фильтр, сортировка и цвет, и оно совпадает со статусом из
   расчёта. Разведи их — и фильтр «статус: просрочен» перестанет находить строки. */
const G_OVERDUE_BY = {v1: 'просрочен по вывозу', v2: 'просрочен по продаже'};
const statusLabel = (st, vkey) =>
  st === 'просрочен' ? (G_OVERDUE_BY[vkey] || st) : st;
const mIdx = m => m ? (+m.slice(0,4))*12 + (+m.slice(5,7)) - 1 : null;
const mStr = i => String(Math.floor(i/12)).padStart(4,'0')+'-'+String(i%12+1).padStart(2,'0');
const MONTH_RU = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
const ROMAN = ['I','II','III','IV'];

/* ---------- масштаб шкалы ----------
   У деления ФИКСИРОВАННАЯ ширина, диаграмма прокручивается по горизонтали, а
   подпись и метрики закреплены слева. Раньше все месяцы делили ширину поровну
   (repeat(n,1fr)) и на 58 месяцах деление выходило в 6–7 пикселей. */
const G_ZOOM = {
  w: {w: 26, step: 0,  name: 'недели'},
  m: {w: 40, step: 1,  name: 'месяцы'},
  q: {w: 60, step: 3,  name: 'кварталы'},
  y: {w: 108, step: 12, name: 'годы'},
};
/* ---------- НЕДЕЛИ: своё пространство корзин ----------
   ⚠️ Месяц, квартал и год кратны месяцу, поэтому для них корзина считается
   делением индекса месяца (`floor(mi/step)`). НЕДЕЛЯ В ЭТУ АРИФМЕТИКУ НЕ
   ЛОЖИТСЯ: она не делит месяц нацело и вообще пересекает его границу
   (29.06–05.07 — одна неделя в двух месяцах). Поэтому у недель СВОЙ индекс —
   номер недели от эпохи, — а `bOf` для них переводит месяц в неделю его
   первого числа. Данные при этом берутся не из `t.m`, а из `t.w`, где ключ
   уже недельный: пересчитывать месяц в недели нельзя, это разные корзины.
   Индекс: дни от 1970-01-01 плюс 3 (эпоха — четверг), делённые на 7. Так
   неделя всегда начинается с понедельника, как в ISO и как в 1С. */
const DAY = 86400000;
const wkIdxOfDate = d => Math.floor((Date.UTC(+d.slice(0,4), +d.slice(5,7)-1,
                                              +d.slice(8,10)) / DAY + 3) / 7);
const wkIdxOfKey = k => {           // «2026-W27» -> индекс недели
  const y = +k.slice(0,4), w = +k.slice(6);
  /* понедельник недели 1 = ближайший понедельник к 4 января */
  const j4 = Date.UTC(y, 0, 4), dow = (new Date(j4).getUTCDay() + 6) % 7;
  return Math.floor((j4 - dow*DAY) / DAY + 3) / 7 + (w - 1);
};
const wkMonday = b => new Date((b*7 - 3) * DAY);       // понедельник корзины
const wkNum = b => {                                    // номер недели по ISO
  const d = wkMonday(b), th = new Date(d.getTime() + 3*DAY);   // четверг недели
  const y = th.getUTCFullYear(), j4 = Date.UTC(y, 0, 4);
  const dow = (new Date(j4).getUTCDay() + 6) % 7;
  return {y, n: Math.round((th.getTime() - (j4 - dow*DAY)) / (7*DAY)) + 1};
};
const dRu = d => String(d.getUTCDate()).padStart(2,'0')+'.'
               + String(d.getUTCMonth()+1).padStart(2,'0');

/* ---------- ТОЧНЫЕ ДАТЫ НА ШКАЛЕ ----------
   ⚠️ Якорь плановой дорожки раньше был МЕСЯЦЕМ, а `bOf` переводит месяц в
   неделю его ПЕРВОГО ЧИСЛА. На недельной шкале это давало настоящую ошибку:
   БП 1892 оплачен 30.07.2026 (неделя 31), план — 1 месяц, и наш флажок вставал
   на неделю 27, то есть НА МЕСЯЦ РАНЬШЕ платежа, от которого он отсчитан.
   Поэтому якорь и наша дата считаются в ДАТАХ, а в корзину переводятся уже
   готовой датой. На месячной шкале ничего не меняется — там корзина та же. */
const dNum = d => Date.UTC(+d.slice(0,4), +d.slice(5,7)-1, +d.slice(8,10)) / DAY;
const dStr = n => { const d = new Date(n * DAY);
  return String(d.getUTCFullYear()).padStart(4,'0')+'-'
       + String(d.getUTCMonth()+1).padStart(2,'0')+'-'
       + String(d.getUTCDate()).padStart(2,'0'); };
/* дата + n месяцев; короткий месяц прижимаем к последнему дню (31.01+1 = 28.02) */
const addMon = (d, n) => {
  const i = (+d.slice(0,4))*12 + (+d.slice(5,7)) - 1 + n;
  const y = Math.floor(i/12), m = i%12;
  const last = new Date(Date.UTC(y, m+1, 0)).getUTCDate();
  return String(y).padStart(4,'0')+'-'+String(m+1).padStart(2,'0')+'-'
       + String(Math.min(+d.slice(8,10), last)).padStart(2,'0');
};
const bOfDate = (d, z) => !d ? null
                        : z==='w' ? wkIdxOfDate(d)
                        : Math.floor(mIdx(d.slice(0,7)) / G_ZOOM[z].step);
const wkMondayStr = b => dStr(b*7 - 3);
const dateRu = s => s ? s.slice(8,10)+'.'+s.slice(5,7)+'.'+s.slice(0,4) : '';

const bOf    = (mi, z) => z==='w'
                        ? wkIdxOfDate(mStr(mi)+'-01')   // месяц -> его первая неделя
                        : Math.floor(mi / G_ZOOM[z].step);
const bYear  = (b,  z) => z==='w' ? wkNum(b).y : Math.floor(b * G_ZOOM[z].step / 12);
const bLabel = (b,  z) => z==='y' ? String(bYear(b,z))
                        : z==='q' ? ROMAN[b%4]
                        : z==='w' ? String(wkNum(b).n)
                        : MONTH_RU[b%12];
const bTitle = (b,  z) => z==='y' ? bYear(b,z)+' год'
                        : z==='q' ? ROMAN[b%4]+' квартал '+bYear(b,z)
                        : z==='w' ? ('неделя '+wkNum(b).n+' ('+dRu(wkMonday(b))+'–'
                            + dRu(new Date(wkMonday(b).getTime()+6*DAY))+' '+wkNum(b).y+')')
                        : MONTH_RU[b%12]+' '+bYear(b,z);

/* ---------- варианты диаграммы ----------
   Оба варианта и вся декомпозиция собираются из ОДНОГО набора узлов
   (проект · вариант серии · склад с помесячными потоками), поэтому итог строки
   всегда равен сумме её разбора. Отличаются варианты тем, какие склады берутся
   и какие потоки считаются расходом — правила приходят из src/config.py. */
const GFLOWS = META.gantt_flows || [];
const GVARS  = (META.gantt_variants || []).map(v=>({
  key:v.key, title:v.title, hint:v.hint,
  sites:v.sites||[],
  ii:(v.in ||[]).map(x=>GFLOWS.indexOf(x)).filter(i=>i>=0),
  oo:(v.out||[]).map(x=>GFLOWS.indexOf(x)).filter(i=>i>=0),
}));
const GVAR = k => GVARS.find(v=>v.key===k) || GVARS[0];
const FSHORT = META.flow_short || {};
/* «Сальдо» — полный оборот склада (приход минус расход по ВСЕМ потокам), той же
   формулой, что колонка ОСТАТОК в дереве. Остаток берётся отсюда, а не из потоков
   варианта: иначе у 1578 на складе «Сандибинское м/р» диаграмма показывала бы
   остаток 160 т, хотя металл оттуда уехал — перемещение в потоки варианта 1
   просто не входит. */
const BIX = GFLOWS.indexOf('сальдо');

/** Узлы -> помесячно {месяц:[приход, расход, сальдо]} по правилам варианта. */
function aggNodes(nodes, V){
  const m = {};
  for(const nd of nodes){
    for(const k in nd.m){
      const v = nd.m[k], a = m[k] || (m[k] = [0,0,0]);
      for(const i of V.ii) a[0] += v[i];
      for(const i of V.oo) a[1] += v[i];
      if(BIX>=0) a[2] += v[BIX];
    }
  }
  return m;
}
/* ⚠️ НЕДЕЛЬНЫЙ РЯД ПОДСТРОК — ТОЙ ЖЕ СВЁРТКОЙ, ЧТО И МЕСЯЦЫ (найдено
   заказчиком 21.08.2026: «почему при декомпозиции нет столбцов» на недельной
   шкале). У строки проекта недельный ряд лежит в снимке готовым (v[..].w), а
   подстроки собираются здесь из узлов — и до этой функции суммировались только
   месяцы: недели узлов (nd.wk, расчёт пишет их за последние 12 месяцев) молча
   терялись, подстроки на неделях выходили без столбиков и с ложной штриховкой
   «нет недельной детализации». */
function aggNodeWeeks(nodes, V){
  const w = {};
  for(const nd of nodes){
    for(const k in (nd.wk || {})){
      const v = nd.wk[k], a = w[k] || (w[k] = [0,0,0]);
      for(const i of V.ii) a[0] += v[i];
      for(const i of V.oo) a[1] += v[i];
      if(BIX>=0) a[2] += v[BIX];
    }
  }
  return w;
}
/** Помесячный ряд -> те же итоги, что считает sales_report.py. */
function totalsOf(m){
  let bought=0, sold=0, bal=0, start='', fact='';
  const ks = Object.keys(m).sort();
  for(const k of ks){
    bought += m[k][0]; sold += m[k][1]; bal += (m[k][2]||0);
    if(!start && m[k][0]>0.0005) start = k;
    if(m[k][1]>0.0005) fact = k;
  }
  if(!start) for(const k of ks) if(m[k][1]>0.0005){ start = k; break; }
  /* `|| 0` убирает минус-ноль: −0,0004 округляется в −0 и печатался как «−0 т» */
  const rnd = x => (Math.round(x*1000)/1000) || 0;
  return {start, fact_end:fact, bought:rnd(bought), sold:rnd(sold), rest:rnd(bal),
          pct: bought>0.0005 ? Math.round(1000*sold/bought)/10 : null, m};
}
/* Статус и опоздание пересчитываются по действующему сроку теми же правилами,
   что и в sales_report.py, — иначе после правки срока строка осталась бы
   «просрочена» с новым сроком в будущем. */
const mClosed = t => t.rest <= Math.max(0.5, 0.01*Math.max(t.bought,0));
function statusOf(g, t, pmD){
  if(!pmD) return g.bad_dates ? 'битая дата вывоза' : 'без плана';
  const pm = pmD.slice(0,7);
  /* «закрыт в срок / с опозданием» остаётся ПОМЕСЯЧНЫМ: сравнивается с месяцем
     последнего расхода, дня у него в расчёте нет. */
  if(mClosed(t)) return (t.fact_end && t.fact_end <= pm) ? 'в срок' : 'с опозданием';
  /* ⚠️ «ПРОСРОЧЕН» — ПО ДНЮ, как и в sales_report.py. Помесячно план числился
     «в работе» весь месяц срока, даже если срок пришёлся на его начало: у
     БП 1864 срок 08.08.2026, выгрузка идёт по 12.08 — просрочен на четыре дня.
     Отсчёт от КОНЦА ВЫГРУЗКИ, а не от текущей даты, чтобы у всех получалось
     одно и то же. Запасной вариант (period_max нет) повторяет прежнее
     помесячное правило слово в слово. */
  return pmD < (META.period_max || ((META.today||'') + '-01'))
         ? 'просрочен' : 'в работе';
}
function delayOf(g, t, pm){
  if(!pm) return null;
  const ref = (mClosed(t) && t.fact_end) ? t.fact_end : (META.today||'');
  return mIdx(ref) - mIdx(pm);
}

/* ---------- правки срока вывоза ----------
   Источник правды — data/plan_dates.json на сервере: его подхватывает
   пересборка. Пока пересборки не было, диаграмма применяет правку прямо в
   браузере. Если дашборд открыт файлом (протокол file:), сохранять некуда —
   правки живут в localStorage этого браузера. */
const PLAN_KEY = 'metoptorg.plan_dates';
/* tools/check_1c_xlsx.py исполняет этот файл в node, где нет ни location, ни
   localStorage: обращение к ним на верхнем уровне роняет проверку выгрузки в 1С */
const PLAN_ONLINE = typeof location !== 'undefined'
  && (location.protocol === 'http:' || location.protocol === 'https:');
let PLAN_EDITS = (()=>{ try{ return JSON.parse(localStorage.getItem(PLAN_KEY)||'{}')||{}; }
                        catch(e){ return {}; } })();
function planStore(){ try{ localStorage.setItem(PLAN_KEY, JSON.stringify(PLAN_EDITS)); }catch(e){} }
function planOf(g){
  const e = PLAN_EDITS[g.series];
  if(e !== undefined && e !== null){
    const d = String(e.date||'');
    return {full:d, month:d.slice(0,7), note:e.note||'',
            src: d ? 'правка' : 'снят вручную', edited:true};
  }
  return {full:g.plan_full||'', month:g.plan_end||'', note:g.plan_note||'',
          src:g.plan_src||'', edited:(g.plan_src==='правка'||g.plan_src==='снят вручную')};
}

TABS.gantt = function(app){
  const G = (DATA.gantt||[]).slice();
  const TODAY = META.today || '';
  /* «Сейчас» с точностью до дня. TODAY — это МЕСЯЦ, и на недельной шкале он
     превращается в неделю своего первого числа: у выгрузки до 12.08 «сейчас»
     уезжало на неделю 31 июля. Для полосы хода берём конец выгрузки — это и
     есть тот момент, до которого факт вообще может что-то знать. */
  /* ⚠️ «СЕЙЧАС» — НЕ ПОЗЖЕ ДАТЫ СБОРКИ (заказчик 21.08.2026: «почему
     актуальная неделя 31, если сейчас 34»). В выгрузке 1С встречаются проводки
     БУДУЩИМ числом (21.08 в движениях лежали 6 строк за 31.08) — они утаскивали
     «конец выгрузки» и линию «сейчас» в будущее. Точка отсчёта — минимум из
     последней даты движений и даты сборки снимка: дата сборки одинакова у всех
     смотрящих, так что правило «у всех одно и то же» не ломается. */
  const NOW_D = [META.period_max, (META.generated||'').slice(0,10)]
      .filter(Boolean).sort()[0] || (TODAY ? TODAY + '-01' : '');
  /* Месяцы выгрузки — от первого до последнего, подряд, чтобы в выборе периода
     не было дыр там, где в каком-то месяце движений не случилось. */
  const MONTHS_ALL = (()=>{
    const a=mIdx((META.period_min||'').slice(0,7)), b=mIdx((META.period_max||'').slice(0,7));
    if(a==null||b==null||b<a) return [];
    const out=[]; for(let i=a;i<=b;i++) out.push(mStr(i)); return out;
  })();
  const mRu = m => MONTH_RU[+m.slice(5,7)-1]+' '+m.slice(0,4);
  /* ⚠️ ДИАГРАММА ВСЕГДА В ТОННАХ. «кг» — способ показа веса, а здесь факт
     сверяется с ПЛАНОМ вывоза, и план записан в тоннах: показать факт в
     килограммах значит сравнить 5 450 с 5,45 и покрасить всё красным.
     Разрез «килограммы» для диаграммы сводится к тоннам. */
  const GMEAS = measBase(MEASURE);
  const gUnit = () => GMEAS;

  const cardBox = el('div','cards'); app.appendChild(cardBox);
  const note = howto('Как читать диаграмму и что показывает выбранный вариант','');
  app.appendChild(note);
  app.appendChild(matLegendNote());

  const zoom0 = localStorage.getItem('metoptorg.gzoom') || 'm';
  const var0  = localStorage.getItem('metoptorg.gvar') || (GVARS[0]||{}).key || 'v1';

  const bar = el('div','toolbar');
  bar.innerHTML =
      /* вариант — ДВУМЯ КНОПКАМИ, не списком (заказчик 01.09.2026, «везде»):
         выбор из двух за один клик, активный виден без раскрытия */
      /* ⚠️ БЕЗ СЛОВА «ВАРИАНТ» И БЕЗ НОМЕРА (финдир 02.09.2026: «вариант 2
         можно прямо не писать — писать заготовка, вся компания»): на кнопке
         остаётся смысл, а номер варианта живёт в подсказке и в справке. */
      '<span class="gzoom gvars">'
      + GVARS.map(v=>'<button data-v="'+esc(v.key)+'"'
          +(v.key===var0?' class="on"':'')
          +' title="'+esc('Вариант '+v.title+'. '+(v.hint||''))+'">'
          +esc(String(v.title).replace(/^\s*\d+\s*[—-]\s*/,''))+'</button>').join('')
      +'</span>'
    + '<input type="search" id="gq" class="bpq" value="'+esc(BP_FOCUS)+'" '
      +'placeholder="Поиск: бизнес-план / договор / контрагент…">'
    + measureSelect('gmeas')
    + groupSelect('ggrp')
    + divSelect('gdiv')
    + caSelect('gca')
    /* ⚠️ VALUE У ПУНКТОВ ОБЯЗАТЕЛЕН. Без него значение берётся из текста, а
       подпись «просрочен» меняется по варианту («по вывозу» / «по продаже») —
       и фильтр перестал бы находить строки, потому что искал бы статус
       «просрочен по вывозу», которого в данных нет. */
    + '<select id="gst"><option value="">статус: все</option>'
      + Object.keys(G_STATUS).map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('')
      + '</select>'
    + '<select id="gsort"><option value="bp">сортировка: номер БП, от большего к меньшему</option>'
      + '<option value="delay">сначала самые просроченные</option>'
      + '<option value="rest">по остатку к вывозу</option>'
      /* прогноз — только если он вообще посчитан: без плана по базам этот пункт
         молча сортировал бы все строки одинаково */
      + (Object.keys(META.prod_fc||{}).length
          ? '<option value="fc">по прогнозу остатка (после плана)</option>' : '')
      + '<option value="plan">по сроку вывоза</option>'
      + '<option value="bought">по объёму закупки</option>'
      + '<option value="az">по алфавиту</option></select>'
    /* ⚠️ Отсчёт «за последние N месяцев» — от КОНЦА ВЫГРУЗКИ (META.today), а не
       от сегодняшнего дня браузера: выгрузку смотрят и через неделю после
       сборки, и «последние 3 месяца» должны означать одно и то же у всех.
       Оплаты есть только у части планов (в выгрузке 08.2026 — у 195 из 1 072),
       поэтому любой из этих фильтров сильно сужает список; сколько осталось,
       видно в счётчике справа. */
    /* ---------- ПОСТУПЛЕНИЕ: ТРИ ВОПРОСА — ТРИ ЭЛЕМЕНТА ----------
       Было: один список, в котором лежали ДВЕ разные логики (по первому завозу и
       по последнему), плюс отдельная галочка «только с поступлением», которая
       ПОВТОРЯЛА пункт «поступления не было вовсе» наизнанку. Их приходилось
       мирить оговоркой в коде, чтобы вместе они не давали пустой список.
       Стало: наличие завоза, давность ПЕРВОГО и давность ПОСЛЕДНЕГО — три
       отдельных элемента, каждый с одним смыслом. Спорить им больше не о чем:
       они сужают выборку независимо. */
    + '<select id="gpay" title="'
      + esc('Был ли по плану ВНЕШНИЙ приход металла: приобретение, оприходование '
          + 'излишков, ввод остатков, возврат от клиента. Внутренняя переработка и '
          + 'перемещение между своими складами поступлением НЕ считаются, возврат '
          + 'поставщику тоже.\n\nПо умолчанию показываются только планы с '
          + 'поступлением: по плану без завоза показывать нечего — у него нет ни '
          + 'движений, ни срока вывоза из 1С. Выберите «не важно», чтобы увидеть все.')
      + '">'
      + '<option value="recv" selected>поступление: есть</option>'
      + '<option value="">поступление: не важно</option>'
      + '<option value="none">поступления не было вовсе</option></select>'
    + '<select id="gfirst" title="'
      + esc('Когда по плану НАЧАЛИ завозить — по ПЕРВОМУ поступлению. '
          + '«За последние 3 мес.» значит, что раньше трёх месяцев назад по плану '
          + 'не завозили вообще: это новый завоз, а не старый проект с мелкими '
          + 'дозаводами. Срок считается от конца выгрузки (' + TODAY + '), а не от '
          + 'текущей даты браузера — иначе у всех получалось бы разное.')
      + '">'
      + '<option value="">первый завоз: не важно</option>'
      + [3,6,9,12].map(n=>'<option value="'+n+'">первый завоз: за последние '+n+' мес.</option>').join('')
      + '</select>'
    + '<select id="glast" title="'
      + esc('Как давно завозили В ПОСЛЕДНИЙ РАЗ — залежался ли план. Если металл '
          + 'пришёл месяц назад, план не залежался, сколько бы лет проекту ни было. '
          + 'Полосы не пересекаются и покрывают всё; план, по которому завоза не '
          + 'было вовсе, ни в одну полосу не попадает — у него нет давности, для '
          + 'него свой пункт в списке слева.')
      + '">'
      + '<option value="">последний завоз: не важно</option>'
      + [[0,'меньше 3 мес. назад'],[3,'3–6 мес. назад'],[6,'6–9 мес. назад'],
         [9,'9–12 мес. назад'],[12,'больше 12 мес. назад']]
        .map(b=>'<option value="age'+b[0]+'">последний завоз: '+esc(b[1])+'</option>').join('')
      + '</select>'
    /* ⚠️ ГЛУБИНА ПРОСРОЧКИ — НАКОПИТЕЛЬНО («от 3 и далее»), в отличие от полос
       завоза, которые не пересекаются. Здесь это правильно: фильтр ничего не
       складывает, а «покажи всё, что просрочено больше полугода» — ровно тот
       вопрос, который задают. Заказчик так и просил: «от 3, от 6, от 9 и до
       конца». В сводной те же месяцы разложены НЕпересекающимися полосами —
       там столбец складывают, и накопительные строки сложить было бы нельзя. */
    + '<select id="gover" title="'
      + esc('Оставить бизнес-планы по глубине просрочки. Глубина считается от '
          + 'срока вывоза 1С до конца выгрузки — то же число, что в колонке '
          + '«ОПОЗД.». Пункты «от N и далее» — накопительные; пункты «полоса» — '
          + 'ТОЧНЫЕ интервалы, те же, что на «Дашборде» и «Сводных» (переход с '
          + 'дашборда ставит именно полосу). Планы без срока вывоза сюда не '
          + 'попадают: у них нет просрочки, а не «нулевая».')
      + '">'
      + '<option value="">просрочка: не важно</option>'
      + [3,6,9,12].map(n=>'<option value="'+n+'">просрочка: от '+n+' мес. и далее</option>').join('')
      + '<option value="b0">полоса: до 3 мес.</option>'
      + '<option value="b3">полоса: 3–6 мес.</option>'
      + '<option value="b6">полоса: 6–9 мес.</option>'
      + '<option value="b9">полоса: 9–12 мес.</option>'
      + '<option value="b12">полоса: больше 12 мес.</option>'
      + '</select>'
    /* Оплатили — а металл стоит. Заказчик 13.08.2026: «видно, что оплата прошла,
       а вывоза нет уже 3 недели». Считаем по ДНЯМ от конца выгрузки, потому что
       три недели внутри месяца помесячной шкалой не поймать. */
    + '<label class="chip" style="cursor:pointer" title="'
      + esc('Оплата по плану прошла, а расхода по правилам варианта с тех пор не '
          + 'было уже больше трёх недель. В варианте «заготовка» расход — это вывоз '
          + 'с площадки, в варианте «вся компания» — продажа. Считается от ПЕРВОЙ '
          + 'оплаты и до конца выгрузки; если после оплаты хоть что-то уехало, план '
          + 'сюда не попадает.')
      + '"><input type="checkbox" id="gnomove"> оплачено, вывоза нет 3 нед.</label>'
    /* Вид плана в рамках. Показывается только если план вообще загружен —
       иначе это мёртвый переключатель, который нечем объяснить. */
    + ((META.prod_bps||0) ? ('<select id="gprod" title="'
      + esc('Какой вид планового объёма показывать рамками на диаграмме. '
          + '⚠️ «Все вместе» — это СУММА видов, и она завышена по своей природе: '
          + 'приход, вывоз с цеха и отгрузка — один и тот же металл на разных '
          + 'стадиях пути, у многих планов они совпадают до килограмма. Чтобы '
          + 'увидеть настоящий объём одной стадии, выберите её отдельно. '
          + 'План есть у ' + (META.prod_bps||0) + ' бизнес-планов, месяцы '
          + ((META.prod_months||[])[0]||'—') + ' … '
          + ((META.prod_months||[])[(META.prod_months||[]).length-1]||'—') + '.')
      + '">'
      + '<option value="">план: не показывать</option>'
      + '<option value="all">план: все виды вместе</option>'
      + Object.keys(META.prod_ops||{}).map(k=>'<option value="'+esc(k)+'">план: '
          + esc((META.prod_ops||{})[k]) + '</option>').join('')
      + '</select>') : '')
    /* Прогноз по базам. Тоже только при загруженном плане: без него на строках
       нечего штриховать, а объяснить пустую галочку нечем. */
    + (Object.keys(META.prod_fc||{}).length ? ('<label class="chip" style="cursor:pointer" title="'
      + esc('Штриховать на шкале месяцы, в которые НАШ ПЛАН съедает остаток '
          + 'этого бизнес-плана на базе, и ставить подпись «останется N т» сразу '
          + 'за планом. Остаток берётся на 1-е число месяца-якоря ('
          + (META.prod_anchor||META.prod_m0||'—')
          + '), из него вычитается план ' + (META.prod_horizon||[]).join(', ')
          + '. Плановое выбытие базы раскладывается по бизнес-планам ФИФО — '
          + 'от самых ранних завозов. ⚠️ Считается ПО БАЗАМ: у планов, чей '
          + 'остаток лежит на заготовке, площадке или в цехе, прогноза нет — '
          + 'плана по ним в выгрузке не заведено.')
      + '"><input type="checkbox" id="gfc"> прогноз по базам</label>') : '')
    + '<label class="chip" style="cursor:pointer"><input type="checkbox" id="gopen"> только с остатком</label>'
    + '<label class="chip" style="cursor:pointer"><input type="checkbox" id="gplan"> только со сроком</label>'
    /* ⚠️ ОКНО ШКАЛЫ. Дискретность (месяц/квартал/год) отвечает за ШИРИНУ
       деления, а не за то, какой кусок времени показан: на 63 месяцах шкала
       уезжала на тысячи пикселей, и увидеть нужный год можно было только
       прокруткой. Здесь выбирается сам ОТРЕЗОК — с какого по какой месяц.
       Пусто с обеих сторон = как было, вся выгрузка. */
    + '<span class="grange" title="Показать только часть шкалы: с какого по '
      + 'какой месяц. Пусто — вся выгрузка.">'
      + '<select id="gm1"><option value="">период: весь</option>'
      + MONTHS_ALL.map(m=>'<option value="'+m+'">с '+esc(mRu(m))+'</option>').join('')+'</select>'
      + '<select id="gm2"><option value="">…</option>'
      + MONTHS_ALL.map(m=>'<option value="'+m+'">по '+esc(mRu(m))+'</option>').join('')+'</select>'
      + '</span>'
    + '<span class="gzoom" title="Дискретность шкалы">'
      + Object.keys(G_ZOOM).map(z=>'<button data-z="'+z+'"'+(z===zoom0?' class="on"':'')+'>'
          +G_ZOOM[z].name+'</button>').join('')+'</span>'
    /* Кнопки, а не пункт в списке сортировки: заказчик щёлкает ими туда-сюда,
       сравнивая свежую просрочку с застарелой. Нажатие по активной снимает. */
    + '<span class="gzoom godsort" title="'
      + esc('Поднять наверх просроченные бизнес-планы выбранной глубины. Это '
          + 'СОРТИРОВКА, а не фильтр: остальные строки никуда не деваются, они '
          + 'остаются ниже. Глубина — месяцы между сроком вывоза и концом '
          + 'выгрузки, то же число, что в колонке «ОПОЗД.». Повторное нажатие '
          + 'на ту же кнопку возвращает обычный порядок.') + '">'
      + '<button data-od="near">просрочка до 3 мес.</button>'
      + '<button data-od="deep">больше 3 мес.</button></span>'
    + '<button id="gslim" class="btn3" title="Убрать колонки с цифрами, кроме остатка, '
      +'и отдать освободившуюся '
      +'ширину шкале. Цифры остаются в подсказках.">↔ шире диаграмму</button>'
    + '<span class="right muted" id="gcnt"></span>';
  app.appendChild(bar);

  const wrap = el('div','panel'); app.appendChild(wrap);
  let LIMIT = 200;
  let zoom = zoom0 in G_ZOOM ? zoom0 : 'm';
  /* '' — обычный порядок, 'near' — просрочка до 3 мес. наверх, 'deep' — больше 3.
     Намеренно НЕ запоминается в localStorage, в отличие от масштаба: это разовый
     разбор, а не постоянный режим работы. Вернулся к отчёту — обычный порядок. */
  let odSort = '';
  /* Какой вид плана показывает рамка. 'all' — сумма всех видов (выбор заказчика
     14.08.2026), остальные значения — ключи META.prod_ops.
     ⚠️ Сумма видов ЗАВЫШЕНА по своей природе: приход, вывоз с цеха и отгрузка —
     это один и тот же металл на разных стадиях пути, и у многих планов они
     совпадают до килограмма (БП 1659: приход 273,35 и вывоз 273,35). В подсказке
     об этом написано прямо, а фильтр позволяет взять один вид. */
  let PROD_OP = localStorage.getItem('metoptorg.prodop') || 'all';
  /* Прогноз остатка по базам: штриховать ли на шкале месяцы, которые план
     съедает. Колонка «прогноз» показывается всегда — это цифра, она не мешает;
     штриховка же ложится поверх дорожки плана, поэтому включается отдельно.
     Умолчание — ВКЛЮЧЕНО: ради этого прогноз и заказан. */
  let FC_ON = localStorage.getItem('metoptorg.gfc') !== '0';
  let vkey = GVARS.some(v=>v.key===var0) ? var0 : (GVARS[0]||{}).key;
  let slim = localStorage.getItem('metoptorg.gslim') === '1';
  const slimBtn = $('#gslim');
  function applySlim(){
    slimBtn.textContent = slim ? '↔ вернуть цифры' : '↔ шире диаграмму';
    slimBtn.classList.toggle('on', slim);
    const b = wrap.querySelector('.gantt');
    if(b) b.classList.toggle('slim', slim);
  }
  slimBtn.addEventListener('click', ()=>{
    slim = !slim;
    try{ localStorage.setItem('metoptorg.gslim', slim?'1':'0'); }catch(e){}
    applySlim();
    /* Ширина деления считается в draw() от свободного места
       (clientWidth − подпись − метрики). Одного переключения класса мало:
       колонка метрик ужималась до остатка, а шкала оставалась прежней ширины и
       освободившееся место пустовало. Поэтому перерисовываем. */
    closePop();
    draw();
  });

  /* ---------- редактор срока вывоза ---------- */
  let POP = null;
  function closePop(){ if(POP){ POP.remove(); POP = null; } }
  document.addEventListener('click', ev=>{
    if(POP && !ev.target.closest('.gpop') && !ev.target.closest('.gpen')) closePop();
  });
  document.addEventListener('keydown', ev=>{ if(ev.key==='Escape') closePop(); });

  async function savePlan(g, payload){
    if(payload.reset) delete PLAN_EDITS[g.series];
    else PLAN_EDITS[g.series] = {date: payload.date||'', note: payload.note||''};
    planStore();
    if(!PLAN_ONLINE)
      return {ok:false, msg:'Дашборд открыт файлом — правка сохранена только в этом браузере.'};
    try{
      const r = await fetch('/api/plan-dates', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(Object.assign({series:g.series, name:g.name}, payload))});
      if(!r.ok) throw new Error('сервер ответил '+r.status);
      return {ok:true, msg:'Сохранено. В расчёт и Excel попадёт после пересборки.'};
    }catch(e){
      return {ok:false, msg:'Не удалось сохранить на сервер ('+(e.message||e)
                          + '). Правка осталась в этом браузере.'};
    }
  }

  function openPlanEditor(g, anchor){
    closePop();
    const p = planOf(g);
    const pop = el('div','gpop');
    pop.innerHTML =
        '<div class="gpop-h">Срок вывоза</div>'
      + '<div class="gpop-n" title="'+esc(g.name)+'">'+esc(g.name)+'</div>'
      + '<label>Плановая дата вывоза<input type="date" id="pd" value="'+esc(p.full)+'"></label>'
      + '<label>Почему изменён<input type="text" id="pn" maxlength="120" '
        +'value="'+esc(p.note)+'" placeholder="основание правки"></label>'
      + '<div class="gpop-b"><button id="pok" class="pri">Сохранить</button>'
        + '<button id="pclr">Срока нет</button>'
        + (p.edited?'<button id="prst">Вернуть из 1С</button>':'')
        + '<button id="pcan">Отмена</button></div>'
      + '<div class="hint" id="pmsg">'
        + (g.plan_1c ? 'В 1С: <b>'+esc(g.plan_1c)+'</b>'
                     + (g.plans>1?' (позднейший из '+g.plans+' серий проекта)':'')
                     : 'В 1С срок вывоза не заполнен')
        + (PLAN_ONLINE ? '' : ' · страница открыта файлом, сохранение только локальное')
        + '</div>';
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.top  = (window.scrollY + r.bottom + 6) + 'px';
    pop.style.left = Math.min(window.scrollX + r.left - 60,
                              window.scrollX + document.documentElement.clientWidth
                              - pop.offsetWidth - 12) + 'px';
    POP = pop;
    const msg = pop.querySelector('#pmsg');
    /* ВАЖНО: draw() окно не закрывает — иначе результат сохранения и сообщение
       об ошибке не успели бы показаться */
    const done = async payload=>{
      const res = await savePlan(g, payload);
      msg.innerHTML = (res.ok?'✓ ':'⚠ ') + esc(res.msg);
      msg.className = 'hint ' + (res.ok?'ok':'warn');
      draw();
      setTimeout(closePop, res.ok ? 900 : 4000);
    };
    pop.querySelector('#pok').onclick = ()=> done({date: pop.querySelector('#pd').value,
                                                   note: pop.querySelector('#pn').value});
    pop.querySelector('#pclr').onclick = ()=> done({date:'', note: pop.querySelector('#pn').value});
    pop.querySelector('#pcan').onclick = closePop;
    const rst = pop.querySelector('#prst');
    if(rst) rst.onclick = ()=> done({reset:true});
    pop.querySelector('#pd').focus();
  }

  /* ---------- ЛЕНТА КОММЕНТАРИЕВ ПО ЗАПАСАМ ----------
     Заказано 20.08.2026. В 1С по каждому остатку экономист ведёт живой
     комментарий («не вывозим, деньги вернули», «заберём с новыми объёмами»), и
     до сих пор их не было видно нигде: любой вопрос «почему тут остаток второй
     год» упирался в звонок человеку.
     ⚠️ ЭТО ЛЕНТА, А НЕ ОДНА ПОДПИСЬ. В выгрузке 39 срезов за полтора года, одна
     позиция комментируется до восьми раз, а у бизнес-плана реплик бывает за
     две сотни. Смысл как раз в том, КАК МЕНЯЛАСЬ причина, поэтому показываем всё
     от свежего к старому, а не последнюю строчку.
     ⚠️ Реплики идут ПО ПОЗИЦИЯМ (склад · номенклатура), и подпись позиции
     повторяется только когда она СМЕНИЛАСЬ: иначе лента из одинаковых заголовков
     читается как список складов, а не как история. */
  /* Комментарии уровня разбора: реплики тех складов, чьи узлы стоят на этой
     строке. Другой связи нет и не надо: склад в выгрузке комментариев называется
     так же, как в движениях (проверено: 7 168 из 7 179 строк). */
  function notesOf(g, nds){
    if(!g.cm) return [];
    const W = new Set(nds.map(n=>n.w));
    return g.cm.filter(c=>W.has(c.w));
  }
  function openNotes(g, anchor, list, label){
    closePop();
    /* ⚠️ ОДИН КОММЕНТАРИЙ 1С ПИШЕТ СРАЗУ НА ВСЕ ПОЗИЦИИ ПЛАНА, и в выгрузке он
       повторён столько раз, сколько у плана складов и номенклатур. У БП 1851
       235 реплик — это ЧЕТЫРЕ разных текста, размноженных на 53 позиции; у
       БП 1205 из 264 реплик разных 21. Показывать их подряд — значит заставить
       листать одну и ту же фразу полсотни раз, поэтому лента собирается по
       ПАРЕ «дата + текст», а позиции сворачиваются в одну строчку с тоннами. */
    const src = list || g.cm || [];
    /* ⚠️ ЛЕНТА ОДНОГО СКЛАДА ГРУППИРУЕТСЯ ПО НОМЕНКЛАТУРЕ (заказчик 20.08.2026:
       комментарии в разборе открываются кнопкой у остатка, как на строке БП, а
       декомпозиция «до номенклатуры включительно» живёт внутри окна). Когда все
       реплики среза с одного склада, писать его в каждой строке бессмысленно —
       ведёт номенклатура, под ней её история. На уровнях выше (база, тип
       площадки, весь БП) склады разные — там группировка по «дата + текст». */
    if(new Set(src.map(c=>c.w)).size === 1 && src.length){
      const byN = new Map();
      src.forEach(c=>{ const k=c.n||'—';
        (byN.get(k)||byN.set(k,[]).get(k)).push(c); });
      const pop = el('div','gpop cmpop');
      let h = '<div class="gpop-h">Комментарии по запасам · '+cnt(src.length)+'</div>'
        + '<div class="gpop-n" title="'+esc(g.name)+'">'
        + (label ? '<b>'+esc(label)+'</b> · ' : '') + esc(g.name)+'</div>'
        + '<div class="cmlist">';
      [...byN.keys()].sort((a,b)=>a.localeCompare(b,'ru')).forEach(n=>{
        const seen = new Set();
        h += '<div class="cmi"><div class="cmp nom">'+esc(n)+'</div>'
          + byN.get(n).map(c=>{
              const k=c.d+'\u0000'+c.t;
              if(seen.has(k)) return '';
              seen.add(k);
              return '<div class="cmt"><i>'+esc(dateRu(c.d))+'</i>'
                + (c.u?'<em class="cmu">'+esc(c.u)+'</em>':'')+esc(c.t)
                + (c.q!=null&&Math.abs(c.q)>0.0005
                    ? ' <b class="cmq">'+fmt(c.q)+' т</b>':'')+'</div>';
            }).join('')
          + '</div>';
      });
      h += '</div><div class="hint">Ведут экономисты в 1С. Выгрузка от '
        + esc(dateRu(META.notes_dump||'')) + ', срезы по '
        + esc(dateRu(META.notes_last||'')) + '.</div>';
      pop.innerHTML = h;
      document.body.appendChild(pop);
      placeNotes(pop, anchor);
      POP = pop;
      return;
    }
    const gr = [], byKey = {};
    src.forEach(c=>{
      const k = c.d + '\u0000' + c.t;
      let a = byKey[k];
      if(!a){ a = byKey[k] = {d:c.d, t:c.t, pos:[], who:[], q:0}; gr.push(a); }
      a.pos.push((c.w||'—') + ' · ' + (c.n||'—'));
      /* кто ведёт: в выгрузке нет колонки с ФИО, подпись — подразделение
         (Пермь, Когалым…); появится «Автор» в 1С — расчёт подставит его сам */
      if(c.u && !a.who.includes(c.u)) a.who.push(c.u);
      if(c.q != null) a.q += c.q;
    });
    const CAP = 30;                        /* сразу столько групп, дальше по кнопке */
    const pop = el('div','gpop cmpop');
    const head = '<div class="gpop-h">Комментарии по запасам · ' + gr.length
      + (gr.length !== src.length
          ? ' <span class="cmw">(' + src.length + ' записей в 1С — '
            + 'один комментарий проставлен на все позиции плана)</span>' : '')
      + '</div>'
      + '<div class="gpop-n" title="'+esc(g.name)+'">'
      + (label ? '<b>'+esc(label)+'</b> · ' : '') + esc(g.name)+'</div>';
    const item = a => {
      const uniq = [...new Set(a.pos)];
      const posTxt = uniq.length === 1 ? uniq[0]
        : uniq.length + ' ' + plural(uniq.length, 'позиция', 'позиции', 'позиций')
          + ': ' + uniq[0] + (uniq.length>1 ? ' и ещё ' + (uniq.length-1) : '');
      return '<div class="cmi">'
           + '<div class="cmt"><i>'+esc(dateRu(a.d))+'</i>'
           + (a.who.length?'<em class="cmu" title="кто ведёт (подразделение из 1С; колонки с ФИО автора в выгрузке нет)">'
             +esc(a.who.join(', '))+'</em>':'')
           + esc(a.t)+'</div>'
           + '<div class="cmp" title="'+esc(uniq.join('\n'))+'">'+esc(posTxt)
             + (Math.abs(a.q) > 0.0005 ? ' · <b>'+fmt(a.q)+' т</b> на эту дату' : '')
           + '</div></div>';
    };
    const body = shown => shown.map(item).join('')
      + (gr.length>shown.length
          ? '<button class="cmmore">показать остальные '+(gr.length-shown.length)+'</button>'
          : '');
    pop.innerHTML = head + '<div class="cmlist">' + body(gr.slice(0,CAP)) + '</div>'
      + '<div class="hint">Ведут экономисты в 1С. Выгрузка от '
      + esc(dateRu(META.notes_dump||'')) + ', срезы по ' + esc(dateRu(META.notes_last||''))
      + '. Отчёт их не правит — только показывает.</div>';
    document.body.appendChild(pop);
    const more = pop.querySelector('.cmmore');
    if(more) more.addEventListener('click', ev=>{
      ev.stopPropagation();
      pop.querySelector('.cmlist').innerHTML = body(gr); });
    placeNotes(pop, anchor);
    POP = pop;
  }
  /* ⚠️ ОКНО НЕ НАКЛАДЫВАЕТСЯ НА ЯКОРЬ (заказчик 20.08.2026): если вниз не
     влезает — открывается ВВЕРХ от кнопки; по горизонтали прижимается к краям
     окна. Раньше длинная лента внизу экрана уезжала за край и перекрывала
     строки под собой. */
  function placeNotes(pop, anchor){
    const r = anchor.getBoundingClientRect();
    const vh = document.documentElement.clientHeight;
    const top = (r.bottom + 6 + pop.offsetHeight > vh
                 && r.top - pop.offsetHeight - 6 > 0)
      ? r.top - pop.offsetHeight - 6 : r.bottom + 6;
    pop.style.top  = (window.scrollY + top) + 'px';
    pop.style.left = Math.max(window.scrollX + 8,
                     Math.min(window.scrollX + r.left - 40,
                              window.scrollX + document.documentElement.clientWidth
                              - pop.offsetWidth - 12)) + 'px';
  }

  /* ---------- ячейки шкалы ---------- */
  /* ---------- ПЛАН ПО ОБЪЁМАМ ПРОИЗВОДСТВА -> РАМКА НА ДЕЛЕНИИ ----------
     Заказан 14.08.2026. План месячный, а шкала бывает недельной, квартальной и
     годовой, поэтому корзину и месяц надо связывать явно:
       * месяц/квартал/год — в корзину попадает несколько месяцев, складываем;
       * недели — неделя относится к месяцу своего ПОНЕДЕЛЬНИКА. Неделя
         29.06–05.07 достаётся июню: делить месячный план по дням не на чем.
     ⚠️ РАМКА РИСУЕТСЯ ПО ВСЕМУ МЕСЯЦУ, а на недельной шкале месяц — это 4–5
     делений. Поэтому боковые грани ставятся только на КРАЙНИХ делениях месяца,
     иначе вместо одной рамки вышел бы частокол из пяти. */
  /* Подсказка деления: из чего сложился план и честная оговорка про сумму. */
  /* Подпись в рамке — КОРОТКАЯ. «120,000» в деление шириной 40 px не влезает и
     обрезается на полуслове; точное число всегда есть в подсказке. */
  /* ⚠️ ТЫСЯЧИ ПОДПИСЫВАЮТСЯ «тыс», А НЕ «т». Было «3,1т» для 3 083 тонн — это
     читается как «три тонны с небольшим», то есть в тысячу раз меньше правды,
     и рядом в том же ряду стоят настоящие тонны без суффикса («120»). Поймано
     на подписи прогноза: 3 083,532 т превращались в «3,1т». */
  function prodShort(v){
    const a = Math.abs(v);
    if(a >= 1000) return (v/1000).toFixed(1).replace('.', ',') + 'тыс';
    if(a >= 10)   return String(Math.round(v));
    return v.toFixed(1).replace('.', ',');
  }
  function prodTitle(prod, b, sc){
    const mons = [];
    if(sc.zoom === 'w'){ mons.push(wkMondayStr(b).slice(0,7)); }
    else { const st = G_ZOOM[sc.zoom].step;
           for(let i=b*st; i<(b+1)*st; i++) mons.push(mStr(i)); }
    const OPS = META.prod_ops || {};
    const acc = {};
    mons.forEach(mo=>{ const o = prod[mo]; if(!o) return;
      for(const k in o) acc[k] = (acc[k]||0) + o[k]; });
    const keys = Object.keys(acc);
    if(!keys.length) return '';
    let t = '\nПЛАН ПО ОБЪЁМАМ, ' + mons.join(', ') + ':';
    keys.forEach(k=>{ t += '\n  ' + (OPS[k]||k) + ' — ' + fmt(acc[k]) + ' т'; });
    if(PROD_OP === 'all' && keys.length > 1)
      t += '\n⚠️ Показана СУММА видов. Это один и тот же металл на разных стадиях'
         + '\n   пути (пришёл -> вывезли с цеха -> отгрузили), поэтому сумма'
         + '\n   больше того, что реально проедет. Выберите один вид в фильтре'
         + '\n   «план», чтобы увидеть его отдельно.';
    t += '\nПлан заведён на бизнес-план ЦЕЛИКОМ: в разборе та же рамка стоит на'
       + '\nкаждой строке с остатком, объём по складам не делится.';
    return t;
  }
  function prodAt(prod, b, sc, op){
    if(!prod) return null;
    let v = 0, has = false, first = false, last = false;
    const take = mo => {
      const o = prod[mo]; if(!o) return;
      if(op === 'all'){ for(const k in o){ v += o[k]; has = true; } }
      else if(o[op] != null){ v += o[op]; has = true; }
    };
    if(sc.zoom === 'w'){
      const mo = wkMondayStr(b).slice(0,7);
      take(mo);
      if(!has) return null;
      first = wkMondayStr(b-1).slice(0,7) !== mo;
      last  = wkMondayStr(b+1).slice(0,7) !== mo;
    } else {
      const step = G_ZOOM[sc.zoom].step;
      for(let i = b*step; i < (b+1)*step; i++) take(mStr(i));
      if(!has) return null;
      first = last = true;                 /* корзина целиком одна */
    }
    return {v, first, last};
  }
  /* ---------- ПРОГНОЗ ОСТАТКА ПО БАЗАМ: ШТРИХОВКА СЪЕДАЕМЫХ МЕСЯЦЕВ ----------
     Заказано 14.08.2026. Расчёт разложил плановое выбытие базы по бизнес-планам
     ФИФО (от самых ранних завозов) — здесь это только рисуется:
       * месяцы, в которые план съедает остаток ЭТОЙ строки, — штрихуются;
       * в первом делении после плана стоит подпись «останется N т».
     ⚠️ Подпись ставится ПОСЛЕ последнего штрихованного деления, а не внутри
     него: внутри она читалась бы как «в этом месяце уйдёт столько», то есть
     ровно наоборот. Если следующее деление за краем шкалы — подпись не рисуем,
     число всегда есть в колонке «прогноз» и в подсказке. */
  /* якорь прогноза и оговорка про устаревший план — в одном месте, чтобы все
     подсказки говорили одно и то же */
  const FC_A = () => META.prod_anchor || META.prod_m0 || '—';
  const FC_STALE = () => (META.prod_anchor && META.prod_m0
      && META.prod_anchor !== META.prod_m0)
    ? ' ⚠️ Выгрузка плана устарела: плановые месяцы ('
      + (META.prod_horizon||[]).filter(m=>m<META.prod_anchor).join(', ')
      + ') уже прожиты фактом, их план не вычитается — факт и так в остатке. '
      + 'Прогноз оживёт со свежей выгрузкой «Объемов на загрузку».'
    : '';
  function fcAt(fc, b, sc){
    if(!FC_ON || !fc || !fc.m) return null;
    let v = 0, has = false, first = false;
    const take = mo => { if(fc.m[mo] != null){ v += fc.m[mo]; has = true; } };
    if(sc.zoom === 'w'){
      const mo = wkMondayStr(b).slice(0,7);
      take(mo);
      if(!has) return null;
      first = wkMondayStr(b-1).slice(0,7) !== mo;
    } else {
      const st = G_ZOOM[sc.zoom].step;
      for(let i = b*st; i < (b+1)*st; i++) take(mStr(i));
      if(!has) return null;
      first = true;
    }
    return {v, first};
  }
  function fcTitle(fc, v){
    const B = (META.prod_fc || {})[fc.b] || null;
    const H = META.prod_horizon || [];
    let t = '\nПРОГНОЗ ПО БАЗЕ «' + fc.b + '»'
          + (fc.bn > 1 ? ' (и ещё ' + (fc.bn-1) + ' — остаток плана лежит на нескольких базах)' : '')
          + ':\n  в этом делении план съедает ' + fmt(v) + ' т остатка этого БП;'
          + '\n  остаток БП на 01.' + FC_A() + ' — ' + fmt(fc.b0) + ' т,'
          + '\n  после плана (' + H.join(', ') + ') останется ' + fmt(fc.left) + ' т.';
    if(B) t += '\nВСЯ БАЗА: остаток ' + fmt(B.b0) + ' т, план выбытия '
             + fmt(H.reduce((s,m)=>s+(B.out[m]||0),0)) + ' т, план поступления '
             + fmt(H.reduce((s,m)=>s+(B.in[m]||0),0)) + ' т, бизнес-планов ' + B.bps + '.';
    t += '\n⚠️ Очередь ФИФО — по первому завозу бизнес-плана: план снимает с базы'
       + '\n   то, что легло раньше.';
    return t;
  }
  function cellsHtml(m, sc, sp, pay, w, wkm, prod, fc){
    const agg = {};
    /* ⚠️ В РЕЖИМЕ НЕДЕЛЬ ДАННЫЕ БЕРУТСЯ ИЗ СВОЕГО РЯДА (`w`), А НЕ ИЗ МЕСЯЦЕВ.
       Разложить месяц по неделям невозможно: внутри месяца неизвестно, в какой
       день что произошло, а неделя вдобавок пересекает границу месяца. Расчёт
       складывает недели отдельно (C.GANTT_WEEKS_MONTHS) — здесь их только
       раскладываем по корзинам. */
    const src = sc.zoom==='w' ? (wkm || {}) : m;
    for(const k in src){
      const b = sc.zoom==='w' ? wkIdxOfKey(k) : bOf(mIdx(k), sc.zoom);
      const v = src[k];
      const a = agg[b] || (agg[b] = [0,0]);
      a[0]+=v[0]; a[1]+=v[1];
    }
    /* ОПЛАТЫ — свойство бизнес-плана целиком: в выгрузке платёж привязан к БП, а
       не к складу, поэтому значки стоят только на строке проекта и в разбор не
       спускаются (иначе одна и та же сумма задвоилась бы по складам). */
    const pagg = {};
    if(pay && sc.zoom==='w'){
      /* у платежей есть ТОЧНАЯ дата, поэтому в неделю они кладутся без потерь
         (в отличие от движений, которые расчёт агрегирует заранее) */
      for(const p of (pay.d || [])){
        const b = wkIdxOfDate(p.d);
        const a = pagg[b] || (pagg[b] = [0,0]);
        a[0]+=p.s; a[1]+=1;
      }
    } else if(pay && pay.m){
      for(const k in pay.m){
        const i = mIdx(k); if(i==null) continue;
        const b = bOf(i, sc.zoom), v = pay.m[k];
        const a = pagg[b] || (pagg[b] = [0,0]);
        a[0]+=v[0]; a[1]+=v[1];
      }
    }
    /* Столбик один — РАСХОД, высота в процентах от самого крупного расхода
       ЭТОЙ строки. Приход отдельной меткой не рисуем: месяц поступления и так
       виден началом дорожки плана, а вторая серая метка рядом с цветной осью
       только рябила. Тонны прихода и расхода — в подсказке деления. */
    let mx=0; for(const b in agg) mx = Math.max(mx, agg[b][1]);
    /* последнее деление, которое съедает план: подпись «останется» стоит СРАЗУ
       ЗА ним, поэтому границу надо знать до отрисовки строки */
    let fcLast = null;
    if(FC_ON && fc && fc.m)
      for(let b=sc.b0;b<=sc.b1;b++) if(fcAt(fc, b, sc)) fcLast = b;
    let out='';
    for(let b=sc.b0;b<=sc.b1;b++){
      const a = agg[b];
      const inFact   = (sp.sB!=null && b>=sp.sB && b<=sp.eB);
      const openTail = sp.open && sp.sB!=null && b>sp.eB && b<=(sc.tB==null?sc.b1:sc.tB);
      const hOut = a && a[1]>0.0005 ? Math.max(6, Math.round(100*a[1]/(mx||1))) : 0;
      /* сетка: граница года — заметная, граница квартала — волосяная.
         Помесячная при узком делении сливалась в серую заливку. */
      const yb = b>sc.b0 && bYear(b,sc.zoom)!==bYear(b-1,sc.zoom);
      const qb = !yb && sc.zoom==='m' && b%3===0;
      /* ---- ЦВЕТ ДЕЛЕНИЯ решает его положение относительно СРОКА ВЫВОЗА,
         а не статус строки целиком ----
         Раньше статус выставлялся на весь контейнер, и у бизнес-плана
         «с опозданием» жёлтой становилась вся шкала — включая месяцы, которые
         уложились в срок. Теперь: деление до срока включительно — «в срок»
         (зелёное), деление после срока — цветом опоздания этой строки.
         ЧИП СТАТУСА в подписи не меняется: он считается по плану целиком,
         поэтому зелёное начало шкалы у строки «с опозданием» — это норма,
         а не рассогласование.
         Срока нет (pB == null) и «в работе» (срок ещё не наступил) — цвет
         остаётся от контейнера, как был. */
      const overdue = sp.pB!=null && b>sp.pB;
      let cc = '';
      if(sp.pB!=null && sp.cls!=='work'){
        cc = !overdue ? 'c-ok '
           : sp.cls==='bad'  ? 'c-bad '
           : sp.cls==='late' ? 'c-late ' : '';
      }
      /* Штриховка «вывоз не закончен» краснеет только за сроком: у строки
         «в работе» срок впереди, и красная штриховка читалась бы как
         просрочка, которой нет. */
      const oBad = openTail && overdue;
      /* Единая плановая дорожка: обе даты задают цвет её участков. */
      const wc = w ? ownCls(b, w) : '';
      const pf = prodAt(prod, b, sc, PROD_OP);
      const wStart = wc && b===w.aB ? 'ws ' : '';
      /* флажки НЕ ограничены концом дорожки: у закрытого плана линия
         обрывается на последнем вывозе, а срок остаётся на своём месте */
      const wEnd = w && b===w.oB && b>=w.aB;
      const lEnd = w && w.lB!=null && b===w.lB && b>=w.aB;
      const fq = fcAt(fc, b, sc);
      const fcEnd = fcLast != null && b === fcLast + 1;
      const tip = (a ? bTitle(b,sc.zoom)+': расход '+fmt3(a[1])+' '+gUnit()+' (приход '+fmt3(a[0])+' '+gUnit()+')'
                    : bTitle(b,sc.zoom)+': движений нет')
                + (wc ? ownTitle(b, w, sc.zoom) : '')
                + (pf ? prodTitle(prod, b, sc) : '')
                + (fq ? fcTitle(fc, fq.v) : '')
                + (fcEnd ? '\nПОСЛЕ ПЛАНА остаётся ' + fmt(fc.left) + ' т этого '
                         + 'бизнес-плана на базе «' + fc.b + '».' : '');
      /* ⚠️ Штриховку «вывоз не закончен» рисуем ТОЛЬКО там, где плановой
         дорожки нет. Дорожка теперь одна и сама тянется до «сейчас», пока
         остаток не вывезен, — вторая штриховка поверх неё и была той самой
         перегруженностью, из-за которой два этажа свели в один. */
      out += '<i class="'+(inFact?'f '+sp.cls+' ':'')+cc
           + (openTail&&!wc?'o ':'')+(oBad&&!wc?'o-bad ':'')
           + (b===sc.tB?'now ':'')+(yb?'yb ':'')+(qb?'q ':'')
           + '" title="'+esc(tip)+'">'
           + (hOut?'<b style="--h:'+hOut+'"></b>':'')
           /* Одна дорожка вместо двух прежних этажей. */
           + (wc?'<span class="w '+wc+wStart+'"></span>':'')
           /* ⚠️ ПОЛОСЫ ХОДА БОЛЬШЕ НЕТ (заказчик 20.08.2026: «сделать их одной»):
              две параллельные линии у оси читались как дубль — обе про наш срок.
              Осталась одна плановая дорожка; зоны хода («есть запас / впритык /
              срок прошёл») по-прежнему написаны в подсказке деления (ownTitle). */
           + (lEnd?'<u title="срок вывоза по 1С (Лукойл)"></u>':'')
           + (wEnd?'<em title="наша плановая дата вывоза"></em>':'')
           /* рамка плана производства — поверх всего, но кликов не ловит */
           + (pf ? '<div class="pf'+(pf.first?' f':'')+(pf.last?' l':'')+'">'
                 + (pf.first ? '<em>'+prodShort(pf.v)+'</em>' : '') + '</div>' : '')
           /* прогноз по базам: штриховка съедаемых месяцев и подпись остатка */
           + (fq ? '<div class="fc">'+(fq.first?'<em>'+prodShort(fq.v)+'</em>':'')
                 + '</div>' : '')
           + (fcEnd ? '<div class="fcl"><em>'+prodShort(fc.left)+'</em></div>' : '')
           + (pagg[b]?'<s title="'+esc(payTitle(b, sc, pay, pagg[b]))+'"></s>':'')
           + '</i>';
    }
    /* ⚠️ ПУСТАЯ ШКАЛА НА НЕДЕЛЯХ — НЕ НОЛЬ, А ОТСУТСТВИЕ ДЕТАЛИЗАЦИИ.
       Недельный ряд расчёт ведёт только за последние C.GANTT_WEEKS_MONTHS
       месяцев, и у планов старше него `w` пуст. Разложить месяц по неделям
       нельзя: внутри месяца неизвестно, в какой день что было. Раньше такая
       строка рисовалась просто белой — и читалась как «движений не было» или
       «данные потеряли». По выгрузке 12.08.2026 таких строк 612 из 934, то есть
       две трети диаграммы: именно на это и пожаловались на совещании.
       Теперь строка помечается штриховкой и говорит правду в подсказке. */
    const noWeeks = sc.zoom==='w'
      && !Object.keys(wkm || {}).length
      && Object.keys(m || {}).length > 0;
    return '<div class="gtime"><div class="gcells st-'+esc(sp.cls||'none')
         + (noWeeks ? ' nowk" title="'+esc('Недельной детализации по этому плану '
             + 'нет: расчёт ведёт недели только за последние 12 месяцев, а '
             + 'движения здесь старше. Пустая шкала тут означает «нет разбивки '
             + 'по неделям», а не «движений не было» — переключитесь на месяцы, '
             + 'и все движения появятся.') + '"' : '"')
         + '>' + out+'</div></div>';
  }
  /** Подсказка значка оплаты: сколько и когда заплатили в этом делении, чем и
      кому. Показываем до восьми платежей — больше в подсказку не влезает, общая
      сумма и число стоят первой строкой, поэтому обрезка ничего не прячет. */
  /** Месяцы, в которых по строке БЫЛ ПРИХОД металла: ['2025-04', …].
      Узел Ганты хранит помесячно пару [приход, расход] — берём месяцы, где
      приход не нулевой. Это и есть «дата поступления» в терминах заказчика. */
  const inMonths = t => Object.keys((t && t.m) || {})
      .filter(m => Math.abs((t.m[m]||[0])[0]) > 0.0005);
  /* ⚠️ ФИЛЬТР «ПОСТУПЛЕНИЕ ЗА N МЕС.» СЧИТАЕТ ДОКУМЕНТЫ, А НЕ ПРИХОД ДИАГРАММЫ.
     Приходная часть Ганты — это «куплено» ПОСЛЕ двух поправок: из неё вычтен
     возврат поставщику (NEGATIVE_IN) и в неё добавлен «приход через разборку»
     (BIRTH_IN, выпуск переработки без тоннажного сырья). Для тоннажа это верно,
     а для вопроса «когда сюда последний раз ЗАВОЗИЛИ металл» — нет: по такому
     приходу в список попадали планы, где за окно не было ни одного документа
     покупки, только внутренняя переработка. На выгрузке 12.08.2026 это 11 планов
     из 93 в варианте 2 (1192, 1401, 1519, 1625, 1637, 1674 …) и 1 из 78 в
     варианте 1. Плюс месяц с одним лишь ВОЗВРАТОМ давал отрицательный приход, а
     `Math.abs` считал его поступлением — ещё 3–6 планов.
     Поэтому месяцы поступления берём из самих движений: документы внешнего
     прихода и только положительные величины. Индекс строится лениво, один раз. */
  const RECEIPT_FLOWS = new Set(['куплено','излишки','ввод_остатков','возврат_от_клиента']);
  /* ⚠️ ПЕРИМЕТР ВАРИАНТА ОБЯЗАТЕЛЕН. Вариант 1 показывает только склады
     заготовки, и если считать поступления по ВСЕМ складам, в список попадут
     планы, у которых на диаграмме прихода нет вовсе (закупка шла на базу) —
     строка без единого столбика прихода, а фильтр её оставил. На выгрузке
     12.08.2026 таких 4–7 в каждом окне. Поэтому индекс строится ПО ВАРИАНТУ,
     через тот же список площадок, что и сама диаграмма (`v.sites`, пусто = все). */
  const _RCPM = new Map();                 // «вариант|край» -> Map(проект -> месяц поступления)
  /** Месяц поступления по каждому проекту. `edge`: 'first' — самый ранний,
      'last' — самый поздний. Один проход на оба края, разница в одном сравнении.

      ⚠️ ПЕРВЫЙ и ПОСЛЕДНИЙ отвечают на РАЗНЫЕ вопросы, и оба нужны:
        * ПЕРВЫЙ — «когда проект начал завозиться». На нём стоит фильтр «за
          последние N мес.»: «не раньше трёх месяцев» значит, что раньше границы
          поступлений не было вовсе. По последнему он держал бы старые планы
          из-за мелких хвостов — у БП 1760 основной завоз 3 121,393 т в декабре
          2025, а в июне и июле 2026 добрали 4,318 и 16,125 т, и он попадал бы в
          «за последние 3 месяца», хотя проект декабрьский;
        * ПОСЛЕДНИЙ — «как давно завозили в последний раз», то есть залежался ли
          план. На нём стоят полосы давности. Если что-то привезли месяц назад,
          план не залежался, сколько бы лет проекту ни было. */
  function receiptMap(vkey, edge){
    const key = vkey + '|' + edge;
    if(_RCPM.has(key)) return _RCPM.get(key);
    const sites = new Set((GVAR(vkey).sites) || []);
    const SK = DATA.sk_site || {};
    const last = edge === 'last';
    const m = new Map();
    for(const r of MOVES){
      if(!RECEIPT_FLOWS.has(mfl(r))) continue;
      if(!(r[MC.tonnes] > 0.0005) && !(r[MC.qty] > 0.0005)) continue;
      if(sites.size && !sites.has(SK[mst(r,'sklad')])) continue;
      const s = mst(r,'series'), mo = (mst(r,'dt')||'').slice(0,7);
      if(!s || !mo) continue;
      const cur = m.get(s);
      if(cur === undefined || (last ? mo > cur : mo < cur)) m.set(s, mo);
    }
    _RCPM.set(key, m);
    return m;
  }
  const _OUTM = new Map();
  /** Дата ПОСЛЕДНЕГО расхода по правилам варианта: для варианта 1 это вывоз за
      пределы заготовки, для варианта 2 — продажа. Нужна фильтру «оплачено, а
      вывоза нет»: там важен не месяц, а день — три недели внутри месяца не
      видны. Точные даты есть только в строках движений, поэтому идём по ним. */
  function lastOutDateMap(vkey){
    if(_OUTM.has(vkey)) return _OUTM.get(vkey);
    const V0 = GVAR(vkey);
    const outs = new Set((V0.oo||[]).map(i=>GFLOWS[i]).filter(Boolean));
    const sites = new Set(V0.sites || []);
    const SK = DATA.sk_site || {};
    const m = new Map();
    for(const r of MOVES){
      if(!outs.has(mfl(r))) continue;
      if(!(r[MC.tonnes] > 0.0005) && !(r[MC.qty] > 0.0005)) continue;
      if(sites.size && !sites.has(SK[mst(r,'sklad')])) continue;
      const s = mst(r,'series'), d = (mst(r,'dt')||'').slice(0,10);
      if(!s || !d) continue;
      const cur = m.get(s);
      if(cur === undefined || d > cur) m.set(s, d);
    }
    _OUTM.set(vkey, m);
    return m;
  }
  const firstReceiptMap = vkey => receiptMap(vkey, 'first');
  const lastReceiptMap  = vkey => receiptMap(vkey, 'last');
  function payTitle(b, sc, pay, a){
    const list = (pay.d||[]).filter(p=>{
      if(sc.zoom==='w') return wkIdxOfDate(p.d)===b;
      const i = mIdx(p.d.slice(0,7)); return i!=null && bOf(i, sc.zoom)===b; });
    let out = 'ОПЛАТА · '+bTitle(b, sc.zoom)+'\n'
      + money(a[0])+' ₽ · платежей '+a[1]+'\n';
    list.slice(0,8).forEach(p=>{
      /* ⚠️ «дата платежа» подписана явно: в назначении почти всегда стоит ЕЩЁ
         ОДНА дата — дата СЧЁТА («по счёту № 2/23Y0618_014 от 6 февраля 2025 г.»),
         и её принимали за дату оплаты. Точка стоит по дате платежа из колонки
         «ДатаПлатежа», а счёт может быть выставлен месяцами раньше. */
      out += '\nдата платежа '+ruDate(p.d)+' · '+money(p.s)+' ₽'
        + (p.ca?' · '+p.ca:'') + (p.doc?'\n   '+p.doc:'')
        + (p.bp?'\n   БП: '+p.bp:'')
        + (p.np?'\n   назначение: '+p.np.slice(0,140):'');
    });
    if(list.length>8) out += '\n\n… и ещё '+(list.length-8)+' платежей';
    return out;
  }
  /* ---------- ЕДИНАЯ ПЛАНОВАЯ ДОРОЖКА: две даты на одной линии ----------
     g.own приходит из расчёта: {m: месяцев, pay: месяц первой оплаты, bp: номер
     БП в Битриксе, ver: дата версии плана, alt: другие значения на ту же дату}.

     ⚠️ ЯКОРЬ (решение заказчика 07.08.2026): месяц первой ОПЛАТЫ за лом, а если
     платежей в выгрузке нет — месяц первого прихода металла, то есть начало той
     же плановой линии, что уже нарисована. Оплаты есть только у 187 планов из
     734, поэтому без запасного якоря дорожки не было бы у трёх четвертей строк.
     Откуда взят якорь — написано в подсказке, гадать не приходится.

     Наша дата = якорь + (месяцев − 1): «2 месяца вывоза» — это два деления,
     сам месяц якоря и следующий, а не два месяца СВЕРХ него.

     Дорожка начинается с первой покупки. Наша дата по-прежнему считается от
     принятого якоря, но больше не создаёт второй этаж.

     Полоса тянется до конца вывоза, а если вывоз не закончен — до «сейчас»:
     иначе у просроченного плана красного не видно вовсе. */
  function ownSpan(g, t, sc, pmLuk){
    const own = g && g.own && g.own.m ? g.own : null;
    if(!own && !pmLuk) return null;
    /* ⚠️ ЯКОРЬ НАШЕЙ ДОРОЖКИ — ТРИ СТУПЕНИ, ПОРЯДОК ЗАДАН ЗАКАЗЧИКОМ 12.08.2026:
         1) месяц первой ОПЛАТЫ за лом — если платёж прошёл, отсчёт от него;
         2) иначе месяц ПЕРВОГО ДВИЖЕНИЯ — металл поехал, план пошёл;
         3) иначе ТЕКУЩИЙ МЕСЯЦ — плана в работе ещё нет, и считать не от чего.
       Третья ступень появилась вместе со строками планов, по которым движений
       нет вовсе (C.GANTT_PLAN_ONLY_MONTHS): у них ни оплаты, ни движения, и
       раньше `ai==null` просто убирало дорожку — строка выходила пустой. */
    const a = anchorOf(g, t);
    if(!a.anchorD) return null;
    const payD = a.payD, moveD = a.moveD, anchorD = a.anchorD;
    const ourD = own ? addMon(anchorD, own.m) : '';
    /* начало дорожки — начало БП: что было раньше, оплата или движение */
    const startD = a.startD;

    const aB = bOfDate(startD, sc.zoom);
    if(aB==null) return null;
    const oB = ourD ? bOfDate(ourD, sc.zoom) : null;
    /* срок 1С (Лукойл) — по ТОЧНОЙ дате: у 324 планов из 582 это 30-е или
       31-е число, и по месяцу флаг вставал на первую неделю месяца */
    const lB = pmLuk ? bOfDate(pmLuk, sc.zoom) : null;
    const factB = lastOutB(t, sc);
    /* докуда дошёл ФАКТ: вывоз закрыт — до последнего движения, не закрыт — до
       «сейчас». ⚠️ `fact_end` — МЕСЯЦ, и на недельной шкале он даёт первую
       неделю месяца: у БП 1892 расход шёл на неделях 29–30, а «конец факта»
       вставал на неделю 27, и полоса пунктирилась поверх настоящих движений.
       Поэтому на недельной шкале берём последнюю НЕДЕЛЮ с движением. */
    const lastB = lastFactB(t, sc);
    const nowB = NOW_D ? bOfDate(NOW_D, sc.zoom) : sc.tB;
    const doneB = t.rest>0.5 ? mx2(nowB, lastB) : (lastB==null ? nowB : lastB);
    /* корзина первой оплаты — граница жёлтого и оранжевого этапов */
    const payB = payD ? bOfDate(payD, sc.zoom) : null;
    /* ⚠️ ДОКУДА КРАСИТЬ (заказчик 20.08.2026: «если остаток ноль — то и линия
       кончается»). Вывоз не закончен — дорожка до «сейчас». Вывоз ЗАКРЫТ —
       дорожка обрывается на последнем расходе: объём кончился, линия тоже.
       Раньше она дотягивалась до сроков даже у закрытого плана и читалась как
       «что-то ещё идёт». Флажки сроков от eB больше не зависят и рисуются на
       своих местах (см. cellsHtml). */
    let eB = Math.max(aB, factB==null?aB:factB);
    if(t.rest>0.5){
      eB = Math.max(eB, oB==null?aB:oB, lB==null?aB:lB);
      if(sc.tB!=null) eB = Math.max(eB, sc.tB);
    }

    /* ---- ПОЛОСА ХОДА ПО НАШЕМУ ПЛАНУ (три зоны) ----
       Идёт по самому низу от начала БП до нашего флажка и дальше, если срок уже
       прошёл. Зоны: три четверти отпущенного времени — «с запасом», последняя
       четверть — «впритык», после нашей даты — «просрочено». */
    const q3D = (ourD && dNum(ourD) > dNum(startD))
              ? dStr(Math.round(dNum(startD) + (dNum(ourD) - dNum(startD)) * 0.75)) : '';
    const pB1 = q3D ? bOfDate(q3D, sc.zoom) : null;
    const pEnd = oB==null ? null : Math.max(oB, doneB==null ? oB : doneB);

    return {aB, oB, lB, eB, payB, m: own ? own.m : null,
            anchor: anchorD, anchorM: anchorD.slice(0,7),
            /* откуда взят якорь — видно в подсказке, чтобы не гадать */
            src: payD ? 'оплата' : moveD ? 'первое движение'
                                         : 'текущий месяц — движений нет',
            our: ourD, ourM: ourD.slice(0,7), luk: pmLuk||'',
            paid: !!payD, own, pB1, pEnd, doneB, start: startD};
  }
  /** ЯКОРЬ дорожки — ТРИ СТУПЕНИ, порядок задан заказчиком: оплата → первое
      движение → текущий месяц. Считается в ДАТАХ, а не в месяцах: платёж
      30-го числа при якоре-месяце давал флажок на первую неделю того же
      месяца, то есть РАНЬШЕ платежа, от которого он отсчитан.
      ⚠️ Одна функция на два места — здесь и в расчёте границ шкалы в draw().
      Пока правил было два, наша дата уезжала за правый край и флажок пропадал. */
  function anchorOf(g, t){
    const payD = firstPayDate(g, g && g.own);
    const moveD = firstMoveDate(t);
    const anchorD = payD || moveD || (TODAY ? TODAY + '-01' : '');
    return {payD, moveD, anchorD,
            startD: [payD, moveD].filter(Boolean).sort()[0] || anchorD};
  }
  /** Наша плановая дата: якорь плюс заложенное число месяцев (правило заказчика
      13.08.2026 — «от даты первого платежа отсчитываем кол-во месяцев»).
      Именно +m, а не +(m−1): при +(m−1) «1 месяц вывоза» дал бы срок в день
      оплаты, то есть ноль дней на работу. */
  function ourDateOf(g, t){
    const own = g && g.own && g.own.m ? g.own : null;
    if(!own) return '';
    const a = anchorOf(g, t);
    return a.anchorD ? addMon(a.anchorD, own.m) : '';
  }
  /** Дата первого платежа по бизнес-плану — ТОЧНАЯ, из строк оплат.
      `own.pay` хранит только месяц, и его одного мало: якорь на месяце и был
      причиной флажка, стоящего раньше платежа. */
  function firstPayDate(g, own){
    let best = '';
    for(const p of ((g && g.pay && g.pay.d) || []))
      if(p.d && (!best || p.d < best)) best = p.d;
    if(best) return best;
    return (own && own.pay) ? own.pay + '-01' : '';
  }
  /** Дата первого движения. Недельный ряд точнее месяца, но расчёт ведёт его
      только за последние 12 месяцев (C.GANTT_WEEKS_MONTHS): если он начинается
      не в том же месяце, что `start`, он не о первом движении — берём 1-е число. */
  function firstMoveDate(t){
    if(!t || !t.start) return '';
    let best = '';
    for(const k in (t.w || {})){
      const s = wkMondayStr(wkIdxOfKey(k));
      if(!best || s < best) best = s;
    }
    return (best && best.slice(0,7) === t.start) ? best : t.start + '-01';
  }
  /** Максимум из двух корзин, любая из которых может быть null. */
  function mx2(a, b){
    if(a==null) return b;
    if(b==null) return a;
    return Math.max(a, b);
  }
  /** Последняя корзина, в которой ФАКТИЧЕСКИ было движение. На недельной шкале
      считается по недельному ряду `t.w` (месяц туда не раскладывается), на
      прочих — по месяцу последнего расхода. */
  function lastFactB(t, sc){
    let best = null;
    if(sc.zoom==='w'){
      for(const k in (t.w || {})){
        const v = t.w[k];
        if(Math.abs(v[0])>0.0005 || Math.abs(v[1])>0.0005){
          const b = wkIdxOfKey(k);
          if(best==null || b>best) best = b;
        }
      }
    }
    return mx2(best, mIdx(t.fact_end)!=null ? bOf(mIdx(t.fact_end), sc.zoom) : null);
  }
  /** ПЯТЬ ЭТАПОВ ДОРОЖКИ (заказчик 20.08.2026). Контрольные точки — срок
      вывоза 1С, первая оплата, наша плановая дата — в ХРОНОЛОГИЧЕСКОМ порядке:
        зелёный   — до первой точки;
        жёлтый    — после первой;
        оранжевый — после второй;
        красный   — после третьей;
        обрыв     — на последнем вывозе, если остаток нулевой (пятый «этап»).
      Точки может не быть (нет оплаты, нет срока 1С) — тогда цвета сдвигаются:
      последний пройденный этап всегда красный. Прежняя раскраска сравнивала
      только две даты между собой и оплату не видела вовсе. */
  function ownPts(w){
    return [w.lB, w.payB, w.oB].filter(x=>x!=null).sort((a,b)=>a-b);
  }
  function ownCls(b, w){
    if(w==null || b<w.aB || b>w.eB) return '';
    const pts = ownPts(w);
    const gone = pts.filter(p=>b>p).length;
    const pal = pts.length>=3 ? ['w1','w2','w4','w3']
              : pts.length===2 ? ['w1','w2','w3']
              : pts.length===1 ? ['w1','w3'] : ['w1'];
    return pal[Math.min(gone, pal.length-1)]+' ';
  }
  function ownTitle(b, w, z){
    /* этап — по тем же точкам, что красят дорожку (ownPts): имена в
       хронологическом порядке, чтобы подсказка совпадала с цветом */
    const named = [[w.lB,'срок вывоза 1С'],[w.payB,'первая оплата'],
                   [w.oB,'наша дата']].filter(x=>x[0]!=null)
                  .sort((a,b)=>a[0]-b[0]);
    const gone = named.filter(x=>b>x[0]).length;
    const zone = !named.length ? 'контрольных точек нет'
               : gone===0 ? 'до первой точки («'+named[0][1]+'»)'
               : gone===named.length ? 'все точки пройдены (последняя — «'
                                       +named[gone-1][1]+'»)'
               : 'между «'+named[gone-1][1]+'» и «'+named[gone][1]+'»';
    /* зона полосы хода — тем же словом, что и в легенде */
    const way = w.oB==null || w.pEnd==null || b<w.aB || b>w.pEnd ? ''
              : b > w.oB ? '\nход по нашему плану: срок прошёл'
              : (w.pB1!=null && b > w.pB1) ? '\nход по нашему плану: идём впритык'
              : '\nход по нашему плану: есть запас';
    return '\nПЛАН ВЫВОЗА · ' + zone + way
      /* откуда взят якорь — три ступени, и человек должен видеть, какая сработала */
      + (w.own ? '\n' + w.m + ' мес. от ' + dateRu(w.anchor) + ' (' + w.src + ')' : '')
      + '\nдата БП ' + (dateRu(w.our)||'нет')
      + (w.luk ? ' · срок вывоза ' + dateRu(w.luk) : ' · срока вывоза нет')
      + (w.own ? '\nБП ' + w.own.bp + ' в Битриксе, версия плана от ' + w.own.ver : '')
      + (w.own && w.own.alt && w.own.alt.length
         ? '\n⚠️ на эту же дату есть и другие значения: ' + w.own.alt.join(', ') + ' мес' : '');
  }
  const spanOf = (t, pmD, cls, sc) => ({
    sB: firstMoveB(t, sc),
    /* pmD — точная дата срока вывоза 1С (см. pmd в draw) */
    pB: pmD ? bOfDate(pmD, sc.zoom) : null,
    eB: (lastOutB(t, sc) ?? firstMoveB(t, sc)),
    open: t.rest>0.5, cls,
  });
  /** Первая корзина с движением. На недельной шкале — по недельному ряду:
      `start` — это МЕСЯЦ, и жирный отрезок факта начинался с первой недели
      месяца, даже если металл поехал в его последних числах. */
  function firstMoveB(t, sc){
    let best = null;
    if(sc.zoom==='w'){
      for(const k in (t.w || {})){
        const v = t.w[k];
        if(Math.abs(v[0])>0.0005 || Math.abs(v[1])>0.0005){
          const b = wkIdxOfKey(k);
          if(best==null || b<best) best = b;
        }
      }
    }
    const mb = mIdx(t.start)==null ? null : bOf(mIdx(t.start), sc.zoom);
    /* недельный ряд ведётся только за последние 12 месяцев: если план старше,
       правды в нём нет, и месяц первого движения точнее */
    if(best==null || mb==null) return best==null ? mb : best;
    return Math.min(best, mb);
  }
  /** Последняя корзина с РАСХОДОМ — конец жирного отрезка факта.
      Именно расход, а не любое движение: `fact_end` в расчёте — это месяц
      последнего расхода, и поздний приход не должен удлинять отрезок. */
  function lastOutB(t, sc){
    let best = null;
    if(sc.zoom==='w'){
      for(const k in (t.w || {}))
        if(Math.abs(t.w[k][1])>0.0005){
          const b = wkIdxOfKey(k);
          if(best==null || b>best) best = b;
        }
    }
    return mx2(best, mIdx(t.fact_end)==null ? null
                     : bOf(mIdx(t.fact_end), sc.zoom));
  }
  /* метрики подчинённой строки: срок и опоздание — свойство проекта, здесь пусто */
  /* cms — комментарии этого уровня разбора (заказчик 20.08.2026: «чтобы
     комменты декомпозировались на конкретный склад и базу»). Значок на остатке
     тот же, что на строке проекта, — одна привычка на все уровни. */
  const subMetrics = (t, cms) =>
      '<div class="gmetrics"><span></span>'
    + '<span'+(t.fact_end?'':' class="muted"')+'>'+(t.fact_end?esc(t.fact_end):'—')+'</span>'
    + '<span'+(t.pct==null?' class="muted"':'')+'>'+(t.pct==null?'—':fmt(t.pct)+' %')+'</span>'
    + '<span class="gbuy">'+fmt(t.bought)+' т</span>'
    + '<span class="grest'+(t.rest>0.5?'':' z')+((cms||[]).length?' cm':'')+'"'
      + ((cms||[]).length?' title="'+esc('Комментарии по этому уровню: '
        +cms.length+' зап. Нажмите, чтобы прочитать.')+'"':'')+'>'+fmt(t.rest)+' т'
      + ((cms||[]).length?'<i class="cmn">'+cms.length+'</i>':'')+'</span>'
    /* прогноз по базам в разборе ПУСТ: ФИФО раскладывает план по бизнес-планам,
       а не по складам внутри плана — делить долю плана между его складами не на
       чем, и выдуманное число здесь было бы хуже прочерка */
    + '<span></span><span></span></div>';

  /* ---------- разбор: номенклатура и документы ---------- */
  /* ⚠️ ЗНАЧКИ КОММЕНТАРИЕВ НА СТРОКАХ НОМЕНКЛАТУРЫ (заказчик 20.08.2026:
     «при развороте номенклатуры окошки с комментариями должны вставать в
     соответствии с НМК»). Связь — имя номенклатуры комментария = имя позиции
     на этом складе; оба идут из одного справочника 1С, поэтому связь по имени
     здесь надёжна. Значок тот же, что у остатка, окно — то же, только срез
     одной номенклатуры. Хук __NOM_CM живёт лишь на время построения дерева. */
  function leafTree(rows, title, g, w){
    const box = el('div','gdet');
    if(title) box.appendChild(el('div','gdet-h', title));
    const tree = el('div','panel tree');
    window.__COLHEAD = 'склад · номенклатура · документы';
    tree.appendChild(colHead());
    const byNom = new Map();
    ((g && g.cm) || []).forEach(c=>{
      if(c.w !== w) return;
      const k = c.n || '—';
      (byNom.get(k)||byNom.set(k,[]).get(k)).push(c);
    });
    if(byNom.size){
      window.__NOM_CM = p0 => {
        const cs = byNom.get(p0.name);
        return cs ? ' <i class="cmn nomcm" data-n="'+esc(p0.name)+'" title="'
          + esc('Комментарии по этой номенклатуре: '+cs.length
              + '. Нажмите, чтобы прочитать.')+'">'+cs.length+'</i>' : '';
      };
      /* capture: перехватить клик ДО обработчика строки, иначе значок ещё и
         раскрывал бы документы под номенклатурой */
      tree.addEventListener('click', ev=>{
        const t = ev.target.closest('.nomcm');
        if(!t) return;
        ev.stopPropagation();
        openNotes(g, t, byNom.get(t.dataset.n)||[], t.dataset.n);
      }, true);
    }
    try{
      if(rows.length) posNodes(tree, rows, false);
      else tree.appendChild(el('div','hint',
        'Продаж по этому узлу в выгрузке нет — был только приход и движения.'));
    } finally { window.__NOM_CM = null; }
    box.appendChild(tree);
    fitLabels();
    return box;
  }

  /* ---------- подчинённая строка разбора ---------- */
  function subRow(g, label, tag, nodes, V, sc, pmD, lvl, buildKids, projectPlan){
    const t = totalsOf(aggNodes(nodes, V));
    t.w = aggNodeWeeks(nodes, V);      /* недели — для столбиков мелкой шкалы */
    /* дорожка подстроки: проектные даты, но у закрытой — обрыв на СВОЁМ
       последнем вывозе (см. комментарий у вызова cellsHtml ниже) */
    let wsp = projectPlan;
    if(projectPlan && t.rest <= 0.5){
      const lb = lastFactB(t, sc);
      wsp = Object.assign({}, projectPlan,
        {eB: Math.max(projectPlan.aB, lb==null ? projectPlan.aB : lb)});
    }
    const cms = notesOf(g, nodes);
    const cls = (G_STATUS[statusOf({bad_dates:0}, t, pmD)]||{}).c || 'none';
    const row = el('div','gsub lvl'+lvl+(buildKids?' has':''));
    row.innerHTML =
        '<div class="glbl"><span class="gtw">'+(buildKids?'▶':'')+'</span>'
      + '<span class="gsnm" title="'+esc(label)+'">'+esc(label)+'</span>'
      + (tag?'<span class="tag">'+esc(tag)+'</span>':'')+'</div>'
      + subMetrics(t, cms)
      /* ⚠️ РАМКИ ПЛАНА И В РАЗБОРЕ (заказчик 20.08.2026: «план должен
         перетягиваться на базу, там где есть остаток»). План заведён на БП
         целиком, по складам 1С его не делит, поэтому рамка повторяется на
         строках, где ЛЕЖИТ ОСТАТОК, — и не рисуется на закрытых: там
         перетягивать нечего. Об этом сказано в подсказке рамки.
         ⚠️ ДОРОЖКА ЗАКРЫТОЙ ПОДСТРОКИ ОБРЫВАЕТСЯ НА ЕЁ ПОСЛЕДНЕМ ВЫВОЗЕ
         (заказчик, вторая поправка: «линии у всех, но до момента, пока есть
         остаток; как только он пропадает — штриховка и линия перестают
         рисоваться»). Даты и цвета этапов — общие, проектные (якорь по складам
         не пересчитывается, см. projectPlan), а вот КОНЕЦ линии свой: склад
         вывез всё в марте — его линия кончается в марте, даже если сосед по
         проекту возит до сих пор. */
      + cellsHtml(t.m, sc, spanOf(t, pmD, cls, sc), null,
                  wsp, t.w,
                  t.rest>0.5 ? g.prod : null);
    /* число остатка — на «Остатки» с этим планом; счётчик — лента комментариев */
    const gr = row.querySelector('.grest');
    if(gr) gr.addEventListener('click', ev=>{
      ev.stopPropagation();
      if((cms||[]).length && ev.target.closest('.cmn')){
        openNotes(g, ev.currentTarget, cms, label.replace(/^[^ ]+ /,'')); return;
      }
      goStock(g.series, V.key);
    });
    if(!buildKids) return [row];
    const kids = el('div','gkids');
    let built = false;
    row.addEventListener('click', ev=>{
      if(ev.target.closest('.gpen')) return;
      ev.stopPropagation();
      const open = kids.classList.toggle('open');
      row.classList.toggle('open', open);
      row.querySelector('.gtw').textContent = open ? '▼' : '▶';
      if(open && !built){ buildKids(kids); built = true; }
    });
    return [row, kids];
  }

  /* ---------- декомпозиция строки бизнес-плана ----------
     Вариант 1: проект → склады (у каждого своя диаграмма на общей шкале).
     Вариант 2: тип площадки → конкретное подразделение/база → склад.
     Ниже в обоих вариантах — номенклатура и документы. */
  function detailsFor(g, V, sc, pmD){
    const box = el('div','gdet-wrap');
    /* узлы разбора — только своего разреза: иначе штуки попали бы в столбик
       с тоннами, и разбор перестал бы сходиться со строкой проекта */
    let nodes = (g.nodes || []).filter(n=>(n.q||MMAIN)===GMEAS);
    if(V.sites.length) nodes = nodes.filter(n=>V.sites.includes(n.s));
    if(!nodes.length){ box.appendChild(el('div','hint','Узлов по этому варианту нет.')); return box; }
    /* Наша плановая дорожка относится ко всему БП, поэтому во всех уровнях
       разбора показываем ТО ЖЕ окно проекта. Не пересчитываем якорь от первого
       прихода конкретного склада: иначе у каждого дочернего узла получилась бы
       своя, выдуманная дата БП. Факт и столбики при этом остаются собственными
       для каждой строки. */
    const projectPlan = ownSpan(g, totalsOf(aggNodes(nodes, V)), sc, pmD);
    const rows = ITEMS.filter(r=>r.series===g.series);
    const by = (arr, f)=>{ const m=new Map();
      for(const x of arr){ const k=f(x)||'—'; (m.get(k)||m.set(k,[]).get(k)).push(x); }
      return m; };
    const put = (host, parts)=> parts.forEach(p=>host.appendChild(p));

    /* склады — по объёму прихода, самый крупный сверху */
    const weight = nd => totalsOf(aggNodes(nd, V)).bought;
    if(V.key==='v1'){
      const bySkl = by(nodes, n=>n.w);
      [...bySkl.keys()].sort((a,b)=> weight(bySkl.get(b))-weight(bySkl.get(a))
        || String(a).localeCompare(String(b),'ru')).forEach(w=>{
        const nd = bySkl.get(w);
        put(box, subRow(g, '🏷 '+w, nd[0].u, nd, V, sc, pmD, 1, kids=>{
          kids.appendChild(leafTree(rows.filter(r=>r.sklad===w),
            'Склад ' + w + ' — номенклатура и документы', g, w));
        }, projectPlan));
      });
      return box;
    }
    /* вариант 2: проект → тип площадки → конкретная база/подразделение → склад.
       Например: База → База Осенцы / База Усинск → их склады. */
    const bySite = by(nodes, n=>n.s);
    [...bySite.keys()].sort((a,b)=> weight(bySite.get(b))-weight(bySite.get(a))
      || String(a).localeCompare(String(b),'ru')).forEach(dv=>{
      const nd = bySite.get(dv);
      put(box, subRow(g, '🏢 '+dv, 'тип площадки', nd, V, sc, pmD, 1, k1=>{
        const byDiv = by(nd, n=>n.d);
        [...byDiv.keys()].sort((a,b)=> weight(byDiv.get(b))-weight(byDiv.get(a))
          || String(a).localeCompare(String(b),'ru')).forEach(un=>{
          const nu = byDiv.get(un);
          put(k1, subRow(g, '🌐 '+un, 'подразделение', nu, V, sc, pmD, 2, k2=>{
            const bySkl = by(nu, n=>n.w);
            [...bySkl.keys()].sort((a,b)=> weight(bySkl.get(b))-weight(bySkl.get(a))
              || String(a).localeCompare(String(b),'ru')).forEach(w=>{
              const nw = bySkl.get(w);
              put(k2, subRow(g, '🏷 '+w, nw[0].u, nw, V, sc, pmD, 3, k3=>{
                k3.appendChild(leafTree(rows.filter(r=>r.sklad===w),
                  'Склад ' + w + ' — номенклатура и документы', g, w));
              }, projectPlan));
            });
          }, projectPlan));
        });
      }, projectPlan));
    });
    return box;
  }

  /* ---------- отрисовка ---------- */
  function draw(){
    const V = GVAR(vkey);
    const q  = ($('#gq').value||'').trim().toLowerCase();
    const fs = $('#gst').value, so = $('#gsort').value;

    /* ЛЕГЕНДА ПЕРЕПИСАНА 20.08.2026 по просьбе заказчика: «сделай все условные
       обозначения более понятными и доступными». Было — сплошной абзац на
       тридцать строк; стало — короткое вступление и СПИСОК обозначений, каждое
       с образцом-свотчем и одним предложением смысла. Порядок списка — от
       самого частого вопроса («что за столбик») к самому редкому. */
    note.querySelector('.nb').innerHTML = '<b>Вариант '+esc(V.title)+'.</b> '+esc(V.hint)
      + '<br><b>Как читать.</b> Одна строка — один бизнес-план (проект). Одно '
      + 'деление шкалы — месяц, квартал или год (переключается в панели). '
      + 'Вертикальная синяя линия — «сейчас», конец выгрузки ('+esc(TODAY)+').'
      + '<br><br><b>Условные обозначения:</b>'
      + '<br><span class="gl gl-out"></span> <b>столбик</b> — вывоз (расход) за '
      + 'деление. Высота — доля от самого большого расхода этой же строки, между '
      + 'строками высоту не сравнивают; точные тонны — в подсказке деления.'
      + '<br><span class="gl gl-axis"></span> <b>жирный отрезок оси</b> — период, '
      + 'когда по плану шли движения: от первого прихода до последнего расхода.'
      + '<br><span class="gl gl-w1"></span><span class="gl gl-w2"></span>'
      + '<span class="gl gl-w4"></span><span class="gl gl-w3"></span> '
      + '<b>плановая дорожка</b> — наш план вывоза, идёт этапами по трём '
      + 'контрольным точкам в порядке их наступления: срок вывоза 1С, первая '
      + 'оплата, наша дата. Зелёный — до первой точки, жёлтый — после первой, '
      + 'оранжевый — после второй, красный — после третьей. Нет какой-то точки — '
      + 'цвета сдвигаются, последний этап всегда красный.'
      + '<br><span class="gl gl-open"></span> <b>штриховка на дорожке</b> — вывоз '
      + 'не закончен, остаток ещё лежит (красная — срок прошёл, серая — срок '
      + 'впереди). Как только остаток вывезен в ноль, дорожка и штриховка '
      + '<b>обрываются на последнем вывозе</b>: объём кончился — линия тоже. Это '
      + 'касается и складов в разборе: вывезенный склад дальше линии не тянет.'
      + '<br><span class="gl gl-luk"></span> <b>синий флажок</b> — срок вывоза по '
      + '1С (ставил Лукойл или условие договора).'
      + '<br><span class="gl gl-ownf"></span> <b>ярко-жёлтый флажок</b> (один '
      + 'цвет в обеих темах; он короче синего, полотнище в середине деления — '
      + 'при совпадении дат флажки не накладываются) — наша '
      + 'плановая дата: якорь плюс число месяцев из Битрикса. Якорь — первая '
      + 'оплата; нет оплаты — первое движение; нет и его — текущий месяц.'
      + '<br><span class="gl gl-pay"></span> <b>точка вверху деления</b> — в этом '
      + 'месяце прошла оплата за лом (сумма, дата и документ — в подсказке). '
      + 'Оплата привязана к плану целиком и в разбор по складам не спускается.'
      + '<br><span class="gl gl-pf"></span> <b>пунктирная скобка</b> — план по '
      + 'объёмам производства («Объемы на загрузку»): сколько заведено на месяц, '
      + 'цифра — объём в тоннах. Какой вид плана показывать — фильтр «план».'
      + '<br><span class="gl gl-fc"></span> <b>косая штриховка с цифрой</b> — '
      + 'прогноз: в этот месяц плановое выбытие базы съедает остаток этой строки '
      + '(раскладка ФИФО от самых ранних завозов); подпись «останется N» сразу '
      + 'за планом — что останется после горизонта. Включается галочкой '
      + '«прогноз по базам», итог — в колонке «прогноз».'
      + '<br><b>Значок-счётчик у остатка</b> — комментарии экономистов из 1С по '
      + 'этому остатку: клик открывает ленту от свежего к старому. Те же значки '
      + 'в разборе — по каждому складу и по каждой номенклатуре.'
      + '<br><br><b>Цвет деления</b> говорит, уложился ли месяц в срок вывоза: '
      + 'до срока — зелёный, после — жёлтый или красный по статусу строки. Чип '
      + 'статуса слева считается по плану ЦЕЛИКОМ, поэтому зелёное начало шкалы '
      + 'у строки «с опозданием» — это норма, а не ошибка.'
      + '<br><b>«Куплено»</b> — по правилам варианта ('
      + esc(V.ii.map(i=>FSHORT[GFLOWS[i]]||GFLOWS[i]).join(' + '))+'). '
      + '<b>«Остаток»</b> — полный оборот склада: приход минус расход по ВСЕМ '
      + 'потокам, включая перемещения и переработку (как колонка ОСТАТОК в '
      + 'дереве), поэтому он не равен «куплено минус вывоз»: металл мог уехать '
      + 'на базу. <b>«Прогноз»</b> — что останется от остатка на базах после '
      + 'планового вывоза.'
      + '<br><b>Щелчок по названию</b> раскрывает разбор: '
      + (V.key==='v1' ? 'проект → склады, у каждого своя диаграмма на общей шкале'
                      : 'проект → тип площадки → база/подразделение → склад')
      + ', ниже номенклатура и документы; итог строки равен сумме разбора. '
      + '<b>Срок вывоза правится</b> карандашом в колонке «срок». Кнопка «шире '
      + 'диаграмму» прячет цифры, кроме остатка и прогноза. <b>Фильтр '
      + '«поступление»</b> оставляет планы с приходом за последние 3 · 6 · 9 · '
      + '12 месяцев; отсчёт от конца выгрузки ('+esc(TODAY)+'), а не от '
      + 'сегодняшней даты, чтобы у всех получалось одно и то же.';

    const V0 = V.key;
    /* ⚠️ Свод варианта теперь лежит ПО РАЗРЕЗАМ: g.v[вариант][единица].
       В строку берём только выбранную единицу — план без неё в диаграмму не
       попадает, ровно как и в дерево. */
    let d = G.filter(g=>g.v && g.v[V0] && g.v[V0][GMEAS]).map(g=>{
      const p = planOf(g), t = g.v[V0][GMEAS];
      /* pm — МЕСЯЦ (подписи, сортировка, статус: он и в расчёте помесячный),
         pmd — ТОЧНАЯ ДАТА срока вывоза, ею флаг ставится на шкалу.
         ⚠️ У всех 582 планов со сроком дата точная, и у 324 из них это 30-е
         или 31-е число. Пока флаг ставился по месяцу, на недельной шкале он
         падал на ПЕРВУЮ неделю месяца — до пяти недель раньше настоящего
         срока, и вместе с ним уезжала вся красная штриховка просрочки. */
      return {g, t, pm:p.month, pmd:(p.full || (p.month ? p.month+'-01' : '')), plan:p,
              status:statusOf(g,t,(p.full || (p.month ? p.month+'-01' : ''))),
              delay:delayOf(g,t,p.month)};
    });
    const fgr = $('#ggrp') ? $('#ggrp').value : '';
    if(fgr) d = d.filter(v=>bpHasGroup(v.g.series, fgr));
    const fdv = $('#gdiv') ? $('#gdiv').value : '';
    if(fdv) d = d.filter(v=>bpHasDiv(v.g.series, fdv));
    const fca = $('#gca') ? $('#gca').value : '';
    if(fca) d = d.filter(v=>bpHasCa(v.g.series, fca));
    if(fs) d = d.filter(v=>v.status===fs);
    /* ПОСТУПЛЕНИЕ за последние N месяцев: сравниваем месяцы, в которых был
       приход металла, с границей, отсчитанной назад от конца выгрузки. Границу
       считаем по ИНДЕКСУ месяца, а не сравнением строк: иначе на переходе через
       год «2026-01» оказался бы меньше «2025-11». */
    /* ---------- ПОСТУПЛЕНИЕ: ТРИ НЕЗАВИСИМЫХ УСЛОВИЯ ----------
       Наличие завоза, давность ПЕРВОГО и давность ПОСЛЕДНЕГО. Понятие прихода у
       всех трёх одно (receiptMap), поэтому спорить им не о чем: они просто
       сужают выборку по очереди, и любое сочетание осмысленно. */
    const ti = mIdx(TODAY);
    const fpay = $('#gpay') ? $('#gpay').value : '';
    if(fpay){
      const RM = firstReceiptMap(V.key);
      d = fpay === 'none' ? d.filter(v=>!RM.get(v.g.series))
                          : d.filter(v=>RM.get(v.g.series));
    }
    /* ПЕРВЫЙ завоз: «не раньше N месяцев» = раньше границы не завозили вообще */
    const ffirst = $('#gfirst') ? $('#gfirst').value : '';
    if(ffirst && ti != null){
      const FR = firstReceiptMap(V.key), from = mStr(ti - (+ffirst) + 1);
      d = d.filter(v=>{ const l = FR.get(v.g.series); return l && l >= from; });
    }
    /* ПОСЛЕДНИЙ завоз: полоса «от A» — A ≤ возраст < B, у последней верха нет.
       ⚠️ План без поступлений не попадает НИ В ОДНУ полосу: у него нет давности,
       а не «бесконечная». Такие ищутся пунктом «поступления не было вовсе». */
    const flast = $('#glast') ? $('#glast').value : '';
    if(/^age\d+$/.test(flast) && ti != null){
      const AGE = [0, 3, 6, 9, 12];
      const lo = +flast.slice(3), hi = AGE[AGE.indexOf(lo) + 1];
      const LR = lastReceiptMap(V.key);
      d = d.filter(v=>{
        const mo = LR.get(v.g.series);
        if(!mo) return false;
        const age = ti - mIdx(mo);
        return age >= lo && (hi === undefined || age < hi);
      });
    }
    /* ГЛУБИНА ПРОСРОЧКИ: «от N и далее» — накопительно, «полоса bN» — точный
       интервал (границы те же, что в sumBand на «Сводных» и «Дашборде»,
       заказчик 01.09.2026: переход с дашборда должен попадать ровно в полосу).
       План без срока вывоза не попадает никуда — у него нет просрочки. */
    const fover = $('#gover') ? $('#gover').value : '';
    if(fover){
      const B = {b0:[null,3], b3:[3,6], b6:[6,9], b9:[9,12], b12:[12,null]}[fover];
      d = d.filter(v=>{
        if(v.status!=='просрочен' || v.delay==null) return false;
        if(!B) return v.delay >= +fover;
        return (B[0]==null || v.delay > B[0]) && (B[1]==null || v.delay <= B[1]);
      });
    }
    /* ОПЛАЧЕНО, А ВЫВОЗА НЕТ. Три недели — это 21 день от ПЕРВОЙ оплаты до конца
       выгрузки, и за это время по правилам варианта не ушло ничего. */
    if($('#gnomove') && $('#gnomove').checked){
      const NOW = META.period_max || ((META.today||'') + '-28');
      const OUT = lastOutDateMap(V.key);
      const days = (a,b) => (Date.parse(b) - Date.parse(a)) / 86400000;
      d = d.filter(v=>{
        const pay = firstPayDate(v.g, v.g.own);
        if(!pay || pay.length < 10) return false;      /* платежей нет — не про нас */
        if(days(pay, NOW) < 21) return false;          /* ждём меньше трёх недель */
        const out = OUT.get(v.g.series);
        return !out || out <= pay;                     /* после оплаты ничего не ушло */
      });
    }
    if($('#gopen').checked) d = d.filter(v=>v.t.rest>0.5);
    if($('#gplan').checked) d = d.filter(v=>v.pm);
    const qmg = qMatcher(q);
    if(qmg) d = d.filter(v=>qmg((v.g.name+' '+v.g.contract+' '+v.g.contragent+' '+v.g.direction)
                            .toLowerCase()));
    const bpNumber = g=>{
      if(g.own && /^\d+$/.test(String(g.own.bp||''))) return Number(g.own.bp);
      const s = String(g.name||'');
      if(/без\s+БП/i.test(s)) return -1;
      /* Те же границы, что у canon_key в расчёте: номер БП — ведущая группа
         из 3–4 цифр. Так номер договора 1291032 и приложения 27/115-2024 не
         становятся фиктивными бизнес-планами. */
      const m = s.match(/^\s*(?:БП\s*)?(\d{3,4}(?:\s*\/\s*\d{3,4})*)(?!\d)/i)
             || s.match(/\bБП\s*(\d{3,4}(?:\s*\/\s*\d{3,4})*)(?!\d)/i);
      if(!m) return -1;
      const nums = m[1].match(/\d+/g) || [];
      return nums.length ? Math.max(...nums.map(Number)) : -1;
    };
    const cmp = {
      bp:(a,b)=>bpNumber(b.g)-bpNumber(a.g)
        || String(a.g.name).localeCompare(String(b.g.name),'ru'),
      delay:(a,b)=>(b.delay==null?-1e9:b.delay)-(a.delay==null?-1e9:a.delay) || b.t.rest-a.t.rest,
      rest:(a,b)=>b.t.rest-a.t.rest,
      /* ⚠️ Строки БЕЗ прогноза (остаток не на базе) уходят вниз, а не считаются
         нулём: ноль означал бы «план всё вывезет», а это прямо противоположное
         тому, что про них известно, — про них не известно ничего. */
      fc:(a,b)=>((b.g.fc||{}).left ?? -1) - ((a.g.fc||{}).left ?? -1),
      plan:(a,b)=>String(a.pm||'9999').localeCompare(String(b.pm||'9999')),
      bought:(a,b)=>b.t.bought-a.t.bought,
      az:(a,b)=>String(a.g.name).localeCompare(String(b.g.name),'ru'),
    }[so];
    d = d.slice().sort(cmp);

    /* ---------- СОРТИРОВКА ПО ГЛУБИНЕ ПРОСРОЧКИ (кнопки, а не список) ----------
       Заказчик 13.08.2026: разделить просроченные на «до 3 месяцев» и «больше
       трёх». Свежая просрочка и застарелая — разные разговоры: первую ещё можно
       закрыть, вторая копилась год.
       ⚠️ ЭТО СОРТИРОВКА, А НЕ ФИЛЬТР: ничего не прячется. Нажатая кнопка
       поднимает свою группу НАВЕРХ, остальные строки остаются ниже в прежнем
       порядке. Спрятать строку молча — худшая беда, чем показать её не первой:
       человек решит, что план потеряли.
       Глубина просрочки берётся из `delay` — это месяцы между сроком вывоза и
       концом выгрузки, тот же счёт, что в колонке «ОПОЗД.». */
    if(odSort){
      const deep = v => v.status==='просрочен' && v.delay!=null && v.delay>3;
      const near = v => v.status==='просрочен' && v.delay!=null && v.delay<=3;
      const pick = odSort==='near' ? near : deep;
      const rank = v => pick(v) ? 0 : 1;
      d = d.slice().sort((a,b)=>rank(a)-rank(b)
        || (rank(a)===0 ? (b.delay||0)-(a.delay||0) : 0));
    }

    /* карточки — по ОТФИЛЬТРОВАННОМУ срезу, иначе шапка и таблица расходятся */
    /* ⚠️ НЕ «cnt»: этим именем зовётся ГЛОБАЛЬНЫЙ форматтер числа, и локальная
       переменная его перекрывала на всю вкладку. Счётчик в шапке
       («N бизнес-планов») показывал 0, потому что cnt(d.length) искал строки
       со статусом «893», и то же ломало подсказку «Показаны N из M». */
    /* Подпись «просрочен» в списке статусов зависит от варианта, а сам список
       строится один раз при сборке панели — обновляем текст на каждой отрисовке.
       ⚠️ Меняется ТОЛЬКО текст: value остаётся 'просрочен', иначе фильтр
       перестанет находить строки. */
    const _o = $('#gst') && [...$('#gst').options].find(o=>o.value==='просрочен');
    if(_o) _o.textContent = statusLabel('просрочен', V.key);

    const nSt = s => d.filter(v=>v.status===s).length;
    const rst = s => sum(d.filter(v=>v.status===s), v=>v.t.rest);
    /* ведущая метрика вкладки — просроченные бизнес-планы: за ними сюда и идут */
    const card=(k,v,s,cls,lead)=>'<div class="card'+(lead?' lead':'')+'"><div class="k">'+k+'</div>'
      +'<div class="v '+(cls||'')+'">'+v+'</div><div class="s">'+s+'</div></div>';
    /* ⚠️ ОСТАТОК ПО ФИЛЬТРАМ — ВЕДУЩАЯ ПЛАШКА (заказчик 02.09.2026: «добавь
       плашку, которая показывает текущий остаток по выбранным фильтрам, то
       есть то, что ниже в списке, отражается суммой здесь»). До неё крупным
       шрифтом стояла только просрочка, а общий остаток был спрятан в серую
       строку счётчика справа: на совещании его каждый раз считали глазами по
       списку. Число — РОВНО сумма колонки «остаток» всех строк под карточками
       (то же, что в счётчике «к вывозу»), поэтому меняется вместе с любым
       фильтром. Просрочка осталась красной, но перестала быть ведущей: она —
       часть этого остатка, а не отдельная величина. */
    const restAll = sum(d.filter(v=>v.t.rest>0), v=>v.t.rest);
    cardBox.innerHTML =
        card('Остаток по фильтрам', fmt(restAll)+' <span class="unit">'+gUnit()+'</span>',
             'не вывезено по '+nOf(d.length,'бизнес-плану','бизнес-планам','бизнес-планам')
             + ' в списке ниже'
             + (restAll>0.5 ? ' · просрочено '
                 + (100*rst('просрочен')/restAll).toFixed(0)+' %' : ''),
             '', true)
      + card('Просрочено', nSt('просрочен')+' <span class="unit">БП</span>',
             'не вывезено '+fmt(rst('просрочен'))+' '+gUnit(), 'bad')
      + card('В работе', nSt('в работе')+' <span class="unit">БП</span>',
             'к вывозу '+fmt(rst('в работе'))+' '+gUnit())
      + card('Закрыто в срок', nSt('в срок')+' <span class="unit">БП</span>',
             'вывоз уложился в срок', 'ok')
      + card('Закрыто с опозданием', nSt('с опозданием')+' <span class="unit">БП</span>',
             'вывезли, но позже срока')
      + card('Без срока вывоза', (nSt('без плана')+nSt('битая дата вывоза'))
             +' <span class="unit">БП</span>', 'срок можно проставить вручную');

    $('#gcnt').textContent = nOf(d.length,'бизнес-план','бизнес-плана','бизнес-планов')+' · к вывозу '
      + fmt(sum(d.filter(v=>v.t.rest>0), v=>v.t.rest))+' '+gUnit();

    wrap.innerHTML='';
    if(!d.length){ wrap.appendChild(el('div','hint','Под фильтры ничего не попало.')); return; }
    const shown = d.slice(0, LIMIT);

    /* ⚠️ ПРЕДУПРЕЖДЕНИЕ О НЕДЕЛЬНОЙ ДЕТАЛИЗАЦИИ — с цифрой, а не общими словами.
       На неделях у планов старше 12 месяцев шкала пуста (см. .nowk в cellsHtml).
       Пока об этом молчали, две трети диаграммы выглядели потерянными данными.
       Плашка показывается ТОЛЬКО на недельном масштабе и только если такие
       строки реально есть на экране. */
    if(zoom==='w'){
      const nw = shown.filter(v=>!Object.keys((v.t && v.t.w) || {}).length
                                 && Object.keys((v.t && v.t.m) || {}).length).length;
      if(nw){
        const warn = el('div','hint gwkwarn');
        warn.innerHTML = '<b>Недели показываются только за последние 12 месяцев.</b> '
          + 'У ' + cnt(nw) + ' из ' + cnt(shown.length) + ' показанных планов движения '
          + 'старше — их шкала заштрихована и пуста. Это НЕ значит, что движений не '
          + 'было: переключитесь на «месяцы», и они появятся. Разложить месяц по '
          + 'неделям расчёт не может — внутри месяца неизвестно, в какой день что '
          + 'произошло.';
        wrap.appendChild(warn);
      }
    }

    /* общая шкала — по видимым строкам */
    let lo=1e9, hi=-1e9;
    for(const v of shown){
      for(const mm of [v.t.start, v.t.fact_end, v.pm]){
        const i=mIdx(mm); if(i==null) continue;
        if(i<lo) lo=i; if(i>hi) hi=i;
      }
      /* наша дата — по ТОМУ ЖЕ правилу, что рисует флажок (ourDateOf):
         иначе она выпадает за край шкалы и флажка на диаграмме просто нет */
      const oD = ourDateOf(v.g, v.t);
      const oi = oD ? mIdx(oD.slice(0,7)) : null;
      if(oi!=null){ if(oi<lo) lo=oi; if(oi>hi) hi=oi; }
      for(const mm in v.t.m){ const i=mIdx(mm); if(i<lo) lo=i; if(i>hi) hi=i; }
    }
    const tIdx = mIdx(TODAY);
    if(tIdx!=null){ if(tIdx<lo) lo=tIdx; if(tIdx>hi) hi=tIdx; }
    if(lo>hi){ lo = hi = (tIdx==null?0:tIdx); }
    /* Окно шкалы: сужаем границы, если период выбран. Данные не фильтруем —
       строки остаются на месте, просто показываем нужный отрезок времени. */
    const w1 = $('#gm1') ? mIdx($('#gm1').value) : null;
    const w2 = $('#gm2') ? mIdx($('#gm2').value) : null;
    if(w1!=null) lo = Math.max(lo, w1);
    if(w2!=null) hi = Math.min(hi, w2);
    if(lo>hi){ lo = w1!=null ? w1 : lo; hi = w2!=null ? w2 : Math.max(lo, hi); }
    if(lo>hi) hi = lo;
    /* ⚠️ В РЕЖИМЕ НЕДЕЛЬ ШКАЛА КОРОЧЕ. Расчёт складывает недели только за
       последние GANTT_WEEKS_MONTHS месяцев — за пределами этого окна недельных
       корзин просто нет, и растягивать шкалу на пять лет значит показать
       четыре года пустоты. Нижнюю границу поднимаем до первой недели, которая
       реально есть в данных. */
    let b0 = bOf(lo,zoom), b1 = bOf(hi,zoom);
    if(zoom==='w'){
      /* границы берём по ПОКАЗАННЫМ строкам (`shown`) — это ровно тот набор,
         который сейчас на экране, и он уже отфильтрован и по варианту, и по
         разрезу единицы */
      let wmin = null;
      for(const v of shown){
        for(const k in ((v.t && v.t.w) || {})){
          const i = wkIdxOfKey(k);
          if(wmin===null || i<wmin) wmin = i;
        }
      }
      if(wmin!==null && wmin>b0) b0 = wmin;
      b1 = Math.max(b0, wkIdxOfDate(mStr(hi)+'-28') + 1);   // хвост месяца
    }
    /* ⚠️ НА НЕДЕЛЯХ «СЕЙЧАС» — ПО ТОЧНОЙ ДАТЕ, а не по месяцу: месяц
       огрублялся до своего 1-го числа, и в конце августа подсвечивалась
       неделя 31 (куда попало 1-е) вместо текущей 34-й. */
    const sc = {zoom, b0, b1,
                tB: zoom==='w' && NOW_D ? bOfDate(NOW_D, 'w')
                  : tIdx==null ? null : bOf(tIdx, zoom)};
    const nb = sc.b1-sc.b0+1;

    const box = el('div','gantt'+(slim?' slim':''));
    /* Ширина деления подгоняется под свободное место. При 63 месяцах по 40 px
       шкала уезжала на 2 500 px: на экране помещалась четверть, и увидеть
       проект целиком было нельзя — именно от этого диаграмма не читалась.
       Меряем ПОСЛЕ вставки в DOM, поэтому box добавляется здесь, а не в конце. */
    wrap.appendChild(box);
    const bcs = getComputedStyle(box);
    const gl  = parseFloat(bcs.getPropertyValue('--gl')) || 0;
    const gm  = parseFloat(bcs.getPropertyValue('--gm')) || 0;
    const avail = Math.max(240, box.clientWidth - gl - gm - 2);
    /* ⚠️ НЕДЕЛЮ НЕ СЖИМАЕМ. Для месяцев ужимание оправдано: 63 месяца по 40 px
       уезжали на 2 500 px, и проект целиком было не увидеть. С неделями логика
       обратная — их включают, чтобы разглядеть детали внутри месяца, и 92
       деления по 12 px превращаются в нечитаемую гребёнку: ни номера недели,
       ни столбика. Поэтому у недель минимум 26 px, а диаграмма прокручивается
       вбок. Хотите увидеть всё разом — сузьте окно селектами «с … по …»:
       квартал это 13 недель, они помещаются целиком. */
    const WMIN = {w:26, m:11, q:20, y:44}[zoom] || 12;
    const W = Math.max(WMIN, Math.min(G_ZOOM[zoom].w, Math.floor(avail / nb)));
    /* узкое деление — подписи месяцев не помещаются, оставляем начала кварталов */
    box.classList.toggle('narrow', zoom==='m' && W < 26);
    box.style.setProperty('--gt', (nb*W)+'px');
    box.style.setProperty('--gcell', W+'px');

    const head = el('div','ghead');
    /* ⚠️ В РЕЖИМЕ НЕДЕЛЬ ВЕРХНЯЯ СТРОКА — МЕСЯЦЫ, А НЕ ГОДЫ. Без неё в ряду
       «36 37 38 …» невозможно понять, где сентябрь, а где март: номер недели
       сам по себе ничего не говорит. Месяц берётся по ЧЕТВЕРГУ недели — по
       ISO именно четверг решает, какому месяцу и году неделя принадлежит,
       иначе неделя 29.06–05.07 попала бы то в июнь, то в июль в зависимости
       от того, что считать. */
    const grpOf = b => {
      if(zoom!=='w') return bYear(b, zoom);
      const th = new Date(wkMonday(b).getTime() + 3*DAY);
      return th.getUTCFullYear()*12 + th.getUTCMonth();
    };
    const grpLabel = g => zoom!=='w' ? String(g)
                        : MONTH_RU[g%12] + ' ' + Math.floor(g/12);
    let years='', cells='', y0=null, cnt2=0;
    for(let b=sc.b0;b<=sc.b1;b++){
      const y = grpOf(b);
      if(y0===null) y0=y;
      if(y!==y0){ years+='<u style="width:'+(cnt2*W)+'px">'+esc(grpLabel(y0))+'</u>'; y0=y; cnt2=0; }
      cnt2++;
      /* начало месяца отбиваем так же, как начало квартала у месяцев */
      const qh = (zoom==='m' && b%3===0) || (zoom==='w' && grpOf(b)!==grpOf(b-1));
      cells+='<i class="'+(b===sc.tB?'now ':'')+(qh?'q':'')+'" title="'
            +esc(bTitle(b,zoom))+'">'+esc(zoom==='y'?'':bLabel(b,zoom))+'</i>';
    }
    years+='<u style="width:'+(cnt2*W)+'px">'+esc(grpLabel(y0))+'</u>';
    head.innerHTML = '<div class="glbl">Бизнес-план (проект)</div>'
      + '<div class="gmetrics"><span title="действующий срок вывоза">срок</span>'
        + '<span title="месяц последнего вывоза">факт</span>'
        + '<span title="доля прихода, которая уже вывезена">% выв.</span>'
        + '<span title="сколько всего пришло по правилам варианта">куплено</span>'
        + '<span title="сколько ещё лежит — по полному обороту складов, как колонка '
          + 'ОСТАТОК в дереве (перемещения и переработка учтены)">остаток</span>'
        + '<span title="'
          + esc('ПРОГНОЗ: сколько останется от этого бизнес-плана на базе, когда '
              + 'отработает наш план по объёмам. Остаток на 1-е число месяца-якоря '
              + '(' + (META.prod_anchor||META.prod_m0||'—') + ') минус плановое выбытие базы за '
              + ((META.prod_horizon||[]).join(', ') || '—') + ', разложенное по '
              + 'бизнес-планам ФИФО — от самых ранних завозов. Прочерк — остаток '
              + 'плана лежит не на базе (заготовка, площадка, цех) либо по его '
              + 'базе плана нет.'+FC_STALE())+'">прогноз</span>'
        + '<span title="на сколько месяцев вывоз вышел за срок">опозд.</span></div>'
      + '<div class="gtime"><div class="gyears">'+years+'</div>'
        + '<div class="gmonths">'+cells+'</div></div>';
    box.appendChild(head);

    shown.forEach(v=>{
      const g=v.g, t=v.t;
      const cls = (G_STATUS[v.status]||{}).c||'none';
      const late = v.delay!=null && v.delay>0;
      const row = el('div','grow st-'+cls);
      row.innerHTML =
          '<div class="glbl">'
        /* значки состава — отдельной колонкой слева от текста: они стоят на одной
           вертикали во всех строках, и список читается по ним сверху вниз */
        + matSet(g.series, 3)
        + '<div class="gltxt">'
        + '<div class="gl1"><span class="gst '+cls+'" title="'
          + esc((G_STATUS[v.status]||{}).t||'')+'">'
          + esc(statusLabel(v.status, V.key))+'</span>'
        + '<span class="gnm" title="'+esc(g.name+' · '+(g.contragent||'')
          +' · договор '+(g.contract||'—')+' · '+(g.site||''))+'">'+esc(g.name)+'</span></div>'
        + '<div class="gl2">'
          /* в Ганте колонка подписи узкая: один значок плюс «+N», остальное — в
             подсказке. Два значка вытесняли площадку и контрагента на свою строку */
          + bpTags(g.series, 1)
          + (g.site?'<span class="gsite" title="где металл вошёл в компанию">'
            +esc(g.site)+'</span>':'')
          + '<span class="gplan2'+(v.plan.edited?' ed':'')+'">срок '
            + (v.pm ? esc(v.pm) : '—')
            + '<button class="gpen" title="поправить срок вывоза">✎</button></span>'
          + (g.contragent?'<span class="tag">'+esc(g.contragent)+'</span>':'')
          /* ⚠️ Почему точек оплат мало: по этому договору часть платежей в 1С
             вообще без бизнес-плана. Приписать их плану нельзя — под одним
             договором десятки проектов (у 23Y0618 их одиннадцать), — но и
             молчать нельзя: экономист видит одну точку и думает, что расчёт
             потерял оплаты. Показываем счёт, не приписывая планам ни рубля. */
          + (g.pay_gap ? '<span class="tag paygap" title="'
              + esc('По договору ' + (g.contract||'—') + ' в 1С НЕ ПРОСТАВЛЕН '
                  + 'бизнес-план у ' + g.pay_gap.n + ' платежей на '
                  + money(g.pay_gap.sum) + ' ₽ (' + (g.pay_gap.dmin||'') + ' … '
                  + (g.pay_gap.dmax||'') + '). Такие платежи на диаграмме не '
                  + 'показаны: под одним договором лежат десятки бизнес-планов, '
                  + 'и разложить их по планам не на чем — это надо проставить в '
                  + '1С. Точки стоят только у платежей со своим БП.')
              + '">без БП в 1С: '+cnt(g.pay_gap.n)+' платежей</span>' : '')
        + '</div></div></div>'
        + '<div class="gmetrics">'
          + '<span class="gplan'+(v.plan.edited?' ed':'')+(v.pm?'':' muted')+'" title="'
            + esc(v.plan.src==='правка' ? 'срок поставлен вручную'
                 + (v.plan.note?': '+v.plan.note:'')+(g.plan_1c?' (в 1С '+g.plan_1c+')':'')
                 : v.plan.src==='снят вручную' ? 'срок снят вручную'
                   +(g.plan_1c?' (в 1С был '+g.plan_1c+')':'')
                 : 'плановая дата вывоза из справочника серий'
                   +(g.plans>1?' — позднейшая из '+g.plans+' серий проекта':''))
            /* точная дата — в подсказке: в колонке месяц, а флаг на шкале
               стоит именно по дате, и расхождение надо объяснить */
            + (v.pmd && v.pmd.slice(8)!=='01' ? ' · точная дата '+dateRu(v.pmd) : '')+'">'
            + (v.pm ? esc(v.pm) : 'нет срока')
            + (v.plan.edited?'<i class="edm" title="срок правлен вручную">✎</i>':'')
            + '<button class="gpen" title="поправить срок вывоза">✎</button></span>'
          + '<span'+(t.fact_end?'':' class="muted"')+' title="'
            + (t.fact_end?'месяц последнего вывоза':'вывоза не было')+'">'
            + (t.fact_end? esc(t.fact_end):'—')+'</span>'
          + '<span'+(t.pct==null?' class="muted"':'')+' title="вывезено от прихода'
            + (t.pct==null?'':': '+fmt(t.pct)+' %')+'">'
            + (t.pct==null?'—':fmt(t.pct)+' %')+'</span>'
          + '<span class="gbuy" title="приход по правилам варианта: '
            + esc(V.ii.map(i=>FSHORT[GFLOWS[i]]||GFLOWS[i]).join(' + '))+'">'
            + fmt(t.bought)+' '+unitLabel()+'</span>'
          /* ---- КОММЕНТАРИИ ПО ЗАПАСАМ ВИСЯТ НА ОСТАТКЕ ----
             Заказано 20.08.2026: «возможно, по клику на остаток». Так и сделано:
             остаток с комментариями становится кнопкой, рядом счётчик реплик.
             ⚠️ Значок ставится ТОЛЬКО когда лента есть: пустая кнопка на каждой
             строке приучила бы не нажимать вовсе. */
          + '<span class="grest'+(t.rest>0.5?'':' z')+(g.cm?' cm':'')+'" title="'
            + esc('Сколько ещё лежит: полный оборот складов — приход минус расход '
                + 'по ВСЕМ потокам, включая перемещения и переработку, как колонка '
                + 'ОСТАТОК в дереве.\nНажмите — откроются остатки этого бизнес-плана '
                + 'по подразделениям, группам учёта и номенклатуре (сверка с 1С).'
                + (g.cm ? '\n\nПО ЭТОМУ ОСТАТКУ ЕСТЬ КОММЕНТАРИИ ЭКОНОМИСТА — '
                        + g.cm.length + ' шт., свежий от ' + dateRu(g.cm[0].d)
                        + (g.cm[0].u ? ' (' + g.cm[0].u + ')' : '') + ':\n«'
                        + g.cm[0].t + '»\nНажмите на счётчик рядом, чтобы прочитать всю ленту.'
                        : ''))+'">'
            + fmt(t.rest)+' '+unitLabel()
            + (g.cm?'<i class="cmn" title="комментарии по запасам: '+g.cm.length
                   +'">'+g.cm.length+'</i>':'')+'</span>'
          /* ПРОГНОЗ ПО БАЗАМ. ⚠️ Стоит рядом с остатком нарочно: это ответ на
             вопрос «а что от него останется», и читать его надо парой.
             ⚠️ Считается ВСЕГДА ПО БАЗАМ и по разрезу «т» — независимо от
             выбранного варианта диаграммы. У варианта 1 (только заготовка) в
             колонке «остаток» стоит другое число, и это не рассогласование:
             план по объёмам заведён на базы, а не на заготовку. В подсказке
             написано прямо. */
          + (g.fc
              ? '<span class="gfc'+(g.fc.left>0.5?'':' z')+'" title="'
                + esc('База «'+g.fc.b+'»'+(g.fc.bn>1?' и ещё '+(g.fc.bn-1):'')
                    + ': остаток этого БП на 01.'+FC_A()+' — '
                    + fmt(g.fc.b0)+' т, план съедает '+fmt(g.fc.b0-g.fc.left)
                    + ' т за '+((META.prod_horizon||[]).join(', ')||'—')
                    + ', останется '+fmt(g.fc.left)+' т.'
                    + (gUnit()!=='т' ? ' Прогноз всегда в тоннах: план по объёмам заведён только в них.' : '')
                    + ' Очередь ФИФО — по первому завозу бизнес-плана.'
                    /* ⚠️ ОТКУДА РАСХОЖДЕНИЕ С КОЛОНКОЙ «ОСТАТОК». Прогноз
                       отсчитывается от 1-го числа МЕСЯЦА-ЯКОРЯ (позднейший из
                       нулевого месяца плана и месяца последних движений — см.
                       sales_report.py, «ЯКОРЬ ПРОГНОЗА») и только по базам, а
                       «остаток» — сегодня и по правилам варианта. Разница —
                       движения внутри месяца-якоря плюс площадки вне баз. */
                    + (Math.abs(t.rest - g.fc.b0) > Math.max(0.5, 0.05*Math.abs(g.fc.b0))
                        ? ' ⚠️ Это НЕ продолжение колонки «остаток»: там сегодняшний '
                          + 'остаток по правилам варианта (' + fmt(t.rest) + ' '
                          + unitLabel() + '), здесь — остаток на базе на 01.'
                          + FC_A() + '. Разница — движения внутри месяца-якоря.'
                        : '')
                    + FC_STALE())
                + '">'+fmt(g.fc.left)+' т</span>'
              : '<span class="muted" title="'
                + esc('Прогноза нет: остаток этого плана лежит не на базе '
                    + '(заготовка, площадка, цех) либо по его базе плана по '
                    + 'объёмам не заведено.')+'">—</span>')
          + (late? '<span class="gdelay" title="на сколько месяцев вывоз вышел за срок">+'
                   +v.delay+' мес</span>' : '<span></span>')
        + '</div>'
        /* наша дорожка — свойство ПРОЕКТА (номер БП в Битриксе один на проект),
           поэтому она есть на строке плана и не спускается в разбор по складам:
           число месяцев по складам не разложено, и деление его было бы выдумкой */
        + cellsHtml(t.m, sc, spanOf(t, v.pmd, cls, sc), g.pay,
                    ownSpan(g, t, sc, v.pmd), t.w, g.prod, g.fc);
      box.appendChild(row);

      row.querySelectorAll('.gpen').forEach(btn=>btn.addEventListener('click', ev=>{
        ev.stopPropagation(); openPlanEditor(g, ev.currentTarget);
      }));
      /* ОСТАТОК — ССЫЛКА НА ВКЛАДКУ «ОСТАТКИ» с этим планом и вариантом
         (заказчик 14.09.2026: «при клике на число открывается эта вкладка и
         выбранный БП»). Лента комментариев осталась — на счётчике рядом с
         числом, а не на самом числе: одно число — одно действие. */
      const gr = row.querySelector('.grest');
      if(gr) gr.addEventListener('click', ev=>{
        ev.stopPropagation();
        if(g.cm && ev.target.closest('.cmn')){ openNotes(g, ev.currentTarget); return; }
        goStock(g.series, V.key);
      });
      let det = null;
      row.querySelector('.gnm').addEventListener('click', ev=>{
        ev.stopPropagation();
        if(det){ det.remove(); det=null; row.classList.remove('open'); return; }
        det = detailsFor(g, V, sc, v.pmd);
        row.classList.add('open');
        row.after(det);
      });
    });
    /* Шкала строится от самого раннего прихода — а это 2021 год, где у
       просроченных бизнес-планов ещё ничего не происходит: диаграмма
       открывалась на пустом месте. Подводим её к «сегодня», оставив слева
       часть окна на предысторию. Если вся шкала влезла — прокручивать нечего. */
    if(sc.tB!=null){
      const left = (sc.tB - sc.b0) * W;
      box.scrollLeft = Math.max(0, left - Math.max(160, box.clientWidth * 0.45));
    }
    if(d.length>shown.length){
      const more = el('div','hint');
      more.innerHTML = 'Показаны '+cnt(shown.length)+' из '+cnt(d.length)
        + ' — <button class="gmore">показать ещё '
        + fmt(Math.min(200, d.length-shown.length))+'</button>';
      more.querySelector('.gmore').onclick = ()=>{ LIMIT += 200; draw(); };
      wrap.appendChild(more);
    }
    applySlim();
  }

  ['#gst','#gsort','#gopen','#gplan','#ggrp','#gdiv','#gca',
   '#gpay','#gfirst','#glast','#gover','#gnomove','#gprod','#gfc','#gm1','#gm2']
    /* ⚠️ ЧЕРЕЗ ПРОВЕРКУ НА NULL: «план» и «прогноз по базам» рисуются только
       когда выгрузка плана загружена, и без неё этот список валился на первом
       же отсутствующем элементе — вместе со всей вкладкой. */
    .forEach(s=>{ const e = $(s);
      if(e) e.addEventListener('change',()=>{ closePop(); LIMIT=200; draw(); }); });
  /* единица сбрасывает кэш потоков — сначала setMeasure, потом перерисовка */
  $('#gmeas').addEventListener('change', ()=>{
    closePop(); setMeasure($('#gmeas').value); LIMIT=200; rerenderTab(); });
  bar.querySelectorAll('.gvars button').forEach(b=>b.addEventListener('click',()=>{
    closePop(); vkey = b.dataset.v; LIMIT = 200;
    bar.querySelectorAll('.gvars button').forEach(x=>x.classList.toggle('on', x===b));
    try{ localStorage.setItem('metoptorg.gvar', vkey); }catch(e){}
    draw();
  }));
  $('#gq').addEventListener('input',()=>{
    closePop(); setBpFocus($('#gq').value, $('#gq')); LIMIT=200; draw(); });
  if($('#gprod')){
    $('#gprod').value = PROD_OP;
    $('#gprod').addEventListener('change', ()=>{
      PROD_OP = $('#gprod').value;
      try{ localStorage.setItem('metoptorg.prodop', PROD_OP); }catch(e){}
    });
  }
  if($('#gfc')){
    $('#gfc').checked = FC_ON;
    $('#gfc').addEventListener('change', ()=>{
      FC_ON = $('#gfc').checked;
      try{ localStorage.setItem('metoptorg.gfc', FC_ON ? '1' : '0'); }catch(e){}
    });
  }
  bar.querySelectorAll('.godsort button').forEach(b=>b.addEventListener('click',()=>{
    closePop();
    /* нажатие по активной снимает выбор — иначе из режима не выйти */
    odSort = (odSort === b.dataset.od) ? '' : b.dataset.od;
    bar.querySelectorAll('.godsort button')
       .forEach(x=>x.classList.toggle('on', x.dataset.od === odSort));
    LIMIT = 200; draw();
  }));
  bar.querySelectorAll('.gzoom:not(.godsort) button').forEach(b=>b.addEventListener('click',()=>{
    closePop();
    zoom = b.dataset.z;
    localStorage.setItem('metoptorg.gzoom', zoom);
    /* ⚠️ :not(.godsort) обязателен — у кнопок просрочки тот же класс-обёртка
       .gzoom (общий вид), и без него переключение шкалы гасило бы их подсветку */
    bar.querySelectorAll('.gzoom:not(.godsort) button')
       .forEach(x=>x.classList.toggle('on', x===b));
    draw();
  }));

  /* ---- ПЕРЕХОД С ДАШБОРДА С КОНТЕКСТНЫМИ ФИЛЬТРАМИ (финдир, 01.09.2026:
     «нашли проблемную точку на общем дашборде, ткнули — и открывается уже
     отфильтрованная Ганта, как контекстные фильтры в Power BI»). Пресет
     кладёт вкладка «Дашборд» в window.__GPRESET и зовёт render('gantt');
     здесь он применяется ОДИН раз и съедается — обычное открытие Ганты
     пресетов не имеет. */
  const PRE = window.__GPRESET;
  if(PRE){
    window.__GPRESET = null;
    try{
      if(PRE.var){ vkey = PRE.var;
        bar.querySelectorAll('.gvars button').forEach(x=>
          x.classList.toggle('on', x.dataset.v===PRE.var)); }
      /* ⚠️ ПОИСК — ЧАСТЬ ПРЕСЕТА, А НЕ МУСОР ОТ ПРОШЛОЙ ВКЛАДКИ. Дашборд шлёт
         в PRE.q имя конкретного плана (клик по строке «на грани» или «перешли
         в худшую полосу»), и раньше эта строка тут же затиралась пустой: Ганта
         открывалась целиком, будто ткнули мимо. Пустой q по-прежнему очищает
         поле — переход по полосе или дивизиону не должен тащить за собой
         чей-то прежний поиск. */
      const q0 = PRE.q == null ? '' : String(PRE.q);
      if($('#gq')) { $('#gq').value = q0; setBpFocus(q0, $('#gq')); }
      if(PRE.div != null && $('#gdiv'))  $('#gdiv').value = PRE.div;
      if(PRE.st  != null && $('#gst'))   $('#gst').value  = PRE.st;
      if(PRE.over!= null && $('#gover')) $('#gover').value = String(PRE.over);
      if(PRE.open && $('#gopen')) $('#gopen').checked = true;
      if($('#gsort')) $('#gsort').value = 'delay';
    }catch(e){}
  }
  draw();

  /* сервер — источник правды по правкам: подтягиваем и перерисовываем */
  if(PLAN_ONLINE){
    fetch('/api/plan-dates').then(r=>r.ok?r.json():null).then(srv=>{
      if(!srv || typeof srv!=='object') return;
      PLAN_EDITS = srv; planStore(); draw();
    }).catch(()=>{});
  }
};

/* ================================================================
   5) ПРОВЕРКА ДАННЫХ
   ================================================================ */
/* ================================================================
   СВОДНЫЕ — экран для совещания
   ================================================================
   Заказано 13.08.2026 на совещании с ген- и исполнительным директором:
   «быстро и чётко понять текущую ситуацию в разрезе каждого направления и групп
   аналитического учёта», чтобы директор раскрыл своё направление и увидел статус.

   ТРИ РЕШЕНИЯ ЗАКАЗЧИКА, НА КОТОРЫХ ВСЁ ДЕРЖИТСЯ:
   1) возраст — это ГЛУБИНА ПРОСРОЧКИ, сколько месяцев прошло ПОСЛЕ срока вывоза,
      а не сколько металл лежит. Отвечает «насколько нарушен срок»;
   2) периметры показываем РЯДОМ: вариант 1 «заготовка» — НЕ ВЫВЕЗЕНО, вариант 2
      «вся компания» — НЕ ПРОДАНО. То же разделение причин, что и в статусе Ганты;
   3) разрез переключается: направление · дивизион · группа учёта.

   ⚠️ ТОЛЬКО ТОННЫ. Сводная складывает планы между собой, а штуки, метры и тонны
   не складываются: смешать их значило бы вывести бессмысленное число крупным
   шрифтом на экран совещания.

   ⚠️ ПОЛОСЫ НЕ ПЕРЕСЕКАЮТСЯ и покрывают всё незакрытое: до 3, 3–6, 6–9, 9–12,
   больше 12, плюс «срок не прошёл» и «без срока». Просили «старше 3, 6, 9 и
   далее» — но накопительные полосы вкладывались бы одна в другую, и сложить
   столбец стало бы нельзя. Здесь сумма строк равна итогу, а «старше 6» читается
   как сумма трёх нижних строк.

   ⚠️ ПЛАН В НЕСКОЛЬКИХ СТРОКАХ РАЗРЕЗА. Направление у плана одно, а дивизионов и
   групп учёта бывает несколько. Тоннаж раскладывается ДОЛЯМИ — тогда сумма по
   столбцу сходится с итогом. Счётчик планов в таких разрезах складывается с
   перехлёстом: один план виден в двух строках. Так и подписано в заголовке. */
const SUM_BANDS = [
  {k:'b0',  n:'просрочено до 3 мес.'},
  {k:'b3',  n:'просрочено 3–6 мес.'},
  {k:'b6',  n:'просрочено 6–9 мес.'},
  {k:'b9',  n:'просрочено 9–12 мес.'},
  {k:'b12', n:'просрочено больше 12 мес.'},
  {k:'work',n:'срок ещё не прошёл'},
  {k:'nop', n:'без срока вывоза'},
];
function sumBand(st, delay){
  if(st==='без плана' || st==='битая дата вывоза') return 'nop';
  if(st==='в работе') return 'work';
  if(st!=='просрочен') return null;          /* закрытые в сводную не идут */
  const d = delay || 0;
  return d<=3 ? 'b0' : d<=6 ? 'b3' : d<=9 ? 'b6' : d<=12 ? 'b9' : 'b12';
}
/* Доли разреза внутри плана. У направления доля одна и равна единице, у
   дивизиона и группы учёта — по их тоннажу в плане. */
function sumParts(g, axis){
  if(axis==='dir') return [{name: g.direction || '— не указано —', share: 1}];
  /* ⚠️ НЕ bpM: он отдаёт блок ТЕКУЩЕЙ выбранной единицы, а сводная всегда
     считается в тоннах. Иначе, переключив вкладку «Движения» на штуки, человек
     вернулся бы сюда и увидел доли, посчитанные по штукам. */
  const m = (BPNM[g.series] && BPNM[g.series][MMAIN]) || null;
  const src = axis==='div' ? (m && m.d) : (m && m.g);
  let tot = 0;
  for(const k in (src||{})) tot += Math.max(+src[k]||0, 0);
  if(!tot) return [{name: '— не указано —', share: 1}];
  return Object.keys(src).map(k=>({name:k, share: Math.max(+src[k]||0,0)/tot}))
                         .filter(x=>x.share>0);
}
/* Незакрытые планы в обоих периметрах, уже с полосой и остатком. */
function sumRows(){
  const out = [];
  for(const g of (DATA.gantt||[])){
    const p = planOf(g), pmd = p.full || (p.month ? p.month+'-01' : '');
    const r = {g, v:{}};
    let any = false;
    for(const vk of ['v1','v2']){
      const t = (g.v && g.v[vk]) ? g.v[vk][MMAIN] : null;
      if(!t) continue;
      if(mClosed(t)) continue;               /* закрыт — в сводной не нужен */
      const dl = delayOf(g, t, p.month);
      r.v[vk] = {rest: t.rest||0, band: sumBand(statusOf(g,t,pmd), dl), delay: dl};
      any = true;
    }
    if(any) out.push(r);
  }
  return out;
}
/* ================================================================
   ДАШБОРД — сигнальный экран для оперативки (финдир, 01–02.09.2026)
   «Нужен дашборд, который смотрят каждое утро на оперативке: неделю назад
   состояние было другим, и РАЗНИЦА показывает, что сделали за неделю. Был
   объём непросроченный — на этой неделе он уже просроченный. И сколько
   остатков перейдёт в просроченные на следующей неделе, если не вывезем».
   Плюс правки заказчика 02.09: цифры прямо на линиях, группировка по
   региональному признаку, компактность, сверка сумм с Гантой.
   Полосы и остатки — ТЕ ЖЕ функции, что у «Сводных» (sumRows/sumBand/
   sumParts): дашборд обязан сходиться со сводной таблицей до тонны.
   ================================================================ */
/* ⚠️ ГРУППИРОВКА ПО ГЕОГРАФИИ, А НЕ ПО ПРИНАДЛЕЖНОСТИ (заказчик 02.09.2026:
   «сделай группировку всё-таки по регионам, а не по принадлежности»).
   На совещании спрашивают «что у нас в Коми», а не «что у нас на базах»: база
   Усинск и заготовка Коми — один регион и один разговор.
   ⚠️ КАРТА РЕГИОНОВ — РУЧНАЯ, И ИНАЧЕ НЕ ВЫХОДИТ. В справочнике подразделений
   1С географической иерархии нет: у всех баз родитель «БАЗЫ», а аналитическое
   подразделение у них своё («Усинск (База + Цех)»). Поэтому регион собирается
   по ключевым словам названия; что не опознано — уходит в «Прочее», а не
   растворяется молча. Появится новая база — допишите слово сюда. */
/* ⚠️ БЕЗ \b В РЕГУЛЯРКАХ: в JS граница слова считается по ASCII, и «юг\b» не
   совпадает со строкой «Юг», а «оса\b» — с «База Оса» (поймано на прогоне:
   оба дивизиона улетали в «Прочее»). Поэтому короткие слова обрамляем явно
   началом строки или пробелом. */
const DASH_REGIONS = [
  {k:'komi', n:'Коми',            re:/коми|усинск|ухта|щельяюр|одес/i},
  {k:'zs',   n:'Западная Сибирь', re:/западная сибирь|когалым|лангепас|покачи|урай|советск|свк|нягань|мегион|излучинск/i},
  {k:'perm', n:'Пермский край',   re:/пермск|пермь|осенц|березник|(^|\s)оса(\s|$)|чайковск|краснокамск/i},
  {k:'ug',   n:'Юг',              re:/южный|(^|\s)юг(\s|$)|астрахан|волгоград|буден|будён|ставропол|краснодар|нурлат|зис/i},
  {k:'centr',n:'Центр',           re:/центр|коломна|мгм|подольск|москв|солнечногорск/i},
  {k:'etc',  n:'Прочее',          re:null},
];
function dashGroup(div){
  const d = String(div||'');
  for(const R of DASH_REGIONS) if(R.re && R.re.test(d)) return R.k;
  return 'etc';
}
/* что именно попало в регион — видно в подсказке заголовка: карта ручная, и
   человек должен иметь возможность проверить, куда ушла его база */
const DASH_GROUPS = DASH_REGIONS.map(r=>({k:r.k, n:r.n,
  t:'Регион собирается по названию дивизиона; состав виден в строках ниже'}));

/* порядок полос от «спокойно» к «беда» — по нему считаются ухудшения за неделю */
const BAND_RANK = {nop:0, work:1, b0:2, b3:3, b6:4, b9:5, b12:6};

TABS.dash = function(app){
  const BANDC = {b0:'#eab308', b3:'#f59e0b', b6:'#ea580c', b9:'#dc2626',
                 b12:'#7f1d1d', work:'#2563eb', nop:'#94a3b8'};
  const PREV = DATA.pulse_prev || {};
  const HAS_PREV = Object.keys(PREV).length > 0;
  const PULSES = DATA.pulses || {};
  const PDATES = (META.pulse_dates || []).filter(d=>PULSES[d]);
  let CMP = '';                       /* дата второго экрана, '' — не показывать */
  let vk = localStorage.getItem('metoptorg.dashvar') || 'v1';
  if(!GVARS.some(v=>v.key===vk)) vk = (GVARS[0]||{}).key;

  app.appendChild(el('h2','', 'Дашборд'));
  const note = howto('Как пользоваться','');
  note.querySelector('.nb').innerHTML =
      '<b>Экран для оперативки.</b> Полоса — дивизион, длина и цифры — '
    + 'невывезенный остаток, цвет — глубина просрочки. Дивизионы разложены по '
    + 'смыслу: регионы заготовки, базы, демонтаж, прочее — на совещании их '
    + 'путать нельзя.<br>'
    + '<b>Клик по сегменту, карточке или дивизиону</b> открывает Ганту с уже '
    + 'выставленными фильтрами (дивизион, точная полоса просрочки, «только с '
    + 'остатком»).<br>'
    + (HAS_PREV
        ? '<b>Динамика за неделю</b> считается по слепку от '
          + esc(META.pulse_prev_date||'') + ' (' + (META.pulse_prev_age||0)
          + ' дн. назад): что вывезли, что перешло в просрочку. '
        : '<b>Динамика за неделю появится со следующей сборкой</b>: слепок '
          + 'состояния пишется при каждом расчёте, сравнивать пока не с чем. ')
    + '<b>«На грани»</b> — планы, у которых срок вывоза истекает в ближайшие '
    + '7 дней: если не вывезти, на следующей оперативке они станут просроченными.'
    + '<br>⚠️ <b>Про сверку с Гантой.</b> Остаток плана делится между '
    + 'дивизионами долями закупки (как на «Сводных»), а Ганта показывает план '
    + 'целиком. Поэтому в подсказке сегмента стоят ОБА числа: доля дивизиона и '
    + 'сколько тонн покажет Ганта после перехода.';
  app.appendChild(note);

  const bar = el('div','toolbar');
  /* ⚠️ Слово «вариант» из кнопок убрано (финдир 02.09: «вариант 2 можно прямо
     не писать — писать заготовка, вся компания»). */
  bar.innerHTML =
      '<span class="gzoom dvars">'
    + GVARS.map(v=>'<button data-v="'+esc(v.key)+'"'+(v.key===vk?' class="on"':'')
        +' title="'+esc(v.hint||'')+'">'
        +esc(String(v.title).replace(/^\s*\d+\s*[—-]\s*/,''))+'</button>').join('')
    + '</span>'
    + '<label class="chip" style="cursor:pointer" title="Спрятать дивизионы, '
      + 'где нечего обсуждать"><input type="checkbox" id="dsmall" checked> '
      + 'скрыть мелочь (до 100 т)</label>'
    /* окно «на грани» — переключателем: неделя это вопрос ближайшей оперативки,
       но на месяц вперёд смотрят при планировании вывоза */
    /* ⚠️ МАСШТАБ ПОЛОС — ВЫБОРОМ, А НЕ УГАДЫВАНИЕМ (заказчик 02.09.2026:
       «каждая диаграмма должна быть отдельно и данные в ней показываются
       относительно, чтобы видеть и другие суммы остатков»). Общая шкала
       честно сравнивает объёмы, но топит мелкие дивизионы; «доли» показывают
       состав каждого дивизиона независимо от размера — на совещании нужны оба
       взгляда, поэтому переключатель. */
    + '<span class="gzoom dscale" title="Как считать длину полос: общая шкала '
      + 'сравнивает дивизионы между собой; по региону — внутри своего блока; '
      + 'доли — каждая полоса на всю ширину, видно состав даже у мелких.">'
    + [['grp','шкала: по региону'],['all','шкала: общая'],['pct','шкала: доли']]
        .map(([k,t],i)=>'<button data-s="'+k+'"'+(i===0?' class="on"':'')+'>'
          +t+'</button>').join('')+'</span>'
    + '<span class="gzoom dedge" title="За сколько дней до срока считать план '
      + '«на грани»: столько у нас есть, чтобы не получить новую просрочку">'
    + [7,14,30].map(n=>'<button data-e="'+n+'"'+(n===7?' class="on"':'')
        +'>на грани: '+n+' дн.</button>').join('')+'</span>'
    /* ⚠️ СРАВНЕНИЕ — ВТОРЫМ ЭКРАНОМ, А НЕ ТОЛЬКО ЦИФРОЙ (уточнение заказчика
       02.09.2026: «чтобы дашборд ниже показывал результат данных на прошлый
       понедельник — для сравнения, какие случились движения»). Даты —
       понедельники: оперативка идёт по понедельникам. */
    + (PDATES.length ? '<span class="gzoom dcmp" title="Показать вторым экраном '
        + 'состояние на выбранный понедельник — сравнить, что изменилось">'
        + '<button data-c="" class="on">без сравнения</button>'
        + PDATES.slice(0,4).map(dt=>'<button data-c="'+esc(dt)+'">'
            + esc(dateRu(dt))+'</button>').join('')+'</span>' : '')
    + '<span class="muted" style="margin-left:auto">клик по любому числу — '
      + 'переход в Ганту с фильтрами</span>';
  app.appendChild(bar);
  const box = el('div','panel dashp');
  app.appendChild(box);

  function go(preset){
    window.__GPRESET = Object.assign({open:true, var:vk}, preset);
    render('gantt');
  }
  const bandPreset = b =>
      b==='work' ? {st:'в работе'} : b==='nop' ? {st:'без плана'} : {over:b};

  function draw(){
    const rows = sumRows();
    const hideSmall = $('#dsmall').checked;
    const EDGE = +((bar.querySelector('.dedge .on')||{}).dataset||{}).e || 7;
    const byDiv = {};                      /* дивизион -> {band: тонны} */
    const divFull = {};                    /* дивизион -> полный остаток и планы */
    const tot = {all:0, over:0, o3:0, o6:0, o12:0, work:0};
    /* динамика: вывезли / перешли в просрочку */
    const dyn = {gone:0, worse:[], newover:0, done:0};
    /* на грани: срок истекает в ближайшие 7 дней */
    const NOWD = META.period_max || ((META.today||'')+'-28');
    const edge = [];

    for(const r of rows){
      const v = r.v[vk];
      if(!v || !v.band) continue;
      tot.all += v.rest;
      if(v.band==='work') tot.work += v.rest;
      if(v.band[0]==='b'){
        tot.over += v.rest;
        const d = v.delay||0;
        if(d>3)  tot.o3  += v.rest;
        if(d>6)  tot.o6  += v.rest;
        if(d>12) tot.o12 += v.rest;
      }
      /* сравнение с прошлой неделей по этому же плану и варианту */
      const p = PREV[r.g.series] && PREV[r.g.series][vk];
      if(p){
        const wasRest = p[0], wasBand = p[1];
        if(wasRest > v.rest + 0.5) dyn.gone += wasRest - v.rest;
        if((BAND_RANK[v.band]||0) > (BAND_RANK[wasBand]||0)){
          dyn.worse.push({g:r.g, from:wasBand, to:v.band, t:v.rest});
          if(wasBand!=='b0' && v.band[0]==='b' && BAND_RANK[wasBand] < 2)
            dyn.newover += v.rest;      /* был не просрочен — стал просрочен */
        }
      }
      /* «на грани»: срок ещё не прошёл, но истекает в выбранном окне */
      if(v.band==='work'){
        const pm = planOf(r.g).full || '';
        if(pm && NOWD){
          const days = (Date.parse(pm) - Date.parse(NOWD)) / 86400000;
          if(days >= 0 && days <= EDGE) edge.push({g:r.g, days:Math.round(days), t:v.rest});
        }
      }
      for(const part of sumParts(r.g, 'div')){
        const a = byDiv[part.name] || (byDiv[part.name] = {});
        a[v.band] = (a[v.band]||0) + v.rest*part.share;
        const f = divFull[part.name] || (divFull[part.name] = {t:0, n:0});
        f.t += v.rest; f.n++;
      }
    }
    if(HAS_PREV){
      /* планы, которые ушли из списка совсем — закрыты за неделю */
      const cur = new Set(rows.filter(r=>r.v[vk]).map(r=>r.g.series));
      for(const sk in PREV) if(PREV[sk][vk] && !cur.has(sk)) dyn.done++;
    }

    /* ---- карточки: ОДИН РЯД, ОДИН РАЗМЕР, БЕЗ ДУБЛЕЙ ----
       ⚠️ Заказчик 02.09.2026: «убери то, что задвоилось, и сделай одного
       размера и в один ряд». Дублями были «просрочено» и «просрочено от
       6 мес.»: первое — ровно сумма пяти полос просрочки, второе — сумма трёх
       последних, и на экране одни и те же тонны стояли дважды. Осталось:
       общий итог, семь полос и блоки динамики. Разбивка «сколько просрочено» —
       в подсказке общего итога, чтобы число не потерялось совсем. */
    const bandTot = {};
    for(const r of rows){
      const v = r.v[vk];
      if(!v || !v.band) continue;
      bandTot[v.band] = (bandTot[v.band]||0) + v.rest;
    }
    const card = (val, lbl, opts) => {
      const o = opts || {};
      return '<button class="dcard'+(o.cls?' '+o.cls:'')+'" data-p=\''
        + JSON.stringify(o.preset||{})+'\''
        + (o.tip?' title="'+esc(o.tip)+'"':'')+'>'
        + '<b>'+(o.color?'<i style="background:'+o.color+'"></i>':'')+fmt(val)+' т</b>'
        + esc(lbl)+'</button>';
    };
    let h = '<div class="dcards">'
      + card(tot.all, 'не вывезено всего', {cls:'sum', preset:{},
          tip:'Всё, что ещё не вывезено. Из них просрочено '+fmt(tot.over)
              + ' т, из них глубже 6 месяцев — '+fmt(tot.o6)+' т.'})
      + SUM_BANDS.map(b=>{
          const v = bandTot[b.k]||0;
          if(v<=0.5) return '';
          return card(v, b.n, {cls:'bnd', color:BANDC[b.k],
            preset:bandPreset(b.k), tip:'Открыть Ганту: '+b.n});
        }).join('')
      + (HAS_PREV
          ? '<div class="dcard dyn"><b>'+fmt(dyn.gone)+' т</b>вывезли за неделю'
            + '<i>с ' + esc(META.pulse_prev_date||'') + '</i></div>'
            + '<div class="dcard dyn'+(dyn.worse.length?' bad':'')+'"><b>'
            + fmt(dyn.worse.reduce((s2,x)=>s2+x.t,0))+' т</b>ухудшились<i>'
            + cnt(dyn.worse.length)+' планов'
            + (dyn.newover>0.5 ? ' · впервые просрочены '+fmt(dyn.newover)+' т' : '')
            + '</i></div>'
          : '')
      + (edge.length
          ? '<div class="dcard edge"><b>'+fmt(edge.reduce((s2,x)=>s2+x.t,0))+' т</b>'
            + 'на грани<i>срок за '+EDGE+' дн. · '+cnt(edge.length)+' планов</i></div>'
          : '')
      + '</div>';

    /* ---- что изменилось за неделю: короткий список ---- */
    if(HAS_PREV && dyn.worse.length){
      const top = dyn.worse.sort((a,b)=>b.t-a.t).slice(0,6);
      h += '<div class="dchg"><div class="dchg-h">Перешли в худшую полосу за '
        + 'неделю <span class="muted">(' + cnt(dyn.worse.length) + ', показаны '
        + 'крупнейшие)</span></div>'
        + top.map(x=>'<button class="dchg-i" data-bp="'+esc(x.g.series)+'">'
            + '<span class="dchg-n" title="'+esc(x.g.name)+'">'+esc(x.g.name)+'</span>'
            + '<span class="dchg-b">'+esc(bandName(x.from))+' → <b>'
            + esc(bandName(x.to))+'</b></span>'
            + '<span class="dchg-t">'+fmt(x.t)+' т</span></button>').join('')
        + '</div>';
    }
    if(edge.length){
      const top = edge.sort((a,b)=>a.days-b.days || b.t-a.t).slice(0,6);
      h += '<div class="dchg edge"><div class="dchg-h">На грани просрочки '
        + '<span class="muted">(срок истекает в ближайшие '+EDGE+' дн.; '
        + cnt(edge.length)+' планов)</span></div>'
        + top.map(x=>'<button class="dchg-i" data-bp="'+esc(x.g.series)+'">'
            + '<span class="dchg-n" title="'+esc(x.g.name)+'">'+esc(x.g.name)+'</span>'
            + '<span class="dchg-b">через '+x.days+' дн.</span>'
            + '<span class="dchg-t">'+fmt(x.t)+' т</span></button>').join('')
        + '</div>';
    }

    /* ---- ЛЕГЕНДА ЦВЕТОВ (заказчик 02.09.2026: «верни подписи, что значит
       каждый цвет») — без неё полосы читаются как абстрактная раскраска ---- */
    h += '<div class="dlegend">' + SUM_BANDS.map(b=>
        '<span title="'+esc(b.n)+'"><i style="background:'+BANDC[b.k]+'"></i>'
        + esc(b.n)+'</span>').join('') + '</div>';

    /* ---- группы дивизионов ---- */
    /* второй экран «как было» строится ТОЙ ЖЕ функцией: иначе экраны разъедутся
       по правилам, и сравнивать станет нечего (уточнение заказчика 02.09) */
    const divsOf = (src) => Object.keys(src)
      .map(d=>({d, sum:Object.values(src[d]).reduce((a,b)=>a+b,0), b:src[d],
                grp:dashGroup(d)}))
      .filter(x=>!hideSmall || x.sum>=100)
      .sort((a,b)=>b.sum-a.sum);
    const divs = divsOf(byDiv);
    /* «было»: те же доли дивизионов, но остатки и полосы из слепка */
    let wasByDiv = null, wasTot = 0;
    if(CMP && PULSES[CMP]){
      wasByDiv = {};
      const P = PULSES[CMP];
      for(const g of (DATA.gantt||[])){
        const p = P[g.series] && P[g.series][vk];
        if(!p) continue;
        const rest = p[0], band = p[1];
        if(!band || rest<=0.5) continue;
        wasTot += rest;
        for(const part of sumParts(g, 'div')){
          const a = wasByDiv[part.name] || (wasByDiv[part.name] = {});
          a[band] = (a[band]||0) + rest*part.share;
        }
      }
    }
    const wasDivs = wasByDiv ? divsOf(wasByDiv) : null;
    /* ⚠️ ШКАЛА — СВОЯ У КАЖДОГО РЕГИОНА (заказчик 02.09.2026: «каждая
       диаграмма должна быть отдельно, и данные в ней показываются
       относительно, чтобы видеть и другие суммы остатков»). При общей шкале
       База Ухта со 110 т рядом с Западной Сибирью на 11 610 т превращалась в
       невидимую чёрточку, и внутри региона сравнивать было нечего. Абсолют
       читается справа числом, а длина полосы говорит про место ВНУТРИ региона.
       ⚠️ Но у «было» и «стало» шкала ОДНА И ТА ЖЕ (максимум по обоим экранам
       региона): со своей у каждого полосы одинаковой длины означали бы разные
       тонны, и сравнение врало бы глазами. */
    const SCALE = ((bar.querySelector('.dscale .on')||{}).dataset||{}).s || 'grp';
    const mxAll = Math.max(...divs.map(x=>x.sum),
                           ...((wasDivs||[]).map(x=>x.sum)), 1);
    const mxOf = (k) => SCALE==='all' ? mxAll : Math.max(
      ...divs.filter(x=>x.grp===k).map(x=>x.sum),
      ...((wasDivs||[]).filter(x=>x.grp===k).map(x=>x.sum)), 1);

    const gridOf = (list, prevList) => {
      let g2 = '<div class="dgrid">';
      DASH_GROUPS.forEach(G=>{
        const sub = list.filter(x=>x.grp===G.k);
        if(!sub.length) return;
        const gsum = sub.reduce((s2,x)=>s2+x.sum,0);
        const mx = mxOf(G.k);            /* своя шкала региона, см. ⚠️ выше */
        g2 += '<div class="dgrp"><div class="dgrp-h" title="'+esc(G.t)+'">'
          + esc(G.n)+' <span class="muted">'+cnt(sub.length)+' · '+fmt(gsum)+' т'
          + (SCALE==='pct' ? ' · доли внутри дивизиона'
                           : ' · шкала до '+fmt(mx)+' т')+'</span></div>';
        sub.forEach(x=>{
          const was = prevList ? (prevList.find(y=>y.d===x.d)||{}).sum : null;
          const dlt = was==null ? null : x.sum - was;
          g2 += '<div class="drow">'
            + '<button class="ddiv" data-div="'+esc(x.d)+'" title="'
              + esc('Открыть Ганту по дивизиону «'+x.d+'». Доля дивизиона '+fmt(x.sum)
                  + ' т; на Ганте покажется '+fmt((divFull[x.d]||{}).t||0)+' т по '
                  + cnt((divFull[x.d]||{}).n||0)+' планам — план целиком, а не доля.')
              + '">'+esc(x.d)+'</button>'
            + '<div class="dbar">';
          SUM_BANDS.forEach(b=>{
            const v = x.b[b.k]||0;
            if(v<=0.5) return;
            /* доли: полоса дивизиона всегда во всю ширину, сегменты — его
               состав; так у Базы Ухта со 110 т видно то же, что у региона */
            const w = SCALE==='pct' ? 100*v/(x.sum||1) : 100*v/mx;
            g2 += '<button class="dseg" data-div="'+esc(x.d)+'" data-b="'+b.k+'" '
              + 'style="width:'+w+'%;background:'+BANDC[b.k]+'" title="'
              + esc(x.d+' · '+b.n+': '+fmt(v)+' т (доля дивизиона). Клик — Ганта '
                  + 'с этой полосой; там показывается полный остаток планов.')
              + '">'+(w>=10 ? '<i>'+fmtShort(v)+'</i>' : '')+'</button>';
          });
          g2 += '</div><span class="dsum" title="итог дивизиона (доля)">'
            + fmt(x.sum)+'</span>'
            + (dlt==null ? ''
               : '<span class="ddlt '+(dlt<-0.5?'good':dlt>0.5?'bad':'zero')+'" title="'
                 + esc('было на '+dateRu(CMP)+': '+fmt(was)+' т')+'">'
                 + (dlt<-0.5?'▼ ':dlt>0.5?'▲ ':'')+fmt(Math.abs(dlt))+'</span>')
            + '</div>';
        });
        g2 += '</div>';
      });
      return g2 + '</div>';
    };

    h += '<div class="dscr"><div class="dscr-h">Сейчас <span class="muted">'
      + esc(META.period_max||'')+' · '+fmt(tot.all)+' т не вывезено</span></div>'
      + gridOf(divs, wasDivs) + '</div>';

    if(wasDivs){
      h += '<div class="dscr was"><div class="dscr-h">Было на '
        + esc(dateRu(CMP))+' <span class="muted">'+fmt(wasTot)+' т не вывезено · '
        + (tot.all<wasTot ? 'за период вывезли '+fmt(wasTot-tot.all)+' т'
                          : 'прибавилось '+fmt(tot.all-wasTot)+' т')
        + '</span></div>' + gridOf(wasDivs, null) + '</div>';
    }
    box.innerHTML = h;

    box.querySelectorAll('.dcard[data-p]').forEach(c=>c.addEventListener('click',
      ()=>go(JSON.parse(c.dataset.p))));
    box.querySelectorAll('.ddiv').forEach(c=>c.addEventListener('click',
      ()=>go({div:c.dataset.div})));
    box.querySelectorAll('.dseg').forEach(c=>c.addEventListener('click',
      ()=>go(Object.assign({div:c.dataset.div}, bandPreset(c.dataset.b)))));
    /* строка списка изменений ведёт на конкретный план */
    box.querySelectorAll('.dchg-i').forEach(c=>c.addEventListener('click',()=>{
      const g = (DATA.gantt||[]).find(x=>x.series===c.dataset.bp);
      go({q: g ? g.name : ''});
    }));
  }
  bar.querySelectorAll('.dvars button').forEach(b=>b.addEventListener('click',()=>{
    vk = b.dataset.v;
    try{ localStorage.setItem('metoptorg.dashvar', vk); }catch(e){}
    bar.querySelectorAll('.dvars button').forEach(x=>x.classList.toggle('on', x===b));
    draw();
  }));
  $('#dsmall').addEventListener('change', draw);
  bar.querySelectorAll('.dscale button').forEach(b=>b.addEventListener('click',()=>{
    bar.querySelectorAll('.dscale button').forEach(x=>x.classList.toggle('on', x===b));
    draw();
  }));
  bar.querySelectorAll('.dedge button').forEach(b=>b.addEventListener('click',()=>{
    bar.querySelectorAll('.dedge button').forEach(x=>x.classList.toggle('on', x===b));
    draw();
  }));
  /* выбор даты второго экрана «как было» */
  bar.querySelectorAll('.dcmp button').forEach(b=>b.addEventListener('click',()=>{
    CMP = b.dataset.c || '';
    bar.querySelectorAll('.dcmp button').forEach(x=>x.classList.toggle('on', x===b));
    draw();
  }));
  draw();
};
/* короткое имя полосы для списка изменений */
function bandName(k){
  const b = SUM_BANDS.find(x=>x.k===k);
  return b ? b.n.replace('просрочено ','') : k;
}
/* компактное число для подписи внутри сегмента: «1,2 тыс» вместо «1 234,567» */
function fmtShort(v){
  const a = Math.abs(v);
  if(a >= 1000) return (v/1000).toFixed(1).replace('.',',')+' тыс';
  if(a >= 100)  return String(Math.round(v));
  return fmt(v).replace(/,\d+$/,'');
}

TABS.summary = function(app){
  const rows = sumRows();
  const axis0 = localStorage.getItem('metoptorg.sumaxis') || 'dir';
  const AX = {dir:'направление', div:'дивизион', grp:'группа учёта'};

  app.appendChild(el('h2','', 'Сводные'));
  const note = el('div','note');
  note.innerHTML = '<div class="nb"><b>Экран для совещания.</b> Показаны только '
    + 'НЕЗАКРЫТЫЕ бизнес-планы — те, по которым ещё есть остаток. '
    + '<b>Не вывезено</b> — вариант «заготовка»: металл не увезли с площадки. '
    + '<b>Не продано</b> — вариант «вся компания»: увезли, но не реализовали. '
    + 'Глубина просрочки считается от срока вывоза 1С до конца выгрузки ('
    + esc(META.today||'') + '). Полосы не пересекаются: сумма строк равна итогу, '
    + '«старше 6 месяцев» — это сумма трёх нижних строк. Всё в тоннах: складывать '
    + 'штуки с тоннами нельзя. <b>Щёлкните по числу планов</b> — раскроется список '
    + 'бизнес-планов, а по названию плана — откроется диаграмма Ганта с ним.</div>';
  app.appendChild(note);

  const tot = k => rows.reduce((s,r)=>s + ((r.v[k]&&r.v[k].rest)||0), 0);
  const cntv = k => rows.filter(r=>r.v[k]).length;
  const cards = el('div','cards');
  cards.innerHTML =
      '<div class="card lead"><div class="k">Не вывезено</div><div class="v">'
    + fmt(tot('v1'))+' <span class="unit">т</span></div>'
    + '<div class="s">вариант «заготовка» — металл не увезли с площадки</div></div>'
    + '<div class="card"><div class="k">Не продано</div><div class="v">'
    + fmt(tot('v2'))+' <span class="unit">т</span></div>'
    + '<div class="s">вариант «вся компания» — увезли, но не реализовали</div></div>'
    + '<div class="card"><div class="k">Планов не закрыто</div><div class="v">'
    + cnt(cntv('v1'))+' / '+cnt(cntv('v2'))+'</div>'
    + '<div class="s">по вывозу / по продаже</div></div>';
  app.appendChild(cards);

  /* ---------- СПИСОК БИЗНЕС-ПЛАНОВ ПОД ЛЮБОЙ ЦИФРОЙ ----------
     Заказчик 13.08.2026: «как понять, какие БП». Сводная без этого отвечает
     «сколько», но не «кто», и с совещания приходится идти искать руками.
     Раскрывается щелчком по ЧИСЛУ ПЛАНОВ — там, где цифра, там и список: цифра
     и её расшифровка не должны жить в разных местах экрана.
     По названию плана — переход на Ганту с ним в поиске: сводная отвечает
     «кто», а «почему» видно только на диаграмме. */
  const LIM = 100;
  function bpList(list, vk){
    const key = vk==='v1' ? 'v1' : 'v2';
    const srt = list.slice().sort((x,y)=>((y.v[key]&&y.v[key].rest)||0)
                                       - ((x.v[key]&&x.v[key].rest)||0));
    const head = srt.slice(0, LIM);
    return '<table class="inner"><thead><tr><th>Бизнес-план</th>'
      + '<th>срок вывоза</th><th class="num">просрочка</th>'
      + '<th class="num">не вывезено, т</th><th class="num">не продано, т</th>'
      + '</tr></thead><tbody>'
      + head.map(r=>{
          const p = planOf(r.g);
          const d = r.v[key] ? r.v[key].delay : null;
          return '<tr><td><a class="bpjump" href="#gantt" data-bp="'+esc(r.g.name)+'">'
            + esc(r.g.name)+'</a></td>'
            + '<td class="mut">'+esc(p.month||'—')+'</td>'
            + '<td class="num">'+(d!=null && d>0 ? '+'+d+' мес' : '')+'</td>'
            + '<td class="num">'+(r.v.v1 ? '<span class="stjump" data-s="'+esc(r.g.series)
                + '" data-v="v1" title="остатки по подразделениям и номенклатуре">'+fmt(r.v.v1.rest)+'</span>' : '')+'</td>'
            + '<td class="num">'+(r.v.v2 ? '<span class="stjump" data-s="'+esc(r.g.series)
                + '" data-v="v2" title="остатки по подразделениям и номенклатуре">'+fmt(r.v.v2.rest)+'</span>' : '')+'</td></tr>';
        }).join('')
      + '</tbody></table>'
      + (srt.length>LIM ? '<div class="hint">Показаны '+cnt(LIM)+' крупнейших из '
          + cnt(srt.length)+' — остальные меньше по остатку.</div>' : '');
  }
  /* Раскрытие: строка-гармошка под той, по которой щёлкнули. Второй щелчок
     закрывает. Открытая строка подсвечивается, иначе на длинной таблице не
     видно, чей это список. */
  function wireDrill(table, get){
    table.querySelectorAll('td.drill').forEach(td=>{
      td.addEventListener('click', ()=>{
        const tr = td.parentElement, nx = tr.nextElementSibling;
        if(nx && nx.classList.contains('bpdrill')){
          nx.remove(); tr.classList.remove('open'); return;
        }
        table.querySelectorAll('tr.bpdrill').forEach(x=>x.remove());
        table.querySelectorAll('tr.open').forEach(x=>x.classList.remove('open'));
        const {list, vk} = get(td);
        if(!list || !list.length) return;
        const row = document.createElement('tr');
        row.className = 'bpdrill';
        row.innerHTML = '<td colspan="'+tr.children.length+'">'+bpList(list, vk)+'</td>';
        tr.after(row); tr.classList.add('open');
        row.querySelectorAll('.bpjump').forEach(a=>a.addEventListener('click', ev=>{
          ev.preventDefault();
          setBpFocus(a.dataset.bp);
          render('gantt');
        }));
        row.querySelectorAll('.stjump').forEach(a=>a.addEventListener('click', ev=>{
          ev.stopPropagation(); goStock(a.dataset.s, a.dataset.v);
        }));
      });
    });
  }

  /* ---- глубина просрочки ---- */
  const t1 = el('div','panel');
  const agg = {};
  SUM_BANDS.forEach(b=>{ agg[b.k] = {v1:{n:0,t:0,l:[]}, v2:{n:0,t:0,l:[]}}; });
  rows.forEach(r=>{ for(const vk of ['v1','v2']){
    const x = r.v[vk]; if(!x || !x.band) continue;
    agg[x.band][vk].n++; agg[x.band][vk].t += x.rest; agg[x.band][vk].l.push(r); }});
  const numCell = (k,vk,n) => n
    ? '<td class="num drill" data-k="'+k+'" data-v="'+vk+'" title="Показать эти бизнес-планы">'+n+'</td>'
    : '<td class="num"></td>';
  t1.innerHTML = '<h3>Глубина просрочки</h3>'
    + '<div class="tw-scroll"><table id="sumdepth"><thead><tr><th></th>'
    + '<th class="num">планов<br><span class="mut">не вывезено</span></th>'
    + '<th class="num">тонн<br><span class="mut">не вывезено</span></th>'
    + '<th class="num">планов<br><span class="mut">не продано</span></th>'
    + '<th class="num">тонн<br><span class="mut">не продано</span></th></tr></thead><tbody>'
    + SUM_BANDS.map(b=>'<tr><td>'+esc(b.n)+'</td>'
        + numCell(b.k,'v1',agg[b.k].v1.n)
        + '<td class="num">'+(agg[b.k].v1.t>0.0005?fmt(agg[b.k].v1.t):'')+'</td>'
        + numCell(b.k,'v2',agg[b.k].v2.n)
        + '<td class="num">'+(agg[b.k].v2.t>0.0005?fmt(agg[b.k].v2.t):'')+'</td></tr>').join('')
    + '<tr class="sum"><td><b>ИТОГО</b></td>'
    + '<td class="num drill" data-k="*" data-v="v1"><b>'+cnt(cntv('v1'))+'</b></td>'
    + '<td class="num"><b>'+fmt(tot('v1'))+'</b></td>'
    + '<td class="num drill" data-k="*" data-v="v2"><b>'+cnt(cntv('v2'))+'</b></td>'
    + '<td class="num"><b>'+fmt(tot('v2'))+'</b></td></tr>'
    + '</tbody></table></div>';
  app.appendChild(t1);
  wireDrill(t1.querySelector('#sumdepth'), td=>{
    const k = td.dataset.k, vk = td.dataset.v;
    return {vk, list: k==='*' ? rows.filter(r=>r.v[vk]) : agg[k][vk].l};
  });

  /* ---- разрез ---- */
  const t2 = el('div','panel');
  app.appendChild(t2);
  function drawAxis(axis){
    localStorage.setItem('metoptorg.sumaxis', axis);
    const by = new Map();
    rows.forEach(r=>{
      sumParts(r.g, axis).forEach(p=>{
        const a = by.get(p.name) || {n1:0,t1:0,n2:0,t2:0,deep:0,l1:[],l2:[]};
        if(r.v.v1){ a.n1++; a.t1 += r.v.v1.rest * p.share; a.l1.push(r);
                    if(r.v.v1.band==='b12') a.deep += r.v.v1.rest * p.share; }
        if(r.v.v2){ a.n2++; a.t2 += r.v.v2.rest * p.share; a.l2.push(r); }
        by.set(p.name, a);
      });
    });
    const list = [...by.entries()].sort((x,y)=>y[1].t2 - x[1].t2 || y[1].t1 - x[1].t1);
    /* ⚠️ У направления 162 значения, и 91 из них встречается ровно у одного
       плана — туда попали номера договоров. Показывать их все значит утопить
       экран совещания; показываем крупные, остальное честно сворачиваем — но
       список планов у «прочих» тоже раскрывается, ничего не теряется. */
    const CAP = 14;
    const head = list.slice(0, CAP), tail = list.slice(CAP);
    const tailAgg = tail.reduce((a,[,x])=>({n1:a.n1+x.n1,t1:a.t1+x.t1,n2:a.n2+x.n2,
      t2:a.t2+x.t2,deep:a.deep+x.deep,l1:a.l1.concat(x.l1),l2:a.l2.concat(x.l2)}),
      {n1:0,t1:0,n2:0,t2:0,deep:0,l1:[],l2:[]});
    const cellN = (i,vk,n) => n
      ? '<td class="num drill" data-i="'+i+'" data-v="'+vk+'" title="Показать эти бизнес-планы">'+n+'</td>'
      : '<td class="num"></td>';
    const line = (name, x, i, mut) => '<tr'+(mut?' class="mut"':'')+'><td>'+esc(name)+'</td>'
      + cellN(i,'v1',x.n1) + '<td class="num">'+(x.t1>0.0005?fmt(x.t1):'')+'</td>'
      + cellN(i,'v2',x.n2) + '<td class="num">'+(x.t2>0.0005?fmt(x.t2):'')+'</td>'
      + '<td class="num">'+(x.deep>0.0005?fmt(x.deep):'')+'</td></tr>';
    t2.innerHTML = '<h3>Разрез: '+esc(AX[axis])+'</h3>'
      + '<div class="hint">Переключите разрез, чтобы увидеть ту же картину по '
      + 'направлениям, дивизионам или группам учёта. '
      + (axis==='dir'
         ? 'У направления одно значение на план, поэтому планы складываются точно.'
         : 'У плана бывает несколько '+(axis==='div'?'дивизионов':'групп учёта')
           +', поэтому тоннаж разложен долями — сумма столбца сходится с итогом, '
           +'а счётчик планов складывается с перехлёстом: один план виден в '
           +'нескольких строках.')
      + ' Щёлкните по числу планов — раскроется список.</div>'
      + '<div class="row" style="margin-bottom:10px">'
      + Object.keys(AX).map(a=>'<button class="btn3 sumax'+(a===axis?' on':'')
          +'" data-ax="'+a+'">'+esc(AX[a])+'</button>').join(' ')
      + '</div>'
      + '<div class="tw-scroll"><table id="sumaxis"><thead><tr><th>'+esc(AX[axis])+'</th>'
      + '<th class="num">планов</th><th class="num">не вывезено, т</th>'
      + '<th class="num">планов</th><th class="num">не продано, т</th>'
      + '<th class="num">из них просрочено<br><span class="mut">больше 12 мес.</span></th>'
      + '</tr></thead><tbody>'
      + head.map(([n,x],i)=>line(n,x,i)).join('')
      + (tail.length ? line('прочие ('+cnt(tail.length)+' значений)', tailAgg, 'tail', true) : '')
      + '</tbody></table></div>';
    t2.querySelectorAll('.sumax').forEach(b=>b.addEventListener('click',()=>drawAxis(b.dataset.ax)));
    wireDrill(t2.querySelector('#sumaxis'), td=>{
      const i = td.dataset.i, vk = td.dataset.v;
      const x = i==='tail' ? tailAgg : head[+i][1];
      return {vk, list: vk==='v1' ? x.l1 : x.l2};
    });
  }
  drawAxis(axis0 in AX ? axis0 : 'dir');
};
/* ================================================================
   ЗАКУПКА ПО БИЗНЕС-ПЛАНУ (заказ финдира, запись 01.09.2026)
   «Вкладка чисто по поводу закупки: сравниваешь, что мы собирались купить по
   Excel и как мы это приходовали здесь; туда же плановая выручка. Сколько было
   в Excel трубы 73 и на какую сумму, сколько в результате купили».
   Здесь строится ФАКТИЧЕСКАЯ сторона мостика: закупка 1С по ГОЛОВНОЙ серии
   (без V/VS — прямое правило заказчика) в разрезе нашей НМК и складов, рядом
   плановая выручка подтверждённой версии расчёта. Плановые ПОЗИЦИИ из Excel
   (номенклатура текстом Лукойла и объём) подключаются следующим шагом — под
   них оставлены колонки и место в структуре.
   ================================================================ */
TABS.bpbuy = function(app){
  const ROWS = DATA.bpbuy || [];
  app.appendChild(el('h2','', 'Закупка по бизнес-плану'));
  if(!ROWS.length){
    app.appendChild(el('div','hint','Данных о закупке нет: пересоберите отчёт.'));
    return;
  }
  const note = howto('Что здесь сравнивается','');
  note.querySelector('.nb').innerHTML =
      '<b>Одна строка — бизнес-план</b>, внутри — наша номенклатура (НМК) и '
    + 'склады, на которые металл оприходовали. <b>Куплено</b> — столбец '
    + '«купили» из себестоимости товаров по сериям, <b>продано</b> — по тем же '
    + 'позициям. <b>План выручки</b> — из версии расчёта, отмеченной верной на '
    + 'вкладке «Версии БП» (или из версии по умолчанию), отдельно по трекам '
    + '«лук» и «дсп».'
    + '<br>⚠️ <b>Берётся только ГОЛОВНАЯ серия</b>, без «V» и «VS»: что '
    + 'написано в Excel-расчёте, то должно пройти по головной серии. Серии '
    + 'V/VS (довозы и пересорт) в это сравнение не входят.'
    + '<br><b>План против факта построчно.</b> Плановые позиции из Excel '
    + 'разложены по нашей НМК: в таблице плана видно, на какую нашу позицию '
    + 'легла строка, в таблице факта — колонки «план, т» и «план, ₽». '
    + 'Сопоставление идёт по смыслу позиции — вид (лом / труба / штанга / '
    + 'кабель / цветмет), марка лома, размеры, — потому что текст у Лукойла и '
    + 'у нас разный («Труба НКТ 73х5.5 б/у общ.назнач.» против «Труба НКТ '
    + '73*5,5 б/у*»). Что не разнеслось, посчитано под таблицей: строка '
    + '«металлолом» без марки при нескольких марках в факте долями НЕ '
    + 'раскладывается — это была бы выдумка.'
    + '<br><b>Факт выручки</b> — из регистра 1С «Выручка и себестоимость '
    + 'продаж» (без НДС): в шапке проекта рядом с планом, в каждой ветке — '
    + 'процент выполнения, в таблице факта — по каждой нашей позиции. Рубли '
    + 'разнесены по сериям так же, как тонны продаж, и только по головной серии.'
    + '<br>⚠️ <b>Чего пока нет.</b> <b>Сумм закупки нет в '
    + 'выгрузке 1С</b>: в «СебестоимостьТоваровОбороты» только количества, для '
    + '«на какую сумму купили» нужна отдельная колонка от 1С.';
  app.appendChild(note);

  /* ---- ДЕЙСТВУЮЩИЕ / СТАРЫЕ БП (заказчик 14.09.2026) ----
     «Площадки и серии без номера БП скрой — это старые БП, они уже не
     актуальны; сделай их во вкладке „старые БП“ и не учитывай в общем
     счётчике». Признак `old` ставит расчёт: у псевдо-серий договорных
     площадок, серий без номера БП и у БЕЗ СЕРИИ в Битриксе расчёта нет и
     быть не может. Счётчик и итог считаются ТОЛЬКО по показанной группе. */
  const N_OLD = ROWS.filter(r=>r.old).length;
  const bar = el('div','toolbar');
  bar.innerHTML =
      '<span class="gzoom bbold">'
      + '<button data-o="0" class="on" title="Проекты с номером бизнес-плана — у них '
        + 'есть (или может быть) версия расчёта в Битриксе">действующие БП</button>'
      + '<button data-o="1" title="Договорные площадки, серии без номера БП и металл '
        + 'без серии: в Битриксе расчёта под них нет, план подтянуть некуда. В общий '
        + 'счётчик не входят.">старые БП'+(N_OLD?' <i>'+cnt(N_OLD)+'</i>':'')+'</button>'
      + '</span>'
    + '<input type="search" id="bbq" placeholder="Поиск: номер БП / проект / номенклатура / склад…">'
    + '<select id="bbsort">'
      + '<option value="t">сортировка: по объёму закупки</option>'
      + '<option value="rev">по плановой выручке</option>'
      + '<option value="bp">по номеру БП</option>'
      + '<option value="nm">по числу номенклатур</option></select>'
    + '<label class="chip" style="cursor:pointer" title="Оставить только планы, '
      + 'у которых есть плановая выручка из расчёта"><input type="checkbox" id="bbrev"> только с планом выручки</label>'
    + '<span class="muted" style="margin-left:auto" id="bbcnt"></span>';
  app.appendChild(bar);
  const box = el('div','panel');
  app.appendChild(box);
  let LIMIT = 40;

  const revOf = r => {
    if(!r.rev) return null;
    let sum = 0; const parts = [];
    ['luk','dsp','other'].forEach(tr=>{
      const d = r.rev[tr]; if(!d) return;
      sum += d.v;
      /* pt/ps/it — плановый объём, сумма и позиции версии (парсер 02.09.2026);
         без них ветка показывала «— план закупки» при живых данных в снимке */
      /* ⚠️ ПЕРЕНОСИТЬ ВСЁ, ЧТО НУЖНО ВЕТКЕ. Один раз уже ловил «— план
         закупки» при живых данных: revOf собирает СВОИ объекты, и поле, не
         переписанное сюда, до вкладки просто не доезжает. mk/mt/ug/un —
         связка плана Excel с нашей НМК из расчёта. */
      parts.push({tr, v:d.v, ver:d.ver, ok:d.ok,
                  pt:d.pt||0, ps:d.ps||0, it:d.it||[],
                  mk:d.mk||[], mt:d.mt||null, ug:d.ug||null, un:d.un||null,
                  mw:d.mw||null, mp:d.mp||[]});
    });
    return {sum, parts};
  };
  const TRN = {luk:'лук', dsp:'дсп', other:'—'};

  /* ---------- ПЛАН EXCEL, РАЗНЕСЁННЫЙ ПО НАШЕЙ НМК ----------
     Заказчик 02.09.2026: «почему не заполнены план, т и план, ₽ — у тебя же
     есть суммы по каждой позиции из Excel и конкретный вес из 1С». Связку
     строит расчёт (P.match_plan_items): по виду позиции (лом / труба / штанга
     / кабель / цветмет), марке лома и размерам. Здесь только показ.
     ⚠️ ЧТО НЕ РАЗНЕСЛОСЬ — ВИДНО ПОД ТАБЛИЦЕЙ, А НЕ ПОТЕРЯНО. Плановая строка
     «Металлолом (черный металл)» без марки при трёх марках в факте по НМК не
     раскладывается: доля была бы выдумкой. Такие тонны и рубли собраны в
     отдельной подписи, поэтому сумма колонки «план» всегда меньше или равна
     плану версии — и никогда его не превышает. */
  const planOf1 = (p, n) => (p && p.mt && p.mt[n.c]) || null;
  /* ---------- ДОЛЯ ЛОТА И ЮРЛИЦО (сделки Битрикса, 14.09.2026) ----------
     План в Excel — на ВЕСЬ лот, а лот часто делится с партнёром (УВМ).
     Наш факт сравнивается с планом × наша доля («план × доля»). Доля под
     вопросом в выгрузке («50%?») помечается. Без записи о сделке колонок
     «план × доля» нет — считать план нашим целиком было бы неверно ровно в
     половине проектов. */
  const dealOf = r => r.deal || null;
  const shareOf = r => { const d = dealOf(r); return d && d.share != null ? d.share : null; };
  const pct = x => (100*x).toFixed(0)+' %';
  const dealChips = r => {
    const d = dealOf(r); if(!d) return '';
    const sh = d.share;
    return (sh != null
        ? '<span class="tag deal'+(d.share_q?' q':'')+'" title="'+esc('Доля Металлолом в лоте по '
            + 'реестру сделок Битрикса'+(d.uvm!=null?' (УВМ — '+pct(d.uvm)+')':'')
            + (d.share_q?'. В выгрузке стоит знак вопроса — доля под сомнением':'')
            + (d.lot_t?'. Объём лота по запросу: '+fmt(d.lot_t)+' т':''))+'">доля '+pct(sh)
            + (d.share_q?' ?':'')+'</span>'
        : '<span class="tag muted" title="В реестре сделок доля не заполнена">доля —</span>')
      + (d.ul ? '<span class="tag" title="Юрлицо, на которое выиграно КП">'+esc(d.ul)+'</span>' : '')
      + (d.stage ? '<span class="tag muted" title="Стадия сделки в Битриксе">'+esc(d.stage)+'</span>' : '');
  };
  const planCells = (p, n) => {
    if(!p || !p.mt){
      return '<td class="num plan muted" title="У этого проекта нет версии '
        + 'расчёта с плановыми позициями — сравнивать не с чем">—</td>'
        + '<td class="num plan muted">—</td>';
    }
    const v = planOf1(p, n);
    if(!v){
      return '<td class="num plan muted" title="В плане этой версии такой '
        + 'позиции нет: либо её не планировали, либо она названа так, что по '
        + 'виду и размеру не сошлась ни с одной строкой плана — см. подпись '
        + 'под таблицей">—</td><td class="num plan muted">—</td>';
    }
    /* факт против плана — в подсказке и цветом: перевыполнение зелёным,
       недобор красным. Считаем от плана, поэтому нулевой план цвета не даёт. */
    const d = v[0] > 0.0005 ? (n.t - v[0]) / v[0] : null;
    const cls = d == null ? '' : (n.t + 0.0005 >= v[0] ? ' ok' : ' bad');
    const tip = 'План ' + fmt(v[0]) + ' т, куплено ' + fmt(n.t) + ' т'
      + (d == null ? '' : ' (' + (d >= 0 ? '+' : '') + (100*d).toFixed(0) + ' % к плану)')
      + (v[1] > 0.5 ? ', плановая стоимость ' + money(v[1]) + ' ₽' : '');
    return '<td class="num plan' + cls + '" title="' + esc(tip) + '">' + fmt(v[0]) + '</td>'
      + '<td class="num plan' + (v[1] > 0.5 ? '' : ' muted') + '" title="' + esc(tip) + '">'
      + (v[1] > 0.5 ? money(v[1]) : '—') + '</td>';
  };
  const shareCells = (p, n, r) => {
    const sh = shareOf(r); if(sh==null) return '';
    const v = planOf1(p, n);
    if(!v) return '<td class="num plan sh muted">—</td><td class="num plan sh muted">—</td>';
    const t = v[0]*sh, rub = v[1]*sh;
    const cls = t>0.0005 ? (n.t+0.0005>=t?' ok':' bad') : '';
    const tip = 'План × доля '+pct(sh)+': '+fmt(t)+' т, куплено '+fmt(n.t)+' т'
      + (t>0.0005 ? ' ('+(n.t-t>=0?'+':'')+(100*(n.t-t)/t).toFixed(0)+' %)' : '');
    return '<td class="num plan sh'+cls+'" title="'+esc(tip)+'">'+fmt(t)+'</td>'
      + '<td class="num plan sh'+(rub>0.5?'':' muted')+'" title="'+esc(tip)+'">'+(rub>0.5?money(rub):'—')+'</td>';
  };
  /* строка итогов: колонки таблицы должны складываться, иначе цифрам не верят */
  const planFoot = (r, p) => {
    if(!r || !r.nm) return '';
    const ft = sum(r.nm, n=>n.t), fs = sum(r.nm, n=>n.s);
    const pt = p && p.mt ? Object.keys(p.mt).reduce((a,k)=>a+p.mt[k][0],0) : 0;
    const ps = p && p.mt ? Object.keys(p.mt).reduce((a,k)=>a+p.mt[k][1],0) : 0;
    const fr = sum(r.nm, n=>n.r||0);
    return '<tfoot><tr><td><b>Итого</b></td>'
      + '<td class="num"><b>'+fmt(ft)+'</b></td>'
      + '<td class="num">'+(fs>0.0005?fmt(fs):'—')+'</td>'
      + (HAS_RUB ? '<td class="num rub"><b>'+(fr>0.5?money(fr):'—')+'</b></td>' : '')
      + '<td></td>'
      + '<td class="num plan"><b>'+(pt>0.0005?fmt(pt):'—')+'</b></td>'
      + '<td class="num plan">'+(ps>0.5?money(ps):'—')+'</td>'
      + (shareOf(r)!=null ? '<td class="num plan sh"><b>'+(pt>0.0005?fmt(pt*shareOf(r)):'—')+'</b></td>'
          + '<td class="num plan sh">'+(ps>0.5?money(ps*shareOf(r)):'—')+'</td>' : '')
      + '</tr></tfoot>';
  };
  const planNote = (p) => {
    if(!p || !p.mt) return '';
    const pt = Object.keys(p.mt).reduce((a,k)=>a+p.mt[k][0],0);
    const ug = p.ug || [0,0,0], un = p.un || [0,0,0];
    const parts = [];
    parts.push('разнесено по НМК <b>'+fmt(pt)+' т</b>'
      + (p.pt>0.0005 ? ' из '+fmt(p.pt)+' т плана версии' : ''));
    if(ug[0] > 0.0005)
      parts.push('<span title="В плане написано просто «металлолом», а в 1С по '
        + 'этому проекту пришло несколько марок. Разложить долями было бы '
        + 'выдумкой, поэтому эти тонны стоят отдельно.">марка в плане не '
        + 'указана — ' + fmt(ug[0]) + ' т ('+cnt(ug[2])+' поз.)</span>');
    if(un[0] > 0.0005)
      parts.push('<span title="Ни по виду, ни по размеру не сошлось ни с одной '
        + 'нашей позицией: обычно это план по трубе, которая в 1С оприходована '
        + 'уже ломом, или позиция, которой по этой серии не покупали.">не '
        + 'сошлось с нашей НМК — ' + fmt(un[0]) + ' т ('+cnt(un[2])+' поз.)</span>');
    return '<div class="bbpn">' + parts.join(' · ') + '</div>';
  };


  function draw(){
    const q = ($('#bbq').value||'').trim().toLowerCase();
    const so = $('#bbsort').value, onlyRev = $('#bbrev').checked;
    const showOld = ((bar.querySelector('.bbold .on')||{}).dataset||{}).o === '1';
    let list = ROWS.filter(r=>{
      if(!!r.old !== showOld) return false;
      if(onlyRev && !r.rev) return false;
      if(!q) return true;
      if((r.bp+' '+r.name+' '+(r.ca||'')).toLowerCase().includes(q)) return true;
      return r.nm.some(n=>(n.n||'').toLowerCase().includes(q)
        || (n.w||[]).some(w=>(w[0]||'').toLowerCase().includes(q)));
    });
    const cmp = {
      t:(a,b)=>b.t-a.t,
      rev:(a,b)=>((revOf(b)||{}).sum||0)-((revOf(a)||{}).sum||0),
      bp:(a,b)=>(+b.bp||0)-(+a.bp||0),
      nm:(a,b)=>b.nm.length-a.nm.length,
    }[so] || ((a,b)=>b.t-a.t);
    list = list.slice().sort(cmp);
    const shown = list.slice(0, LIMIT);
    $('#bbcnt').textContent = cnt(list.length)+(showOld?' старых':'')+' планов · '
      + fmt(list.reduce((s,r)=>s+r.t,0))+' т закупки';

    box.innerHTML = shown.map(r=>{
      const rv = revOf(r);
      return '<div class="bbrow" data-s="'+esc(r.s)+'">'
        + '<div class="bbh">'
          + '<span class="tw">▶</span>'
          + '<b>'+(r.bp?'БП '+esc(r.bp):'<span class="muted">'+esc(r.old||'без номера БП')+'</span>')+'</b>'
          + '<span class="bbnm" title="'+esc(r.name)+'">'+esc(r.name)+'</span>'
          + (r.ca?'<span class="tag">'+esc(r.ca)+'</span>':'')
          + dealChips(r)
          + '<span class="bbv" title="сколько оприходовали по головной серии">'
            + fmt(r.t)+' т <i>куплено</i></span>'
          + '<span class="bbv" title="продано по тем же позициям">'
            + fmt(r.sold)+' т <i>продано</i></span>'
          /* ФАКТ ВЫРУЧКИ по головной серии — рядом с планом, чтобы читалось
             одной строкой: «план 27,7 млн · факт 24,3 млн» (14.09.2026) */
          + (HAS_RUB && r.rub>0.5 ? '<span class="bbv" title="факт выручки без НДС '
              + 'по головной серии, регистр «Выручка и себестоимость продаж» по '
              + esc(META.revenue.max_date||'—')+'">'+money(r.rub)+' ₽ <i>факт выручки</i></span>'
            /* МАРЖА = выручка − себестоимость продаж (оба из регистра 1С, по
               головной серии). Отрицательная — красным: продали дешевле, чем
               обошлось. */
            + '<span class="bbv'+((r.rub-(r.cost||0))<-0.5?' bad':'')+'" title="выручка − себестоимость продаж '
              + '(регистр 1С): '+money(r.rub)+' − '+money(r.cost||0)+'">'
              + money(r.rub-(r.cost||0))+' ₽ <i>маржа'+(r.rub>0.5?' '+(100*(r.rub-(r.cost||0))/r.rub).toFixed(0)+' %':'')+'</i></span>' : '')
          + (rv ? '<span class="bbv rev" title="'
              + esc(rv.parts.map(p=>TRN[p.tr]+': '+money(p.v)+' ₽ — '
                  + (p.ok?'версия подтверждена':'версия по умолчанию')
                  + ' («'+p.ver+'»)').join('\n'))+'">'
              + money(rv.sum)+' ₽ <i>план выручки</i></span>'
            : '<span class="bbv muted" title="Плановой выручки нет: у этого БП '
              + 'не найдена версия расчёта с «Выручкой без НДС» — проверьте '
              + 'вкладку «Версии БП»">— <i>план выручки</i></span>')
        + '</div><div class="bbkids"></div></div>';
    }).join('')
      + (list.length>shown.length
          ? '<button class="gmore" id="bbmore">показать ещё '
            + Math.min(200, list.length-shown.length)+' (из '
            + (list.length-shown.length)+')</button>' : '');

    box.querySelectorAll('.bbrow').forEach(row=>{
      const r = ROWS.find(x=>x.s===row.dataset.s);
      const kids = row.querySelector('.bbkids');
      /* ---- РАЗБОР: ПРОЕКТ -> ЛУКОЙЛ / ДСП -> НОМЕНКЛАТУРА ----
         Заказчик 02.09.2026: «сделай декомпозицию как в других вкладках: проект,
         потом выбор лукойл или дсп, и внутри каждого открывается НМК — это же
         две разные версии одного и того же». Поэтому треки — раскрывающиеся
         ветки, у каждой своя таблица.
         ⚠️ ФАКТ ЗАКУПКИ В ВЕТКАХ ОДИН И ТОТ ЖЕ: в 1С металл приходуется один
         раз, по версиям расчёта он не делится. Различаются ПЛАНЫ — выручка
         версии сейчас и плановые позиции из Excel, когда их подключим. Об этом
         сказано в шапке ветки, чтобы никто не сложил факт дважды. */
      /* ⚠️ ТАБЛИЦА ФАКТА ЗНАЕТ ПРО ПЛАН СВОЕЙ ВЕТКИ. Расчёт (P.match_plan_items)
         разложил плановые позиции Excel по нашей НМК: p.mt — {код: [т, ₽]}.
         Ветки лук и дсп берут РАЗНЫЕ версии расчёта, поэтому план у одной и той
         же строки факта в них разный, а факт один и тот же. */
      const nmTable = (p) => '<table class="bbt"><thead><tr>'
        + '<th>номенклатура (наша НМК)</th>'
        + '<th class="num">куплено, т</th><th class="num">продано, т</th>'
        + (HAS_RUB ? '<th class="num rub" title="Факт выручки без НДС по этой позиции '
            + 'на головной серии (регистр «Выручка и себестоимость продаж»)">выручка, ₽</th>' : '')
        + '<th>склады оприходования</th>'
        + '<th class="num plan">план, т</th><th class="num plan">план, ₽</th>'
        + (shareOf(r)!=null ? '<th class="num plan sh" title="'+esc('План × наша доля лота ('
            + pct(shareOf(r))+') — с этим и сравнивать наш факт')+'">план × доля, т</th>'
            + '<th class="num plan sh">план × доля, ₽</th>' : '')
        + '</tr></thead><tbody>'
        + r.nm.map(n=>{
            /* ⚠️ ПОЧЕМУ КУПИЛИ ТРУБУ, А ПРОДАЛИ ЛОМ (финдир 02.09.2026):
               берём цепочку переработки проекта из снимка — сколько по коду
               ушло в переработку и вернулось. */
            const ch = (DATA.chain||{})[r.s] || {};
            const sum = (arr) => (arr||[]).filter(x=>x.code===n.c)
                .reduce((a,x)=>a+(x.tonnes||0), 0);
            const inp = sum(ch.in), outp = sum(ch.out);
            const tip = inp>0.5 || outp>0.5
              ? 'Переработка по этой позиции: ушло '+fmt(inp)+' т, вернулось '
                + fmt(outp)+' т. Так труба и штанга превращаются в лом: '
                + 'купили одно, продаём другое.'
              : '';
            return '<tr'+(tip?' class="mix" title="'+esc(tip)+'"':'')+'>'
              + '<td title="'+esc(n.c)+'">'+esc(n.n)
                + (tip?' <i class="mixi" title="'+esc(tip)+'">⇄</i>':'')+'</td>'
              + '<td class="num">'+fmt(n.t)+'</td>'
              + '<td class="num'+(n.s>0.0005?'':' muted')+'">'+(n.s>0.0005?fmt(n.s):'—')+'</td>'
              + (HAS_RUB ? '<td class="num rub'+((n.r||0)>0.5?'':' muted')+'">'+((n.r||0)>0.5?money(n.r):'—')+'</td>' : '')
              + (()=>{
                  /* ПЛАН ПО СКЛАДАМ (финдир: «наша НМК × склады»): место хранения
                     из Excel сопоставлено с нашим складом по городу/площадке
                     (P.match_place); рядом с фактом склада — «план N т». */
                  const pw = (p && p.mw && p.mw[n.c]) || {};
                  const line = w => esc(w[0])+' <i>'+fmt(w[1])+'</i>'
                    + (pw[w[0]] ? ' <u class="pw" title="'+esc('план по этому месту хранения: '
                        + fmt(pw[w[0]][0])+' т'+(pw[w[0]][1]>0.5?', '+money(pw[w[0]][1])+' ₽':''))
                        + '">план '+fmt(pw[w[0]][0])+'</u>' : '');
                  const tip = (n.w||[]).map(w=>w[0]+' — '+fmt(w[1])+' т'
                    + (pw[w[0]] ? ' (план '+fmt(pw[w[0]][0])+' т)' : '')).join('\n');
                  const noW = Object.keys(pw).filter(k=>!(n.w||[]).some(w=>w[0]===k));
                  return '<td class="bbw" title="'+esc(tip)+'">'
                    + (n.w||[]).slice(0,2).map(line).join(', ')
                    + ((n.w||[]).length>2 ? ' <span class="muted">и ещё '+((n.w||[]).length-2)+'</span>' : '')
                    + '</td>'; })()
              + planCells(p, n)
              + shareCells(p, n, r)
            + '</tr>';
          }).join('')
        + '</tbody>' + planFoot(r, p) + '</table>'
        + planNote(p);

      row.querySelector('.bbh').addEventListener('click', ()=>{
        const open = row.classList.toggle('open');
        row.querySelector('.tw').textContent = open ? '▼' : '▶';
        if(open && !kids.dataset.built){
          kids.dataset.built = '1';
          const rv = revOf(r);
          if(rv && rv.parts.length){
            /* ветки версий: сначала выбор лук / дсп, номенклатура внутри */
            kids.innerHTML = rv.parts.map(p=>
                '<div class="bbver" data-tr="'+esc(p.tr)+'">'
              + '<div class="bbvh"><span class="tw">▶</span>'
                + '<b>'+esc(TRN[p.tr]==='—'?'без признака':TRN[p.tr].toUpperCase())+'</b>'
                + (()=>{ const sh = shareOf(r); const base = sh!=null ? p.v*sh : p.v;
                    return '<span class="bbvv">'+money(p.v)+' ₽ <i>план выручки'
                      + (sh!=null && sh<0.9995 ? ' · наша доля '+pct(sh)+' = '+money(base)+' ₽' : '')+'</i>'
                      + (HAS_RUB ? '<em class="'+(r.rub>=base-0.5?'ok':'no')+'" title="'
                          + esc('Факт выручки без НДС по головной серии: '+money(r.rub)
                            + ' ₽ (регистр 1С по '+(META.revenue.max_date||'—')+'); план версии '
                            + money(p.v)+' ₽'+(sh!=null?', наша доля лота '+pct(sh)+' — '+money(base)+' ₽':''))
                          + '">факт '+money(r.rub)+' ₽'
                          + (base>0.5 ? ' · '+(100*r.rub/base).toFixed(0)+' %'+(sh!=null&&sh<0.9995?' от доли':'') : '')+'</em>' : '')
                      + '</span>'; })()
                + (p.pt>0.0005
                    ? '<span class="bbvp" title="'+esc('Плановый объём закупки по '
                        + 'этой версии расчёта: '+fmt(p.pt)+' т; факт по головной '
                        + 'серии: '+fmt(r.t)+' т')+'">'+fmt(p.pt)+' т <i>план закупки</i>'
                      + '<em class="'+(r.t>=p.pt-0.5?'ok':'no')+'">'
                        + (r.t>=p.pt-0.5?'факт ≥ плана':'факт '+fmt(r.t)+' т')+'</em></span>'
                    : '<span class="bbvp muted">— <i>план закупки</i></span>')
                + '<span class="bbvs'+(p.ok?' ok':'')+'" title="'+esc(p.ver)+'">'
                  + (p.ok?'✓ версия подтверждена':'версия по умолчанию')+'</span>'
              + '</div><div class="bbvk"></div></div>').join('')
              + '<div class="bbtr-note">Факт закупки в обеих ветках ОДИН И ТОТ ЖЕ: '
              + 'в 1С металл приходуется один раз, по версиям расчёта он не '
              + 'делится. Различаются планы — выручка версии и плановые позиции '
              + 'из Excel, когда их подключим.</div>';
            kids.querySelectorAll('.bbver').forEach(vr=>{
              const vk2 = vr.querySelector('.bbvk');
              /* ⚠️ часть берём ПО data-tr, а не из замыкания генерации HTML:
                 обработчик вешается в отдельном проходе, и переменная цикла
                 rv.parts.map(p=>…) здесь уже недоступна (ловил ReferenceError) */
              const p = rv.parts.find(x=>x.tr===vr.dataset.tr) || {};
              vr.querySelector('.bbvh').addEventListener('click', ()=>{
                const o2 = vr.classList.toggle('open');
                vr.querySelector('.tw').textContent = o2 ? '▼' : '▶';
                if(o2 && !vk2.dataset.built){
                  vk2.dataset.built = '1';
                  const it = (p.it||[]);
                  /* ⚠️ ПЛАН И ФАКТ — ДВЕ РАЗНЫЕ ТАБЛИЦЫ, А НЕ ОДНА СТРОКА В
                     СТРОКУ. В Excel номенклатура написана словами Лукойла
                     («Металлолом 5А (ремонт ж/д пути)»), у нас — своей НМК, и
                     учётчики схлопывают несколько плановых позиций в одну нашу
                     (финдир: «эти три позиции для нас всё 5А»). Пока связка
                     «текст -> НМК» не построена, честнее показать обе таблицы
                     рядом и сверять итоги, чем склеивать строки наугад. */
                  vk2.innerHTML =
                      (it.length
                        ? '<div class="bbpl"><div class="bbpl-h">План из расчёта '
                          + '<span class="muted">'+cnt(it.length)+' позиций · '
                          + fmt(p.pt)+' т'+(p.ps>0?' · '+money(p.ps)+' ₽':'')
                          + ' · лист «'+esc(p.ver.split('|').pop().trim())+'»</span></div>'
                          + '<table class="bbt plan"><thead><tr>'
                          + '<th>номенклатура (как в Excel)</th><th>место хранения</th>'
                          + '<th>категория</th><th class="num">объём, т</th>'
                          + '<th class="num">цена, ₽</th><th class="num">стоимость, ₽</th>'
                          /* ⚠️ СВЯЗКА ВИДНА В ОБЕ СТОРОНЫ: в плане — на какую
                             нашу позицию легла строка, в факте — сколько плана
                             на неё пришло. Без этой колонки экономист не может
                             проверить сопоставление, а верить ему на слово в
                             деньгах нельзя. */
                          + '<th>легло на нашу НМК</th>'
                          + '</tr></thead><tbody>'
                          + it.map((x,ix)=>'<tr>'
                              + '<td title="'+esc(x.nomenclature||'')+'">'+esc((x.nomenclature||'').slice(0,80))+'</td>'
                              + '<td>'+esc(x.division||x.supplier||'—')+'</td>'
                              + '<td>'+esc(x.category||'—')+'</td>'
                              + '<td class="num">'+fmt(x.volume||0)+'</td>'
                              + '<td class="num">'+(x.price?money(x.price):'—')+'</td>'
                              + '<td class="num">'+(x.cost?money(x.cost):'—')+'</td>'
                              + (()=>{ const mc = (p.mk||[])[ix];
                                  const nn = mc && (r.nm||[]).find(y=>y.c===mc);
                                  const pl = (p.mp||[])[ix];
                                  return nn
                                    ? '<td class="bbmk" title="'+esc('Плановая '
                                        + 'строка отнесена к нашей позиции «'+nn.n
                                        + '»: куплено по ней '+fmt(nn.t)+' т'
                                        + (pl ? '. Место хранения сопоставлено с нашим складом «'+pl+'»'
                                              : '. Место хранения с нашим складом не сошлось'))+'">'
                                      + esc(nn.n)
                                      + (pl ? '<br><small class="muted">'+esc(pl)+'</small>'
                                            : '<br><small class="muted">склад — ?</small>')+'</td>'
                                    : '<td class="bbmk muted" title="'+esc('Эту '
                                        + 'строку плана не с чем сопоставить: '
                                        + 'либо марка не указана, либо такой '
                                        + 'позиции в 1С по головной серии нет')
                                      + '">—</td>'; })()
                            + '</tr>').join('')
                          + '</tbody>'
                          /* ИТОГО плана (заказчик 14.09.2026): объём, стоимость и
                             сколько строк легло на нашу НМК — чтобы сверять с
                             итогом таблицы факта одним взглядом */
                          + (()=>{ const tv = it.reduce((a,x)=>a+(x.volume||0),0);
                              const tc = it.reduce((a,x)=>a+(x.cost||0),0);
                              const nOk = (p.mk||[]).filter(Boolean).length;
                              return '<tfoot><tr><td><b>Итого</b> <span class="muted">'+cnt(it.length)+' поз.</span></td>'
                                + '<td></td><td></td><td class="num"><b>'+fmt(tv)+'</b></td><td></td>'
                                + '<td class="num"><b>'+(tc>0.5?money(tc):'—')+'</b></td>'
                                + '<td class="bbmk">'+(nOk===it.length ? 'легли все'
                                    : cnt(nOk)+' из '+cnt(it.length)+' легли')+'</td></tr></tfoot>'; })()
                          + '</table></div>'
                        : '<div class="hint">Плановых позиций в этой версии '
                          + 'расчёта не нашлось: в файле нет таблицы «Номенклатура '
                          + '/ Объём покупки» либо лист заполнен по-другому.</div>')
                    + '<div class="bbpl-h">Факт 1С <span class="muted">по головной '
                      + 'серии, наша НМК и склады — план из таблицы выше разнесён '
                      + 'по нашим позициям</span></div>'
                    + nmTable(p);
                }
              });
            });
          } else {
            /* версий расчёта нет — лишний уровень не заводим, сразу таблица */
            kids.innerHTML = nmTable(null);
          }
        }
      });
    });
    const more = box.querySelector('#bbmore');
    if(more) more.addEventListener('click', ()=>{ LIMIT+=200; draw(); });
  }
  ['#bbq','#bbsort','#bbrev'].forEach(sel=>{
    const e = $(sel);
    if(e) e.addEventListener(sel==='#bbq'?'input':'change', ()=>{ LIMIT=40; draw(); });
  });
  bar.querySelectorAll('.bbold button').forEach(b=>b.addEventListener('click', ()=>{
    bar.querySelectorAll('.bbold button').forEach(x=>x.classList.toggle('on', x===b));
    LIMIT=40; draw();
  }));
  draw();
};

/* ================================================================
   ВЕРСИИ РАСЧЁТОВ БП (заказано 25.08.2026)
   У одного файла БП несколько листов-версий расчёта с разной выручкой.
   Какая верна — знает только экономист: эта вкладка и есть место, где он
   это говорит. Галочка = «версия верна, алгоритмы используют её» (в т.ч.
   число месяцев вывоза в load_bp_months перекрывается выбором); крестик =
   «версия неверна, из расчётов исключить». Пока выбора нет, подсвечена
   версия С ПОСЛЕДНЕЙ ДАТОЙ — правило заказчика: «по умолчанию та, которая
   была изменена последней». Отметки хранит сервер (data/bp_version_choice
   .json) и подхватывает пересборка.
   ================================================================ */
TABS.bpver = function(app){
  const ROWS = DATA.bpver || [];
  app.appendChild(el('h2','', 'Версии расчётов БП'));
  if(!ROWS.length){
    app.appendChild(el('div','hint','Выгрузки версий ещё нет: она появляется '
      +'после еженедельного прогона парсера БП (вс 03:10) или ручного запуска '
      +'metoptorg-bp-weekly на сервере.'));
    return;
  }
  const note = howto('Что это и как отмечать','');
  note.querySelector('.nb').innerHTML =
      '<b>Одна строка — одна версия расчёта</b> (лист в excel-файле БП из '
    + 'Битрикса). <b>✓</b> — версия верна: её и будут использовать алгоритмы. '
    + '⚠️ <b>Верная версия — одна НА ТРЕК:</b> одна «лук», одна «дсп» (версия '
    + 'лук+дсп занимает оба трека; версии без признаков — свой трек). Галочка '
    + 'в треке снимает прежнюю галочку этого же трека, а другой трек не '
    + 'трогает. <b>✗</b> — версия неверна, из расчётов исключается. Повторное '
    + 'нажатие снимает отметку. Пока в треке выбора нет, жёлтым подсвечена его '
    + 'версия <b>с последней датой</b> — её алгоритмы используют по умолчанию. '
    + 'Выручка и прибыль — «Выручка без НДС» и «Чистая прибыль» из столбца '
    + '«Суммарно» этой версии; «мес.» — сколько месяцев вывоза заложено. '
    + 'Отметки применяются к расчётам при ближайшей пересборке (ночная 04:03).'
    + (PLAN_ONLINE ? '' : ' ⚠️ Страница открыта файлом — сохранение недоступно.');
  app.appendChild(note);

  /* локальная копия отметок: после клика строка перерисовывается сразу,
     не дожидаясь пересборки */
  const CH = {};
  ROWS.forEach(r=>{ if(r.choice) CH[r.name]={verdict:r.choice, by:r.by, at:r.at}; });

  const byBp = new Map();
  ROWS.forEach(r=>{ const k=r.bp||'—';
    (byBp.get(k)||byBp.set(k,[]).get(k)).push(r); });
  const bps = [...byBp.keys()].sort((a,b)=>(+b||0)-(+a||0));

  const bar = el('div','toolbar');
  bar.innerHTML =
      '<input type="search" id="bvq" placeholder="Поиск: номер БП / файл / лист…">'
    + '<label class="chip" style="cursor:pointer" title="БП, у которых версий '
    + 'больше одной, — именно там выбор и нужен"><input type="checkbox" id="bvmulti"> только с несколькими версиями</label>'
    + '<span class="muted" style="margin-left:auto">'
    + esc((META.bpver_bps||0)+' БП · '+ROWS.length+' версий · выгрузка '
        + (META.bpver_generated||'—').replace('T',' '))+'</span>';
  app.appendChild(bar);

  const box = el('div','panel');
  app.appendChild(box);
  let LIMIT = 60;

  function verdictOf(r){ return (CH[r.name]||{}).verdict || ''; }
  /* треки версии: «лук», «дсп», версии без признаков — «other». Верная версия
     выбирается НА ТРЕК (правка заказчика 01.09.2026). */
  /* ⚠️ трек — по ИМЕНИ ЛИСТА: файловые признаки лук/дсп стоят на всей книге,
     и в книге с обоими листами треки сливались (файловый — запасной) */
  const trOf = r => { const sh=(r.sheet||r.name||'').toLowerCase(); const t=[];
    if(sh.includes('лук'))t.push('luk'); if(sh.includes('дсп'))t.push('dsp');
    if(!t.length){ if(r.lukoil==='да')t.push('luk'); if(r.dsp==='да')t.push('dsp'); }
    return t.length?t:['other']; };
  const TRN = {luk:'лук', dsp:'дсп', other:'без признака'};
  /* «по умолчанию» пересчитывается на лету И ПО ТРЕКАМ: галочка гасит
     умолчание своего трека, крестик передаёт умолчание следующей по дате */
  function dfltTracks(list){
    const out = {};                      /* имя версии -> ['luk','dsp'] */
    ['luk','dsp','other'].forEach(tr=>{
      const rows = list.filter(r=>trOf(r).includes(tr));
      if(!rows.length || rows.some(r=>verdictOf(r)==='ok'
                                      && trOf(r).includes(tr))) return;
      const cand = rows.filter(r=>verdictOf(r)!=='bad');
      if(!cand.length) return;
      const best = cand.reduce((a,b)=> (b.ver+b.name)>(a.ver+a.name)?b:a);
      (out[best.name]=out[best.name]||[]).push(tr);
    });
    return out;
  }

  async function save(r, verdict){
    const cur = verdictOf(r);
    const body = cur===verdict ? {name:r.name, reset:true}
                               : {name:r.name, bp:r.bp, verdict, tracks:trOf(r)};
    if(!PLAN_ONLINE){ alert('Страница открыта файлом — отметка не сохранится. '
      +'Откройте отчёт по адресу сервера.'); return; }
    try{
      const resp = await fetch('/api/bp-versions', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      if(!resp.ok) throw new Error('сервер ответил '+resp.status);
      if(body.reset) delete CH[r.name];
      else{
        if(verdict==='ok')  /* галочка одна НА ТРЕК — та же логика, что на сервере */
          (byBp.get(r.bp)||[]).forEach(x=>{
            if((CH[x.name]||{}).verdict==='ok'
               && trOf(x).some(t=>trOf(r).includes(t))) delete CH[x.name]; });
        CH[r.name]={verdict, by:'вы', at:'только что'};
      }
      draw();
    }catch(e){
      alert('Не удалось сохранить: '+(e.message||e));
    }
  }

  function draw(){
    const q = ($('#bvq').value||'').trim().toLowerCase();
    const onlyMulti = $('#bvmulti').checked;
    let list = bps.filter(bp=>{
      const rows = byBp.get(bp);
      if(onlyMulti && rows.length<2) return false;
      if(!q) return true;
      return bp.includes(q) || rows.some(r=>
        (r.file+' '+r.sheet+' '+r.name).toLowerCase().includes(q));
    });
    const shown = list.slice(0, LIMIT);
    let h = '<table class="bvt"><thead><tr>'
      + '<th>версия расчёта (лист)</th><th>версия от</th>'
      + '<th>лук</th><th>дсп</th>'
      + '<th class="num">мес.</th><th class="num">выручка, ₽</th>'
      + '<th class="num">прибыль, ₽</th><th>парсер</th>'
      + '<th>отметка</th><th></th></tr></thead><tbody>';
    shown.forEach(bp=>{
      const rows = byBp.get(bp).slice()
        .sort((a,b)=> (b.ver+b.name).localeCompare(a.ver+a.name));
      const dfl = dfltTracks(rows);
      h += '<tr class="bvh"><td colspan="10">БП '+esc(bp)
        + ' <span class="muted">· '+esc(rows[0].file)+'</span>'
        + (rows.length>1?' <span class="tag">'+rows.length+' версии</span>':'')
        + '</td></tr>';
      rows.forEach(r=>{
        const v = verdictOf(r);
        const dtr = dfl[r.name] || [];
        const cls = v==='ok'?' ok': v==='bad'?' bad': dtr.length?' dflt':'';
        h += '<tr class="bvr'+cls+'" data-n="'+esc(r.name)+'">'
          + '<td class="bvs" title="'+esc(r.name)+'">'+esc(r.sheet)+'</td>'
          + '<td>'+esc(r.ver||'—')+'</td>'
          + '<td>'+(trOf(r).includes('luk')?'да':'—')+'</td>'
          + '<td>'+(trOf(r).includes('dsp')?'да':'—')+'</td>'
          + '<td class="num">'+(r.months==null?'—':r.months)+'</td>'
          + '<td class="num">'+(r.revenue==null?'—':money(r.revenue))+'</td>'
          + '<td class="num">'+(r.profit==null?'—':money(r.profit))+'</td>'
          + '<td>'+esc(r.status||'—')+'</td>'
          + '<td class="bvm">'
            + (v==='ok'?'✓ верна ('+trOf(r).map(t=>TRN[t]).join('+')+')'
                 +(CH[r.name].by?' · '+esc(CH[r.name].by):'')
              : v==='bad'?'✗ неверна'
              : dtr.length?'по умолчанию ('+dtr.map(t=>TRN[t]).join('+')+')':'')+'</td>'
          + '<td class="bvb">'
            + '<button class="bok'+(v==='ok'?' on':'')+'" title="версия верна — использовать её">✓</button>'
            + '<button class="bbad'+(v==='bad'?' on':'')+'" title="версия неверна — исключить из расчётов">✗</button>'
          + '</td></tr>';
      });
    });
    h += '</tbody></table>';
    if(list.length>shown.length)
      h += '<button class="gmore" id="bvmore">показать ещё '
         + Math.min(200, list.length-shown.length)+' БП (из '
         + (list.length-shown.length)+')</button>';
    box.innerHTML = h;
    box.querySelectorAll('.bvr').forEach(tr=>{
      const r = ROWS.find(x=>x.name===tr.dataset.n);
      tr.querySelector('.bok').addEventListener('click', ()=>save(r,'ok'));
      tr.querySelector('.bbad').addEventListener('click', ()=>save(r,'bad'));
    });
    const more = box.querySelector('#bvmore');
    if(more) more.addEventListener('click', ()=>{ LIMIT+=200; draw(); });
    /* перерисовка стирает обёртку прокрутки — вернуть, кнопка уедет внутрь неё */
    wrapWideTables(box);
  }
  $('#bvq').addEventListener('input', ()=>{ LIMIT=60; draw(); });
  $('#bvmulti').addEventListener('change', ()=>{ LIMIT=60; draw(); });
  draw();
};

TABS.check = function(app){
  /* ================================================================
     ПРОВЕРКА ДАННЫХ — переписана 12.08.2026 по просьбе заказчика.
     Было: четыре карточки, два разреза «цех/база», расход по типам
     документа и огромная таблица по ключам «склад · код». Всё это
     отвечало на вопросы, которые давно закрыты, и главного —
     СХОДИТСЯ ЛИ НАШ ОСТАТОК С 1С И ГДЕ ИМЕННО НЕ СХОДИТСЯ — на
     вкладке не было вовсе.
     Стало: автопроверки + сверка по подразделениям, от крупного к
     мелкому. Прежняя таблица по ключам свёрнута, но не выброшена:
     в неё проваливаются, когда нашли проблемное подразделение.
     ================================================================ */
  const RECON = DATA.recon || [];
  const checks = DATA.checks||[];
  const covered = checks.filter(c=>c.covered);
  const bad = covered.filter(c=>Math.abs(c.diff)>0.001);

  const our = sum(RECON, r=>r.our), fact = sum(RECON, r=>r.fact);
  const dlt = our - fact;
  /* ⚠️ Расхождения складываются ПО МОДУЛЮ, а не алгебраически. Плюс на одном
     складе и минус на другом гасят друг друга в итоге, и «Δ по компании 1 134 т»
     выглядит как маленькая проблема, хотя разошлось вдвое больше. Масштаб беды
     показывает именно сумма модулей. */
  const absSum = sum(RECON, r=>Math.abs(r.our-r.fact));

  const c=el('div','cards');
  const pct = fact ? (100*dlt/fact) : 0;
  c.innerHTML=
     '<div class="card lead"><div class="k">Наш складской остаток</div><div class="v">'
      +fmt(our)+' <span class="unit">т</span></div>'
      +'<div class="s">приход − расход по движениям, как считает 1С</div></div>'
    +'<div class="card"><div class="k">Факт 1С</div><div class="v">'+fmt(fact)
      +' <span class="unit">т</span></div><div class="s">снимок остатков на '
      +esc(META.stock_date)+'</div></div>'
    +'<div class="card"><div class="k">Расхождение</div><div class="v '
      +(Math.abs(pct)<1?'ok':'warn')+'">'+fmt(dlt)+' <span class="unit">т</span></div>'
      +'<div class="s">'+(pct>=0?'+':'')+pct.toFixed(2)+' % от факта</div></div>'
    +'<div class="card"><div class="k">Разошлось по модулю</div><div class="v">'
      +fmt(absSum)+' <span class="unit">т</span></div>'
      +'<div class="s" title="Плюс на одном складе и минус на другом гасят друг '
      +'друга в итоге. Настоящий масштаб — сумма модулей.">плюсы и минусы не гасятся</div></div>';
  app.appendChild(c);

  /* ---- постоянные автопроверки: считаются при каждой сборке ---- */
  const AC = META.autochecks||[];
  if(AC.length){
    const nbad = AC.filter(a=>a.status!=='ok').length;
    const pa=el('div','panel');
    pa.innerHTML='<h3>Автопроверки — считаются при каждой сборке отчёта</h3>'
      +'<div class="hint">Ничего не «замазывается»: если проверка не проходит, она остаётся '
      +'с цифрой. <b>✓</b> — сходится, <b>!</b> — вопрос к данным 1С (не ошибка расчёта), '
      +'<b>✗</b> — ошибка расчёта, так быть не должно. '
      +'Сейчас без замечаний: '+(AC.length-nbad)+' из '+AC.length+'.</div>';
    const ta=el('table');
    ta.innerHTML='<thead><tr><th style="width:34px"></th><th>Проверка</th>'
      +'<th>Результат</th><th>Что это значит</th></tr></thead><tbody>'
      + AC.map(a=>{
          const m = a.status==='ok'?'<span class="ok">✓</span>'
                  : a.status==='bad'?'<span class="warn">✗</span>'
                  : '<span class="attn">!</span>';
          return '<tr><td>'+m+'</td><td><b>'+esc(a.name)+'</b></td>'
            +'<td class="'+(a.status==='bad'?'warn':'')+'">'+esc(a.value)+'</td>'
            +'<td class="muted">'+esc(a.note||'')+'</td></tr>';
        }).join('')+'</tbody>';
    pa.appendChild(ta); app.appendChild(pa);
  }

  /* ---- как читать сверку ---- */
  const nh=howto('Как читать сверку: что с чем сравнивается','');
  nh.querySelector('.nb').innerHTML =
     '<b>Сравниваются сопоставимые величины.</b> «Наш» остаток здесь — <i>складской</i>: '
    +'приход минус расход по всем строкам регистра, включая односторонние переработки, '
    +'то есть ровно та арифметика, что у 1С. Колонка ОСТАТОК в дереве — <i>проектная</i>, '
    +'из неё односторонние выброшены намеренно, и с 1С она несопоставима: в снимке '
    +'треть тоннажа лежит вообще без серии, 1С не знает, чей это металл.<br>'
    +'<b>Знак Δ — это разные беды.</b> <span class="warn">Δ больше нуля</span> — у нас '
    +'на складе лежит металл, которого в 1С уже нет: не увидели расход. '
    +'<span class="attn">Δ меньше нуля</span> — в 1С металл есть, а у нас нет: не увидели '
    +'приход. Первое завышает остаток к вывозу, второе уводит остаток в минус.<br>'
    +'<b>Сверяется только то, что есть в снимке:</b> '+cnt(RECON.length)+' складов. '
    +'Склады без якоря расхождением не считаются.';
  app.appendChild(nh);

  /* ---- КЛЮЧИ: где именно расходится ---- */
  const B = RECON.reduce((a,r)=>({n:a.n+r.n, same:a.same+r.same, only_our:a.only_our+r.only_our,
      only_1c:a.only_1c+r.only_1c, differ:a.differ+r.differ, neg:a.neg+r.neg}),
      {n:0,same:0,only_our:0,only_1c:0,differ:0,neg:0});
  const pb=el('div','panel');
  const bucket=(t,v,cls,hint)=>'<tr><td>'+esc(t)+'</td><td class="num '+cls+'"><b>'+cnt(v)+'</b></td>'
      +'<td class="num">'+(B.n?(100*v/B.n).toFixed(1):0)+' %</td><td class="muted">'+esc(hint)+'</td></tr>';
  pb.innerHTML='<h3>Где расходится — по ключам «склад · номенклатура»</h3>'
    +'<div class="hint">Сверяемых ключей '+cnt(B.n)+'. Разбивка показывает не «сколько тонн», '
    +'а <b>какого рода</b> расхождение: у каждого рода своя причина и свой способ разбора.</div>';
  const tb0=el('table');
  tb0.innerHTML='<thead><tr><th>Что с ключом</th><th class="num">Ключей</th><th class="num">Доля</th>'
    +'<th>Что это значит</th></tr></thead><tbody>'
    + bucket('сошлось точно', B.same, 'ok', 'наш остаток равен факту 1С до килограмма')
    + bucket('величина расходится', B.differ, 'warn',
             'обе стороны есть, но цифры разные — разбирать по документам')
    + bucket('есть у нас, у 1С нуль', B.only_our, 'warn',
             'мы не увидели расход: металл списан или продан мимо нашей модели')
    + bucket('есть у 1С, у нас нуль', B.only_1c, 'attn',
             'мы не увидели приход: поступление прошло документом, который не разобран')
    + bucket('наш остаток отрицательный', B.neg, 'warn',
             'расход больше прихода — в регистре не хватает поступления')
    +'</tbody>';
  pb.appendChild(tb0); app.appendChild(pb);

  /* ---- ГЛАВНОЕ: по подразделениям ---- */
  const byP = new Map();
  RECON.forEach(r=>{
    const k=r.podr||'—';
    let o=byP.get(k);
    if(!o){ o={podr:k, site:r.site, our:0, fact:0, n:0, same:0, neg:0, w:[]}; byP.set(k,o); }
    o.our+=r.our; o.fact+=r.fact; o.n+=r.n; o.same+=r.same; o.neg+=r.neg; o.w.push(r);
    if(o.site!==r.site) o.site='—';
  });
  const rowsP=[...byP.values()].map(o=>({...o, d:o.our-o.fact}))
      .sort((a,b)=>Math.abs(b.d)-Math.abs(a.d));
  const maxAbs = rowsP.length ? Math.abs(rowsP[0].d) : 1;
  const totAbs = sum(rowsP, r=>Math.abs(r.d)) || 1;

  const pp=el('div','panel');
  pp.innerHTML='<h3>Сверка по подразделениям — от самого проблемного</h3>'
    +'<div class="hint">Отсортировано по величине расхождения. Колонка <b>вклад</b> — какую '
    +'долю всего расхождения даёт подразделение, <b>нарастающим</b> — сколько дают все строки '
    +'сверху вместе: обычно три-четыре подразделения дают львиную долю, и разбирать надо их, '
    +'а не список целиком. Щелчок по строке разворачивает склады.</div>';
  const tp=el('table'); tp.className='recon';
  let acc=0;
  tp.innerHTML='<thead><tr><th>Подразделение</th><th>Тип</th><th class="num">Наш, т</th>'
    +'<th class="num">1С, т</th><th class="num">Δ, т</th><th class="num">Δ, %</th>'
    +'<th class="num">Ключей сошлось</th><th class="num">Вклад</th><th class="num">Нарастающим</th>'
    +'<th style="width:120px"></th></tr></thead><tbody>'
    + rowsP.map((r,i)=>{
        const share=100*Math.abs(r.d)/totAbs; acc+=share;
        const dp = r.fact ? (100*r.d/r.fact) : (r.d?100:0);
        const cls = Math.abs(r.d)<0.001 ? 'ok' : (r.d>0?'warn':'attn');
        const w = Math.max(2, Math.round(100*Math.abs(r.d)/maxAbs));
        return '<tr class="rp" data-i="'+i+'">'
          +'<td><span class="rtog">▸</span> <b>'+esc(r.podr)+'</b></td>'
          +'<td class="muted">'+esc(r.site||'—')+'</td>'
          +'<td class="num">'+fmt(r.our)+'</td><td class="num">'+fmt(r.fact)+'</td>'
          +'<td class="num '+cls+'"><b>'+(r.d>0?'+':'')+fmt(r.d)+'</b></td>'
          +'<td class="num '+cls+'">'+(r.d>0?'+':'')+dp.toFixed(1)+' %</td>'
          +'<td class="num'+(r.same<r.n?' muted':'')+'">'+cnt(r.same)+' / '+cnt(r.n)+'</td>'
          +'<td class="num">'+share.toFixed(1)+' %</td>'
          +'<td class="num muted">'+acc.toFixed(0)+' %</td>'
          +'<td><i class="rbar '+(r.d>0?'p':'m')+'" style="width:'+w+'%"></i></td></tr>'
          +'<tr class="rsub" data-i="'+i+'" hidden><td colspan="10"></td></tr>';
      }).join('')
    + '<tr class="rtot"><td><b>ИТОГО</b></td><td></td><td class="num"><b>'+fmt(our)+'</b></td>'
      +'<td class="num"><b>'+fmt(fact)+'</b></td>'
      +'<td class="num '+(Math.abs(pct)<1?'ok':'warn')+'"><b>'+(dlt>0?'+':'')+fmt(dlt)+'</b></td>'
      +'<td class="num">'+(dlt>0?'+':'')+pct.toFixed(2)+' %</td>'
      +'<td class="num">'+cnt(B.same)+' / '+cnt(B.n)+'</td><td colspan="3"></td></tr>'
    +'</tbody>';
  pp.appendChild(tp); app.appendChild(pp);

  /* разворот подразделения — склады внутри него */
  tp.addEventListener('click', ev=>{
    const tr=ev.target.closest('tr.rp'); if(!tr) return;
    const i=+tr.dataset.i, sub=tp.querySelector('tr.rsub[data-i="'+i+'"]');
    if(!sub) return;
    const open = sub.hasAttribute('hidden');
    sub.toggleAttribute('hidden', !open);
    tr.querySelector('.rtog').textContent = open ? '▾' : '▸';
    if(open && !sub.dataset.done){
      const ws=rowsP[i].w.map(w=>({...w, d:w.our-w.fact}))
          .sort((a,b)=>Math.abs(b.d)-Math.abs(a.d));
      sub.firstElementChild.innerHTML='<table class="inner"><thead><tr><th>Склад</th>'
        +'<th class="num">Наш, т</th><th class="num">1С, т</th><th class="num">Δ, т</th>'
        +'<th class="num">сошлось</th><th class="num">у нас есть,<br>у 1С нуль</th>'
        +'<th class="num">у 1С есть,<br>у нас нуль</th><th class="num">цифры<br>разные</th>'
        +'<th class="num">наш<br>минус</th></tr></thead><tbody>'
        + ws.map(w=>'<tr><td>'+esc(w.sklad)+'</td>'
            +'<td class="num">'+fmt(w.our)+'</td><td class="num">'+fmt(w.fact)+'</td>'
            +'<td class="num '+(Math.abs(w.d)<0.001?'ok':(w.d>0?'warn':'attn'))+'">'
              +(w.d>0?'+':'')+fmt(w.d)+'</td>'
            +'<td class="num">'+cnt(w.same)+'</td>'
            +'<td class="num'+(w.only_our?' warn':' muted')+'">'+cnt(w.only_our)+'</td>'
            +'<td class="num'+(w.only_1c?' attn':' muted')+'">'+cnt(w.only_1c)+'</td>'
            +'<td class="num'+(w.differ?' warn':' muted')+'">'+cnt(w.differ)+'</td>'
            +'<td class="num'+(w.neg?' warn':' muted')+'">'+cnt(w.neg)+'</td></tr>').join('')
        +'</tbody></table>';
      sub.dataset.done='1';
    }
  });

  /* ---- прежняя построчная сверка: свёрнута ---- */
  const det=el('details','panel');
  det.innerHTML='<summary><b>Построчная сверка по ключам «склад · код»</b> — '
    +cnt(covered.length)+' ключей, из них расходится '+cnt(bad.length)
    +'</summary><div class="hint">Нужна, когда по таблице выше нашли проблемное '
    +'подразделение и надо понять, на какой именно номенклатуре расходится. '
    +'Колонки идут в порядке обратного хода: якорь → отматываем движения → результат.</div>';
  const bar=el('div','toolbar');
  bar.innerHTML='<input type="search" id="cq" placeholder="Поиск: склад / код / номенклатура…">'
    +'<select id="cf"><option value="bad">показать: только расхождения</option>'
    +'<option value="neg">показать: отрицательный нач. остаток</option>'
    +'<option value="all">показать: все с якорем</option>'
    +'<option value="nocov">показать: нет якоря</option></select>'
    +'<span class="right muted" id="ccnt"></span>';
  det.appendChild(bar);
  const wrapT=el('div','scroll'); det.appendChild(wrapT); app.appendChild(det);
  function drawT(){
    const q=$('#cq').value.trim().toLowerCase(), mode=$('#cf').value;
    let d = mode==='bad'?bad
          : mode==='neg'?covered.filter(c=>c.opening<-0.001)
          : mode==='all'?covered : checks.filter(c=>!c.covered);
    if(q) d=d.filter(c=>(c.sklad+' '+c.code+' '+c.name).toLowerCase().includes(q));
    $('#ccnt').textContent=cnt(d.length)+' строк';
    d=d.slice(0,3000);
    wrapT.innerHTML='<table><thead><tr><th>Подразделение</th><th>Склад</th><th>Код</th>'
      +'<th>Номенклатура</th><th class="num">Наш остаток</th><th class="num">Факт 1С</th>'
      +'<th class="num">Δ</th></tr></thead><tbody>'
      + d.map(c=>'<tr><td class="muted">'+esc((DATA.sk_unit||{})[c.sklad]||'—')+'</td>'
          +'<td>'+esc(c.sklad)+'</td><td>'+esc(c.code)+'</td>'
          +'<td class="muted">'+esc(c.name)+'</td>'
          +'<td class="num">'+fmt(c.calc)+'</td><td class="num">'+fmt(c.fact)+'</td>'
          +'<td class="num'+(Math.abs(c.diff)>0.001?' warn':'')+'">'+fmt(c.diff)+'</td></tr>').join('')
      +'</tbody></table>';
  }
  $('#cq').addEventListener('input',drawT); $('#cf').addEventListener('change',drawT);
  drawT();
};

/* ================================================================
   6) ВЫГРУЗКА В 1С  —  xlsx через sharedStrings (1С НЕ читает inlineStr)
   ================================================================ */
/* --- CRC32 + минимальный ZIP (store, без сжатия) --- */
const CRC_T=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;
  for(let k=0;k<8;k++)c=c&1?0xEDB88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
function crc32(u8){let c=0xFFFFFFFF;for(let i=0;i<u8.length;i++)c=CRC_T[(c^u8[i])&0xFF]^(c>>>8);return (c^0xFFFFFFFF)>>>0;}
const ENC=new TextEncoder();
function zip(files){
  const chunks=[],central=[];let off=0;
  const dos=(()=>{const d=new Date();return {t:((d.getHours()<<11)|(d.getMinutes()<<5)|(d.getSeconds()/2))&0xFFFF,
    d:(((d.getFullYear()-1980)<<9)|((d.getMonth()+1)<<5)|d.getDate())&0xFFFF};})();
  for(const f of files){
    const name=ENC.encode(f.name), data=typeof f.data==='string'?ENC.encode(f.data):f.data;
    const crc=crc32(data);
    const lh=new DataView(new ArrayBuffer(30));
    lh.setUint32(0,0x04034b50,true); lh.setUint16(4,20,true); lh.setUint16(6,0x0800,true);
    lh.setUint16(8,0,true); lh.setUint16(10,dos.t,true); lh.setUint16(12,dos.d,true);
    lh.setUint32(14,crc,true); lh.setUint32(18,data.length,true); lh.setUint32(22,data.length,true);
    lh.setUint16(26,name.length,true); lh.setUint16(28,0,true);
    chunks.push(new Uint8Array(lh.buffer),name,data);
    const ch=new DataView(new ArrayBuffer(46));
    ch.setUint32(0,0x02014b50,true); ch.setUint16(4,20,true); ch.setUint16(6,20,true);
    ch.setUint16(8,0x0800,true); ch.setUint16(10,0,true); ch.setUint16(12,dos.t,true); ch.setUint16(14,dos.d,true);
    ch.setUint32(16,crc,true); ch.setUint32(20,data.length,true); ch.setUint32(24,data.length,true);
    ch.setUint16(28,name.length,true); ch.setUint32(42,off,true);
    central.push(new Uint8Array(ch.buffer),name);
    off+=30+name.length+data.length;
  }
  let csize=0; for(const c of central) csize+=c.length;
  const eo=new DataView(new ArrayBuffer(22));
  eo.setUint32(0,0x06054b50,true); eo.setUint16(8,files.length,true); eo.setUint16(10,files.length,true);
  eo.setUint32(12,csize,true); eo.setUint32(16,off,true);
  const all=[...chunks,...central,new Uint8Array(eo.buffer)];
  let tot=0; for(const c of all) tot+=c.length;
  const out=new Uint8Array(tot); let p=0; for(const c of all){out.set(c,p);p+=c.length;}
  return out;
}
const xesc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]))
  .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g,'');
function colName(n){let s='';while(n>0){const m=(n-1)%26;s=String.fromCharCode(65+m)+s;n=(n-m-1)/26;}return s;}

/**
 * Строит .xlsx, который читает 1С:
 *  - строки ТОЛЬКО через sharedStrings (t="s"), никакого inlineStr;
 *  - есть styles.xml (1С падает без него);
 *  - имя листа как в шаблоне загрузки 1С.
 */
function buildXlsx(sheetName, header, rows){
  const shared=[], sidx=new Map();
  const si=s=>{const k=String(s==null?'':s); let i=sidx.get(k);
    if(i===undefined){i=shared.length;sidx.set(k,i);shared.push(k);} return i;};
  const lines=[];
  const cell=(r,c,v)=> (typeof v==='number' && isFinite(v))
      ? '<c r="'+colName(c)+r+'"><v>'+v+'</v></c>'
      : '<c r="'+colName(c)+r+'" t="s"><v>'+si(v)+'</v></c>';
  lines.push('<row r="1">'+header.map((h,i)=>'<c r="'+colName(i+1)+'1" t="s" s="1"><v>'+si(h)+'</v></c>').join('')+'</row>');
  rows.forEach((row,ri)=>{
    const r=ri+2;
    lines.push('<row r="'+r+'">'+row.map((v,ci)=>cell(r,ci+1,v)).join('')+'</row>');
  });
  const sheet='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    +'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    +'<sheetData>'+lines.join('')+'</sheetData></worksheet>';
  const sst='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    +'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="'+shared.length
    +'" uniqueCount="'+shared.length+'">'
    +shared.map(s=>'<si><t xml:space="preserve">'+xesc(s)+'</t></si>').join('')+'</sst>';
  const styles='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    +'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    +'<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    +'<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    +'<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    +'<borders count="1"><border/></borders>'
    +'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    +'<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    +'<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    +'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    +'</styleSheet>';
  const files=[
    {name:'[Content_Types].xml', data:'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      +'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      +'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      +'<Default Extension="xml" ContentType="application/xml"/>'
      +'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
      +'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
      +'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
      +'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
      +'</Types>'},
    {name:'_rels/.rels', data:'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      +'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      +'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
      +'</Relationships>'},
    {name:'xl/workbook.xml', data:'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      +'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      +'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
      +'<sheets><sheet name="'+xesc(sheetName)+'" sheetId="1" r:id="rId1"/></sheets></workbook>'},
    {name:'xl/_rels/workbook.xml.rels', data:'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      +'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      +'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
      +'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
      +'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
      +'</Relationships>'},
    {name:'xl/worksheets/sheet1.xml', data:sheet},
    {name:'xl/sharedStrings.xml', data:sst},
    {name:'xl/styles.xml', data:styles},
  ];
  return zip(files);
}
function download(bytes,name){
  const b=new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download=name;
  document.body.appendChild(a); a.click(); setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1500);
}

TABS.export1c = function(app){
  const bases=[...new Set(ITEMS.map(r=>r.base))].sort(baseSort);
  const bps=[...new Set(ITEMS.map(r=>r.contract).filter(Boolean))].sort();
  const months=[...new Set(ITEMS.flatMap(r=>r.docs.map(d=>d.dt.slice(0,7))))].sort();

  const p=el('div','panel');
  p.innerHTML='<h3>Выгрузка в 1С</h3>'
    +'<div class="hint">Файл формируется в браузере в формате, который читает 1С: строки через '
    +'<b>sharedStrings</b> (<code>t="s"</code>), а не <code>inlineStr</code>; есть <code>styles.xml</code> '
    +'и <code>cellStyles</code>; имя листа — <code>Лист_1</code>, как в шаблоне загрузки. '
    +'В колонке «Серия» — <b>полная строка серии</b>. Числа точные, без округления. Сортировка А→Я.<br>'
    +'<b>Формат «шаблон 1С»</b> повторяет колонки файла «Загрузка Перемещение товаров.xls»: '
    +'Номенклатура · Количество · Количество(в единицах хранения) · Статус указания серий · '
    +'… отправитель · … получатель · Серия. Статус серий = <code>14</code> (как в шаблоне заказчика). '
    +'<b>Формат «расширенный»</b> добавляет склад, код и подразделение — для сверки, а не для загрузки.</div>';
  const bar=el('div','toolbar');
  bar.innerHTML='<select id="xfmt"><option value="tpl">формат: шаблон 1С (Перемещение товаров)</option>'
      +'<option value="ext">формат: расширенный (склад · код · подразделение)</option></select>'
    +'<select id="xb"><option value="">база: все</option>'+bases.map(b=>'<option>'+esc(b)+'</option>').join('')+'</select>'
    +'<select id="xs"><option value="">склад: все</option></select>'
    +'<select id="xbp"><option value="">бизнес-план: все</option>'+bps.map(b=>'<option>'+esc(b)+'</option>').join('')+'</select>'
    +'<select id="xm1"><option value="">период с: начало</option>'+months.map(m=>'<option>'+m+'</option>').join('')+'</select>'
    +'<select id="xm2"><option value="">по: конец</option>'+months.map(m=>'<option>'+m+'</option>').join('')+'</select>'
    +'<select id="xmode"><option value="all">режим: все</option><option value="with">только с серией</option>'
      +'<option value="without">без серии (колонка пустая)</option></select>'
    +'<input type="search" id="xq" placeholder="поиск позиции…">';
  p.appendChild(bar);
  const bar2=el('div','toolbar');
  bar2.innerHTML='<button class="btn3" id="xall">Выбрать все</button>'
    +'<button class="btn3" id="xnone">Снять все</button>'
    +'<span class="muted" id="xcnt"></span>'
    +'<button class="btn2 right" id="xgo">⬇ Скачать .xlsx для 1С</button>';
  p.appendChild(bar2);
  const list=el('div','scroll'); p.appendChild(list); app.appendChild(p);

  const sel=new Set();
  function skladOptions(){
    const b=$('#xb').value;
    const sk=[...new Set(ITEMS.filter(r=>!b||r.base===b).map(r=>r.sklad))].sort();
    $('#xs').innerHTML='<option value="">склад: все</option>'+sk.map(s=>'<option>'+esc(s)+'</option>').join('');
  }
  function current(){
    const b=$('#xb').value, s=$('#xs').value, bp=$('#xbp').value;
    const m1=$('#xm1').value, m2=$('#xm2').value, mode=$('#xmode').value;
    const q=$('#xq').value.trim().toLowerCase();
    let d=ITEMS;
    if(b) d=d.filter(r=>r.base===b);
    if(s) d=d.filter(r=>r.sklad===s);
    if(bp) d=d.filter(r=>r.contract===bp);
    if(mode==='with') d=d.filter(REAL);
    if(mode==='without') d=d.filter(r=>!REAL(r));
    if(q) d=d.filter(r=>(r.name+' '+r.code+' '+r.series_full).toLowerCase().includes(q));
    /* период: пересчитываем тоннаж по документам внутри окна */
    d=d.map(r=>{
      if(!m1&&!m2) return r;
      const docs=r.docs.filter(x=>{const m=x.dt.slice(0,7); return (!m1||m>=m1)&&(!m2||m<=m2);});
      if(!docs.length) return null;
      return Object.assign({},r,{docs,tonnes:docs.reduce((a,x)=>a+x.tonnes,0),
                                 qty:docs.reduce((a,x)=>a+x.qty,0)});
    }).filter(Boolean);
    d.sort((a,b2)=> a.name.localeCompare(b2.name,'ru') || a.series_full.localeCompare(b2.series_full,'ru'));
    return d;
  }
  function draw(){
    const d=current();
    list.innerHTML='';
    d.slice(0,2500).forEach((r,i)=>{
      const id='x'+i, key=r.base+'|'+r.sklad+'|'+r.code+'|'+r.series;
      const row=el('div','chk');
      row.innerHTML='<input type="checkbox" id="'+id+'"'+(sel.has(key)?' checked':'')+'>'
        +'<span class="code">'+esc(r.code)+'</span>'
        +'<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
          +esc(r.name)+' <span class="tag">· '+esc(r.series_full)+'</span></span>'
        +badge(r.badge)
        +'<span style="min-width:96px;text-align:right;font-variant-numeric:tabular-nums">'+fmt3(r.tonnes)+' т</span>';
      row.querySelector('input').addEventListener('change',e=>{
        e.target.checked?sel.add(key):sel.delete(key); upd(); });
      list.appendChild(row);
    });
    if(d.length>2500) list.appendChild(el('div','hint','Показаны первые 2500 из '+cnt(d.length)+'. Уточните фильтры — «Выбрать все» действует на весь отфильтрованный список.'));
    upd(d);
  }
  function upd(d){
    d=d||current();
    $('#xcnt').textContent='отфильтровано '+cnt(d.length)+' позиций · отмечено '+cnt(sel.size);
  }
  ['#xb','#xs','#xbp','#xm1','#xm2','#xmode'].forEach(s=>$(s).addEventListener('change',()=>{
    if(s==='#xb') skladOptions(); draw(); }));
  $('#xq').addEventListener('input',draw);
  $('#xall').addEventListener('click',()=>{ current().forEach(r=>sel.add(r.base+'|'+r.sklad+'|'+r.code+'|'+r.series)); draw(); });
  $('#xnone').addEventListener('click',()=>{ sel.clear(); draw(); });
  $('#xgo').addEventListener('click',()=>{
    const mode=$('#xmode').value;
    let d=current();
    if(sel.size) d=d.filter(r=>sel.has(r.base+'|'+r.sklad+'|'+r.code+'|'+r.series));
    if(!d.length){ alert('Нечего выгружать: под фильтры не попала ни одна позиция.'); return; }
    const ser = r => mode==='without' ? '' : (REAL(r)? r.series_full : '');
    let header, rows, ni, si;
    if($('#xfmt').value==='tpl'){
      /* колонки как в шаблоне 1С «Загрузка Перемещение товаров.xls» (лист Лист_1).
         Статус указания серий = 14 — значение из шаблона заказчика. */
      header=['Номенклатура','Количество','Количество(в единицах хранения)',
              'Статус указания серий','Статус указания серий отправитель',
              'Статус указания серий получатель','Серия'];
      rows=d.map(r=>[ r.name, r.tonnes, r.tonnes, 14, '', 14, ser(r) ]);
      ni=0; si=6;
    }else{
      header=['Склад','Ед. изм.','Номенклатура.Код','Подразделение','Номенклатура','Серия','Количество'];
      rows=d.map(r=>[ r.sklad, r.unit, r.code, r.division||r.base, r.name, ser(r), r.tonnes ]);
      ni=4; si=5;
    }
    rows.sort((a,b2)=> String(a[ni]).localeCompare(String(b2[ni]),'ru')
                    || String(a[si]).localeCompare(String(b2[si]),'ru'));
    download(buildXlsx('Лист_1',header,rows),
      'Реализация_1С_'+(new Date().toISOString().slice(0,10))+'.xlsx');
  });
  skladOptions(); draw();
};

/* ================================================================
   7) ОБНОВЛЕНИЕ ДАННЫХ
   ================================================================ */
TABS.update = function(app){
  const p=el('div','panel');
  p.innerHTML='<h3>Обновление данных</h3>'
    +'<div class="hint">SQL-подключения нет — сервис живёт на выгрузках 1С. Положите свежие файлы в '
    +'<code>data/</code> (имена распознаются автоматически) и пересоберите отчёт.</div>'
    +'<pre class="code-block">'
    +'python3 sales_report.py   # data/ -> out/sales_data.json + сверка в консоль\n'
    +'python3 make_dash.py      # -> out/dashboard.html  (этот файл)\n'
    +'python3 make_excel.py     # -> out/Реализация_metoptorg_&lt;дата&gt;.xlsx</pre>';
  const t=el('table');
  t.innerHTML='<thead><tr><th>Роль файла</th><th>Шаблон имени</th><th>Использован в этой сборке</th></tr></thead><tbody>'
    +[['Движения (обороты регистра)','СебестоимостьТоваровОбороты_*.csv','movements'],
      ['Остатки на СЕГОДНЯ (якорь обратного хода)','Остатки на складах *.xlsx','stock_end'],
      ['Справочник номенклатуры','Номенклатура_*.csv','nomen'],
      ['Справочник серий','СерииНоменклатуры_*.csv','series'],
      ['Справочник складов','Склады_*.csv','sklady'],
      ['Производственные подразделения (цех/база — авторитетно)','*Производственные_подразделения*.csv','divisions'],
      ['Ручные привязки серий','overrides.json','overrides']]
      .map(([r,pat,k])=>{const v=(META.files||{})[k];
        return '<tr><td>'+esc(r)+'</td><td><code>'+esc(pat)+'</code></td><td>'
          +(v?'<span class="ok">'+esc(v)+'</span>':'<span class="warn">не найден</span>')+'</td></tr>';}).join('')
    +'</tbody>';
  p.appendChild(t); app.appendChild(p);

  const p2=el('div','panel');
  p2.innerHTML='<h3>Снимок текущего расчёта</h3>'
    +'<div class="hint">JSON со всеми данными, правками и справочными полями — для архива или переноса.</div>';
  const b=el('button','btn2','⬇ Скачать снимок JSON');
  b.addEventListener('click',()=>{
    const blob=new Blob([JSON.stringify(DATA)],{type:'application/json'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download='sales_snapshot_'+(new Date().toISOString().slice(0,10))+'.json';
    document.body.appendChild(a); a.click(); setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1500);
  });
  p2.appendChild(b); app.appendChild(p2);

  const p3=el('div','panel');
  p3.innerHTML='<h3>Ручные привязки серий</h3>'
    +'<div class="hint">Файл <code>data/overrides.json</code> — применяется ПЕРВЫМ в каскаде, бейдж «ручная», '
    +'лог попадает на вкладку «Проверка данных».</div>'
    +'<pre class="code-block">'
    +'[{"regnum":"РУМЛ-001175","code":"00-00017878","series":"1651",\n'
    +'  "author":"экономист","note":"подтверждено актом"}]</pre>';
  app.appendChild(p3);

  const p4=el('div','panel');
  p4.innerHTML='<h3>Как считается</h3>'
    +'<div class="hint">Каскад восстановления серии для каждой строки реализации (порядок важен):</div>'
    +'<ol style="font-size:13px;line-height:1.7">'
    +'<li>ручная привязка <code>overrides.json</code> → бейдж <b>ручная</b></li>'
    +'<li>инлайн-серия самой строки реализации → <b>1С</b></li>'
    +'<li>смешанный лот (в приходе несколько серий): сначала <b>точное количество</b> '
      +'(0.008 ↔ 0.008), затем аллокация по остатку ёмкости компонента (FIFO по дате) → '
      +'<b>док</b>; если строка не влезает ни в один компонент — делится и помечается '
      +'<b>доля</b> (видно в «Проблемах»)</li>'
    +'<li>перемещение, приходная сторона: серия берётся с расходной стороны '
      +'<b>того же документа</b> → <b>док</b></li>'
    +'<li>карта <code>(тип документа + номер партии + код) → серия</code> → <b>док</b>. '
      +'Ключ включает ТИП документа: «Приобретение РУМЛ-000146» и «Производство без '
      +'заказа РУМЛ-000146» — разные документы с одним номером</li>'
    +'<li>переработка → <b>переработка</b>: для пары «в производство ↔ из производства» выпуск '
      +'распределяется между БП по полному тоннажному составу сырья. Для последующей '
      +'продажи конкретной позиции серия наследуется только при доле ≥ '
      +(((META.thresholds||{}).INHERIT_MIN_SHARE||0.5)*100)
      +' %; инлайн-серия 1С всегда приоритетнее</li>'
    +'<li>единственная серия, приходившая на этот склад под этим кодом → <b>вероятно</b></li>'
    +'<li>договор и номер БП из имени склада-отправителя → <b>площадка</b></li>'
    +'<li>пометки ВВОД ОСТАТКОВ / ИЗЛИШКИ → <b>ввод</b></li>'
    +'<li>иначе → <b>БЕЗ СЕРИИ</b> (бейдж «нет»)</li></ol>'
    +'<div class="hint">Пустое описание партии лотом <b>не считается</b>: ключ '
      +'(«пусто» + код) склеивал бы все безпартийные приходы этого кода по всей базе. '
      +'Таких строк реализации '+fmt(META.sale_rows_no_lot||0)+' — они остаются '
      +'БЕЗ СЕРИИ, и это показано открыто, а не замазано «привязкой по документу».</div>'
    +'<div class="legend">'+Object.keys(BADGE_RANK).map(b=>badge(b)+' <span class="muted">'
      +esc(BADGE_TITLE[b]||'')+'</span>').join(' · ')+'</div>';
  app.appendChild(p4);

  const p5=el('div','panel');
  const TH=META.thresholds||{};
  p5.innerHTML='<h3>Пороги расчёта</h3>'
    +'<div class="hint">Живут в <code>src/config.py</code>, меняются без правки логики.</div>'
    +'<table><thead><tr><th>Порог</th><th class="num">Значение</th><th>Смысл</th></tr></thead><tbody>'
    +'<tr><td><code>INHERIT_MIN_SHARE</code></td><td class="num">'+esc(TH.INHERIT_MIN_SHARE)+'</td>'
      +'<td class="muted">минимальная доля для отнесения последующей продажи к БП; '
      +'на баланс самой пары резки этот порог не влияет</td></tr>'
    +'<tr><td><code>MIX_MIN_SHARE</code></td><td class="num">'+esc(TH.MIX_MIN_SHARE)+'</td>'
      +'<td class="muted">компонент смешанного лота меньше этой доли сворачивается '
      +'в доминирующую серию</td></tr>'
    +'<tr><td><code>MIX_MAX_COMPONENTS</code></td><td class="num">'+esc(TH.MIX_MAX_COMPONENTS)+'</td>'
      +'<td class="muted">не более этого числа компонент на лот — иначе лоты на 23–28 '
      +'серий дробят каждую продажу на десятки строк</td></tr>'
    +'</tbody></table>'
    +'<div class="hint">Смешанных лотов в этой сборке: <b>'+fmt(META.mix_lots||0)+'</b>, '
    +'строк расхода из них <b>'+fmt(META.mix_alloc_rows||0)+'</b>, из них пришлось делить '
    +'<b>'+fmt(META.mix_split_rows||0)+'</b>. Построчных движений: <b>'
    +fmt(META.move_rows||0)+'</b>.</div>';
  app.appendChild(p5);
};

/* ================================================================
   ОСТАТКИ ПО ПОДРАЗДЕЛЕНИЯМ (заказано 14.09.2026)
   Заказчик: «нужна вкладка с остатками по двум вариантам, как на других
   вкладках, чтобы можно было их сверить с 1С. Выглядит так: подразделение →
   группа аналитического учёта → НМК, и все остатки. Они должны соответствовать
   тому, что на Ганте и других вкладках по движениям; при клике на число там
   открывается эта вкладка с выбранным БП. Столбцы — в соответствии с
   движениями».
   ⚠️ СЧИТАЕТСЯ ИЗ ТЕХ ЖЕ СТРОК ДВИЖЕНИЙ (MOVES) ТЕМИ ЖЕ ПРАВИЛАМИ, ЧТО ДЕРЕВО:
   aggRow (возврат — отрицательный приход, сортировка — своя пара, приход через
   разборку — один раз) и balanceOf. Вариант ограничивает только СКЛАДЫ
   (заготовка / все), как на Ганте; потоки показываются все, какие есть в
   выборке, — остаток же считается по полному обороту, и прятать поток значило
   бы показывать остаток, который не складывается из колонок.
   Итог вкладки по бизнес-плану обязан совпасть с остатком строки на Ганте в
   том же варианте и той же единице: это и есть проверка, и она печатается
   рядом с итогом («на Ганте: …»).
   Подразделение — КАК В 1С (DATA.sk_podr, колонка регистра), группа — из
   справочника номенклатуры (DATA.nm_grp, «НоменклатурнаяГруппаГрузов»): сверка
   с отчётом 1С идёт по тем же именам, что печатает сама 1С.
   ================================================================ */
TABS.stock = function(app){
  const SK_SITE = DATA.sk_site || {}, SK_PODR = DATA.sk_podr || {}, NM_GRP = DATA.nm_grp || {};
  const SK_TREE = DATA.sk_tree || {};
  const PRE = window.__SPRESET; window.__SPRESET = null;
  let vkey = (PRE && PRE.var) || localStorage.getItem('metoptorg.gvar') || (GVARS[0]||{}).key || 'v1';
  if(!GVAR(vkey)) vkey = (GVARS[0]||{}).key;
  /* имена бизнес-планов — из Ганты (там же лежит остаток для сверки) */
  const G_BY = new Map((DATA.gantt||[]).map(g=>[g.series, g]));
  const nameOf = s => (G_BY.get(s)||{}).name || s;
  /* какие серии вообще есть в движениях — список для выбора */
  const SERIES = (()=>{ const st = new Set(); for(const r of MOVES) st.add(mst(r,'series'));
    return [...st].filter(Boolean).sort((a,b)=>nameOf(a).localeCompare(nameOf(b),'ru')); })();
  let sel = (PRE && PRE.s && SERIES.includes(PRE.s)) ? PRE.s : '';

  app.appendChild(el('h2','', 'Остатки по подразделениям'));
  /* ---- НА КАКОЕ ЧИСЛО ОСТАТКИ (заказчик 14.09.2026) ----
     Дата — колонка «ДатаВыгрузки» самой таблицы оборотов: момент, когда
     Extractor снял регистр из 1С. Если выгрузка сломалась, ночная сборка
     возьмёт старый файл, и здесь это видно сразу: дата не сдвинулась, плашка
     красная. Дата последнего движения рядом — она может отставать и при живой
     выгрузке (движений просто не было). */
  (()=>{
    const dd = META.dump_dt || '';
    const ageH = dd ? (Date.now() - new Date(dd.replace(' ','T')).getTime())/36e5 : null;
    const stale = ageH == null || ageH > 36;
    const bar = el('div','freshbar'+(stale?' stale':''));
    bar.innerHTML = (dd
        ? '<b>Остатки на '+esc(dateRu(dd.slice(0,10)))+' '+esc(dd.slice(11))+'</b> — момент выгрузки '
          + 'регистра «Товары на складах» из 1С'
          + (stale ? ' · <b>выгрузке '+Math.round(ageH/24)+' дн.</b> — данные старые, проверьте Extractor и ночную сборку' : '')
        : '<b>Дата выгрузки в файле не указана</b> — на какое число остатки, сказать нельзя')
      + ' · последнее движение '+esc(dateRu(META.period_max||''))
      + ' · отчёт собран '+esc(META.generated||'—');
    app.appendChild(bar);
  })();
  const note = howto('Как сверять с 1С','');
  note.querySelector('.nb').innerHTML =
      '<b>Те же остатки, что на Ганте и в дереве движений</b>, но в разрезе '
    + 'отчёта 1С: <b>подразделение → группа аналитического учёта → номенклатура</b>. '
    + 'Столбцы — потоки движений по выбранным строкам, последний — <b>ОСТАТОК</b> '
    + '(приход минус расход по всем потокам). Итог по бизнес-плану сверен с '
    + 'остатком его строки на Ганте в том же варианте — совпадение печатается '
    + 'рядом с итогом.'
    + '<br><b>Вариант</b> ограничивает склады: «заготовка» — только склады '
    + 'заготовки (остаток = не вывезено), «вся компания» — все склады '
    + '(остаток = не продано). Потоки в обоих вариантах показаны все, какие '
    + 'есть по этим складам.'
    + '<br>Подразделение — как в колонке регистра 1С «Складская территория → '
    + 'Подразделение» (у старых складов 1С пишет туда имя склада). Группа — '
    + '«Номенклатурная группа грузов» справочника номенклатуры. Без '
    + 'бизнес-плана показывается вся компания: под номенклатурой раскрывается '
    + 'список бизнес-планов.';
  app.appendChild(note);

  const bar = el('div','toolbar');
  bar.innerHTML =
      '<span class="gzoom gvars">'
      + GVARS.map(v=>'<button data-v="'+esc(v.key)+'"'+(v.key===vkey?' class="on"':'')
          +' title="'+esc('Вариант '+v.title+'. '+(v.hint||''))+'">'
          +esc(String(v.title).replace(/^\s*\d+\s*[—-]\s*/,''))+'</button>').join('')
      +'</span>'
    + '<input type="search" id="stq" placeholder="найти бизнес-план: номер / название…">'
    + '<select id="stbp" title="Бизнес-план (серия 1С). Пусто — вся компания."></select>'
    + measureSelect('stmeas')
    + '<input type="search" id="stf" placeholder="фильтр строк: подразделение / группа / НМК…">'
    ;
  app.appendChild(bar);
  const cards = el('div','cards'); app.appendChild(cards);
  const box = el('div','panel'); app.appendChild(box);

  function fillSelect(){
    const q = ($('#stq').value||'').trim().toLowerCase();
    const qm = qMatcher(q);
    const list = qm ? SERIES.filter(s=>qm((nameOf(s)+' '+s).toLowerCase())) : SERIES;
    const cur = sel;
    $('#stbp').innerHTML = '<option value="">— вся компания ('+cnt(SERIES.length)+' БП) —</option>'
      + list.slice(0, 1500).map(s=>'<option value="'+esc(s)+'"'+(s===cur?' selected':'')+'>'
          + esc(nameOf(s))+'</option>').join('');
    if(cur && !list.includes(cur)) $('#stbp').value = '';
    /* один точный кандидат по поиску — выбираем сразу, без второго клика */
    if(q && list.length===1 && sel!==list[0]){ sel = list[0]; $('#stbp').value = sel; draw(); }
  }

  /* ---- агрегат: ОБЩИЙ КЭШ flowAgg, а не свой проход по строкам ----
     ⚠️ Раньше вкладка на каждую перерисовку (вариант, выбор БП, каждая буква
     в фильтре) заново прогоняла все 320 тыс. строк движений через aggRow —
     полсекунды на быстрой машине и несколько секунд на рабочем ПК (заказчик
     14.09.2026: «почему вкладка так долго открывается»). Правила те же, что у
     дерева, поэтому берём готовый агрегат flowAgg (считается один раз на
     единицу и общий для всех вкладок), а бизнес-план и склады варианта
     отбираем уже по его ключам (серия · вариант · склад · код) — их около
     20 тысяч, а не 320. */
  const keyOk = (s, w) => {
    if(sel && s !== sel) return false;
    const V = GVAR(vkey);
    return !V.sites.length || V.sites.includes(SK_SITE[w]||'—');
  };
  /* порядок колонок — как в дереве (LC_COLS), редкие потоки — следом */
  const ORDER = LC_COLS.map(c=>c[0]).filter(k=>k!=='остаток');
  const colLabel = k => LC_LABEL[k] || MSHORT[k] || k.replace(/_/g,' ');
  const NZ = x => Math.abs(x||0) > 0.0005;
  const num = (x, cls) => '<td class="num'+(cls?' '+cls:'')+(NZ(x)?'':' z')+(x<-0.0005?' neg':'')+'">'
    + (NZ(x) ? fmt(x) : '') + '</td>';

  function draw(){
    const V = GVAR(vkey);
    const B = flowAgg();
    const filt = ($('#stf').value||'').trim().toLowerCase();
    const fm = qMatcher(filt);
    /* ДЕРЕВО РОДИТЕЛЬ → РЕБЁНОК (заказчик 14.09.2026: «Усинск, Ухта — это
       Коми, а это производство/заготовка, и так со всеми»):
         корень справочника подразделений 1С («ПРОИЗВОДСТВО / ЗАГОТОВКА»,
         «БАЗЫ», «ДОГОВОРНЫЕ ПЛОЩАДКИ»…) → регион / база («Коми», «База
         Осенцы») → подразделение как в 1С («Усинск») → группа аналитического
         учёта → номенклатура → (бизнес-план, когда выбрана вся компания).
       Родители — из DATA.sk_tree (расчёт берёт их из справочника
       «Производственные подразделения»). */
    const T = new Map();
    const tot = {};
    const flowsSeen = new Set();
    const mk = (m, k) => { let o = m.get(k); if(!o){ o = {v:{}, kids:new Map(), w:new Set()}; m.set(k, o); } return o; };
    B.C.forEach((agg, key)=>{
      const [s, , w, c] = key.split(SEP);
      if(!keyOk(s, w)) return;
      const tr   = SK_TREE[w] || ['ПРОЧЕЕ', '—'];
      const podr = SK_PODR[w] || w || '—';
      const grp  = NM_GRP[c] || '— без группы —';
      const nm   = (NM[c]||c||'—');
      if(fm && !fm((tr[0]+' '+tr[1]+' '+podr+' '+grp+' '+nm+' '+c+' '+w+(sel?'':' '+nameOf(s))).toLowerCase())) return;
      const path = [[tr[0], tr[0]], [tr[1], tr[1]], [podr, podr], [grp, grp], [c, nm]];
      if(!sel) path.push([s, nameOf(s)]);
      let m = T; const chain = [];
      for(const [k, label] of path){ const n = mk(m, k); n.name = label; n.key = k; chain.push(n); m = n.kids; }
      chain.forEach(n=>{ addInto(n.v, agg); n.w.add(w); });
      addInto(tot, agg);
      for(const k in agg) if(k!=='_born' && NZ(agg[k])) flowsSeen.add(k);
    });
    const DEPTH_MAX = sel ? 4 : 5;              /* глубина листа */
    const LVL_TIP = ['корень справочника подразделений 1С', 'регион / база',
                     'подразделение как в 1С', 'группа аналитического учёта', 'код ', 'серия '];
    const nPodr = (()=>{ let n=0; T.forEach(a=>a.kids.forEach(b=>{ n+=b.kids.size; })); return n; })();
    const cols = ORDER.filter(k=>flowsSeen.has(k))
      .concat([...flowsSeen].filter(k=>!ORDER.includes(k)).sort((a,b)=>a.localeCompare(b,'ru')));
    const bal = v => balanceOf(v);
    /* Колонки «как в 1С» больше нет: с 14.09.2026 ОСТАТОК считается по формуле
       1С (односторонние переработки входят), см. balanceOf. */
    const HAS_1C = false;
    const unit = unitLabel();

    /* ---- карточки: итог и сверка с Гантой ---- */
    const g = sel ? G_BY.get(sel) : null;
    const gv = g && g.v && g.v[vkey] && g.v[vkey][curMeasure()] ? g.v[vkey][curMeasure()] : null;
    const restT = bal(tot);
    /* узлы Ганты округлены до трёх знаков, отсюда допуск в пару тысячных */
    const same = gv ? Math.abs((gv.rest||0) - restT) <= 0.005 : null;
    cards.innerHTML =
        '<div class="card lead"><div class="k">ОСТАТОК'+(sel?' по бизнес-плану':' по компании')+'</div>'
        + '<div class="v'+(restT<-0.0005?' bad':'')+'">'+fmt(restT)+' <span class="unit">'+esc(unit)+'</span></div>'
        + '<div class="s">'+esc(String(V.title).replace(/^\s*\d+\s*[—-]\s*/,''))+(filt?' · с фильтром строк':'')+'</div></div>'
      + (gv ? '<div class="card"><div class="k">на Ганте</div><div class="v'+(same?'':' bad')+'">'
            + fmt(gv.rest||0)+' <span class="unit">'+esc(unit)+'</span></div>'
            + '<div class="s">'+(same ? '✓ совпадает с итогом вкладки'
                : filt ? 'итог с фильтром строк — не сравнивать' : '✗ не совпадает: разбираться')+'</div></div>'
          : sel ? '<div class="card"><div class="k">на Ганте</div><div class="v small">—</div>'
            + '<div class="s">в этом варианте и единице строки на Ганте нет</div></div>' : '')
      + '<div class="card"><div class="k">Купили</div><div class="v">'+fmt(tot['куплено']||0)+' <span class="unit">'+esc(unit)+'</span></div>'
        + '<div class="s">за вычетом возврата поставщику</div></div>'
      + '<div class="card"><div class="k">Продали</div><div class="v">'+fmt(tot['продано']||0)+' <span class="unit">'+esc(unit)+'</span></div>'
        + '<div class="s">по складам варианта</div></div>'
      + '<div class="card"><div class="k">Подразделений</div><div class="v">'+cnt(nPodr)+'</div>'
        + '<div class="s">'+cnt([...T.values()].reduce((a,n)=>a+n.w.size,0))+' складов</div></div>';
    slimCards(app);

    if(!T.size){
      box.innerHTML = '<div class="hint">Движений под выбранные условия нет'
        + (sel && V.sites.length ? ': по этому бизнес-плану на складах заготовки ничего не проходило — '
          + 'переключите на «вся компания»' : '')+'.</div>';
      return;
    }
    /* ОСТАТОК — ПЕРВОЙ ЧИСЛОВОЙ КОЛОНКОЙ: ради него вкладка и открыта, а
       потоков бывает до пятнадцати, и последняя колонка уезжала за экран */
    const head = '<tr><th>корень · регион · подразделение · группа · номенклатура</th>'
      + '<th class="num bal" title="приход минус расход по всем потокам — как ОСТАТОК в дереве и на Ганте">ОСТАТОК</th>'
      + (HAS_1C ? '<th class="num bal1c" title="то же плюс односторонние переработки (приход / расход без '
          + 'весовой пары): так остаток считает сама 1С. В проектный ОСТАТОК они не входят.">как в 1С</th>' : '')
      + cols.map(k=>'<th class="num" title="'+esc(MPHRASE[k]||k)+'">'+esc(colLabel(k))+'</th>').join('')
      + '</tr>';
    const cells = v => num(bal(v), 'bal') + (HAS_1C ? num(balanceOf1c(v), 'bal1c') : '')
      + cols.map(k=>num(v[k]||0)).join('');
    const rowHtml = (n, depth) => {
      const hasKids = depth < DEPTH_MAX && n.kids.size > 0;
      const title = (depth >= 4 ? LVL_TIP[depth] + n.key : LVL_TIP[depth])
        + (depth <= 2 || depth === 4 ? '\nсклады: ' + [...n.w].join(', ') : '');
      return '<tr class="sr d'+depth+(hasKids?' rp':'')+'" data-d="'+depth+'">'
        + '<td style="padding-left:'+(8+depth*16)+'px" title="'+esc(title)+'">'
          + (hasKids ? '<span class="rtog">▸</span> ' : '<span class="rtog"></span> ')
          + (depth<=1 ? '<b>'+esc(n.name)+'</b>' : esc(n.name))
          + (depth<=2 && n.w.size>1 ? ' <span class="muted">'+cnt(n.w.size)+' скл.</span>' : '')
        + '</td>' + cells(n.v) + '</tr>';
    };
    const sortK = m => [...m.entries()].sort((x,y)=>Math.abs(bal(y[1].v))-Math.abs(bal(x[1].v))
                                              || String(x[0]).localeCompare(String(y[0]),'ru'));
    box.innerHTML = '<table class="recon stock sticky1"><thead>'+head+'</thead><tbody>'
      + '<tr class="rtot"><td><b>ИТОГО</b></td>'+cells(tot)+'</tr>'
      + sortK(T).map(([, n])=>rowHtml(n, 0)).join('')
      + '</tbody></table>';
    const tbody = box.querySelector('tbody');
    /* ⚠️ ДЕТИ СТРОЯТСЯ ПРИ РАСКРЫТИИ: по всей компании кодов × БП — десятки
       тысяч строк, рисовать их все разом незачем. Ссылки на узлы держим в
       WeakMap по элементу строки, чтобы не сериализовать ключи в data-*. */
    const NODE = new WeakMap();
    tbody.querySelectorAll('tr.d0').forEach((tr, i)=>NODE.set(tr, sortK(T)[i][1]));
    const kidsOf = (n, depth) => sortK(n.kids).map(([, m])=>rowHtml(m, depth+1));
    tbody.addEventListener('click', ev=>{
      const tr = ev.target.closest('tr.rp'); if(!tr) return;
      const n = NODE.get(tr); if(!n) return;
      const depth = +tr.dataset.d;
      const open = tr.classList.toggle('open');
      tr.querySelector('.rtog').textContent = open ? '▾' : '▸';
      if(open){
        if(!tr.dataset.built){
          tr.dataset.built = '1';
          const html = kidsOf(n, depth);
          const tmp = document.createElement('tbody'); tmp.innerHTML = html.join('');
          const kids = [...tmp.children];
          const order = sortK(n.kids);
          kids.forEach((k, i)=>NODE.set(k, order[i][1]));
          let at = tr;
          kids.forEach(k=>{ at.after(k); at = k; });
        } else {
          /* уже построены — показать прямых детей (их потомки остаются свёрнутыми) */
          let x = tr.nextElementSibling;
          while(x && +x.dataset.d > depth){
            if(+x.dataset.d === depth+1) x.hidden = false;
            x = x.nextElementSibling;
          }
        }
      } else {
        let x = tr.nextElementSibling;
        while(x && +x.dataset.d > depth){
          x.hidden = true; x.classList.remove('open');
          const tg = x.querySelector('.rtog'); if(tg && x.classList.contains('rp')) tg.textContent = '▸';
          x = x.nextElementSibling;
        }
      }
    });
    /* один бизнес-план — раскрыть корни и регионы сразу: их обычно 1–3, и
       сверять начинают с подразделений, а не с одной строки */
    if(sel && nPodr <= 6){
      tbody.querySelectorAll('tr.d0').forEach(tr=>tr.click());
      tbody.querySelectorAll('tr.d1').forEach(tr=>tr.click());
    }
    wrapWideTables(box);
  }

  bar.querySelectorAll('.gvars button').forEach(b=>b.addEventListener('click',()=>{
    vkey = b.dataset.v;
    bar.querySelectorAll('.gvars button').forEach(x=>x.classList.toggle('on', x===b));
    try{ localStorage.setItem('metoptorg.gvar', vkey); }catch(e){}
    draw();
  }));
  $('#stq').addEventListener('input', fillSelect);
  $('#stbp').addEventListener('change', ()=>{ sel = $('#stbp').value; draw(); });
  /* фильтр строк — с задержкой: перерисовывать на каждую букву незачем */
  let _ft = null;
  $('#stf').addEventListener('input', ()=>{ clearTimeout(_ft); _ft = setTimeout(draw, 250); });
  $('#stmeas').addEventListener('change', e=>{ setMeasure(e.target.value); rerenderTab(); });
  fillSelect();
  draw();
};
/** Переход на «Остатки» с бизнес-планом и вариантом — с Ганты и сводных. */
function goStock(series, vk){
  window.__SPRESET = {s: series, var: vk};
  render('stock');
}
