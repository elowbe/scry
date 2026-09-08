import pytest
from gamestream.gnome_capture import monitor_area


def display_state(scale=1.0, transform=0, layout=1):
    spec = ('HDMI-1', 'vendor', 'model', 'serial')
    mode = ('mode', 2560, 1440, 60., 1., [1., 2.], {'is-current': True})
    return (1, [(spec, [mode], {})], [(100, 200, scale, transform, True, [spec], {})], {'layout-mode': layout})


def test_primary_monitor_area():
    assert monitor_area(display_state()) == ('HDMI-1', (100, 200, 2560, 1440))


def test_scaled_and_rotated_monitor_area():
    assert monitor_area(display_state(scale=2, transform=1))[1] == (100, 200, 720, 1280)


def test_physical_layout_does_not_apply_logical_scale():
    assert monitor_area(display_state(scale=2, layout=2))[1] == (100, 200, 2560, 1440)


def test_missing_monitor_is_not_silently_replaced():
    with pytest.raises(RuntimeError, match='not active'):
        monitor_area(display_state(), 'DP-1')
