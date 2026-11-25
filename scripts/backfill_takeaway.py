"""Fill the takeaway/dine-in split for past days (new days are filled by every sales sync).

    docker exec cafe-manager-cafe-1 python3 scripts/backfill_takeaway.py 2026-05-25 2026-11-25

Reads orders from GoPOS and writes only the daily_takeaway table: sales and stock are not touched,
so it is safe to re-run. About 3 s per day.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import init_db  # noqa: E402
from app.gopos_api import backfill_takeaway  # noqa: E402

logging.basicConfig(level=logging.INFO)
if len(sys.argv) != 3:
    sys.exit(__doc__)
init_db()
print('days filled:', backfill_takeaway(sys.argv[1], sys.argv[2]))
