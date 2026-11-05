"""Send the monthly report by hand (on prod: docker exec cafe-manager-cafe-1 python3 scripts/monthly_report.py ...).

    python3 scripts/monthly_report.py                    # last month → everyone
    python3 scripts/monthly_report.py 2026-10            # given month → everyone
    python3 scripts/monthly_report.py 2026-10 --admin    # preview to admin only
    python3 scripts/monthly_report.py 2026-10 --print    # print, send nothing
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.monthly_report import build, send_monthly_report  # noqa: E402

logging.basicConfig(level=logging.INFO)
args = [a for a in sys.argv[1:] if not a.startswith('--')]
month = args[0] if args else None

if '--print' in sys.argv:
    numbers, summary, meta = build(month)
    print(numbers, '\n\n', summary or '(no AI summary)', '\n\n', meta)
else:
    print(send_monthly_report(month, admin_only='--admin' in sys.argv))
