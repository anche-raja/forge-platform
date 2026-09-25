/* FORGE chat — the browser half of the leader agent, and now the whole product.

   Loaded BEFORE app.js, because app.js ends with `health().then(start)` and
   start() renders this file: if that fetch continuation wins the race against
   the parser, there would be no window.ForgeChat to render. Nothing here may
   touch window.FORGE at load time for the same reason — every reference to it
   is inside a function that only runs from render() or a handler, by which time
   app.js has defined it.

   Three rules in this file are load-bearing and easy to undo by accident.

   ONE SINK. Everything this page shows is untrusted: model prose, file names out
   of the migrated repository, diff lines, reviewer feedback, tool arguments the
   model wrote. There is exactly one function that turns a string into markup —
   html() — and the only strings that reach it are literals plus values passed
   through esc() or md(), which escapes before it does anything else. Diff lines,
   log lines and the push command never reach it at all; they are written with
   textContent. A single missed escape here is a stored XSS that replays on every
   reload, from a file called `<img src=x onerror=...>.java`.

   THE RELOAD RULE. A turn is a job, and its SSE stream always replays from seq 0.
   So a browser that reloads mid-turn renders the transcript items whose job_id is
   NOT the active job's, and lets the stream draw that entire turn — the user
   bubble included, from turn_start.user. Taking either half from both sources
   double-renders every message of the live turn.

   THE LEADER OWNS THE PROJECT. There is no project form any more: the leader
   asks which folder the repository is in and calls set_project. This file never
   sends a source_dir. It only reads the resolved dirs back out of the discovery
   a plan card already carries, because two things here need to reach the output
   directory afterwards — the review queue a card belongs to, and an artifact
   download — and neither is worth a round trip to the model. It never writes
   S.project.decisions: R9, the hold gate is not the model's to lower.

   Two more rules came in with the chat surface itself.

   STREAMING APPENDS, IT DOES NOT REDRAW. A delta goes into the live text node
   with appendData(), so the paragraph being written is never rebuilt and the
   page never jumps under the reader's eyes. See feed() for how the buffer is
   split into a settled half (parsed once) and the one block still being
   written; re-parsing the whole message on every token is what that replaced.

   STICKINESS IS THE USER'S, NOT OURS. The transcript follows the bottom only
   while the reader is at the bottom. Scrolling up more than 60px releases it
   and raises the "↓ New messages" pill; nothing yanks the viewport back. */
(function () {
  'use strict';

  // ─── the one markup sink ──────────────────────────────────────────────────
  function html(el, markup) { el.innerHTML = markup; }   /* markup = literals + esc() + md(), never raw */

  // ─── app.js, resolved lazily (it loads after this file) ───────────────────
  function api(method, path, body) { return window.FORGE.api(method, path, body); }
  function esc(value) { return window.FORGE.esc(value); }
  function tag(value, cls) { return window.FORGE.tag(value, cls); }
  function fileUrl(name) { return window.FORGE.fileUrl(name); }
  function S() { return window.FORGE.S; }
  function $(id) { return document.getElementById(id); }
  function node(name, cls) { var e = document.createElement(name); if (cls) e.className = cls; return e; }
  function num(v, places) { return Number(v || 0).toFixed(places); }
  function str(v) { return String(v == null ? '' : v); }
  function fmtErr(e) { return e && e.message ? e.message : String(e); }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function arr(v) { return Array.isArray(v) ? v : []; }

  // Every row of the transcript goes in through here. Something that arrives
  // now gets `c-in`, which motion.css animates in; a transcript redrawn on
  // reload is history, and fifty cards rising at once would be noise.
  function put(el) {
    if (!C.replaying) el.classList.add('c-in');
    $('c-items').appendChild(el);
    return el;
  }

  // ─── minimal markdown, escaped first ──────────────────────────────────────
  // Deliberately tiny: bold, inline code, fenced blocks, dash lists, paragraphs.
  // No links, no images, no headings, no raw HTML pass-through — a model that
  // writes `<a href="javascript:...">` gets a literal string back.
  function inline(text) {
    var s = esc(text);
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?;:]|$)/g, '$1<em>$2</em>');
    return s.replace(/\n/g, '<br>');
  }

  function prose(chunk) {
    var lines = chunk.split('\n'), out = '', para = [], list = [];
    function flushPara() { if (para.length) { out += '<p>' + inline(para.join('\n')) + '</p>'; para = []; } }
    function flushList() {
      if (!list.length) return;
      out += '<ul>' + list.map(function (t) { return '<li>' + inline(t) + '</li>'; }).join('') + '</ul>';
      list = [];
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (/^\s*[-*]\s+/.test(line)) { flushPara(); list.push(line.replace(/^\s*[-*]\s+/, '')); }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
    return out;
  }

  function md(text) {
    var lines = str(text).replace(/\u0000/g, '').split('\n');
    var out = '', block = [], fenced = false;
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*```/.test(lines[i])) {
        if (fenced) { out += '<pre><code>' + esc(block.join('\n')) + '</code></pre>'; }
        else { out += prose(block.join('\n')); }
        block = []; fenced = !fenced;
        continue;
      }
      block.push(lines[i]);
    }
    // An unclosed fence runs to the end: a half-streamed code block is still a
    // code block, and must not be re-read as prose when the delta arrives.
    out += fenced ? '<pre><code>' + esc(block.join('\n')) + '</code></pre>' : prose(block.join('\n'));
    return out;
  }

  // ─── state ────────────────────────────────────────────────────────────────
  var C = {
    id: '',                 // conversation id, mirrored into localStorage
    es: null, jobId: null,  // the turn this tab is following
    busy: false, booted: false, stick: true,
    replaying: false,       // true while reload() redraws the transcript: nothing animates in
    unitCost: 0,            // from the last estimate card; drives the running cost
    queue: { run: '', keys: {} },   // the review queue the live cards belong to
    cards: {},              // card_id -> {card, el}
    decisions: {},          // card_id -> {decision, note, rule} — survives a redraw
    pending: {},            // pending_id -> true while the confirmation is open
    tools: {},              // tool_id -> live row
    msgs: {},               // message_id -> streaming assistant block
    packs: {},              // pack name -> true, once run_pack has come back ok
    ghost: null,            // the placeholder bubble, replaced by turn_start
    wait: null,             // the dots row, open from turn_start to the first token
    tick: null              // the 1s interval that ages the running tool rows
  };

  // Every chip FILLS the composer; none of them sends. A suggestion the user
  // has not read yet must not become a turn, and the first one is a path with
  // a shape to edit — the folder is the one thing the leader cannot guess.
  function suggestions() {
    var bound = str(S().project.source_dir);
    return (bound ? [] : ['Migrate ~/path/to/my/app']).concat([
      'Profile this repository',
      'What would it take to get to Java 21?',
      'Show me what needs review'
    ]).slice(0, 4);
  }

  function loadStore() {
    try { var v = JSON.parse(localStorage.getItem('forge.chat') || '{}'); return v && typeof v === 'object' ? v : {}; }
    catch (e) { return {}; }
  }
  function remember() {
    try { localStorage.setItem('forge.chat', JSON.stringify({ conversation_id: C.id })); }
    catch (e) { /* private window */ }
  }
  function forget() { try { localStorage.removeItem('forge.chat'); } catch (e) { /* private window */ } C.id = ''; }

  // ─── scrolling ────────────────────────────────────────────────────────────
  // 60px is the whole rule: below that the reader is still "at the bottom" and
  // new text follows them down; above it they are reading something and the
  // viewport stops moving until they ask for it back.
  function atBottom() {
    var t = $('c-transcript');
    return !t || (t.scrollHeight - t.scrollTop - t.clientHeight) <= 60;
  }
  function jump(on) {
    var b = $('c-jump');
    if (b) b.hidden = !on;
  }
  function fit(force) {
    var t = $('c-transcript');
    if (!t) return;
    if (force) C.stick = true;
    if (C.stick) { t.scrollTop = t.scrollHeight; jump(false); }
    else jump(true);   // only ever called when something new arrived
  }

  // ─── composer / busy ──────────────────────────────────────────────────────
  // Never gated on a project: the leader asks for the folder in the chat, so the
  // first thing a first-time visitor must be able to do is type. The field stays
  // enabled while a turn runs — what changes is that Send becomes Stop, so a
  // half-typed next message survives the turn it was typed during.
  function setBusy(on) {
    C.busy = !!on;
    $('c-send').hidden = C.busy;
    $('c-stop').hidden = !C.busy;
    $('c-stop').disabled = false;
    // The model is fixed for the turn it was sent with; a click mid-run would only mislead.
    $('c-trial').disabled = C.busy;
    sendable();
    var acts = document.querySelectorAll('#c-items .c-act');
    for (var i = 0; i < acts.length; i++) acts[i].disabled = C.busy || acts[i].getAttribute('data-done') === '1';
    applyBar();
  }

  // An empty field cannot be sent, and the button says so rather than failing.
  function sendable() {
    var i = $('c-input'), b = $('c-send');
    if (i && b) b.disabled = !i.value.trim() || C.busy;
  }

  function notice(text, bad) {
    var el = $('c-notice');
    el.className = bad ? 'bad' : '';
    el.textContent = str(text);
    el.hidden = !text;
  }

  // One row up to eight, then it scrolls. The cap is computed from the field's
  // own line-height rather than hard-coded, so changing the type size in
  // chat.css cannot quietly turn eight rows into five.
  function grow() {
    var i = $('c-input');
    if (!i) return;
    i.style.height = 'auto';
    var cs = window.getComputedStyle(i);
    var line = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) * 1.5) || 22;
    // Eight rows is 180px, which is a fifth of a desktop pane and a third of a
    // 603px phone one. The narrow cap matches the ≤640 rule in chat.css.
    var max = Math.round(line * (window.innerWidth <= 640 ? 5 : 8));
    i.style.height = Math.min(i.scrollHeight, max) + 'px';
    i.style.overflowY = i.scrollHeight > max ? 'auto' : 'hidden';
  }

  // ─── boot / render ────────────────────────────────────────────────────────
  function boot() {
    if (!C.booted) {
      C.booted = true;
      $('c-chips').onclick = function (ev) {
        var b = ev.target.closest('button[data-say]');
        if (!b) return;
        var i = $('c-input');
        i.value = b.getAttribute('data-say');
        grow(); sendable(); i.focus();
      };
    }
    $('c-new').onclick = reset;
    $('c-form').onsubmit = function (ev) { ev.preventDefault(); submit(); };
    $('c-input').oninput = function () { grow(); sendable(); };
    $('c-input').onkeydown = function (ev) {
      // An IME is mid-composition: this Enter is choosing a candidate, not
      // sending. keyCode 229 is the same signal from browsers that do not set
      // isComposing. Without this, a Japanese or Chinese user sends half a word.
      if (ev.isComposing || ev.keyCode === 229) return;
      if (ev.key !== 'Enter') return;
      // Shift+Enter is a newline. ⌘/Ctrl+Enter sends too, because every other
      // chat box on the machine does.
      if (ev.shiftKey && !(ev.metaKey || ev.ctrlKey)) return;
      ev.preventDefault();
      submit();
    };
    $('c-apply').onclick = applyClick;
    $('c-clear').onclick = clearClick;
    $('c-all').onchange = approveAllChange;
    // A per-viewer convenience: remembered in this browser only, sent with every turn.
    try { $('c-trial').checked = localStorage.getItem('forge.trial') === '1'; } catch (e) { /* storage blocked */ }
    $('c-trial').onchange = function () {
      try { localStorage.setItem('forge.trial', $('c-trial').checked ? '1' : '0'); } catch (e) { /* storage blocked */ }
    };
    $('c-stop').onclick = stop;
    $('c-jump').onclick = function () { fit(true); $('c-input').focus(); };
    $('c-transcript').onscroll = function () {
      C.stick = atBottom();
      if (C.stick) jump(false);
    };
    // The field starts at exactly one row and Send starts dimmed, rather than
    // waiting for the first keystroke to agree with the CSS.
    grow(); sendable();
  }

  // Rebuilt on every reload rather than once: the first chip depends on whether
  // a repository is bound yet, and reset() unbinds one.
  function chips() {
    html($('c-chips'), suggestions().map(function (text) {
      return '<button type="button" data-say="' + esc(text) + '">' + esc(text) + '</button>';
    }).join(''));
  }

  function render() {
    boot();
    // A turn is already streaming into this DOM. Re-fetching here would wipe the
    // live nodes and a second follow() would double every delta.
    if (C.es && C.es.readyState !== 2) { fit(true); return; }
    return reload();
  }

  async function reload() {
    C.id = str(loadStore().conversation_id);

    clearItems();
    notice('');
    setBusy(false);

    var data = null;
    if (C.id) {
      try { data = await api('GET', '/api/chat/' + encodeURIComponent(C.id)); }
      catch (e) { forget(); }   // a restarted server has never heard of this id
    }
    if (data) bindProject(data);
    await reconcileQueue();
    C.replaying = true;
    try {
      if (data) draw(data);
      else spend(null, null);
    } finally {
      C.replaying = false;
    }
    $('c-empty').hidden = !!(data && (data.transcript || []).length);
    chips();
    railPath();
    refreshCards();
    fit(true);
  }

  // The conversation's own directories, as the server bound them. The output
  // directory is the repository's `.migrated` unless the user named another, so
  // the browser must never fall back to a default of its own: a queue read or a
  // download against the wrong folder would judge every card by another run.
  function bindProject(data) {
    var source = str(data.source_dir), out = str(data.output_dir);
    var p = S().project;
    if (!source) return;
    if (p.source_dir === source && p.output_dir === out) return;
    p.source_dir = source; p.output_dir = out;
    window.FORGE.save();
  }

  function draw(data) {
    C.id = str(data.conversation_id) || C.id;
    remember();
    C.pending = {};
    arr(data.pending).forEach(function (p) { if (p && p.pending_id) C.pending[p.pending_id] = true; });
    spend(data.spend_usd, data.leader_cost_usd);

    var active = data.active_job && data.active_job.id ? data.active_job : null;
    arr(data.transcript).forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      if (active && item.job_id === active.id) return;   // the stream draws that turn, whole
      drawItem(item);
    });
    if (active) attach(active.id);
    else setBusy(false);
  }

  function clearItems() {
    var box = $('c-items');
    while (box.firstChild) box.removeChild(box.firstChild);
    C.cards = {}; C.tools = {}; C.msgs = {}; C.ghost = null; C.wait = null;
    // The pack counter is read back off the transcript, so it is rebuilt with
    // it rather than carried across a redraw.
    C.packs = {};
    paintPacks();
  }

  async function reset() {
    if (C.busy) return;
    if (C.id) { try { await api('POST', '/api/chat/' + encodeURIComponent(C.id) + '/reset'); } catch (e) { /* a gone conversation is already reset */ } }
    forget();
    C.decisions = {};
    // A new conversation is unbound — the leader will ask for the folder again —
    // so the remembered dirs would point a download or a queue read at a project
    // this chat is no longer working on.
    var p = S().project;
    p.source_dir = ''; p.output_dir = '';
    window.FORGE.save();
    await reload();
  }

  // ─── sending ──────────────────────────────────────────────────────────────
  function submit() {
    var input = $('c-input'), text = input.value.trim();
    if (!text || C.busy) return;
    input.value = ''; grow(); sendable();
    ghost(text);
    post({ message: text }, function () { input.value = text; grow(); sendable(); });
  }

  function ghost(text) {
    // The user bubble itself is drawn from turn_start, so that a reload mid-turn
    // has exactly one source for it. This is the placeholder until then, dimmed
    // rather than labelled: the alignment already says whose turn it is.
    var row = node('div', 'c-user c-ghost');
    var box = node('div');
    var body = node('div', 'c-md c-blocks');
    body.textContent = text;
    box.appendChild(body); row.appendChild(box);
    put(row);
    $('c-empty').hidden = true;
    C.ghost = row;         // dropGhost() has nothing to remove without this
    fit(true);
  }

  function dropGhost() {
    if (C.ghost && C.ghost.parentNode) C.ghost.parentNode.removeChild(C.ghost);
    C.ghost = null;
    // Removing the ghost can leave the transcript empty again — the first
    // message of a fresh chat failing (no agents.yaml, say) took the empty
    // state away with it and left a blank pane under the error. The empty
    // state belongs on screen whenever there is nothing else to show.
    $('c-empty').hidden = $('c-items').children.length > 0;
  }

  // No source_dir and no config: the route falls back to agents.yaml, and the
  // project is the leader's to set through set_project. Sending a remembered
  // folder here would re-bind a conversation the leader already bound.
  async function post(extra, restore) {
    var body = Object.assign({}, extra || {});
    if (C.id) body.conversation_id = C.id;
    body.trial = !!$('c-trial').checked;
    setBusy(true);
    notice('');
    try {
      var r = await api('POST', '/api/chat', body);
      C.id = str(r.conversation_id) || C.id;
      remember();
      attach(r.job_id);
      return true;
    } catch (e) {
      setBusy(false);
      dropGhost();
      var message = fmtErr(e);
      // 409 from the registry: something else holds the single job slot. The
      // typed text and every collected decision stay exactly where they were.
      if (/still running/i.test(message)) notice(message + ' — FORGE runs one job at a time; wait for it to finish.');
      else notice(message, true);
      if (restore) restore();
      return false;
    }
  }

  function act(action) { return post({ action: action }); }

  async function stop() {
    if (!C.jobId) return;
    $('c-stop').disabled = true;
    try { await api('POST', '/api/runs/' + encodeURIComponent(C.jobId) + '/cancel'); }
    catch (e) { notice(fmtErr(e), true); }
    $('c-stop').disabled = false;
  }

  // ─── the stream ───────────────────────────────────────────────────────────
  function attach(jobId) {
    if (!jobId) { setBusy(false); return; }
    if (C.es && C.es.readyState !== 2) {
      if (C.jobId === jobId) return;
      C.es.close();
    }
    C.jobId = jobId;
    setBusy(true);
    C.es = window.FORGE.follow(jobId, onEvent);
    // follow()'s 'error' listener is for the server's error FRAME; the native
    // connection-error Event has no .data and never reaches onEvent, so without
    // this the composer would stay disabled forever after a server restart.
    C.es.onerror = function () { if (C.es && C.es.readyState === 2) lost(); };
    window.FORGE.health();
  }

  function lost() {
    drop();
    setBusy(false);
    notice('The connection to that turn dropped. Reload the page to see where it got to.', true);
  }

  // follow() closes the EventSource on done/error, but render() tests C.es to
  // decide whether a turn is still streaming into this DOM — so the handle is
  // dropped here rather than left to another file's behaviour.
  function drop() {
    if (C.es) { try { C.es.close(); } catch (e) { /* already closed */ } }
    C.es = null;
    C.jobId = null;
  }

  function onEvent(type, d) {
    if (!d || typeof d !== 'object') d = {};
    if (d.via === 'tool' && d.tool_id) { relayed(type, d); return; }
    if (type === 'turn_start') { dropGhost(); $('c-empty').hidden = true; if (d.user) drawItem(d.user); waiting(); }
    else if (type === 'assistant_delta') { feed(stream(d.message_id), str(d.text)); }
    else if (type === 'assistant_message') { settle(stream(d.message_id), d.text); }
    else if (type === 'tool_start') { idle(); toolStart(d); }
    else if (type === 'tool_result') { toolResult(d); }
    else if (type === 'card') { drawCard(d.card_id, d.card, true); }
    else if (type === 'usage') { spend(d.spend_usd, d.leader_cost_usd); }
    else if (type === 'done') { finish(d); }
    else if (type === 'error') { failed(d); }
    fit();
  }

  function finish(d) {
    drop();
    idle();
    setBusy(false);
    if (d && d.result && Array.isArray(d.result.pending)) {
      C.pending = {};
      d.result.pending.forEach(function (p) { if (p && p.pending_id) C.pending[p.pending_id] = true; });
      refreshConfirms();
    }
    window.FORGE.health();
    // A run or an apply inside that turn rewrote manual-review-queue.json, so
    // which cards may still be applied — and which files are still listed in an
    // expanded queue — is now a question for the queue itself.
    reconcileQueue().then(function () { refreshCards(); refreshFrames(); });
  }

  function failed(d) {
    drop();
    idle();
    setBusy(false);
    var p = node('p', 'c-note bad');
    p.textContent = 'That turn failed: ' + str(d.error);
    put(p);
    window.FORGE.health();
  }

  // ─── assistant blocks ─────────────────────────────────────────────────────
  // No bubble and no card: a turn from FORGE is the column's full width with a
  // mark on the first line. The mark is a sibling of the text rather than
  // something inside it, so every paragraph after the first aligns to the text.
  // The dots have to be on screen before the first token, and the first token
  // is what creates the message. So turn_start opens an empty assistant row and
  // the first message claims it; whatever comes first instead — a tool call, a
  // failure, the end of the turn — takes it away again, because an empty row
  // pulsing under a finished turn is a lie about what is happening.
  // Only `if (C.wait)`: a reload mid-turn has already drawn the earlier turns'
  // messages into C.msgs, and testing that would leave the replayed turn with
  // no dots at all — the one case where the wait is longest.
  function waiting() {
    if (C.wait) return;
    C.wait = blank();
    fit();
  }
  function idle() {
    if (C.wait && C.wait.root.parentNode) C.wait.root.parentNode.removeChild(C.wait.root);
    C.wait = null;
  }

  function stream(messageId) {
    var id = str(messageId) || 'm';
    if (C.msgs[id]) return C.msgs[id];
    if (C.wait) { C.msgs[id] = C.wait; C.wait = null; return C.msgs[id]; }
    C.msgs[id] = blank();
    return C.msgs[id];
  }

  function blank() {
    var root = node('div', 'c-assistant c-live');
    var mark = node('span', 'c-mark');
    mark.textContent = '✦';
    mark.setAttribute('aria-hidden', 'true');
    var body = node('div', 'c-abody c-md');
    var dots = node('div', 'c-dots');
    html(dots, '<i></i><i></i><i></i>');      // literal markup, no input reaches this
    var done = node('div', 'c-done c-blocks');
    var tail = node('div', 'c-tail c-blocks');
    body.appendChild(dots); body.appendChild(done); body.appendChild(tail);
    root.appendChild(mark); root.appendChild(body);
    put(root);
    return { buf: '', cut: 0, seen: '', text: null, root: root, dots: dots, done: done, tail: tail };
  }

  // Where does the finished part of the buffer end? At the last blank line that
  // is not inside a fence — that is a block boundary for md(), so parsing the
  // two halves separately gives the same markup as parsing the whole. Anything
  // after it is the block still being written, and it is the only thing that
  // gets re-parsed when a delta lands.
  function settledCut(buf) {
    var lines = buf.split('\n'), cut = 0, pos = 0, fence = false;
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*```/.test(lines[i])) fence = !fence;
      pos += lines[i].length + 1;
      if (!fence && !lines[i].trim() && i < lines.length - 1) cut = pos;
    }
    return cut;
  }

  // Can this tail be grown by appending to a text node? Only while it is one
  // run of ordinary prose: a backtick, a `*`, a `_`, a newline or a list marker
  // means md() would produce something other than a single <p>text</p>.
  function plainTail(t) {
    return !!t && !/[`*_\n]/.test(t) && !/^\s*-\s/.test(t) && !!t.trim();
  }

  function feed(m, delta) {
    m.buf += delta;
    if (m.buf) m.dots.hidden = true;

    var cut = settledCut(m.buf);
    if (cut > m.cut) {
      // One parse, once, for a block that will never change again.
      m.cut = cut;
      html(m.done, md(m.buf.slice(0, cut)));
      m.text = null; m.seen = '';
    }

    var tail = m.buf.slice(m.cut);
    if (m.text && plainTail(tail) && tail.indexOf(m.seen) === 0) {
      m.text.appendData(tail.slice(m.seen.length));   // the append: no redraw, no jump
      m.seen = tail;
      return;
    }

    // The tail is not (or is no longer) a plain run — reparse it, and it alone.
    // The empty <p> keeps the block cursor on screen between blocks.
    html(m.tail, tail ? md(tail) : '<p></p>');
    m.text = null; m.seen = '';
    if (plainTail(tail)) {
      var p = m.tail.firstChild;
      if (p && p.tagName === 'P' && p.childNodes.length === 1 && p.firstChild.nodeType === 3) {
        m.text = p.firstChild;
        m.seen = tail;
      }
    }
  }

  // The end of a message, from `assistant_message` or from a replayed
  // transcript item: one final parse of the authoritative text, no cursor.
  function settle(m, text) {
    m.buf = str(text);
    m.cut = m.buf.length;
    m.text = null; m.seen = '';
    m.dots.hidden = true;
    html(m.done, md(m.buf));
    html(m.tail, '');
    m.root.classList.remove('c-live');
    m.root.hidden = !m.buf;
  }

  // ─── transcript items (the reload path) ───────────────────────────────────
  function drawItem(item) {
    var role = str(item.role);
    if (role === 'user' || role === 'action') { userBubble(item.text); }
    else if (role === 'assistant') { settle(stream(item.message_id), item.text); }
    else if (role === 'tool') { toolStart(item, true); if (item.ok !== null && item.ok !== undefined) toolResult(item); }
    else if (role === 'card') { drawCard(item.card_id, item.card); }
    else if (role === 'cancelled') { simpleNote(str(item.text) || 'Stopped.', false); }
    else if (role === 'error') { simpleNote('That turn failed: ' + str(item.message), true); }
  }

  // Right-aligned and tinted. No label and no avatar — the alignment says it.
  function userBubble(text) {
    var row = node('div', 'c-user');
    var box = node('div');
    var body = node('div', 'c-md c-blocks');
    html(body, md(text));
    box.appendChild(body); row.appendChild(box);
    put(row);
  }

  function simpleNote(text, bad) {
    var p = node('p', bad ? 'c-note bad' : 'c-note');
    p.textContent = text;
    put(p);
  }

  // ─── tool rows ────────────────────────────────────────────────────────────
  // One line while it runs: mark, title, elapsed. The args and the raw log sit
  // behind the header because they answer a question that is usually not asked;
  // the progress, which is the reason to look at all, stays outside it.
  // `replayed` is true for a row drawn out of the transcript on reload. Such a
  // row was never timed in this browser: a finished one already reports no
  // duration, and a still-running one would otherwise tick up from the reload,
  // which is a number with no relation to when the tool started.
  function toolStart(d, replayed) {
    var id = str(d.tool_id);
    if (C.tools[id]) return C.tools[id];
    var el = node('div', 'c-tool c-running');
    html(el,
      '<button type="button" class="c-toolhead" aria-expanded="false">'
      + '<span class="c-state"><span class="c-spin"></span></span>'
      + '<b>' + esc(d.title || d.tool) + '</b>'
      + '<span class="c-elapsed"></span><span class="c-caret" aria-hidden="true">▶</span></button>'
      + '<div class="c-prog" hidden><div class="progress"><div></div></div>'
      + '<div class="c-now" hidden></div>'
      + '<div class="c-counts"></div><div class="c-prognote"></div></div>'
      + '<div class="c-toolsum" hidden></div>'
      + '<div class="c-toolbody" hidden><div class="c-toolargs"></div><div class="c-log" hidden></div></div>');
    var args = argsText(d.args);
    el.querySelector('.c-toolargs').textContent = args;   // model-written JSON, never markup
    el.querySelector('.c-toolargs').hidden = !args;
    put(el);

    var head = el.querySelector('.c-toolhead'), body = el.querySelector('.c-toolbody');
    head.onclick = function () {
      var open = body.hidden;
      body.hidden = !open;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    C.tools[id] = {
      el: el, state: el.querySelector('.c-state'), prog: el.querySelector('.c-prog'),
      bar: el.querySelector('.progress div'), counts: el.querySelector('.c-counts'),
      prognote: el.querySelector('.c-prognote'), log: el.querySelector('.c-log'),
      now: el.querySelector('.c-now'), time: el.querySelector('.c-elapsed'),
      sum: el.querySelector('.c-toolsum'),
      tool: str(d.tool), pack: str(obj(d.args).pack || ''), dry: !!obj(d.args).dry_run,
      t0: Date.now(), untimed: !!replayed, ended: false,
      total: 0, done: 0, cost: null, notes: []
    };
    ticking();
    return C.tools[id];
  }

  function argsText(args) {
    if (!args || typeof args !== 'object') return '';
    try { var s = JSON.stringify(args); return s === '{}' ? '' : s; } catch (e) { return ''; }
  }

  function elapsed(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  }

  // One interval for every running row, and it stops itself when the last one
  // finishes — a per-row timer that outlives its row is a leak nobody sees.
  function ticking() {
    if (C.tick) return;
    C.tick = setInterval(function () {
      var live = 0;
      Object.keys(C.tools).forEach(function (k) {
        var t = C.tools[k];
        if (t.ended) return;
        live++;
        if (t.untimed) return;   // replayed: this browser never saw it start
        t.time.textContent = elapsed(Date.now() - t.t0);
      });
      if (!live) { clearInterval(C.tick); C.tick = null; }
    }, 1000);
  }

  function toolResult(d) {
    var t = C.tools[str(d.tool_id)] || toolStart(d);
    var ok = d.ok !== false;
    t.ended = true;
    // A row replayed out of the transcript was never timed here, so it reports
    // no duration rather than the 0s it took this browser to draw it.
    t.time.textContent = (!t.untimed && (Date.now() - t.t0) > 400) ? elapsed(Date.now() - t.t0) : '';
    var mark = d.needs_confirmation ? '●' : (ok ? '✓' : '✗');
    var cls = d.needs_confirmation ? 'hold' : (ok ? 'ok' : 'no');
    html(t.state, '<span class="c-mk ' + cls + '">' + mark + '</span>');
    t.state.setAttribute('title', d.needs_confirmation ? 'waiting for you' : (ok ? 'done' : 'failed'));
    // classList, not className: `c-in` may still be animating the row in.
    t.el.classList.remove('c-running');
    t.el.classList.toggle('bad', !ok);
    t.sum.textContent = str(d.summary);
    t.sum.hidden = !d.summary;
    if (t.total && t.done < t.total) t.done = t.total;
    if (t.total) paintBar(t);
    // A pack that ran to completion is what the rail counts. The live event
    // carries `completed_pack`, which the agent sets only for a real run that
    // reached `done`; a replayed transcript row does not, so the fallback is
    // the tool's own args minus the dry runs — a dry run spends the same money
    // but leaves the pack undone, and counting it would overstate progress.
    var finished = str(d.completed_pack) || (ok && !d.needs_confirmation && t.tool === 'run_pack' && !t.dry ? t.pack : '');
    if (finished) { C.packs[finished] = true; paintPacks(); }
    window.FORGE.health();
  }

  // Relayed service events, keyed by tool_id — the same `file`, `summary` and
  // `testgen_*` frames a run emits, arriving inside a tool row.
  function relayed(type, d) {
    var t = C.tools[str(d.tool_id)];
    if (!t) return;
    if (type === 'start') { t.total = d.total || 0; t.done = 0; t.dry = !!d.dry_run; paintBar(t); }
    else if (type === 'testgen_start') { t.total = d.targets || 0; t.done = 0; note(t, d.targets + ' class(es) to write tests for, ' + d.skipped + ' skipped'); paintBar(t); }
    else if (type === 'file' || type === 'testgen_unit') { t.total = d.total || t.total; t.done = d.index || t.done; t.spent = (t.spent || 0) + (Number(d.cost_usd) || 0); logLine(t, d); paintBar(t); }
    else if (type === 'skipped') { note(t, d.count + ' file(s) outside the scope prefix'); }
    // The last pack of a plan builds the project in the same row, and a build
    // can take minutes after the bar has already reached the end.
    else if (type === 'build_start') { note(t, 'building the project'); }
    else if (type === 'build') { note(t, 'build ' + str(d.outcome)); }
    else if (type === 'queue') { note(t, d.count + ' file(s) staged for review'); }
    else if (type === 'nothing') { note(t, str(d.message)); }
    else if (type === 'cancelled' || type === 'testgen_cancelled') { note(t, 'cancelled after ' + d.done + ' of ' + d.total); }
    else if (type === 'acceptance') { note(t, 'acceptance ' + str(d.verdict)); }
    else if (type === 'acceptance_skipped') { note(t, 'acceptance skipped — ' + str(d.reason)); }
    else if (type === 'snapshot_skipped') { note(t, 'snapshot skipped — ' + str(d.reason)); }
    else if (type === 'summary' || type === 'testgen_summary') { t.cost = d.cost_usd; t.totals = d; t.done = t.total; paintBar(t); }
    else if (type === 'apply_outcome') { logLine(t, d); resolveCard(d); }
    else if (type === 'apply_done') { note(t, d.remaining + ' file(s) still awaiting review'); }
    fit();
  }

  function note(t, text) {
    if (!text) return;
    t.notes.push(text);
    t.prog.hidden = false;
    t.prognote.textContent = t.notes.join(' · ');
  }

  function cell(value, label) {
    return '<div><b>' + esc(value) + '</b><span>' + esc(label) + '</span></div>';
  }

  function paintBar(t) {
    t.prog.hidden = false;
    var pct = t.total ? Math.round(100 * Math.min(t.done, t.total) / t.total) : 0;
    t.bar.style.width = pct + '%';
    var bits = [];
    if (t.total) bits.push(cell(t.done + '/' + t.total, t.dry ? 'units (dry run)' : 'units'));
    var totals = t.totals;
    if (totals) {
      ['passed', 'held', 'manual', 'blocked', 'generated'].forEach(function (k) {
        if (totals[k] !== undefined) bits.push(cell(totals[k], k));
      });
      if (totals.tests_failed !== undefined) bits.push(cell(totals.tests_failed, 'tests failed'));
      if (totals.bedrock_calls !== undefined) bits.push(cell(totals.bedrock_calls, 'Bedrock calls'));
    }
    // Each finished unit reports what it actually cost, so the running figure is
    // real, not the per-unit estimate; `summary` replaces it with the run total.
    if (t.cost != null) bits.push(cell('$' + num(t.cost, 3), 'cost'));
    else if (t.spent) bits.push(cell('$' + num(t.spent, 3), 'spent so far'));
    else if (C.unitCost && t.done) bits.push(cell('$' + num(t.done * C.unitCost, 2), 'spent so far (est.)'));
    html(t.counts, bits.join(''));
  }

  // Every value here is a repository path or a pipeline verdict, so each one
  // goes into its own element as text. Nothing on this path touches html().
  function logLine(t, d) {
    var path = str(d.label || d.file);
    var after = [];
    if (d.status || d.status_after) after.push(str(d.status || d.status_after));
    if (d.decision) after.push(str(d.decision));
    if (d.score != null) after.push('score ' + d.score);

    var line = node('div');
    if (d.index && d.total) {
      var n = node('span', 'c-lognum');
      n.textContent = d.index + '/' + d.total;
      line.appendChild(n);
    }
    var p = node('span', 'c-logpath');
    window.FORGE.setPath(p, path);
    line.appendChild(p);
    if (after.length) {
      var s = node('span', 'c-logstat');
      s.textContent = after.join(' · ');
      line.appendChild(s);
    }
    t.log.hidden = false;
    t.log.appendChild(line);
    while (t.log.childNodes.length > 200) t.log.removeChild(t.log.firstChild);
    t.log.scrollTop = t.log.scrollHeight;

    // The one line that updates in place, so the progress block says what is
    // happening now without anyone opening the log.
    if (path) {
      t.now.hidden = false;
      window.FORGE.setPath(t.now, path);
    }
  }

  // ─── cards ────────────────────────────────────────────────────────────────
  // `live` is true only for a card arriving on the stream. A card replayed from
  // the transcript describes the past: it may not re-open a consumed pending
  // confirmation, and it may not overwrite the review queue this page just read.
  function drawCard(cardId, card, live) {
    if (!card || typeof card !== 'object') return;
    var id = str(cardId) || ('c' + Object.keys(C.cards).length);
    var kind = str(card.kind);
    var el = node('div', 'card c-card');
    C.cards[id] = { id: id, card: card, el: el };
    put(el);

    if (kind === 'plan') { planCard(el, card); adopt(card.discovery); }
    else if (kind === 'evidence') { evidenceCard(el, card); }
    else if (kind === 'estimate') { estimateCard(el, card); }
    else if (kind === 'confirm') { confirmCard(el, card, id, live); }
    else if (kind === 'review_file') { reviewCard(el, card, id, live); }
    else if (kind === 'review_more') { moreCard(el, card); }
    else if (kind === 'acceptance') { acceptanceCard(el, card); }
    else if (kind === 'tests') { testsCard(el, card); }
    else if (kind === 'feedback') { feedbackCard(el, card); }
    else if (kind === 'artifacts') { artifactsCard(el, card); }
    else if (kind === 'land') { landCard(el, card); }
    else if (kind === 'pull_request') { prCard(el, card); }
    else if (kind === 'build') { buildCard(el, card); }
    else { noteCard(el, kind || 'card', 'This page does not know that card.'); }
    fit();
  }

  function head(title, extra) {
    return '<h3>' + esc(title) + (extra || '') + '</h3>';
  }

  // A filesystem path in a card header. The dir="ltr" isolate is what lets
  // chat.css truncate it from the left with `direction:rtl`; without it the
  // header wrapped mid-identifier at phone width (…/OrderAction.j / ava).
  // Same contract as setPath() in app.js, but as markup for the one sink.
  function pathSpan(value) {
    var v = str(value);
    return ' <span class="c-path" title="' + esc(v) + '"><span dir="ltr">' + esc(v) + '</span></span>';
  }

  // The only two fields on a card that reach an executable context. Everything
  // else goes in as text; these go in as an href, and esc() stops the attribute
  // breaking out but does NOT stop `javascript:`. Both values are server-built
  // as /api/… today — this keeps that true if one ever stops being.
  function href(url) {
    var u = str(url);
    return /^\/api\//.test(u) ? u : '';
  }

  function noteCard(el, title, message) {
    html(el, head(title) + '<p>' + esc(message) + '</p>');
  }

  // The leader resolved the folder; the browser needs it only to reach the
  // output directory (the review queue, an artifact download). discover() puts
  // the resolved source on the profile and writes its outputs into the output
  // directory, so both come back out of the plan card rather than out of a form
  // the owner asked to delete.
  function adopt(discovery) {
    var d = obj(discovery);
    var profile = obj(d.profile), paths = obj(d.paths);
    var source = str(d.source_dir || profile.source_dir);
    var out = str(d.output_dir) || dirOf(str(paths.json) || str(paths.yaml));
    var p = S().project, changed = false;
    if (source && p.source_dir !== source) { p.source_dir = source; changed = true; }
    if (out && p.output_dir !== out) { p.output_dir = out; changed = true; }
    if (!changed) return;
    window.FORGE.save();
    chips();      // the "Migrate ~/path" chip is only for a chat with no project
    railPath();
    // The queue read at reload time used the previous directory — or none at all,
    // on a first visit — so every review card on screen is being judged against
    // the wrong queue until this lands.
    reconcileQueue().then(refreshCards);
  }

  function dirOf(path) {
    var cut = Math.max(str(path).lastIndexOf('/'), str(path).lastIndexOf('\\'));
    return cut > 0 ? path.slice(0, cut) : '';
  }

  function planCard(el, card) {
    var d = obj(card.discovery);
    var acts = arr(d.activations);
    var order = arr(d.order);
    var profile = obj(d.profile);
    var plan = obj(d.intent);
    var selected = Array.isArray(plan.packs) ? plan.packs : null;

    var rows = acts.map(function (a) {
      var state = a.runnable ? 'runnable' : (a.complete ? 'blocked' : 'detect-only');
      var out = selected && selected.indexOf(a.pack) < 0;
      return '<tr' + (out ? ' class="out"' : '') + '><td class="mono">' + esc(a.pack) + '</td><td>'
        + tag(state, state === 'detect-only' ? 'skip' : state) + '</td><td class="hint">'
        + esc(arr(a.evidence).slice(0, 2).join(' · ')) + '</td></tr>';
    }).join('');

    var totals = cell(selected ? selected.length : acts.length, selected ? 'selected' : 'packs apply')
      + cell(order.length, 'in the plan')
      + cell(str(profile.build_system || '?'), 'build')
      + cell('Java ' + str(profile.java_level || '?'), 'source level')
      + (d.intent ? cell('$' + num(plan.cost_usd, 4), 'this plan call') : '');

    var notes = '';
    if (d.intent) {
      notes = block('Assumed — not stated in your request', plan.assumptions)
        + block('Worth confirming', plan.questions)
        + block('Set aside', arr(plan.excluded).map(function (e) { return str(e.pack) + ' — ' + str(e.reason); }))
        + block('Asked for, but not available here', arr(plan.unsupported).map(function (u) { return str(u.asked) + ' — ' + str(u.reason); }));
    }

    html(el, head(card.intent ? 'The plan, from your request' : 'Project profile',
      card.request ? ' <span class="c-subject">' + esc(card.request) + '</span>' : '')
      + '<div class="totals">' + totals + '</div>'
      + '<ol class="chips">' + order.map(function (id) { return '<li>' + esc(id) + '</li>'; }).join('') + '</ol>'
      + '<table class="tbl"><tr><th>Pack</th><th>State</th><th>Evidence</th></tr>'
      + (rows || '<tr><td colspan="3" class="hint">Nothing recognised in this repository.</td></tr>') + '</table>'
      + notes);
  }

  // What the Discover step used to be: every activation with its whole evidence
  // list, the run order, and where each decision came from. Collapsed, because
  // it is the answer to "why that plan?" and not the plan itself.
  function evidenceCard(el, card) {
    // cards.evidence_card() has already done the reading: each pack carries its
    // own state and whether the plan kept it, each decision carries where its
    // value came from. This draws that, and nothing more.
    var packs = arr(card.packs);
    var order = arr(card.order);
    var decisions = arr(card.decisions);
    var scope = obj(card.scope);

    var rows = packs.map(function (p) {
      var state = str(p.state);
      var lines = arr(p.evidence).map(function (e) { return '<li>' + esc(e) + '</li>'; }).join('');
      return '<tr' + (p.selected === false ? ' class="out"' : '') + '><td class="mono">' + esc(p.pack) + '</td><td>'
        + tag(state, state === 'detect-only' ? 'skip' : state) + '</td><td>'
        + (lines ? '<ul>' + lines + '</ul>' : '<span class="hint">no evidence line recorded</span>') + '</td></tr>';
    }).join('');

    var chips = order.map(function (p) {
      var state = str(p.state);
      var cls = state === 'runnable' ? '' : (state === 'blocked' ? 'blocked' : 'detect');
      if (p.selected === false) cls = (cls + ' out').trim();
      return '<li class="' + esc(cls) + '">' + esc(p.pack) + '</li>';
    }).join('');

    var decisionRows = decisions.map(function (d) {
      var from = str(d.from || 'default');
      return '<tr><td class="mono">' + esc(d.key) + '</td><td class="mono">' + esc(d.value) + '</td><td>'
        + tag(from, from === 'prompt' ? 'runnable' : 'skip') + '</td></tr>';
    }).join('');

    var scopeKeys = Object.keys(scope);
    var scopeLine = scopeKeys.length
      ? '<p class="hint">Scope — ' + esc(scopeKeys.map(function (k) {
          var v = scope[k];
          return k + ': ' + (Array.isArray(v) ? v.join(', ') : str(v));
        }).join(' · ')) + '</p>'
      : '';

    html(el, head('The evidence', ' <span class="c-subject">' + esc(packs.length + ' pack(s) recognised') + '</span>')
      + (card.error ? '<p class="hint">' + esc(card.error) + '</p>' : '')
      + '<details class="c-evidence"><summary>Why FORGE chose that plan</summary>'
      + (chips ? '<p class="hint">Run order — one pack at a time, in dependency order. A dashed chip is '
        + 'recognised but not migrated here; a struck-through one was set aside.</p>'
        + '<ol class="chips">' + chips + '</ol>' : '')
      + '<table class="tbl"><tr><th>Pack</th><th>State</th><th>Evidence</th></tr>'
      + (rows || '<tr><td colspan="3" class="hint">Nothing recognised in this repository.</td></tr>') + '</table>'
      + (decisionRows
        ? '<p class="hint">Platform decisions. They reach the packs, the hold gate and the acceptance checks '
          + 'on every run.</p>'
          + '<table class="tbl"><tr><th>Decision</th><th>Value</th><th>From</th></tr>' + decisionRows + '</table>'
        : '')
      + scopeLine
      + block('Set aside', arr(card.excluded).map(function (x) { return str(x.pack) + ' — ' + str(x.reason); }))
      + block('Asked for, but not available here', arr(card.unsupported).map(function (u) { return str(u.asked) + ' — ' + str(u.reason); }))
      + (card.summary ? '<details><summary>Discovery summary (as the CLI prints it)</summary><pre>'
        + esc(card.summary) + '</pre></details>' : '')
      + '</details>');
  }

  function block(title, items) {
    if (!items || !items.length) return '';
    return '<details><summary>' + esc(title) + ' (' + items.length + ')</summary><ul class="c-reasons">'
      + items.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul></details>';
  }

  function estimateCard(el, card) {
    if (card.unit_cost_usd) C.unitCost = Number(card.unit_cost_usd) || 0;
    html(el, head('What ' + str(card.pack) + ' would cost')
      + '<div class="totals">' + cell('$' + num(card.est_usd, 2), 'estimated')
      + cell(card.units, 'units') + cell(card.generated, 'generated')
      + cell('$' + num(card.unit_cost_usd, 3), 'per unit') + '</div>'
      + '<p class="hint">' + esc(card.note) + '</p>');
  }

  function confirmCard(el, card, id, live) {
    if (live && card.pending_id) C.pending[card.pending_id] = true;
    var rows = Array.isArray(card.decisions) ? card.decisions : null;
    var table = rows ? '<table class="tbl"><tr><th>File</th><th>Decision</th><th>Note</th></tr>'
      + rows.map(function (r) {
        return '<tr><td class="mono">' + esc(r.file) + '</td><td>' + tag(str(r.decision))
          + '</td><td class="hint">' + esc(r.note || '') + (r.rule ? ' <span class="mono">' + esc(r.rule) + '</span>' : '') + '</td></tr>';
      }).join('') + '</table>' : '';
    var args = argsText(card.args);
    // The tint is the point: this is the one card that spends money, and it
    // must not read like the ones that only report.
    el.className = 'card c-card c-confirm';
    html(el, head('FORGE needs your go-ahead')
      + '<p><b>' + esc(card.title) + '</b></p>'
      + '<p><span class="c-est">$' + esc(num(card.est_usd, 2)) + '</span> '
      + '<span class="hint">estimated' + (card.units != null ? ' · ' + esc(card.units) + ' units' : '')
      + ' · ' + esc(str(card.tool)) + '</span></p>'
      + (args ? '<details><summary class="hint">Exactly what it will be called with</summary>'
        + '<p class="hint mono">' + esc(args) + '</p></details>' : '')
      + table
      + (card.build ? buildLine(card.build) : '')
      + (card.preview ? '<details><summary class="hint">The description FORGE will publish with it</summary>'
        + '<pre class="c-preview">' + esc(card.preview) + '</pre></details>' : '')
      + '<div class="row"><button type="button" class="primary c-act" data-do="confirm">Run</button>'
      + '<button type="button" class="c-act" data-do="decline">Not now</button>'
      + '<span class="hint c-answer"></span></div>');

    el.querySelector('[data-do="confirm"]').onclick = function () { answerConfirm(el, card, 'confirm', 'Confirmed — running it.'); };
    el.querySelector('[data-do="decline"]').onclick = function () { answerConfirm(el, card, 'decline', 'Left alone.'); };
    if (!C.pending[card.pending_id]) closeConfirm(el, 'Already handled.');
    else if (C.busy) setBusy(true);
  }

  async function answerConfirm(el, card, type, said) {
    if (C.busy) return;
    var ok = await act({ type: type, pending_id: card.pending_id });
    if (!ok) return;
    delete C.pending[card.pending_id];
    closeConfirm(el, said);
  }

  function closeConfirm(el, said) {
    var buttons = el.querySelectorAll('.c-act');
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = true; buttons[i].setAttribute('data-done', '1'); }
    el.querySelector('.c-answer').textContent = said;
  }

  function refreshConfirms() {
    Object.keys(C.cards).forEach(function (id) {
      var rec = C.cards[id];
      if (rec.card.kind !== 'confirm') return;
      if (!C.pending[rec.card.pending_id]) closeConfirm(rec.el, 'Handled.');
    });
  }

  // ─── review cards ─────────────────────────────────────────────────────────
  function reviewCard(el, card, id, live) {
    // A card arriving from a newer run means the queue on disk was replaced;
    // every card from the old run is now about a file this diff no longer
    // describes, so it must stop being clickable rather than apply quietly.
    var replaced = live && card.run && str(card.run) !== C.queue.run;
    if (replaced) C.queue = { run: str(card.run), keys: {} };
    if (live && card.run) C.queue.keys[str(card.file) + '|' + str(card.pack)] = true;

    var reasons = [];
    if (card.hold_reason) reasons.push(str(card.hold_reason));
    arr(card.risk_reasons).forEach(function (r) { reasons.push(str(r)); });

    var badges = tag(str(card.status), str(card.status))
      + (card.risk_tier ? ' ' + tag(str(card.risk_tier) + ' risk' + (card.risk_score != null ? ' · ' + card.risk_score : ''), str(card.risk_tier)) : '')
      + (card.review_verdict ? ' ' + tag('review ' + (card.review_score != null ? card.review_score : '') + ' · ' + str(card.review_verdict), str(card.review_verdict)) : '')
      + (card.build_verdict ? ' ' + tag('build ' + str(card.build_verdict), str(card.build_verdict)) : '')
      + (card.generate ? ' ' + tag('generated', 'skip') : '');

    var findings = arr(card.guardrail_findings).map(function (k) { return tag(str(k), 'MEDIUM'); }).join(' ');

    html(el, head(str(card.pack), pathSpan(card.file))
      + '<p>' + badges + (findings ? ' ' + findings : '') + '</p>'
      + (reasons.length ? '<ul class="c-reasons">' + reasons.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>' : '')
      + (card.review_feedback ? '<details><summary>Reviewer feedback</summary><pre>' + esc(card.review_feedback) + '</pre></details>' : '')
      + (card.error ? '<details><summary>Error</summary><pre>' + esc(card.error) + '</pre></details>' : '')
      + (card.diff ? '<div class="c-diffwrap"><pre class="c-diff c-diffhead"></pre>'
        + '<pre class="c-diff c-diffrest" hidden></pre>'
        + '<button type="button" class="c-diffmore" hidden></button></div>'
        + (card.diff_truncated ? '<p class="hint">The diff itself was truncated before it reached this page.</p>' : '')
        : '<p class="hint">No diff: ' + esc(str(card.status) === 'BLOCKED' ? 'this unit was blocked before it was transformed' + (card.unblock ? ': ' + str(card.unblock) : '.') : 'nothing was transformed for this unit.') + '</p>')
      + block('Also transformed', card.also_transformed)
      + block('Superseded files', card.deleted_files)
      + '<div class="c-decide" data-card="' + esc(id) + '" hidden>'
      + '<p class="c-decidelab hint">Your decision — the approval is yours, not the model’s</p>'
      + '<div class="c-seg" role="group" aria-label="Your decision for this file">'
      + ['approve', 'reject', 'retry'].map(function (v) {
        return '<button type="button" class="c-segbtn" data-v="' + esc(v) + '" aria-pressed="false">' + esc(v) + '</button>';
      }).join('')
      + '</div>'
      + '<div class="c-notewrap" hidden>'
      + '<label class="c-rule">rule <input class="rule" placeholder="e.g. Rule 2"></label>'
      + '<textarea class="note" placeholder="What should change, or why this is rejected"></textarea>'
      + '</div></div>'
      + '<p class="hint c-stalenote" hidden></p><p class="hint c-outcome" hidden></p>');

    if (card.diff) clipDiff(el, str(card.diff));
    wireDecision(el, id);
    // Every older card is now about a queue that no longer exists on disk, so
    // they all have to be re-judged — not just this one.
    if (replaced) refreshCards();
    else refreshOne(C.cards[id]);
  }

  // Past 24 lines the diff is a wall, and a wall is not read. The tail is drawn
  // but hidden rather than dropped, so "Show all N lines" costs no second pass
  // and the count on the button is the real one.
  var DIFF_LINES = 24;
  function clipDiff(el, text) {
    var lines = text.split('\n');
    var wrap = el.querySelector('.c-diffwrap');
    var top = el.querySelector('.c-diffhead'), rest = el.querySelector('.c-diffrest');
    var more = el.querySelector('.c-diffmore');
    paintDiff(top, lines.slice(0, DIFF_LINES));
    if (lines.length <= DIFF_LINES) return;
    paintDiff(rest, lines.slice(DIFF_LINES));
    more.hidden = false;
    more.textContent = 'Show all ' + lines.length + ' lines';
    more.onclick = function () {
      var open = rest.hidden;
      rest.hidden = !open;
      more.textContent = open ? 'Show less' : 'Show all ' + lines.length + ' lines';
      wrap.classList.toggle('c-open', open);
    };
    // One diff, drawn in two boxes so the tail can be folded away — so they
    // scroll sideways as one. The guard stops the echo: setting scrollLeft on
    // the other pane fires its own scroll event straight back.
    var syncing = false;
    function sync(from, to) {
      return function () {
        if (syncing) return;
        syncing = true;
        to.scrollLeft = from.scrollLeft;
        syncing = false;
      };
    }
    top.addEventListener('scroll', sync(top, rest));
    rest.addEventListener('scroll', sync(rest, top));
  }

  // Segmented, and it SELECTS: nothing here posts. Clicking the chosen button
  // again clears the choice, which is the only way back to "no decision" once
  // one has been made — a radio group cannot be unchecked by clicking it.
  function wireDecision(el, id) {
    var seg = el.querySelector('.c-seg');
    var wrap = el.querySelector('.c-notewrap');
    var noteEl = el.querySelector('textarea.note'), ruleEl = el.querySelector('input.rule');

    function paintSeg(picked) {
      var bs = seg.querySelectorAll('.c-segbtn');
      for (var i = 0; i < bs.length; i++) {
        var on = bs[i].getAttribute('data-v') === picked;
        bs[i].setAttribute('aria-pressed', on ? 'true' : 'false');
        bs[i].className = on ? 'c-segbtn on' : 'c-segbtn';
      }
      // The note is what a reject or a retry has to carry back to the pipeline,
      // so it appears with the choice that needs it and nowhere else.
      wrap.hidden = !(picked === 'reject' || picked === 'retry');
    }
    seg.reset = function () { noteEl.value = ''; ruleEl.value = ''; paintSeg(''); };
    // Approve all sets the same state a click would; it still posts nothing.
    seg.pick = function (v) {
      if (!v) delete C.decisions[id];
      else C.decisions[id] = { decision: v, note: noteEl.value.trim(), rule: ruleEl.value.trim() };
      paintSeg(v);
    };

    seg.onclick = function (ev) {
      var b = ev.target.closest('.c-segbtn');
      if (!b) return;
      var v = b.getAttribute('data-v');
      var cur = C.decisions[id] ? C.decisions[id].decision : '';
      var next = cur === v ? '' : v;
      if (!next) delete C.decisions[id];
      else C.decisions[id] = { decision: next, note: noteEl.value.trim(), rule: ruleEl.value.trim() };
      paintSeg(next);
      applyBar();
    };
    wrap.oninput = function () {
      var d = C.decisions[id];
      if (!d) return;
      d.note = noteEl.value.trim();
      d.rule = ruleEl.value.trim();
    };

    var saved = C.decisions[id];
    if (saved) { noteEl.value = saved.note || ''; ruleEl.value = saved.rule || ''; }
    paintSeg(saved ? saved.decision : '');
  }

  // Diff lines are repository source: `List<String>`, a `<script>` in a JSP, a
  // `"` in an XML attribute. They go in as text nodes and never near markup.
  function paintDiff(pre, lines) {
    lines.forEach(function (line) {
      var cls = '';
      if (line.indexOf('+++') === 0 || line.indexOf('---') === 0) cls = 'meta';
      else if (line.indexOf('@@') === 0) cls = 'hunk';
      else if (line.charAt(0) === '+') cls = 'add';
      else if (line.charAt(0) === '-') cls = 'del';
      var span = node('span', cls);
      span.textContent = line;
      pre.appendChild(span);
    });
  }

  // ─── the rest of the queue, inline ────────────────────────────────────────
  // The leader only cards the first 25 held files; the Review step used to be
  // where you found the others, and there is no Review step now. So this opens
  // them here, as the server already renders them for the static review page:
  // diff, guardrail findings, the same fieldset.decision[data-file][data-pack]
  // the apply bar collects from. It goes in an iframe rather than this document
  // because that HTML arrives with its own stylesheet and its own escaping —
  // srcdoc keeps both out of the page and out of the one markup sink above.
  function moreCard(el, card) {
    html(el, head('More files are waiting')
      + '<p>' + esc(card.shown + ' of ' + card.total + ' held file(s) are carded above.') + '</p>'
      + '<div class="row"><button type="button" class="primary c-more">Show the rest here</button>'
      + '<span class="hint c-morestat"></span></div>'
      + '<div class="c-morewrap" hidden></div>');
    el.querySelector('.c-more').onclick = function () { expandMore(el); };
  }

  async function expandMore(el) {
    var button = el.querySelector('.c-more'), status = el.querySelector('.c-morestat'), wrap = el.querySelector('.c-morewrap');
    if (!button || !wrap || !status) return;
    button.disabled = true;
    status.textContent = 'reading the queue…';
    var v;
    var out = str(S().project.output_dir);
    if (!out) { status.textContent = 'no output directory is bound to this chat yet'; button.disabled = false; return; }
    try { v = await api('GET', '/api/review?output_dir=' + encodeURIComponent(out)); }
    catch (e) { status.textContent = fmtErr(e); button.disabled = false; return; }

    while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
    var frame = node('iframe', 'c-moreframe');
    frame.title = 'The rest of the review queue';
    // See the note above: allow-same-origin keeps contentDocument reachable for
    // trimFrame()/frameRows(); omitting allow-scripts is the point.
    frame.setAttribute('sandbox', 'allow-same-origin');
    frame.onload = function () {
      var left = trimFrame(frame);
      status.textContent = left
        ? left + ' more file(s) — decide here and they join the Apply button below.'
        : 'Nothing else is waiting; every held file is already carded above.';
      applyBar();
    };
    wrap.appendChild(frame);
    wrap.hidden = false;
    // Same srcdoc contract the Review step used: the entries and their stylesheet,
    // both produced by forge/review_queue.py, which escapes as it renders.
    frame.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><style>' + str(v.css)
      + 'body{background:#f4f4f2;padding:12px;margin:0}</style></head><body>' + str(v.entries_html) + '</body></html>';
    button.textContent = 'Read the queue again';
    button.disabled = C.busy;
  }

  // Everything already on screen as a card is hidden inside the frame: one file,
  // one decision — two copies of the same fieldset would be two rows in the apply.
  function trimFrame(frame) {
    var doc = frameDoc(frame);
    if (!doc) return 0;
    var carded = {};
    Object.keys(C.cards).forEach(function (id) {
      var card = C.cards[id].card;
      if (card.kind === 'review_file') carded[str(card.file) + '|' + str(card.pack)] = true;
    });
    var sets = doc.querySelectorAll('fieldset.decision'), left = 0;
    for (var i = 0; i < sets.length; i++) {
      var f = sets[i];
      var section = f.closest('section.entry') || f.parentNode;
      if (carded[str(f.getAttribute('data-file')) + '|' + str(f.getAttribute('data-pack'))]) {
        if (section) { section.hidden = true; section.style.display = 'none'; }
      } else { left++; }
    }
    doc.addEventListener('change', applyBar, true);
    doc.addEventListener('input', applyBar, true);
    return left;
  }

  function frameDoc(frame) {
    try { return frame.contentDocument || null; } catch (e) { return null; }   // never same-origin-denied for srcdoc, but do not bet the page on it
  }

  // Same DOM contract as the static review page's own collect().
  function frameRows() {
    var out = [], frames = document.querySelectorAll('#c-items iframe.c-moreframe');
    for (var i = 0; i < frames.length; i++) {
      var doc = frameDoc(frames[i]);
      if (!doc) continue;
      var sets = doc.querySelectorAll('fieldset.decision');
      for (var j = 0; j < sets.length; j++) {
        var f = sets[j], picked = f.querySelector('input[type=radio]:checked');
        if (!picked || picked.value === 'skip') continue;
        var noteEl = f.querySelector('textarea.note'), ruleEl = f.querySelector('input.rule');
        var row = {
          file: str(f.getAttribute('data-file')), pack: str(f.getAttribute('data-pack')),
          decision: picked.value, note: noteEl ? noteEl.value.trim() : ''
        };
        var rule = ruleEl ? ruleEl.value.trim() : '';
        if (rule) row.rule = rule;
        if (row.file) out.push(row);
      }
    }
    return out;
  }

  // After an apply, the entries in an open frame describe files that may no
  // longer be staged — re-read rather than leave a decided file offering itself.
  function refreshFrames() {
    var wraps = document.querySelectorAll('#c-items .c-morewrap');
    for (var i = 0; i < wraps.length; i++) {
      if (wraps[i].hidden || !wraps[i].firstChild) continue;
      expandMore(wraps[i].parentNode);
    }
  }

  function acceptanceCard(el, card) {
    var results = arr(card.results);
    var rows = results.map(function (r) {
      var lines = arr(r.evidence);
      var ev = lines.length
        ? '<details><summary>' + lines.length + ' evidence line(s)</summary><pre>' + esc(lines.join('\n')) + '</pre></details>' : '';
      return '<tr><td>' + tag(str(r.outcome).toUpperCase()) + '</td><td class="mono">' + esc(r.kind)
        + '</td><td class="mono">' + esc(r.value) + '</td><td class="mono">' + esc(r.scope)
        + '</td><td class="hint">' + esc(r.detail) + ev + '</td></tr>';
    }).join('');
    html(el, head('Acceptance — ' + str(card.pack), card.verdict ? ' ' + tag(str(card.verdict)) : '')
      + (card.verdict
        ? '<div class="totals">' + cell(card.passed, 'passed') + cell(card.failed, 'failed') + cell(card.skipped, 'skipped') + '</div>'
        : '<p class="hint">' + esc(card.skipped_reason || 'skipped') + '</p>')
      + (rows ? '<table class="tbl"><tr><th>Outcome</th><th>Check</th><th>Value</th><th>Scope</th><th>Detail</th></tr>' + rows + '</table>' : '')
      + (card.verdict === 'INCOMPLETE' ? '<p class="hint">INCOMPLETE, never PASS, while any check was skipped.</p>' : ''));
  }

  function testsCard(el, card) {
    var t = obj(card.totals);
    var deps = arr(card.dependencies);
    html(el, head('Generated tests')
      + '<div class="totals">' + cell(t.generated || 0, 'written') + cell(t.held || 0, 'held')
      + cell(t.blocked || 0, 'blocked') + cell(t.tests_passed || 0, 'tests passed')
      + cell(t.tests_failed || 0, 'tests failed') + cell('$' + num(t.cost_usd, 3), 'cost') + '</div>'
      + (deps.length ? '<p class="hint">Add at test scope: ' + deps.map(function (d) { return '<code>' + esc(d) + '</code>'; }).join(', ') + '</p>' : '')
      + (href(card.report_href) ? '<div class="row"><a class="button" target="_blank" rel="noopener" href="' + esc(href(card.report_href)) + '">Test report</a></div>' : ''));
  }

  function feedbackCard(el, card) {
    var packs = arr(card.packs);
    html(el, head('Reviewer notes, grouped')
      + '<div class="totals">' + cell(card.notes || 0, 'notes') + cell(packs.length, 'packs') + '</div>'
      + (packs.length ? '<ol class="chips">' + packs.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ol>' : '')
      + '<p class="hint">A correction that repeats across files is a pack edit waiting to happen.</p>'
      + (card.path ? '<div class="row"><a class="button" target="_blank" rel="noopener" href="'
        + esc(fileUrl('pack-feedback.md')) + '">pack-feedback.md</a><span class="hint mono">' + esc(card.path) + '</span></div>' : ''));
  }

  // What the Artifacts step used to be. `url` comes from /api/artifacts when the
  // tool passes it through; fileUrl() is the fallback, and both point at the
  // output directory the leader bound.
  function artifactsCard(el, card) {
    if (card.output_dir) adopt({ output_dir: str(card.output_dir) });
    var items = arr(card.artifacts);
    var rows = items.map(function (a) {
      var name = str(a.name);
      return '<tr><td>' + esc(a.label || name) + '</td><td class="mono"><a target="_blank" rel="noopener" href="'
        + esc(href(a.url) || fileUrl(name)) + '">' + esc(name) + '</a></td><td class="mono">' + esc(size(a.size))
        + '</td><td class="mono">' + esc(when(a.modified)) + '</td></tr>';
    }).join('');
    html(el, head('Artifacts', card.output_dir ? pathSpan(card.output_dir) : '')
      + '<table class="tbl"><tr><th>Artifact</th><th>File</th><th>Size</th><th>Modified</th></tr>'
      + (rows || '<tr><td colspan="4" class="hint">Nothing in the output directory yet.</td></tr>') + '</table>'
      + '<p class="hint">Each opens in a new tab. None of these are committed when FORGE lands a branch.</p>');
  }

  // The end of the road: the migration is on a branch in the user's own
  // repository. Nothing was pushed — the command to do that is theirs to run.
  // The last build of this output, as the landing confirmation shows it. A
  // failed or stale build never disables Confirm: the decision is the user's.
  function buildLine(b) {
    b = obj(b);
    var outcome = str(b.outcome || 'not_run');
    var label = { pass: 'Build passed', fail: 'Build FAILED', skip: 'Build skipped', not_run: 'Not built yet' }[outcome] || outcome;
    // Colours come from the existing tag classes in app.css.
    var cls = { pass: 'PASS', fail: 'FAIL', skip: 'skip' }[outcome] || 'INCOMPLETE';
    return '<p>' + tag(label, cls) + (b.stale ? ' ' + tag('stale — the output changed since', 'INCOMPLETE') : '')
      + (b.detail ? ' <span class="hint">' + esc(b.detail) + '</span>' : '')
      + (outcome === 'not_run' ? ' <span class="hint">Ask FORGE to build the project first if you want to know it compiles.</span>' : '')
      + '</p>';
  }

  function buildCard(el, card) {
    var steps = arr(card.steps);
    var tail = arr(card.tail);
    var outcome = str(card.outcome);
    html(el, head('Project build', ' ' + tag(outcome.toUpperCase(), { pass: 'PASS', fail: 'FAIL' }[outcome] || 'skip')
      + (card.stale ? ' ' + tag('stale', 'INCOMPLETE') : ''))
      + '<p class="hint">' + esc(card.detail || '') + (card.seconds != null ? ' · ' + esc(card.seconds) + 's' : '')
      + (card.java_home ? ' · JDK <span class="mono">' + esc(card.java_home) + '</span>' : '') + '</p>'
      + (steps.length ? '<ul>' + steps.map(function (s) { return '<li class="mono">' + esc(s) + '</li>'; }).join('') + '</ul>' : '')
      + (tail.length ? '<details' + (card.outcome === 'fail' ? ' open' : '') + '><summary>' + tail.length
        + ' line(s) of build output</summary><pre>' + esc(tail.join('\n')) + '</pre></details>' : ''));
  }

  function landCard(el, card) {
    var packs = arr(card.packs);
    var files = arr(card.files).map(function (f) { return str(f); });
    var over = Number(card.files_truncated || 0);
    if (over > 0) files = files.concat([over + ' more, not listed here']);
    html(el, head('Landed on', pathSpan(card.branch))
      + '<div class="totals">' + cell(card.files_changed || 0, 'files changed')
      + cell(card.deleted || 0, 'deleted') + cell(str(card.commit).slice(0, 10) || '—', 'commit')
      + cell(packs.length, 'packs') + '</div>'
      + (card.source_dir ? '<p class="hint mono">' + esc(card.source_dir) + '</p>' : '')
      + (card.base_branch ? '<p class="hint">Branched from <span class="mono">' + esc(card.base_branch) + '</span>.</p>' : '')
      + (card.excluded ? '<p class="hint">The output folder is inside the repository, so '
          + '<span class="mono">' + esc(card.excluded) + '</span> '
          + (card.exclude_added ? 'was added to' : 'is already in')
          + ' <span class="mono">.git/info/exclude</span> — local to this clone, never committed. Your .gitignore was not touched.</p>' : '')
      + (packs.length ? '<ol class="chips">' + packs.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ol>' : '')
      + block('Files committed', files)
      + block('Files removed', arr(card.deleted_files).map(function (f) { return str(f); }))
      + '<p class="hint">' + esc(card.note || 'nothing was pushed — the branch is local until you push it')
      + ' Nothing was amended and nothing was forced. To push it yourself instead:</p>'
      + '<pre class="c-push"></pre>'
      + '<div class="row"><button type="button" class="c-copy">Copy the command</button><span class="hint c-copied"></span></div>');
    var command = str(card.push_command);
    el.querySelector('.c-push').textContent = command;   // a branch name is user text
    el.querySelector('.c-copy').onclick = function () { copy(command, el.querySelector('.c-copied')); };
  }

  // The one card that links off the machine. The URL is gh's output, so it is
  // only ever an https link set as a property, never markup, and it opens in a
  // new tab with no referrer and no handle back on this page.
  function prCard(el, card) {
    var url = str(card.url);
    var safe = /^https:\/\/[^\s"'<>]+$/.test(url) ? url : '';
    html(el, head(card.existing ? 'Pull request already open' : 'Pull request opened')
      + '<p><span class="mono">' + esc(card.branch) + '</span> into <span class="mono">' + esc(card.base) + '</span>'
      + (card.title ? ' — ' + esc(card.title) : '') + '</p>'
      + '<p><a class="c-prlink" target="_blank" rel="noopener noreferrer"></a></p>'
      + (card.build ? buildLine(card.build) : '')
      + '<p class="hint">FORGE pushed this one branch to origin, without forcing, and wrote the description from its own records.</p>');
    var a = el.querySelector('.c-prlink');
    if (safe) { a.href = safe; a.textContent = safe; } else { a.textContent = url || '(no link came back)'; }
  }

  function copy(text, said) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { said.textContent = 'copied'; },
          function () { said.textContent = 'select the line above and copy it'; });
        return;
      }
    } catch (e) { /* no clipboard in this context */ }
    said.textContent = 'select the line above and copy it';
  }

  function size(bytes) {
    var n = Number(bytes || 0);
    if (!isFinite(n) || n < 0) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function when(seconds) {
    var t = Number(seconds || 0) * 1000;
    if (!t || !isFinite(t)) return '';
    try { return new Date(t).toLocaleString(); } catch (e) { return ''; }
  }

  // ─── the review queue, and which cards may still act on it ────────────────
  async function reconcileQueue() {
    var p = S().project;
    if (!p.source_dir || !p.output_dir) { C.queue = { run: '', keys: {} }; return; }
    try {
      var v = await api('GET', '/api/review?output_dir=' + encodeURIComponent(p.output_dir));
      var keys = {};
      arr(v.entries).forEach(function (e) { keys[str(e.rel_path) + '|' + str(e.pack)] = true; });
      C.queue = { run: str(v.run), keys: keys };
    } catch (e) {
      // No queue, or one this build cannot read. Either way nothing on screen
      // describes a file that is still staged, so nothing may be applied.
      C.queue = { run: '', keys: {} };
    }
  }

  function applicable(card) {
    if (!card || card.kind !== 'review_file') return false;
    if (!C.queue.run || str(card.run) !== C.queue.run) return false;
    return C.queue.keys[str(card.file) + '|' + str(card.pack)] === true;
  }

  function refreshOne(rec) {
    if (!rec || rec.card.kind !== 'review_file') return;
    var ok = applicable(rec.card);
    var box = rec.el.querySelector('.c-decide');
    var stale = rec.el.querySelector('.c-stalenote');
    if (!box || !stale) return;
    box.hidden = !ok;
    stale.hidden = ok;
    rec.el.classList.toggle('c-stale', !ok);
    if (!ok) {
      delete C.decisions[rec.id];
      stale.textContent = rec.card.run && rec.card.run !== C.queue.run
        ? 'Stale — this card is from run ' + rec.card.run + ' and the review queue has moved on. Ask for the held files again.'
        : 'This file is no longer in the review queue. Ask for the held files again.';
    }
  }

  function refreshCards() {
    Object.keys(C.cards).forEach(function (id) { refreshOne(C.cards[id]); });
    applyBar();
  }

  function resolveCard(d) {
    Object.keys(C.cards).forEach(function (id) {
      var rec = C.cards[id];
      if (rec.card.kind !== 'review_file' || str(rec.card.file) !== str(d.file)) return;
      var out = rec.el.querySelector('.c-outcome');
      if (out) {
        out.hidden = false;
        out.textContent = str(d.decision) + ' — ' + (d.applied ? 'applied' : 'not applied')
          + ' · now ' + str(d.status_after) + (d.detail ? ' · ' + str(d.detail) : '');
      }
      delete C.decisions[id];
      delete C.queue.keys[str(rec.card.file) + '|' + str(rec.card.pack)];
      refreshOne(rec);
    });
    applyBar();
  }

  // ─── the apply bar ────────────────────────────────────────────────────────
  // One apply, one run: every row here belongs to the queue reconcileQueue() last
  // read, whether it came from a card or from an expanded queue frame.
  function chosen() {
    var rows = [], seen = {};
    Object.keys(C.decisions).forEach(function (id) {
      var rec = C.cards[id], d = C.decisions[id];
      if (!rec || !d || !applicable(rec.card)) return;
      var row = { file: rec.card.file, pack: rec.card.pack, decision: d.decision, note: d.note || '' };
      if (d.rule) row.rule = d.rule;
      seen[str(row.file) + '|' + str(row.pack)] = true;
      rows.push(row);
    });
    if (C.queue.run) {
      frameRows().forEach(function (row) {
        var key = row.file + '|' + row.pack;
        if (seen[key] || C.queue.keys[key] !== true) return;
        seen[key] = true;
        rows.push(row);
      });
    }
    return { run: C.queue.run, rows: rows };
  }

  // ─── approve all ──────────────────────────────────────────────────────────
  // Every file still waiting on a human: the live cards on the current queue,
  // and the rows of any expanded queue frame. The box only SELECTS approve on
  // them — Apply is still the one click that writes anything.
  function waitingCards() {
    return Object.keys(C.cards).filter(function (id) { return applicable(C.cards[id].card); });
  }

  function frameSets() {
    var out = [], frames = document.querySelectorAll('#c-items iframe.c-moreframe');
    for (var i = 0; i < frames.length; i++) {
      var doc = frameDoc(frames[i]);
      if (!doc) continue;
      var sets = doc.querySelectorAll('fieldset.decision');
      for (var j = 0; j < sets.length; j++) {
        var f = sets[j], section = f.closest('section.entry');
        // A row hidden because it already has a card is that card's file, not a second one.
        if (section && section.hidden) continue;
        if (C.queue.keys[str(f.getAttribute('data-file')) + '|' + str(f.getAttribute('data-pack'))] !== true) continue;
        out.push(f);
      }
    }
    return out;
  }

  function radioIn(set, value) { return set.querySelector('input[type=radio][value="' + value + '"]'); }

  function approveAllChange() {
    var on = $('c-all').checked;
    waitingCards().forEach(function (id) {
      var seg = C.cards[id].el.querySelector('.c-seg');
      if (!seg || !seg.pick) return;
      var cur = C.decisions[id] ? C.decisions[id].decision : '';
      if (on) seg.pick('approve');
      else if (cur === 'approve') seg.pick('');   // leave a reject or retry alone
    });
    frameSets().forEach(function (set) {
      var approve = radioIn(set, 'approve'), skip = radioIn(set, 'skip');
      if (on && approve) approve.checked = true;
      else if (!on && approve && approve.checked && skip) skip.checked = true;
    });
    applyBar();
  }

  function paintAll() {
    var box = $('c-all');
    if (!box) return 0;
    var cards = waitingCards(), sets = frameSets(), total = cards.length + sets.length;
    var all = total > 0
      && cards.every(function (id) { return C.decisions[id] && C.decisions[id].decision === 'approve'; })
      && sets.every(function (set) { var a = radioIn(set, 'approve'); return !a || a.checked; });
    box.checked = all;
    box.disabled = !total || C.busy;
    $('c-all-label').textContent = 'Approve all ' + total + ' file' + (total === 1 ? '' : 's') + ' waiting';
    return total;
  }

  function applyBar() {
    var bar = $('c-applybar');
    if (!bar) return;
    var pick = chosen();
    var n = pick.rows.length;
    var waiting = paintAll();
    $('c-apply-n').textContent = n + ' decision' + (n === 1 ? '' : 's') + ' ready';
    $('c-apply').disabled = !n || C.busy;
    $('c-clear').disabled = !n || C.busy;
    $('c-apply-hint').textContent = n
      ? 'run ' + pick.run + ' — approve writes the staged file, reject discards it, retry re-runs it with your note.'
      : '';
    bar.hidden = !n && !waiting;
    // chat.css fades the last 22px of the transcript into the composer. The bar
    // is opaque and sits in exactly that strip, so the fade comes off while it
    // is up rather than dimming the button that applies the decisions.
    var pane = document.querySelector('.c-pane');
    if (pane) pane.classList.toggle('c-hasbar', !bar.hidden);
  }

  function applyClick() {
    var pick = chosen();
    if (!pick.rows.length || C.busy) return;
    act({ type: 'apply_decisions', run: pick.run, decisions: pick.rows });
  }

  // Clear puts every card and every expanded queue row back to "no decision".
  // It never posts — the bar collects, it does not remember.
  function clearClick() {
    if (C.busy) return;
    C.decisions = {};
    Object.keys(C.cards).forEach(function (id) {
      var seg = C.cards[id].el.querySelector('.c-seg');
      if (seg && seg.reset) seg.reset();
    });
    var frames = document.querySelectorAll('#c-items iframe.c-moreframe');
    for (var i = 0; i < frames.length; i++) {
      var doc = frameDoc(frames[i]);
      if (!doc) continue;
      var sets = doc.querySelectorAll('fieldset.decision');
      for (var j = 0; j < sets.length; j++) {
        var skip = sets[j].querySelector('input[type=radio][value="skip"]');
        if (skip) skip.checked = true;
        var note = sets[j].querySelector('textarea.note');
        if (note) note.value = '';
      }
    }
    applyBar();
  }

  // ─── the rail's three numbers ─────────────────────────────────────────────
  function spend(pipeline, leader) {
    var el = $('c-spend');
    if (!el) return;
    if (pipeline == null && leader == null) { el.hidden = true; return; }
    pipeline = Number(pipeline || 0);
    leader = Number(leader || 0);
    el.hidden = false;
    html(el, '<b>$' + esc(num(pipeline + leader, 3)) + '</b><span>spent in this chat</span>'
      + '<span class="hint">pipeline $' + esc(num(pipeline, 3)) + ' · leader $' + esc(num(leader, 4)) + '</span>');
  }

  function paintPacks() {
    var el = $('c-packs');
    if (!el) return;
    var n = Object.keys(C.packs).length;
    el.hidden = !n;
    if (n) html(el, '<b>' + esc(n) + '</b><span>pack' + (n === 1 ? '' : 's') + ' completed</span>');
  }

  // #cwd is app.js's: health() writes the server's working directory into it,
  // because that is what a relative output_dir resolves against. Once the leader
  // has bound a repository, THAT is the more useful line — so this claims the
  // element with data-project, and health() leaves it alone while the flag is
  // set. Releasing the flag hands it back on the next poll.
  function railPath() {
    var el = $('cwd');
    if (!el) return;
    var lab = $('cwd-lab');
    var p = str(S().project.source_dir);
    if (!p) {
      // Hand the line back to health(), which labels it as the server's own
      // working directory — unlabelled it read as a bound project.
      if (el.getAttribute('data-project')) { el.removeAttribute('data-project'); window.FORGE.health(); }
      return;
    }
    el.setAttribute('data-project', '1');
    if (lab) { lab.textContent = 'repository'; lab.hidden = false; }
    window.FORGE.setPath(el, p);
  }

  window.ForgeChat = { render: render };
})();
