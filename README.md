# Scry - Server

<img src="web/icon.svg" width="100" alt="Scry diamond play icon">

Stream your games and desktop to a browser with **Scry**.

**Scry - Server** runs on your host computer. **Scry Web** is the browser client, with a game library, desktop access, live stream settings and host diagnostics. Video and audio travel over WebRTC, while keyboard, mouse and controller input return to the host.

Scry supports standard streaming without an NVIDIA GPU. Optional **DLSS 5 Neural Rendering** enhances captured frames before encoding, without modifying game files.

## Features

- Steam library discovery and launching, plus configurable standalone Ubisoft Connect entries.
- Desktop streaming without Steam or a game launcher.
- H.264 video with automatic NVIDIA NVENC selection and CPU encoding fallback.
- System audio, physical keyboard input, relative mouse input and Xbox-style controller support.
- Live resolution, frame rate, bitrate and DLSS 5 Neural Rendering controls.
- A desktop tray menu with server status, start/stop controls, browser access and logs.
- Authenticated HTTPS access and one controlling client at a time.

## Platform support

| | Linux | Windows |
| --- | --- | --- |
| Desktop capture | X11 or Wayland/PipeWire | Windows desktop capture |
| System audio | PulseAudio or PipeWire's PulseAudio interface | WASAPI loopback |
| Keyboard and mouse | uinput | Native Windows input |
| Controller | Virtual Xbox-style uinput device | ViGEmBus virtual controller |
| DLSS 5 Neural Rendering runtime | Dedicated Proton prefix | Native Windows process |

The server requires a logged-in graphical session. Windows support targets Windows 10/11 x64 and is experimental; hardware validation is limited. Linux compatibility depends on the distribution's multimedia packages, graphics drivers and compositor.

Output is SDR BT.709. HDR streaming is not supported. CPU encoding performance depends on the host; lower resolution or frame rate can help on slower systems.

## Installation

Download or clone the project into a folder you own. Keep the folder in place after setup, since the application shortcut points to it.

### Linux

From the project folder:

```bash
bash scripts/bootstrap.sh
```

The installer supports apt, dnf, pacman and zypper. It installs system dependencies and configures remote-input permissions. A sign-out and sign-in may be needed before input is available.

### Windows

Double-click **Setup.cmd**, or run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

Windows setup requires Microsoft App Installer (`winget`). It installs FFmpeg and the ViGEmBus controller driver, which may request administrator access or a restart.

Both installers manage Python, the project virtual environment, application dependencies, a local HTTPS certificate and a private pairing token. Setup also creates the **Scry - Server** application-menu shortcut and prompts for the optional DLSS 5 Neural Rendering DLL.

The full installation guide is located at [`docs/installation.md`](docs/installation.md). It covers package requirements, Linux distribution support, Windows prerequisites, desktop tray support, DLSS 5 Neural Rendering setup, updates and removal.

## First connection

1. Open **Scry - Server** from the application or Start menu. The tray opens **Scry Web** when the server is ready.
2. On another device, open the host's pairing URL, replacing `localhost` with the host's LAN or VPN address. Accept the local HTTPS certificate on that device.
3. Select **Desktop** or an installed game from the library. Approve screen sharing on the host if prompted.
4. Click the video to capture the mouse and begin controlling the host.

The pairing URL grants access to the host. Keep it and `.state/access.token` private.

A bright tray icon means the server is responding; a dim icon means it is starting or stopped. **Host status** in Scry Web reports streaming requirements and optional capabilities. **Stop server** ends active sessions; **Quit** also closes the tray application.

Desktop streaming leaves applications open when the stream ends. Ending a game session closes the active game after confirmation in the client.

## Optional DLSS 5 Neural Rendering

DLSS 5 Neural Rendering requires a supported NVIDIA RTX GPU, compatible drivers and a compatible **user-supplied `nvngx_dlssnr.dll`**. Scry does not download or redistribute the DLSS 5 Neural Rendering DLL.

Supply its path during installation, or run setup afterward:

**Linux**

```bash
.venv/bin/python -m gamestream setup --dlss-dll /path/to/nvngx_dlssnr.dll
```

**Windows**

```powershell
.venv\Scripts\python.exe -m gamestream setup --dlss-dll 'C:\path\nvngx_dlssnr.dll'
```

Setup copies the DLSS 5 Neural Rendering DLL into the configured runtime directory, downloads the supporting NVIDIA DLSS Super Resolution runtime and prepares Proton on Linux when needed. The open-source bridge and helper programs are included; ComfyUI is not required. Downloaded components retain their upstream licenses.

DLSS 5 Neural Rendering runs as a separate post-processing stage between capture and encoding. It adds processing latency and may reduce the delivered frame rate. Toggle it from the player toolbar or with **Ctrl+Alt+D**.

### Rendering options

The default pass order is **DLSS Super Resolution upscale → DLSS 5 Neural Rendering**. **Neural rendering before DLSS upscale** reverses that order. At native resolution (1×), only the DLSS 5 Neural Rendering pass runs.

Stabilization is disabled by default. Two methods are available:

- **Static regions:** smooths output where source pixels remain unchanged, while preserving the current render around motion. Moving regions can still flicker.
- **Optical flow:** experimental motion-guided stabilization that can smear or distort moving content.

Stabilization uses one rendering worker. With stabilization disabled, the worker-count setting controls parallel rendering. Changing rendering dimensions or options rebuilds the renderer and can briefly interrupt video.

## Stream settings

Open **Settings** in the player to choose a quality preset or adjust resolution, frame rate, bitrate, buffering and rendering options. Applied settings persist in `.state/player-settings.json`.

The video bitrate cap excludes audio and network overhead. In VBR mode, a target bitrate of `0` selects quality-driven encoding; a cap of `0` removes the application video bitrate limit. Encoder-specific settings, such as NVENC presets and adaptive quantization, apply when that encoder is active.

Host-level settings belong in [`config.toml`](config.toml):

| Section | Settings |
| --- | --- |
| `[server]` | Listen address, HTTPS port, certificates and STUN/TURN servers |
| `[stream]` | Capture backend, monitor, audio source, encoder and video defaults |
| `[steam]` | Steam location, launch command and optional Proton configuration |
| `[input]` | Remote input and mouse sensitivity |
| `[dlss]` | DLSS 5 Neural Rendering runtime, pass order, stabilization and worker settings |
| `[[ubisoft_games]]` | Standalone Ubisoft game entries |

Steam and display settings are discovered automatically where supported. Relative file paths resolve beside the configuration file. Use `--config PATH` before the command to select another configuration.

## Input controls

- Click the video to request relative mouse capture. The stream includes the host cursor when visible.
- Press **Escape** to release pointer lock. Use the player's **ESC** button to send Escape to the host.
- Keyboard input follows physical key positions rather than characters from the client's keyboard layout.
- The first standard-mapped browser controller is exposed as an Xbox-compatible controller.
- Browser and operating-system reserved shortcuts cannot be forwarded.

Remote input controls the logged-in desktop, including applications outside the streamed game. Windows restricts input to elevated windows and the secure desktop.

## Network access

For local access, use the host's LAN address. For access from another network, use a private VPN or configure a STUN/TURN service. HTTPS uses TCP port `8443` by default; WebRTC also requires UDP connectivity.

Set `server.public_host` to the address clients use, then regenerate the certificate and restart the server:

```bash
scry-server init --force-certificate
```

Example TURN configuration:

```toml
[server]
ice_urls = ["stun:turn.example.net:3478", "turn:turn.example.net:3478?transport=udp"]
ice_username = "scry"
ice_credential = "replace-with-a-long-secret"
```

Restrictive NAT or firewall configurations may require TURN relay access. TURN credentials are available only to authenticated clients.

HTTPS and a private access token protect the web interface and API. Session cookies are Secure, HttpOnly and SameSite-restricted. Run the server as an ordinary user and keep credentials out of shared files and source control.

## Diagnostics and command line

The `scry-server` command is available in the installed virtual environment. `game-stream` remains a compatibility alias.

```bash
scry-server doctor                 # Check host requirements
scry-server doctor --probe-capture # Capture a frame to verify the desktop backend
scry-server list-games             # List discovered games
scry-server serve                  # Run in the foreground
scry-server tray                   # Run with the desktop menu
```

Without activating the environment, use `.venv/bin/python -m gamestream` on Linux or `.venv\Scripts\python.exe -m gamestream` on Windows in place of `scry-server`.

Server logs are stored in `.state/game-stream.log`; tray output is stored in `.state/scry-tray.log`. Scry Web displays connection and capture errors in host status and stream telemetry. Video capture automatically retries after a stall; portal-based capture may ask for screen-sharing permission again.

For Linux background operation, [`config/game-stream.service`](config/game-stream.service) provides a user-service template. Set its paths to the installation folder and run it in the graphical session. Use one server instance per configured port.

## Development

Install the test dependencies and run the automated checks:

```bash
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest
node --test tests/test_client.cjs
```

On Windows, substitute `.venv\Scripts\python.exe` for the Python path. Hardware-dependent probes are separate from the automated test suite. For an end-to-end capture and WebRTC check in a graphical session:

```bash
.venv/bin/python -m tests.probe_webrtc
```

The authenticated `/info` endpoint documents the HTTP API, WebRTC negotiation, stream settings and binary input protocol. See [`native/README.md`](native/README.md) for native helper build instructions.

## License

Scry's source code is available under the [MIT license](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for bundled components and runtime licensing. NVIDIA runtime DLLs, credentials and local application state are excluded from release packages.
