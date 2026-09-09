#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Opt out for declarative/immutable distributions with dependencies already installed.
if [[ "${SCRY_SKIP_SYSTEM_PACKAGES:-0}" == 1 ]]; then
  echo 'Using existing system dependencies (SCRY_SKIP_SYSTEM_PACKAGES=1).'
  exit 0
fi
privilege=()
if (( EUID != 0 )); then privilege=(sudo); fi
if command -v apt-get >/dev/null 2>&1; then
  "${privilege[@]}" apt-get update
  "${privilege[@]}" apt-get install -y ffmpeg python3 python3-dev libgirepository1.0-dev libcairo2-dev build-essential pkg-config python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-gstreamer-1.0 gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good xdg-desktop-portal libvulkan1 libpipewire-0.3-dev
elif command -v dnf >/dev/null 2>&1; then
  "${privilege[@]}" dnf install -y ffmpeg-free python3 python3-devel gobject-introspection-devel cairo-devel gcc pkgconf-pkg-config python3-gobject gtk3 libappindicator-gtk3 gstreamer1-plugins-base gstreamer1-plugins-good pipewire-gstreamer xdg-desktop-portal vulkan-loader pipewire-devel
elif command -v pacman >/dev/null 2>&1; then
  "${privilege[@]}" pacman -S --needed --noconfirm ffmpeg python python-gobject gobject-introspection cairo base-devel gtk3 libappindicator-gtk3 gst-plugins-base gst-plugins-good pipewire xdg-desktop-portal vulkan-icd-loader
elif command -v zypper >/dev/null 2>&1; then
  "${privilege[@]}" zypper --non-interactive install ffmpeg python3 python3-devel gobject-introspection-devel cairo-devel gcc python3-gobject typelib-1_0-Gtk-3_0 libappindicator3-1 gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugin-pipewire xdg-desktop-portal libvulkan1 pipewire-devel pkg-config
else
  echo 'No supported package manager found. See docs/installation.md for manual dependencies.' >&2
  echo 'After provisioning dependencies, rerun with SCRY_SKIP_SYSTEM_PACKAGES=1.' >&2
  exit 1
fi
"${privilege[@]}" groupadd -f scry-input
"${privilege[@]}" install -m 0644 "$project_dir/config/70-game-stream-uinput.rules" /etc/udev/rules.d/70-scry-uinput.rules
"${privilege[@]}" install -m 0644 "$project_dir/config/scry-uinput.conf" /etc/modules-load.d/scry-uinput.conf
"${privilege[@]}" modprobe uinput
"${privilege[@]}" udevadm control --reload-rules
"${privilege[@]}" udevadm trigger --name-match=uinput
# Dedicated group avoids granting access to physical keyboards through the input group.
"${privilege[@]}" groupadd -f scry-input
"${privilege[@]}" usermod -aG scry-input "${SUDO_USER:-$USER}"
echo 'Input permissions installed. Sign out and back in if /dev/uinput is not yet writable.'
