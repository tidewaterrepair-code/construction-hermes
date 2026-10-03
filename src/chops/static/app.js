// Progressive enhancement only; every action works without JavaScript.
document.addEventListener("submit", function (ev) {
  var form = ev.target;
  var msg = form.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { ev.preventDefault(); return; }
  var btn = form.querySelector("button[type=submit],button:not([type])");
  if (btn) { setTimeout(function () { btn.disabled = true; btn.textContent = "Working…"; }, 0); }
});
