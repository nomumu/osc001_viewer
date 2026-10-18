"""デバイス API(submodule libs/api の python/osc001.py)を読み込む。

osc001.py は stdlib だけの単一モジュールで、パッケージとしては配布していない。
pip でインストールすると osc001.py も一緒に入る(pyproject の py-modules)。
インストールせずに使うときはリポジトリの libs/api/python を、環境変数 OSC001_API_PATH があればそちらを探す。
"""

import os
import sys


def _candidates():
    env = os.environ.get("OSC001_API_PATH")
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.normpath(os.path.join(here, "..", "..", "libs", "api", "python"))


def _load():
    try:
        import osc001  # noqa: F401  (インストール済み・パス設定済みならそれを使う)
        return sys.modules["osc001"]
    except ImportError:
        pass
    for path in _candidates():
        if os.path.isfile(os.path.join(path, "osc001.py")):
            sys.path.insert(0, path)
            import osc001
            return osc001
    raise ImportError("osc001.py が見つからない(git submodule update --init か OSC001_API_PATH を設定)")


osc001 = _load()
