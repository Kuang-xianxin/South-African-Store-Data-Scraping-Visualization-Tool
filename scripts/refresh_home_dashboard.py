"""Read official finance and warehouse data without starting a sales or competitor batch."""
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from takealot_ops.api.client import TakealotClient
from takealot_ops.collectors.home import collect_home_finance, save_snapshot, warehouse_payload
from takealot_ops.settings import Settings, configured_stores
from takealot_ops.storage.migrations import create_engine_for_settings
from takealot_ops.storage.models import SellerHomeSnapshot
from takealot_ops.storage.repository import Repository
from takealot_ops.storage.store_context import store_scope


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures = 0
    for store in configured_stores(root):
        with store_scope(store.code):
            engine = create_engine_for_settings(Settings.from_env(root, store.code))
            # Add only this new table. No broad schema migration or existing data replacement.
            SellerHomeSnapshot.__table__.create(engine, checkfirst=True)
            client = TakealotClient(Settings.from_env(root, store.code), trust_env=False)
            try:
                with Session(engine) as session:
                    result = collect_home_finance(client, Repository(session), datetime.now(UTC))
                    print(store.code, "finance", result.status, result.counts, flush=True)
                    failures += not result.succeeded
                    items = list(client.iter_items("/offers", {"limit": 100, "expands": ["takealot_warehouse_stock"]}))
                    with session.begin():
                        save_snapshot(session, "warehouse", warehouse_payload(items), datetime.now(UTC))
                    print(store.code, "warehouse", len(items), flush=True)
            except Exception as error:
                print(store.code, "failed", type(error).__name__, flush=True)
                failures += 1
            finally:
                client.close()
                engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
