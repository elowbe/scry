"""Host capability discovery shared by setup, diagnostics and media."""
import os
import shutil
import subprocess
from functools import lru_cache


def system_python():
    return os.environ.get("SCRY_SYSTEM_PYTHON") or ("/usr/bin/python3" if os.path.isfile("/usr/bin/python3") else shutil.which("python3") or "python3")


@lru_cache(maxsize=1)
def auto_encoder():
    for encoder in ("h264_nvenc", "libx264"):
        try:
            result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=size=256x256:rate=1", "-frames:v", "1", "-c:v", encoder,
                "-f", "null", "-"], capture_output=True, timeout=15)
            if result.returncode == 0:
                return encoder
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise RuntimeError("FFmpeg cannot encode H.264. Run the Scry installer and host checks.")


def resolve_encoder(name):
    if name == "auto":
        return auto_encoder()
    if name not in {"h264_nvenc", "libx264"}:
        raise ValueError("stream.encoder must be auto, h264_nvenc or libx264")
    return name
