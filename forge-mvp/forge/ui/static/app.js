/* FORGE local UI — the helpers the chat surface is built on.

   This file used to be the whole product: nine wizard steps and a hash router.
   The owner looked at that page and asked "what does each step do, do I really
   need all these? I asked to change it with a prompt instead of this project
   setup" — and then chose to delete them. So the conversation is the page, the
   leader asks for the repository folder itself, and what is left here is plain
   fetch + EventSource with no build step: state, escaping, the job stream and
   the top-of-rail health poll. Everything visible lives in chat.js.

   Nothing here may render a hash-route link. There is no router to receive one. */
(function () {
  'use strict';

  // ─── state ────────────────────────────────────────────────────────────────
  // The chat's memory of one project. The leader binds it (set_project) and the
  // browser only keeps it so it can reach the output directory afterwards — the
  // review queue behind a card, an artifact download. There is no `decisions`
  // key here on purpose: R9 says the hold gate is not the model's to lower, and
  // the surest way to keep chat.js from writing one is to have nowhere to put it.
  var S = {
    project: load('forge.project', { source_dir: '', output_dir: '' }),
    pollTimer: null
  };

  function load(key, fallback) { try { var v = localStorage.getItem(key); return v ? Object.assign({}, fallback, JSON.parse(v)) : fallback; } catch (e) { return fallback; } }
  function save() { try { localStorage.setItem('forge.project', JSON.stringify(S.project)); } catch (e) { /* private window */ } }

  // ─── helpers ──────────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function tag(v, cls) { return '<span class="tag ' + esc(cls || v) + '">' + esc(v) + '</span>'; }
  function flash(msg) { var f = $('flash'); if (!msg) { f.hidden = true; return; } f.textContent = msg; f.hidden = false; }
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

  // A filesystem path, written so it can be truncated from the LEFT: chat.css
  // gives these elements `direction:rtl` to move the ellipsis to the start, and
  // an RTL line would otherwise fling a leading "/" to the far end. The path
  // goes in an isolated dir="ltr" span, as text — never markup, these are
  // repository paths. It lives here because both files write such elements:
  // health() writes the server's cwd into #cwd and chat.js writes the bound
  // repository into the same line, the tool log and the progress row.
  function setPath(el, value) {
    if (!el) return;
    while (el.firstChild) el.removeChild(el.firstChild);
    var s = document.createElement('span');
    s.setAttribute('dir', 'ltr');
    s.textContent = String(value == null ? '' : value);
    el.appendChild(s);
    el.title = s.textContent;
  }

  // Downloads out of the output directory the leader bound. Relative to that
  // directory only — /api/files refuses anything that escapes it. No bound
  // directory, no link: the chat's output lives inside the repository
  // (<repo>/.migrated), so there is no default to guess at any more.
  function fileUrl(name) {
    var out = S.project.output_dir || '';
    return out ? '/api/files?output_dir=' + encodeURIComponent(out) + '&name=' + encodeURIComponent(name) : '';
  }

  // Follow a job's SSE stream. The browser reconnects with Last-Event-ID on its own; we close on done/error.
  var EVENT_TYPES = ['start', 'skipped', 'file', 'snapshot', 'snapshot_skipped', 'context_missing', 'chained',
    'queue', 'acceptance', 'acceptance_skipped',
    'cancelled', 'summary', 'nothing', 'apply_outcome', 'apply_done', 'testgen_start', 'testgen_unit',
    'testgen_cancelled', 'testgen_summary',
    // A chat turn is a job in the same registry and streams down the same route.
    'turn_start', 'assistant_delta', 'assistant_message', 'tool_start', 'tool_result', 'card', 'usage',
    'done', 'error'];
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

  // ─── the rail: active job + cwd ───────────────────────────────────────────
  // One job at a time, registry-wide (R7), so this says what is holding the slot.
  // It no longer links anywhere: a chat turn is already on screen, and a run
  // started by the CLI is not something this page can show.
  async function health() {
    try {
      var h = await api('GET', '/api/health');
      // The rail's path line is shared. This owns it until chat.js has a bound
      // repository to name there — the server's working directory is only what
      // a relative output_dir resolves against, which is the less useful of the
      // two once there is a project. chat.js claims it with data-project.
      var cwd = $('cwd'), lab = $('cwd-lab');
      if (cwd && !cwd.getAttribute('data-project')) {
        // Labelled, because unlabelled this line read as "a project is already
        // open here" on a page where none was. chat.js relabels it `repository`
        // once the leader has bound one.
        setPath(cwd, h.cwd);
        if (lab) { lab.textContent = 'working directory'; lab.hidden = false; }
      }
      var bar = $('jobbar');
      if (h.active) {
        bar.hidden = false; bar.className = 'jobbar';
        var what = h.active.kind === 'chat' ? 'this turn' : esc(h.active.kind) + ' ' + esc(h.active.params.phase || '');
        bar.innerHTML = tag(h.active.state, 'running') + ' ' + what;
        if (!S.pollTimer) S.pollTimer = setInterval(health, 3000);
      } else {
        bar.hidden = true;
        if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
      }
      return h;
    } catch (e) { $('jobbar').hidden = false; $('jobbar').className = 'jobbar err'; $('jobbar').textContent = 'server unreachable'; return null; }
  }

  // chat.js is a second plain script with no module system, so the pieces it
  // needs are published here rather than duplicated there. It reads them only
  // from inside its own functions — this assignment runs after chat.js has been
  // parsed, because index.html loads chat.js first.
  window.FORGE = {
    S: S, api: api, follow: follow, esc: esc, tag: tag, flash: flash,
    save: save, fileUrl: fileUrl, health: health, setPath: setPath
  };

  // The guard is the point: boot is `health().then(start)`, so a chat.js that
  // 404s or fails to parse would throw inside a promise continuation, leaving an
  // unhandled rejection, no flash and a blank page — on the only surface there is.
  function start() {
    if (!window.ForgeChat) {
      $('step-chat').textContent = 'chat.js did not load — reload the page.';
      return;
    }
    return window.ForgeChat.render();
  }

  health().then(start).catch(function (e) { flash(fmtErr(e)); });
})();
