#!/bin/sh
# cos installer. From a checkout:  ./install.sh
# Or:  curl -fsSL https://raw.githubusercontent.com/hanshika0292/cos/main/install.sh | sh
set -e
REPO="https://github.com/hanshika0292/cos.git"
COS_HOME="${COS_HOME:-$HOME/.cos}"

DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
if [ ! -f "$DIR/cos.py" ] || [ ! -f "$DIR/snap.js" ]; then
  # piped from curl: fetch (or update) the code into ~/.cos/app
  command -v git >/dev/null || { echo "cos needs git (xcode-select --install)"; exit 1; }
  DIR="$COS_HOME/app"
  if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only; else git clone -q --depth 1 "$REPO" "$DIR"; fi
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "cos needs python3 (on macOS: xcode-select --install)"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 6))' || { echo "cos needs Python 3.6+"; exit 1; }

# stable launcher (no spaces, survives moving the checkout): ~/.cos/bin/cos -> python3 cos.py
mkdir -p "$COS_HOME/bin" "$HOME/.local/bin"
cat > "$COS_HOME/bin/cos" <<EOF
#!/bin/sh
exec "$PY" "$DIR/cos.py" "\$@"
EOF
chmod +x "$COS_HOME/bin/cos" "$DIR/cos.py" "$DIR/hooks/cos-hook.sh"
ln -sf "$COS_HOME/bin/cos" "$HOME/.local/bin/cos"
echo "installed: cos -> $DIR"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add ~/.local/bin to your PATH, e.g.  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac

# guided setup; answers come from the keyboard even when this script is piped from curl
if [ -n "${COS_SKIP_SETUP-}" ]; then
  echo "skipped setup; run \`cos setup\` when ready"
elif [ -r /dev/tty ] && [ -z "${COS_NONINTERACTIVE-}" ]; then
  "$COS_HOME/bin/cos" setup < /dev/tty
else
  "$COS_HOME/bin/cos" setup --yes
fi
