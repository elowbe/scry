"""Validated public controls shared by TOML, HTTPS and the player form."""
from dataclasses import replace
import math

# label, kind, allowed values/range. No filesystem or command controls here.
STREAM_FIELDS = {
    "width": ("Width", "int", [640, 7680]),
    "height": ("Height", "int", [360, 4320]),
    "fps": ("Target FPS", "int", [24, 240]),
    "bitrate_mbps": ("Target bitrate · Mbps (0 = quality driven)", "int", [0, 500]),
    "max_bitrate_mbps": ("Video bandwidth cap · Mbps (0 = unlimited)", "int", [0, 500]),
    "rate_control": ("Bitrate control", "choice", ["vbr", "cbr"]),
    "cq": ("Quality QP · lower is better", "int", [1, 40]),
    "encoder_preset": ("Encoder effort · p1 fastest, p7 highest", "choice", [f"p{i}" for i in range(1, 8)]),
    "vbv_ms": ("Bitrate buffer · ms", "int", [16, 500]),
    "keyframe_seconds": ("Keyframe interval · seconds", "int", [1, 5]),
    "spatial_aq": ("Adaptive quantization", "bool", []),
    "jitter_ms": ("Client video buffer · ms", "int", [0, 200]),
    "pacing_ms": ("Packet pacing · ms (0 = off)", "int", [0, 12]),
}
DLSS_FIELDS = {
    "temporal_stability": ("Enable stabilization · off restores original flickery rendering", "bool", []),
    "temporal_method": ("Stabilization method (only when enabled)", "choice", ["static_regions", "optical_flow"]),
    "neural_before_upscale": ("Neural rendering before DLSS upscale", "bool", []),
    "upscaling_factor": ("DLSS quality mode", "choice", [1.0, 1.5, 1.724, 2.0, 3.0]),
    "target_fps": ("DLSS target FPS", "int", [1, 240]),
    "workers": ("Concurrent render workers", "int", [1, 4]),
    "nr_style": ("Neural rendering style", "choice", ["Default", "Natural", "Cinematic"]),
    "nr_preset": ("Neural rendering preset", "choice", ["Default", "Preset #1", "Preset #2", "Preset #3"]),
    "nr_intensity": ("Neural intensity", "float", [0, 2]),
    "local_tone_strength": ("Local tone", "float", [0, 2]),
    "local_structure_strength": ("Local structure", "float", [0, 2]),
    "skin_structure_strength": ("Skin structure", "float", [-1, 2]),
    "automatic_mask": ("Automatic mask", "bool", []),
    "keep_warm": ("Keep renderer ready between toggles", "bool", []),
}


def apply_settings(config, changes):
    if not isinstance(changes, dict) or changes.keys() - {"stream", "dlss"}:
        raise ValueError("settings must contain only stream and dlss objects")
    result = config
    for section, fields in (("stream", STREAM_FIELDS), ("dlss", DLSS_FIELDS)):
        values = changes.get(section, {})
        if not isinstance(values, dict) or values.keys() - fields.keys():
            raise ValueError(f"Unknown {section} settings")
        for key, value in values.items():
            label, kind, allowed = fields[key]
            valid = False
            if kind == "bool":
                valid = type(value) is bool
            elif kind == "choice":
                valid = type(value) is not bool and value in allowed
            elif type(value) in (int, float):
                valid = math.isfinite(value) and allowed[0] <= value <= allowed[1]
                if kind == "int":
                    valid = valid and type(value) is int
            if not valid:
                raise ValueError(f"Invalid {label}: expected {allowed or 'boolean'}")
        result = replace(result, **{section: replace(getattr(result, section), **values)})
    s = result.stream
    if s.width % 2 or s.height % 2:
        raise ValueError("Stream dimensions must be even")
    if s.max_bitrate_mbps and s.bitrate_mbps > s.max_bitrate_mbps:
        raise ValueError("Target bitrate must not exceed the bandwidth cap")
    if s.rate_control == "cbr" and not s.bitrate_mbps:
        raise ValueError("Constant bitrate requires a nonzero target bitrate; use VBR for unlimited")
    if result.dlss.target_fps > s.fps:
        raise ValueError("DLSS target FPS must not exceed stream target FPS")
    return result


def public_settings(config):
    return {section: {key: getattr(getattr(config, section), key) for key in fields}
            for section, fields in (("stream", STREAM_FIELDS), ("dlss", DLSS_FIELDS))}


def settings_schema():
    return {"stream": STREAM_FIELDS, "dlss": DLSS_FIELDS}
