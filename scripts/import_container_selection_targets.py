"""Import the versioned container-selection candidates into competitor radar."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from takealot_ops.container_selection import import_container_selection_targets
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--actor-username",
        default="codex-container-selection",
        help="Traceable system actor stored in competitor_target_audits.",
    )
    parser.add_argument(
        "--actor-display-name",
        default="Codex 配柜选品导入",
        help="Human-readable audit actor.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    settings = DashboardSettings.from_env(root)

    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        result = import_container_selection_targets(
            root,
            engine,
            actor_username=args.actor_username,
            actor_display_name=args.actor_display_name,
        )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "selection_batch_id": result.batch_id,
                "configured_count": result.configured_count,
                "added_count": result.added_count,
                "reactivated_count": result.reactivated_count,
                "existing_count": result.existing_count,
                "new_plids": [plid for plid, _ in result.new_targets],
                "collection_pickup": (
                    "持续轮巡会按既有每完成25项的目标重扫机制纳入活跃目标"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
