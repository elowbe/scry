"""Desktop status menu supervising a gracefully stopped server process."""
import json
import os
import signal
import ssl
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from .auth import AuthManager
from .certificates import ensure_certificate
from .config import PROJECT_ROOT


def run_tray(config):
    # Desktop imports stay lazy so headless serve never requires a tray/display.
    try:
        import pystray
        from PIL import Image, ImageEnhance
    except Exception as exc:
        raise RuntimeError('Tray unavailable. Use scry-server serve, or enable a desktop tray/AppIndicator: ' + str(exc)) from exc
    if not pystray.Icon.HAS_MENU:
        raise RuntimeError("A menu-capable AppIndicator tray is unavailable. Run the installer and enable your desktop tray, or use scry-server serve.")
    ensure_certificate(config)
    token = AuthManager(config.server.token_file).token
    base = f'https://localhost:{config.server.port}'
    process = None
    stopping = threading.Event()
    status = 'Starting'
    lock = threading.Lock()
    state = config.source.parent / '.state'
    state.mkdir(parents=True, exist_ok=True)
    log = (state / 'scry-tray.log').open('a', encoding='utf-8')
    bright = Image.open(PROJECT_ROOT / 'web/icon.png').convert('RGBA')
    dim = ImageEnhance.Brightness(bright).enhance(0.35)

    def open_web(icon=None, item=None):
        webbrowser.open(base + '/?token=' + token)

    def start(icon=None, item=None):
        nonlocal process, status
        with lock:
            if process is None or process.poll() is not None:
                # Never attach to/claim ownership of an unrelated listener.
                status = 'Starting'
                process = subprocess.Popen([sys.executable, '-m', 'gamestream', '--config', str(config.source), 'serve'],
                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)

    def stop(icon=None, item=None):
        nonlocal process, status
        with lock:
            if process is not None and process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
                    process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait()
            process = None
            status = 'Stopped'

    def quit_app(icon, item):
        stopping.set()
        stop()
        icon.stop()

    def monitor(icon):
        nonlocal status
        icon.visible = True
        start()
        opened = False
        while not stopping.wait(2):
            running = False
            with lock:
                child = process
            if child is not None and child.poll() is None:
                try:
                    with urllib.request.urlopen(base + '/healthz', context=ssl._create_unverified_context(), timeout=1) as response:
                        health = json.load(response)
                        running = response.status == 200 and health.get("service") == "scry-server" and health.get("pid") == child.pid
                    status = 'Running' if running else 'Starting'
                except OSError:
                    status = 'Starting'
            elif child is not None:
                status = f'Stopped (exit {child.returncode}) — see log'
            icon.title = 'Scry - Server · ' + status
            icon.icon = bright if running else dim
            icon.update_menu()
            if running and not opened:
                opened = True
                open_web()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: 'Scry - Server · ' + status, None, enabled=False),
        pystray.MenuItem('Open Scry Web', open_web, default=True),
        pystray.MenuItem('Start server', start),
        pystray.MenuItem('Stop server', stop),
        pystray.MenuItem('Open log', lambda icon, item: webbrowser.open((state / 'scry-tray.log').as_uri())),
        pystray.MenuItem('Quit', quit_app))
    icon = pystray.Icon('scry-server', dim, 'Scry - Server · Starting', menu)
    try:
        icon.run(setup=monitor)
    finally:
        stopping.set()
        stop()
        log.close()
