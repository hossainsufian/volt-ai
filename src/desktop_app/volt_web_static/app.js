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
    bubble.appendChild(actions);
  } else if (m.role === "assistant") {
    bubble.innerHTML = renderMarkdown(m.text || "");
    const actions = document.createElement("div"); actions.className = "msg-actions";
    const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy";
    copy.onclick = () => copyText(m.text, copy);
    actions.appendChild(copy); bubble.appendChild(actions);
  } else {
    bubble.textContent = m.text;
  }
  bubble.querySelectorAll(".codeblock .copy").forEach((btn) => {
    btn.onclick = () => copyText(btn.closest(".codeblock").querySelector("code").textContent, btn);
  });
  if (m.role === "assistant" && !String(m.status || "").startsWith("pending")) {
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
  els.send.disabled = !els.input.value.trim();
  const brain = state.brain || "fast";
  const claude = brain === "claude";
  const sel = $("brainSel");
  const hasKey = !!(state.claude && state.claude.has_key);
  const claudeOpt = sel.querySelector('option[value="claude"]');
  claudeOpt.disabled = !hasKey;  // <option hidden> is ignored by some phone browsers
  claudeOpt.textContent = hasKey ? "✨ Claude" : "✨ Claude (needs key)";
  if (document.activeElement !== sel) sel.value = brain;
  sel.title = { fast: "Fast: quick answers from this computer. Private, no web search.",
    smart: "Smart: can search the web and remember things, but much slower.",
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
  if (!text || busy()) return;
  const imageMode = els.imageMode.getAttribute("aria-pressed") === "true";
  try {
    const chat = await ensureChat();
    els.input.value = ""; autosize();
    if (imageMode) {
      await api(`/api/chats/${chat.id}/images`, { method: "POST", body: { prompt: text } });
      setImageMode(false);
    } else {
      await api(`/api/chats/${chat.id}/messages`, { method: "POST", body: { text, artifact: artifactOn() } });
    }
    await openChat(chat.id); loadChats();
  } catch (err) {
    els.input.value = text; autosize(); toast(err.message);
  }
}
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

function setImageMode(on) {
  els.imageMode.setAttribute("aria-pressed", String(on));
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
})();
