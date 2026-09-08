from pathlib import Path
import tempfile
import unittest

from gamestream.catalog import GameCatalog, discover_library_roots, parse_vdf_pairs
from gamestream.config import load_config


class CatalogTests(unittest.TestCase):
    def test_parse_vdf_pairs_handles_tabs_and_escapes(self):
        text = '"AppState"\n{\n  "appid"\t\t"42"\n  "name" "A \\"Game\\""\n}'
        self.assertEqual(dict(parse_vdf_pairs(text)), {"appid": "42", "name": 'A "Game"'})

    def test_discovers_external_library_and_filters_runtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            root = tmp_path / "Steam"
            external = tmp_path / "External"
            (root / "steamapps").mkdir(parents=True)
            (external / "steamapps/common/TestGame").mkdir(parents=True)
            (root / "steamapps/libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n"1"\n{{\n"path" "{external}"\n}}\n}}\n'
            )
            (external / "steamapps/appmanifest_42.acf").write_text(
                '"AppState"\n{\n"appid" "42"\n"name" "Test Game"\n'
                '"installdir" "TestGame"\n"SizeOnDisk" "123"\n}\n'
            )
            (external / "steamapps/appmanifest_1493710.acf").write_text(
                '"AppState"\n{\n"appid" "1493710"\n"name" "Proton Experimental"\n}\n'
            )
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[steam]\nroot = "{root}"\n')
            config = load_config(config_path)
            catalog = GameCatalog(config)
            games = catalog.all()
            self.assertEqual(discover_library_roots(root), [root.resolve(), external.resolve()])
            self.assertEqual(
                [(game.id, game.name, game.size_bytes) for game in games],
                [("desktop", "Desktop", 0), ("42", "Test Game", 123)],
            )
