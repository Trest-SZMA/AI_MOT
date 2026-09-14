/* Сортировка по заголовкам и ручная правка ячеек таблицы штрафов */
(function () {
  const table = document.getElementById('finesTable');
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const readonly = table.dataset.readonly === '1';

  /* ---------- сортировка ---------- */
  const headers = Array.from(table.querySelectorAll('th'));
  table.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const idx = headers.indexOf(th);
      const dir = th.classList.contains('asc') ? -1 : 1;
      headers.forEach((h) => h.classList.remove('asc', 'desc'));
      th.classList.add(dir === 1 ? 'asc' : 'desc');
      const isNum = th.dataset.type === 'num';
      const rows = Array.from(tbody.querySelectorAll('tr')).filter((r) => !r.querySelector('.empty'));
      rows.sort((a, b) => {
        const ta = cellVal(a.cells[idx]), tb = cellVal(b.cells[idx]);
        if (isNum) return dir * ((parseFloat(ta) || 0) - (parseFloat(tb) || 0));
        return dir * String(ta).localeCompare(String(tb), 'ru');
      });
      rows.forEach((r) => tbody.appendChild(r));
      renumber();
    });
  });

  function cellVal(td) {
    if (!td) return '';
    return td.dataset.sort !== undefined ? td.dataset.sort : td.textContent.trim();
  }

  /* ---------- сворачивание длинного текста ---------- */
  const CLAMP_FIELDS = ['company', 'vehicle', 'article', 'violation'];
  const CLAMP_LEN = 70;

  function clampCell(td) {
    const text = td.textContent.trim();
    const wrapped = td.querySelector('span.clamp');
    if (text.length <= CLAMP_LEN) {
      if (wrapped) td.textContent = text;
      return;
    }
    if (!wrapped) {
      td.textContent = '';
      const span = document.createElement('span');
      span.className = 'clamp';
      span.textContent = text;
      span.title = 'Нажмите, чтобы развернуть';
      td.appendChild(span);
    }
  }

  tbody.querySelectorAll('td.editable').forEach((td) => {
    if (CLAMP_FIELDS.includes(td.dataset.field)) clampCell(td);
  });

  tbody.addEventListener('click', (ev) => {
    const span = ev.target.closest('span.clamp');
    if (!span) return;
    span.classList.toggle('open');
    span.title = span.classList.contains('open') ? 'Нажмите, чтобы свернуть' : 'Нажмите, чтобы развернуть';
  });

  function renumber() {
    Array.from(tbody.querySelectorAll('tr td.rownum')).forEach((td, i) => { td.textContent = i + 1; });
  }

  /* ---------- ручная правка (двойной клик) ---------- */
  if (readonly) return;

  /* смена статуса — одинарный клик по бейджу */
  const STATUSES = ['Не оплачен', 'Оплачен', 'ИП возбуждено', 'Обжалуется', 'Отменен'];
  function badgeClass(s) {
    return s === 'Оплачен' ? 'ok' : (s === 'ИП возбуждено' ? 'warn' : 'bad');
  }
  tbody.addEventListener('click', (ev) => {
    const td = ev.target.closest('td.status-cell');
    if (!td || td.querySelector('select')) return;
    const current = td.dataset.sort;
    const oldHtml = td.innerHTML;
    const sel = document.createElement('select');
    sel.className = 'cell-edit';
    STATUSES.forEach((s) => {
      const o = document.createElement('option');
      o.value = o.textContent = s;
      if (s === current) o.selected = true;
      sel.appendChild(o);
    });
    td.innerHTML = '';
    td.appendChild(sel);
    sel.focus();

    let done = false;
    const restore = () => { if (!done) { done = true; td.innerHTML = oldHtml; } };
    const commit = () => {
      if (done) return;
      done = true;
      const value = sel.value;
      if (value === current) { td.innerHTML = oldHtml; return; }
      const body = new URLSearchParams({ field: 'status', value });
      fetch('/fine/' + td.dataset.id + '/field', { method: 'POST', body })
        .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
        .then(({ ok, j }) => {
          if (!ok || !j.ok) { alert(j.error || 'Ошибка сохранения'); td.innerHTML = oldHtml; return; }
          td.dataset.sort = value;
          td.innerHTML = '<span class="badge ' + badgeClass(value) + '">' + value + '</span>';
          td.classList.add('saved');
          setTimeout(() => td.classList.remove('saved'), 900);
          updatePayButton(td, value);
        })
        .catch(() => { alert('Нет связи с сервером'); td.innerHTML = oldHtml; });
    };
    sel.addEventListener('change', commit);
    sel.addEventListener('blur', () => setTimeout(commit, 100));
    sel.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' || e.key === 'Esc' || e.keyCode === 27) restore();
    });
  });

  function updatePayButton(td, value) {
    const row = td.closest('tr');
    const form = row.querySelector('form[action$="/status"]');
    if (!form) return;
    const hidden = form.querySelector('input[name="status"]');
    const btn = form.querySelector('button');
    if (value === 'Оплачен') {
      hidden.value = 'Не оплачен';
      btn.className = 'mini gray';
      btn.title = 'Снять отметку об оплате';
      btn.textContent = '↩';
    } else {
      hidden.value = 'Оплачен';
      btn.className = 'mini';
      btn.title = 'Отметить оплаченным';
      btn.textContent = '✓ оплачен';
    }
  }

  tbody.addEventListener('dblclick', (ev) => {
    const td = ev.target.closest('td.editable');
    if (!td || td.querySelector('input')) return;
    ev.preventDefault();
    startEdit(td);
  });

  function currentText(td) {
    const t = td.textContent.trim();
    return t === '—' ? '' : t;
  }

  function startEdit(td) {
    const field = td.dataset.field;
    const old = currentText(td);
    const oldHtml = td.innerHTML;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cell-edit';
    input.value = field === 'amount' ? old.replace(/\s/g, '') : old;
    td.innerHTML = '';
    td.appendChild(input);
    input.focus();
    input.select();

    let finished = false;
    const cancel = () => { if (finished) return; finished = true; td.innerHTML = oldHtml; };
    const save = () => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      if (value === old) { td.innerHTML = oldHtml; return; }
      const body = new URLSearchParams({ field, value });
      fetch('/fine/' + td.dataset.id + '/field', { method: 'POST', body })
        .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
        .then(({ ok, j }) => {
          if (!ok || !j.ok) { alert(j.error || 'Ошибка сохранения'); td.innerHTML = oldHtml; return; }
          applyValue(td, field, j);
        })
        .catch(() => { alert('Нет связи с сервером'); td.innerHTML = oldHtml; });
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'Return' || e.keyCode === 13) { e.preventDefault(); save(); }
      if (e.key === 'Escape' || e.key === 'Esc' || e.keyCode === 27) cancel();
    });
    input.addEventListener('blur', save);
  }

  function applyValue(td, field, resp) {
    if (td.dataset.sort !== undefined) td.dataset.sort = resp.sort;
    if (field === 'number' && td.dataset.link) {
      td.innerHTML = '';
      const a = document.createElement('a');
      a.href = td.dataset.link;
      a.textContent = resp.display;
      td.appendChild(a);
    } else {
      td.textContent = resp.display;
      if (CLAMP_FIELDS.includes(field)) clampCell(td);
    }
    td.classList.add('saved');
    setTimeout(() => td.classList.remove('saved'), 900);
    if (field === 'amount') updateTotal();
  }

  function updateTotal() {
    const cell = document.getElementById('totalCell');
    if (!cell) return;
    let sum = 0;
    tbody.querySelectorAll('td[data-field="amount"]').forEach((td) => { sum += parseFloat(td.dataset.sort) || 0; });
    cell.innerHTML = '<b>' + sum.toLocaleString('ru-RU').replace(/,/g, ' ') + '</b>';
  }
})();
