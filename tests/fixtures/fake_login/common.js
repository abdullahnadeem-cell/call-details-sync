// Shared helpers for the fake Google sign-in pages used by tests only.
const Q = new URLSearchParams(location.search);
const MODE = Q.get("mode") || "ok";
function go(page) { location.href = page + "?mode=" + encodeURIComponent(MODE); }
function showError(id, msg) { const e = document.getElementById(id); e.textContent = msg; e.style.display = "block"; }
