"use strict";
// ⋯ → Guest links… (admins): make a time-limited link + QR code (qr.js, drawn here in the browser) that lets a visitor
// switch one room's lights, or chosen lights / plugs, and nothing else; see who's got one; revoke. The server keeps only
// a hash of each link (backend/guest_links.py), so a new link is shown once, right after it's made.
(() => {
  const gl = { links: [], created: null, msg: "", err: false, busy: false };
  const DURATIONS = [[120, "2 hours"], [480, "8 hours"], [1440, "1 day"], [4320, "3 days"], [10080, "1 week"], [43200, "30 days"]];
  const ux = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmtWhen = (s) => new Date(s * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  // ---------- DOM: menu item and sheet ----------
  const item = ux("button", null, "Guest links…"); item.id = "guestLinksBtn"; item.type = "button"; item.setAttribute("role", "menuitem");
  const anchor = $("usersBtn"); if (anchor) anchor.before(item); else $("moreMenu").appendChild(item);
  const sheet = ux("div", "sheet"); sheet.id = "guestSheet"; sheet.hidden = true;
  sheet.innerHTML = `<div class="sheet-body" role="dialog" aria-modal="true" aria-labelledby="guestSheetTitle">
    <button class="close" id="guestClose" type="button" aria-label="Close">×</button><div id="guestContent"></div></div>`;
  document.body.appendChild(sheet);
  const close = () => { sheet.hidden = true; gl.created = null; };
  $("guestClose").onclick = close;
  sheet.addEventListener("click", (e) => { if (e.target === sheet) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) close(); });

  // ---------- what a link can cover ----------
  const protectedSet = () => new Set([...(st.layout.settings?.keep_on || []), ...(typeof protectedPlugs === "function" ? protectedPlugs() : [])]);
  const visible = (d) => d && !d.hidden;
  function roomLights(r) {
    return (st.layout.placements || []).filter((p) => p.entity_id.startsWith("light.") && visible(st.devices.get(p.entity_id)) && inRoom(r, p))
      .map((p) => p.entity_id);
  }
  function pickable() {
    const prot = protectedSet();
    return [...st.devices.values()].filter((d) => visible(d) && (d.kind === "light" || (d.kind === "plug" && !prot.has(d.entity_id))))
      .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "light" ? -1 : 1));
  }

  // ---------- load / act ----------
  async function openSheet() {
    gl.msg = ""; gl.err = false; gl.created = null;
    sheet.hidden = false;
    await load();
  }
  async function load() {
    try { gl.links = (await api("/api/guest/links")).links; } catch (e) { gl.msg = e.message; gl.err = true; }
    draw();
  }
  function say(msg, err = false) { gl.msg = msg; gl.err = err; const m = $("guestMsg"); if (m) { m.textContent = msg; m.classList.toggle("warn", err); } }

  async function createLink(body) {
    if (gl.busy) return;
    gl.busy = true;
    try {
      gl.created = await api("/api/guest/links", { method: "POST", body: JSON.stringify(body) });
      gl.msg = ""; gl.err = false;
      await load();
      $("guestNew")?.scrollIntoView({ block: "nearest" });
    } catch (e) { say(e.message, true); } finally { gl.busy = false; }
  }
  async function revoke(l) {
    if (!confirm(`Revoke “${l.label}”? The link stops working at once.`)) return;
    try {
      await api(`/api/guest/links/${encodeURIComponent(l.id)}`, { method: "DELETE" });
      if (gl.created?.id === l.id) gl.created = null;
      gl.msg = `“${l.label}” revoked`; gl.err = false;
    } catch (e) { gl.msg = e.message; gl.err = true; }
    await load();
  }

  // ---------- drawing ----------
  function scopeText(l) {
    const names = (l.devices || []).map((d) => d.name);
    if (l.scope.room) return l.room_name ? `${l.room_name} · ${plural(names.length, "light")}` : "Its room was deleted";
    return names.length ? names.join(", ") : "No devices left";
  }

  function draw() {
    const c = $("guestContent"); if (!c) return;
    c.replaceChildren();
    const h = ux("h3", null, "Guest links"); h.id = "guestSheetTitle"; c.appendChild(h);
    c.appendChild(ux("p", "sub", "A link or QR code that lets a visitor switch one room's lights — or the lights and plugs you pick — and nothing else. No account needed; it stops by itself, or when you revoke it."));
    if (gl.created) c.appendChild(drawCreated(gl.created));
    const msg = ux("p", "hint gl-msg" + (gl.err ? " warn" : ""), gl.msg); msg.id = "guestMsg"; msg.setAttribute("role", "status"); c.appendChild(msg);
    drawList(c);
    drawForm(c);
  }

  function drawCreated(l) {
    const url = location.origin + l.path;
    const box = ux("section", "gl-new"); box.id = "guestNew";
    box.appendChild(ux("h4", null, `Scan to open “${l.label}”`));
    const qr = ux("div", "gl-qr"); qr.id = "guestQr";
    try { qr.innerHTML = hcQR.svg(url, { ecl: "M" }); qr.firstChild.setAttribute("aria-label", `QR code for ${l.label}`); } catch (e) { qr.textContent = e.message; }
    const inp = ux("input", "gl-url"); inp.id = "guestUrl"; inp.readOnly = true; inp.value = url; inp.setAttribute("aria-label", "Guest link");
    inp.onfocus = () => inp.select();
    const btns = ux("div", "gl-new-btns");
    const copy = ux("button", "primary", "Copy link"); copy.type = "button"; copy.id = "guestCopy";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(url); } catch { inp.select(); document.execCommand?.("copy"); }
      copy.textContent = "Copied";
    };
    btns.appendChild(copy);
    if (navigator.share) {
      const share = ux("button", null, "Share…"); share.type = "button";
      share.onclick = () => navigator.share({ title: "Lights", text: `Lights for ${l.label}`, url }).catch(() => {});
      btns.appendChild(share);
    }
    const done = ux("button", null, "Done"); done.type = "button"; done.id = "guestDone";
    done.onclick = () => { gl.created = null; draw(); };
    btns.appendChild(done);
    box.append(qr, inp, btns,
      ux("p", "hint", `${scopeText(l)} · until ${fmtWhen(l.expires)}. Shown only now: the app keeps just a fingerprint of the link, so it can't show it again — revoke it and make a new one if it's lost.`));
    return box;
  }

  function drawList(c) {
    if (!gl.links.length) { c.appendChild(ux("p", "hint", "No guest links yet.")); return; }
    const ul = ux("ul", "gl-list"); ul.id = "guestList";
    for (const l of gl.links) {
      const li = ux("li", "gl-row" + (l.active ? " live" : "")); li.dataset.id = l.id;
      const top = ux("div", "gl-top");
      top.append(ux("span", "gl-name", l.label), ux("span", "gl-pill", l.active ? "Active" : l.revoked ? "Revoked" : "Expired"));
      if (l.active) {
        const b = ux("button", "danger gl-revoke", "Revoke"); b.type = "button";
        b.onclick = () => revoke(l);
        top.appendChild(b);
      }
      li.appendChild(top);
      const when = l.active ? `until ${fmtWhen(l.expires)}` : l.revoked ? `revoked ${fmtWhen(l.revoked)}` : `ended ${fmtWhen(l.expires)}`;
      const used = l.uses ? ` · opened ${plural(l.uses, "time")}` : " · not opened yet";
      li.appendChild(ux("p", "sub gl-info", `${scopeText(l)} · ${when}${used}`));
      ul.appendChild(li);
    }
    c.appendChild(ul);
  }

  function field(label, input) { const l = ux("label", "gl-field"); l.append(ux("span", null, label), input); return l; }

  function drawForm(c) {
    const sec = ux("section", "auto-sec");
    sec.appendChild(ux("h4", null, "New guest link"));
    const f = ux("form"); f.id = "guestForm"; f.autocomplete = "off";
    const name = ux("input"); name.name = "label"; name.required = true; name.maxLength = 40; name.placeholder = "Who it's for, e.g. Sam";
    const what = ux("select"); what.name = "what";
    const rooms = (st.layout.rooms || []).map((r) => [r, roomLights(r)]).filter(([, ids]) => ids.length);
    for (const [r, ids] of rooms) { const o = ux("option", null, `${r.name} — ${plural(ids.length, "light")}`); o.value = `room:${r.id}`; what.appendChild(o); }
    const pick = ux("option", null, "Lights and plugs I choose…"); pick.value = "devices"; what.appendChild(pick);
    const devs = ux("div", "gl-devs"); devs.id = "guestDevs"; devs.setAttribute("role", "group"); devs.setAttribute("aria-label", "Devices");
    for (const d of pickable()) {
      const l = ux("label"); const cb = ux("input"); cb.type = "checkbox"; cb.value = d.entity_id; cb.name = "dev";
      l.append(cb, ux("span", null, d.name), ux("span", "gl-kind", d.kind === "light" ? "light" : "plug"));
      devs.appendChild(l);
    }
    const sync = () => { devs.hidden = what.value !== "devices"; };
    what.onchange = sync; sync();
    const dur = ux("select"); dur.name = "minutes";
    for (const [m, t] of DURATIONS) { const o = ux("option", null, t); o.value = String(m); dur.appendChild(o); }
    dur.value = "1440";
    const go = ux("button", "primary", "Create link"); go.type = "submit";
    f.append(field("Name", name), field("Can switch", what), devs, field("Works for", dur), go);
    f.onsubmit = (e) => {
      e.preventDefault();
      const body = { label: name.value.trim(), minutes: Number(dur.value) };
      if (what.value === "devices") {
        body.devices = [...devs.querySelectorAll("input:checked")].map((x) => x.value);
        if (!body.devices.length) { say("Tick at least one light or plug", true); return; }
      } else body.room = what.value.slice(5);
      createLink(body);
    };
    sec.appendChild(f);
    c.appendChild(sec);
  }

  item.addEventListener("click", openSheet);
})();
