"""Stable bootstrap: keep unchanged across automatic updates."""
import importlib.util as _update_import
from pathlib import Path as _UpdatePath
_update_root = _UpdatePath(__file__).resolve().parent
_update_spec = _update_import.spec_from_file_location('_lingyun_update_boot', _update_root / 'lingyun_update_core.py')
_update_boot = _update_import.module_from_spec(_update_spec)
_update_spec.loader.exec_module(_update_boot)
_UPDATE_LEASE = _update_boot.startup(_update_root)
exec(compile(_update_boot.io(_update_root / 'lingyun_main.py').read_bytes(),
             str(_update_root / 'lingyun_main.py'), 'exec'), globals())
