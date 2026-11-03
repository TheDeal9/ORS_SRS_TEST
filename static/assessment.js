/* Client-facing ORS/SRS page. The browser only reports where each mark sits (0 to 1 along the line);
   the server turns that into a score and never sends it back. Mouse, pen, touch and keyboard all work. */
(function () {
  'use strict';
  var body = document.body;
  var token = body.dataset.token;
  var pending = body.dataset.pending.split(',').filter(Boolean);
  var definitions = {
    ORS: [{ name: 'Individually', hint: 'Personal well-being' }, { name: 'Interpersonally', hint: 'Family, close relationships' },
          { name: 'Socially', hint: 'Work, school, friendships' }, { name: 'Overall', hint: 'General sense of well-being' }],
    SRS: [{ name: 'Relationship', left: 'I did not feel heard, understood, and respected.', right: 'I felt heard, understood, and respected.' },
          { name: 'Goals and Topics', left: 'We did not work on or talk about what I wanted to work on and talk about.', right: 'We worked on and talked about what I wanted to work on and talk about.' },
          { name: 'Approach or Method', left: 'approach is not a good fit for me.', right: 'approach is a good fit for me.' },
          { name: 'Overall', left: 'There was something missing in the session today.', right: 'Overall, today’s session was right for me.' }]
  };
  var currentType = pending[0];
  var marks = { ORS: {}, SRS: {} };
  var drawing = null;
  var itemsEl = document.getElementById('items'), titleEl = document.getElementById('formTitle'),
      introEl = document.getElementById('intro'), messageEl = document.getElementById('message');

  function say(text, kind) { messageEl.textContent = text; messageEl.className = 'toast ' + kind; }
  function hideMessage() { messageEl.className = 'toast hidden'; }
  function paint(parts, p) { parts.stroke.style.width = (p * 100) + '%'; parts.stop.style.left = (p * 100) + '%'; parts.stop.style.display = 'block'; }

  function render() {
    var isOrs = currentType === 'ORS';
    titleEl.textContent = isOrs ? 'Outcome Rating Scale (ORS)' : 'Session Rating Scale (SRS V.3.1)';
    introEl.textContent = isOrs ? 'Looking back over the last week, including today, rate how you have been doing in the following areas.'
      : 'Please rate today’s session by placing a mark on each line nearest to the description that best fits your experience.';
    itemsEl.textContent = '';
    definitions[currentType].forEach(function (it) {
      var item = document.createElement('section');
      item.className = 'item' + (isOrs ? '' : ' srs-item');
      item.innerHTML = '<h3>' + it.name + '</h3>' +
        (isOrs ? '<div class="small">' + it.hint + '</div>' : '<div class="srs-descriptions"><div class="left">' + it.left + '</div><div class="right">' + it.right + '</div></div>') +
        '<div class="scale" tabindex="0" role="group" aria-roledescription="rating line" aria-label="' + it.name +
        '. Drag along the line, or use the left and right arrow keys to place your mark."><div class="base"></div>' +
        '<div class="endpoint l"></div><div class="endpoint r"></div><div class="ticks">' + '<span></span>'.repeat(11) +
        '</div><div class="stroke"></div><div class="stop"></div></div><div class="status" aria-live="polite"></div>';
      var scale = item.querySelector('.scale'), status = item.querySelector('.status');
      var parts = { stroke: item.querySelector('.stroke'), stop: item.querySelector('.stop') };
      var set = function (p) { p = Math.max(0, Math.min(1, p)); marks[currentType][it.name] = p; paint(parts, p); status.textContent = 'Mark recorded'; hideMessage(); };
      var fraction = function (e) { var r = scale.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };

      scale.addEventListener('pointerdown', function (e) {
        if (e.button !== undefined && e.button !== 0) return;
        drawing = scale; scale.setPointerCapture(e.pointerId);
        paint(parts, fraction(e)); status.textContent = ''; e.preventDefault();
      });
      scale.addEventListener('pointermove', function (e) { if (drawing === scale) { paint(parts, fraction(e)); e.preventDefault(); } });
      scale.addEventListener('pointerup', function (e) { if (drawing === scale) { set(fraction(e)); drawing = null; e.preventDefault(); } });
      scale.addEventListener('pointercancel', function () { if (drawing === scale) { drawing = null; var m = marks[currentType][it.name]; if (m === undefined) parts.stop.style.display = 'none'; else paint(parts, m); } });
      scale.addEventListener('keydown', function (e) {
        var cur = marks[currentType][it.name], p = cur === undefined ? 0 : cur, step = e.shiftKey ? 0.1 : 0.02;
        if (e.key === 'ArrowRight' || e.key === 'ArrowUp') p += step;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') p -= step;
        else if (e.key === 'Home') p = 0;
        else if (e.key === 'End') p = 1;
        else return;
        e.preventDefault(); set(p);
      });

      var m = marks[currentType][it.name];
      if (m === undefined) status.textContent = 'Not marked'; else { paint(parts, m); status.textContent = 'Mark recorded'; }
      itemsEl.appendChild(item);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
      b.classList.toggle('active', b.dataset.type === currentType);
      b.classList.toggle('hidden', pending.indexOf(b.dataset.type) < 0);
    });
    document.querySelector('.tabs').classList.toggle('hidden', pending.length < 2);
    hideMessage();
  }

  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
    b.addEventListener('click', function () { if (pending.indexOf(b.dataset.type) >= 0) { currentType = b.dataset.type; render(); } });
  });
  document.getElementById('resetBtn').addEventListener('click', function () { marks[currentType] = {}; render(); });

  document.getElementById('saveBtn').addEventListener('click', function () {
    var names = definitions[currentType].map(function (x) { return x.name; });
    if (names.some(function (k) { return marks[currentType][k] === undefined; })) { say('Please mark all four scales before finishing.', 'warn'); return; }
    var btn = document.getElementById('saveBtn');
    btn.disabled = true; btn.textContent = 'Saving…';
    fetch('/api/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, type: currentType, positions: names.map(function (k) { return marks[currentType][k]; }) }) })
      .then(function (res) { return res.json().then(function (data) { if (!res.ok) throw new Error(data.error || 'Unable to save'); return data; }); })
      .then(function () {
        pending = pending.filter(function (t) { return t !== currentType; });
        marks[currentType] = {};
        if (!pending.length) {
          document.querySelector('.panel').innerHTML = '<h1>Thank you</h1><p class="notice">Your responses have been saved. You can now close this page.</p>';
          return;
        }
        currentType = pending[0]; render(); say('Saved. Please complete the next form.', 'ok');
      })
      .catch(function (err) { say(err.message, 'err'); })
      .then(function () { btn.disabled = false; btn.textContent = 'Finish assessment'; });
  });
  render();
})();
