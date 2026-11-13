#!/usr/bin/env bash
# ⚡ Volt AI installer for Linux and macOS (no admin rights needed, except to
# install Ollama on Linux, which asks for your password once).
#
#   curl -fsSL https://raw.githubusercontent.com/hossainsufian/volt-ai/main/scripts/install/install.sh | bash
#
# Installs Volt AI into ~/.local/opt/volt-ai with its own Python, adds it to the
# app menu (Linux) or ~/Applications (macOS), sets up the free local AI brain
# (Ollama + models) and, on Linux, the `volt` command (Volt Code).
# Run it again any time to update. Based on Jarvis by isair (non-commercial licence).
#
# Options (environment variables):
#   VOLT_HOME=/path       install somewhere else
#   VOLT_NO_MODELS=1      don't install Ollama or download AI models
#   VOLT_AUTOSTART=1      also start Volt AI (quietly) when you log in
#   VOLT_NO_START=1       don't start Volt AI at the end
#   VOLT_NO_SHORTCUTS=1   don't add menu entries, launchers or the volt command
#   VOLT_SOURCE=/path     install from a local copy of the code instead of GitHub

set -euo pipefail

REPO="hossainsufian/volt-ai"
BRANCH="${VOLT_BRANCH:-main}"
VOLT_HOME="${VOLT_HOME:-$HOME/.local/opt/volt-ai}"
OS="$(uname -s)"
CHAT_MODEL="qwen2.5:3b"
EMBED_MODEL="nomic-embed-text"

say()  { printf '\n\033[38;5;209m⚡ %s\033[0m\n' "$1"; }
info() { printf '   %s\n' "$1"; }
warn() { printf '   \033[33m⚠️  %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m❌ %s\033[0m\n' "$1"; exit 1; }

case "$OS" in
  Linux|Darwin) ;;
  *) fail "This installer is for Linux and macOS. On Windows use the PowerShell installer (see the README)." ;;
esac
command -v curl >/dev/null || fail "curl is needed. Install it with your package manager and run this again."
command -v tar  >/dev/null || fail "tar is needed. Install it with your package manager and run this again."

say "Installing Volt AI into $VOLT_HOME"

# --- Python (via uv, a small, fast Python manager) ---------------------------------
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  info "📦 Installing uv (Python manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
UV="$(command -v uv)"
info "🐍 Getting Python 3.11…"
"$UV" python install 3.11 >/dev/null 2>&1 || true
mkdir -p "$VOLT_HOME/app"
[ -x "$VOLT_HOME/venv/bin/python" ] || "$UV" venv --quiet --python 3.11 "$VOLT_HOME/venv"
PY="$VOLT_HOME/venv/bin/python"

# --- The app itself -----------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "${VOLT_SOURCE:-}" ]; then
  NEW="$VOLT_SOURCE"
else
  info "⬇️  Downloading the latest Volt AI…"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$TMP"
  NEW="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
[ -d "$NEW/src/desktop_app" ] || fail "The download looks incomplete. Try again."
rm -rf "$VOLT_HOME/app/src.new"
cp -R "$NEW/src" "$VOLT_HOME/app/src.new"
rm -rf "$VOLT_HOME/app/src"
mv "$VOLT_HOME/app/src.new" "$VOLT_HOME/app/src"
cp "$NEW/requirements-app.txt" "$NEW/LICENSE" "$NEW/README.md" "$VOLT_HOME/app/"
cp "$NEW/src/desktop_app/volt_web_static/icon.svg" "$VOLT_HOME/volt-ai.svg"

info "📚 Installing what Volt needs (first time: a few minutes)…"
if [ "$OS" = Linux ]; then
  # pynput's evdev backend needs a C compiler; Volt uses its X11 backend, so
  # install pynput on its own without evdev.
  grep -v '^pynput' "$VOLT_HOME/app/requirements-app.txt" > "$TMP/requirements.txt"
  "$UV" pip install --quiet --python "$PY" -r "$TMP/requirements.txt"
  "$UV" pip install --quiet --python "$PY" --no-deps "pynput>=1.7.6"
  "$UV" pip install --quiet --python "$PY" python-xlib six
else
  "$UV" pip install --quiet --python "$PY" -r "$VOLT_HOME/app/requirements-app.txt"
fi

# Sound for voice: PortAudio is a system library on Linux.
if [ "$OS" = Linux ] && [ "$( (ldconfig -p 2>/dev/null || true) | grep -c libportaudio || true)" = 0 ]; then
  warn "Voice features need PortAudio. Install it, for example:"
  info "     Ubuntu/Debian: sudo apt install libportaudio2     Arch: sudo pacman -S portaudio     Fedora: sudo dnf install portaudio"
fi

# --- Launcher -------------------------------------------------------------------------
cat > "$VOLT_HOME/run.sh" <<EOF
#!/bin/sh
# Volt AI launcher (modified Jarvis by isair, see $VOLT_HOME/app/LICENSE)
cd "$VOLT_HOME/app/src" || exit 1
export PYTHONPATH="$VOLT_HOME/app/src"
mkdir -p "\$HOME/.local/state/volt-ai"
exec "$PY" -m desktop_app "\$@" >> "\$HOME/.local/state/volt-ai/volt-ai.log" 2>&1
EOF
chmod +x "$VOLT_HOME/run.sh"

# --- Settings: opening the app shows the chat; a login start (VOLT_AUTOSTART) stays quiet
"$PY" - "$CHAT_MODEL" <<'EOF'
import json, os, sys
path = os.path.expanduser("~/.config/jarvis/config.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
cfg.setdefault("ollama_chat_model", sys.argv[1])
cfg.setdefault("open_chat_on_start", False)
json.dump(cfg, open(path, "w"), indent=2)
EOF

# --- Free local AI brain (Ollama) -------------------------------------------------------
if [ -z "${VOLT_NO_MODELS:-}" ]; then
  if ! command -v ollama >/dev/null; then
    if [ "$OS" = Linux ]; then
      info "🧠 Installing Ollama (it asks for your password once)…"
      curl -fsSL https://ollama.com/install.sh | sh
    elif command -v brew >/dev/null; then
      info "🧠 Installing Ollama with Homebrew…"
      brew install ollama
    else
      warn "Install Ollama from https://ollama.com/download, open it once, then run this installer again."
    fi
  fi
  if command -v ollama >/dev/null; then
    if ! curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
      if [ "$OS" = Darwin ] && [ -d /Applications/Ollama.app ]; then open -a Ollama; else (ollama serve >/dev/null 2>&1 &); fi
      for _ in $(seq 1 30); do curl -fsS -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
    fi
    info "🧠 Downloading the AI models (about 2.3 GB the first time)…"
    ollama pull "$CHAT_MODEL"
    ollama pull "$EMBED_MODEL"
  fi
fi

# --- App menu, login start, and the volt command ----------------------------------------
if [ -z "${VOLT_NO_SHORTCUTS:-}" ]; then
  if [ "$OS" = Linux ]; then
    APPS="$HOME/.local/share/applications"
    mkdir -p "$APPS" "$HOME/.local/bin"
    cat > "$APPS/volt-ai.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Volt AI
Comment=Your local AI assistant (chat and voice)
Exec=$VOLT_HOME/run.sh --open-chat
Icon=$VOLT_HOME/volt-ai.svg
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
    # Volt Code: the coding assistant for the terminal.
    cat > "$HOME/.local/bin/volt" <<EOF
#!/bin/sh
# ⚡ Volt Code: coding assistant in the terminal (part of Volt AI).
export PYTHONPATH="$VOLT_HOME/app/src"
exec "$PY" -m volt_code "\$@"
EOF
    chmod +x "$HOME/.local/bin/volt"
    cat > "$APPS/volt-code.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Volt Code
Comment=Coding assistant in the terminal (part of Volt AI)
Exec=$HOME/.local/bin/volt
Icon=$VOLT_HOME/volt-ai.svg
Terminal=true
Categories=Development;Utility;
EOF
    if [ -n "${VOLT_AUTOSTART:-}" ]; then
      mkdir -p "$HOME/.config/autostart"
      sed -e "s|--open-chat||" -e "s|^Name=Volt AI|Name=Volt AI (start quietly)|" \
        "$APPS/volt-ai.desktop" > "$HOME/.config/autostart/volt-ai.desktop"
    fi
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
  else
    # macOS: a small app in ~/Applications that opens Volt AI's chat.
    APP="$HOME/Applications/Volt AI.app"
    mkdir -p "$APP/Contents/MacOS"
    cat > "$APP/Contents/MacOS/volt-ai" <<EOF
#!/bin/sh
exec "$VOLT_HOME/run.sh" --open-chat
EOF
    chmod +x "$APP/Contents/MacOS/volt-ai"
    cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Volt AI</string>
  <key>CFBundleIdentifier</key><string>io.github.hossainsufian.voltai</string>
  <key>CFBundleExecutable</key><string>volt-ai</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
    if [ -n "${VOLT_AUTOSTART:-}" ]; then
      mkdir -p "$HOME/Library/LaunchAgents"
      cat > "$HOME/Library/LaunchAgents/io.github.hossainsufian.voltai.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.github.hossainsufian.voltai</string>
  <key>ProgramArguments</key><array><string>$VOLT_HOME/run.sh</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    fi
  fi
fi

say "Volt AI is installed."
if [ "$OS" = Linux ]; then
  info "🖱️  Open it: app menu → Volt AI"
  [ -z "${VOLT_NO_SHORTCUTS:-}" ] && info "💻 Volt Code: open a new terminal and type  volt"
else
  info "🖱️  Open it: Finder → your home folder → Applications → Volt AI"
fi
info "🔄 To update later, run the same install line again."

if [ -z "${VOLT_NO_START:-}" ]; then
  (setsid "$VOLT_HOME/run.sh" --open-chat >/dev/null 2>&1 &) 2>/dev/null || ("$VOLT_HOME/run.sh" --open-chat >/dev/null 2>&1 &)
  info "🚀 Starting Volt AI…"
fi
