const $ = (s, el = document) => el.querySelector(s);
const esc = (t) => (t ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const BUSY = ["queued", "analyzing", "translating", "exporting"];
const STATUS = { queued: "في الانتظار", analyzing: "تحليل", translating: "ترجمة", exporting: "إخراج",
                 ready: "جاهز", needs_review: "يحتاج مراجعة", failed: "فشل", no_credit: "بانتظار رصيد" };
let books = [], categories = [], filter = "all", pollTimer = null;

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2600); }

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("سجّل الدخول"); }
  if (!r.ok) { let d = "حدث خطأ"; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
  return r.json();
}

function coverHTML(b, href) {
  const tag = href ? `a href="${href}"` : "div";
  const end = href ? "a" : "div";
  const inner = b.has_cover ? `<img src="/api/books/${b.id}/cover" alt="" loading="lazy">` : b.cover_html;
  return `<${tag} class="cover" aria-label="${esc(b.title)}">${inner}</${end}>`;
}

let coverStyle = "calligraphic", coverStyles = [];
function renderStyleSwitch() {
  $("#styleSwitch").innerHTML = coverStyles.map((s) =>
    `<button role="radio" aria-checked="${s.key === coverStyle}" class="${s.key === coverStyle ? "on" : ""}" onclick="setCoverStyle('${s.key}')">${esc(s.label)}</button>`).join("");
}
async function setCoverStyle(key) {
  if (key === coverStyle) return;
  coverStyle = key; renderStyleSwitch();
  await api("/api/me", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cover_style: key }) });
  await load();
  toast("تم تغيير نمط الأغلفة — الكتب الجديدة تُصدَّر بهذا النمط");
}

function badge(b) {
  if (b.status === "ready") return "";
  if (b.status === "needs_review") return `<span class="badge review" title="${b.flagged} فقرة منخفضة الثقة">يحتاج مراجعة</span>`;
  if (b.status === "failed") return `<span class="badge failed">فشل</span>`;
  if (b.status === "no_credit") return `<span class="badge wait">بانتظار رصيد</span>`;
  return `<span class="badge">${STATUS[b.status] || b.status}</span>`;
}

function render() {
  const q = $("#search").value.trim().toLowerCase();
  const sort = $("#sort").value;
  let list = books.filter((b) => (filter === "all" || b.category === filter || (filter === "busy" && BUSY.includes(b.status)))
    && (!q || b.title.toLowerCase().includes(q) || (b.display_title || "").includes(q) || (b.author || "").toLowerCase().includes(q)));
  if (sort === "title") list.sort((a, b) => a.title.localeCompare(b.title, "ar"));
  else if (sort === "read") list.sort((a, b) => (b.last_read || "").localeCompare(a.last_read || ""));

  $("#count").textContent = books.length ? `${books.length} كتاب` : "لا توجد كتب بعد";
  $("#empty").hidden = books.length > 0;

  $("#grid").innerHTML = list.map((b) => {
    const busy = BUSY.includes(b.status);
    const readable = ["ready", "needs_review"].includes(b.status) || (b.status === "translating" && b.progress > 25);
    return `<article class="book ${busy ? "busy" : ""}" data-id="${b.id}">
      ${badge(b)}
      ${coverHTML(b, readable ? `/read/${b.id}` : null)}
      ${busy ? `<div class="busy-info"><div>${esc(b.stage || "")}</div><div class="bar"><span style="width:${b.progress}%"></span></div></div>` : ""}
      <div class="meta-row">
        <div class="meta"><h3>${esc(b.display_title)}</h3>${b.display_title !== b.title ? `<div class="muted orig-title" dir="auto">${esc(b.title)}</div>` : ""}
          <div class="muted">${esc(b.author || b.format_label || "")}${b.read_percent > 0 ? ` · قرأت ${Math.round(b.read_percent)}%` : ""}</div></div>
        <button class="btn icon ghost menu-btn" aria-label="خيارات" onclick="openMenu(event, ${b.id})">
          <svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg></button>
      </div></article>`;
  }).join("");

  const reading = books.filter((b) => b.read_percent > 0 && b.read_percent < 99)
    .sort((a, b) => (b.last_read || "").localeCompare(a.last_read || "")).slice(0, 6);
  $("#continueSection").hidden = !reading.length;
  $("#continueRow").innerHTML = reading.map((b) => `<a class="continue-card" href="/read/${b.id}">
      ${coverHTML(b)}<div style="flex:1;min-width:0"><h3>${esc(b.display_title)}</h3>
      <div class="muted" style="font-size:13px">${Math.round(b.read_percent)}% مقروء</div>
      <div class="bar"><span style="width:${b.read_percent}%"></span></div></div></a>`).join("");

  const used = new Set(books.map((b) => b.category));
  const chips = [["all", "الكل"], ...(books.some((b) => BUSY.includes(b.status)) ? [["busy", "قيد الترجمة"]] : []),
    ...categories.filter((c) => used.has(c.key)).map((c) => [c.key, c.label])];
  $("#chips").innerHTML = chips.length > 2 || filter !== "all" ? chips.map(([k, l]) =>
    `<button class="chip ${filter === k ? "on" : ""}" onclick="setFilter('${k}')">${esc(l)}</button>`).join("") : "";
}

function setFilter(k) { filter = k; render(); }

async function load() {
  books = await api("/api/books");
  render();
  const waiting = books.filter((b) => b.status === "no_credit").length;
  $("#creditBanner").hidden = !waiting;
  $("#creditBanner").innerHTML = waiting ? `${waiting} كتاب متوقف لأن رصيد المفاتيح انتهى. <a href="/keys">أضف مفتاحك</a> وسيكمل تلقائياً من حيث توقف.` : "";
  clearTimeout(pollTimer);
  if (books.some((b) => BUSY.includes(b.status))) pollTimer = setTimeout(load, 4000);
}

/* ---------- card menu ---------- */
function closeMenu() { document.querySelectorAll(".menu").forEach((m) => m.remove()); }
function openMenu(ev, id) {
  ev.stopPropagation(); closeMenu();
  const b = books.find((x) => x.id === id);
  const readable = ["ready", "needs_review"].includes(b.status);
  const m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = `
    ${readable ? `<a href="/read/${id}">📖 قراءة</a>` : ""}
    ${b.has_pdf ? `<a href="/api/books/${id}/pdf">⬇️ تحميل PDF</a>` : ""}
    <button onclick="showDetails(${id})">ℹ️ التفاصيل والمسار</button>
    <button onclick="renameBook(${id})">✏️ تعديل الاسم</button>
    ${["ready", "needs_review", "failed"].includes(b.status) ? `<button onclick="reprocessBook(${id})">♻️ إعادة المعالجة</button>` : ""}
    ${["failed", "no_credit"].includes(b.status) ? `<button onclick="retryBook(${id})">🔁 إعادة المحاولة</button>` : ""}
    ${BUSY.includes(b.status) ? "" : `<button class="danger" onclick="deleteBook(${id})">🗑️ حذف</button>`}`;
  document.body.appendChild(m);
  const r = ev.currentTarget.getBoundingClientRect();
  m.style.top = `${window.scrollY + r.bottom + 4}px`;
  m.style.left = `${Math.max(8, window.scrollX + r.left)}px`;
}
document.addEventListener("click", closeMenu);

async function renameBook(id) {
  const b = books.find((x) => x.id === id);
  const title = prompt("اسم الكتاب بالعربي (يُستخدم اسماً لملف PDF)", b.display_title);
  if (!title || title === b.display_title) return;
  const body = /[\u0600-\u06FF]/.test(b.title) ? { title } : { title_ar: title };
  await api(`/api/books/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (["ready", "needs_review"].includes(b.status)) await api(`/api/books/${id}/export`, { method: "POST" });
  toast("تم تعديل الاسم"); load();
}
async function deleteBook(id) {
  const b = books.find((x) => x.id === id);
  if (!confirm(`حذف «${b.display_title}» نهائياً مع ملفاته؟`)) return;
  await api(`/api/books/${id}`, { method: "DELETE" }); toast("تم الحذف"); load();
}
async function reprocessBook(id) {
  const b = books.find((x) => x.id === id);
  if (!confirm(`إعادة استخراج وترجمة «${b.title}» من الملف الأصلي؟\n\n• بيصرف من الرصيد مرة ثانية\n• العلامات ونسبة القراءة الحالية بتنحذف`)) return;
  await api(`/api/books/${id}/reprocess`, { method: "POST" }); toast("بدأت إعادة المعالجة"); load();
}
async function retryBook(id) { await api(`/api/books/${id}/retry`, { method: "POST" }); toast("أُعيد للطابور، سيكمل من حيث توقف"); load(); }

async function showDetails(id) {
  const b = await api(`/api/books/${id}`);
  const ri = b.router_info || {}, cls = ri.classification || {};
  $("#detailsBody").innerHTML = `<div class="dlg-head"><h2>${esc(b.title)}</h2>
      <button class="btn icon ghost" onclick="detailsDialog.close()" aria-label="إغلاق"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <dl class="details-grid">
      <dt>الحالة</dt><dd>${STATUS[b.status] || b.status} — ${esc(b.stage)}</dd>
      <dt>مسار الاستخراج</dt><dd>${esc(b.format_label || "—")}${ri.pages ? ` · ${ri.pages} صفحة` : ""}${ri.ocr_pages ? ` · ${ri.ocr_pages} صفحة بالتعرف الضوئي` : ""}</dd>
      <dt>نوع المحتوى</dt><dd>${esc(b.category_label || "—")}${cls.confidence ? ` (ثقة ${Math.round(cls.confidence * 100)}%)` : ""}</dd>
      <dt>لغة الكتاب</dt><dd>${esc(b.source_lang)}</dd>
      <dt>قاموس المصطلحات</dt><dd>${b.glossary_terms} مصطلح</dd>
      ${b.key_used ? `<dt>المفتاح المستخدم</dt><dd>${esc(b.key_used)}</dd>` : ""}
      ${ri.handwritten ? `<dt>ملاحظة</dt><dd>مخطوط بخط اليد — يحتاج مراجعة بشرية</dd>` : ""}
      ${b.flagged ? `<dt>تحتاج مراجعة</dt><dd>${b.flagged} فقرة</dd>` : ""}
    </dl>${b.error ? `<div class="error-box">${esc(b.error)}</div>` : ""}`;
  detailsDialog.showModal();
}

/* ---------- upload ---------- */
const uploadDialog = $("#uploadDialog"), detailsDialog = $("#detailsDialog");
function openUpload() { $("#uploadForm").reset(); $("#drop").classList.remove("has-file"); $("#dropTitle").textContent = "اسحب الملف هنا أو اختره"; $("#formError").hidden = true; uploadDialog.showModal(); }
$("#openUpload").onclick = openUpload;

function fileChosen(f) {
  $("#drop").classList.add("has-file");
  $("#dropTitle").textContent = `${f.name} · ${(f.size / 1048576).toFixed(1)} MB`;
  if (!$("#title").value) $("#title").value = f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
  $("#title").focus(); $("#title").select();
}
$("#fileInput").onchange = (e) => e.target.files[0] && fileChosen(e.target.files[0]);
const drop = $("#drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) { $("#fileInput").files = e.dataTransfer.files; fileChosen(f); } });

$("#uploadForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target, fd = new FormData(form);
  if (!fd.get("cover")?.size) fd.delete("cover");
  const btn = $("#submitBtn"), bar = $("#uploadProgress");
  btn.disabled = true; bar.hidden = false; $("#formError").hidden = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/books");
  xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = `${(ev.loaded / ev.total) * 100}%`; };
  xhr.onload = () => {
    btn.disabled = false; bar.hidden = true; bar.firstElementChild.style.width = "0";
    if (xhr.status >= 200 && xhr.status < 300) { uploadDialog.close(); toast("تم الرفع — بدأت رحلة الترجمة"); load(); }
    else { let d = "تعذر الرفع"; try { d = JSON.parse(xhr.responseText).detail || d; } catch {} $("#formError").textContent = d; $("#formError").hidden = false; }
  };
  xhr.onerror = () => { btn.disabled = false; bar.hidden = true; $("#formError").textContent = "انقطع الاتصال أثناء الرفع"; $("#formError").hidden = false; };
  xhr.send(fd);
});

$("#logout").onclick = async () => { await fetch("/api/auth/logout", { method: "POST" }); location.href = "/login"; };
$("#search").oninput = render;
$("#sort").onchange = render;

(async () => {
  const meta = await api("/api/meta");
  categories = meta.categories;
  coverStyle = meta.cover_style; coverStyles = meta.cover_styles; renderStyleSwitch();
  $("#mockBanner").hidden = !meta.mock;
  $("#category").insertAdjacentHTML("beforeend", categories.map((c) => `<option value="${c.key}">${esc(c.label)}</option>`).join(""));
  load();
})();
