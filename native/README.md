# Native DLSS stream host

`dlss5nr_stream_host.cpp` is an MIT-licensed RGBA8 transport for the bundled
MIT-licensed `dlss5nr_bridge.dll`. It does not contain or redistribute NVIDIA code.

Rebuild on Ubuntu with:

```bash
x86_64-w64-mingw32-g++-posix -std=c++17 -O3 -ffast-math -municode \
  -static -static-libgcc -static-libstdc++ -pthread \
  native/dlss5nr_stream_host.cpp -o native/bin/dlss5nr_stream_host.exe
```

The checked-in binary was built without a system-wide toolchain by using Zig's MinGW target:

```bash
ZIG_GLOBAL_CACHE_DIR=/tmp/gamestream-zig-cache \
ZIG_LOCAL_CACHE_DIR=/tmp/gamestream-zig-local \
  zig c++ -target x86_64-windows-gnu -std=c++17 -O3 -ffast-math -municode \
  -Wno-nullability-completeness -static -pthread \
  native/dlss5nr_stream_host.cpp -o native/bin/dlss5nr_stream_host.exe
```

The DNR4 control protocol accepts `frame_count = 0` for unknown-duration streaming. With
`GAMESTREAM_FRAME_MAP`, pixels occupy a private shared mapping (input RGBA8 followed by output
RGBA8, then input-sized FP16 XY backward motion); pipes carry only frame/result headers. Without it, each DNR4 pipe frame carries RGBA8 input followed by FP16 XY motion.
The 68-byte video header and FRM3/OUT3/END3 messages retain their layouts.
Legacy DNR3 callers remain supported with zero motion. DNR4 requires the rebuilt host.
The bridge's additive `dlss5nr_process_rgba8_v3` export converts directly between RGBA8 and
D3D12 half-float surfaces using bounded lookup tables. Original float ABI exports remain intact.
Rebuild the sibling bridge with `-O3` using its documented MinGW command as well.

Performance probes: `.venv/bin/python -m tests.probe_dlss_throughput --frames 180 --workers 2
--factor 1` measures steady-state rendering after warmup (one worker with temporal
stability; add `--independent-frames` for parallel rendering); `tests.probe_webrtc --live-settings`
measures actual decoded cadence and toggle time. The latter uses a 2048-packet test receiver
because aiortc's 128-packet default cannot hold a high-quality 1440p IDR.

Set `GAMESTREAM_DLSS_PROFILE=1` on the server to log input transfer, conversion, NGX, output
conversion, and output transfer timings for the first 16 frames. Profiling is off by default.

## Optional neural-first bridge

`dlss5nr_bridge.cpp` is a local adaptation of the sibling bridge (MIT, see
`BRIDGE_LICENSE`). Both pass orders use the bundled DLL; the environment switch selects the order.
`dlss.neural_before_upscale = true` selects `native/bin/dlss5nr_bridge.dll`
and sets `GAMESTREAM_NEURAL_FIRST=1` for each worker. The intermediate is
input-sized: feature 18 renders first, a UAV/resource transition makes its
output readable, then feature 1 upscales into the final target-sized surface.
At native size only feature 18 runs. The existing RGBA8 ABI is unchanged.

Rebuild the optional bridge with:

```bash
zig c++ -target x86_64-windows-gnu -shared -std=c++17 -O3 \
  -Wno-nullability-completeness native/dlss5nr_bridge.cpp \
  -o native/bin/dlss5nr_bridge.dll -ld3d12 -ldxgi -lole32
```

Probe it using `.venv/bin/python -m tests.probe_dlss_throughput --neural-first
--factor 2 --workers 1 --frames 6`.

## Caller shim

`caller_shim.cpp` and `bin/nvngx.dll_comfy.dll` are MIT-licensed upstream code;
see `BRIDGE_LICENSE`. Rebuild with:

```sh
x86_64-w64-mingw32-g++ -shared -std=c++17 -O2 -static-libgcc -static-libstdc++ \
  native/caller_shim.cpp -o native/bin/nvngx.dll_comfy.dll
```

Scry launches the same stream host directly on Windows and through a dedicated Proton
prefix on Linux. NVIDIA DLLs belong in the configured runtime directory, never Git.
