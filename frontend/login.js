"use strict";
(() => {
  const form = document.getElementById("loginForm");
  const err = document.getElementById("loginError");
  const btn = form.querySelector("button");
  fetch("/api/me").then((r) => { if (r.ok) location.replace("/"); }).catch(() => {});
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    const fd = new FormData(form);
    try {
      const r = await fetch("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: fd.get("username"), password: fd.get("password") }),
      });
      if (r.ok) { location.replace("/"); return; }
      err.textContent = r.status === 429 ? "Too many attempts. Try again in a few minutes."
        : r.status === 401 ? "Wrong username or password." : `Sign-in failed (${r.status}).`;
    } catch { err.textContent = "Can't reach the server. Are you offline?"; }
    btn.disabled = false;
    form.password.select();
  });
})();
