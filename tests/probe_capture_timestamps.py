"""Reproduce capture timestamp poisoning with system GStreamer (no desktop/GPU).

PipeWire 1.0.x's keepalive rewrites a buffer still held downstream. Model one
absolute-clock timestamp escaping into the running-time stream, then normal
frames. The legacy videorate path stops; the byte-sampling path must keep going.
Run: /usr/bin/python3 -m tests.probe_capture_timestamps
"""
import time
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst
from gamestream.portal_capture import capture_pipeline


def exercise(description):
    pipeline = Gst.parse_launch(description)
    source = pipeline.get_by_name('desktop')
    source.set_property('caps', Gst.Caps.from_string(
        'video/x-raw,format=RGBA,width=64,height=64,framerate=0/1'))
    sink = pipeline.get_by_name('frames')
    pipeline.set_state(Gst.State.PLAYING)
    seen = []
    try:
        # Initial frames, one leaked absolute timestamp, then healthy frames.
        stamps = [0, Gst.SECOND // 60, 10 * Gst.SECOND]
        stamps += [i * Gst.SECOND // 60 for i in range(3, 15)]
        for marker, pts in enumerate(stamps):
            buffer = Gst.Buffer.new_wrapped(bytes([marker]) * (64 * 64 * 4))
            buffer.pts = buffer.dts = pts
            buffer.duration = Gst.SECOND // 60
            assert source.emit('push-buffer', buffer) == Gst.FlowReturn.OK
            time.sleep(.02)
            while True:
                sample = sink.emit('try-pull-sample', 0)
                if sample is None:
                    break
                seen.append(sample.get_buffer().extract_dup(0, 1)[0])
        return seen
    finally:
        pipeline.set_state(Gst.State.NULL)


def main():
    Gst.init(None)
    source = 'appsrc name=desktop is-live=true format=time ! '
    sink = 'appsink name=frames sync=false max-buffers=1 drop=true wait-on-eos=false'
    legacy = source + (
        'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
        'videoconvert ! videoscale ! videorate skip-to-first=true max-duplication-time=50000000 ! '
        'video/x-raw,format=RGBA,width=64,height=64,framerate=60/1 ! '
    ) + sink
    corrected = source + capture_pipeline(64, 64).split(' ! ', 1)[1]
    before, after = exercise(legacy), exercise(corrected)
    assert not any(marker >= 3 for marker in before), before
    assert after and after[-1] == 14, after
    print(f'PASS: legacy stops after markers {before}; corrected receives through marker {after[-1]}')


if __name__ == '__main__':
    main()
