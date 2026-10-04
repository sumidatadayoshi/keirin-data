"""開催一覧ページ(racecard/YYYY/MM/DD/)の構造診断用。結果は data/dump/ に保存(gitには入らない)。"""
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

import keirin_scraper as k

out = Path(__file__).parent / "data" / "dump"
out.mkdir(parents=True, exist_ok=True)
s = k.PoliteSession(interval_sec=1.5)
for d in ["20260930", "20261001", "20261002", "20261004"]:
    y, m, dd = d[:4], d[4:6], d[6:]
    url = f"https://keirin.kdreams.jp/racecard/{y}/{m}/{dd}/"
    t = s.get(url)
    if t is None:
        print(d, "取得失敗(None)")
        continue
    (out / f"daylist_{d}.html").write_text(t, encoding="utf-8")
    soup = BeautifulSoup(t, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    print(d, "len=", len(t), "title=", title,
          "raceinfo_table=", bool(soup.find("div", class_="raceinfo_table")),
          "racecard_links=", len(re.findall(r"/racecard/\d{14}/", t)),
          "racedetail_links=", len(re.findall(r"/racedetail/\d{16}/", t)))
print("保存先:", out)
