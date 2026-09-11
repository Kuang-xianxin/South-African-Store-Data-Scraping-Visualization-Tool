"""Install derived projections after the old GREEN listener has stopped."""
import argparse
import json
import socket
from pathlib import Path

from takealot_ops.erp.radar_code_version import materialized_code_fingerprint
from takealot_ops.erp.release_cache import copy_radar_cache


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Main listener is still active; do not replace its cache")
    root = Path.cwd().resolve()
    ready = json.loads(args.ready.read_text(encoding="utf-8"))
    if ready["namespace"] != materialized_code_fingerprint(root):
        raise RuntimeError("Source changed since preparation")
    expected = args.ready.resolve().parent / "radar-cache"
    if Path(ready["cache_directory"]).resolve() != expected:
        raise RuntimeError("Unexpected preparation directory")
    copied = copy_radar_cache(expected, root / "data" / "runtime-cache", ready["namespace"])
    if len(copied) != 2:
        raise RuntimeError("Both prepared radar partitions are required")
    print("Installed both complete prepared radar partitions")


if __name__ == "__main__":
    main()
