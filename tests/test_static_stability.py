import numpy as np
import pytest

from gamestream.temporal import StaticRegionStabilizer
from gamestream.config import AppConfig, DlssConfig, load_config
from gamestream.settings import apply_settings


def rgba(value=100, size=96):
    frame = np.full((size, size, 4), value, np.uint8)
    frame[..., 3] = 255
    return frame


def test_static_flicker_is_reduced_and_influence_is_bounded():
    filter = StaticRegionStabilizer()
    source = rgba()
    values = []
    for i in range(20):
        current = rgba(100 if i % 2 else 108)
        result = filter.process(source, current)
        values.append(int(result[48, 48, 0]))
        assert np.abs(result.astype(int) - current.astype(int)).max() <= 8
    assert max(values[8:]) - min(values[8:]) < 8
    strong_change = rgba(220)
    assert filter.process(source, strong_change)[48, 48, 0] >= 212


@pytest.mark.parametrize('kind', ['pan', 'cut', 'one_level_change'])
def test_global_changes_never_reuse_old_pixels(kind):
    rng = np.random.default_rng(42)
    source = rng.integers(0, 200, (96, 96, 4), np.uint8)
    source[..., 3] = 255
    filter = StaticRegionStabilizer()
    for _ in range(4): filter.process(source, rgba(10))
    if kind == 'pan': changed = np.roll(source, (3, -2), axis=(0, 1))
    elif kind == 'cut': changed = rgba(240)
    else:
        changed = source.copy()
        changed[..., :3] += 1
    current = rgba(180)
    np.testing.assert_array_equal(filter.process(changed, current), current)


def test_small_moving_object_and_disocclusion_have_no_trail_at_2x():
    filter = StaticRegionStabilizer()
    before = rgba(100)
    before[30:40, 30:40, :3] = 220
    for _ in range(4): filter.process(before, rgba(10, 192))
    after = rgba(100)
    after[30:40, 40:50, :3] = 220
    current = rgba(120, 192)
    result = filter.process(after, current)
    # Both old and new object footprints and their surrounding margin are fresh.
    np.testing.assert_array_equal(result[44:96, 44:116], current[44:96, 44:116])
    assert result[140, 140, 0] < current[140, 140, 0]  # Static background still smoothed.


def test_warm_reuse_resets_output_history():
    filter = StaticRegionStabilizer()
    source = rgba()
    for _ in range(4): filter.process(source, rgba(10))
    filter.reset()
    current = rgba(180)
    np.testing.assert_array_equal(filter.process(source, current), current)


def test_original_is_default_and_both_stabilizers_are_explicit(tmp_path):
    assert not DlssConfig().temporal_stability
    assert not DlssConfig().neural_before_upscale
    cfg = AppConfig(source=tmp_path / 'config.toml')
    for method in ('static_regions', 'optical_flow'):
        enabled = apply_settings(cfg, {'dlss': {'temporal_stability': True, 'temporal_method': method}})
        assert enabled.dlss.temporal_method == method
        restored = apply_settings(enabled, {'dlss': {'temporal_stability': False, 'neural_before_upscale': False}})
        assert not restored.dlss.temporal_stability and not restored.dlss.neural_before_upscale
    with pytest.raises(ValueError): apply_settings(cfg, {'dlss': {'temporal_method': 'unknown'}})


def test_old_saved_boolean_does_not_enable_new_algorithm(tmp_path):
    import json
    from gamestream.server import SessionManager
    state = tmp_path / '.state'
    state.mkdir()
    (state / 'player-settings.json').write_text(json.dumps({'quality': 4, 'settings': {'dlss': {'temporal_stability': True}}}))
    manager = SessionManager(AppConfig(source=tmp_path / 'config.toml'))
    assert not manager.effective_config().dlss.temporal_stability
