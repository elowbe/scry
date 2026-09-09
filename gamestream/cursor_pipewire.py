"""Run a normal isolated PipeWire metadata reader; no process injection."""
from pathlib import Path
import subprocess
import tempfile
import threading
import sys


class CursorReader:
    def __init__(self, node, fd=None):
        self.directory = tempfile.TemporaryDirectory(prefix='scry-cursor-')
        self.process = None
        try:
            executable = Path(self.directory.name) / 'cursor-reader'
            flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libpipewire-0.3'], text=True).split()
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', str(Path(__file__).with_suffix('.c')),
                            '-o', str(executable), *flags], check=True, timeout=30)
            self.process = subprocess.Popen([str(executable), str(node), str(fd if fd is not None else -1)],
                pass_fds=(fd,) if fd is not None else (), stdout=subprocess.PIPE)
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in iter(self.process.stdout.readline, b''):
            if len(line) <= 1200000:
                print('SCRY_CURSOR ' + line.decode().strip(), file=sys.stderr, flush=True)

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            if hasattr(self, 'thread'):
                self.thread.join(timeout=2)
            self.process.stdout.close()
        self.directory.cleanup()
