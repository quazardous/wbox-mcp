#!/usr/bin/env bash
# wbox-mcp installer
#
# Remote install (clone + install + deps):
#   curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash
#   curl ... | bash -s -- --install-dir ~/my/path
#
# Local install (use current repo as-is, no clone):
#   ./setup.sh --dev-mode
#
# Skip system deps install:
#   ./setup.sh --no-install-deps
#
# Update:
#   ~/.local/share/wbox-mcp/setup.sh
#
set -euo pipefail

REPO="https://github.com/quazardous/wbox-mcp.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
INSTALL_DIR=""
DEV_MODE=false
INSTALL_DEPS=true

# ── Parse args ──────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --install-dir=*)
            INSTALL_DIR="${1#*=}"
            shift
            ;;
        --dev-mode)
            DEV_MODE=true
            shift
            ;;
        --no-install-deps)
            INSTALL_DEPS=false
            shift
            ;;
        -h|--help)
            echo "Usage: setup.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --install-dir DIR   Install to DIR (default: ~/.local/share/wbox-mcp)"
            echo "  --dev-mode          Use current repo directory (no clone, no pull)"
            echo "  --no-install-deps   Skip automatic install of system dependencies"
            echo "  -h, --help          Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

echo "=== wbox-mcp setup ==="

# ── Check setup dependencies ───────────────────────────────────────

for cmd in python3 uv; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: '$cmd' is required but not found." >&2
        exit 1
    fi
done

# ── Detect package manager ─────────────────────────────────────────

PKG_MGR=""
if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
fi

# Binary -> package name mapping per distro
# Format: "binary:dnf_pkg:apt_pkg:pacman_pkg"
DEPS_MAP=(
    "xdotool:xdotool:xdotool:xdotool"
    "weston:weston:weston:weston"
    "cage:cage:cage:cage"
    "grim:grim:grim:grim"
    "weston-screenshooter:weston:weston:weston"
    "xev:xorg-x11-utils:x11-utils:xorg-xev"
    "xclip:xclip:xclip:xclip"
    "xsel:xsel:xsel:xsel"
    "wtype:wtype:wtype:wtype"
    "ydotool:ydotool:ydotool:ydotool"
    "wl-copy:wl-clipboard:wl-clipboard:wl-clipboard"
)

pkg_for() {
    local binary="$1"
    for entry in "${DEPS_MAP[@]}"; do
        IFS=: read -r bin dnf_pkg apt_pkg pacman_pkg <<< "$entry"
        if [ "$bin" = "$binary" ]; then
            case "$PKG_MGR" in
                dnf)    echo "$dnf_pkg" ;;
                apt)    echo "$apt_pkg" ;;
                pacman) echo "$pacman_pkg" ;;
            esac
            return
        fi
    done
}

install_pkgs() {
    local pkgs=("$@")
    [ ${#pkgs[@]} -eq 0 ] && return 0
    echo "Installing system packages: ${pkgs[*]}"
    case "$PKG_MGR" in
        dnf)    sudo dnf install -y "${pkgs[@]}" ;;
        apt)    sudo apt-get install -y "${pkgs[@]}" ;;
        pacman) sudo pacman -S --noconfirm "${pkgs[@]}" ;;
    esac
}

# ── Runtime dependencies ───────────────────────────────────────────

REQUIRED_BINS=(xdotool)
OPTIONAL_BINS=(weston cage grim weston-screenshooter xev xclip xsel wtype ydotool wl-copy)

MISSING_REQUIRED=()
MISSING_OPTIONAL=()

for cmd in "${REQUIRED_BINS[@]}"; do
    command -v "$cmd" &>/dev/null || MISSING_REQUIRED+=("$cmd")
done
for cmd in "${OPTIONAL_BINS[@]}"; do
    command -v "$cmd" &>/dev/null || MISSING_OPTIONAL+=("$cmd")
done

ALL_MISSING=("${MISSING_REQUIRED[@]+"${MISSING_REQUIRED[@]}"}" "${MISSING_OPTIONAL[@]+"${MISSING_OPTIONAL[@]}"}")

if [ ${#ALL_MISSING[@]} -gt 0 ]; then
    echo ""
    if [ ${#MISSING_REQUIRED[@]} -gt 0 ]; then
        echo "MISSING (required): ${MISSING_REQUIRED[*]}"
    fi
    if [ ${#MISSING_OPTIONAL[@]} -gt 0 ]; then
        echo "MISSING (optional): ${MISSING_OPTIONAL[*]}"
        echo "  You need at least one compositor (weston or cage)."
        echo "  grim is needed for cage screenshots, weston-screenshooter for weston."
        echo "  xclip or xsel is needed for clipboard tools (x11 backend)."
        echo "  wtype, ydotool, wl-clipboard are needed for wayland input backend."
    fi
    echo ""

    if $INSTALL_DEPS; then
        if [ -z "$PKG_MGR" ]; then
            echo "Could not detect package manager (dnf/apt/pacman)."
            echo "Please install manually: ${ALL_MISSING[*]}"
            if [ ${#MISSING_REQUIRED[@]} -gt 0 ]; then
                exit 1
            fi
        else
            # Resolve to package names, dedup
            PKGS_TO_INSTALL=()
            for bin in "${ALL_MISSING[@]}"; do
                pkg=$(pkg_for "$bin")
                if [ -n "$pkg" ]; then
                    # Dedup
                    already=false
                    for existing in "${PKGS_TO_INSTALL[@]+"${PKGS_TO_INSTALL[@]}"}"; do
                        [ "$existing" = "$pkg" ] && already=true
                    done
                    $already || PKGS_TO_INSTALL+=("$pkg")
                fi
            done

            if [ ${#PKGS_TO_INSTALL[@]} -gt 0 ]; then
                install_pkgs "${PKGS_TO_INSTALL[@]}"
            fi
        fi
    else
        echo "Skipping dependency install (--no-install-deps)."
        if [ -n "$PKG_MGR" ]; then
            # Show the command they'd need to run
            PKGS=()
            for bin in "${ALL_MISSING[@]}"; do
                pkg=$(pkg_for "$bin")
                if [ -n "$pkg" ]; then
                    already=false
                    for existing in "${PKGS[@]+"${PKGS[@]}"}"; do
                        [ "$existing" = "$pkg" ] && already=true
                    done
                    $already || PKGS+=("$pkg")
                fi
            done
            if [ ${#PKGS[@]} -gt 0 ]; then
                case "$PKG_MGR" in
                    dnf)    echo "  sudo dnf install ${PKGS[*]}" ;;
                    apt)    echo "  sudo apt-get install ${PKGS[*]}" ;;
                    pacman) echo "  sudo pacman -S ${PKGS[*]}" ;;
                esac
            fi
        fi
        echo ""
        if [ ${#MISSING_REQUIRED[@]} -gt 0 ]; then
            echo "Install required dependencies first, then re-run setup." >&2
            exit 1
        fi
    fi
fi

# ── Resolve install dir ─────────────────────────────────────────────

if $DEV_MODE; then
    INSTALL_DIR="$SCRIPT_DIR"
    echo "Dev mode: using local repo at $INSTALL_DIR"
elif [ -n "$INSTALL_DIR" ]; then
    :
elif [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q "wbox-mcp" "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
    INSTALL_DIR="$SCRIPT_DIR"
else
    INSTALL_DIR="$HOME/.local/share/wbox-mcp"
fi

# ── Clone or update ─────────────────────────────────────────────────

if $DEV_MODE; then
    echo "Skipping git operations (dev mode)."
elif [ -d "$INSTALL_DIR/.git" ]; then
    if ! command -v git &>/dev/null; then
        echo "Error: 'git' is required for updates." >&2
        exit 1
    fi
    echo "Updating existing install in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    if ! command -v git &>/dev/null; then
        echo "Error: 'git' is required for initial install." >&2
        exit 1
    fi
    echo "Cloning wbox-mcp to $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO" "$INSTALL_DIR"
fi

# ── Install package ─────────────────────────────────────────────────

echo "Installing package..."
cd "$INSTALL_DIR"
uv venv --python python3 .venv 2>/dev/null || true
# shellcheck source=/dev/null
source .venv/bin/activate
uv pip install -e .

# ── Symlink binaries ────────────────────────────────────────────────

mkdir -p "$HOME/.local/bin"
for bin in wboxr wbox-mcp; do
    if [ -f "$INSTALL_DIR/.venv/bin/$bin" ]; then
        ln -sf "$INSTALL_DIR/.venv/bin/$bin" "$HOME/.local/bin/$bin"
    fi
done

# Check PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo "WARNING: ~/.local/bin is not in your PATH."
    echo "Add this to your ~/.bashrc or ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done! Installed from: $INSTALL_DIR"
if $DEV_MODE; then
    echo "  (dev mode — edits to source take effect immediately)"
fi
echo ""
echo "  wboxr     — $(which wboxr 2>/dev/null || echo "$HOME/.local/bin/wboxr")"
echo "  wbox-mcp  — $(which wbox-mcp 2>/dev/null || echo "$HOME/.local/bin/wbox-mcp")"
echo ""
echo "Quick start:"
echo "  mkdir my-app-mcp && cd my-app-mcp"
echo "  wboxr init"
echo ""
echo "To update later:"
if $DEV_MODE; then
    echo "  git pull && $INSTALL_DIR/setup.sh --dev-mode"
else
    echo "  $INSTALL_DIR/setup.sh"
fi
