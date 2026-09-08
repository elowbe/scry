"""System-Python helper: desktop portal -> PipeWire -> packed RGBA on stdout.

Kept out of the virtualenv so distro GObject/GStreamer bindings stay ABI compatible.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import select
import threading
import time
import uuid


def capture_pipeline(width, height):
    # PipeWire 1.0.x keepalive reuses last_buffer and rewrites its timestamps
    # while downstream may still own it. With a queue + videorate this can
    # introduce an absolute-clock PTS into the running-time timeline and stall
    # output. Consume only real samples; pace owned bytes outside GStreamer.
    return (
        'pipewiresrc name=desktop do-timestamp=true always-copy=true keepalive-time=0 ! '
        'videoconvert ! videoscale ! '
        f'video/x-raw,format=RGBA,width={width},height={height} ! '
        'appsink name=frames sync=false max-buffers=1 drop=true '
        'enable-last-sample=false wait-on-eos=false'
    )


def write_frame(fd, frame, stopped):
    """Finish each raw frame, but let shutdown interrupt pipe backpressure."""
    view = memoryview(frame)
    while view and not stopped.is_set():
        _, writable, _ = select.select([], [fd], [], .1)
        if not writable:
            continue
        try:
            count = os.write(fd, view)
        except BlockingIOError:
            continue
        if not count:
            raise BrokenPipeError('Video encoder closed its capture input')
        view = view[count:]


def pump_frames(pull_frame, fd, fps, stopped):
    """Sample the latest owned frame at a monotonic cadence, without backlog."""
    interval = 1 / fps
    deadline = time.monotonic()
    previous = None
    while not stopped.is_set():
        frame = pull_frame()
        if frame is not None:
            previous = frame
        if previous is not None:
            write_frame(fd, previous, stopped)
        # Skip missed slots after a slow encoder instead of bursting old frames.
        deadline = max(deadline + interval, time.monotonic())
        stopped.wait(max(0, deadline - time.monotonic()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--width', type=int, default=2560)
    parser.add_argument('--height', type=int, default=1440)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--method', choices=('portal', 'gnome'), default='portal')
    parser.add_argument('--monitor', default='')
    args = parser.parse_args()
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gio, GLib, Gst
    Gst.init(None)
    for name in ('pipewiresrc', 'videoconvert', 'videoscale', 'appsink'):
        if not Gst.ElementFactory.find(name):
            raise RuntimeError(f'Missing GStreamer {name}; run scripts/install-host.sh')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    destination = 'org.freedesktop.portal.Desktop'
    path = '/org/freedesktop/portal/desktop'
    interface = 'org.freedesktop.portal.ScreenCast'
    if args.method == 'gnome':
        from gnome_capture import check, start
        detail = check(bus, Gio, GLib)
    else:
        version = bus.call_sync(destination, path, 'org.freedesktop.DBus.Properties', 'Get',
                                GLib.Variant('(ss)', (interface, 'version')), None,
                                Gio.DBusCallFlags.NONE, 5000, None)
        detail = f'ScreenCast portal version {version.unpack()[0]}; PipeWire/GStreamer available'
    if args.check:
        print(detail)
        return

    session = None
    pipeline = None
    fd = None
    loop = GLib.MainLoop()
    failures = []
    stopped = threading.Event()
    writer = None

    def request(method, signature, positional, options):
        token = 'gs_' + uuid.uuid4().hex
        options['handle_token'] = GLib.Variant('s', token)
        sender = bus.get_unique_name()[1:].replace('.', '_')
        request_path = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
        response = []
        wait = GLib.MainLoop()
        def on_response(_bus, _sender, _path, _interface, _signal, parameters):
            response.append(parameters.unpack())
            wait.quit()
        subscription = bus.signal_subscribe(destination, 'org.freedesktop.portal.Request',
                                            'Response', request_path, None,
                                            Gio.DBusSignalFlags.NONE, on_response)
        timeout = GLib.timeout_add_seconds(110, lambda: (wait.quit(), False)[1])
        try:
            bus.call_sync(destination, path, interface, method,
                          GLib.Variant(signature, (*positional, options)), None,
                          Gio.DBusCallFlags.NONE, 10000, None)
            if not response:
                wait.run()
            if not response:
                bus.call_sync(destination, request_path, 'org.freedesktop.portal.Request',
                              'Close', None, None, Gio.DBusCallFlags.NONE, 2000, None)
                raise RuntimeError(f'{method}: screen sharing selection timed out on the host')
            code, results = response[0]
            if code:
                raise RuntimeError(f'{method}: screen sharing was cancelled or denied (response {code})')
            return results
        finally:
            if GLib.MainContext.default().find_source_by_id(timeout):
                GLib.source_remove(timeout)
            bus.signal_unsubscribe(subscription)

    try:
        if args.method == 'gnome':
            session, node, monitor = start(bus, Gio, GLib, args.monitor)
            destination = 'org.gnome.Mutter.ScreenCast'
            properties = {}
            print(f'GNOME unattended capture: {monitor}', file=sys.stderr, flush=True)
        else:
            session = request('CreateSession', '(a{sv})', (), {
                'session_handle_token': GLib.Variant('s', 'gs_' + uuid.uuid4().hex),
            })['session_handle']
            request('SelectSources', '(oa{sv})', (session,), {
                'types': GLib.Variant('u', 1), 'multiple': GLib.Variant('b', False),
                'cursor_mode': GLib.Variant('u', 2),
            })
            print('Select the game monitor in the host desktop screen-sharing dialog.', file=sys.stderr, flush=True)
            result = request('Start', '(osa{sv})', (session, ''), {})
            streams = result.get('streams', [])
            if not streams:
                raise RuntimeError('ScreenCast portal returned no monitor streams')
            node, properties = streams[0]
            reply, descriptors = bus.call_with_unix_fd_list_sync(
                destination, path, interface, 'OpenPipeWireRemote',
                GLib.Variant('(oa{sv})', (session, {})), GLib.VariantType.new('(h)'),
                Gio.DBusCallFlags.NONE, 10000, None, None)
            fd = descriptors.get(reply.unpack()[0])
        pipeline = Gst.parse_launch(capture_pipeline(args.width, args.height))
        source = pipeline.get_by_name('desktop')
        if fd is not None:
            source.set_property('fd', fd)
        if 'pipewire-serial' in properties and source.find_property('target-object'):
            source.set_property('target-object', str(properties['pipewire-serial']))
        else:
            source.set_property('path', str(node))
        def on_message(_bus, message):
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                failures.append(f'PipeWire capture failed: {error.message}; {debug}')
                loop.quit()
            elif message.type == Gst.MessageType.EOS:
                failures.append('PipeWire screen sharing ended')
                loop.quit()
        gst_bus = pipeline.get_bus()
        gst_bus.add_signal_watch()
        gst_bus.connect('message', on_message)
        def closed(*_args):
            failures.append('Host ended the screen-sharing session')
            loop.quit()
        session_interface = 'org.gnome.Mutter.ScreenCast.Session' if args.method == 'gnome' else 'org.freedesktop.portal.Session'
        bus.signal_subscribe(destination, session_interface, 'Closed', session,
                             None, Gio.DBusSignalFlags.NONE, closed)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (loop.quit(), False)[1])
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (loop.quit(), False)[1])
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('PipeWire pipeline could not enter PLAYING')
        sink = pipeline.get_by_name('frames')
        def pull_frame():
            sample = sink.emit('try-pull-sample', 0)
            if sample is None:
                return None
            caps = sample.get_caps().get_structure(0)
            if (caps.get_value('width'), caps.get_value('height')) != (args.width, args.height):
                raise RuntimeError('PipeWire returned unexpected frame dimensions')
            buffer = sample.get_buffer()
            # extract_dup owns its bytes. No sample or GstBuffer leaves here;
            # a blocked encoder cannot retain PipeWire's producer buffers.
            frame = buffer.extract_dup(0, buffer.get_size())
            # appsink does not request video-meta allocation, so negotiated
            # RGBA uses the default packed layout (four bytes per pixel).
            if len(frame) != args.width * args.height * 4:
                raise RuntimeError('PipeWire returned an unexpected RGBA frame size')
            return frame
        def feed():
            try:
                pump_frames(pull_frame, 1, args.fps, stopped)
            except Exception as exc:
                failures.append(f'Capture output failed: {exc}')
                GLib.idle_add(lambda: (loop.quit(), False)[1])
        os.set_blocking(1, False)
        writer = threading.Thread(target=feed, name='capture-output', daemon=True)
        writer.start()
        print(f'PipeWire monitor {node}: {args.width}x{args.height} at {args.fps} fps', file=sys.stderr, flush=True)
        loop.run()
        if failures:
            raise RuntimeError(failures[-1])
    finally:
        stopped.set()
        if writer:
            writer.join(timeout=1)
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
        if fd is not None:
            os.close(fd)
        if session:
            bus.call_sync(destination, session,
                          'org.gnome.Mutter.ScreenCast.Session' if args.method == 'gnome' else 'org.freedesktop.portal.Session',
                          'Stop' if args.method == 'gnome' else 'Close',
                          None, None, Gio.DBusCallFlags.NONE, 2000, None)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Wayland capture: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
