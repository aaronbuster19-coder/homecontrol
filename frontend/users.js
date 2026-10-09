"use strict";
// Accounts and roles. GET /api/me -> {user, role, expires, owner}; the role goes on <html data-role> and style.css hides
// what that role can't use (the server enforces every route itself: backend/roles.py). ⋯ → Users… (admins: add, remove,
// reset, change role / guest expiry) or Account… (everyone else: change your own password). Last role is remembered so
// the page doesn't flash admin controls before /api/me answers.
(() => {  // everything private: the other scripts share one global scope
  const usr = { me: null, list: [], msg: "", err: false, busy: false };
  const ROLE_LABEL = { admin: "Admin", member: "Member", guest: "Guest" };
  const ROLE_HINT = { admin: "everything, including settings and users", member: "controls every device, no settings",
    guest: "lights only" };
  const EXPIRY_CHOICES = [["", "Never"], ["86400", "1 day"], ["259200", "3 days"], ["604800", "1 week"], ["2592000", "1 month"]];
  const ux = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function setRole(role) {
    if (!ROLE_LABEL[role]) return;
    document.documentElement.dataset.role = role;
    try { localStorage.setItem("hc.role", role); } catch {}
    const b = $("usersBtn"); b.hidden = false; b.textContent = role === "admin" ? "Users…" : "Account…";
  }
  try { const r = localStorage.getItem("hc.role"); if (ROLE_LABEL[r]) setRole(r); } catch {}

  async function loadMe() {
    try { usr.me = await api("/api/me"); setRole(usr.me.role); } catch {}
  }
  loadMe();

  const fmtWhen = (s) => new Date(s * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  function suggestPassword() {
    const a = new Uint8Array(12); crypto.getRandomValues(a);
    const abc = "abcdefghijkmnpqrstuvwxyz23456789";
    const s = [...a].map((n) => abc[n % abc.length]).join("");
    return `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8)}`;
  }

  // ---------- sheet ----------
  function usersSheet() {
    let sh = $("usersSheet");
    if (sh) return sh;
    sh = ux("div", "sheet"); sh.id = "usersSheet"; sh.hidden = true;
    const body = ux("div", "sheet-body"), close = ux("button", "close", "×");
    close.id = "usersClose"; close.setAttribute("aria-label", "Close"); close.onclick = () => { sh.hidden = true; };
    const content = ux("div"); content.id = "usersContent";
    body.append(close, content); sh.appendChild(body);
    sh.addEventListener("click", (e) => { if (e.target === sh) sh.hidden = true; });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") sh.hidden = true; });
    document.body.appendChild(sh);
    return sh;
  }
  async function openUsers() {
    usr.msg = ""; usr.err = false;
    usersSheet().hidden = false;
    await loadMe();
    if (usr.me?.role === "admin") await loadUsers(); else drawUsers();
  }
  async function loadUsers() {
    try { usr.list = (await api("/api/users")).users; } catch (e) { say(e.message, true); }
    drawUsers();
  }
  function say(msg, err = false) { usr.msg = msg; usr.err = err; const m = $("usersMsg"); if (m) { m.textContent = msg; m.classList.toggle("warn", err); } }
  async function act(fn, ok) {
    if (usr.busy) return; usr.busy = true;
    try { await fn(); say(ok); } catch (e) { say(e.message, true); }
    finally { usr.busy = false; }
    if (usr.me?.role === "admin") await loadUsers(); else drawUsers();
  }

  function drawUsers() {
    const c = $("usersContent"); if (!c) return; c.replaceChildren();
    const me = usr.me || {}, admin = me.role === "admin";
    c.appendChild(ux("h3", null, admin ? "Users" : "Account"));
    const who = ux("div", "sub users-me");
    who.append("Signed in as ", ux("b", null, me.user || "?"), ` · ${ROLE_LABEL[me.role] || ""}`);
    if (me.expires) who.append(` · until ${fmtWhen(me.expires)}`);
    c.appendChild(who);
    const msg = ux("p", "hint users-msg" + (usr.err ? " warn" : ""), usr.msg); msg.id = "usersMsg"; msg.setAttribute("role", "status"); c.appendChild(msg);
    if (admin) { drawList(c, me); drawAdd(c); }
    drawOwnPassword(c, me);
  }

  function drawList(c, me) {
    const ul = ux("ul", "users-list"); ul.id = "usersList";
    for (const u of usr.list) {
      const li = ux("li", "user-row" + (u.expired ? " expired" : "")); li.dataset.user = u.username;
      const top = ux("div", "user-top");
      const name = ux("span", "user-name", u.username);
      const self = u.username.toLowerCase() === (me.user || "").toLowerCase();
      if (self) name.appendChild(ux("span", "user-tag", "you"));
      top.appendChild(name);
      const role = ux("select", "user-role"); role.setAttribute("aria-label", `Role of ${u.username}`);
      for (const r of ["admin", "member", "guest"]) { const o = ux("option", null, ROLE_LABEL[r]); o.value = r; role.appendChild(o); }
      role.value = u.role; role.disabled = u.owner || self;
      role.onchange = () => act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PATCH", body: JSON.stringify({ role: role.value }) }),
        `${u.username} is now ${ROLE_LABEL[role.value].toLowerCase()}`);
      top.appendChild(role);
      li.appendChild(top);
      const info = ux("div", "sub user-info");
      info.textContent = u.owner ? "Set by APP_USER / APP_PASSWORD in .env" : u.role === "guest"
        ? (u.expired ? "Expired " + fmtWhen(u.expires) : u.expires ? `Until ${fmtWhen(u.expires)}` : "No expiry") : ROLE_HINT[u.role];
      li.appendChild(info);
      if (!u.owner && !self) {
        const btns = ux("div", "user-btns");
        if (u.role === "guest") {
          const ex = ux("select", "user-expiry"); ex.setAttribute("aria-label", `Expiry of ${u.username}`);
          const cur = ux("option", null, u.expires ? (u.expired ? "Expired" : "Keep expiry") : "No expiry"); cur.value = "keep"; ex.appendChild(cur);
          for (const [v, t] of EXPIRY_CHOICES) { const o = ux("option", null, v ? `${t} from now` : "Never expires"); o.value = v || "never"; ex.appendChild(o); }
          ex.onchange = () => {
            if (ex.value === "keep") return;
            const expires = ex.value === "never" ? null : Date.now() / 1000 + Number(ex.value);
            act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "PATCH", body: JSON.stringify({ expires }) }),
              expires ? `${u.username} can sign in until ${fmtWhen(expires)}` : `${u.username} no longer expires`);
          };
          btns.appendChild(ex);
        }
        const reset = ux("button", "user-reset", "Reset password"); reset.type = "button";
        reset.onclick = () => {
          const pw = prompt(`New password for ${u.username} (at least 8 characters). They will be signed out everywhere.`, suggestPassword());
          if (pw == null) return;
          act(() => api(`/api/users/${encodeURIComponent(u.username)}/password`, { method: "PUT", body: JSON.stringify({ password: pw }) }),
            `New password set for ${u.username} — they're signed out everywhere`);
        };
        const del = ux("button", "danger user-remove", "Remove"); del.type = "button";
        del.onclick = () => {
          if (!confirm(`Remove ${u.username}? They are signed out at once.`)) return;
          act(() => api(`/api/users/${encodeURIComponent(u.username)}`, { method: "DELETE" }), `${u.username} removed`);
        };
        btns.append(reset, del);
        li.appendChild(btns);
      }
      ul.appendChild(li);
    }
    c.appendChild(ul);
  }

  function field(label, input) { const l = ux("label", "users-field"); l.append(ux("span", null, label), input); return l; }

  function drawAdd(c) {
    const sec = ux("section", "auto-sec users-add");
    sec.appendChild(ux("h4", null, "Add someone"));
    const f = ux("form"); f.id = "userAddForm"; f.autocomplete = "off";
    const name = ux("input"); name.name = "username"; name.required = true; name.maxLength = 32; name.autocapitalize = "none"; name.spellcheck = false;
    name.pattern = "[A-Za-z0-9][A-Za-z0-9._@\\-]{0,31}";
    const pw = ux("input"); pw.name = "password"; pw.required = true; pw.minLength = 8; pw.maxLength = 256; pw.value = suggestPassword();
    pw.autocomplete = "new-password"; pw.spellcheck = false;
    const role = ux("select"); role.name = "role";
    for (const r of ["member", "guest", "admin"]) { const o = ux("option", null, `${ROLE_LABEL[r]} — ${ROLE_HINT[r]}`); o.value = r; role.appendChild(o); }
    const exp = ux("select"); exp.name = "expires";
    for (const [v, t] of EXPIRY_CHOICES) { const o = ux("option", null, t); o.value = v; exp.appendChild(o); }
    const expRow = field("Expires", exp); expRow.hidden = true;
    role.onchange = () => { expRow.hidden = role.value !== "guest"; };
    const go = ux("button", "primary", "Add"); go.type = "submit";
    f.append(field("Username", name), field("Password", pw), field("Role", role), expRow, go);
    f.onsubmit = (e) => {
      e.preventDefault();
      const body = { username: name.value.trim(), password: pw.value, role: role.value };
      if (role.value === "guest" && exp.value) body.expires = Date.now() / 1000 + Number(exp.value);
      act(() => api("/api/users", { method: "POST", body: JSON.stringify(body) }),
        `${body.username} added — give them the password: ${body.password}`);
    };
    sec.appendChild(f); c.appendChild(sec);
  }

  function drawOwnPassword(c, me) {
    const sec = ux("section", "auto-sec users-own");
    sec.appendChild(ux("h4", null, "Your password"));
    if (me.owner) { sec.appendChild(ux("p", "hint", "This account's password is APP_PASSWORD in the server's .env file.")); c.appendChild(sec); return; }
    const f = ux("form"); f.id = "ownPwForm";
    const cur = ux("input"); cur.type = "password"; cur.name = "current"; cur.required = true; cur.autocomplete = "current-password";
    const nw = ux("input"); nw.type = "password"; nw.name = "password"; nw.required = true; nw.minLength = 8; nw.autocomplete = "new-password";
    const go = ux("button", "primary", "Change password"); go.type = "submit";
    f.append(field("Current password", cur), field("New password (8+ characters)", nw), go);
    f.onsubmit = (e) => {
      e.preventDefault();
      act(() => api("/api/me/password", { method: "POST", body: JSON.stringify({ current: cur.value, password: nw.value }) }),
        "Password changed — your other devices are signed out");
    };
    sec.appendChild(f); c.appendChild(sec);
  }

  $("usersBtn").addEventListener("click", openUsers);

  // Members see the alert settings but only an admin can change them.
  (() => {
    const s = $("alertSheet")?.querySelector(".sheet-body .sub");
    if (s) s.after(ux("p", "hint role-note", "Only an admin can change these settings. You can still turn alerts on for this device."));
  })();
})();
