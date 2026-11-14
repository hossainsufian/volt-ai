"use strict";
// Volt AI chat client. Talks only to the Volt server on the laptop.

const $ = (id) => document.getElementById(id);
function toast(message) {
  let el = document.getElementById("toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
  el.textContent = message; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), 3500);
}
const els = {
  app: $("app"), list: $("chatList"), messages: $("messages"), welcome: $("welcome"),
  thread: $("thread"), title: $("chatTitle"), status: $("status"), input: $("input"),
  form: $("composer"), send: $("sendBtn"), stop: $("stopBtn"), imageMode: $("imageMode"),
  artifactMode: $("artifactMode"),
};
let state = { chats: [], current: null, messages: [], available: false, settings: {}, local: false, claude: {} };
let pollTimer = null;
let pendingFiles = [];  // attached pictures/files waiting to be sent: {name, type, data (base64), preview}

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", headers: {}, ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/"; throw new Error("signed out"); }
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
  return data;
}

// --- Safe, small markdown renderer (escape first, then format) -------------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function renderMarkdown(text) {
  const parts = text.split(/```([\w+-]*)\n?([\s\S]*?)```/g);
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 3 === 1) continue;
    if (i % 3 === 2) {
      const lang = escapeHtml(parts[i - 1] || "");
      html += `<div class="codeblock"><div class="code-head"><span>${lang || "code"}</span><button type="button" class="copy">Copy</button></div><pre><code>${escapeHtml(parts[i])}</code></pre></div>`;
      continue;
    }
    const lines = escapeHtml(parts[i]).split("\n");
    let list = null, para = [];
    const flushPara = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
    const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const line of lines) {
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.*)/))) { flushPara(); flushList(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; }
      else if ((m = line.match(/^\s*[-*]\s+(.*)/))) { flushPara(); if (list !== "ul") { flushList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushPara(); if (list !== "ol") { flushList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
  }
  return html;
}

// --- Rendering -------------------------------------------------------------
function groupLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}
function renderChatList() {
  els.list.innerHTML = "";
  let lastGroup = null;
  for (const chat of state.chats) {
    const g = groupLabel(chat.updated);
    if (g !== lastGroup) {
      const h = document.createElement("div"); h.className = "chat-group"; h.textContent = g;
      els.list.appendChild(h); lastGroup = g;
    }
    const item = document.createElement("div");
    item.className = "chat-item" + (state.current && chat.id === state.current.id ? " active" : "");
    item.setAttribute("role", "button"); item.tabIndex = 0;
    const span = document.createElement("span"); span.textContent = chat.title;
    const del = document.createElement("button"); del.className = "del"; del.textContent = "✕";
    del.title = "Delete chat"; del.setAttribute("aria-label", `Delete ${chat.title}`);
    del.onclick = (e) => { e.stopPropagation(); deleteChat(chat); };
    item.append(span, del);
    item.onclick = () => openChat(chat.id);
    item.onkeydown = (e) => { if (e.key === "Enter") openChat(chat.id); };
    els.list.appendChild(item);
  }
}
function messageNode(m) {
  const row = document.createElement("div");
  row.className = `msg ${m.role}` + (m.status === "error" ? " error" : "");
  if (m.role === "assistant") {
    const av = document.createElement("img"); av.src = "/static/icon.svg"; av.alt = ""; av.className = "avatar";
    row.appendChild(av);
  }
  const bubble = document.createElement("div"); bubble.className = "bubble";
  if (m.status === "pending" && m.text) {
    // Claude is writing: show what has arrived so far.
    // Close a code block that is still being written so it renders as code.
    const open = (m.text.match(/```/g) || []).length % 2 === 1;
    bubble.innerHTML = renderMarkdown(open ? m.text + "\n```" : m.text) + '<span class="cursor" aria-hidden="true"></span>';
  } else if (m.status === "pending" || m.status === "pending_image") {
    const secs = elapsed(m);
    const label = m.status === "pending_image" ? "Creating your image" : "Thinking";
    bubble.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>' +
      ` <small class="wait">${label}…${secs >= 3 ? ` ${secs}s` : ""}</small>`;
  } else if (m.image) {
    const src = `/images/${encodeURIComponent(m.image)}`;
    bubble.innerHTML = `<a href="${src}" target="_blank" rel="noopener"><img class="gen-image" alt="" src="${src}"></a>`;
    bubble.querySelector("img").alt = m.text || "Generated image";
    const actions = document.createElement("div"); actions.className = "msg-actions";
    actions.innerHTML = `<a href="${src}?download=1" download><button type="button">⬇ Download</button></a>`;
    if (m.meta && m.meta.seed !== undefined) {
      const again = document.createElement("button"); again.type = "button"; again.textContent = "🔁 Again";
      again.title = "Make a new version of this picture";
      again.onclick = () => remakeImage(m.id, null);
      const change = document.createElement("button"); change.type = "button"; change.textContent = "✏️ Change";
      change.title = "Change this picture, for example: make it night";
      change.onclick = () => {
        const what = prompt("What should change? For example: make it night, add a hat, blue background");
        if (what && what.trim()) remakeImage(m.id, what.trim());
      };
      actions.append(again, change);
    }
    bubble.appendChild(actions);
    if (m.meta && m.meta.final_prompt && m.meta.final_prompt !== m.text) {
      const used = document.createElement("details"); used.className = "prompt-used";
      used.innerHTML = "<summary>Prompt used</summary><p></p>";
      used.querySelector("p").textContent = m.meta.final_prompt;
      bubble.appendChild(used);
    }
  } else if (m.role === "assistant") {
    bubble.innerHTML = renderMarkdown(m.text || "");
    const actions = document.createElement("div"); actions.className = "msg-actions";
    const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy";
    copy.onclick = () => copyText(m.text, copy);
    actions.appendChild(copy); bubble.appendChild(actions);
  } else {
    bubble.textContent = m.text;
    const meta = m.meta || {};
    if ((meta.attachments || []).length || (meta.files || []).length) {
      const box = document.createElement("div"); box.className = "msg-attach";
      for (const a of meta.attachments || []) {
        const img = document.createElement("img"); img.src = `/images/${encodeURIComponent(a.image)}`; img.alt = a.name;
        img.onclick = () => window.open(img.src, "_blank", "noopener");
        box.appendChild(img);
      }
      for (const f of meta.files || []) {
        const chip = document.createElement("span"); chip.className = "file-chip"; chip.textContent = "📄 " + f.name;
        box.appendChild(chip);
      }
      bubble.appendChild(box);
    }
  }
  bubble.querySelectorAll(".codeblock .copy").forEach((btn) => {
    btn.onclick = () => copyText(btn.closest(".codeblock").querySelector("code").textContent, btn);
  });
  if (m.role === "assistant" && String(m.status || "").startsWith("pending")) {
    // While a drawing is being written, show progress instead of raw SVG code.
    bubble.querySelectorAll(".codeblock").forEach((block) => {
      const lang = block.querySelector(".code-head span").textContent.toLowerCase();
      const code = block.querySelector("code").textContent;
      if (lang === "svg" || /^\s*<svg/i.test(code)) block.outerHTML = '<p class="wait">✏️ Drawing your picture…</p>';
    });
  }
  if (m.role === "assistant" && !String(m.status || "").startsWith("pending")) {
    // SVG drawings (logos, diagrams) show as pictures, not code.
    bubble.querySelectorAll(".codeblock").forEach((block) => {
      const lang = block.querySelector(".code-head span").textContent.toLowerCase();
      const code = block.querySelector("code").textContent;
      if (!(lang === "svg" || /^\s*<svg/i.test(code))) return;
      const src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(code);
      const fig = document.createElement("div"); fig.className = "svg-art";
      fig.innerHTML = `<img class="gen-image" alt="Drawing" src="${src}"><div class="msg-actions">
        <a download="volt-drawing.svg" href="${src}"><button type="button">⬇ Download</button></a>
        <button type="button" class="open-art">⤢ Open</button></div>`;
      fig.querySelector(".open-art").onclick = () => openArtifact(code, "svg");
      block.replaceWith(fig);
    });
    bubble.querySelectorAll(".codeblock").forEach((block) => {
      const code = block.querySelector("code").textContent;
      const lang = block.querySelector(".code-head span").textContent.toLowerCase();
      if (!isPreviewable(lang, code)) return;
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "preview"; btn.textContent = "▶ Preview";
      btn.onclick = () => openArtifact(code, lang);
      block.querySelector(".code-head").insertBefore(btn, block.querySelector(".copy"));
    });
  }
  row.appendChild(bubble);
  return row;
}
function elapsed(m) { return Math.max(0, Math.round(Date.now() / 1000 - (m.created || Date.now() / 1000))); }
function messageKey(m) {
  const tick = m.status && m.status.startsWith("pending") && !m.text ? elapsed(m) : "";
  return `${m.id}|${m.status}|${(m.text || "").length}|${m.image || ""}|${tick}`;
}
function renderMessages() {
  const atBottom = els.thread.scrollHeight - els.thread.scrollTop - els.thread.clientHeight < 80;
  // Only rebuild messages that changed, so live replies don't flicker.
  const nodes = els.messages.children;
  state.messages.forEach((m, i) => {
    const key = messageKey(m);
    if (nodes[i] && nodes[i].dataset.key === key) return;
    const node = messageNode(m); node.dataset.key = key;
    if (nodes[i]) els.messages.replaceChild(node, nodes[i]); else els.messages.appendChild(node);
  });
  while (nodes.length > state.messages.length) els.messages.lastChild.remove();
  els.welcome.hidden = state.messages.length > 0;
  els.title.textContent = state.current && state.messages.length ? state.current.title : "Volt AI";
  if (atBottom || busy()) els.thread.scrollTop = els.thread.scrollHeight;
  updateComposer();
}
function busy() { return state.messages.some((m) => m.status === "pending" || m.status === "pending_image"); }
function updateComposer() {
  const pending = state.messages.some((m) => m.status === "pending");
  els.stop.hidden = !pending;
  els.send.hidden = pending;
  els.send.disabled = !els.input.value.trim() && !pendingFiles.length;
  const brain = state.brain || "fast";
  const claude = brain === "claude";
  const sel = $("brainSel");
  const hasKey = !!(state.claude && state.claude.has_key);
  const claudeOpt = sel.querySelector('option[value="claude"]');
  claudeOpt.disabled = !hasKey;  // <option hidden> is ignored by some phone browsers
  claudeOpt.textContent = hasKey ? "✨ Claude" : "✨ Claude (needs key)";
  const hasOnline = !!(state.online && state.online.has_key);
  const onlineOpt = sel.querySelector('option[value="online"]');
  onlineOpt.disabled = !hasOnline;
  onlineOpt.textContent = hasOnline ? "🌐 Gemini" : "🌐 Gemini (needs key)";
  if (document.activeElement !== sel) sel.value = brain;
  sel.title = { fast: "Fast: quick answers from this computer. Private, no web search.",
    smart: "Smart: can search the web and remember things, but much slower.",
    online: "Gemini: very fast answers from Google. Sent to Google.",
    claude: "Claude: strongest for coding. Paid, sent to Anthropic." }[brain];
  // Fast and Claude don't need the full assistant to be running.
  const ready = brain !== "smart" || state.available;
  els.status.textContent = ready ? "" : "Starting…";
  els.status.className = "status" + (ready ? "" : " off");
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
  catch (_) { btn.textContent = "Copy failed"; }
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}

// --- Actions -------------------------------------------------------------------
async function loadChats() { state.chats = (await api("/api/chats")).chats; renderChatList(); }
async function openChat(id) {
  const data = await api(`/api/chats/${id}`);
  state.current = data.chat; state.messages = data.messages;
  try { localStorage.setItem("volt.lastChat", id); } catch (_) { /* storage blocked */ }
  renderChatList(); renderMessages(); closeMenu(); schedulePoll();
  els.thread.scrollTop = els.thread.scrollHeight;
}
function newChat() {
  state.current = null; state.messages = [];
  try { localStorage.removeItem("volt.lastChat"); } catch (_) { /* storage blocked */ }
  renderChatList(); renderMessages(); closeMenu(); els.input.focus();
}
async function ensureChat() {
  if (state.current) return state.current;
  state.current = (await api("/api/chats", { method: "POST" })).chat;
  return state.current;
}
async function deleteChat(chat) {
  if (!confirm(`Delete "${chat.title}"? This can't be undone.`)) return;
  await api(`/api/chats/${chat.id}`, { method: "DELETE" });
  if (state.current && state.current.id === chat.id) newChat();
  loadChats();
}
async function submit(e) {
  if (e) e.preventDefault();
  const text = els.input.value.trim();
  if ((!text && !pendingFiles.length) || busy()) return;
  const imageMode = els.imageMode.getAttribute("aria-pressed") === "true";
  try {
    const chat = await ensureChat();
    els.input.value = ""; autosize();
    if (imageMode) {
      await api(`/api/chats/${chat.id}/images`, { method: "POST", body: {
        prompt: text, style: $("imgStyle").value || "auto", shape: $("imgShape").value || "square",
        count: Number($("imgCount").value) || 1, enhance: $("imgEnhance").checked } });
      setImageMode(false);
    } else {
      const attachments = pendingFiles.map(({ name, type, data }) => ({ name, type, data }));
      await api(`/api/chats/${chat.id}/messages`, { method: "POST", body: { text, artifact: artifactOn(), attachments } });
      pendingFiles = []; renderAttachments();
    }
    await openChat(chat.id); loadChats();
  } catch (err) {
    els.input.value = text; autosize(); toast(err.message);
  }
}
// --- Attachments: 📎, paste and drag and drop ----------------------------------------------
const MAX_SIDE = 1568;  // big photos are shrunk here so uploads stay quick
function readAsDataURL(blob) {
  return new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = bad; r.readAsDataURL(blob); });
}
async function shrinkPicture(file) {
  const url = await readAsDataURL(file);
  if (file.type === "image/gif") return { type: file.type, url };
  const img = await new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = url; });
  const scale = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
  if (scale === 1 && file.size < 1.5e6) return { type: file.type, url };
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return { type: "image/jpeg", url: c.toDataURL("image/jpeg", 0.88) };
}
async function addFiles(list) {
  for (const file of list) {
    if (pendingFiles.length >= 6) { toast("You can attach up to 6 files at once."); break; }
    try {
      if (file.type.startsWith("image/")) {
        const { type, url } = await shrinkPicture(file);
        pendingFiles.push({ name: file.name || "pasted-picture.png", type, data: url.split(",")[1], preview: url });
      } else {
        if (file.size > 2e6) { toast(`${file.name} is too big to read.`); continue; }
        const url = await readAsDataURL(file);
        pendingFiles.push({ name: file.name, type: file.type || "text/plain", data: url.split(",")[1] });
      }
    } catch (_) { toast(`Couldn't read ${file.name}.`); }
  }
  renderAttachments();
}
function renderAttachments() {
  const box = $("attachList"); box.innerHTML = ""; box.hidden = !pendingFiles.length;
  pendingFiles.forEach((f, i) => {
    const item = document.createElement("div"); item.className = "attach-item";
    item.innerHTML = f.preview ? `<img alt="">` : `<span class="file-chip"></span>`;
    if (f.preview) item.querySelector("img").src = f.preview; else item.querySelector(".file-chip").textContent = "📄 " + f.name;
    const x = document.createElement("button"); x.type = "button"; x.className = "attach-x"; x.textContent = "✕";
    x.setAttribute("aria-label", "Remove " + f.name);
    x.onclick = () => { pendingFiles.splice(i, 1); renderAttachments(); };
    item.appendChild(x); box.appendChild(item);
  });
  updateComposer();
}
$("attachBtn").onclick = () => $("attachInput").click();
$("attachInput").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
els.input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
els.form.addEventListener("dragover", (e) => { e.preventDefault(); els.form.classList.add("dropping"); });
els.form.addEventListener("dragleave", () => els.form.classList.remove("dropping"));
els.form.addEventListener("drop", (e) => {
  e.preventDefault(); els.form.classList.remove("dropping");
  if (e.dataTransfer?.files?.length) addFiles([...e.dataTransfer.files]);
});

async function stop() { await api("/api/stop", { method: "POST" }); if (state.current) openChat(state.current.id); }
function schedulePoll() {
  clearTimeout(pollTimer);
  if (state.current && busy()) {
    pollTimer = setTimeout(async () => {
      try {
        const data = await api(`/api/chats/${state.current.id}`);
        state.current = data.chat; state.messages = data.messages; renderMessages();
        state.wasBusy = true;
        if (!busy()) { loadChats(); maybeAutoOpenArtifact(); state.wasBusy = false; }
      } catch (_) { /* retry next tick */ }
      schedulePoll();
    }, 500);
  }
}
// --- Artifacts: web pages, apps and games Volt builds, shown live -------------------
// The preview runs in a sandboxed frame with its own blank origin, so it can't
// read Volt's chats, cookies or anything else on this page.
const ART_KEY = "volt.artifactMode";
let artifactCode = "", artifactLang = "html", autoOpened = new Set();
function artifactOn() { return els.artifactMode.getAttribute("aria-pressed") === "true"; }
function setArtifactMode(on) {
  els.artifactMode.setAttribute("aria-pressed", String(on));
  try { localStorage.setItem(ART_KEY, on ? "1" : "0"); } catch (_) { /* private mode */ }
  if (on) setImageMode(false);
}
function isPreviewable(lang, code) {
  if (lang === "html" || lang === "svg") return true;
  return /^\s*(<!doctype html|<html|<svg)/i.test(code);
}
function artifactDocument(code, lang) {
  if (lang === "svg" || /^\s*<svg/i.test(code)) {
    return `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#fff">${code}</body>`;
  }
  return code;
}
function openArtifact(code, lang) {
  artifactCode = code; artifactLang = lang;
  const title = (code.match(/<title>([^<]{1,80})<\/title>/i) || [])[1];
  $("artifactTitle").textContent = title ? title.trim() : (lang === "svg" ? "Picture" : "Artifact");
  $("artFrame").srcdoc = artifactDocument(code, lang);
  $("artCode").querySelector("code").textContent = code;
  showArtifactTab("preview");
  $("artifact").hidden = false; els.app.classList.add("has-artifact");
}
function closeArtifact() {
  $("artifact").hidden = true; els.app.classList.remove("has-artifact");
  $("artFrame").srcdoc = "";
}
function showArtifactTab(tab) {
  const preview = tab === "preview";
  $("artFrame").hidden = !preview; $("artCode").hidden = preview;
  $("artTabPreview").setAttribute("aria-selected", String(preview));
  $("artTabCode").setAttribute("aria-selected", String(!preview));
}
function maybeAutoOpenArtifact() {
  // Open the newest finished artifact once, right after Volt finishes it.
  const last = state.messages[state.messages.length - 1];
  if (!last || last.role !== "assistant" || last.status !== "done" || autoOpened.has(last.id)) return;
  autoOpened.add(last.id);
  if (!artifactOn() || !state.wasBusy) return;
  const m = (last.text || "").match(/```(html|svg)\n?([\s\S]*?)```/i);
  if (m) openArtifact(m[2], m[1].toLowerCase());
}
$("artTabPreview").onclick = () => showArtifactTab("preview");
$("artTabCode").onclick = () => showArtifactTab("code");
$("artClose").onclick = closeArtifact;
$("artCopy").onclick = () => copyText(artifactCode, $("artCopy"));
$("artDownload").onclick = () => {
  const blob = new Blob([artifactDocument(artifactCode, artifactLang)], { type: "text/html" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = ($("artifactTitle").textContent.replace(/[^\w -]+/g, "").trim() || "artifact") + ".html";
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};
els.artifactMode.onclick = () => setArtifactMode(!artifactOn());
try { if (localStorage.getItem(ART_KEY) === "1") setArtifactMode(true); } catch (_) { /* private mode */ }

async function remakeImage(id, change) {
  try {
    await api(`/api/images/${id}/${change ? "change" : "again"}`, { method: "POST", body: change ? { change } : {} });
    if (state.current) openChat(state.current.id);
  } catch (err) { toast(err.message); }
}
const IMG_PREFS = "volt.imageOptions";
function renderImageOptions(s) {
  const im = s.images || {};
  const fill = (sel, items) => {
    const key = JSON.stringify(items);
    if (sel.dataset.key === key) return;
    const keep = sel.value; sel.innerHTML = ""; sel.dataset.key = key;
    for (const [value, label] of items || []) sel.add(new Option(label, value));
    if (keep) sel.value = keep;
  };
  fill($("imgStyle"), im.styles); fill($("imgShape"), im.shapes);
  try {
    const saved = JSON.parse(localStorage.getItem(IMG_PREFS) || "{}");
    if (saved.style && !$("imgStyle").dataset.restored) { $("imgStyle").value = saved.style; $("imgShape").value = saved.shape || "square";
      $("imgCount").value = String(saved.count || 1); $("imgEnhance").checked = saved.enhance !== false; $("imgStyle").dataset.restored = "1"; }
  } catch (_) { /* private mode */ }
  if (!s.local) { $("imagesBox").hidden = true; return; }
  $("imagesBox").hidden = !s.settings.images_enabled;
  $("removeImgKey").hidden = $("imgModelRow").hidden = !im.has_key;
  $("imgKey").placeholder = im.has_key ? "Paste a new key to replace it" : "Paste Pollinations key: sk_…";
  if (im.has_key) $("imgKeyLine").innerHTML = `<small>✅ Pollinations key saved (${im.key_hint}): good models, no watermark, and ✏️ Change edits the real picture.</small>`;
  fill($("imgModel"), [["", "Default (Z-Image Turbo)"], ...(im.models || [])]);
  if (document.activeElement !== $("imgModel")) $("imgModel").value = s.settings.image_model || "";
}
function saveImagePrefs() {
  try { localStorage.setItem(IMG_PREFS, JSON.stringify({ style: $("imgStyle").value, shape: $("imgShape").value,
    count: Number($("imgCount").value), enhance: $("imgEnhance").checked })); } catch (_) { /* private mode */ }
}
["imgStyle", "imgShape", "imgCount", "imgEnhance"].forEach((id) => { $(id).onchange = saveImagePrefs; });
$("imgModel").onchange = (e) => saveSetting("image_model", e.target.value);
$("saveImgKey").onclick = async () => {
  const key = $("imgKey").value.trim();
  if (!key) { toast("Paste your Pollinations key first."); return; }
  try {
    await api("/api/images/key", { method: "POST", body: { key } });
    $("imgKey").value = ""; toast("Pollinations key saved. Better pictures from now on.");
  } catch (err) { toast(err.message); }
  refreshState().catch(() => {});
};
$("removeImgKey").onclick = async () => {
  if (!confirm("Remove your Pollinations key? Volt goes back to the free basic model.")) return;
  await api("/api/images/key", { method: "POST", body: { key: "" } }).catch((e) => toast(e.message));
  refreshState().catch(() => {});
};

function setImageMode(on) {
  els.imageMode.setAttribute("aria-pressed", String(on));
  $("imageOpts").hidden = !on;
  if (on && els.artifactMode) els.artifactMode.setAttribute("aria-pressed", "false");
  els.input.placeholder = on ? "Describe the image you want…" : "Message Volt AI…";
}
function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, window.innerHeight * 0.4) + "px";
  updateComposer();
}
function openMenu() { els.app.classList.add("menu-open"); }
function closeMenu() { els.app.classList.remove("menu-open"); }

// --- Settings & gallery ------------------------------------------------------------
async function refreshState() {
  const s = await api("/api/state");
  state.available = s.available; state.settings = s.settings; state.local = s.local;
  $("setImages").checked = !!s.settings.images_enabled;
  $("setPhone").checked = !!s.settings.phone_access;
  $("setSystem").checked = !!s.settings.system_analysis;
  $("setImages").disabled = $("setPhone").disabled = $("setSystem").disabled = !s.local;
  $("laptopOnly").hidden = s.local;
  $("phoneRow").hidden = !s.local;
  $("phoneBox").hidden = !(s.local && s.settings.phone_access && s.phone_url);
  $("phoneUrl").textContent = s.phone_url || "";
  $("tsLine").hidden = !s.tailscale_url;
  $("tsUrl").textContent = s.tailscale_url || "";
  $("pwLine").textContent = s.phone_password_set
    ? "✅ Phone password is set. Phones sign in with it once."
    : "⚠️ Set a phone password so your phone can sign in.";
  els.imageMode.hidden = !s.settings.images_enabled;
  state.claude = s.claude || {};
  state.online = s.online || {};
  renderOnline(s);
  renderImageOptions(s);
  if (s.about) { $("aboutText").textContent = s.about.text; $("aboutBased").textContent = s.about.based_on; }
  renderFastModel(s);
  state.brain = s.brain || "fast";
  renderClaude(s);
  updateComposer();
}
function renderClaude(s) {
  const c = s.claude || {};
  $("claudeBox").hidden = !s.local;
  if (!s.local) return;
  $("claudeRow").hidden = $("claudeModelRow").hidden = $("removeClaudeKey").hidden = !c.has_key;
  $("setClaude").checked = !!s.settings.claude_enabled;
  const sel = $("claudeModel");
  if (!sel.options.length) {
    for (const [id, label] of Object.entries(c.models || {})) sel.add(new Option(label, id));
  }
  sel.value = s.settings.claude_model;
  $("claudeKeyLine").textContent = c.has_key ? `Key saved: ${c.key_hint}` : "";
  $("claudeKey").placeholder = c.has_key ? "Paste a new key to replace it" : "Paste key: sk-ant-…";
}
function renderFastModel(s) {
  const f = s.fast_models || {}, sel = $("fastModel");
  $("fastModelRow").hidden = !s.local || !(f.installed || []).length;
  if (!s.local) return;
  const want = s.settings.fast_model || "";
  const options = [["", `Same as the assistant (${f.default})`],
    ...(f.installed || []).filter((n) => n !== f.default).map((n) => [n, n])];
  const key = JSON.stringify(options);
  if (sel.dataset.key !== key) {
    sel.innerHTML = ""; sel.dataset.key = key;
    for (const [value, label] of options) sel.add(new Option(label, value));
  }
  if (document.activeElement !== sel) sel.value = want;
}
$("fastModel").onchange = async (e) => {
  await saveSetting("fast_model", e.target.value);
  toast(`Fast brain now uses ${e.target.value || "the assistant's model"}. Loading it…`);
};
function renderOnline(s) {
  const o = s.online || {};
  $("onlineBox").hidden = !s.local;
  if (!s.local) return;
  $("removeOnlineKey").hidden = !o.has_key;
  $("onlineKeyLine").textContent = o.has_key ? `✅ Key saved: ${o.key_hint} · model ${o.model}` : "";
  $("onlineKey").placeholder = o.has_key ? "Paste a new key to replace it" : "Paste Gemini API key";
}
$("saveOnlineKey").onclick = async () => {
  const key = $("onlineKey").value.trim();
  if (!key) { toast("Paste your Gemini API key first."); return; }
  $("saveOnlineKey").disabled = true; $("saveOnlineKey").textContent = "Checking…";
  try {
    const r = await api("/api/online/key", { method: "POST", body: { key } });
    $("onlineKey").value = ""; toast(`Gemini is on (${r.model}). New messages will use it.`);
  } catch (err) { toast(err.message); }
  $("saveOnlineKey").disabled = false; $("saveOnlineKey").textContent = "Save key";
  refreshState().catch(() => {});
};
$("removeOnlineKey").onclick = async () => {
  if (!confirm("Remove your Gemini API key from this computer? Volt goes back to the free brain.")) return;
  await api("/api/online/key", { method: "POST", body: { key: "" } }).catch((e) => toast(e.message));
  refreshState().catch(() => {});
};
$("saveClaudeKey").onclick = async () => {
  const key = $("claudeKey").value.trim();
  if (!key) { toast("Paste your Claude API key first."); return; }
  try {
    await api("/api/claude/key", { method: "POST", body: { key } });
    $("claudeKey").value = ""; toast("Claude is on. New messages will use Claude.");
  } catch (err) { toast(err.message); }
  refreshState().catch(() => {});
};
$("removeClaudeKey").onclick = async () => {
  if (!confirm("Remove your Claude API key from this laptop? Volt goes back to the free brain.")) return;
  await api("/api/claude/key", { method: "POST", body: { key: "" } }).catch((e) => toast(e.message));
  refreshState().catch(() => {});
};
$("setClaude").onchange = (e) => saveSetting("claude_enabled", e.target.checked);
$("claudeModel").onchange = (e) => saveSetting("claude_model", e.target.value);
async function refreshAccount() {
  if (!state.local) { $("accountBox").hidden = true; return; }
  const a = await api("/api/account");
  const signedIn = !!a.email;
  $("accountLine").textContent = signedIn ? `Signed in as ${a.email}` : "";
  const st = a.backup || {};
  let line = st.message || "";
  if (st.last && st.state === "idle") line = `Last backup: ${new Date(st.last).toLocaleString()}`;
  $("backupStatus").textContent = line;
  $("backupStatus").className = "backup-status" + (st.state === "error" ? " error" : "");
  $("signIn").hidden = signedIn;
  $("backupNow").hidden = $("restoreNow").hidden = $("signOut").hidden = !signedIn;
  const working = st.state === "working";
  ["signIn", "backupNow", "restoreNow"].forEach((id) => ($(id).disabled = working));
  if (working) setTimeout(() => refreshAccount().catch(() => {}), 1500);
  else if (st.message && st.message.startsWith("Restored")) { loadChats(); }
}
async function accountAction(path, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try { await api(path, { method: "POST" }); } catch (err) { toast(err.message); }
  setTimeout(() => refreshAccount().catch(() => {}), 400);
}
$("signIn").onclick = () => accountAction("/api/account/sign-in");
$("backupNow").onclick = () => accountAction("/api/backup");
$("restoreNow").onclick = () => accountAction("/api/restore",
  "Restore chats and images from Google Drive? Your current data is kept in a safety copy first.");
$("signOut").onclick = () => accountAction("/api/account/sign-out", "Sign out of Google? Backups stop until you sign in again.");

async function saveSetting(key, value) {
  await api("/api/settings", { method: "POST", body: { [key]: value } });
  setTimeout(refreshState, key === "phone_access" ? 1200 : 0);
}
async function openGallery() {
  const grid = $("galleryGrid"); grid.innerHTML = "";
  const { images } = await api("/api/images");
  if (!images.length) grid.innerHTML = '<p class="empty-note">No images yet. Turn on image creation in Settings, then press 🎨 Image.</p>';
  for (const img of images) {
    const a = document.createElement("a");
    a.href = `/images/${encodeURIComponent(img.image)}`; a.target = "_blank"; a.rel = "noopener";
    const el = document.createElement("img"); el.src = a.href; el.alt = img.text || "Generated image"; el.loading = "lazy";
    a.appendChild(el); grid.appendChild(a);
  }
  closeMenu(); $("gallery").showModal();
}

async function openArtifactsSheet() {
  const list = $("artifactList"); list.innerHTML = "";
  const { artifacts } = await api("/api/artifacts");
  if (!artifacts.length) {
    list.innerHTML = '<p class="empty-note">Nothing yet. Turn on 🧩 Artifact next to the brain picker and ask Volt to build something, like "a stopwatch" or "a snake game".</p>';
  }
  for (const a of artifacts) {
    const card = document.createElement("div"); card.className = "artifact-card";
    const when = new Date(a.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
    card.innerHTML = `<div class="artifact-icon" aria-hidden="true">${a.lang === "svg" ? "🖼" : "🧩"}</div>
      <div class="artifact-info"><strong></strong><small></small></div>
      <button type="button" class="primary">Open</button><button type="button" class="link">Chat</button>`;
    card.querySelector("strong").textContent = a.title;
    card.querySelector("small").textContent = `${a.chat_title} · ${when}`;
    const [open, chat] = card.querySelectorAll("button");
    open.onclick = async () => {
      try {
        const r = await api(`/api/artifacts/${a.message_id}/${a.index}`);
        $("artifactsSheet").close(); openArtifact(r.code, r.lang);
      } catch (err) { toast(err.message); }
    };
    chat.onclick = () => { $("artifactsSheet").close(); location.hash = `chat=${a.chat_id}`; openChat(a.chat_id); };
    list.appendChild(card);
  }
  closeMenu(); $("artifactsSheet").showModal();
}
$("openArtifacts").onclick = () => openArtifactsSheet().catch((e) => toast(e.message));

// --- Volt Code remote control (/rc) ----------------------------------------------------
const rc = { id: null, after: 0, timer: null, answered: new Set() };
async function openCodeSheet() {
  const list = $("codeList"); list.innerHTML = "";
  const { sessions } = await api("/api/rc/sessions");
  if (!sessions.length) {
    list.innerHTML = '<p class="empty-note">No shared sessions. Open Volt Code in a terminal and type <b>/rc</b>.</p>';
  }
  for (const s of sessions) {
    const card = document.createElement("div"); card.className = "artifact-card";
    card.innerHTML = `<div class="artifact-icon" aria-hidden="true">${s.online ? "🟢" : "⚪"}</div>
      <div class="artifact-info"><strong></strong><small></small></div><button type="button" class="primary">Open</button>`;
    card.querySelector("strong").textContent = s.title;
    card.querySelector("small").textContent = `${s.host} · ${s.folder}${s.online ? "" : " · offline"}`;
    card.querySelector("button").onclick = () => { $("codeSheet").close(); openSession(s); };
    list.appendChild(card);
  }
  closeMenu(); $("codeSheet").showModal();
}
function openSession(s) {
  rc.id = s.id; rc.after = 0; rc.answered = new Set();
  $("rcTitle").textContent = s.title; $("rcSub").textContent = `${s.host} · ${s.folder}`;
  $("rcLog").innerHTML = ""; $("rcView").hidden = false;
  pollSession();
}
function closeSession() {
  clearTimeout(rc.timer); rc.id = null; $("rcView").hidden = true;
  if (location.hash.includes("code=")) history.replaceState(null, "", location.pathname);
}
function rcLine(cls, html) {
  const el = document.createElement("div"); el.className = "rc-line " + cls; el.innerHTML = html;
  $("rcLog").appendChild(el); return el;
}
function renderEvent(e) {
  const log = $("rcLog");
  log.querySelectorAll(".rc-thinking").forEach((n) => n.remove());
  if (e.type === "user") rcLine("rc-user", `<span class="who">${e.from === "app" ? "📱 You" : "⌨️ You"}</span> ` + escapeHtml(e.text));
  else if (e.type === "assistant") rcLine("rc-assistant", "<b>⏺</b> " + renderMarkdown(e.text));
  else if (e.type === "tool") rcLine("rc-tool", `<b class="dot">⏺</b> <b>${escapeHtml(e.label)}</b>`);
  else if (e.type === "tool_result") rcLine("rc-result" + (e.failed ? " bad" : ""), "⎿ " + escapeHtml(e.text));
  else if (e.type === "status") rcLine("rc-status", escapeHtml(e.text));
  else if (e.type === "thinking") rcLine("rc-thinking", '<span class="thinking"><i></i><i></i><i></i></span> Thinking…');
  else if (e.type === "approval_done") {
    rc.answered.add(e.id);
    const card = log.querySelector(`[data-approval="${e.id}"]`);
    if (card) { card.querySelectorAll("button").forEach((b) => b.remove());
      card.insertAdjacentHTML("beforeend", `<p class="rc-done">${e.answer === "no" ? "✗ Not allowed" : "✓ Allowed"}</p>`); }
  } else if (e.type === "approval") {
    const card = rcLine("rc-approval", `<b>✋ ${escapeHtml(e.title)}</b><pre></pre>`);
    card.dataset.approval = e.id;
    card.querySelector("pre").textContent = e.detail || "";
    if (!rc.answered.has(e.id)) {
      const row = document.createElement("div"); row.className = "msg-actions";
      for (const [answer, label] of [["yes", "✓ Allow"], ["always", "Always allow"], ["no", "✗ Deny"]]) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = label;
        b.onclick = () => api(`/api/rc/sessions/${rc.id}/input`, { method: "POST", body: { approval: e.id, answer } })
          .catch((err) => toast(err.message));
        row.appendChild(b);
      }
      card.appendChild(row);
    }
  }
}
async function pollSession() {
  clearTimeout(rc.timer);
  if (!rc.id) return;
  try {
    const atBottom = $("rcLog").scrollHeight - $("rcLog").scrollTop - $("rcLog").clientHeight < 80;
    const { events } = await api(`/api/rc/sessions/${rc.id}/events?after=${rc.after}`);
    for (const e of events) { renderEvent(e); rc.after = e.seq; }
    if (events.length && atBottom) $("rcLog").scrollTop = $("rcLog").scrollHeight;
  } catch (err) {
    if (/not found|404/i.test(err.message)) { rcLine("rc-status", "This session has ended."); return; }
  }
  rc.timer = setTimeout(pollSession, 1000);
}
$("openCode").onclick = () => openCodeSheet().catch((e) => toast(e.message));

// Live Volt Code sessions in the sidebar, like Claude's remote-control sessions.
const seenSessions = new Set();
let firstSessionCheck = true;
async function refreshCodeSessions() {
  let sessions = [];
  try { ({ sessions } = await api("/api/rc/sessions")); } catch (_) { return; }
  const live = sessions.filter((s) => s.online);
  const box = $("codeLive"); box.innerHTML = ""; box.hidden = !live.length;
  if (live.length) box.insertAdjacentHTML("beforeend", '<div class="chat-group">💻 Volt Code · live</div>');
  for (const s of live) {
    const b = document.createElement("button"); b.type = "button"; b.className = "chat-item code-item";
    b.innerHTML = '<span class="live-dot" aria-hidden="true"></span><span class="code-name"></span>';
    b.querySelector(".code-name").textContent = s.title.replace(/^Volt Code · /, "");
    b.title = `${s.host} · ${s.folder}`;
    b.onclick = () => { closeMenu(); openSession(s); };
    box.appendChild(b);
    if (!seenSessions.has(s.id) && !firstSessionCheck && rc.id !== s.id) {
      toast(`💻 Volt Code connected: ${s.title.replace(/^Volt Code · /, "")}. Open it from the sidebar.`);
    }
    seenSessions.add(s.id);
  }
  firstSessionCheck = false;
}
async function openSessionFromLink(id) {
  try {
    const { sessions } = await api("/api/rc/sessions");
    const s = sessions.find((x) => x.id === id);
    if (s) openSession(s); else toast("That Volt Code session has ended.");
  } catch (err) { toast(err.message); }
}
$("rcBack").onclick = closeSession;
$("rcStop").onclick = () => api(`/api/rc/sessions/${rc.id}/input`, { method: "POST", body: { stop: true } })
  .then(() => toast("Asked Volt Code to stop.")).catch((e) => toast(e.message));
$("rcForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("rcInput").value.trim();
  if (!text || !rc.id) return;
  try {
    await api(`/api/rc/sessions/${rc.id}/input`, { method: "POST", body: { text } });
    $("rcInput").value = "";
  } catch (err) { toast(err.message); }
});
$("rcInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(min-width: 761px)").matches) {
    e.preventDefault(); $("rcForm").requestSubmit();
  }
});

// --- Wiring ----------------------------------------------------------------------
function greet() {
  const h = new Date().getHours();
  $("greeting").textContent = h < 5 ? "Hello, night owl" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}
els.form.addEventListener("submit", submit);
els.input.addEventListener("input", autosize);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(min-width: 761px)").matches) submit(e);
});
els.stop.onclick = stop;
$("brainSel").onchange = async (e) => {
  const want = e.target.value;
  try {
    const r = await api("/api/brain", { method: "POST", body: { brain: want } });
    state.brain = r.brain; state.claude.active = r.active; updateComposer();
    toast({ fast: "Fast mode: quick answers from this computer.",
      smart: "Smart mode: web search and memory, but slower.",
      online: "Gemini: very fast answers (sent to Google).",
      claude: "Claude: best for coding (paid)." }[r.brain]);
  } catch (err) { toast(err.message); e.target.value = state.brain || "fast"; }
};
els.imageMode.onclick = () => setImageMode(els.imageMode.getAttribute("aria-pressed") !== "true");
$("newChat").onclick = newChat;
$("openSidebar").onclick = openMenu;
$("closeSidebar").onclick = closeMenu;
$("scrim").onclick = closeMenu;
$("openSettings").onclick = async () => {
  await refreshState().catch(() => {}); refreshAccount().catch(() => {});
  closeMenu(); $("settings").showModal(); refreshSystem();
};
$("openGallery").onclick = openGallery;
$("setImages").onchange = (e) => saveSetting("images_enabled", e.target.checked);
$("setPhone").onchange = (e) => saveSetting("phone_access", e.target.checked);
$("setSystem").onchange = async (e) => {
  await saveSetting("system_analysis", e.target.checked);
  if (e.target.checked) toast("System analyzing is on. Ask Volt about your computer.");
  refreshSystem();
};

// --- System analyzing panel (live while Settings is open) -----------------------
let sysTimer = null;
function bar(label, percent, detail) {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  const hot = p >= 85 ? " hot" : p >= 65 ? " warm" : "";
  return `<div class="sys-row"><div class="sys-label"><span>${esc(label)}</span><span>${esc(detail)}</span></div>` +
    `<div class="sys-bar${hot}"><i style="width:${p}%"></i></div></div>`;
}
const esc = (t) => escapeHtml(String(t ?? ""));
async function refreshSystem() {
  const box = $("sysBox");
  clearTimeout(sysTimer);
  if (!state.settings || !$("setSystem").checked || !$("settings").open) { box.hidden = true; return; }
  try {
    const s = await api("/api/system");
    const c = s.cpu, r = s.ram;
    let html = `<p class="sys-title">${esc(s.os)} · up ${s.uptime_hours} h</p>`;
    html += bar(`CPU · ${c.name}`, c.percent, `${c.percent}% · ${c.cores} cores / ${c.threads} threads`);
    html += bar("RAM", r.percent, `${r.used_gb} of ${r.total_gb} GB`);
    for (const g of s.gpus) {
      html += "busy_percent" in g
        ? bar(`GPU · ${g.name}`, g.busy_percent, `${g.busy_percent}%` + (g.vram_total_gb ? ` · ${g.vram_used_gb} of ${g.vram_total_gb} GB` : ""))
        : `<p class="sys-line">GPU · ${esc(g.name)}</p>`;
    }
    for (const d of s.disks) html += bar(`Disk ${d.mount}`, d.percent, `${d.free_gb} GB free of ${d.total_gb} GB`);
    if (s.drives.length) html += `<p class="sys-line">Drives: ${esc(s.drives.join(" · "))}</p>`;
    const t = Object.entries(s.temps_c || {});
    if (t.length) html += `<p class="sys-line">🌡️ ${t.map(([k, v]) => `${esc(k)} ${v}°C`).join(" · ")}</p>`;
    if (s.battery) html += `<p class="sys-line">🔋 ${s.battery.percent}% · ${s.battery.plugged_in ? "plugged in" : "on battery"}</p>`;
    if (s.top_ram.length) html += `<p class="sys-line">Most RAM: ${s.top_ram.map((p) => `${esc(p.name)} ${p.ram_gb} GB`).join(" · ")}</p>`;
    box.innerHTML = html; box.hidden = false;
  } catch { box.hidden = true; }
  sysTimer = setTimeout(refreshSystem, 3000);
}
$("savePhonePassword").onclick = async () => {
  const password = $("phonePassword").value;
  if (password.length < 6) { toast("Use at least 6 characters."); return; }
  try {
    await api("/api/phone-password", { method: "POST", body: { password } });
    $("phonePassword").value = ""; toast("Phone password saved."); refreshState();
  } catch (e) { toast(e.message || "Could not save the password."); }
};
$("forgetDevices").onclick = async () => {
  if (confirm("Disconnect every phone? They will need to enter the phone password again.")) {
    await api("/api/devices/forget", { method: "POST" }); refreshState();
  }
};
document.querySelectorAll(".suggestions button").forEach((b) => {
  b.onclick = () => {
    if (b.dataset.image) {
      if (!state.settings.images_enabled) { toast("Turn on image creation in Settings first."); return; }
      setImageMode(true); els.input.value = b.dataset.image;
    } else { els.input.value = b.dataset.prompt; }
    autosize(); els.input.focus();
  };
});

(async function start() {
  greet();
  await refreshState().catch(() => {});
  await loadChats();
  let last = null;
  try { last = (location.hash.match(/chat=([0-9a-f]+)/) || [])[1] || localStorage.getItem("volt.lastChat"); } catch (_) { /* storage blocked */ }
  if (last && state.chats.some((c) => c.id === last)) await openChat(last); else renderMessages();
  setInterval(() => refreshState().catch(() => {}), 10000);
  refreshCodeSessions(); setInterval(refreshCodeSessions, 4000);
  const code = (location.hash.match(/code=([0-9a-f]+)/) || [])[1];
  if (code) openSessionFromLink(code);
})();
