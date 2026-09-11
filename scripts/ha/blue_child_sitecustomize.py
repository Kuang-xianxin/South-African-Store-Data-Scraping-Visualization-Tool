"""Deployed only as BLUE child-guard/sitecustomize.py, never installed globally."""
import os
import sys


if os.environ.get("TAKEALOT_BLUE_CHILD_GUARD") == "1":
    try:
        import blue_runtime

        blue_runtime.prepare_environment()
    except BaseException:
        # Python normally ignores sitecustomize errors. BLUE must fail closed.
        sys.stderr.write("BLUE child isolation preflight failed; process refused\n")
        os._exit(78)
