# -*- coding: utf-8 -*-
"""把 reports/ 下散落的报告文件按日期归档到 reports/YYYYMMDD/ 子文件夹,便于追溯管理。
日期优先取文件名里的 YYYY-MM-DD;取不到则用文件修改时间。可反复运行(幂等)。

用法:python scripts/organize_reports.py
"""
import datetime as dt
import re
import shutil
from pathlib import Path

REPORTS = Path(__file__).parent.parent / "reports"


def main() -> None:
    if not REPORTS.exists():
        print("无 reports 目录"); return
    moved = 0
    for f in list(REPORTS.iterdir()):
        if f.is_dir():
            continue
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", f.name)
        if m:
            day = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        else:
            day = dt.date.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d")
        dest = REPORTS / day
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f.name
        if target.exists():
            target.unlink()
        shutil.move(str(f), str(target))
        moved += 1
        print(f"  {f.name} → {day}/")
    print(f"归档完成,移动 {moved} 个文件。")


if __name__ == "__main__":
    main()
