#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ "$(uname -s)" != Linux ]]; then
  echo 'Use scripts/bootstrap.ps1 on Windows. This installer supports Linux.' >&2
  exit 1
fi
# Keep a working environment intact on repeated runs.
if ! command -v uv >/dev/null 2>&1; then
  mkdir -p "$project_dir/.state/tools"
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --proto '=https' --tlsv1.2 https://astral.sh/uv/install.sh -o "$installer"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$installer" https://astral.sh/uv/install.sh
  else
    echo 'Install curl or wget with your package manager, then rerun setup.' >&2
    exit 1
  fi
  UV_UNMANAGED_INSTALL="$project_dir/.state/tools" sh "$installer"
  export PATH="$project_dir/.state/tools:$PATH"
fi
# System Python supplies distro GI bindings for Wayland; the app uses managed Python.
bash "$project_dir/scripts/install-host.sh"
uv python install 3.12
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
  uv venv --python 3.12 "$project_dir/.venv"
fi
uv pip install --python "$project_dir/.venv/bin/python" -e "$project_dir"
setup_args=("$@")
if (( $# == 0 )) && [[ -t 0 ]]; then
  read -r -p 'Optional DLSS: path to your nvngx_dlssnr.dll (Enter to skip): ' dlss_file
  if [[ -n "$dlss_file" ]]; then setup_args=(--dlss-dll "$dlss_file"); fi
fi
"$project_dir/.venv/bin/python" -m gamestream --config "$project_dir/config.toml" setup "${setup_args[@]}"
"$project_dir/.venv/bin/python" "$project_dir/scripts/install-desktop.py"
echo 'Setup complete. Open Scry - Server from your application menu.'
