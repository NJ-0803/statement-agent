/*
 * Statement Intelligence Agent — browser flow.
 *
 * Add files -> Check what I found -> Fix highlighted items -> Done, over the /api/imports job API.
 * All text is inserted with textContent (never innerHTML): statement contents are untrusted.
 * No alert/confirm dialogs: destructive actions ask for a second click on the same button instead.
 */
(() => {
  'use strict';

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const WAITING = new Set(['uploaded', 'analyzing']);

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function announce(message) {
    const region = $('#announce');
    region.textContent = '';
    setTimeout(() => { region.textContent = message; }, 60);
  }

  async function api(method, url, body, isForm = false) {
    const opts = { method, headers: {} };
    if (method !== 'GET') opts.headers['X-CSRF-Token'] = csrf;
    if (body && isForm) opts.body = body;
    else if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, opts); } catch (e) { throw new Error("I couldn't reach the app. Is it still running?"); }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok && !(data && data.rejected)) {
      const err = new Error((data && data.error) || `Something went wrong (error ${res.status}).`);
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function money(amount, currency) {
    try {
      return new Intl.NumberFormat('en-IN', { style: 'currency', currency }).format(Number(amount));
    } catch (e) { return `${amount} ${currency}`; }
  }
  function moneyList(obj) {
    const entries = Object.entries(obj || {});
    return entries.length ? entries.map(([c, a]) => money(a, c)).join(' + ') : 'None';
  }
  function niceDate(iso) {
    if (!iso) return '';
    const d = new Date(`${iso}T00:00:00`);
    return isNaN(d) ? iso : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
  }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}`; }

  function statusBadge(state) {
    const map = {
      committed: ['good', 'Added'], ready: ['good', 'Ready to add'], needs_review: ['warn', 'Needs your check'],
      needs_mapping: ['warn', 'Check the columns'], needs_password: ['warn', 'Needs its password'], analyzing: ['info', 'Reading…'], uploaded: ['info', 'Waiting'],
      failed: ['bad', "Couldn't use this file"], duplicate: ['info', 'Already added'],
      rolled_back: ['info', 'Removed'], cancelled: ['info', 'Cancelled'],
    };
    const [cls, text] = map[state] || ['info', state];
    return h('span', { class: `status ${cls}`, text });
  }

  /** A destructive button that needs a second click within 6 seconds. */
  function confirmButton(label, confirmLabel, onConfirm, cls = 'btn danger') {
    let armed = false, timer = null;
    const btn = h('button', { type: 'button', class: cls, text: label });
    btn.addEventListener('click', async () => {
      if (!armed) {
        armed = true; btn.textContent = confirmLabel; announce(`${confirmLabel}? Press the button again to confirm.`);
        timer = setTimeout(() => { armed = false; btn.textContent = label; }, 6000);
        return;
      }
      clearTimeout(timer); btn.disabled = true;
      try { await onConfirm(); } finally { btn.disabled = false; armed = false; btn.textContent = label; }
    });
    return btn;
  }

  function errorBox(message) { return h('p', { class: 'banner bad', role: 'alert', text: message }); }

  // ------------------------------------------------------------------ navigation & status

  function showView(name) {
    $$('nav.tabs button').forEach((b) => b.toggleAttribute('aria-current', false));
    const tab = $(`nav.tabs button[data-view="${name}"]`);
    tab.setAttribute('aria-current', 'page');
    ['add', 'list', 'txns', 'ask'].forEach((v) => { $(`#view-${v}`).hidden = v !== name; });
    if (name === 'list') loadImports();
    if (name === 'txns') loadTransactions();
    if (name === 'ask') $('#question').focus();
  }
  $$('nav.tabs button').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));

  let ledgerReady = false, hasKey = false, hasGroq = false;
  async function checkStatus() {
    const bar = $('#status-bar');
    try {
      const s = await api('GET', '/api/status');
      ledgerReady = s.ready; hasKey = s.has_api_key;
      $('#groq-box').hidden = !(s.groq && s.ready);
      hasGroq = !!s.groq;
      let text = s.ready
        ? `You have ${plural(s.transaction_count, 'transaction')} from ${plural(s.document_count, 'statement')}.`
        : s.reason;
      if (!s.has_api_key) text += ' Asking questions and reading photos or scanned pages need an ANTHROPIC_API_KEY on the server.';
      bar.textContent = text;
      bar.classList.toggle('bad', false);
    } catch (e) {
      bar.textContent = e.message; bar.classList.add('bad');
    }
    $('#send').disabled = !(ledgerReady && hasKey);
  }

  // ------------------------------------------------------------------ step 1: add files

  const wizard = { picked: [], queue: [], fixIndex: 0, undo: [] };
  const STEP_IDS = ['files', 'check', 'fix', 'done'];

  function setStep(step, { progress = false } = {}) {
    const idx = STEP_IDS.indexOf(step);
    $$('ol.steps li').forEach((li, i) => {
      li.toggleAttribute('aria-current', false);
      li.classList.toggle('done', i < idx);
      if (i === idx) li.setAttribute('aria-current', 'step');
    });
    $('#step-files').hidden = step !== 'files' || progress;
    $('#step-progress').hidden = !progress;
    $('#step-check').hidden = step !== 'check' || progress;
    $('#step-fix').hidden = step !== 'fix' || progress;
    $('#step-done').hidden = step !== 'done' || progress;
  }

  function focusHeading(container) {
    const heading = $('h2', container);
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
  }

  function addPicked(fileList) {
    for (const f of fileList) {
      if (!wizard.picked.some((p) => p.name === f.name && p.size === f.size)) wizard.picked.push(f);
    }
    renderPicked();
  }

  function renderPicked() {
    const list = $('#picked');
    list.replaceChildren(...wizard.picked.map((f, i) => h('li', {},
      h('span', {}, f.name, h('span', { class: 'muted', text: ` · ${(f.size / 1024).toFixed(0)} KB` })),
      h('button', { type: 'button', class: 'btn quiet', 'aria-label': `Remove ${f.name}`, text: 'Remove',
        onclick: () => { wizard.picked.splice(i, 1); renderPicked(); } }),
    )));
    $('#start').disabled = wizard.picked.length === 0;
  }

  $('#file-input').addEventListener('change', (e) => { addPicked(e.target.files); e.target.value = ''; });
  $('#photo-input').addEventListener('change', (e) => { addPicked(e.target.files); e.target.value = ''; });
  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', (e) => addPicked(e.dataTransfer.files));

  $('#start').addEventListener('click', async () => {
    const form = new FormData();
    wizard.picked.forEach((f) => form.append('files', f));
    const list = $('#progress-list');
    list.replaceChildren(h('li', { text: `Sending ${plural(wizard.picked.length, 'file')}…` }));
    setStep('files', { progress: true });
    focusHeading($('#step-progress'));
    announce('Reading your files. This can take a little while for scanned pages.');

    let data;
    try {
      data = await api('POST', '/api/imports', form, true);
    } catch (e) {
      list.replaceChildren(h('li', {}, errorBox(e.message)));
      list.append(h('li', {}, h('button', { type: 'button', class: 'btn', text: 'Choose files again', onclick: resetToFiles })));
      return;
    }
    const rows = {};
    list.replaceChildren();
    for (const r of data.rejected || []) {
      list.append(h('li', {}, h('span', {}, h('strong', { text: r.file }), ` — ${r.error}`), statusBadge('failed')));
    }
    for (const job of data.imports) {
      rows[job.id] = h('li', {}, h('span', {}, h('strong', { text: job.file }), h('span', { class: 'msg', text: ` — ${job.message}` })), statusBadge(job.state));
      list.append(rows[job.id]);
    }
    wizard.picked = []; renderPicked();
    if (!data.imports.length) {
      list.append(h('li', {}, h('button', { type: 'button', class: 'btn', text: 'Choose other files', onclick: resetToFiles })));
      announce('None of those files could be used.');
      return;
    }

    const pending = new Set(data.imports.map((j) => j.id));
    while (pending.size) {
      await sleep(1000);
      for (const id of Array.from(pending)) {
        try {
          const job = await api('GET', `/api/imports/${id}`);
          const row = rows[id];
          row.replaceChildren(h('span', {}, h('strong', { text: job.file }), ` — ${job.message}`), statusBadge(job.state));
          if (!WAITING.has(job.state)) pending.delete(id);
        } catch (e) { pending.delete(id); }
      }
    }
    wizard.queue = data.imports.map((j) => j.id);
    announce('Finished reading.');
    nextJob();
  });

  function resetToFiles() {
    wizard.queue = [];
    setStep('files');
    // #dropzone is a <label>, which cannot take focus — focus the input it stands for, so keyboard
    // users land back on "Choose statement files" instead of at the top of the document.
    $('#file-input').focus();
  }

  async function nextJob() {
    const id = wizard.queue.shift();
    if (!id) { resetToFiles(); checkStatus(); return; }
    openJob(id);
  }

  async function openJob(id) {
    showView('add');
    let p;
    try { p = await api('GET', `/api/imports/${id}/preview`); } catch (e) {
      const box = $('#step-check'); box.replaceChildren(errorBox(e.message)); setStep('check'); return;
    }
    wizard.fixIndex = 0; wizard.undo = [];
    route(p);
  }

  function route(p) {
    if (p.state === 'committed') return renderDone(p);
    if (['needs_mapping', 'needs_review', 'ready'].includes(p.state)) return renderCheck(p);
    if (p.state === 'needs_password') return renderPassword(p);
    return renderProblem(p);
  }

  function remainingNote() {
    return wizard.queue.length ? h('p', { class: 'muted', text: `${plural(wizard.queue.length, 'more file')} waiting after this one.` }) : null;
  }

  // ------------------------------------------------------------------ unusable file

  function renderPassword(p) {
    const box = $('#step-check');
    const alerts = h('div');
    const input = h('input', { type: 'password', id: 'pdf-password', autocomplete: 'off' });
    const form = h('form', {},
      h('label', { class: 'field', for: 'pdf-password', text: 'Password for this PDF' }), input,
      h('p', { class: 'muted', text: 'Banks often use part of your name and date of birth. The password is only used to open this file; it is not saved.' }),
      h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary', text: 'Open the file' }),
        h('button', { type: 'button', class: 'btn quiet', text: 'Cancel this file', onclick: async () => {
          try { await api('DELETE', `/api/imports/${p.id}`); } catch (e) { announce(e.message); }
          nextJob();
        } })));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const password = input.value;
      input.value = '';
      try {
        await api('POST', `/api/imports/${p.id}/password`, { password });
      } catch (err) { alerts.replaceChildren(errorBox(err.message)); input.focus(); return; }
      setStep('files', { progress: true });
      $('#progress-list').replaceChildren(h('li', { text: `Reading ${p.file}…` }));
      let job;
      do { await sleep(1000); job = await api('GET', `/api/imports/${p.id}`); } while (WAITING.has(job.state));
      openJob(p.id);
    });
    box.replaceChildren(h('div', { class: 'panel' }, h('h2', { text: p.file }), statusBadge(p.state),
      h('p', { style: 'margin-top:.75rem', text: p.message }), alerts, form, remainingNote()));
    setStep('check'); focusHeading(box);
  }

  function renderProblem(p) {
    const box = $('#step-check');
    const actions = h('div', { class: 'actions' });
    if (p.state === 'failed') {
      actions.append(h('button', { type: 'button', class: 'btn', text: 'Try reading it again', onclick: async () => {
        try { await api('POST', `/api/imports/${p.id}/retry`); } catch (e) { box.prepend(errorBox(e.message)); return; }
        const list = $('#progress-list');
        list.replaceChildren(h('li', { text: `Reading ${p.file} again…` }));
        setStep('files', { progress: true });
        let job;
        do { await sleep(1000); job = await api('GET', `/api/imports/${p.id}`); } while (WAITING.has(job.state));
        openJob(p.id);
      } }));
    }
    actions.append(h('button', { type: 'button', class: 'btn primary', text: wizard.queue.length ? 'Next file' : 'Add other files', onclick: nextJob }));
    box.replaceChildren(h('div', { class: 'panel' },
      h('h2', { text: p.file }), statusBadge(p.state), h('p', { style: 'margin-top:.75rem', text: p.message }),
      (p.warnings || []).length ? h('details', {}, h('summary', { text: 'What went wrong, in detail' }),
        h('ul', {}, ...p.warnings.map((w) => h('li', { text: w })))) : null,
      remainingNote(), actions,
    ));
    setStep('check'); focusHeading(box);
  }

  // ------------------------------------------------------------------ step 2: check what I found

  function openIssues(p) { return (p.issues || []).filter((i) => !i.resolution && i.severity !== 'info'); }

  function renderCheck(p, flash) {
    const box = $('#step-check');
    const s = p.summary || {};
    const issues = openIssues(p);
    const children = [h('h2', { text: `Check what I found — ${p.file}` })];
    if (flash) children.push(h('p', { class: 'banner', role: 'status', text: flash }));

    if (p.state === 'needs_mapping') {
      children.push(h('p', { class: 'headline', text: 'Please check how I read the columns in this file.' }));
      const reasons = [...((p.mapping && p.mapping.ambiguities) || [])];
      if (p.mapping && p.mapping.missing_required && p.mapping.missing_required.length) {
        reasons.push(`I'm not sure which column holds the ${p.mapping.missing_required.join(' and the ')}.`);
      }
      if (p.message && p.message !== 'Please check how I read the columns.') reasons.push(p.message);
      if (reasons.length) children.push(h('ul', {}, ...reasons.map((r) => h('li', { text: r }))));
    } else {
      const period = s.date_from ? ` from ${niceDate(s.date_from)} to ${niceDate(s.date_to)}` : '';
      const tail = issues.length ? ` ${plural(issues.length, 'item needs', 'items need')} your check.` : ' Everything checks out.';
      children.push(h('p', { class: 'headline', text: `I found ${plural(s.transaction_count || 0, 'transaction')}${period}.${tail}` }));
      children.push(h('dl', { class: 'facts' },
        h('dt', { text: 'Money in' }), h('dd', { text: moneyList(s.money_in) }),
        h('dt', { text: 'Money out' }), h('dd', { text: moneyList(s.money_out) }),
        h('dt', { text: 'Account' }), h('dd', { text: s.account || 'Not stated in the file' }),
        h('dt', { text: 'Currency' }), h('dd', { text: currencyText(p) }),
        h('dt', { text: 'Adds up?' }), h('dd', {}, reconciliationBadge(s.reconciliation), s.reconciliation_detail ? h('span', { text: ` ${s.reconciliation_detail}` }) : null),
      ));
    }

    const counts = p.counts || {};
    if (counts.ignored) {
      children.push(h('details', {}, h('summary', { text: `Rows I didn't use as transactions (${counts.ignored})` }),
        h('ul', {}, ...(p.ignored_rows || []).map((r) => h('li', { text: `${r.row ? `Row ${r.row}` : r.page ? `Page ${r.page}` : 'Line'}: ${r.reason}${r.text ? ` — “${r.text}”` : ''}` })))));
    }

    const kept = (p.extra_columns || []).filter((c) => c.kind !== 'empty');
    if (kept.length && p.state !== 'needs_mapping') {
      children.push(h('details', {}, h('summary', { text: `Other columns I kept (${kept.length})` }),
        h('ul', {}, ...kept.map((c) => h('li', { text: `${c.header} — ${c.meaning ? `${c.meaning}, ` : ''}${c.kind}${c.sample.length ? `, e.g. ${c.sample.join(', ')}` : ''}` })))));
    }
    if (p.kind === 'tabular' && (p.headers || []).length) children.push(mappingEditor(p));
    if (p.state !== 'needs_mapping' && (p.transactions || []).length) children.push(transactionsTable(p.transactions));

    const actions = h('div', { class: 'actions' });
    if (p.state === 'needs_review') {
      actions.append(h('button', { type: 'button', class: 'btn primary', text: `Next: check ${plural(issues.length, 'item')}`,
        onclick: () => { wizard.fixIndex = 0; renderFix(p); } }));
    } else if (p.state === 'ready') {
      actions.append(h('button', { type: 'button', class: 'btn primary', text: 'Add to my statements', onclick: (e) => commit(p, e.currentTarget) }));
    }
    actions.append(h('button', { type: 'button', class: 'btn quiet', text: 'Cancel this file', onclick: async () => {
      try { await api('DELETE', `/api/imports/${p.id}`); announce(`${p.file} cancelled.`); } catch (e) { announce(e.message); }
      nextJob();
    } }));
    children.push(remainingNote(), actions);

    box.replaceChildren(h('div', { class: 'panel' }, ...children));
    setStep('check'); focusHeading(box);
  }

  function reconciliationBadge(status) {
    const map = {
      RECONCILED: ['good', 'Yes — matches the statement'],
      MISMATCH: ['bad', 'No — doesn’t match the statement'],
      CANNOT_CHECK: ['info', 'Couldn’t check'],
    };
    const [cls, text] = map[status] || ['info', 'Nothing to check against'];
    return h('span', { class: `status ${cls}`, text });
  }

  function currencyText(p) {
    const m = p.mapping;
    if (m) {
      if (m.currency_source === 'column') return 'Read from the currency column';
      if (m.currency_source === 'user') return `${m.currency} — confirmed by you`;
      if (m.currency) return `${m.currency} (${{ header: 'from the column names', document: 'from the top of the file', amount_cells: 'from the money symbols', profile: 'as before' }[m.currency_source] || 'found in the file'})`;
      return 'Not written in the file — please confirm';
    }
    const assumed = (p.issues || []).some((i) => i.rule === 'currency_assumed' && !i.resolution);
    return assumed ? 'Not written in the file — please confirm' : 'Found in the statement';
  }

  function mappingEditor(p) {
    const m = p.mapping || { roles: {} };
    const byColumn = {};
    Object.entries(m.roles || {}).forEach(([role, col]) => { byColumn[col] = role; });
    const wrap = h('div', {}, h('h3', { text: 'How I read your columns' }),
      h('p', { class: 'muted', text: 'Each column shows a few rows from your file. Change a choice if it’s wrong, then press “Use these columns”.' }));
    const alerts = h('div');
    wrap.append(alerts);

    const alts = (p.sniff && p.sniff.alternatives) || [];
    let tableSelect = null;
    if (alts.length > 1) {
      tableSelect = h('select', { id: 'table-choice' }, ...alts.map((a, i) => h('option', {
        value: String(i), selected: m.sheet === a.sheet && m.header_row === a.header_row, text: a.label })));
      wrap.append(h('label', { class: 'field', for: 'table-choice', text: 'Which part of the file holds the transactions?' }), tableSelect,
        h('button', { type: 'button', class: 'btn quiet', style: 'margin-left:.5rem', text: 'Use this part', onclick: async () => {
          const a = alts[Number(tableSelect.value)];
          try { route(await api('PUT', `/api/imports/${p.id}/mapping`, { sheet: a.sheet, header_row: a.header_row })); }
          catch (e) { alerts.replaceChildren(errorBox(e.message)); }
        } }));
    }

    const extraByIndex = {};
    (p.extra_columns || []).forEach((c) => { extraByIndex[c.index] = c; });
    const selects = p.headers.map((header, j) => {
      const id = `role-${j}`;
      const sel = h('select', { id }, h('option', { value: '', text: 'Keep as extra information' }),
        ...p.role_options.map((o) => h('option', { value: o.value, selected: byColumn[j] === o.value, text: o.label })));
      const extra = extraByIndex[j];
      return { sel, cell: h('th', { scope: 'col' }, h('div', { text: header }),
        extra ? h('div', { class: 'muted', style: 'font-weight:400', text: extraText(extra) }) : null,
        h('div', { class: 'role-select' }, h('label', { for: id, text: 'What is in this column?' }), sel)) };
    });
    const rows = (p.sample_rows || []).slice(0, 8).map((r) => h('tr', {},
      ...p.headers.map((_, j) => h('td', { text: (r.cells[j] || '') }))));
    wrap.append(h('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Columns in your file' },
      h('table', {}, h('thead', {}, h('tr', {}, ...selects.map((s) => s.cell))), h('tbody', {}, ...rows))));

    const ccy = h('select', { id: 'currency-choice' }, ...p.currencies.map((c) => h('option', { value: c, selected: (m.currency || 'INR') === c, text: c })));
    const order = m.date_order || 'DMY';
    const radio = (value, label) => h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'date-order', value, checked: order === value }), label);
    wrap.append(
      h('fieldset', {}, h('legend', { text: 'Dates are written as' }), radio('DMY', 'Day / month / year — 31/03/2025'), radio('MDY', 'Month / day / year — 03/31/2025')),
      h('p', { style: 'margin-top:1rem' }, h('label', { class: 'field', for: 'currency-choice', text: 'Currency for amounts without one' }), ccy),
    );
    if (hasGroq) {
      wrap.append(h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn quiet', text: 'Ask Groq to suggest the columns', onclick: async (e) => {
        e.currentTarget.disabled = true;
        try {
          const r = await api('POST', `/api/imports/${p.id}/suggest-columns`);
          const byCol = {};
          Object.entries(r.roles || {}).forEach(([role, col]) => { byCol[col] = role; });
          selects.forEach(({ sel }, j) => { sel.value = byCol[j] || ''; });
          alerts.replaceChildren(h('p', { class: 'banner', role: 'status', text: r.note || 'Groq filled in its suggestions (only the column names and kinds of values were sent). Check them, then press “Use these columns”.' }));
        } catch (err) { alerts.replaceChildren(errorBox(err.message)); }
        e.currentTarget.disabled = false;
      } })));
    }
    wrap.append(h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', text: 'Use these columns', onclick: async (e) => {
      const roles = {};
      for (const [j, { sel }] of selects.entries()) {
        if (!sel.value) continue;
        if (roles[sel.value] !== undefined) {
          alerts.replaceChildren(errorBox(`“${sel.selectedOptions[0].text}” is chosen for more than one column. Pick it for just one.`));
          sel.focus(); return;
        }
        roles[sel.value] = j;
      }
      const body = { roles, currency: ccy.value, date_order: $('input[name="date-order"]:checked', wrap).value, sheet: m.sheet, header_row: m.header_row };
      e.currentTarget.disabled = true;
      try {
        const next = await api('PUT', `/api/imports/${p.id}/mapping`, body);
        announce(next.message);
        renderCheck(next, 'Updated with your column choices.');
      } catch (err) { alerts.replaceChildren(errorBox(err.message)); e.currentTarget.disabled = false; }
    } })));
    return wrap;
  }

  function extraText(c) {
    if (c.kind === 'empty') return 'Kept (empty in this file)';
    return `Kept: ${c.meaning ? `${c.meaning}, ` : ''}${c.kind}`;
  }

  function extrasLine(fields) {
    const entries = Object.entries(fields || {});
    if (!entries.length) return null;
    return h('p', { class: 'muted', style: 'margin:.1rem 0 0', text: entries.map(([k, v]) => `${k}: ${v}`).join(' · ') });
  }

  const FIELD_WORDS = { date: 'Date', amount: 'Amount', direction: 'Money in or out', currency: 'Currency' };

  function transactionsTable(txns) {
    return h('div', {}, h('h3', { text: 'First transactions' }),
      h('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'First transactions' },
        h('table', {},
          h('thead', {}, h('tr', {}, h('th', { text: 'Date' }), h('th', { text: 'Description' }), h('th', { text: 'Category' }), h('th', { class: 'num', text: 'Money in' }), h('th', { class: 'num', text: 'Money out' }), h('th', { class: 'num', text: 'Balance' }))),
          h('tbody', {}, ...txns.slice(0, 10).map((t) => h('tr', {},
            h('td', { text: niceDate(t.date) }),
            h('td', {}, t.description,
              t.flagged ? h('span', { class: 'status warn', style: 'margin-left:.4rem', text: 'Worth a look' }) : null,
              (t.unsure || []).length ? h('span', { class: 'status warn', style: 'margin-left:.4rem', text: 'Not sure' }) : null,
              (t.unsure || []).length ? h('div', { class: 'muted', text: t.unsure.map((u) => `${FIELD_WORDS[u.field] || u.field}: ${u.reason}`).join('. ') }) : null),
            h('td', {}, t.category || '', t.why && t.why.category.startsWith('your rule') ? h('div', { class: 'muted', text: 'from your rule' }) : null),
            h('td', { class: 'num', text: t.direction === 'CREDIT' ? money(t.amount, t.currency) : '' }),
            h('td', { class: 'num', text: t.direction === 'DEBIT' ? money(t.amount, t.currency) : '' }),
            h('td', { class: 'num', text: t.balance_after ? money(t.balance_after, t.currency) : '' }),
          ))))));
  }

  // ------------------------------------------------------------------ step 3: fix highlighted items

  async function review(p, body, inverse) {
    const next = await api('PUT', `/api/imports/${p.id}/review`, body);
    if (inverse) wizard.undo.push(inverse);
    announce(next.message);
    return next;
  }

  function renderFix(p) {
    const box = $('#step-fix');
    const issues = openIssues(p);
    if (!issues.length) { renderCheck(p, p.state === 'ready' ? 'All items are handled. You can add this file now.' : undefined); return; }
    wizard.fixIndex = Math.min(wizard.fixIndex, issues.length - 1);
    const issue = issues[wizard.fixIndex];
    const alerts = h('div');

    const act = async (body, inverse) => {
      try { const next = await review(p, body, inverse); renderFix(next); }
      catch (e) { alerts.replaceChildren(errorBox(e.message)); }
    };
    const buttons = h('div', { class: 'actions' });
    const extra = [];

    if (issue.rule === 'currency_assumed') {
      const sel = h('select', { id: 'fix-currency' }, ...p.currencies.map((c) => h('option', { value: c, selected: c === 'INR', text: c })));
      extra.push(h('p', {}, h('label', { class: 'field', for: 'fix-currency', text: 'Currency of this file' }), sel));
      buttons.append(h('button', { type: 'button', class: 'btn primary', text: 'Confirm currency',
        onclick: () => act({ currency: sel.value, acknowledge: [issue.issue_id] }, { unacknowledge: [issue.issue_id] }) }));
    } else if (issue.rule === 'date_order_assumed' && p.kind === 'tabular') {
      const radio = (value, label) => h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'fix-order', value, checked: value === 'DMY' }), label);
      extra.push(h('fieldset', {}, h('legend', { text: 'Dates in this file are' }), radio('DMY', 'Day / month / year — 05/07/2025 is 5 July'), radio('MDY', 'Month / day / year — 05/07/2025 is 7 May')));
      buttons.append(h('button', { type: 'button', class: 'btn primary', text: 'Confirm date format',
        onclick: () => act({ date_order: $('input[name="fix-order"]:checked', box).value, acknowledge: [issue.issue_id] }, { unacknowledge: [issue.issue_id] }) }));
    } else if (issue.rule === 'amount_format_assumed' && p.kind === 'tabular') {
      const radio = (value, label) => h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'fix-decimal', value, checked: value === '.' }), label);
      extra.push(h('fieldset', {}, h('legend', { text: 'In this file, 1.234 means' }),
        radio('.', 'One thousand two hundred and thirty-four — the dot groups thousands'),
        radio(',', 'One point two three four — the dot is the decimal point')));
      buttons.append(h('button', { type: 'button', class: 'btn primary', text: 'Confirm number format',
        onclick: () => act({ decimal_separator: $('input[name="fix-decimal"]:checked', box).value === '.' ? ',' : '.', acknowledge: [issue.issue_id] }, { unacknowledge: [issue.issue_id] }) }));
    } else if (issue.rule === 'direction_unknown' || (issue.rule === 'low_confidence' && issue.field === 'direction' && p.kind === 'tabular')) {
      buttons.append(
        h('button', { type: 'button', class: 'btn primary', text: 'Money out', onclick: () => act({ directions: { [issue.target]: 'DEBIT' } }, { directions: { [issue.target]: null } }) }),
        h('button', { type: 'button', class: 'btn primary', text: 'Money in', onclick: () => act({ directions: { [issue.target]: 'CREDIT' } }, { directions: { [issue.target]: null } }) }),
      );
    } else if (issue.severity === 'check') {
      buttons.append(h('button', { type: 'button', class: 'btn primary', text: 'This is fine',
        onclick: () => act({ acknowledge: [issue.issue_id] }, { unacknowledge: [issue.issue_id] }) }));
    }
    if (issue.target) {
      buttons.append(h('button', { type: 'button', class: 'btn', text: 'Leave this row out',
        onclick: () => act({ exclude: [issue.target] }, { include: [issue.target] }) }));
    }
    if (issue.severity === 'blocking' && p.kind === 'tabular') {
      buttons.append(h('button', { type: 'button', class: 'btn quiet', text: 'Change column choices', onclick: () => renderCheck(p) }));
    }

    const nav = h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn quiet', text: 'Back', onclick: () => {
        if (wizard.fixIndex > 0) { wizard.fixIndex -= 1; renderFix(p); } else renderCheck(p);
      } }),
      issues.length > 1 ? h('button', { type: 'button', class: 'btn quiet', text: 'Skip for now', onclick: () => {
        wizard.fixIndex = (wizard.fixIndex + 1) % issues.length; renderFix(p);
      } }) : null,
      h('button', { type: 'button', class: 'btn quiet', text: 'Undo my last change', disabled: !wizard.undo.length, onclick: async () => {
        const inverse = wizard.undo.pop();
        try { renderFix(await review(p, inverse, null)); } catch (e) { alerts.replaceChildren(errorBox(e.message)); }
      } }),
    );

    box.replaceChildren(h('div', { class: `panel issue ${issue.severity}` },
      h('h2', { text: `Item ${wizard.fixIndex + 1} of ${issues.length}` }),
      issue.severity === 'blocking' ? h('span', { class: 'status bad', text: 'Must be sorted out before adding' }) : h('span', { class: 'status warn', text: 'Please check' }),
      alerts,
      h('p', { class: 'headline', style: 'margin-top:.75rem', text: issue.message }),
      issue.evidence ? h('div', {}, h('p', { class: 'muted', style: 'margin-bottom:.3rem', text: 'From your file:' }), h('p', { class: 'evidence', text: issue.evidence })) : null,
      h('p', { class: 'muted', text: issue.suggested_action }),
      ...extra, buttons, nav,
    ));
    setStep('fix'); focusHeading(box);
  }

  // ------------------------------------------------------------------ step 4: done

  async function commit(p, button) {
    button.disabled = true;
    try {
      const view = await api('POST', `/api/imports/${p.id}/commit`);
      announce('Added to your statements.');
      checkStatus();
      renderDone(view);
    } catch (e) {
      button.disabled = false;
      const box = $('#step-check');
      box.prepend(errorBox(e.message));
    }
  }

  function renderDone(view) {
    const box = $('#step-done');
    const s = view.summary || {};
    const period = s.date_from ? `${niceDate(s.date_from)} – ${niceDate(s.date_to)}` : '';
    const undone = h('div');
    const ask = (q) => { showView('ask'); $('#question').value = q; $('#ask-form').requestSubmit(); };
    const undoButton = confirmButton('Undo this import', 'Yes, remove these transactions', async () => {
      try {
        await api('POST', `/api/imports/${view.id}/rollback`);
        $('h2', box).textContent = `Removed — ${view.file} is no longer in your statements`;
        $('.cards', box).remove();
        undone.replaceChildren(h('p', { class: 'banner', role: 'status', text: 'Its transactions were removed. Nothing else changed.' }));
        undoButton.remove();
        announce('Import removed.');
        checkStatus();
      } catch (e) { undone.replaceChildren(errorBox(e.message)); }
    });
    box.replaceChildren(h('div', { class: 'panel' },
      h('h2', { text: `Done — ${view.file} is added` }),
      h('p', { class: 'headline', text: `${plural(s.transaction_count || view.transaction_count || 0, 'transaction')} added${period ? `, ${period}` : ''}.` }),
      h('div', { class: 'cards' },
        h('div', { class: 'card' }, h('div', { class: 'k', text: 'Money in' }), h('div', { class: 'v', text: moneyList(s.money_in) })),
        h('div', { class: 'card' }, h('div', { class: 'k', text: 'Money out' }), h('div', { class: 'v', text: moneyList(s.money_out) })),
        h('div', { class: 'card' }, h('div', { class: 'k', text: 'Transactions' }), h('div', { class: 'v', text: String(s.transaction_count || 0) })),
        h('div', { class: 'card' }, h('div', { class: 'k', text: 'Worth a look' }), h('div', { class: 'v', text: s.flagged ? String(s.flagged) : 'None' })),
      ),
      undone,
      h('h3', { text: 'Questions you could ask' }),
      h('div', { class: 'suggestions' },
        h('button', { type: 'button', class: 'btn quiet', disabled: !hasKey, text: 'What did I spend the most on?', onclick: () => ask('What did I spend the most on?') }),
        h('button', { type: 'button', class: 'btn quiet', disabled: !hasKey, text: 'Anything unusual I should check?', onclick: () => ask('Are there any charges I should double-check or dispute?') }),
        h('button', { type: 'button', class: 'btn quiet', disabled: !hasKey, text: 'What are my regular payments?', onclick: () => ask('What are my regular payments?') }),
      ),
      remainingNote(),
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn primary', text: wizard.queue.length ? 'Next file' : 'Add more files', onclick: nextJob }),
        undoButton,
      ),
    ));
    setStep('done'); focusHeading(box);
  }

  // ------------------------------------------------------------------ your statements

  async function loadImports() {
    const list = $('#imports-list');
    list.replaceChildren(h('li', { text: 'Loading…' }));
    let data;
    try { data = await api('GET', '/api/imports'); } catch (e) { list.replaceChildren(h('li', {}, errorBox(e.message))); return; }
    if (!data.imports.length) { list.replaceChildren(h('li', { text: 'Nothing added yet.' })); return; }
    list.replaceChildren(...data.imports.map((job) => {
      const s = job.summary || {};
      const actions = h('div', { class: 'actions', style: 'margin-top:.5rem' });
      if (job.state === 'committed') {
        actions.append(confirmButton('Undo import', 'Yes, remove it', async () => {
          try { await api('POST', `/api/imports/${job.id}/rollback`); announce(`${job.file} removed.`); } catch (e) { announce(e.message); }
          checkStatus(); loadImports();
        }));
      } else if (['needs_mapping', 'needs_review', 'ready', 'needs_password'].includes(job.state)) {
        actions.append(h('button', { type: 'button', class: 'btn primary', text: 'Continue', onclick: () => openJob(job.id) }));
        actions.append(h('button', { type: 'button', class: 'btn quiet', text: 'Cancel', onclick: async () => {
          try { await api('DELETE', `/api/imports/${job.id}`); } catch (e) { announce(e.message); }
          loadImports();
        } }));
      } else if (job.state === 'failed') {
        actions.append(h('button', { type: 'button', class: 'btn', text: 'See what went wrong', onclick: () => openJob(job.id) }));
      }
      const detail = job.state === 'committed' && s.date_from
        ? `${plural(job.transaction_count, 'transaction')}, ${niceDate(s.date_from)} – ${niceDate(s.date_to)}`
        : job.message;
      return h('li', {},
        h('div', { class: 'row' }, h('strong', { text: job.file }), statusBadge(job.state)),
        h('p', { class: 'muted', style: 'margin:.3rem 0 0', text: detail }),
        actions.childElementCount ? actions : null,
      );
    }));
  }

  // ------------------------------------------------------------------ transactions & corrections

  const txnState = { offset: 0, categories: [], types: [], editing: null };
  const LINK_WORDS = {
    refund: 'Refund', reimbursement: 'Reimbursement', transfer: 'Transfer between accounts', card_payment: 'Card bill payment',
  };
  const LINK_STATUS = { matched: ['good', 'Linked'], confirmed: ['good', 'You confirmed'], suggested: ['warn', 'Possible link'], rejected: ['info', 'Not related'] };

  async function decideLink(id, decision) {
    try {
      await api('POST', `/api/links/${id}/decision`, { decision });
      announce(decision === 'confirmed' ? 'Link confirmed.' : decision === 'rejected' ? 'Marked as not related.' : 'Your choice was undone.');
    } catch (e) { announce(e.message); }
    loadTransactions();
  }

  function linkLine(l) {
    const [cls, word] = LINK_STATUS[l.status] || ['info', l.status];
    const other = l.others.map((o) => `${o.description} (${o.direction === 'CREDIT' ? '+' : '−'}${money(o.amount, o.currency)}, ${niceDate(o.date)})`).join('; ');
    const buttons = [];
    if (l.status === 'suggested') {
      buttons.push(h('button', { type: 'button', class: 'btn', text: 'Yes, related', onclick: () => decideLink(l.id, 'confirmed') }));
    }
    if (l.status !== 'rejected') {
      buttons.push(h('button', { type: 'button', class: 'btn quiet', text: 'Not related', onclick: () => decideLink(l.id, 'rejected') }));
    }
    return h('div', { style: 'margin-top:.4rem' },
      h('p', { style: 'margin:0' }, h('span', { class: `status ${cls}`, text: word }), ` ${LINK_WORDS[l.kind] || l.kind}: ${other}`),
      h('p', { class: 'muted', style: 'margin:.1rem 0 0', text: l.reason }),
      buttons.length ? h('div', { class: 'actions', style: 'margin-top:.3rem' }, ...buttons) : null);
  }
  const PAGE = 50;

  function signedMoney(t) { return `${t.direction === 'CREDIT' ? '+' : '−'}${money(t.amount, t.currency)}`; }

  function categoryLine(t) {
    if (!t.is_purchase && t.direction === 'CREDIT') return 'Money in — not counted as spending.';
    if (!t.is_purchase) return `${t.kind.charAt(0).toUpperCase()}${t.kind.slice(1)} — not counted as spending.`;
    return t.category ? `Category: ${t.category} — ${t.why.category}.` : 'No category yet.';
  }

  async function loadTransactions({ append = false } = {}) {
    const list = $('#txn-list');
    if (!append) { txnState.offset = 0; list.replaceChildren(h('li', { text: 'Loading…' })); }
    const params = new URLSearchParams({ q: $('#txn-q').value.trim(), category: $('#txn-category').value, limit: PAGE, offset: txnState.offset });
    let data;
    try { data = await api('GET', `/api/transactions?${params}`); } catch (e) { list.replaceChildren(h('li', {}, errorBox(e.message))); return; }
    txnState.categories = data.categories;
    txnState.types = data.types || [];
    fillCategoryFilter(data.categories);
    const items = data.transactions.map(txnItem);
    if (append) list.append(...items); else list.replaceChildren(...(items.length ? items : [h('li', { text: 'No transactions match.' })]));
    txnState.offset += data.transactions.length;
    $('#txn-count').textContent = data.total ? `Showing ${txnState.offset} of ${plural(data.total, 'transaction')}.` : '';
    $('#txn-more').hidden = txnState.offset >= data.total;
    renderRules(data.rules);
    loadLinks();
  }

  async function loadLinks() {
    let data;
    try { data = await api('GET', '/api/links'); } catch (e) { return; }
    const suggested = data.links.filter((l) => l.status === 'suggested');
    $('#links-box').hidden = !suggested.length;
    $('#links-list').replaceChildren(...suggested.map((l) => h('li', {},
      h('p', { style: 'margin:0', text: `${LINK_WORDS[l.kind] || l.kind}?` }),
      h('ul', {}, ...l.members.map((m) => h('li', { text: `${m.description} — ${m.direction === 'CREDIT' ? '+' : '−'}${money(m.amount, m.currency)}, ${niceDate(m.date)}` }))),
      h('p', { class: 'muted', text: l.reason }),
      h('div', { class: 'actions', style: 'margin-top:.3rem' },
        h('button', { type: 'button', class: 'btn primary', text: 'Yes, related', onclick: () => decideLink(l.id, 'confirmed') }),
        h('button', { type: 'button', class: 'btn quiet', text: 'Not related', onclick: () => decideLink(l.id, 'rejected') })),
    )));
    const rec = data.recurring;
    $('#recurring-list').replaceChildren(...(rec.length ? rec.map((r) => {
      const d = r.details;
      const rejected = r.status === 'rejected';
      return h('li', {},
        h('div', { class: 'row' }, h('strong', { text: d.name }), h('span', { class: 'txn-amount', text: `${d.direction === 'CREDIT' ? '+' : '−'}${money(d.typical_amount, d.currency)} ${d.cadence}` })),
        h('p', { class: 'muted', style: 'margin:.2rem 0 0', text: rejected ? 'You said this isn’t a regular payment.' :
          `${plural(d.count, 'time')} so far, last on ${niceDate(d.last_date)}. Next expected around ${niceDate(d.next_expected)}.` }),
        d.amount_changed && !rejected ? h('p', { class: 'status warn', text: `Latest amount changed to ${money(d.last_amount, d.currency)}` }) : null,
        d.possibly_stopped && !rejected ? h('p', { class: 'status warn', text: 'Hasn’t appeared when expected — it may have stopped' }) : null,
        h('div', { class: 'actions', style: 'margin-top:.3rem' }, rejected
          ? h('button', { type: 'button', class: 'btn quiet', text: 'Undo', onclick: () => decideLink(r.id, null) })
          : h('button', { type: 'button', class: 'btn quiet', text: 'Not a regular payment', onclick: () => decideLink(r.id, 'rejected') })),
      );
    }) : [h('li', { class: 'muted', text: 'None found yet. It takes at least three similar payments on a regular schedule.' })]));
  }

  function fillCategoryFilter(categories) {
    const sel = $('#txn-category');
    const current = sel.value;
    sel.replaceChildren(h('option', { value: '', text: 'All' }), h('option', { value: '__none__', text: 'Purchases without a category' }),
      ...categories.map((c) => h('option', { value: c, text: c })));
    sel.value = current;
  }

  function txnItem(t) {
    const li = h('li', { 'data-txn': t.id });
    const render = () => {
      li.replaceChildren(...[
        h('div', { class: 'row' }, h('strong', { text: t.merchant_name || t.description }), h('span', { class: 'txn-amount', text: signedMoney(t) })),
        h('p', { class: 'muted', style: 'margin:.2rem 0 0', text: [niceDate(t.date), t.merchant_name ? t.description : null, t.file].filter(Boolean).join(' · ') }),
        h('p', { style: 'margin:.35rem 0 0', text: `Kind: ${t.type_label}${t.type_source === 'you' ? ' — you set this' : t.type_source === 'rule' ? ' — from your rule' : t.type_source === 'link' ? ' — linked to money from your own account' : t.type_unsure ? ' — my best guess, change it if wrong' : ''}.` }),
        t.is_purchase || t.category ? h('p', { style: 'margin:.1rem 0 0', text: categoryLine(t) }) : null,
        extrasLine(t.extra_fields),
        ...(t.links || []).map(linkLine),
        t.merchant_name ? h('p', { class: 'muted', style: 'margin:.1rem 0 0', text: `Merchant name “${t.merchant_name}” — ${t.why.merchant}.` }) : null,
        h('div', { class: 'actions', style: 'margin-top:.5rem' },
          h('button', { type: 'button', class: 'btn', 'data-change': '1', text: 'Change',
            'aria-label': `Change ${t.description}`, onclick: () => {
              // opening the editor removes the button that holds focus; move focus to the editor's
              // first field, and hand it back to this row's Change button when the editor closes
              li.replaceChildren(editor(t, () => { render(); $('button[data-change]', li).focus(); }));
              $('select, input', li).focus();
            } })),
      ].filter(Boolean));
    };
    render();
    return li;
  }

  let previewTimer = null;
  function editor(t, close) {
    const id = t.id.slice(0, 8);
    const alerts = h('div');
    const catInput = h('input', { type: 'text', id: `cat-${id}`, list: `cats-${id}`, value: t.category || '', autocomplete: 'off' });
    const typeSelect = h('select', { id: `type-${id}` }, ...txnState.types.map((o) => h('option', { value: o.value, text: o.label, selected: o.value === t.economic_type })));
    const nameInput = h('input', { type: 'text', id: `name-${id}`, value: t.merchant_name || '', autocomplete: 'off', placeholder: 'For example: Swiggy' });
    const patternInput = h('input', { type: 'text', id: `pat-${id}`, value: t.suggested_pattern, autocomplete: 'off' });
    const preview = h('p', { class: 'muted indent', role: 'status' });
    const one = h('input', { type: 'radio', name: `scope-${id}`, value: 'one', checked: true });
    const rule = h('input', { type: 'radio', name: `scope-${id}`, value: 'rule' });
    const patternBox = h('div', { class: 'indent', hidden: true },
      h('label', { class: 'field', for: `pat-${id}`, text: 'Descriptions containing these words' }), patternInput);

    const refreshPreview = () => {
      clearTimeout(previewTimer);
      if (!rule.checked) { preview.textContent = ''; return; }
      previewTimer = setTimeout(async () => {
        try {
          const p = await api('POST', '/api/rules/preview', { pattern: patternInput.value });
          const eg = p.examples.length ? ` For example: ${p.examples.slice(0, 3).join('; ')}.` : '';
          const mine = p.set_by_you ? ` ${plural(p.set_by_you, 'of them was', 'of them were')} changed by you one at a time and will stay as you set.` : '';
          preview.textContent = `This covers ${plural(p.matches, 'transaction')} you've already added, and any future ones that match.${eg}${mine}`;
        } catch (e) { preview.textContent = e.message; }
      }, 250);
    };
    [one, rule].forEach((r) => r.addEventListener('change', () => { patternBox.hidden = !rule.checked; refreshPreview(); }));
    patternInput.addEventListener('input', refreshPreview);

    const save = async (body) => {
      alerts.replaceChildren();
      try {
        const res = await api('POST', `/api/transactions/${t.id}/correction`, body);
        const n = res.changed;
        announce(n ? `Saved. ${plural(n, 'transaction')} updated.` : 'Saved. Nothing needed to change.');
        await loadTransactions();
        // the save rebuilt every row, so put focus back on the row that was being edited
        const back = $(`#txn-list li[data-txn="${CSS.escape(t.id)}"] button[data-change]`);
        if (back) back.focus(); else focusHeading($('#view-txns'));
      } catch (e) { alerts.replaceChildren(errorBox(e.message)); }
    };

    const buttons = h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn primary', text: 'Save', onclick: () => {
        const body = { scope: rule.checked ? 'rule' : 'one' };
        const cat = catInput.value.trim(), name = nameInput.value.trim();
        const type = typeSelect.value;
        const willBePurchase = type === 'PURCHASE';
        if (type !== t.economic_type) body.economic_type = type;
        if (willBePurchase && cat !== (t.category || '')) body.category = cat || null;
        if (name !== (t.merchant_name || '')) body.merchant_name = name || null;
        if (rule.checked) {
          body.pattern = patternInput.value;
          if (willBePurchase && cat && body.category === undefined) body.category = cat;
          if (name && body.merchant_name === undefined) body.merchant_name = name;
        }
        if (Object.keys(body).length === 1) { alerts.replaceChildren(errorBox('Nothing has changed yet.')); return; }
        save(body);
      } }),
      h('button', { type: 'button', class: 'btn quiet', text: 'Cancel', onclick: close }),
      (t.category_source === 'you' || t.merchant_source === 'you' || t.type_source === 'you')
        ? h('button', { type: 'button', class: 'btn quiet', text: 'Go back to automatic', onclick: () => save({
          scope: 'one', ...(t.category_source === 'you' ? { category: null } : {}), ...(t.merchant_source === 'you' ? { merchant_name: null } : {}),
          ...(t.type_source === 'you' ? { economic_type: null } : {}),
        }) })
        : null,
    );

    const catBox = h('p', {}, h('label', { class: 'field', for: `cat-${id}`, text: 'Category (purchases only)' }), catInput,
      h('datalist', { id: `cats-${id}` }, ...txnState.categories.map((c) => h('option', { value: c }))));
    const syncCat = () => { catBox.hidden = typeSelect.value !== 'PURCHASE'; };
    typeSelect.addEventListener('change', syncCat);
    syncCat();

    return h('div', {},
      h('div', { class: 'row' }, h('strong', { text: t.description }), h('span', { class: 'txn-amount', text: signedMoney(t) })),
      h('p', { class: 'muted', style: 'margin:.2rem 0 0', text: [niceDate(t.date), t.file].filter(Boolean).join(' · ') }),
      h('div', { class: 'editor' },
        alerts,
        h('p', {}, h('label', { class: 'field', for: `type-${id}`, text: 'What kind of transaction is this?' }), typeSelect),
        catBox,
        h('p', {}, h('label', { class: 'field', for: `name-${id}`, text: 'Merchant name (optional)' }), nameInput),
        h('fieldset', {}, h('legend', { text: 'Apply this to' }),
          h('label', { class: 'choice' }, one, 'Only this transaction'),
          h('label', { class: 'choice' }, rule, 'Every transaction from the same place, now and in future files'),
          patternBox, preview),
        buttons,
      ),
    );
  }

  function renderRules(rules) {
    const list = $('#rules-list');
    if (!rules.length) { list.replaceChildren(h('li', { class: 'muted', text: 'No rules yet. Choose “Every transaction from the same place” when you change one.' })); return; }
    list.replaceChildren(...rules.map((r) => {
      const sets = [r.type_label ? `kind “${r.type_label}”` : null, r.category ? `category ${r.category}` : null, r.merchant_name ? `merchant name “${r.merchant_name}”` : null].filter(Boolean).join(' and ');
      return h('li', {},
        h('p', { style: 'margin:0', text: `Descriptions containing “${r.pattern}” get ${sets}.` }),
        h('p', { class: 'muted', style: 'margin:.2rem 0 0', text: `Used for ${plural(r.applied_to, 'transaction')} right now.` }),
        h('div', { class: 'actions', style: 'margin-top:.5rem' }, confirmButton('Remove rule', 'Yes, remove it', async () => {
          try {
            const res = await api('DELETE', `/api/rules/${r.id}`);
            announce(`Rule removed. ${plural(res.changed, 'transaction')} went back to automatic.`);
          } catch (e) { announce(e.message); }
          loadTransactions();
        })),
      );
    }));
  }

  $('#wipe-run').addEventListener('click', async () => {
    const note = $('#wipe-note');
    const confirm = $('#wipe-confirm').value.trim();
    if (confirm !== 'DELETE EVERYTHING') { note.textContent = 'Type DELETE EVERYTHING exactly to confirm.'; return; }
    try {
      await api('DELETE', '/api/everything', { confirm });
      $('#wipe-confirm').value = '';
      note.textContent = 'Everything was deleted.';
      announce('Everything was deleted.');
      checkStatus(); loadImports();
    } catch (e) { note.textContent = e.message; }
  });

  $('#groq-run').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    $('#groq-note').textContent = 'Asking Groq…';
    try {
      const r = await api('POST', '/api/categorize/groq');
      const msg = r.asked ? `Groq answered for ${plural(r.answered, 'merchant')} of ${r.asked}; ${plural(r.changed, 'transaction')} updated.` : r.note;
      $('#groq-note').textContent = r.note && r.asked ? `${msg} ${r.note}` : msg;
      announce(msg);
      loadTransactions();
    } catch (err) { $('#groq-note').textContent = err.message; }
    btn.disabled = false;
  });

  $('#txn-filter').addEventListener('submit', (e) => { e.preventDefault(); loadTransactions(); });
  $('#txn-more').addEventListener('click', () => loadTransactions({ append: true }));

  // ------------------------------------------------------------------ ask a question

  const STATUS_WORDS = {
    VERIFIED: ['good', 'Checked against your statements'],
    VERIFIED_WITH_CAVEATS: ['warn', 'Checked, with notes'],
    INSUFFICIENT_INFORMATION: ['bad', 'Not enough information to answer'],
    ERROR: ['bad', 'Something went wrong'],
  };

  let hasChat = false;
  window.addEventListener('beforeunload', (e) => { if (hasChat) { e.preventDefault(); e.returnValue = ''; } });

  $('#examples').addEventListener('click', (e) => {
    const q = e.target.closest('button') && e.target.closest('button').dataset.q;
    if (q) { $('#question').value = q; $('#ask-form').requestSubmit(); }
  });

  $('#ask-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('#question');
    const question = input.value.trim();
    if (!question) { input.focus(); return; }
    const chat = $('#chat');
    hasChat = true;
    chat.append(h('p', { class: 'q', text: question }));
    const card = h('div', { class: 'a', text: 'Checking your statements…' });
    chat.append(card);
    input.value = '';
    $('#send').disabled = true;
    try {
      renderAnswer(card, await api('POST', '/api/ask', { question }));
    } catch (err) {
      renderAnswer(card, { status: 'ERROR', error: err.message });
    } finally {
      $('#send').disabled = !(ledgerReady && hasKey);
      input.focus();
    }
  });

  function renderAnswer(card, data) {
    const [cls, words] = STATUS_WORDS[data.status] || STATUS_WORDS.INSUFFICIENT_INFORMATION;
    const parts = [h('span', { class: `status ${cls}`, text: words })];
    if (data.error) {
      parts.push(h('p', { style: 'margin-top:.5rem', text: data.error }));
      card.replaceChildren(...parts);
      return;
    }
    parts.push(h('p', { class: 'answer-text', style: 'margin-top:.5rem', text: data.answer_text || '' }));
    if (data.chart_image) parts.push(h('img', { src: data.chart_image, alt: 'Chart for this answer' }));
    const dt = data.dashboard_table;
    if (dt && dt.rows && dt.rows.length) {
      parts.push(h('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Table for this answer', style: 'margin-top:.75rem' },
        h('table', {}, h('thead', {}, h('tr', {}, ...['Group', 'Rank', 'Merchant', 'Date', `Amount${dt.currency ? ` (${dt.currency})` : ''}`].map((t) => h('th', { text: t })))),
          h('tbody', {}, ...dt.rows.map((r) => h('tr', {}, ...[r.group, r.rank, r.merchant || '', r.date || '', r.amount].map((v) => h('td', { text: String(v) }))))))));
      if (dt.truncated) parts.push(h('p', { class: 'muted', text: `Showing ${dt.rows.length} of ${dt.total_rows} rows.` }));
    }
    if ((data.amounts || []).length) {
      parts.push(h('ul', {}, ...data.amounts.map((a) => h('li', { text: `${money(a.amount, a.currency)}${a.label ? ` — ${a.label}` : ''}` }))));
    }
    if ((data.caveats || []).length) {
      parts.push(h('h3', { text: 'Please note' }), h('ul', {}, ...data.caveats.map((c) => h('li', { text: c }))));
    }
    if ((data.sources || []).length) {
      parts.push(h('details', {}, h('summary', { text: `Why? Show the ${plural(data.sources.length, 'row')} this answer used` }),
        h('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Source rows' },
          h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Date' }), h('th', { text: 'Description' }), h('th', { class: 'num', text: 'Amount' }), h('th', { text: 'Where it came from' }))),
            h('tbody', {}, ...data.sources.map((s) => h('tr', {},
              h('td', { text: niceDate(s.date) }), h('td', { text: s.description }),
              h('td', { class: 'num', text: `${s.direction === 'CREDIT' ? '+' : '−'}${money(s.amount, s.currency)}` }),
              h('td', { text: `${s.file || ''}${s.page ? `, page ${s.page}` : ''}${s.row ? `, row ${s.row}` : ''}` }),
            )))))));
    }
    if ((data.trace || []).length) {
      parts.push(h('details', {}, h('summary', { text: 'Technical details' }),
        h('ol', {}, ...data.trace.map((t) => h('li', { class: 'evidence', text: `${t.tool}(${JSON.stringify(t.input)})` })))));
    }
    card.replaceChildren(...parts);
  }

  checkStatus();
})();
