"""
keirin.db(凍結済み: 〜2026/9/29) と keirin_2026q4.db(新しいデータ) を統合して
data/keirin_all.db を作る。解析・ダッシュボード・GBM学習用(ローカル専用、gitには入れない)。

使い方:
    python merge_db.py
"""
import shutil
import sqlite3
from pathlib import Path

DATA = Path(__file__).parent / "data"
BASE = DATA / "keirin.db"
EXTRA = [DATA / "keirin_2026q4.db"]   # 期間を増やしたらここに追加
OUT = DATA / "keirin_all.db"


def main():
    shutil.copyfile(BASE, OUT)
    conn = sqlite3.connect(OUT)
    for i, extra in enumerate(EXTRA):
        if not extra.exists():
            print("skip (not found):", extra)
            continue
        alias = f"x{i}"
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(extra),))
        tables = [r[0] for r in conn.execute(
            f"SELECT name FROM {alias}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM {alias}.{t}").fetchone()[0]
            conn.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM {alias}.{t}")
            print(f"{extra.name}: {t} {n}行を統合")
        conn.commit()
        conn.execute(f"DETACH DATABASE {alias}")
    for t in ["races", "entries", "results", "payouts"]:
        lo, hi = conn.execute(f"SELECT MIN(race_date), MAX(race_date) FROM {t}").fetchone()
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n}行 {lo}〜{hi}")
    conn.close()
    print("->", OUT)


if __name__ == "__main__":
    main()
