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
      html += `<div class="codeblock"><button type="button" class="copy">Copy</button><pre><code>${escapeHtml(parts[i])}</code></pre></div>`;
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
  if (m.status === "pending" || m.status === "pending_image") {
    bubble.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>' +
      (m.status === "pending_image" ? ' <small>Creating your image…</small>' : "");
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
    btn.onclick = () => copyText(btn.nextElementSibling.textContent, btn);
  });
  row.appendChild(bubble);
  return row;
}
function renderMessages() {
  const atBottom = els.thread.scrollHeight - els.thread.scrollTop - els.thread.clientHeight < 80;
  els.messages.innerHTML = "";
  for (const m of state.messages) els.messages.appendChild(messageNode(m));
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
  const claude = !!(state.claude && state.claude.active);
  const ready = claude || state.available;
  els.status.textContent = claude ? "Claude" : ready ? "" : "Starting…";
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
  localStorage.setItem("volt.lastChat", id);
  renderChatList(); renderMessages(); closeMenu(); schedulePoll();
  els.thread.scrollTop = els.thread.scrollHeight;
}
function newChat() {
  state.current = null; state.messages = [];
  localStorage.removeItem("volt.lastChat");
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
      await api(`/api/chats/${chat.id}/messages`, { method: "POST", body: { text } });
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
        if (!busy()) loadChats();
      } catch (_) { /* retry next tick */ }
      schedulePoll();
    }, 1200);
  }
}
function setImageMode(on) {
  els.imageMode.setAttribute("aria-pressed", String(on));
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
  $("setImages").disabled = $("setPhone").disabled = !s.local;
  $("laptopOnly").hidden = s.local;
  $("phoneRow").hidden = !s.local;
  $("phoneBox").hidden = !(s.local && s.settings.phone_access && s.phone_url);
  $("phoneUrl").textContent = s.phone_url || "";
  $("tsLine").hidden = !s.tailscale_url;
  $("tsUrl").textContent = s.tailscale_url || "";
  $("pairCode").textContent = s.pairing_code || "";
  els.imageMode.hidden = !s.settings.images_enabled;
  state.claude = s.claude || {};
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
els.imageMode.onclick = () => setImageMode(els.imageMode.getAttribute("aria-pressed") !== "true");
$("newChat").onclick = newChat;
$("openSidebar").onclick = openMenu;
$("closeSidebar").onclick = closeMenu;
$("scrim").onclick = closeMenu;
$("openSettings").onclick = async () => {
  await refreshState().catch(() => {}); refreshAccount().catch(() => {});
  closeMenu(); $("settings").showModal();
};
$("openGallery").onclick = openGallery;
$("setImages").onchange = (e) => saveSetting("images_enabled", e.target.checked);
$("setPhone").onchange = (e) => saveSetting("phone_access", e.target.checked);
$("forgetDevices").onclick = async () => {
  if (confirm("Disconnect every paired phone? They will need a new pairing code.")) {
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
  const last = localStorage.getItem("volt.lastChat");
  if (last && state.chats.some((c) => c.id === last)) await openChat(last); else renderMessages();
  setInterval(() => refreshState().catch(() => {}), 10000);
})();
