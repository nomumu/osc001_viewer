"""画面の設定の保存(~/.config/osc001_viewer/settings.json)。いまはログの保存先だけ。"""

import json
import os

from .vertical import config_dir


def _path():
    return os.path.join(config_dir(), "settings.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(**kw):
    d = load()
    d.update(kw)
    os.makedirs(config_dir(), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())
    return d
