"use strict";
document.getElementById("pairForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const password = document.getElementById("password").value;
  try {
    const res = await fetch("/api/pair", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { err.textContent = data.error || "Could not connect."; return; }
    location.href = "/";
  } catch (_) {
    err.textContent = "Can't reach Volt AI. Check that the computer is on and Tailscale is on on this phone.";
  }
});
