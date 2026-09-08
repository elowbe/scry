"""Isolated WASAPI loopback helper, emitting stereo 48 kHz signed PCM."""
import argparse
import sys


def main():
    import av
    import numpy as np
    import pyaudiowpatch as audio
    from .dlss import _write_exact
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='@DEFAULT_MONITOR@')
    args = parser.parse_args()
    with audio.PyAudio() as engine:
        device = (engine.get_default_wasapi_loopback()
                  if args.source in {'@DEFAULT_MONITOR@', 'default', ''}
                  else engine.get_device_info_by_index(int(args.source)))
        channels = min(2, int(device['maxInputChannels']))
        rate = int(device['defaultSampleRate'])
        resampler = av.AudioResampler(format='s16', layout='stereo', rate=48000)
        with engine.open(format=audio.paInt16, channels=channels, rate=rate,
                         input=True, input_device_index=device['index'],
                         frames_per_buffer=max(1, rate // 50)) as stream:
            while True:
                raw = stream.read(max(1, rate // 50), exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.int16).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(samples, format='s16', layout='stereo' if channels == 2 else 'mono')
                frame.sample_rate = rate
                for converted in resampler.resample(frame):
                    _write_exact(sys.stdout.buffer, converted.to_ndarray().tobytes())


if __name__ == '__main__':
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
