"""Optional compilation with caches confined to the active runtime directory."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def numeric_cache_directory() -> Path:
    """Resolve an operational cache without writing to delivered model sources."""
    value = os.environ.get("XAJ_RUNTIME_DIR")
    if value:
        runtime = Path(value).resolve()
    else:
        try:
            from platform_config import SETTINGS
        except ImportError:
            runtime = Path(tempfile.gettempdir()) / "xaj-model-runtime"
        else:
            runtime = Path(SETTINGS["workspace"]).resolve()
    model_root = Path(__file__).resolve().parents[3]
    if runtime == model_root or model_root in runtime.parents:
        raise ValueError("numeric cache must stay outside the model source directory")
    return runtime / "numeric-cache"


def optional_njit(function):
    """Keep the same Python kernel when Numba is absent or its cache is unwritable."""
    try:
        import numba
    except ImportError:
        return function
    directory = numeric_cache_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return function
    # Numba's default __pycache__ locator would write into vendor. Set the
    # explicit runtime locator before creating this function's dispatcher.
    numba.config.CACHE_DIR = str(directory)
    return numba.njit(cache=True)(function)
