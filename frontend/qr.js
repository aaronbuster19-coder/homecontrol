"use strict";
// A small QR code encoder (ISO/IEC 18004, byte mode, versions 1–40), vendored so guest links get their QR code in the
// browser with no CDN and no server round trip. After the reference algorithm of Project Nayuki's QR Code generator
// (MIT licence); tests/qr.test.js checks its matrices module for module against another encoder's.
//   hcQR.encode(text, {ecl: "L"|"M"|"Q"|"H", mask?: 0–7}) -> {version, size, mask, modules: rows of booleans (true = dark)}
//   hcQR.svg(text, {ecl, border}) -> "<svg …>": class qr-bg (light) and qr-fg (dark) for the page's CSS to colour.
(function (root) {
  // Per version (index 1–40): error-correction codewords per block, and number of blocks.
  const ECC = {
    L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
    Q: [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    H: [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  };
  const BLOCKS = {
    L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
    M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
    Q: [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
    H: [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
  };
  const FORMAT = { L: 1, M: 0, Q: 3, H: 2 };
  const bit = (x, i) => ((x >>> i) & 1) !== 0;

  function rawModules(ver) {  // data + ECC modules: everything but the function patterns
    let r = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      const n = Math.floor(ver / 7) + 2;
      r -= (25 * n - 10) * n - 55;
      if (ver >= 7) r -= 36;
    }
    return r;
  }
  const dataCodewords = (ver, ecl) => Math.floor(rawModules(ver) / 8) - ECC[ecl][ver] * BLOCKS[ecl][ver];

  // ---- Reed–Solomon over GF(256), polynomial 0x11D ----
  function gfMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11D);
      z ^= ((y >>> i) & 1) * x;
    }
    return z;
  }
  function rsDivisor(degree) {
    const r = new Array(degree).fill(0);
    r[degree - 1] = 1;
    let rootVal = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < r.length; j++) {
        r[j] = gfMul(r[j], rootVal);
        if (j + 1 < r.length) r[j] ^= r[j + 1];
      }
      rootVal = gfMul(rootVal, 0x02);
    }
    return r;
  }
  function rsRemainder(data, divisor) {
    const r = divisor.map(() => 0);
    for (const b of data) {
      const f = b ^ r.shift();
      r.push(0);
      divisor.forEach((c, i) => { r[i] ^= gfMul(c, f); });
    }
    return r;
  }

  // ---- data codewords: byte mode, terminator, padding ----
  function codewords(bytes, ver, ecl) {
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4);
    put(bytes.length, ver <= 9 ? 8 : 16);
    for (const b of bytes) put(b, 8);
    const cap = dataCodewords(ver, ecl) * 8;
    put(0, Math.min(4, cap - bits.length));
    put(0, (8 - bits.length % 8) % 8);
    for (let pad = 0xEC; bits.length < cap; pad ^= 0xEC ^ 0x11) put(pad, 8);
    const out = [];
    for (let i = 0; i < bits.length; i += 8) out.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
    return out;
  }

  // Split into blocks, add each block's ECC, interleave.
  function withEcc(data, ver, ecl) {
    const nBlocks = BLOCKS[ecl][ver], eccLen = ECC[ecl][ver], raw = Math.floor(rawModules(ver) / 8);
    const nShort = nBlocks - raw % nBlocks, shortLen = Math.floor(raw / nBlocks);
    const div = rsDivisor(eccLen), blocks = [];
    for (let i = 0, k = 0; i < nBlocks; i++) {
      const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1));
      k += dat.length;
      const ecc = rsRemainder(dat, div);
      if (i < nShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    const out = [];
    for (let i = 0; i < blocks[0].length; i++) {
      blocks.forEach((b, j) => { if (i !== shortLen - eccLen || j >= nShort) out.push(b[i]); });
    }
    return out;
  }

  function alignmentPositions(ver, size) {
    if (ver === 1) return [];
    const n = Math.floor(ver / 7) + 2, step = Math.floor((ver * 8 + n * 3 + 5) / (n * 4 - 4)) * 2, out = [6];
    for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
    return out;
  }

  function build(ver, ecl, data, mask) {
    const size = ver * 4 + 17;
    const mods = Array.from({ length: size }, () => new Array(size).fill(false));
    const fn = Array.from({ length: size }, () => new Array(size).fill(false));
    const setF = (x, y, dark) => { mods[y][x] = dark; fn[y][x] = true; };

    for (let i = 0; i < size; i++) { setF(6, i, i % 2 === 0); setF(i, 6, i % 2 === 0); }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
          if (x >= 0 && x < size && y >= 0 && y < size) setF(x, y, d !== 2 && d !== 4);
        }
      }
    }
    const al = alignmentPositions(ver, size), n = al.length;
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setF(al[i] + dx, al[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }
    const formatBits = (m) => {
      const d = (FORMAT[ecl] << 3) | m;
      let rem = d;
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const bits = ((d << 10) | rem) ^ 0x5412;
      for (let i = 0; i <= 5; i++) setF(8, i, bit(bits, i));
      setF(8, 7, bit(bits, 6)); setF(8, 8, bit(bits, 7)); setF(7, 8, bit(bits, 8));
      for (let i = 9; i < 15; i++) setF(14 - i, 8, bit(bits, i));
      for (let i = 0; i < 8; i++) setF(size - 1 - i, 8, bit(bits, i));
      for (let i = 8; i < 15; i++) setF(8, size - 15 + i, bit(bits, i));
      setF(8, size - 8, true);  // the dark module
    };
    formatBits(0);  // reserve the area; the real bits go in once the mask is chosen
    if (ver >= 7) {
      let rem = ver;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
      const bits = (ver << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const a = size - 11 + i % 3, b = Math.floor(i / 3);
        setF(a, b, bit(bits, i)); setF(b, a, bit(bits, i));
      }
    }

    // Data in the zig-zag, two columns at a time from the bottom right, skipping the vertical timing column.
    let i = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < size; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j, y = ((right + 1) & 2) === 0 ? size - 1 - vert : vert;
          if (!fn[y][x] && i < data.length * 8) { mods[y][x] = bit(data[i >>> 3], 7 - (i & 7)); i++; }
        }
      }
    }

    const MASKS = [
      (x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
      (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
      (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0,
    ];
    const applyMask = (m) => {
      for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fn[y][x] && MASKS[m](x, y)) mods[y][x] = !mods[y][x];
    };
    if (mask == null) {
      let best = Infinity;
      for (let m = 0; m < 8; m++) {
        applyMask(m); formatBits(m);
        const p = penalty(mods);
        if (p < best) { best = p; mask = m; }
        applyMask(m);  // XOR again: undo
      }
    }
    applyMask(mask); formatBits(mask);
    return { version: ver, size, mask, modules: mods };
  }

  // The four penalty rules of the standard; the lowest score picks the mask (any mask decodes, this one scans best).
  function penalty(m) {
    const size = m.length;
    let score = 0, dark = 0;
    const lines = [];
    for (let y = 0; y < size; y++) { lines.push(m[y]); lines.push(m.map((row) => row[y])); }
    const F1 = [true, false, true, true, true, false, true];
    for (const line of lines) {
      for (let i = 0, run = 1; i < size; i++) {
        if (i > 0 && line[i] === line[i - 1]) run++; else run = 1;
        if (run === 5) score += 3; else if (run > 5) score += 1;
      }
      for (let i = 0; i + 7 <= size; i++) {
        if (!F1.every((v, k) => line[i + k] === v)) continue;
        const before = i >= 4 && [1, 2, 3, 4].every((k) => !line[i - k]);
        const after = i + 11 <= size && [7, 8, 9, 10].every((k) => !line[i + k]);
        if (before) score += 40;
        if (after) score += 40;
      }
    }
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (m[y][x]) dark++;
        if (x < size - 1 && y < size - 1 && m[y][x] === m[y][x + 1] && m[y][x] === m[y + 1][x] && m[y][x] === m[y + 1][x + 1]) score += 3;
      }
    }
    const total = size * size;
    return score + (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
  }

  function encode(text, opts = {}) {
    const ecl = opts.ecl || "M";
    if (!ECC[ecl]) throw new Error("ecl must be L, M, Q or H");
    const bytes = Array.from(new TextEncoder().encode(String(text)));
    for (let ver = 1; ver <= 40; ver++) {
      if (4 + (ver <= 9 ? 8 : 16) + bytes.length * 8 <= dataCodewords(ver, ecl) * 8) {
        return build(ver, ecl, withEcc(codewords(bytes, ver, ecl), ver, ecl), opts.mask);
      }
    }
    throw new Error("too long for a QR code");
  }

  function svg(text, opts = {}) {
    const q = encode(text, opts), b = opts.border == null ? 4 : opts.border, s = q.size + 2 * b;
    let d = "";
    q.modules.forEach((row, y) => {  // one rectangle per horizontal run of dark modules
      for (let x = 0; x < q.size; x++) {
        if (!row[x]) continue;
        let n = 1;
        while (x + n < q.size && row[x + n]) n++;
        d += `M${x + b} ${y + b}h${n}v1h-${n}z`;
        x += n - 1;
      }
    });
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${s} ${s}" shape-rendering="crispEdges" role="img">` +
      `<rect class="qr-bg" width="${s}" height="${s}" fill="#fff"/><path class="qr-fg" d="${d}" fill="#000"/></svg>`;
  }

  const api = { encode, svg };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.hcQR = api;
})(typeof window !== "undefined" ? window : globalThis);
