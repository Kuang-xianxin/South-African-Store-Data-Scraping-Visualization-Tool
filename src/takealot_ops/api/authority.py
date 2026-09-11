"""Optional machine-wide Seller admission shared by the two ERP environments."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from typing import Any


def install_local_authority(client_type: Any) -> None:
    if sys.platform != "win32":
        return
    root = Path("D:/TakealotBlue")
    if not (root / "state/seller-api-enabled.json").exists():
        return
    # No fallback if an enabled deployment is incomplete. The shared hook only
    # controls HTTP admission; it does not load BLUE application/DB settings.
    expected = (root / "blue_seller_runtime.py").resolve()
    if not expected.is_file():
        raise RuntimeError("Seller API authority enabled but runtime missing")
    if str(root) not in sys.path:
        sys.path.append(str(root))
    runtime = importlib.import_module("blue_seller_runtime")
    module_path = runtime.__file__
    if not isinstance(module_path, str) or Path(module_path).resolve() != expected:
        raise RuntimeError("Seller API authority loaded from an unexpected path")
    runtime.install(client_type)
