// H3 Studio front end. No dependencies; strings live in i18n.js.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// [width, height, label key]
const SIZES = {
  "9:16": [[288, 512, "preview"], [576, 1024, "standard"], [768, 1344, "max47"]],
  "16:9": [[512, 288, "preview"], [864, 480, "default95"], [1024, 576, "standard"], [1344, 768, "max74"]],
  "1:1": [[256, 256, "preview"], [512, 512, "standard"], [768, 768, "hd"]],
  "3:4": [[384, 512, "preview"], [768, 1024, "hd"]],
  "4:3": [[512, 384, "preview"], [1024, 768, "hd"]],
  custom: [],
};
const QUALITY = {
  draft: { layers: 45, core: 4, render: 0.5 },
  recommended: { layers: 45, core: 4 },
  high: { layers: 50, steps: 20 },
  max: { layers: 50, steps: 50 },
};
// What a reference image contributes (matches REF_ROLES in h3d.py), with examples for the two text boxes:
// [what it is, features]. "What it is" must be a specific noun; a generic one and the image is mostly ignored.
const REF_ROLES = ["person", "scene", "object", "outfit", "style"];
const REF_HINTS = {
  person: ["young woman", "short black bob, light gray shirt, beige apron"],
  scene: ["modern office", "open desks, floor-to-ceiling windows, gray carpet"],
  object: ["red fox", "fluffy tail, white chest"],
  outfit: ["navy blazer", "gold buttons, slim fit"],
  style: ["hand-painted watercolor look", "soft warm colors"],
};
const STORE_KEY = "h3-form-v2";
const SEEN_KEY = "h3-seen-v1";
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const CJK = /[㐀-鿿]/;

const st = {
  mode: "t2v", aspect: "9:16", size: [576, 1024], quality: "recommended",
  uploads: { refs: [], first: null, last: null, audios: [], videos: [] },
  status: null, jobs: [], promptFor: null, seen: new Set(), promptState: "p.auto", notifyWant: "", notifySig: null, offline: false,
};
try { st.seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || "[]")); } catch {}

// ------------------------------------------------------------ helpers
function toast(msg, ms = 3200) {
  const el = $("#toast"); el.hidden = true; void el.offsetWidth; el.textContent = msg; el.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), ms);
}
async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { "Content-Type": "application/json", "X-H3-Lang": LANG },
    body: opt.body && typeof opt.body !== "string" && !(opt.body instanceof Blob) ? JSON.stringify(opt.body) : opt.body });
  let data = {}; try { data = await r.json(); } catch {}
  if (!r.ok) { const e = new Error(data.error || t("t.reqFail", { n: r.status })); e.code = data.code; throw e; }
  return data;
}
const fmtDur = s => { s = Math.round(s || 0); return s >= 60 ? t("dur.min", { m: Math.floor(s / 60), s: s % 60 }) : t("dur.s", { s }); };
const framesFor = sec => { const want = Math.max(1, Math.ceil(sec * 24)); return 5 + 17 * Math.max(0, Math.ceil((want - 5) / 17)); };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function renderDims(w, h, scale) {
  const gw = w / 32, gh = h / 32, gcd = (a, b) => (b ? gcd(b, a % b) : a), g = gcd(gw, gh);
  const k = Math.min(g, Math.max(1, Math.round(g * scale)));
  return [(gw / g) * k * 32, (gh / g) * k * 32];
}
function retrigger(el, cls) { if (!el || REDUCED) return; el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); }
function setPromptState(key) { st.promptState = key; $("#promptState").textContent = t(key); }

// A styled replacement for confirm(). Resolves to the chosen button's value, or undefined if dismissed.
function ask(msg, buttons) {
  return new Promise(resolve => {
    const dlg = $("#confirmDlg");
    $("#confirmMsg").textContent = msg;
    $("#confirmActs").innerHTML = buttons.map((b, i) => `<button class="btn ${b.cls || ""}" data-i="${i}">${esc(b.label)}</button>`).join("");
    $("#confirmActs").onclick = e => { const b = e.target.closest("button"); if (!b) return; dlg.close(); resolve(buttons[+b.dataset.i].value); };
    dlg.onclose = () => resolve(undefined);
    dlg.showModal();
  });
}

// ------------------------------------------------------------ confetti when a job finishes
function confetti(x = innerWidth / 2, y = innerHeight / 3) {
  if (REDUCED) return;
  const cv = $("#fx"), cx = cv.getContext("2d"), dpr = devicePixelRatio || 1;
  cv.width = innerWidth * dpr; cv.height = innerHeight * dpr; cx.scale(dpr, dpr);
  const colors = ["#ff4f8b", "#ffd43b", "#29c48a", "#7c5cff", "#5ab8ff"];
  const parts = Array.from({ length: 120 }, () => ({
    x, y, vx: (Math.random() - .5) * 13, vy: -Math.random() * 13 - 4, r: Math.random() * Math.PI,
    vr: (Math.random() - .5) * .35, w: 6 + Math.random() * 8, h: 4 + Math.random() * 6, c: colors[(Math.random() * colors.length) | 0], star: Math.random() < .25,
  }));
  let n = 0;
  (function frame() {
    cx.clearRect(0, 0, innerWidth, innerHeight);
    for (const p of parts) {
      p.vy += .32; p.vx *= .99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
      cx.save(); cx.translate(p.x, p.y); cx.rotate(p.r); cx.fillStyle = p.c; cx.strokeStyle = "#1d1b2f"; cx.lineWidth = 1.5;
      if (p.star) { cx.beginPath(); for (let i = 0; i < 5; i++) { cx.lineTo(Math.cos((18 + i * 72) * Math.PI / 180) * 7, -Math.sin((18 + i * 72) * Math.PI / 180) * 7); cx.lineTo(Math.cos((54 + i * 72) * Math.PI / 180) * 3, -Math.sin((54 + i * 72) * Math.PI / 180) * 3); } cx.closePath(); cx.fill(); cx.stroke(); }
      else { cx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); cx.strokeRect(-p.w / 2, -p.h / 2, p.w, p.h); }
      cx.restore();
    }
    if (++n < 160) requestAnimationFrame(frame); else cx.clearRect(0, 0, innerWidth, innerHeight);
  })();
}

// ------------------------------------------------------------ form -> request
function currentSize() {
  if (st.aspect === "custom") return [+$("#cw").value || 576, +$("#ch").value || 1024];
  return st.size;
}
function collectParams() {
  const [w, h] = currentSize();
  const p = { width: w, height: h, seconds: +$("#seconds").value || 2, quality: st.quality };
  const steps = $("#steps").value, layers = $("#layers").value, reuse = $("#reuseMode").value, render = $("#renderMode").value;
  if (steps || layers || reuse || render) {
    const q = QUALITY[st.quality];
    p.quality = "custom";
    p.steps = +(steps || q.steps || 20);
    p.layers = +(layers || q.layers || 50);
    const r = reuse || (q.core ? "core" + q.core : "none");
    p.reuse = r.startsWith("reuse") ? +r.slice(5) : 1;
    p.core_reuse = r.startsWith("core") ? +r.slice(4) : 1;
    const scale = render ? +render : (q.render || 1);
    if (scale < 1) { const [rw, rh] = renderDims(w, h, scale); if (rw !== w) { p.render_width = rw; p.render_height = rh; } }
  }
  p.seed = $("#seedMode").value === "random" ? "random" : +$("#seed").value || 0;
  p.crop = $("#crop").value; p.ref_image_size = $("#refSize").value;
  p.token_reduction = $("#tokenRed").checked; p.reference_rope = $("#refRope").checked;
  p.ssd_streaming = $("#ssd").checked; p.int8_row_fc2 = $("#int8row").checked;
  if ($("#noZeroCopy").checked) p.zero_copy = false;
  if ($("#qwenPf").value !== "") p.qwen_prefetch = +$("#qwenPf").value;
  if ($("#qwenDepth").value !== "") p.qwen_prefetch_depth = +$("#qwenDepth").value;
  return p;
}
function collectInputs() {
  const u = st.uploads, i = {};
  if (st.mode === "ref") {
    i.ref_images = u.refs.map(x => x.id);
    i.ref_audios = u.audios.map(x => x.id);
    i.ref_videos = u.videos.map(x => ({ id: x.id, kind: x.kind || "video" }));
  } else if (st.mode === "fl") {
    if (u.first) i.first_frame = u.first.id;
    if (u.last) i.last_frame = u.last.id;
  }
  return i;
}
function apiMode() {
  if (st.mode === "ref") return "ref2va";
  if (st.mode === "fl") { const f = st.uploads.first, l = st.uploads.last; return f && l ? "fl2va" : f ? "i2va" : l ? "l2va" : "t2va"; }
  return "t2va";
}
// Videos, audio and per-image notes in reference mode (the server needs them to number @ mentions and write the prompt).
function refExtras() {
  if (st.mode !== "ref") return {};
  return { videos: st.uploads.videos.map(x => ({ id: x.id, kind: x.kind || "video" })), audios: st.uploads.audios.map(x => x.id),
    refs: st.uploads.refs.map(x => ({ role: x.role || "person", name: x.what || "", desc: x.desc || "" })) };
}
function promptSignature() {
  const [w, h] = currentSize();
  return JSON.stringify([apiMode(), $("#desc").value.trim(), $("#dialogue").value.trim(), $("#lang").value,
    +$("#seconds").value, w, h, st.uploads.refs.map(x => x.id), st.uploads.first?.id, st.uploads.last?.id,
    st.uploads.videos.map(x => x.id + (x.kind || "")), st.uploads.audios.map(x => x.id),
    st.uploads.refs.map(x => (x.role || "") + "|" + (x.what || "") + "|" + (x.desc || ""))]);
}

// ------------------------------------------------------------ time estimate
function estimate() {
  const p = collectParams();
  const f = framesFor(p.seconds), q = p.quality === "custom" ? null : QUALITY[p.quality];
  let ew = p.render_width || p.width, eh = p.render_height || p.height;
  if (q?.render && !p.render_width) [ew, eh] = renderDims(p.width, p.height, q.render);
  const steps = p.steps || q?.steps || 20, layers = p.layers || q?.layers || 50, core = p.core_reuse || q?.core || 1, reuse = p.reuse || 1;
  // Fitted to 13 real runs on an M5 Pro (576x1024 recommended: 2 s -> 160 s, 5 s -> 444 s, 9 s -> 1040 s):
  // denoising ~ frames^1.7 x pixels^1.38 (each extra second costs more than the last); decoding is roughly
  // linear in frames and pixels; about 33 s of fixed overhead. Other machines: set speed_factor in the config.
  const F = f / 56, P = (ew * eh) / (576 * 1024);
  let den = 255 * Math.pow(F, 1.7) * Math.pow(P, 1.38) * (steps / 20) * (layers / 50);
  if (core > 1) den *= core === 4 ? 0.3 : 0.24;
  if (reuse > 1) den *= reuse === 2 ? 0.55 : 0.42;
  if (p.token_reduction) den *= 0.72;
  if (p.ssd_streaming) den *= 1.5;
  if (st.mode !== "t2v") den *= 1.25;          // references and frames add tokens to process
  const vae = 58 * Math.pow(F, 1.08) * P;
  return { total: (33 + den + vae + (st.mode === "ref" ? 6 : 0)) * (st.status?.speed_factor || 1), frames: f };
}
function syncAIState() {
  const on = $("#useAI").checked && !!st.status?.rewriter, has = $("#prompt").value.trim();
  if (!on) setPromptState(has ? "p.keep" : "p.direct");
  else if (!has) setPromptState("p.auto");
  else $("#promptState").textContent = t(st.promptState);
  $("#desc").placeholder = on || st.mode !== "t2v" ? t("desc.ph." + st.mode) : t("desc.ph.plain");
}
function refreshEstimate() {
  const [w, h] = currentSize(), sec = +$("#seconds").value || 0, e = estimate();
  $("#framesInfo").textContent = t("dur.frames", { f: e.frames, s: (e.frames / 24).toFixed(2) });
  const r = $("#secondsRange"); r.style.setProperty("--p", `${((+r.value - 0.5) / 14.5) * 100}%`);
  const bad = w % 32 || h % 32 || w * h > 768 * 1344;
  $("#estimate").innerHTML = bad ? t("est.bad") : t("est", { t: fmtDur(e.total), w, h, s: sec });
  const n = $("#notifyTo"), title = $("#title").value;
  const parts = [];
  if (st.status?.notify_targets?.length) parts.push(n.value ? t("out.sum.push", { x: n.options[n.selectedIndex]?.text || "" }) : t("out.sum.none"));
  if (title) parts.push(title);
  $("#outSummary").textContent = parts.join(" · ") || t("out.title");
  saveForm();
}

// ------------------------------------------------------------ size and mode
function moveBlob() {
  const act = $("#modeTabs button.active"), blob = $(".tab-blob");
  if (!act || !blob) return;
  blob.style.width = act.offsetWidth + "px";
  blob.style.transform = `translateX(${act.offsetLeft}px) rotate(${st.mode === "ref" ? 1.5 : st.mode === "fl" ? -1.5 : -1}deg)`;
}
function renderAspect() {
  if (!(st.aspect in SIZES)) st.aspect = "custom";      // forms saved by older versions used a localized key
  $("#aspectChips").innerHTML = Object.keys(SIZES).map(a =>
    `<button type="button" data-a="${a}" class="${a === st.aspect ? "active" : ""}">${a === "custom" ? t("aspect.custom") : a}</button>`).join("");
  const sel = $("#sizeSel"), list = SIZES[st.aspect];
  $("#customSize").hidden = st.aspect !== "custom"; $("#sizeWrap").hidden = st.aspect === "custom";
  sel.innerHTML = list.map(([w, h, k]) => `<option value="${w}x${h}">${w}×${h} · ${t("size." + k)}</option>`).join("");
  const pick = list.find(([w, h]) => w === st.size[0] && h === st.size[1]) || list.find(x => x[2] === "standard") || list[0];
  if (pick) { st.size = [pick[0], pick[1]]; sel.value = `${pick[0]}x${pick[1]}`; }
  refreshEstimate();
}
function setMode(m) {
  st.mode = m;
  $$("#modeTabs button").forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  $("#refBlock").hidden = m !== "ref"; $("#flBlock").hidden = m !== "fl";
  moveBlob(); syncAIState(); renderMentionBar(); refreshEstimate();
}
function setQuality(q) {
  st.quality = q;
  $$("#qualitySeg button").forEach(b => b.classList.toggle("active", b.dataset.q === q));
  refreshEstimate();
}

// ------------------------------------------------------------ uploads
async function uploadFile(file) {
  const r = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file, headers: { "X-H3-Lang": LANG } });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || t("t.uploadFail"));
  return d;
}
function renderUploads() {
  const u = st.uploads;
  renderMentionBar();
  // One row per reference image: thumbnail, what it contributes, what it is, features.
  // The composer writes each image's definition (required by the official format) from these.
  const roleOpts = r => REF_ROLES.map(k => `<option value="${k}" ${k === r ? "selected" : ""}>${t("role." + k)}</option>`).join("");
  // `what` = what the image is (not `name`: an upload's own `name` is its file name)
  u.refs.forEach((x, i) => { x.role ||= i === 0 ? "person" : "scene"; x.desc ||= ""; x.what ||= ""; });
  $("#refThumbs").innerHTML = u.refs.map((x, i) => `
    <div class="ref-row">
      <div class="thumb"><img src="${x.url}" alt="${t("ref.alt", { n: i + 1 })}"><span class="tag">${t("ref.n", { n: i + 1 })}</span>
        <div class="acts"><button data-act="left" data-i="${i}" title="${t("ref.left")}">◀</button><button data-act="del" data-i="${i}" title="${t("remove")}">✕</button><button data-act="right" data-i="${i}" title="${t("ref.right")}">▶</button></div></div>
      <div class="ref-meta">
        <label class="sub" for="refRole${i}">${t("ref.is", { n: i + 1 })}</label>
        <select id="refRole${i}" class="bubble" data-meta="role" data-i="${i}">${roleOpts(x.role)}</select>
        <label class="sub" for="refName${i}">${t("ref.what")}</label>
        <input id="refName${i}" class="bubble" data-meta="what" data-i="${i}" value="${esc(x.what)}" placeholder="${t("eg", { x: REF_HINTS[x.role]?.[0] || "" })}">
        <label class="sub" for="refDesc${i}">${t("ref.feat")}</label>
        <input id="refDesc${i}" class="bubble" data-meta="desc" data-i="${i}" value="${esc(x.desc)}" placeholder="${t("eg", { x: REF_HINTS[x.role]?.[1] || "" })}">
      </div>
    </div>`).join("");
  for (const k of ["first", "last"]) {
    const x = u[k];
    $(`.drop[data-target="${k}"] .thumbs`).innerHTML = x ? `<div class="thumb"><img src="${x.url}" alt=""><div class="acts"><button data-act="del1" data-k="${k}">✕ ${t("remove")}</button></div></div>` : "";
  }
  $(`.drop[data-target="audios"] .thumbs`).innerHTML = u.audios.map((x, i) =>
    `<div class="file-row"><span class="badge">${t("m.aud", { n: i + 1 })}</span><span class="name">${esc(x.name)}</span><button data-act="delA" data-i="${i}" title="${t("remove")}">✕</button></div>`).join("");
  $(`.drop[data-target="videos"] .thumbs`).innerHTML = u.videos.map((x, i) =>
    `<div class="file-row"><span class="badge">${t("m.vid", { n: i + 1 })}</span><span class="name">${esc(x.name)}</span>
      <select data-act="kindV" data-i="${i}"><option value="video" ${x.kind !== "silent" ? "selected" : ""}>${t("v.sound")}</option><option value="silent" ${x.kind === "silent" ? "selected" : ""}>${t("v.silent")}</option></select>
      <button data-act="delV" data-i="${i}" title="${t("remove")}">✕</button></div>`).join("");
  refreshEstimate();
}
function bindDrop(drop) {
  const input = $("input[type=file]", drop), target = drop.dataset.target;
  const add = async files => {
    for (const f of files) {
      try {
        toast(t("t.uploading", { x: f.name }), 60000);
        const d = await uploadFile(f);
        if (target === "refs") st.uploads.refs.push(d);
        else if (target === "first" || target === "last") st.uploads[target] = d;
        else if (target === "audios") st.uploads.audios.push(d);
        else st.uploads.videos.push({ ...d, kind: "video" });
        toast(t("t.added", { x: f.name }));
      } catch (e) { toast(e.message); }
    }
    renderUploads();
  };
  $(".drop-hint", drop).onclick = () => input.click();
  input.onchange = () => { add([...input.files]); input.value = ""; };
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", e => { e.preventDefault(); drop.classList.remove("over"); add([...e.dataTransfer.files]); });
  drop.addEventListener("click", e => {
    const b = e.target.closest("[data-act]"); if (!b || b.tagName === "SELECT") return;
    const i = +b.dataset.i, u = st.uploads, act = b.dataset.act;
    const swap = (k, a, c) => renumberMentions(k, n => (n === a ? c : n === c ? a : n));      // two references trade places
    const drop1 = (k, d) => renumberMentions(k, n => (n === d ? 0 : n > d ? n - 1 : n));      // one removed, later ones move up
    const kind = { del: "img", delV: "vid", delA: "aud" }[act];
    if (kind && mentionUsed(kind, i + 1)) toast(t("m.removed", { x: mentionToken(kind, i + 1) }), 4500);
    if (act === "del") { u.refs.splice(i, 1); drop1("img", i + 1); }
    if (act === "left" && i > 0) { [u.refs[i - 1], u.refs[i]] = [u.refs[i], u.refs[i - 1]]; swap("img", i, i + 1); }
    if (act === "right" && i < u.refs.length - 1) { [u.refs[i + 1], u.refs[i]] = [u.refs[i], u.refs[i + 1]]; swap("img", i + 1, i + 2); }
    if (act === "del1") u[b.dataset.k] = null;
    if (act === "delA") { u.audios.splice(i, 1); drop1("aud", i + 1); }
    if (act === "delV") { u.videos.splice(i, 1); drop1("vid", i + 1); }
    renderUploads();
  });
  drop.addEventListener("input", e => {
    const m = e.target.dataset.meta; if (!m) return;
    const x = st.uploads.refs[+e.target.dataset.i]; if (!x) return;
    x[m] = e.target.value;
    if (m === "role") { const i = e.target.dataset.i, h = REF_HINTS[x.role] || ["", ""];
      $(`#refName${i}`).placeholder = t("eg", { x: h[0] }); $(`#refDesc${i}`).placeholder = t("eg", { x: h[1] }); }
    saveForm();
  });
  drop.addEventListener("change", e => {
    if (e.target.dataset.act !== "kindV") return;
    const i = +e.target.dataset.i; st.uploads.videos[i].kind = e.target.value;
    if (e.target.value === "silent") {      // once silent, "video N's sound" no longer exists
      for (const id of ["desc", "dialogue", "prompt"])
        $("#" + id).value = $("#" + id).value.replace(MENTION_RE, (all, k, n, snd) => (KIND[k.toLowerCase()] === "vid" && +n === i + 1 && snd ? "" : all));
    }
    renderMentionBar(); refreshEstimate();
  });
}
document.addEventListener("paste", async e => {
  if (st.mode !== "ref") return;
  const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith("image/"));
  if (!files.length) return;
  for (const f of files) { try { st.uploads.refs.push(await uploadFile(f)); } catch (err) { toast(err.message); } }
  renderUploads();
});

// ------------------------------------------------------------ @ mentions
// The description refers to uploads as @img1 / @vid1 / @vid1.audio / @aud1 (@图1 / @视频1 / @视频1原声 / @音频1 in
// the Chinese UI; both spellings always work). Turning them into official labels is the server's job.
const MENTION_RE = /@(图|视频|音频|img|vid|aud)\s*(\d+)(原声|\.audio)?/gi;
const KIND = { "图": "img", img: "img", "视频": "vid", vid: "vid", "音频": "aud", aud: "aud" };
const WORDS = { zh: { img: "图", vid: "视频", aud: "音频", snd: "原声" }, en: { img: "img", vid: "vid", aud: "aud", snd: ".audio" } };
const mentionToken = (kind, n, snd = false) => `@${WORDS[LANG][kind]}${n}${snd ? WORDS[LANG].snd : ""}`;
function mentionItems() {
  if (st.mode !== "ref") return [];
  const u = st.uploads, out = [];
  u.refs.forEach((x, i) => out.push({ token: mentionToken("img", i + 1), label: t("m.img", { n: i + 1 }), img: x.url, name: x.name }));
  u.videos.forEach((x, i) => {
    out.push({ token: mentionToken("vid", i + 1), label: t("m.vid", { n: i + 1 }), ico: "▶", name: x.name });
    if (x.kind !== "silent") out.push({ token: mentionToken("vid", i + 1, true), label: t("m.vidAudio", { n: i + 1 }), ico: "♪", name: x.name });
  });
  u.audios.forEach((x, i) => out.push({ token: mentionToken("aud", i + 1), label: t("m.aud", { n: i + 1 }), ico: "♪", name: x.name }));
  return out;
}
const mentionIcon = it => it.img ? `<img src="${it.img}" alt="">` : `<span class="ico">${it.ico}</span>`;
function renderMentionBar() {
  const items = mentionItems(), bar = $("#mentionBar");
  bar.hidden = !items.length;
  bar.innerHTML = items.length ? `<span>${t("mention.bar")}</span>` +
    items.map(it => `<button type="button" data-token="${it.token}" title="${esc(it.name || "")}">${mentionIcon(it)}${it.token}</button>`).join("") : "";
}
// Insert a mark at the caret; with replaceFrom, replace the "@xxx" being typed from there to the caret.
function insertMention(token, replaceFrom = null) {
  const el = $("#desc"), end = el.selectionEnd ?? el.value.length, start = replaceFrom ?? el.selectionStart ?? end;
  const before = el.value.slice(0, start), after = el.value.slice(end);
  const text = token + (/^[\s，。、,.]/.test(after) ? "" : " ");
  el.value = before + text + after;
  const pos = (before + text).length;
  el.focus(); el.setSelectionRange(pos, pos);
  closeMentionPop(); refreshEstimate();
}
const pop = { open: false, from: 0, items: [], idx: 0 };
function closeMentionPop() { pop.open = false; $("#mentionPop").hidden = true; }
function updateMentionPop() {
  const el = $("#desc"), caret = el.selectionStart, m = /@([^\s@，。、,]{0,10})$/.exec(el.value.slice(0, caret));
  const all = mentionItems();
  if (!m || !all.length) return closeMentionPop();
  const q = m[1].toLowerCase();
  pop.items = all.filter(it => it.token.slice(1).toLowerCase().startsWith(q) || it.label.replace(/\s/g, "").toLowerCase().includes(q));
  if (!pop.items.length) return closeMentionPop();
  pop.open = true; pop.from = caret - m[0].length; pop.idx = Math.min(pop.idx, pop.items.length - 1);
  const box = $("#mentionPop");
  box.innerHTML = pop.items.map((it, i) => `<button type="button" role="option" data-i="${i}" class="${i === pop.idx ? "on" : ""}">${mentionIcon(it)}${it.label}<small>${esc(it.name || "")}</small></button>`).join("");
  box.hidden = false;
}
// References were reordered or removed: renumber the marks in the text (fn returns the new number, 0 = gone).
function renumberMentions(kind, fn) {
  for (const id of ["desc", "dialogue", "prompt"]) {
    const el = $("#" + id);
    el.value = el.value.replace(MENTION_RE, (all, k, n, snd) => {
      if (KIND[k.toLowerCase()] !== kind) return all;
      const nn = fn(+n);
      return nn ? `@${k}${nn}${snd || ""}` : "";
    });
  }
}
const mentionUsed = (kind, n) => ["desc", "dialogue", "prompt"].some(id =>
  [...$("#" + id).value.matchAll(MENTION_RE)].some(m => KIND[m[1].toLowerCase()] === kind && +m[2] === n));

// ------------------------------------------------------------ AI rewrite (needs a rewriter plugin)
async function rewrite() {
  const desc = $("#desc").value.trim();
  if (!desc) { toast(t("t.needDesc")); $("#desc").focus(); return null; }
  const [w, h] = currentSize();
  const images = st.mode === "ref" ? st.uploads.refs.map(x => x.id)
    : st.mode === "fl" ? [st.uploads.first, st.uploads.last].filter(Boolean).map(x => x.id) : [];
  const btn = $("#rewriteBtn"); btn.disabled = true; btn.classList.add("loading");
  setPromptState("p.writing"); $("#formMsg").textContent = t("p.writingMsg");
  try {
    const d = await api("/api/rewrite", { method: "POST", body: {
      mode: apiMode(), description: desc, dialogue: $("#dialogue").value, language: $("#lang").value,
      seconds: +$("#seconds").value, width: w, height: h, music: $("#music").checked, images, ...refExtras() } });
    $("#prompt").value = d.prompt; st.promptFor = promptSignature();
    setPromptState("p.done"); $("#formMsg").textContent = "";
    saveForm(); return d.prompt;
  } catch (e) {
    setPromptState("p.failed"); $("#formMsg").textContent = e.message; toast(e.message); return null;
  } finally { btn.disabled = false; btn.classList.remove("loading"); }
}

// ------------------------------------------------------------ submit
async function submit() {
  const btn = $("#submitBtn");
  if (st.mode === "ref" && !st.uploads.refs.length && !st.uploads.videos.length && !st.uploads.audios.length) return toast(t("t.needRef"));
  if (st.mode === "fl" && !st.uploads.first && !st.uploads.last) return toast(t("t.needFrame"));
  btn.disabled = true; btn.classList.add("loading");
  retrigger($(".clapper"), "clap");
  try {
    let prompt = $("#prompt").value.trim();
    if (!($("#useAI").checked && st.status?.rewriter)) {
      // No AI: use the prompt box as is if it has text; otherwise wrap description, dialogue and language in the
      // official format on the server. With several reference images the model must be told what each one
      // provides (tested: otherwise it follows just one), so remind once if a "what it is" box is empty.
      const u = st.uploads, blank = st.mode === "ref" && u.refs.length >= 2 ? u.refs.map((x, i) => (x.what || "").trim() ? 0 : i + 1).filter(Boolean) : [];
      if (!prompt && blank.length) {
        const v = await ask(t("ask.blank", { x: blank.join(t("listSep")) }),
          [{ label: t("ask.blank.go"), value: "go", cls: "ghost" }, { label: t("ask.blank.back"), value: "back", cls: "primary" }]);
        if (v !== "go") { $(`#refThumbs [data-meta="what"][data-i="${blank[0] - 1}"]`)?.focus(); return; }
      }
      if (!prompt) {
        if (!$("#desc").value.trim()) { toast(t("t.needDesc")); $("#desc").focus(); return; }
        const [w, h] = currentSize();
        const r = await api("/api/compose", { method: "POST", body: { mode: apiMode(), description: $("#desc").value,
          dialogue: $("#dialogue").value, language: $("#lang").value, seconds: +$("#seconds").value, width: w, height: h, music: $("#music").checked,
          images: st.mode === "ref" ? st.uploads.refs.map(x => x.id) : [], translate: $("#translateMode").value, ...refExtras() } });
        prompt = r.prompt;
        if (r.note) toast(r.note, 6000);
      }
    } else {
      const stale = st.promptFor && st.promptFor !== promptSignature();
      let redo = !prompt;
      if (stale) redo = await ask(t("ask.stale"), [
        { label: t("ask.stale.keep"), value: false, cls: "ghost" }, { label: t("ask.stale.redo"), value: true, cls: "primary" }]);
      if (redo === undefined) return;
      if (redo) { prompt = await rewrite(); if (!prompt) return; }
    }
    const [plugin, ...rest] = $("#notifyTo").value.split("|");
    const body = { prompt, params: collectParams(), inputs: collectInputs(), title: $("#title").value, description: $("#desc").value,
      notify: rest.length ? { plugin, target: rest.join("|") } : false, music: $("#music").checked, source: "web", ui: snapshot() };
    let d;
    try { d = await api("/api/jobs", { method: "POST", body }); }
    catch (e) {
      if (e.code !== "engine_off") throw e;
      const go = await ask(t("ask.off"), [{ label: t("ask.off.no"), value: false, cls: "ghost" }, { label: t("ask.off.yes"), value: true, cls: "primary" }]);
      if (!go) return;
      d = await api("/api/jobs", { method: "POST", body: { ...body, autostart: true } });
    }
    $("#formMsg").textContent = "";
    toast(d.ahead > 0 ? t("t.queued", { n: d.ahead }) : t("t.go"));
    poll(true);
  } catch (e) { toast(e.message); $("#formMsg").textContent = e.message; }
  finally { btn.disabled = false; btn.classList.remove("loading"); }
}

// ------------------------------------------------------------ status and gallery
let lastLabel = "";
function renderPlugins() {
  const s = st.status; if (!s) return;
  // AI rewrite: only when a rewriter plugin is loaded
  $("#aiToggle").hidden = $("#rewriteBtn").hidden = !s.rewriter;
  if (s.rewriter) $("#aiToggleText").textContent = t("ai.toggle", { name: s.rewriter.label });
  // Notifications: one option per target offered by notifier plugins
  const targets = s.notify_targets || [], sig = LANG + JSON.stringify(targets);
  $("#notifyWrap").hidden = !targets.length;
  if (sig !== st.notifySig) {
    const sel = $("#notifyTo"), want = st.notifySig === null ? st.notifyWant : sel.value;
    sel.innerHTML = `<option value="">${t("out.none")}</option>` + targets.map(x => `<option value="${esc(x.plugin + "|" + x.target)}">${esc(x.label)}</option>`).join("");
    sel.value = [...sel.options].some(o => o.value === want) ? want : "";
    st.notifySig = sig;
  }
  // Translation
  const trs = s.translators || [], ok = trs.filter(x => x.available), mode = $("#translateMode").value;
  const eng = mode === "off" ? null : mode === "local" ? ok.find(x => x.local) : ok[0];
  const typedChinese = CJK.test($("#desc").value + st.uploads.refs.map(x => (x.what || "") + (x.desc || "")).join(""));
  $(".translate-row").hidden = !trs.length && LANG === "en" && !typedChinese;   // nothing to say to someone writing English
  $("#translateMode option[value=local]").hidden = !trs.some(x => x.local);
  const hint = $("#translateHint");
  hint.textContent = mode === "off" ? t("tr.hint.off") : !eng ? t("tr.hint.none") : eng.local ? t("tr.hint.local") : t("tr.hint.cloud", { name: eng.label });
  hint.classList.toggle("warn", mode !== "off" && !eng && (LANG === "zh" || typedChinese));
  // Setup problems
  const warn = $("#setupWarn"), msg = !s.setup?.h3 ? t("setup.h3") : !s.setup?.fl2va ? t("setup.model") : "";
  warn.hidden = !msg; warn.textContent = msg;
  $("#engineSwitch").title = t("engine.title", { n: s.engine.idle_minutes });
}
function renderStatus() {
  const s = st.status;
  if (st.offline) { $("#engineLabel").textContent = t("engine.lost"); $("#engineLabel").className = "pill off"; }
  if (!s) return;
  renderPlugins();
  const e = s.engine, c = s.current, label = $("#engineLabel"), sub = $("#engineSub");
  document.body.classList.toggle("busy", !!c);
  $("#engineToggle").checked = e.enabled && !e.stopping;
  let cls, text, subText = "";
  if (!e.enabled) { cls = "off"; text = t("engine.off"); subText = e.by === "idle" ? t("engine.offIdle") : t("engine.offHint"); }
  else if (e.stopping) { cls = "stopping"; text = t("engine.stopping"); }
  else if (c) { cls = "busy"; text = t("engine.busy"); subText = s.queue.length ? t("engine.queued", { n: s.queue.length }) : ""; }
  else { cls = "on"; text = t("engine.on"); subText = e.idle_left_s != null ? t("engine.idleLeft", { n: Math.max(1, Math.round(e.idle_left_s / 60)) }) : ""; }
  if (s.memory?.free_pct != null) subText += `${subText ? " · " : ""}${t("engine.mem", { n: s.memory.free_pct })}`;
  if (!st.offline) { label.className = `pill ${cls}`; label.textContent = text; }
  sub.textContent = subText;
  if (lastLabel && lastLabel !== cls) retrigger(label, "bump");
  lastLabel = cls;

  const box = $("#current");
  if (c) {
    const pr = c.progress, el = Date.now() / 1000 - c.started, pct = pr?.overall ?? (pr ? Math.round((pr.done / pr.total) * 100) : 3);
    const cover = c.inputs?.ref_images?.[0] || c.inputs?.first_frame || "";
    // The GPU runs every denoising step in one submission and h3 reports nothing meanwhile: say it is not stuck.
    const gpuWait = pr && pr.phase === "denoise enqueue" && pr.done === pr.total;
    const phase = pr ? (STR[LANG]["ph." + pr.phase] ?? STR.en["ph." + pr.phase] ?? pr.phase) : "";
    box.innerHTML = `<div class="job">
        <div class="flip"><span></span><span></span><span style="${cover ? `background-image:url('${cover}')` : ""}"></span></div>
        <div style="min-width:0"><div class="job-title">${esc(c.title || c.mode_name)}</div>
          <div class="job-meta">${esc(c.mode_name)} · ${c.params.width}×${c.params.height} · ${c.params.seconds ?? (c.params.frames / 24).toFixed(1)} ${t("sec")} · ${t("job.elapsed", { t: fmtDur(el) })}</div>
          <div class="progress"><div style="width:${pct}%"></div></div>
          <div class="job-step"><span>${!pr ? t("job.warm") : gpuWait ? t("job.gpu") : t("job.step", { label: esc(phase), done: pr.done, total: pr.total, pct })}</span><button class="btn sm danger" data-cancel="${c.id}">${t("cancel")}</button></div></div></div>`;
  } else {
    box.innerHTML = `<div class="idle"><span class="zzz">${e.enabled ? "☕" : "💤"}</span><span>${e.enabled ? t("idle.on") : t("idle.off")}</span></div>`;
  }
  $("#queue").innerHTML = s.queue.map(q => `<div class="queue-item"><span>⏳ ${esc(q.title || q.mode_name)} · ${q.params.width}×${q.params.height}</span><button class="btn sm" data-cancel="${q.id}">${t("cancel")}</button></div>`).join("");
  $("#queueCount").textContent = s.queue.length ? t("queue.n", { n: s.queue.length }) : "";
}
function renderHistory() {
  const list = st.jobs.filter(j => !["queued", "running"].includes(j.status));
  $("#histCount").textContent = list.length ? t("wall.n", { n: list.length }) : "";
  const now = Date.now() / 1000;
  $("#history").innerHTML = list.length ? list.map((j, i) => {
    const fresh = j.status === "done" && now - (j.finished || 0) < 3600 && !st.seen.has(j.id);
    return `<div class="card" tabindex="0" data-job="${j.id}" style="--i:${i}">
      <div class="media">${j.poster ? `<img loading="lazy" src="${j.poster}" alt="">` : `<span class="none">${j.status === "done" ? t("card.video") : t("card.none")}</span>`}</div>
      ${j.status === "done" ? `<span class="play">▶</span>` : ""}${fresh ? `<span class="new">NEW!</span>` : ""}
      <button class="del" data-del="${j.id}" title="${t("card.del")}" aria-label="${t("card.del")}">✕</button>
      <div class="meta"><b>${esc(j.title || j.mode_name)}</b>
        <span class="badge ${j.status}">${STR[LANG]["st." + j.status] ?? j.status}</span>
        ${j.metrics?.wall_s ? fmtDur(j.metrics.wall_s) : ""}</div></div>`;
  }).join("") : `<div class="wall-empty"><div>🎬</div>${t("wall.empty")}</div>`;
}
async function openDetail(id) {
  const j = await api(`/api/jobs/${id}`);
  st.seen.add(id); try { localStorage.setItem(SEEN_KEY, JSON.stringify([...st.seen].slice(-200))); } catch {}
  const p = j.params, m = j.metrics || {}, title = j.title || j.mode_name;
  const inputs = [...(j.inputs.ref_images || []), j.inputs.first_frame, j.inputs.last_frame].filter(Boolean);
  const quality = [t("q." + p.quality), t("d.steps", { n: p.steps }), t("d.layers", { n: p.layers }), p.core_reuse > 1 && t("d.core", { n: p.core_reuse }),
    p.reuse > 1 && t("d.reuse", { n: p.reuse }), p.token_reduction && t("d.token")].filter(Boolean).join(" · ");
  $("#detailBody").innerHTML = `<h2>${esc(title)} <span class="badge ${j.status}">${esc(j.mode_name)}</span></h2><div class="detail">
    <div>${j.video ? `<video src="${j.video}" controls autoplay loop playsinline poster="${j.poster || ""}"></video>` : `<p class="hint">${esc(j.error || t("d.none"))}</p>`}
      ${inputs.length ? `<div class="in-thumbs">${inputs.map(u => `<img src="${u}" alt="${t("d.input")}">`).join("")}</div>` : ""}</div>
    <div><dl>
      <dt>${t("d.spec")}</dt><dd>${p.width}×${p.height}${p.render_width ? ` (${t("d.inner", { w: p.render_width, h: p.render_height })})` : ""} · ${p.seconds ?? (p.frames / 24).toFixed(1)} ${t("sec")}</dd>
      <dt>${t("d.quality")}</dt><dd>${quality}</dd>
      <dt>${t("d.seed")}</dt><dd>${p.seed}</dd>
      ${m.wall_s ? `<dt>${t("d.time")}</dt><dd>${fmtDur(m.wall_s)}${m.denoise_s ? ` (${t("d.timeSplit", { a: fmtDur(m.denoise_s), b: fmtDur(m.vae_s) })})` : ""}</dd>` : ""}
      ${m.peak_gb ? `<dt>${t("d.peak")}</dt><dd>${m.peak_gb} GB</dd>` : ""}
      <dt>${t("d.from")}</dt><dd>${j.source === "chat" ? t("d.chat") : t("d.web")} · ${new Date(j.created * 1000).toLocaleString()}</dd>
    </dl>
    ${j.description ? `<label class="sub">${t("d.desc")}</label><p style="margin:0 0 6px;font-weight:700">${esc(j.description)}</p>` : ""}
    <details><summary>${t("d.prompt")}</summary><pre class="mono">${esc(j.prompt)}</pre></details>
    ${j.status !== "done" ? `<details open><summary>${t("d.log")}</summary><pre class="mono">${esc(j.log_tail)}</pre></details>` : ""}
    <div class="acts">
      ${j.video ? `<a class="btn primary" href="${j.video}" download="${esc(title)}.mp4">${t("d.download")}</a>` : ""}
      <button class="btn" data-reuse="${j.id}">${t("d.reuseBtn")}</button>
      <button class="btn" data-again="${j.id}">${t("d.again")}</button>
      <button class="btn danger" data-del="${j.id}">${t("d.delete")}</button>
    </div></div></div>`;
  $("#detailDlg")._job = j;
  if (!$("#detailDlg").open) $("#detailDlg").showModal();
  renderHistory();
}

// ------------------------------------------------------------ form snapshot ("reuse settings", remembered form)
const FIELDS = ["translateMode", "music", "useAI", "desc", "dialogue", "lang", "seconds", "cw", "ch", "steps", "layers", "reuseMode", "seedMode", "seed",
  "renderMode", "crop", "refSize", "tokenRed", "refRope", "ssd", "int8row", "noZeroCopy", "qwenPf", "qwenDepth", "title", "notifyTo", "prompt"];
function snapshot() {
  const f = {};
  for (const id of FIELDS) { const el = $("#" + id); f[id] = el.type === "checkbox" ? el.checked : el.value; }
  if (st.notifySig === null) f.notifyTo = st.notifyWant;     // targets not loaded yet: keep what was saved
  return { mode: st.mode, aspect: st.aspect, size: st.size, quality: st.quality, fields: f, uploads: st.uploads, promptFor: st.promptFor };
}
function restore(s) {
  if (!s) return;
  const f = { ...(s.fields || {}) };
  if (f.translateMode === "system") f.translateMode = "local";          // renamed
  if (f.notifyTo && !f.notifyTo.includes("|")) f.notifyTo = "";         // older versions stored a profile name
  st.notifyWant = f.notifyTo || "";
  for (const [id, v] of Object.entries(f)) { const el = $("#" + id); if (!el) continue; el.type === "checkbox" ? (el.checked = !!v) : (el.value = v); }
  if (!$("#translateMode").value) $("#translateMode").value = "auto";
  if (!$("#lang").value) $("#lang").value = "English";
  $("#secondsRange").value = $("#seconds").value;
  st.aspect = s.aspect || st.aspect; st.size = s.size || st.size; st.promptFor = s.promptFor || null;
  if (s.uploads) st.uploads = s.uploads;
  if ($("#prompt").value) st.promptState = "p.has";
  setMode(s.mode || "t2v"); setQuality(s.quality || "recommended"); renderAspect(); renderUploads();
}
function saveForm() { try { localStorage.setItem(STORE_KEY, JSON.stringify(snapshot())); } catch {} }
function loadForm() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch {}
  if (saved) restore(saved);
  else $("#lang").value = LANG === "zh" ? "Chinese" : "English";       // first visit: speak the UI language
}

// ------------------------------------------------------------ language
function renderAll() {
  applyI18n();
  renderAspect(); setMode(st.mode); renderUploads(); renderStatus(); renderHistory();
  if ($("#detailDlg").open && $("#detailDlg")._job) openDetail($("#detailDlg")._job.id);
}

// ------------------------------------------------------------ polling
let pollTimer = null;
async function poll(refreshJobs = false) {
  clearTimeout(pollTimer);
  try {
    const prevCur = st.status?.current?.id;
    st.status = await api("/api/status"); st.offline = false;
    renderStatus();
    if (refreshJobs || prevCur !== st.status.current?.id || !st.jobs.length) {
      st.jobs = await api("/api/jobs?limit=60"); renderHistory();
      if (prevCur && prevCur !== st.status.current?.id) {
        const j = st.jobs.find(x => x.id === prevCur);
        if (j?.status === "done") { const card = $(`.card[data-job="${j.id}"]`); const r = card?.getBoundingClientRect();
          confetti(r ? r.left + r.width / 2 : undefined, r ? r.top + r.height / 3 : undefined); toast(t("t.done", { x: j.title || j.mode_name })); }
        else if (j?.status === "failed") toast(t("t.failed", { x: j.error || j.title }), 6000);
      }
    }
  } catch { st.offline = true; renderStatus(); }
  pollTimer = setTimeout(poll, st.status?.current ? 2000 : 6000);
}

// ------------------------------------------------------------ events
function bind() {
  $("#langBtn").onclick = () => { setLang(LANG === "zh" ? "en" : "zh"); renderAll(); poll(true); };
  $("#modeTabs").onclick = e => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); };
  $("#aspectChips").onclick = e => { const b = e.target.closest("button"); if (b) { st.aspect = b.dataset.a; renderAspect(); } };
  $("#sizeSel").onchange = e => { st.size = e.target.value.split("x").map(Number); refreshEstimate(); };
  $("#qualitySeg").onclick = e => { const b = e.target.closest("button"); if (b) setQuality(b.dataset.q); };
  $("#secondsRange").oninput = e => { $("#seconds").value = e.target.value; refreshEstimate(); };
  $("#seconds").oninput = e => { $("#secondsRange").value = e.target.value; refreshEstimate(); };
  $$("input, select, textarea").forEach(el => el.addEventListener("change", refreshEstimate));
  $("#title").addEventListener("input", refreshEstimate);
  $("#prompt").addEventListener("input", () => { st.promptFor = null; setPromptState("p.edited"); saveForm(); });
  $("#useAI").addEventListener("change", syncAIState);
  $("#translateMode").addEventListener("change", () => { renderPlugins(); saveForm(); });
  const desc = $("#desc");
  desc.addEventListener("input", () => { pop.idx = 0; updateMentionPop(); });
  desc.addEventListener("change", renderPlugins);
  desc.addEventListener("click", updateMentionPop);
  desc.addEventListener("blur", () => setTimeout(closeMentionPop, 150));       // leave time for a click on the list
  desc.addEventListener("keydown", e => {
    if (!pop.open) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); pop.idx = (pop.idx + (e.key === "ArrowDown" ? 1 : -1) + pop.items.length) % pop.items.length; updateMentionPop(); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); insertMention(pop.items[pop.idx].token, pop.from); }
    else if (e.key === "Escape") { e.preventDefault(); closeMentionPop(); }
  });
  $("#mentionPop").addEventListener("mousedown", e => { const b = e.target.closest("button"); if (b) { e.preventDefault(); insertMention(pop.items[+b.dataset.i].token, pop.from); } });
  $("#mentionBar").addEventListener("click", e => { const b = e.target.closest("button"); if (b) insertMention(b.dataset.token); });
  $("#rewriteBtn").onclick = rewrite;
  $("#clearPromptBtn").onclick = () => { $("#prompt").value = ""; st.promptFor = null; syncAIState(); saveForm(); };
  $("#submitBtn").onclick = submit;
  $$(".drop").forEach(bindDrop);
  addEventListener("resize", moveBlob);
  $("#engineToggle").onchange = async e => {
    try {
      if (e.target.checked) { await api("/api/engine", { method: "POST", body: { action: "start" } }); toast(t("t.engineOn")); retrigger($(".clapper"), "clap"); }
      else {
        let force = false;
        if (st.status?.current) {
          const v = await ask(t("ask.busy"), [{ label: t("ask.busy.wait"), value: false, cls: "ghost" }, { label: t("ask.busy.force"), value: true, cls: "primary" }]);
          if (v === undefined) { e.target.checked = true; return; }
          force = v;
        }
        const r = await api("/api/engine", { method: "POST", body: { action: "stop", force } });
        toast(r.result === "stopping" ? t("t.engineStopping") : t("t.engineOff"));
      }
    } catch (err) { toast(err.message, 6000); }
    poll(true);
  };
  document.body.addEventListener("click", async e => {
    const el = e.target.closest("[data-cancel],[data-job],[data-reuse],[data-again],[data-del]"); if (!el) return;
    try {
      if (el.dataset.cancel) {
        if (!(await ask(t("ask.cancel"), [{ label: t("ask.cancel.no"), value: false, cls: "ghost" }, { label: t("ask.cancel.yes"), value: true, cls: "primary" }]))) return;
        await api(`/api/jobs/${el.dataset.cancel}/cancel`, { method: "POST", body: {} }); toast(t("t.canceled")); poll(true);
      } else if (el.dataset.del) {
        if (!(await ask(t("ask.del"), [{ label: t("ask.del.no"), value: false, cls: "ghost" }, { label: t("ask.del.yes"), value: true, cls: "primary" }]))) return;
        await api(`/api/jobs/${el.dataset.del}`, { method: "DELETE" });
        if ($("#detailDlg").open) $("#detailDlg").close();
        toast(t("t.deleted")); poll(true);
      } else if (el.dataset.job) openDetail(el.dataset.job);
      else if (el.dataset.reuse || el.dataset.again) {
        const j = $("#detailDlg")._job;
        if (!j.ui) { toast(t("t.noUi")); return; }
        st.notifySig = null;                       // let the saved notification target be applied again
        restore(j.ui); $("#prompt").value = j.prompt; st.promptFor = promptSignature(); renderPlugins();
        if (el.dataset.again) $("#seedMode").value = "random";
        $("#detailDlg").close(); scrollTo({ top: 0, behavior: REDUCED ? "auto" : "smooth" });
        toast(el.dataset.again ? t("t.loadedAgain") : t("t.loaded")); syncAIState(); refreshEstimate();
      }
    } catch (err) { toast(err.message); }
  });
  $("#history").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.dataset.job) openDetail(e.target.dataset.job); });
  // Closing the detail view (button, Esc or backdrop) must stop playback: pause, then drop the player entirely.
  const dlg = $("#detailDlg");
  dlg.addEventListener("close", () => {
    $$("video, audio", dlg).forEach(m => { m.pause(); m.removeAttribute("src"); m.load(); });
    $("#detailBody").innerHTML = ""; dlg._job = null;
  });
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
}

applyI18n(); bind(); renderAspect(); setMode("t2v"); loadForm(); renderUploads(); renderStatus(); poll(true);
document.fonts?.ready.then(moveBlob);
