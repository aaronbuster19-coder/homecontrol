// The vendored QR encoder in frontend/qr.js. Run: node --test tests/   (no npm packages needed)
// tests/qr-fixtures.json holds matrices made by an independent encoder (python-qrcode, byte mode) for the same
// text, level and mask: every module must match, for versions 1–34 and every ECC level.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const qr = require("../frontend/qr.js");
const fixtures = require("./qr-fixtures.json");

const rows = (q) => q.modules.map((r) => r.map((d) => (d ? "1" : "0")).join(""));

test("matrices match an independent encoder module for module", () => {
  for (const f of fixtures) {
    const q = qr.encode(f.text, { ecl: f.ecl, mask: f.mask });
    assert.equal(q.version, f.version, f.text);
    assert.equal(q.size, f.rows.length);
    assert.deepEqual(rows(q), f.rows, `${f.ecl} v${f.version} mask ${f.mask}: ${f.text.slice(0, 30)}`);
  }
});

test("picks the smallest version and a mask by itself", () => {
  for (const f of fixtures) {
    const q = qr.encode(f.text, { ecl: f.ecl });
    assert.equal(q.version, f.version);
    assert.ok(q.mask >= 0 && q.mask <= 7);
    assert.deepEqual(rows(q), rows(qr.encode(f.text, { ecl: f.ecl, mask: q.mask })));
  }
});

test("a guest link fits a small code and renders as SVG", () => {
  const url = "https://home.example.co.uk/guest.html#" + "x".repeat(43);
  const q = qr.encode(url, { ecl: "M" });
  assert.ok(q.version <= 6, `version ${q.version}`);
  const s = qr.svg(url);
  const side = q.size + 8;  // 4 modules of quiet zone each side
  assert.match(s, new RegExp(`^<svg [^>]*viewBox="0 0 ${side} ${side}"`));
  assert.match(s, /class="qr-bg"/);
  assert.match(s, /class="qr-fg" d="M4 4h7v1h-7z/);  // the finder pattern's top edge: 7 dark modules in a row
});

test("too long or a bad level throws", () => {
  assert.throws(() => qr.encode("x".repeat(3000), { ecl: "H" }), /too long/);
  assert.throws(() => qr.encode("x", { ecl: "Z" }), /ecl/);
});
