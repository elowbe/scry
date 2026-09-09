"""GNOME's native remote-desktop capture API, for unattended local hosting.

Mutter's API is desktop-specific; the portable portal backend remains available.
"""
from __future__ import annotations

DESTINATION = 'org.gnome.Mutter.ScreenCast'
PATH = '/org/gnome/Mutter/ScreenCast'


def check(bus, Gio, GLib):
    reply = bus.call_sync(DESTINATION, PATH, 'org.freedesktop.DBus.Properties', 'Get',
                          GLib.Variant('(ss)', (DESTINATION, 'Version')), None,
                          Gio.DBusCallFlags.NONE, 5000, None)
    return f'GNOME native ScreenCast version {reply.unpack()[0]}; unattended PipeWire capture'


def monitor_area(state, connector=''):
    """Return the selected monitor rectangle in Mutter logical coordinates."""
    monitors, logical = state[1], state[2]
    if not logical:
        raise RuntimeError('GNOME has no active monitor to capture')
    if connector:
        target = next((item for item in logical if any(spec[0] == connector for spec in item[5])), None)
        if target is None:
            raise RuntimeError(f'Configured capture monitor {connector!r} is not active')
    else:
        target = next((item for item in logical if item[4]), logical[0])
        connector = target[5][0][0]
    physical = next((item for item in monitors if item[0][0] == connector), None)
    mode = next((mode for mode in physical[1] if mode[6].get('is-current')), None) if physical else None
    if mode is None:
        raise RuntimeError(f'Cannot determine active mode for {connector}')
    width, height = mode[1:3]
    if target[3] in (1, 3, 5, 7):
        width, height = height, width
    # Layout mode 1 is logical (scaled); 2 is physical (X11-compatible).
    scale = target[2] if state[3].get('layout-mode', 1) == 1 else 1.0
    return connector, (target[0], target[1], round(width / scale), round(height / scale))


def start(bus, Gio, GLib, monitor='', *, cursor_only=False):
    def call(path, interface, method, signature=None, args=()):
        return bus.call_sync(DESTINATION, path, interface, method,
                             GLib.Variant(signature, args) if signature else None,
                             None, Gio.DBusCallFlags.NONE, 10000, None)
    state = bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                          'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None,
                          Gio.DBusCallFlags.NONE, 5000, None).unpack()
    monitor, area = monitor_area(state, monitor)
    session = call(PATH, DESTINATION, 'CreateSession', '(a{sv})', ({},)).unpack()[0]
    try:
        # Mutter 46 RecordMonitor only handles pre-paint direct scanout for
        # DMA-BUF consumers. Our CPU RGBA path can otherwise repeat one frame
        # forever while a fullscreen game bypasses normal compositor painting.
        # RecordArea schedules capture on scanout even for CPU buffers.
        if cursor_only:
            # Cursor metadata uses the standard monitor stream, separately from
            # the existing RecordArea video path, keeping their buffer
            # negotiation and lifetimes independent.
            stream = call(session, DESTINATION + '.Session', 'RecordMonitor', '(sa{sv})',
                          (monitor, {'cursor-mode': GLib.Variant('u', 2)})).unpack()[0]
        else:
            stream = call(session, DESTINATION + '.Session', 'RecordArea', '(iiiia{sv})',
                          (*area, {'cursor-mode': GLib.Variant('u', 0)})).unpack()[0]
        nodes = []
        loop = GLib.MainLoop()
        def added(_bus, _sender, _path, _iface, _signal, parameters):
            nodes.append(parameters.unpack()[0])
            loop.quit()
        subscription = bus.signal_subscribe(DESTINATION, DESTINATION + '.Stream',
                                            'PipeWireStreamAdded', stream, None,
                                            Gio.DBusSignalFlags.NONE, added)
        timer = GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
        try:
            call(session, DESTINATION + '.Session', 'Start')
            if not nodes:
                loop.run()
            if not nodes:
                raise RuntimeError('GNOME did not create a PipeWire stream within 10 seconds')
        finally:
            bus.signal_unsubscribe(subscription)
            if GLib.MainContext.default().find_source_by_id(timer):
                GLib.source_remove(timer)
        return session, nodes[0], f"{monitor} area={area}"
    except Exception:
        call(session, DESTINATION + '.Session', 'Stop')
        raise
