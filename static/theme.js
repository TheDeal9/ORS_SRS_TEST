/* Runs in <head> before first paint: restores the saved theme and marks the page as script-enabled
   (so elements that animate in start hidden only when the animation can actually run). */
(function () {
  var root = document.documentElement, saved = null;
  root.classList.add('js');
  try { saved = localStorage.getItem('theme'); } catch (e) { /* private mode: follow the system */ }
  if (saved === 'dark' || saved === 'light') root.dataset.theme = saved;
})();
