from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
import pytest
from gamestream.config import DlssConfig
from gamestream.dlss import WarmDlssCache
from gamestream.errors import PreflightError


class Pool:
    def __init__(self, config, width, height):
        self.input_width, self.input_height = width, height
        self.aborted = False
        self.workers = [SimpleNamespace(_worker=SimpleNamespace(poll=lambda: None))]
    def reset_history(self):
        self.history_reset = True
    def abort(self):
        self.aborted = True


def test_warm_pool_is_reused_but_never_lent_twice():
    cache = WarmDlssCache()
    config = DlssConfig()
    with patch('gamestream.dlss.ParallelDlssFilter', Pool):
        first = cache.acquire(config, 2560, 1440)
        with pytest.raises(PreflightError):
            cache.acquire(config, 2560, 1440)
        cache.release(config, first)
        assert cache.acquire(replace(config, target_fps=30), 2560, 1440) is first
        cache.release(config, first)
        second = cache.acquire(replace(config, nr_style='Cinematic'), 2560, 1440)
        assert second is not first and first.aborted
        cache.release(replace(config, nr_style='Cinematic'), second)
        cache.clear()
        assert second.aborted and cache.idle is None
