/* FORGE local UI. Plain fetch + EventSource; no build step. Every action is one API call
   into the same service functions the CLI uses. */
(function () {
  'use strict';

  // ─── state ────────────────────────────────────────────────────────────────
  var S = {
    project: load('forge.project', { source_dir: '', output_dir: './migrated', config: '', decisions: {}, plan: [], completed: [], intent: '' }),
    packs: null,          // /api/packs
    discovery: null,      // last /api/discover result
    intent: null,         // last /api/intent result's plan
    job: null,            // job being followed on the Run page
    es: null,             // its EventSource
    review: null,         // last /api/review result
    pollTimer: null
  };

  function load(key, fallback) { try { var v = localStorage.getItem(key); return v ? Object.assign({}, fallback, JSON.parse(v)) : fallback; } catch (e) { return fallback; } }
  function save() { try { localStorage.setItem('forge.project', JSON.stringify(S.project)); } catch (e) { /* private window */ } }

  // ─── helpers ──────────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function tag(v, cls) { return '<span class="tag ' + esc(cls || v) + '">' + esc(v) + '</span>'; }
  function flash(msg) { var f = $('flash'); if (!msg) { f.hidden = true; return; } f.textContent = msg; f.hidden = false; window.scrollTo(0, 0); }
  function projectBody(extra) {
    var p = S.project;
    var b = { source_dir: p.source_dir, output_dir: p.output_dir || './migrated' };
    if (p.config) b.config = p.config;
    if (p.decisions && Object.keys(p.decisions).length) b.decisions = p.decisions;
    return Object.assign(b, extra || {});
  }
  function needProject() { if (!S.project.source_dir) { flash('Set the source directory on the Project step first.'); location.hash = '#/project'; return false; } return true; }
  function fmtErr(e) { return e && e.message ? e.message : String(e); }

  async function api(method, path, body) {
    var r = await fetch(path, { method: method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    var ct = r.headers.get('content-type') || '';
    var data = ct.indexOf('json') >= 0 ? await r.json() : await r.text();
    if (!r.ok) {
      var d = data && data.detail !== undefined ? data.detail : data;
      throw new Error(typeof d === 'string' ? d : JSON.stringify(d));
    }
    return data;
  }
  async function packs() { if (!S.packs) S.packs = await api('GET', '/api/packs'); return S.packs; }
  function fileUrl(name) { return '/api/files?output_dir=' + encodeURIComponent(S.project.output_dir || './migrated') + '&name=' + encodeURIComponent(name); }

  // Follow a job's SSE stream. The browser reconnects with Last-Event-ID on its own; we close on done/error.
  var EVENT_TYPES = ['start', 'skipped', 'file', 'snapshot', 'snapshot_skipped', 'queue', 'acceptance', 'acceptance_skipped',
    'cancelled', 'summary', 'nothing', 'apply_outcome', 'apply_done', 'testgen_start', 'testgen_unit',
    'testgen_cancelled', 'testgen_summary', 'done', 'error'];
  function follow(jobId, onEvent) {
    var es = new EventSource('/api/runs/' + jobId + '/events');
    EVENT_TYPES.forEach(function (t) {
      es.addEventListener(t, function (e) {
        var d = JSON.parse(e.data);
        onEvent(t, d);
        if (t === 'done' || t === 'error') es.close();
      });
    });
    return es;
  }

  // ─── top bar: active job + cwd ────────────────────────────────────────────
  async function health() {
    try {
      var h = await api('GET', '/api/health');
      $('cwd').textContent = h.cwd;
      var bar = $('jobbar');
      if (h.active) {
        bar.hidden = false; bar.className = 'jobbar';
        var where = { apply: 'review', testgen: 'tests' }[h.active.kind] || 'run';
        bar.innerHTML = tag(h.active.state, 'running') + ' ' + esc(h.active.kind) + ' ' + esc(h.active.params.phase || '') + ' · <a href="#/' + where + '">watch</a>';
        if (!S.pollTimer) S.pollTimer = setInterval(health, 3000);
      } else {
        bar.hidden = true;
        if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
      }
      return h;
    } catch (e) { $('jobbar').hidden = false; $('jobbar').className = 'jobbar err'; $('jobbar').textContent = 'server unreachable'; return null; }
  }

  // ─── steps ────────────────────────────────────────────────────────────────
  var steps = {};

  steps.project = {
    render: function () {
      $('p-source').value = S.project.source_dir; $('p-output').value = S.project.output_dir; $('p-config').value = S.project.config;
      $('project-form').onsubmit = async function (ev) {
        ev.preventDefault();
        S.project.source_dir = $('p-source').value.trim(); S.project.output_dir = $('p-output').value.trim() || './migrated'; S.project.config = $('p-config').value.trim();
        save(); $('p-status').textContent = 'checking…';
        try {
          var arts = await api('GET', '/api/artifacts?output_dir=' + encodeURIComponent(S.project.output_dir));
          var disc = await api('POST', '/api/discover', projectBody());   // cheap: no model, validates the source dir
          S.discovery = disc; S.project.decisions = Object.assign({}, disc.decisions, S.project.decisions); S.project.plan = disc.order.slice(); save();
          $('p-status').textContent = 'saved';
          var box = $('p-summary'); box.hidden = false;
          box.innerHTML = '<b>' + esc(disc.profile.build_system || 'build: ?') + '</b> · Java ' + esc(disc.profile.java_version || '?') + ' · ' + disc.activations.length + ' pack(s) apply · '
            + (arts.exists ? arts.artifacts.length + ' artifact(s) already in ' + esc(S.project.output_dir) : 'output directory not created yet')
            + '<div class="row"><a class="button primary" href="#/intent">Next: Intent →</a>'
            + '<a class="button" href="#/discover">or skip to Discover</a></div>';
        } catch (e) { $('p-status').textContent = ''; flash(fmtErr(e)); }
      };
    }
  };

  steps.intent = {
    render: function () {
      if (!needProject()) return;
      $('i-text').value = S.project.intent || '';
      $('intent-form').onsubmit = steps.intent.resolve;
      if (S.intent) steps.intent.show(S.intent);
    },
    resolve: async function (ev) {
      ev.preventDefault(); flash('');
      var text = $('i-text').value.trim();
      if (!text) { flash('Say what you want migrated first.'); return; }
      // Deliberately without `decisions`: a second resolve must start from the
      // defaults and agents.yaml, or last run's answers come back attributed to
      // the config instead of the prompt.
      var body = projectBody({ intent: text }); delete body.decisions;
      $('i-run').disabled = true; $('i-status').textContent = 'asking…';
      try {
        var d = await api('POST', '/api/intent', body);
        S.intent = d; S.discovery = d;
        S.project.intent = text;
        S.project.plan = d.order.slice();
        S.project.decisions = Object.assign({}, d.decisions);
        save();
        steps.intent.show(d);
        $('i-status').textContent = 'plan written to ' + d.paths.yaml;
      } catch (e) { flash(fmtErr(e)); $('i-status').textContent = ''; }
      $('i-run').disabled = false;
    },
    show: function (d) {
      var p = d.intent || {};
      $('i-result').hidden = false;

      $('i-totals').innerHTML = [
        ['<b>' + p.packs.length + '</b><span>selected</span>'],
        ['<b>' + p.excluded.length + '</b><span>set aside</span>'],
        ['<b>' + p.assumptions.length + '</b><span>assumed</span>'],
        ['<b>$' + Number(p.cost_usd || 0).toFixed(4) + '</b><span>this call</span>']
      ].map(function (x) { return '<div>' + x + '</div>'; }).join('');

      var rows = p.packs.map(function (id, i) {
        var state = (p.states || {})[id] || 'runnable';
        return '<tr><td class="mono">' + (i + 1) + '</td><td class="mono">' + esc(id) + '</td><td>'
          + tag(state, state === 'detect-only' ? 'skip' : state) + '</td><td></td></tr>';
      });
      rows = rows.concat(p.excluded.map(function (e) {
        return '<tr class="out"><td></td><td class="mono">' + esc(e.pack) + '</td><td>' + tag('set aside', 'skip')
          + '</td><td class="hint">' + esc(e.reason) + '</td></tr>';
      }));
      $('i-packs').innerHTML = '<tr><th></th><th>Pack</th><th>State</th><th>Why not</th></tr>'
        + (rows.join('') || '<tr><td colspan="4">Nothing applies to this repository.</td></tr>');

      $('i-decisions').innerHTML = '<tr><th>Decision</th><th>Value</th><th>From</th></tr>'
        + Object.keys(p.decisions).map(function (k) {
          var src = p.provenance[k] || 'default';
          return '<tr><td class="mono">' + esc(k) + '</td><td class="mono">' + esc(p.decisions[k]) + '</td><td>'
            + tag(src, src === 'prompt' ? 'runnable' : 'skip') + '</td></tr>';
        }).join('');

      function block(title, items, cls) {
        if (!items || !items.length) return '';
        return '<div class="card ' + (cls || '') + '"><h3>' + esc(title) + '</h3><ul>'
          + items.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul></div>';
      }
      var gaps = Object.keys(p.gaps || {}).map(function (k) { return k + ' expects ' + p.gaps[k].join(', '); });
      var notes = block('Assumed — not stated in your request', p.assumptions)
        + block('Worth confirming', p.questions)
        + block('Asked for, but not available here', (p.unsupported || []).map(function (u) { return u.asked + ' — ' + u.reason; }))
        + block('Ignored from the answer', (p.rejected || []).map(function (r) { return r.key + '=' + r.value + ' — ' + r.reason; }))
        + block('Ordering gaps', gaps);
      $('i-notes').innerHTML = notes || '<p class="hint">Nothing to flag — the request settled every decision the '
        + 'selected packs read.</p>';
    }
  };

  steps.discover = {
    render: async function () {
      if (!needProject()) return;
      $('d-run').onclick = steps.discover.run;
      if (S.discovery) steps.discover.show(S.discovery);
    },
    run: async function () {
      $('d-status').textContent = 'profiling…'; $('d-run').disabled = true;
      try { S.discovery = await api('POST', '/api/discover', projectBody()); S.project.plan = S.discovery.order.slice(); save(); steps.discover.show(S.discovery); $('d-status').textContent = 'profile written to ' + S.discovery.paths.yaml; }
      catch (e) { flash(fmtErr(e)); $('d-status').textContent = ''; }
      $('d-run').disabled = false;
    },
    show: async function (d) {
      var pk = await packs();
      $('d-result').hidden = false;
      var byId = {}; pk.packs.forEach(function (p) { byId[p.id] = p; });
      var rows = d.activations.map(function (a) {
        var state = a.runnable ? 'runnable' : (a.complete ? 'blocked' : 'detect-only');
        return '<tr><td class="mono">' + esc(a.pack) + '</td><td>' + tag(state, state === 'detect-only' ? 'skip' : state) + '</td><td><ul>' + (a.evidence || []).map(function (e) { return '<li>' + esc(e) + '</li>'; }).join('') + '</ul></td></tr>';
      });
      $('d-packs').innerHTML = '<tr><th>Pack</th><th>State</th><th>Evidence</th></tr>' + (rows.join('') || '<tr><td colspan="3">Nothing recognised.</td></tr>');
      $('d-order').innerHTML = d.order.map(function (id) {
        var a = d.activations.filter(function (x) { return x.pack === id; })[0] || {};
        return '<li class="' + (a.runnable ? '' : (a.complete ? 'blocked' : 'detect')) + '" title="' + esc((byId[id] || {}).title || '') + '">' + esc(id) + '</li>';
      }).join('');
      $('d-summary').textContent = d.summary;
      var opts = pk.decision_options, cur = Object.assign({}, d.decisions, S.project.decisions);
      $('d-decisions').innerHTML = Object.keys(opts).map(function (k) {
        return '<label>' + esc(k) + ' <select data-decision="' + esc(k) + '">' + opts[k].map(function (v) { return '<option' + (cur[k] === v ? ' selected' : '') + '>' + esc(v) + '</option>'; }).join('') + '</select></label>';
      }).join('');
      $('d-decisions').onchange = function (ev) { var k = ev.target.getAttribute('data-decision'); if (k) { S.project.decisions[k] = ev.target.value; save(); } };
    }
  };

  steps.run = {
    render: async function () {
      if (!needProject()) return;
      var pk = await packs();
      var sel = $('r-phase'), plan = (S.project.plan || []).filter(function (p) { return pk.runnable.indexOf(p) >= 0; });
      var next = plan.filter(function (p) { return (S.project.completed || []).indexOf(p) < 0; })[0];
      sel.innerHTML = pk.runnable.map(function (id) {
        var inPlan = plan.indexOf(id) >= 0, done = (S.project.completed || []).indexOf(id) >= 0;
        return '<option value="' + esc(id) + '"' + (id === next ? ' selected' : '') + '>' + esc(id) + (inPlan ? (done ? '  ✓ done' : '  · in plan') : '') + '</option>';
      }).join('');
      steps.run.plan(plan, next);
      $('run-form').onsubmit = steps.run.start;
      $('r-cancel').onclick = steps.run.cancel;
      steps.run.history();
      var h = await health();
      if (h && h.active && h.active.kind === 'run' && (!S.job || S.job.id !== h.active.id)) steps.run.attach(h.active.id, h.active.params.phase);
    },
    plan: function (plan, next) {
      // The whole queue, with the one you are on marked. Clicking a row selects
      // it rather than starting it — nothing in this UI spends money on one click.
      var wrap = $('r-plan-wrap');
      if (!plan.length) { wrap.hidden = true; return; }
      wrap.hidden = false;
      var done = S.project.completed || [];
      $('r-plan').innerHTML = plan.map(function (id) {
        var isDone = done.indexOf(id) >= 0, isNext = id === next;
        var mark = isDone ? '✓' : (isNext ? '▶' : '');
        return '<li class="' + (isDone ? 'done' : (isNext ? 'next' : '')) + '" data-pack="' + esc(id) + '">'
          + '<span class="mark">' + mark + '</span><span class="mono">' + esc(id) + '</span>'
          + '<span class="hint">' + (isDone ? 'done' : (isNext ? 'next' : '')) + '</span></li>';
      }).join('');
      $('r-plan').onclick = function (ev) {
        var li = ev.target.closest('li[data-pack]');
        if (!li) return;
        $('r-phase').value = li.getAttribute('data-pack');
        $('r-status').textContent = 'selected ' + li.getAttribute('data-pack') + ' — press Start when ready';
      };
    },
    start: async function (ev) {
      ev.preventDefault(); flash('');
      var body = projectBody({ phase: $('r-phase').value, dry_run: $('r-dry').checked, acceptance: $('r-acc').checked, acceptance_build: $('r-accbuild').checked, no_metrics: $('r-nometrics').checked, generate_tests: $('r-tests').checked, run_tests: $('r-runtests').checked });
      $('r-start').disabled = true; $('r-status').textContent = 'starting…';
      try { var r = await api('POST', '/api/runs', body); steps.run.attach(r.job_id, body.phase); }
      catch (e) { flash(fmtErr(e)); $('r-start').disabled = false; $('r-status').textContent = ''; }
    },
    attach: function (jobId, phase) {
      if (S.es) S.es.close();
      S.job = { id: jobId, phase: phase, total: 0, files: 0 };
      $('r-live').hidden = false; $('r-files').innerHTML = ''; $('r-log').textContent = ''; $('r-done').hidden = true; $('r-bar').style.width = '0';
      $('r-head').textContent = 'job ' + jobId + ' · ' + phase; $('r-start').disabled = true; $('r-cancel').hidden = false; $('r-status').textContent = 'running';
      health();
      S.es = follow(jobId, function (t, d) {
        $('r-log').textContent += t + ' ' + JSON.stringify(d) + '\n';
        if (t === 'start') { S.job.total = d.total; $('r-head').innerHTML = 'job ' + esc(jobId) + ' · ' + esc(d.phase) + ' · ' + d.files + ' file(s)' + (d.generated ? ' + ' + d.generated + ' generated' : '') + (d.dry_run ? ' · <b>dry run</b>' : ''); }
        else if (t === 'skipped') $('r-head').innerHTML += ' · ' + d.count + ' outside scope prefix';
        else if (t === 'file') {
          S.job.files = d.index; $('r-bar').style.width = Math.round(100 * d.index / Math.max(1, d.total)) + '%';
          $('r-files').insertAdjacentHTML('beforeend', '<li><span class="idx">' + d.index + '/' + d.total + '</span><span class="lbl" title="' + esc(d.file) + '">' + esc(d.label) + '</span>' + tag(d.status) + '<span class="hint">' + (d.score != null ? 'score ' + d.score : '') + '</span>' + (d.risk_tier ? tag(d.risk_tier) : '<span></span>') + '</li>');
        }
        else if (t === 'testgen_start') $('r-files').insertAdjacentHTML('beforeend', '<li><span class="idx">tests</span><span class="lbl">' + d.targets + ' class(es) to write tests for, ' + d.skipped + ' skipped</span><span></span><span></span><span></span></li>');
        else if (t === 'testgen_unit') $('r-files').insertAdjacentHTML('beforeend', '<li><span class="idx">' + d.index + '/' + d.total + '</span><span class="lbl" title="' + esc(d.test || '') + '">' + esc(d.label) + '</span>' + tag(d.status) + '<span class="hint">' + (d.score != null ? 'score ' + d.score : '') + '</span><span></span></li>');
        else if (t === 'cancelled') $('r-status').textContent = 'cancelled after ' + d.done + ' of ' + d.total;
        else if (t === 'nothing') { $('r-done').hidden = false; $('r-done').textContent = d.message; }
        else if (t === 'done' || t === 'error') steps.run.finish(t, d);
      });
    },
    finish: function (t, d) {
      $('r-start').disabled = false; $('r-cancel').hidden = true; $('r-bar').style.width = '100%'; health(); steps.run.history();
      var box = $('r-done'); box.hidden = false;
      if (t === 'error') { $('r-status').textContent = 'failed'; box.innerHTML = '<b>Failed:</b> ' + esc(d.error) + '<pre>' + esc(d.traceback) + '</pre>'; return; }
      $('r-status').textContent = d.state;
      var r = d.result; if (!r) return;
      var tt = r.totals;
      if (d.state === 'done' && !r.dry_run && S.project.completed.indexOf(r.phase) < 0) {
        S.project.completed.push(r.phase); save();
        // Advance the queue so the next pack is marked before you look away.
        packs().then(function (pk) {
          var plan = (S.project.plan || []).filter(function (p) { return pk.runnable.indexOf(p) >= 0; });
          steps.run.plan(plan, plan.filter(function (p) { return S.project.completed.indexOf(p) < 0; })[0]);
        });
      }
      var acc = r.acceptance ? (r.acceptance.verdict ? ' · acceptance ' + tag(r.acceptance.verdict) : ' · acceptance skipped: ' + esc(r.acceptance.skipped_reason)) : '';
      box.innerHTML = '<div class="totals">'
        + ['passed', 'held', 'manual', 'blocked'].map(function (k) { return '<div><b>' + tt[k] + '</b><span>' + k + '</span></div>'; }).join('')
        + '<div><b>' + tt.bedrock_calls + '</b><span>Bedrock calls</span></div><div><b>$' + Number(tt.cost_usd).toFixed(3) + '</b><span>est. cost</span></div></div>'
        + '<div class="row">' + (r.queue_count ? '<a class="button primary" href="#/review">Review ' + r.queue_count + ' file(s) →</a>' : '<span class="tag DONE">nothing needs review</span>')
        + ' <a class="button" target="_blank" rel="noopener" href="' + fileUrl('migration-report.md') + '">report</a>'
        + (r.paths.page ? ' <a class="button" target="_blank" rel="noopener" href="' + fileUrl('migration-review.html') + '">static review page</a>' : '')
        + (r.acceptance && r.acceptance.verdict ? ' <a class="button" href="#/accept">acceptance detail</a>' : '')
        + (r.testgen ? ' <a class="button" href="#/tests">tests: ' + r.testgen.totals.generated + ' written, ' + r.testgen.totals.held + ' held</a>' : '') + acc + '</div>';
    },
    cancel: async function () { if (!S.job) return; try { await api('POST', '/api/runs/' + S.job.id + '/cancel'); $('r-cancel').disabled = true; $('r-status').textContent = 'cancelling after the current file…'; } catch (e) { flash(fmtErr(e)); } },
    history: async function () {
      try {
        var j = (await api('GET', '/api/jobs')).jobs;
        $('r-history').innerHTML = '<tr><th>Job</th><th>Kind</th><th>Pack</th><th>State</th><th>Started</th><th>Result</th></tr>' + j.map(function (x) {
          var r = x.result && x.result.totals ? x.result.totals.passed + ' passed · ' + x.result.totals.held + ' held' : (x.error || (x.result && x.result.all_applied !== undefined ? x.result.outcomes.length + ' decision(s)' : ''));
          return '<tr><td class="mono">' + esc(x.id) + '</td><td>' + esc(x.kind) + '</td><td class="mono">' + esc(x.params.phase || '') + '</td><td>' + tag(x.state) + '</td><td class="mono">' + esc(x.started_at || '') + '</td><td>' + esc(r) + '</td></tr>';
        }).join('');
      } catch (e) { /* history is a nicety */ }
    }
  };

  steps.review = {
    render: async function () {
      if (!needProject()) return;
      $('v-reload').onclick = steps.review.load; $('v-apply').onclick = steps.review.apply;
      var h = await health();
      if (h && h.active && h.active.kind === 'apply') steps.review.watch(h.active.id);
      else steps.review.load();
    },
    load: async function () {
      $('v-status').textContent = 'loading…'; $('v-outcomes').hidden = true;
      try {
        var v = S.review = await api('GET', '/api/review?output_dir=' + encodeURIComponent(S.project.output_dir));
        $('v-status').textContent = ''; $('v-static').hidden = false; $('v-static').href = fileUrl('migration-review.html');
        var meta = $('v-meta'); meta.hidden = false;
        meta.innerHTML = '<b>' + esc(v.phase) + '</b> · run ' + esc(v.run) + (v.dry_run ? ' · <b>dry run</b> — nothing was written; approve writes from the transformed text' : '') + ' · ' + v.count + ' file(s): '
          + Object.keys(v.by_status).map(function (k) { return tag(v.by_status[k] + ' ' + k, k); }).join(' ');
        var frame = $('v-frame'); frame.hidden = false;
        frame.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><style>' + v.css + 'body{background:#f4f4f2;padding:12px}</style></head><body>' + v.entries_html + '</body></html>';
        frame.onload = function () {
          var doc = frame.contentDocument;
          var fit = function () { frame.style.height = (doc.documentElement.scrollHeight + 20) + 'px'; };
          fit(); doc.addEventListener('toggle', fit, true); doc.addEventListener('change', function () { fit(); steps.review.count(); });
          if (window.ResizeObserver) new ResizeObserver(fit).observe(doc.body);
          steps.review.count();
        };
        $('v-footer').hidden = v.count === 0;
        if (v.count === 0) { frame.hidden = true; }
      } catch (e) { $('v-status').textContent = fmtErr(e); $('v-meta').hidden = true; $('v-frame').hidden = true; $('v-footer').hidden = true; }
    },
    // Same DOM contract as the static page's collect(): fieldset.decision[data-file][data-pack] + radios + note + rule.
    collect: function () {
      var doc = $('v-frame').contentDocument, out = [];
      if (!doc) return out;
      var fs = doc.querySelectorAll('fieldset.decision');
      for (var i = 0; i < fs.length; i++) {
        var f = fs[i], sel = f.querySelector('input[type=radio]:checked');
        if (!sel || sel.value === 'skip') continue;
        var d = { file: f.getAttribute('data-file'), pack: f.getAttribute('data-pack'), decision: sel.value, note: f.querySelector('textarea.note').value.trim() };
        var rule = f.querySelector('input.rule').value.trim(); if (rule) d.rule = rule;
        out.push(d);
      }
      return out;
    },
    count: function () { var n = steps.review.collect().length; $('v-apply').textContent = 'Apply ' + n + ' decision' + (n === 1 ? '' : 's'); $('v-apply').disabled = n === 0; },
    apply: async function () {
      var decisions = steps.review.collect(); if (!decisions.length) return;
      var body = { source_dir: S.project.source_dir, output_dir: S.project.output_dir, decisions: decisions, run: S.review ? S.review.run : '', dry_run: $('v-dry').checked, phase: S.review ? S.review.phase : null };
      if (S.project.config) body.config = S.project.config;
      if (S.project.decisions && Object.keys(S.project.decisions).length) body.decision_overrides = S.project.decisions;
      $('v-apply').disabled = true; $('v-apply-status').textContent = 'applying…';
      try { var r = await api('POST', '/api/review/decisions', body); steps.review.watch(r.job_id); }
      catch (e) { flash(fmtErr(e)); $('v-apply-status').textContent = ''; steps.review.count(); }
    },
    watch: function (jobId) {
      var t = $('v-outcomes'); t.hidden = false; t.innerHTML = '<tr><th>File</th><th>Decision</th><th>Applied</th><th>Status after</th><th>Detail</th></tr>';
      $('v-apply-status').textContent = 'applying (job ' + jobId + ')…'; $('v-footer').hidden = false; $('v-apply').disabled = true;
      follow(jobId, function (ty, d) {
        if (ty === 'apply_outcome') t.insertAdjacentHTML('beforeend', '<tr><td class="mono">' + esc(d.file) + '</td><td>' + esc(d.decision) + '</td><td>' + (d.applied ? 'yes' : 'no') + '</td><td>' + tag(d.status_after) + '</td><td>' + esc(d.detail) + '</td></tr>');
        else if (ty === 'file') t.insertAdjacentHTML('beforeend', '<tr><td class="mono">' + esc(d.label) + '</td><td>retry</td><td>…</td><td>' + tag(d.status) + '</td><td>re-ran' + (d.score != null ? ', score ' + d.score : '') + '</td></tr>');
        else if (ty === 'done') { $('v-apply-status').textContent = d.result ? (d.result.remaining + ' still awaiting review' + (d.result.log_path ? ' · logged to ' + d.result.log_path : '')) : 'done'; health(); steps.review.load(); }
        else if (ty === 'error') { $('v-apply-status').textContent = ''; flash('Apply failed: ' + d.error); steps.review.count(); health(); }
      });
    }
  };

  steps.accept = {
    render: async function () {
      if (!needProject()) return;
      var pk = await packs(), sel = $('a-phase');
      var last = S.project.completed.length ? S.project.completed[S.project.completed.length - 1] : null;
      sel.innerHTML = pk.phases.map(function (id) { return '<option' + (id === last ? ' selected' : '') + '>' + esc(id) + '</option>'; }).join('');
      $('acc-form').onsubmit = async function (ev) {
        ev.preventDefault(); $('a-status').textContent = $('a-build').checked ? 'running checks and the build — this can take minutes…' : 'running checks…';
        try {
          var r = await api('POST', '/api/acceptance', projectBody({ phase: sel.value, run_build: $('a-build').checked }));
          $('a-status').textContent = r.path ? 'record: ' + r.path : (r.skipped_reason || '');
          $('a-result').hidden = false;
          $('a-verdict').innerHTML = r.verdict ? tag(r.verdict) + ' ' + r.passed + ' passed · ' + r.failed + ' failed · ' + r.skipped + ' skipped' : 'Skipped — ' + esc(r.skipped_reason);
          $('a-table').innerHTML = '<tr><th>Outcome</th><th>Check</th><th>Value</th><th>Scope</th><th>Detail</th></tr>' + (r.results || []).map(function (c) {
            var ev = c.evidence && c.evidence.length ? '<details><summary>' + c.evidence.length + ' evidence line(s)</summary><pre>' + esc(c.evidence.join('\n')) + '</pre></details>' : '';
            return '<tr><td>' + tag(c.outcome.toUpperCase()) + '</td><td class="mono">' + esc(c.kind) + '</td><td class="mono">' + esc(c.value) + '</td><td class="mono">' + esc(c.scope) + '</td><td>' + esc(c.detail) + ev + '</td></tr>';
          }).join('');
        } catch (e) { $('a-status').textContent = ''; flash(fmtErr(e)); }
      };
    }
  };

  steps.tests = {
    render: async function () {
      if (!needProject()) return;
      $('tg-form').onsubmit = steps.tests.start;
      $('tg-cancel').onclick = steps.tests.cancel;
      var h = await health();
      if (h && h.active && h.active.kind === 'testgen') { steps.tests.attach(h.active.id); return; }
      steps.tests.load();
    },
    load: async function () {
      try {
        var record = await api('GET', '/api/testgen?output_dir=' + encodeURIComponent(S.project.output_dir));
        steps.tests.show(record.totals, record.units, record.dependencies, record.skipped, record.dry_run);
        $('tg-status').textContent = 'last generated ' + record.run;
      } catch (e) { $('tg-status').textContent = 'No tests generated yet for this output directory.'; }
    },
    start: async function (ev) {
      ev.preventDefault(); flash('');
      var body = projectBody({ dry_run: $('tg-dry').checked, run_tests: $('tg-run').checked });
      $('tg-start').disabled = true; $('tg-status').textContent = 'starting…';
      try { var r = await api('POST', '/api/testgen', body); steps.tests.attach(r.job_id); }
      catch (e) { flash(fmtErr(e)); $('tg-start').disabled = false; $('tg-status').textContent = ''; }
    },
    attach: function (jobId) {
      if (S.es) S.es.close();
      S.job = { id: jobId, kind: 'testgen' };
      $('tg-live').hidden = false; $('tg-units').innerHTML = ''; $('tg-done').hidden = true; $('tg-table').hidden = true;
      $('tg-bar').style.width = '0'; $('tg-start').disabled = true; $('tg-cancel').hidden = false;
      $('tg-status').textContent = 'generating (job ' + jobId + ')…';
      health();
      S.es = follow(jobId, function (t, d) {
        if (t === 'testgen_start') $('tg-status').textContent = d.targets + ' class(es), ' + d.skipped + ' skipped' + (d.run_tests ? ' · running each test' : '');
        else if (t === 'testgen_unit') {
          $('tg-bar').style.width = Math.round(100 * d.index / Math.max(1, d.total)) + '%';
          $('tg-units').insertAdjacentHTML('beforeend', '<li><span class="idx">' + d.index + '/' + d.total + '</span><span class="lbl" title="' + esc(d.file) + '">' + esc(d.label) + '</span>' + tag(d.status) + '<span class="hint">' + (d.score != null ? 'score ' + d.score : '') + (d.reason ? ' · ' + esc(d.reason) : '') + '</span>' + (d.test_verdict && d.test_verdict !== 'SKIPPED' ? tag(d.test_verdict) : '<span></span>') + '</li>');
        }
        else if (t === 'testgen_cancelled') $('tg-status').textContent = 'cancelled after ' + d.done + ' of ' + d.total;
        else if (t === 'done' || t === 'error') steps.tests.finish(t, d);
      });
    },
    finish: function (t, d) {
      $('tg-start').disabled = false; $('tg-cancel').hidden = true; $('tg-bar').style.width = '100%'; health();
      if (t === 'error') { $('tg-status').textContent = 'failed'; $('tg-done').hidden = false; $('tg-done').innerHTML = '<b>Failed:</b> ' + esc(d.error) + '<pre>' + esc(d.traceback) + '</pre>'; return; }
      $('tg-status').textContent = d.state;
      var r = d.result; if (!r) return;
      steps.tests.show(r.totals, r.units, r.dependencies, null, r.dry_run);
    },
    show: function (totals, units, dependencies, skipped, dryRun) {
      var box = $('tg-done'); box.hidden = false;
      box.innerHTML = '<div class="totals">'
        + ['generated', 'held', 'blocked'].map(function (k) { return '<div><b>' + (totals[k] || 0) + '</b><span>' + k + '</span></div>'; }).join('')
        + '<div><b>' + (totals.tests_passed || 0) + '</b><span>tests passed</span></div>'
        + '<div><b>' + (totals.tests_failed || 0) + '</b><span>tests failed</span></div>'
        + '<div><b>$' + Number(totals.cost_usd || 0).toFixed(3) + '</b><span>est. cost</span></div></div>'
        + (dryRun ? '<p class="hint">Dry run — nothing was written.</p>' : '')
        + ((dependencies && dependencies.length) ? '<p>Add at test scope: ' + dependencies.map(function (x) { return '<code>' + esc(x) + '</code>'; }).join(', ') + '</p>' : '')
        + '<div class="row"><a class="button" target="_blank" rel="noopener" href="' + fileUrl('test-generation-report.md') + '">test report</a>'
        + ' <a class="button" target="_blank" rel="noopener" href="' + fileUrl('generated-tests.json') + '">record</a></div>';

      var t = $('tg-table'); t.hidden = false;
      t.innerHTML = '<tr><th>Class</th><th>Kind</th><th>Status</th><th>Score</th><th>Retries</th><th>Test</th><th>Test file</th><th>Why held</th></tr>'
        + (units || []).map(function (u) {
          return '<tr><td class="mono">' + esc(u.rel_path) + '</td><td>' + esc(u.kind) + '</td><td>' + tag(u.status) + '</td><td>' + (u.score != null ? u.score : '—') + '</td><td>' + (u.retry_count || 0) + '</td><td>' + esc(u.test_verdict || '—') + '</td><td class="mono">' + esc(u.test_rel_path) + '</td><td>' + esc(u.hold_reason || '') + '</td></tr>';
        }).join('');

      var wrap = $('tg-skipped-wrap');
      if (skipped && skipped.length) {
        wrap.hidden = false;
        $('tg-skipped').innerHTML = '<tr><th>Class</th><th>Reason</th></tr>' + skipped.map(function (row) {
          return '<tr><td class="mono">' + esc(row.rel_path) + '</td><td>' + esc(row.reason) + '</td></tr>';
        }).join('');
      } else { wrap.hidden = true; }
    },
    cancel: async function () {
      if (!S.job) return;
      try { await api('POST', '/api/runs/' + S.job.id + '/cancel'); $('tg-cancel').disabled = true; $('tg-status').textContent = 'cancelling after the current class…'; }
      catch (e) { flash(fmtErr(e)); }
    }
  };

  steps.feedback = {
    render: function () { if (!needProject()) return; $('f-reload').onclick = steps.feedback.load; steps.feedback.load(); },
    load: async function () {
      $('f-status').textContent = 'loading…';
      try {
        var r = await api('GET', '/api/feedback?output_dir=' + encodeURIComponent(S.project.output_dir));
        $('f-status').innerHTML = r.notes + ' note(s) across ' + r.packs.length + ' pack(s) · <a target="_blank" rel="noopener" href="' + fileUrl('pack-feedback.md') + '">pack-feedback.md</a>';
        var packsOut = Object.keys(r.groups);
        $('f-result').innerHTML = packsOut.length ? packsOut.map(function (p) {
          var g = r.groups[p];
          return '<h2>' + esc(p) + ' <span class="hint">edit: <code>' + esc(g.edit) + '</code></span></h2><table class="tbl"><tr><th>Rule</th><th>Count</th><th>Decisions</th><th>Files</th><th>Examples</th></tr>'
            + g.rows.map(function (row) {
              return '<tr><td>' + esc(row.label) + '</td><td>' + row.count + '</td><td>' + Object.keys(row.decisions).map(function (k) { return esc(k) + ' ' + row.decisions[k]; }).join(', ') + '</td><td class="mono">' + row.files.map(esc).join('<br>') + '</td><td>' + row.examples.map(function (x) { return '<div>' + esc(x) + '</div>'; }).join('') + '</td></tr>';
            }).join('') + '</table>';
        }).join('') : '<p class="hint">No reviewer notes yet. Notes come from retry and reject decisions on the Review step.</p>';
      } catch (e) { $('f-status').textContent = fmtErr(e); }
    }
  };

  steps.artifacts = {
    render: function () { if (!needProject()) return; $('x-reload').onclick = steps.artifacts.load; steps.artifacts.load(); },
    load: async function () {
      try {
        var r = await api('GET', '/api/artifacts?output_dir=' + encodeURIComponent(S.project.output_dir));
        $('x-status').textContent = r.exists ? r.output_dir : 'no output directory yet at ' + r.output_dir;
        $('x-table').innerHTML = '<tr><th>Artifact</th><th>File</th><th>Size</th><th>Modified</th></tr>' + r.artifacts.map(function (a) {
          return '<tr><td>' + esc(a.label) + '</td><td class="mono"><a target="_blank" rel="noopener" href="' + esc(a.url) + '">' + esc(a.name) + '</a></td><td class="mono">' + a.size + ' B</td><td class="mono">' + new Date(a.modified * 1000).toLocaleString() + '</td></tr>';
        }).join('') || '<tr><td colspan="4" class="hint">Nothing yet.</td></tr>';
      } catch (e) { $('x-status').textContent = fmtErr(e); }
    }
  };

  // ─── router ───────────────────────────────────────────────────────────────
  function route() {
    var name = (location.hash.replace(/^#\/?/, '') || 'project').split('?')[0];
    if (!steps[name]) name = 'project';
    flash('');
    document.querySelectorAll('.step').forEach(function (s) { s.hidden = s.id !== 'step-' + name; });
    document.querySelectorAll('#nav a').forEach(function (a) { a.classList.toggle('active', a.getAttribute('data-step') === name); });
    Promise.resolve(steps[name].render()).catch(function (e) { flash(fmtErr(e)); });
  }
  window.addEventListener('hashchange', route);
  health().then(route);
})();
