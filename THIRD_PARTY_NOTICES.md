# Third-party notices

Scry's own code is MIT licensed (LICENSE). The icons were supplied for Scry branding.

`gamestream/dlss_support/{launcher,paths,settings,diagnostics}.py` were adapted from
ComfyUI-DLSS5-Enhancer-Linux / ComfyUI-DLSS5-NR, copyright 2026 Blueforcer,
under MIT. The original license is retained in `gamestream/dlss_support/LICENSE`.
Only the standalone integration modules are included; no ComfyUI environment or
machine configuration is copied. Scry changes runtime discovery and direct Windows
launching and uses the bundled bridge on both platforms.

The D3D12 bridge and caller shim retain their MIT attribution in
`native/BRIDGE_LICENSE` and source headers. Compiled project helper binaries are
included with their source and rebuild commands in `native/README.md`.

NVIDIA's `nvngx_dlssnr.dll` is user-supplied and never downloaded by Scry. Optional
setup downloads `nvngx_dlss.dll` from the NVIDIA/DLSS repository; it remains under
NVIDIA's runtime license, not Scry's MIT license. Neither DLL belongs in source
releases. No NVIDIA runtime is bundled in Scry's wheels.

Optional Linux setup downloads GE-Proton from GloriousEggroll/proton-ge-custom's
published release with its upstream SHA-512 checksum. Its component licenses remain
in the downloaded distribution. uv and its managed Python come from Astral's official
installer. Windows FFmpeg and ViGEmBus use their winget upstream packages and licenses;
ViGEmBus is an archived project. Python dependencies retain their upstream licenses.
