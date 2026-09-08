"""Install a launcher at the actual checkout path, including paths with spaces."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if os.name == 'nt':
    import subprocess
    # Launch via python.exe so CTRL_BREAK can gracefully stop the supervised child.
    programs = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs'
    programs.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    icon = root / '.state/scry.ico'
    Image.open(root / 'web/icon.png').save(icon, sizes=[(16,16),(32,32),(48,48),(64,64),(256,256)])
    def ps(value): return "'" + str(value).replace("'", "''") + "'"
    target = programs / 'Scry - Server.lnk'
    script = (f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps(target)});'
              f'$s.TargetPath={ps(sys.executable)};'
              f'$s.Arguments={ps("-m gamestream --config " + chr(34) + str(root / "config.toml") + chr(34) + " tray")};'
              f'$s.WorkingDirectory={ps(root)};$s.IconLocation={ps(icon)};$s.WindowStyle=7;$s.Save()')
    subprocess.run(['powershell.exe','-NoProfile','-Command',script],check=True)
else:
    applications = Path(os.environ.get('XDG_DATA_HOME', Path.home()/'.local/share'))/'applications'
    applications.mkdir(parents=True,exist_ok=True)
    def quoted(value):
        return '"' + str(value).replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$').replace('%','%%') + '"'
    (applications/'scry-server.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Scry - Server\nComment=Stream your games and desktop with Scry\n'
        f'Exec={quoted(sys.executable)} -m gamestream --config {quoted(root/"config.toml")} tray\n'
        f'Path={root}\nIcon={root/"web/icon.png"}\nTerminal=false\nCategories=Game;Network;\n')
print('Installed Scry - Server launcher.')
