"use strict";
document.getElementById("pairForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const code = document.getElementById("code").value.trim();
  try {
    const res = await fetch("/api/pair", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { err.textContent = data.error || "Could not connect."; return; }
    location.href = "/";
  } catch (_) {
    err.textContent = "Can't reach your laptop. Check both are on the same Wi-Fi and Volt AI is running.";
  }
});
