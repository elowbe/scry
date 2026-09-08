# Installing Scry - Server

Download/extract the source into a folder you own and keep it there after setup. Install from an ordinary user account in your graphical desktop session. The setup scripts request administrator access only for OS packages, device permissions and the Windows controller driver.

## Linux

```sh
bash scripts/bootstrap.sh
```

Setup detects apt (Debian/Ubuntu), dnf (Fedora), pacman (Arch) or zypper (openSUSE). It installs FFmpeg, system Python/GObject/GStreamer for Wayland, AppIndicator dependencies and uinput permissions. Astral uv installs Python 3.12 and the app dependencies in `.venv`; repeated runs retain that environment. Use the **Scry - Server** application-menu shortcut afterward. Sign out and back in if remote input is unavailable after setup.

For declarative/immutable distributions, provision the equivalent packages yourself, then run `SCRY_SKIP_SYSTEM_PACKAGES=1 bash scripts/bootstrap.sh`. NixOS, Alpine/musl and immutable images are not validated by this installer. No installer can guarantee arbitrary compositor, driver and package repository combinations; `doctor --probe-capture` verifies the installed host.

Wayland needs your desktop's ScreenCast portal backend (for example `xdg-desktop-portal-kde`, `-gnome` or `-wlr`) and PipeWire. Desktop installations usually supply it. GNOME uses Mutter capture; other compositors may ask you to select a monitor. X11 uses FFmpeg x11grab. Set `SCRY_SYSTEM_PYTHON` if the distro Python with GI is not `/usr/bin/python3`. Scry deliberately keeps capture GI bindings in the system interpreter.

GNOME needs an AppIndicator extension enabled to display tray icons. Other desktops need a StatusNotifier/AppIndicator tray. If no tray is available, use `scry-server serve` in a terminal. Scry cannot force a desktop shell to expose its tray.

FFmpeg must provide `libx264` or working `h264_nvenc`, plus `pulse` and `x11grab` where applicable. Some Fedora/openSUSE repositories omit H.264 encoders; enable your distribution's multimedia repository and install its full FFmpeg package if doctor reports an encoder error. Setup does not silently add third-party OS repositories or change GPU drivers.

## Windows 10/11 x64

Double-click **Setup.cmd**, or in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

Microsoft App Installer (`winget`) must be available; the script explains how to install it if missing. Setup installs FFmpeg, the signed ViGEmBus controller driver, uv, Python 3.12, Python dependencies and a Start-menu shortcut. Accept the driver elevation prompt and restart if requested. Start **Scry - Server** from the Start menu. A minimized console supports graceful server shutdown; the status menu is in the notification area, possibly under the overflow arrow.

Video uses Windows desktop capture (`gdigrab`); audio uses WASAPI loopback of the default output device. Keyboard/mouse use Windows injection and controllers use ViGEmBus. Steam is discovered from its registry entry or standard installation folder. Native Windows games do not use Proton. Run the server at the same privilege level as your games; Windows prevents ordinary processes controlling elevated windows and the secure desktop.

Allow Scry's Python executable through Windows Firewall on your **private** network when prompted. HTTPS uses TCP 8443; WebRTC needs UDP connectivity. Do not create a public Internet firewall exception by default.

**Windows is implemented but has not been tested on a physical Windows host.** The CI workflow checks imports, platform branches and tests on Windows. Before a release, validate desktop/audio capture, keyboard/mouse/controller, Steam lifecycle, tray stop/restart and DLSS on Windows hardware. ViGEmBus is an archived upstream driver; its signed final release is required by vgamepad.

## Optional DLSS

Provide **your own `nvngx_dlssnr.dll`**. Scry never downloads or redistributes this neural rendering DLL.

```sh
# Linux, from the checkout
.venv/bin/python -m gamestream setup --dlss-dll /path/to/nvngx_dlssnr.dll
```

```powershell
# Windows, from the checkout
.venv\Scripts\python.exe -m gamestream setup --dlss-dll 'C:\path\nvngx_dlssnr.dll'
```

Setup prompts for the optional DLL path; press Enter to skip. You can also pass `--dlss-dll PATH` to the Linux bootstrap or `-DlssDll PATH` to the Windows bootstrap. Setup copies the DLL into `.state/runtime`, downloads the Super Resolution DLL from NVIDIA's DLSS repository, and reuses installed Proton or downloads checksum-verified GE-Proton10-15 on Linux. The MIT direct bridge, helper executable, caller shim and Python integration are included in this project; ComfyUI is not required. Downloaded Proton includes its component licenses. NVIDIA's downloaded Super Resolution runtime remains subject to NVIDIA's terms.

DLSS requires x86-64, a supported NVIDIA RTX GPU, compatible drivers and a compatible neural runtime. Setup validates known GPU/runtime constraints; actual feature execution is checked during streaming. Ordinary streaming works without either NVIDIA or DLSS. Unsupported DLL/GPU pairs fail clearly instead of silently claiming enhancement. On Linux, system Vulkan/graphics drivers still must work; setup does not replace your drivers.

## Pair and check

The tray opens **Scry Web** automatically after the server responds. The host prints a private pairing URL. On another device, replace `localhost` with the host LAN/VPN address and approve the self-signed certificate once. Keep the token private. For a certificate matching a fixed address, set `server.public_host`, then run `init --force-certificate` and restart.

Choose **Desktop** first, or launch a game from the library. Host status explains missing requirements. Steam and Proton are optional for desktop streaming. Encoding probes NVENC and falls back to CPU H.264; reduce resolution/FPS if the CPU cannot keep up.

```sh
.venv/bin/python -m gamestream doctor --probe-capture
.venv/bin/python -m gamestream serve   # foreground, without tray
```

On Windows substitute `.venv\Scripts\python.exe`. `scry-server` is the new CLI; `game-stream` remains a compatibility alias. `--config PATH` goes before the command. Relative config paths resolve beside the TOML file, so shortcuts work from any working directory. `SCRY_CONFIG` (or legacy `GAMESTREAM_CONFIG`) selects another configuration for library callers; CLI `--config` is explicit.

Tray brightness indicates whether its owned HTTPS server is responding; Host status reports streaming readiness. Stop closes sessions and releases input. Logs are under `.state`. Quit also stops the server. Run only one tray/foreground/service instance per configured port.

## Updates and removal

Stop Scry before updating the source and rerun setup. The installer preserves `.venv`, configuration, tokens and downloaded runtimes. It does not reset existing credentials.

To remove Scry, stop it, delete its application/Start-menu shortcut and the checkout. OS packages and the shared ViGEmBus driver remain for other applications; remove those with your OS package manager if no longer needed. The Linux udev rule is `/etc/udev/rules.d/70-scry-uinput.rules` the module rule is `/etc/modules-load.d/scry-uinput.conf`, and the dedicated group is `scry-input`.
