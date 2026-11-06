const $ = (s, el = document) => el.querySelector(s);
const esc = (t) => (t ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bookId = Number(location.pathname.split("/").pop());
const BUSY = ["queued", "analyzing", "translating", "exporting"];

const FONTS = [
  ["Noto Naskh Arabic", "نسخ"], ["Amiri", "أميري"], ["IBM Plex Sans Arabic", "بلكس"],
  ["Tajawal", "تجوّل"], ["Noto Kufi Arabic", "كوفي"],
];
const THEMES = [["light", "فاتح", "#FBF8F1", "#262019"], ["sepia", "سيبيا", "#F1E4C8", "#3E2F1C"],
                ["dark", "داكن", "#1C1A17", "#DDD5C7"], ["black", "أسود", "#000", "#C9C3B8"]];
const DEFAULTS = { size: 20, font: "Noto Naskh Arabic", theme: "light", lh: 2, width: 38, align: "justify",
                   orig: "tap", showFlags: false, layout: "pages" };
const paged = () => settings.layout === "pages" && settings.orig !== "side";
let pw = 0, pageNo = 0, pages = 1;

let data = null, settings = { ...DEFAULTS }, perBook = false, bookmarks = [], paraEls = [], current = null;
let srcRtl = false;

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2200); }
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("سجّل الدخول"); }
  if (!r.ok) { let d = "حدث خطأ"; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
  return r.json();
}
const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function lsGet(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }

/* ---------------- settings ---------------- */
function applySettings() {
  const b = $("#book");
  b.style.setProperty("--size", `${settings.size}px`);
  b.style.setProperty("--font", `'${settings.font}'`);
  b.style.setProperty("--lh", settings.lh);
  b.style.setProperty("--w", `${settings.width}em`);
  b.classList.toggle("align-right", settings.align === "right");
  document.body.dataset.rtheme = settings.theme;
  $("#sizeOut").textContent = settings.size;
  $("#lh").value = settings.lh; $("#width").value = settings.width; $("#showFlags").checked = settings.showFlags;
  $("#perBook").checked = perBook;
  document.querySelectorAll("#fontOpts button").forEach((x) => x.classList.toggle("on", x.dataset.v === settings.font));
  document.querySelectorAll("#themeOpts button").forEach((x) => x.classList.toggle("on", x.dataset.v === settings.theme));
  document.querySelectorAll("#alignOpts button").forEach((x) => x.classList.toggle("on", x.dataset.v === settings.align));
  document.querySelectorAll("#origOpts button").forEach((x) => x.classList.toggle("on", x.dataset.v === settings.orig));
  document.querySelectorAll("#layoutOpts button").forEach((x) => x.classList.toggle("on", x.dataset.v === settings.layout));
  relayout();
  const meta = document.querySelector('meta[name="theme-color"]') || document.head.appendChild(Object.assign(document.createElement("meta"), { name: "theme-color" }));
  meta.content = THEMES.find((t) => t[0] === settings.theme)[2];
}

let saveSettingsTimer;
function changeSetting(key, value, rerender = false) {
  const anchor = current;
  settings[key] = value;
  applySettings();
  if (rerender) renderBook();
  if (anchor) scrollToPara(anchor, false, "auto");
  clearTimeout(saveSettingsTimer);
  saveSettingsTimer = setTimeout(() => {
    if (perBook) api(`/api/books/${bookId}/state`, jsonOpts("PUT", { settings: { ...settings, _perBook: true } })).catch(() => {});
    else { lsSet("reader-settings", settings); api(`/api/books/${bookId}/state`, jsonOpts("PUT", { settings: {} })).catch(() => {}); }
  }, 400);
}

function buildSettingsUI() {
  $("#fontOpts").innerHTML = FONTS.map(([f, l]) => `<button data-v="${f}" style="font-family:'${f}'">${l}</button>`).join("");
  $("#themeOpts").innerHTML = THEMES.map(([k, l, bg, fg]) => `<button data-v="${k}" style="background:${bg};color:${fg}">${l}</button>`).join("");
  $("#fontOpts").onclick = (e) => e.target.dataset.v && changeSetting("font", e.target.dataset.v);
  $("#themeOpts").onclick = (e) => e.target.dataset.v && changeSetting("theme", e.target.dataset.v);
  $("#alignOpts").onclick = (e) => e.target.dataset.v && changeSetting("align", e.target.dataset.v);
  $("#origOpts").onclick = (e) => e.target.dataset.v && changeSetting("orig", e.target.dataset.v, true);
  $("#layoutOpts").onclick = (e) => e.target.dataset.v && changeSetting("layout", e.target.dataset.v);
  document.querySelectorAll("[data-step]").forEach((b) => b.onclick = () =>
    changeSetting("size", Math.min(36, Math.max(13, settings.size + Number(b.dataset.d)))));
  $("#lh").oninput = (e) => changeSetting("lh", Number(e.target.value));
  $("#width").oninput = (e) => changeSetting("width", Number(e.target.value));
  $("#showFlags").onchange = (e) => changeSetting("showFlags", e.target.checked, true);
  $("#perBook").onchange = (e) => { perBook = e.target.checked; changeSetting("size", settings.size); toast(perBook ? "الإعدادات خاصة بهذا الكتاب" : "الإعدادات عامة لكل الكتب"); };
  $("#resetSettings").onclick = () => { settings = { ...DEFAULTS }; changeSetting("size", DEFAULTS.size, true); };
}

/* ---------------- rendering ---------------- */
function paraHTML(p) {
  const flag = settings.showFlags && p.f ? " flag" : "";
  const marked = bookmarks.some((b) => b.paragraph_id === p.id) ? " marked" : "";
  const title = settings.showFlags && p.f ? ' title="فقرة منخفضة الثقة — تحتاج مراجعة"' : "";
  if (p.k === "code") return `<pre class="para code${marked}" data-id="${p.id}" dir="ltr">${esc(p.s)}</pre>`;
  const tag = p.k === "h2" || p.k === "h3" ? p.k : "p";
  const text = p.t ? esc(p.t) : `<span class="muted">قيد الترجمة…</span>`;
  const main = `<${tag} class="para ${p.k}${flag}${marked}${p.t ? "" : " pending"}" data-id="${p.id}"${title}>${text}</${tag}>`;
  if (settings.orig === "side" && p.k !== "h1")
    return `<div class="pair">${main}<div class="orig ${srcRtl ? "rtl" : ""}">${esc(p.s)}</div></div>`;
  return main;
}

function renderBook() {
  const side = settings.orig === "side";
  $("#book").classList.toggle("side", side);
  $("#book").innerHTML = data.chapters.map((ch) => {
    const paras = ch.paragraphs.filter((p) => p.k !== "h1");
    const h1 = ch.paragraphs.find((p) => p.k === "h1");
    return `<section class="chapter" id="ch-${ch.id}" data-cid="${ch.id}">
      <h1 class="ch-title"${h1 ? ` data-id="${h1.id}"` : ""}>${esc(ch.title)}</h1>
      ${!ch.done && !paras.some((p) => p.t) ? `<div class="ch-pending">هذا الفصل قيد الترجمة والتدقيق…</div>` :
        paras.map(paraHTML).join("")}</section>`;
  }).join("");
  paraEls = [...document.querySelectorAll("#book [data-id]")];
  buildToc();
  relayout();
}

function buildToc() {
  $("#tocList").innerHTML = data.chapters.map((ch, i) => `<li data-cid="${ch.id}"><button onclick="goChapter(${ch.id})">
    <span class="n">${(i + 1).toLocaleString("ar-EG")}</span><span>${esc(ch.title)}</span>
    ${ch.done ? "" : `<span class="wait">قيد الترجمة</span>`}</button></li>`).join("");
}

/* ---------------- page mode ---------------- */
function relayout() {
  const on = paged();
  document.body.classList.toggle("paged", on);
  const book = $("#book"), page = $("#page");
  if (!on) { book.style.columnWidth = book.style.columnGap = book.style.padding = ""; return; }
  pw = page.clientWidth;
  const textW = Math.min(pw - 40, settings.width * settings.size * 0.62);   // the column width setting, in px
  const pad = Math.max(20, (pw - textW) / 2);
  book.style.padding = `0 ${pad}px`;
  book.style.columnWidth = `${pw - 2 * pad}px`;
  book.style.columnGap = `${2 * pad}px`;
  pages = Math.max(1, Math.round(book.scrollWidth / pw));
  goPage(Math.min(pageNo, pages - 1), "auto");
}
function goPage(n, behavior = "smooth") {
  if (!paged()) return;
  pageNo = Math.max(0, Math.min(pages - 1, n));
  $("#page").scrollTo({ left: -pageNo * pw, behavior });     // RTL: pages run leftwards
  hidePop();
  setTimeout(track, behavior === "auto" ? 0 : 350);
}
function firstBox(el) { const rs = el.getClientRects(); return rs.length ? rs[0] : el.getBoundingClientRect(); }
function pageOf(el) {
  const c = $("#page").getBoundingClientRect(), r = firstBox(el);
  return Math.max(0, Math.floor((c.right - r.right + pageNo * pw + 2) / pw));
}
$("#turnNext").onclick = () => goPage(pageNo + 1);
$("#turnPrev").onclick = () => goPage(pageNo - 1);
let touchX = null;
$("#page").addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
$("#page").addEventListener("touchend", (e) => {
  if (touchX == null || !paged()) return;
  const dx = e.changedTouches[0].clientX - touchX; touchX = null;
  if (Math.abs(dx) > 45) goPage(pageNo + (dx > 0 ? 1 : -1));   // Arabic books turn toward the right
});
addEventListener("resize", () => { const a = current; relayout(); if (a) scrollToPara(a, false, "auto"); });

function goChapter(cid) {
  closePanels();
  const sec = document.getElementById(`ch-${cid}`);
  if (paged()) goPage(pageOf(sec.querySelector(".ch-title") || sec)); else sec.scrollIntoView({ behavior: "smooth" });
}

function scrollToPara(id, flash = true, behavior = "smooth") {
  const el = document.querySelector(`#book [data-id="${id}"]`);
  if (!el) return;
  if (paged()) goPage(pageOf(el), behavior);
  else window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - 90, behavior });
  if (flash) { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1400); }
}

/* ---------------- position tracking ---------------- */
let saveTimer, lastY = 0;
function track() {
  let found = null, pct;
  if (paged()) {
    const c = $("#page").getBoundingClientRect();
    let spanning = null;
    for (const el of paraEls) {
      const r = firstBox(el);
      if (r.right > c.left + 1 && r.left < c.right - 1 && r.height) { found = el; break; }   // starts on this page
      if (!spanning) for (const f of el.getClientRects()) if (f.right > c.left + 1 && f.left < c.right - 1) { spanning = el; break; }
    }
    found = found || spanning;
    pct = pages > 1 ? (pageNo / (pages - 1)) * 100 : 100;
  } else {
    for (const el of paraEls) { if (el.getBoundingClientRect().bottom > 100) { found = el; break; } }
    const docH = document.documentElement.scrollHeight - innerHeight;
    pct = docH > 0 ? Math.min(100, (scrollY / docH) * 100) : 100;
  }
  if (!found) return;
  current = Number(found.dataset.id);
  const idx = paraEls.indexOf(found);
  const ch = data.chapters.find((c) => c.id === Number(found.closest(".chapter").dataset.cid));
  $("#chapterName").textContent = ch ? ch.title : "";
  $("#fchap").textContent = ch ? ch.title : "";
  $("#fpct").textContent = paged() ? `صفحة ${(pageNo + 1).toLocaleString("ar-EG")} من ${pages.toLocaleString("ar-EG")}` : `${Math.round(pct)}%`;
  $("#fprog").style.width = `${pct}%`;
  document.querySelectorAll("#tocList li").forEach((li) => li.classList.toggle("current", ch && Number(li.dataset.cid) === ch.id));
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api(`/api/books/${bookId}/state`, jsonOpts("PUT", { paragraph_id: current, percent: pct })).catch(() => {}), 1200);
  return idx;
}
addEventListener("scroll", () => {
  requestAnimationFrame(track);
  const y = scrollY, down = y > lastY + 6, up = y < lastY - 6;
  if (down && y > 200) { $("#rbar").classList.add("hide"); $("#rfoot").classList.add("hide"); hidePop(); }
  if (up) { $("#rbar").classList.remove("hide"); $("#rfoot").classList.remove("hide"); }
  lastY = y;
}, { passive: true });

/* ---------------- paragraph popover ---------------- */
let popFor = null;
function hidePop() { $("#pop").hidden = true; popFor = null; }
$("#page").addEventListener("click", (e) => {
  if (window.getSelection().toString()) return;
  if (paged()) {
    const x = e.clientX / innerWidth;
    if (x < 0.22) { goPage(pageNo + 1); return; }
    if (x > 0.78) { goPage(pageNo - 1); return; }
  }
  const el = e.target.closest("[data-id]");
  if (!el || el.classList.contains("ch-title")) { hidePop(); $("#rbar").classList.toggle("hide"); $("#rfoot").classList.toggle("hide"); return; }
  popFor = Number(el.dataset.id);
  const pop = $("#pop");
  pop.querySelector('[data-act="orig"]').hidden = settings.orig === "side";
  pop.querySelector('[data-act="mark"]').textContent = bookmarks.some((b) => b.paragraph_id === popFor) ? "🔖 إزالة العلامة" : "🔖 علامة";
  pop.hidden = false;
  const r = el.getBoundingClientRect();
  pop.style.top = `${scrollY + r.top - pop.offsetHeight - 8 < scrollY + 70 ? scrollY + r.bottom + 8 : scrollY + r.top - pop.offsetHeight - 8}px`;
  pop.style.left = `${Math.max(8, Math.min(innerWidth - pop.offsetWidth - 8, e.clientX - pop.offsetWidth / 2))}px`;
});
$("#pop").addEventListener("click", async (e) => {
  const act = e.target.dataset.act; if (!act || popFor == null) return;
  const p = findPara(popFor);
  if (act === "copy") { try { await navigator.clipboard.writeText(p.t || p.s); toast("تم النسخ"); } catch { toast("تعذر النسخ"); } }
  if (act === "orig") {
    const el = document.querySelector(`#book [data-id="${popFor}"]`);
    const next = el.nextElementSibling;
    if (next && next.classList.contains("orig")) next.remove();
    else el.insertAdjacentHTML("afterend", `<div class="orig ${srcRtl ? "rtl" : ""}">${esc(p.s)}</div>`);
  }
  if (act === "mark") await toggleMark(popFor);
  hidePop();
});
function findPara(id) { for (const c of data.chapters) for (const p of c.paragraphs) if (p.id === id) return { ...p, chapter: c }; return null; }

/* ---------------- bookmarks ---------------- */
async function loadMarks() { bookmarks = await api(`/api/books/${bookId}/bookmarks`); renderMarks(); }
function renderMarks() {
  $("#marksList").innerHTML = bookmarks.length ? bookmarks.map((b) => {
    const p = findPara(b.paragraph_id);
    if (!p) return "";
    return `<li><button onclick="closePanels();scrollToPara(${b.paragraph_id})"><span class="ch">${esc(p.chapter.title)}</span>
      <span class="snip">${esc((p.t || p.s).slice(0, 140))}</span></button>
      <button class="btn icon ghost" aria-label="حذف" onclick="deleteMark(${b.id})"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></li>`;
  }).join("") : `<li class="empty-note">لا توجد علامات بعد. انقر على أي فقرة واختر «علامة».</li>`;
  document.querySelectorAll("#book [data-id]").forEach((el) =>
    el.classList.toggle("marked", bookmarks.some((b) => b.paragraph_id === Number(el.dataset.id))));
}
async function toggleMark(pid) {
  const ex = bookmarks.find((b) => b.paragraph_id === pid);
  if (ex) { await deleteMark(ex.id); return; }
  await api(`/api/books/${bookId}/bookmarks`, jsonOpts("POST", { paragraph_id: pid }));
  toast("تمت إضافة العلامة"); loadMarks();
}
async function deleteMark(id) { await api(`/api/books/${bookId}/bookmarks/${id}`, { method: "DELETE" }); loadMarks(); }
$("#markHere").onclick = () => current && toggleMark(current);

/* ---------------- panels ---------------- */
function closePanels() { document.querySelectorAll(".panel").forEach((p) => p.hidden = true); $("#scrim").hidden = true; }
document.querySelectorAll("[data-panel]").forEach((b) => b.onclick = () => {
  const panel = $(`#panel-${b.dataset.panel}`), open = !panel.hidden;
  closePanels(); hidePop();
  if (!open) { panel.hidden = false; $("#scrim").hidden = false; }
});
document.querySelectorAll(".panel .close").forEach((b) => b.onclick = closePanels);
$("#scrim").onclick = closePanels;
addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePanels(); hidePop(); }
  if (e.target.matches("input")) return;
  if (paged() && ["ArrowLeft", "PageDown", " "].includes(e.key)) { e.preventDefault(); goPage(pageNo + 1); }
  if (paged() && ["ArrowRight", "PageUp"].includes(e.key)) { e.preventDefault(); goPage(pageNo - 1); }
  if (e.key === "+" || e.key === "=") changeSetting("size", Math.min(36, settings.size + 1));
  if (e.key === "-") changeSetting("size", Math.max(13, settings.size - 1));
});

/* ---------------- boot ---------------- */
async function refreshWhileBusy() {
  if (!BUSY.includes(data.book.status)) return;
  setTimeout(async () => {
    const anchor = current;
    data = await api(`/api/books/${bookId}/content`);
    renderBook(); renderMarks();
    if (anchor) scrollToPara(anchor, false, "auto");
    refreshWhileBusy();
  }, 30000);
}

(async () => {
  buildSettingsUI();
  const [content, state] = await Promise.all([api(`/api/books/${bookId}/content`), api(`/api/books/${bookId}/state`)]);
  data = content;
  srcRtl = ["ar", "fa", "ur", "he"].includes(data.book.source_lang);
  document.title = data.book.display_title;
  $("#bookTitle").textContent = data.book.display_title;
  if (data.book.has_pdf) { const l = $("#pdfLink"); l.hidden = false; l.href = `/api/books/${bookId}/pdf`; }
  if (state.settings && state.settings._perBook) { perBook = true; settings = { ...DEFAULTS, ...state.settings }; delete settings._perBook; }
  else settings = { ...DEFAULTS, ...(lsGet("reader-settings") || {}) };
  applySettings();
  renderBook();
  await document.fonts.ready; relayout();
  await loadMarks();
  if (state.paragraph_id) { scrollToPara(state.paragraph_id, true, "auto"); toast("تابع من حيث توقفت"); }
  track();
  refreshWhileBusy();
})().catch((e) => { $("#book").innerHTML = `<p class="ch-pending">${esc(e.message)}</p>`; });
