"""
keirin.db の内容をテーブルごとにCSVへ書き出す補助スクリプト。

使い方:
    python export_csv.py                          # 全期間・全テーブルをdata/csv/へ出力
    python export_csv.py --date 20260824           # 特定日のみ
    python export_csv.py --from 20260801 --to 20260831
"""

import argparse
import csv
import sqlite3
from pathlib import Path

TABLES = ["races", "entries", "results", "payouts"]


def export_table(conn, table, out_dir, date=None, date_from=None, date_to=None):
    query = f"SELECT * FROM {table}"
    params = []
    conditions = []
    if date:
        conditions.append("race_date = ?")
        params.append(date)
    elif date_from and date_to:
        conditions.append("race_date BETWEEN ? AND ?")
        params.extend([date_from, date_to])
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY race_date, venue_code, rno"

    cur = conn.execute(query, params)
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{date}" if date else (f"_{date_from}_{date_to}" if date_from else "")
    out_path = out_dir / f"{table}{suffix}.csv"

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    print(f"{table}: {len(rows)}行 -> {out_path}")


def main():
    parser = argparse.ArgumentParser(description="keirin.dbの内容をCSVに書き出す")
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "keirin.db"))
    parser.add_argument("--out", default=str(Path(__file__).parent / "data" / "csv"))
    parser.add_argument("--date", help="対象日 (YYYYMMDD) 単日のみ出力")
    parser.add_argument("--from", dest="date_from", help="開始日 (YYYYMMDD)")
    parser.add_argument("--to", dest="date_to", help="終了日 (YYYYMMDD)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    out_dir = Path(args.out)
    for table in TABLES:
        export_table(conn, table, out_dir, date=args.date, date_from=args.date_from, date_to=args.date_to)
    conn.close()


if __name__ == "__main__":
    main()
