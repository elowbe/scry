"""Discover installed Steam libraries without depending on Steam's network API."""

from __future__ import annotations

import re
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .config import AppConfig, UbisoftGameConfig

_PAIR = re.compile(r'^\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s+"([^"\\]*(?:\\.[^"\\]*)*)"')
_NON_GAME_NAMES = (
    "proton ",
    "steam linux runtime",
    "steamworks common redistributables",
    "steam controller configs",
)


def parse_vdf_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _PAIR.match(line)
        if match:
            pairs.append(
                (
                    match.group(1).replace(r'\"', '"').replace(r"\\", "\\"),
                    match.group(2).replace(r'\"', '"').replace(r"\\", "\\"),
                )
            )
    return pairs


def _manifest_fields(path: Path) -> dict[str, str]:
    try:
        return dict(parse_vdf_pairs(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return {}


def discover_steam_root(configured: Path | None = None) -> Path | None:
    candidates = [
        configured,
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                candidates.insert(1, Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except (OSError, ImportError):
            pass
        candidates.append(Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam")
    for candidate in candidates:
        if candidate and (candidate / "steamapps").is_dir():
            return candidate.resolve()
    return None


def discover_library_roots(steam_root: Path) -> list[Path]:
    result = [steam_root.resolve()]
    library_file = steam_root / "steamapps/libraryfolders.vdf"
    try:
        pairs = parse_vdf_pairs(library_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pairs = []
    for key, value in pairs:
        if key.casefold() != "path":
            continue
        candidate = Path(value).expanduser()
        if (candidate / "steamapps").is_dir() and candidate.resolve() not in result:
            result.append(candidate.resolve())
    return result


def find_artwork(steam_root: Path, app_id: str) -> Path | None:
    folder = steam_root / "appcache/librarycache" / app_id
    for name in ("library_600x900.jpg", "header.jpg", "library_hero.jpg"):
        candidate = folder / name
        if candidate.is_file():
            return candidate.resolve()
    legacy = steam_root / "appcache/librarycache"
    for pattern in (f"{app_id}_library_600x900.jpg", f"{app_id}_header.jpg"):
        candidate = legacy / pattern
        if candidate.is_file():
            return candidate.resolve()
    return None


@dataclass(frozen=True, slots=True)
class Game:
    id: str
    name: str
    provider: str
    install_dir: Path
    manifest: Path | None = None
    artwork: Path | None = None
    size_bytes: int = 0
    proton_ready: bool = False
    uplay_id: str = ""

    def public(self) -> dict:
        data = asdict(self)
        for key in ("install_dir", "manifest", "artwork"):
            value = data[key]
            data[key] = str(value) if value else None
        data["artwork_url"] = f"/api/games/{self.provider}/{self.id}/art"
        return data


class GameCatalog:
    def __init__(self, config: AppConfig):
        self.config = config
        self.steam_root = discover_steam_root(config.steam.root)
        self._games: dict[tuple[str, str], Game] = {}

    @staticmethod
    def _is_game(name: str) -> bool:
        lowered = name.casefold()
        return bool(name.strip()) and not any(lowered.startswith(item) for item in _NON_GAME_NAMES)

    def refresh(self) -> list[Game]:
        games: dict[tuple[str, str], Game] = {
            ("desktop", "desktop"): Game("desktop", "Desktop", "desktop", Path.home())
        }
        if self.steam_root:
            for library in discover_library_roots(self.steam_root):
                steamapps = library / "steamapps"
                for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
                    fields = _manifest_fields(manifest)
                    app_id = fields.get("appid", manifest.stem.removeprefix("appmanifest_"))
                    name = fields.get("name", "").strip()
                    if not app_id.isdecimal() or not self._is_game(name):
                        continue
                    install_dir = steamapps / "common" / fields.get("installdir", "")
                    try:
                        size = int(fields.get("SizeOnDisk", "0"))
                    except ValueError:
                        size = 0
                    compat = steamapps / "compatdata" / app_id / "pfx"
                    games[("steam", app_id)] = Game(
                        id=app_id,
                        name=name,
                        provider="steam",
                        install_dir=install_dir.resolve(),
                        manifest=manifest.resolve(),
                        artwork=find_artwork(self.steam_root, app_id),
                        size_bytes=size,
                        proton_ready=compat.is_dir() or sys.platform == "win32",
                    )
        for item in self.config.ubisoft_games:
            game = self._ubisoft_game(item)
            games[(game.provider, game.id)] = game
        self._games = games
        return sorted(games.values(), key=lambda game: game.name.casefold())

    @staticmethod
    def _ubisoft_game(item: UbisoftGameConfig) -> Game:
        return Game(
            id=item.id,
            name=item.name,
            provider="ubisoft",
            install_dir=item.executable.parent,
            artwork=item.artwork,
            proton_ready=(item.compat_data / "pfx").is_dir(),
            uplay_id=item.uplay_id,
        )

    def all(self) -> list[Game]:
        return self.refresh()

    def get(self, provider: str, game_id: str) -> Game:
        if not self._games:
            self.refresh()
        try:
            return self._games[(provider, game_id)]
        except KeyError as exc:
            raise KeyError(f"No installed {provider} game with id {game_id}") from exc

    def public(self) -> list[dict]:
        return [game.public() for game in self.refresh()]

    def __iter__(self) -> Iterable[Game]:
        return iter(self.all())

