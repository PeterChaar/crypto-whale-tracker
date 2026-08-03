"""Reproduce the bug that used to lose paying customers, then prove it is gone.

Stands a fake Postgres in front of store.py that mimics the supabase-py call
shapes actually used, so the test exercises the real rehydrate and write-through
paths. The uniqueness guarantee itself is enforced by a primary key and is
verified separately against the live database.

    ./venv/bin/python test_redeploy.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "bot"))


class FakeTable:
    def __init__(self, rows, pk):
        self.rows, self.pk = rows, pk
        self._filter = None

    # writes
    def upsert(self, row, on_conflict=None):
        self.rows = [r for r in self.rows if r[self.pk] != row[self.pk]] + [row]
        return self

    def insert(self, row):
        if any(r[self.pk] == row[self.pk] for r in self.rows):
            raise Exception('duplicate key value violates unique constraint (23505)')
        self.rows.append(row)
        return self

    def delete(self):
        self._deleting = True
        return self

    def eq(self, col, val):
        if getattr(self, "_deleting", False):
            self.rows = [r for r in self.rows if r[col] != val]
            self._deleting = False
        return self

    # reads
    def select(self, *_):
        return self

    def order(self, *_, **__):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": list(self.rows)})()


class FakeDB:
    def __init__(self):
        self.t = {
            "whaleradar_subscribers": FakeTable([], "chat_id"),
            "whaleradar_pending": FakeTable([], "chat_id"),
            "whaleradar_processed_tx": FakeTable([], "tx_id"),
        }

    def table(self, name):
        return self.t[name]


def main() -> int:
    import store

    db = FakeDB()
    store.set_client(db)
    root = Path(__file__).parent
    files = [root / "subscribers.json", root / "pending_payments.json", root / "processed_txs.json"]
    backups = {f: (f.read_text() if f.exists() else None) for f in files}
    fails = []

    try:
        CUSTOMER, TX = 555000111, "abc123def456"

        # 1. A customer pays. The transfer is claimed, then Pro is granted.
        assert store.claim_tx(TX, CUSTOMER, 9.99) is True, "first claim should win"
        store.push_sub(CUSTOMER, {
            "is_pro": True, "username": "paying_customer",
            "alerts_today": 0, "last_reset": "2026-08-03",
            "pro_expires": "2026-09-02T00:00:00", "auth_token": "tok_abc",
        })
        store.save_subs({str(CUSTOMER): {"is_pro": True, "username": "paying_customer"}})

        # 2. Railway redeploys. The disk is new and empty.
        for f in files:
            f.unlink(missing_ok=True)

        if store.load_subs() != {}:
            fails.append("cache should be empty right after a wipe")

        # 3. The bot boots and rehydrates before answering anyone.
        store.rehydrate()
        subs = store.load_subs()

        if str(CUSTOMER) not in subs:
            fails.append("PAYING CUSTOMER LOST after redeploy")
        elif not subs[str(CUSTOMER)]["is_pro"]:
            fails.append("customer survived but lost Pro")
        if subs.get(str(CUSTOMER), {}).get("auth_token") != "tok_abc":
            fails.append("dashboard token did not survive")

        # 4. The poller sees the same transfer again in TronGrid's last 20.
        if store.claim_tx(TX, CUSTOMER, 9.99) is not False:
            fails.append("SAME TRANSFER CREDITED TWICE")

        # 5. A genuinely new transfer still goes through.
        if store.claim_tx("newtx999", 777000222, 9.99) is not True:
            fails.append("a new transfer was wrongly rejected")

        print(f"subscribers recovered : {list(subs)}")
        print(f"is_pro after redeploy : {subs.get(str(CUSTOMER), {}).get('is_pro')}")
        print(f"repeat transfer       : rejected")
        print()
        if fails:
            print("FAILED")
            for f in fails:
                print("  -", f)
            return 1
        print("PASS: a paid subscriber survives a redeploy, and no transfer credits twice")
        return 0
    finally:
        for f, content in backups.items():
            if content is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(content)


if __name__ == "__main__":
    raise SystemExit(main())
