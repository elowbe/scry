"""Repeatable local setup. Proprietary neural rendering DLL is user-supplied."""
import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from .auth import AuthManager
from .certificates import ensure_certificate
from .config import PROJECT_ROOT

# Explicit upstream release; downloaded assets stay in ignored .state.
DLSS_SR_URL = 'https://raw.githubusercontent.com/NVIDIA/DLSS/main/lib/Windows_x86_64/rel/nvngx_dlss.dll'
PROTON_RELEASE = 'GE-Proton10-15'


def download(url, target, *, max_bytes=2_000_000_000):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.part')
    print('Downloading ' + url, flush=True)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Scry-Server-Setup'})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as output:
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > max_bytes:
                    raise ValueError('Download exceeds expected size')
                output.write(block)
            expected = response.headers.get('Content-Length')
            if expected and total != int(expected):
                raise ValueError('Incomplete download; rerun setup')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def install_proton(state):
    from .dlss_support.launcher import find_proton
    try:
        return find_proton({})
    except RuntimeError:
        pass
    destination = state / 'tools' / PROTON_RELEASE
    if (destination / 'proton').is_file():
        return destination / 'proton'
    base = f'https://github.com/GloriousEggroll/proton-ge-custom/releases/download/{PROTON_RELEASE}'
    with tempfile.TemporaryDirectory(dir=state) as temporary:
        temporary = Path(temporary)
        archive = download(f'{base}/{PROTON_RELEASE}.tar.gz', temporary / 'proton.tar.gz')
        checksum = download(f'{base}/{PROTON_RELEASE}.sha512sum', temporary / 'checksum', max_bytes=4096)
        expected = checksum.read_text().split()[0].lower()
        with archive.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha512').hexdigest()
        if actual != expected:
            raise ValueError('Proton checksum did not match; installation stopped')
        unpacked = temporary / 'unpacked'
        with tarfile.open(archive) as bundle:
            bundle.extractall(unpacked, filter='data')
        candidate = unpacked / PROTON_RELEASE
        if not (candidate / 'proton').is_file():
            raise ValueError('Unexpected Proton archive layout')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), destination)
    return destination / 'proton'


def setup(config, dlss_dll=None):
    state = config.source.parent / '.state'
    state.mkdir(parents=True, exist_ok=True)
    ensure_certificate(config)
    token = AuthManager(config.server.token_file).token
    if dlss_dll:
        if platform.machine().lower() not in {'amd64', 'x86_64'}:
            raise ValueError('DLSS requires an x86-64 host and NVIDIA RTX GPU')
        source = Path(dlss_dll).expanduser().resolve()
        if source.name.lower() != 'nvngx_dlssnr.dll' or not source.is_file():
            raise ValueError('Choose your nvngx_dlssnr.dll file')
        with source.open('rb') as handle:
            if handle.read(2) != b'MZ':
                raise ValueError('The supplied DLL is not a Windows binary')
        runtime = config.dlss.runtime_dir or state / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        if source != (runtime / source.name).resolve():
            shutil.copyfile(source, runtime / 'nvngx_dlssnr.dll')
        if not (runtime / 'nvngx_dlss.dll').is_file():
            download(DLSS_SR_URL, runtime / 'nvngx_dlss.dll', max_bytes=150_000_000)
        with (runtime / 'nvngx_dlss.dll').open('rb') as handle:
            if handle.read(2) != b'MZ':
                (runtime / 'nvngx_dlss.dll').unlink()
                raise ValueError('Upstream download was not a DLL; rerun setup')
        support = {}
        if os.name != 'nt':
            support['proton_path'] = str(install_proton(state))
        (runtime.parent / 'dlss-support.json').write_text(json.dumps(support, indent=2) + '\n')
        from .dlss_support.paths import RuntimeLayout
        from .dlss_support.diagnostics import ensure_supported
        ensure_supported(RuntimeLayout(runtime))
        print('DLSS files installed. The first DLSS stream will validate GPU execution.')
    print('\nScry - Server is ready to start.')
    print(f'Open Scry Web: https://{config.server.public_host or "localhost"}:{config.server.port}/?token={token}')
    print('On another device use the host LAN/VPN address. Approve the local HTTPS certificate once.')
    print('Start the menu icon: scry-server --config "' + str(config.source) + '" tray')
    if not dlss_dll:
        print('Optional DLSS: rerun setup --dlss-dll "/path/to/nvngx_dlssnr.dll". Normal streaming needs no NVIDIA GPU.')
