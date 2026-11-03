/* Behaviour for the admin pages. Loaded with `defer`; everything is opt-in through data- attributes
   so the templates need no inline script (the Content-Security-Policy forbids it). */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var M = window.Motion;
  var canAnimate = !!(M && M.animate) && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var root = document.documentElement;

  /* ---- theme toggle: saved choice wins, otherwise the OS setting ---- */
  function theme() { return root.dataset.theme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
  $$('[data-theme-toggle]').forEach(function (b) {
    function label() { var dark = theme() === 'dark'; b.textContent = dark ? 'Light mode' : 'Dark mode'; b.setAttribute('aria-pressed', String(dark)); }
    label();
    b.addEventListener('click', function () {
      var next = theme() === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('theme', next); } catch (e) { /* not saved, still applied */ }
      label();
    });
  });

  /* ---- small behaviours ---- */
  $$('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var done = function () { b.textContent = 'Link copied'; };
      if (navigator.clipboard) navigator.clipboard.writeText(b.dataset.copy).then(done, function () { window.prompt('Copy this link:', b.dataset.copy); });
      else window.prompt('Copy this link:', b.dataset.copy);
    });
  });
  $$('[data-print]').forEach(function (b) { b.addEventListener('click', function () { window.print(); }); });
  $$('[data-select]').forEach(function (i) { i.addEventListener('focus', function () { i.select(); }); });
  $$('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  $$('form[data-clean]').forEach(function (f) {  // keep the address bar short: leave blank and default fields out of the URL
    f.addEventListener('submit', function () {
      Array.prototype.forEach.call(f.elements, function (el) { if (el.name && (el.value === '' || (el.name === 'sort' && el.value === 'new'))) el.disabled = true; });
    });
  });
  $$('details.filters').forEach(function (d) {  // close the filter panel on outside click or Escape
    document.addEventListener('click', function (e) { if (d.open && !d.contains(e.target)) d.open = false; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && d.open) { d.open = false; $('summary', d).focus(); } });
  });

  /* ---- slide-over drawer (native <dialog>: focus trap, Esc and backdrop come free) ---- */
  $$('dialog.drawer').forEach(function (dlg) {
    function open() {
      dlg.showModal();
      if (canAnimate) M.animate(dlg, { transform: ['translateX(100%)', 'translateX(0%)'] }, { duration: 0.28, ease: 'easeOut' });
      var first = $('input', dlg); if (first) first.focus();
    }
    function close() {
      if (!canAnimate) { dlg.close(); return; }
      M.animate(dlg, { transform: ['translateX(0%)', 'translateX(100%)'] }, { duration: 0.2, ease: 'easeIn' }).finished.then(function () { dlg.close(); dlg.style.transform = ''; });
    }
    $$('[data-open="' + dlg.id + '"]').forEach(function (b) { b.addEventListener('click', open); });
    $$('[data-close]', dlg).forEach(function (b) { b.addEventListener('click', close); });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) close(); });
    dlg.addEventListener('cancel', function (e) { e.preventDefault(); close(); });
    if (dlg.hasAttribute('data-autoopen')) open();
  });

  /* ---- report tabs ---- */
  $$('[data-tabs]').forEach(function (group) {
    var tabs = $$('[data-tab]', group), panels = $$('[data-panel]', group);
    tabs.forEach(function (t) {
      t.addEventListener('click', function () {
        tabs.forEach(function (x) { x.classList.toggle('active', x === t); x.setAttribute('aria-selected', String(x === t)); });
        panels.forEach(function (p) { p.classList.toggle('hidden', p.dataset.panel !== t.dataset.tab); });
      });
    });
  });

  /* ---- live refresh: the client page reloads itself when a result or link change arrives ---- */
  var live = $('#linkpanel');
  if (live) {
    var state = live.dataset.state;
    setInterval(function () {
      fetch(live.dataset.url, { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.state !== state) window.location.reload(); }).catch(function () { /* offline: try again next tick */ });
    }, 5000);
  }

  /* ---- tooltips: any element with data-tip, on hover and on keyboard focus. Text only, never HTML. ---- */
  var tip = document.createElement('div');
  tip.className = 'tip hidden'; tip.setAttribute('role', 'tooltip');
  document.body.appendChild(tip);
  function showTip(el, x, y) {
    tip.textContent = '';
    el.getAttribute('data-tip').split('\n').forEach(function (line) { var d = document.createElement('div'); d.textContent = line; tip.appendChild(d); });
    tip.classList.remove('hidden');
    var r = tip.getBoundingClientRect();
    var left = Math.max(8, Math.min(x + 14, window.innerWidth - r.width - 8));
    var top = y + 18 + r.height > window.innerHeight - 8 ? y - r.height - 12 : y + 18;
    tip.style.left = left + 'px'; tip.style.top = Math.max(8, top) + 'px';
  }
  function hideTip() { tip.classList.add('hidden'); }
  function tipTarget(e) { return e.target.closest ? e.target.closest('[data-tip]') : null; }
  document.addEventListener('pointermove', function (e) { var t = tipTarget(e); if (t) showTip(t, e.clientX, e.clientY); else hideTip(); });
  document.addEventListener('pointerleave', hideTip);
  document.addEventListener('focusin', function (e) {
    var t = tipTarget(e); if (!t) return;
    var b = t.getBoundingClientRect(); showTip(t, b.left + b.width / 2, b.top + b.height / 2);
  });
  document.addEventListener('focusout', hideTip);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideTip(); });

  /* ---- motion (Motion for JavaScript, vendored). Skipped for reduced-motion; content is never left hidden. ---- */
  function show(els) { els.forEach(function (e) { e.style.opacity = 1; }); }
  var cards = $$('.reveal:not(tr)'), rows = $$('tr.reveal');
  if (!canAnimate) { show(cards.concat(rows)); return; }
  try {
    M.animate(cards, { opacity: [0, 1], transform: ['translateY(12px)', 'translateY(0px)'] }, { duration: 0.45, delay: M.stagger(0.06), ease: 'easeOut' });
    M.animate(rows, { opacity: [0, 1] }, { duration: 0.35, delay: M.stagger(0.035, { startDelay: 0.15 }) });
    $$('svg.chart .series').forEach(function (line) {  // draw the line from left to right
      var len = line.getTotalLength();
      line.style.strokeDasharray = len;
      M.animate(line, { strokeDashoffset: [len, 0] }, { duration: 0.9, delay: 0.2, ease: 'easeOut' }).finished.then(function () { line.style.strokeDasharray = ''; line.style.strokeDashoffset = ''; });
    });
    $$('svg.chart').forEach(function (svg) {  // then the markers pop in
      M.animate($$('.pt', svg), { opacity: [0, 1] }, { duration: 0.3, delay: M.stagger(0.07, { startDelay: 0.45 }) });
    });
  } catch (err) { show(cards.concat(rows)); }
})();
