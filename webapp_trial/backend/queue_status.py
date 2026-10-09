"""Print queue depth and recent jobs — quick operational check."""

import json
import sys

import db


def main() -> int:
    db.init_db()
    print("queue depth:", json.dumps(db.queue_depth()))
    print()
    for job in db.list_jobs(25):
        mode = job["mode"] or "-"
        cost = (job.get("metrics") or {}).get("usage", {}).get("cost_usd")
        cost_str = f"${cost:.4f}" if cost is not None else "-"
        print(f"  {job['id']}  {job['status']:<18} {mode:<20} {cost_str:>9}  {job['filename'][:46]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
