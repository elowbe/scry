"""Live streaming presets, ordered from lowest processing/network load to detail."""
from dataclasses import replace
from .config import AppConfig

PRESETS = (
    ('Fastest', 720, .25, 'p1', 0),
    ('Fast', 900, .35, 'p1', 0),
    ('Balanced', 1080, .5, 'p2', 10),
    ('Detailed', 1440, .8, 'p3', 20),
    ('Maximum detail', 1440, 1., None, 30),
)
DEFAULT_QUALITY = 4


def quality_config(config: AppConfig, level: int) -> AppConfig:
    if type(level) is not int or not 0 <= level < len(PRESETS):
        raise ValueError('quality must be an integer from 0 to 4')
    _, height, bitrate_factor, preset, _ = PRESETS[level]
    stream = config.stream
    height = stream.height if level == 4 else min(stream.height, height)
    width = max(2, round(stream.width * height / stream.height / 2) * 2)
    bitrate = max(4, round(stream.bitrate_mbps * bitrate_factor)) if stream.bitrate_mbps else 0
    return replace(config, stream=replace(stream, width=width, height=height,
                   bitrate_mbps=bitrate, max_bitrate_mbps=(max(bitrate, round(stream.max_bitrate_mbps * bitrate_factor)) if stream.max_bitrate_mbps else 0),
                   encoder_preset=preset or stream.encoder_preset))


def quality_options(config: AppConfig) -> list[dict]:
    options = []
    for level, (name, _, _, _, jitter) in enumerate(PRESETS):
        stream = quality_config(config, level).stream
        options.append(dict(level=level, name=name, width=stream.width, height=stream.height,
                            fps=stream.fps, bitrate_mbps=stream.bitrate_mbps, jitter_ms=jitter))
    return options
