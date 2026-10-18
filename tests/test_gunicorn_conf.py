"""gunicorn.conf.py, read the way gunicorn reads it as the broker starts.

gunicorn sets each name in the file that is one of its settings, and checks each value: a hook's arity, through
inspect.signature, which on Python 3.14 evaluates the hook's annotations. A value it rejects stops the broker
starting at all ("Invalid value for post_worker_init"), and the tests that call a hook directly don't see that.
"""

import runpy
from pathlib import Path

from gunicorn.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_gunicorn_accepts_every_setting_in_the_config_file():
    config = Config()
    loaded = runpy.run_path(str(ROOT / "gunicorn.conf.py"))
    settings = {name: value for name, value in loaded.items() if name in config.settings}

    assert "post_worker_init" in settings
    for name, value in settings.items():
        config.set(name, value)  # raises as gunicorn's own start does, for a value it won't take
