"""
競輪データを楽天Kドリームス(https://keirin.kdreams.jp)から取得し、SQLiteに保存する。

個人の私的利用を目的としたデータ収集スクリプト。
サイトへの負荷を避けるため、リクエスト間隔を空けて順番にアクセスする。

--------------------------------------------------------------------------
このスクリプトについて
--------------------------------------------------------------------------
実際にサイトから保存したレース詳細ページ(出走表・結果・払戻金)のHTMLを見て、
以下の構造をもとに実装している:

  - 出走表: <table class="racecard_table"> (ページ内に複数あるタブのうち
    "none"クラスが付いていない=デフォルト表示の1つが本体。各選手行は
    <tr class="n1">...<tr class="n7"> のように車番でクラス付けされている)
  - 結果:   <table class="result_table">
  - 払戻金: <table class="refund_table"> (「2枠連/2車連/3連勝/ワイド」の
    4グループを「複」「単」の2行にまとめた、rowspan/colspanを多用する
    レイアウト。ワイドのみ複/単の区別がなく1グループで3組番)
  - レース見出し: <p class="raceinfo_contents-title"> 内の
    <span class="icon_grade">(グレード) と <span class="text">(開催名)、
    <h2 class="title"><span class="status">(このレースの級班)、
    <p class="weather">(天候・風速)、<h1 class="raceinfo_headline">(場名)

  URL構造 (岐阜競輪 2026/08/26 1R の実例で確認済み):
    レース詳細: https://keirin.kdreams.jp/gifu/racedetail/4320260824030001/
    日程一覧:   https://keirin.kdreams.jp/gifu/racecard/43202608240300/
  IDは 場コード(2桁)+開催初日(YYYYMMDD,8桁)+開催日次(2桁,初日=01)+レース番号
  (4桁,0001〜0012)の16桁。日程一覧IDは末尾が"00"の14桁(=レース番号部分なし)。

  - 開催一覧: <div class="raceinfo_table"><table> の各行に場名(td)と、
    そのレースの実際のracedetail URLを列挙した <ul class="raceinfo_table-list">
    がある。レース数を推測する必要がなく、そこに並んでいるレースだけを
    取得すればよい(get_active_venues)。

  - ライン予想(並び予想): <dl class="racecard_footer-contents"><dt>並び予想</dt>
    <dd><div class="line_position"> に、<span class="icon_p"> が隊列順に並ぶ。
    通常のicon_pは車番と役割(先行/追込/自在/押え先など)の2つの内側<span>を持つ。
    <span class="icon_p space">(中身が空)がライン同士の区切り。先頭付近の
    「←」だけのicon_p(p000)は凡例的な矢印で選手データではないため無視する。
    (岐阜1R/2Rの実データで確認済み。ライン構成そのものが持つ情報として、
    競輪では「どの選手が同じラインで、誰が先頭(先行)/番手(追込)/自在か」が
    最重要の予想材料の1つ)。

検証には岐阜競輪(2026/08/26, F1 A級一般, 7車立て)の1R・2R(共に決着済み)と、
同日の開催一覧ページの実HTMLを使った。したがって以下はまだ未検証で、
実データがずれる可能性がある:
  - 9車立てなど出走数が異なるレースでの枠番(rowspanの挙動)
  - ガールズケイリンのページでの icon_girls の実際の見え方
  - 事故(落車・失格等)があったレースの結果表の表記
  - 開催一覧ページで「複数日開催の初日/2日目/最終日」以外のパターン
    (例:1日開催のみのレース)でのHTML構造の違い

--dump-html で任意のレースの生HTMLを保存できるので、上記が疑わしい場合は
まずそれで実データを確認してから調整するとよい。
    python keirin_scraper.py --dump-html --venue-slug gifu --day-id 43202608240300 --rno 1

使い方:
    python keirin_scraper.py                 # 前日分を取得
    python keirin_scraper.py --date 20260824  # 日付を指定して取得
    python keirin_scraper.py --db data\\keirin.db --interval 1.5
"""

import argparse
import logging
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")

BASE_URL = "https://keirin.kdreams.jp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# 開催日全体の結果取得率がこれを下回ると scrape_log の status を
# 'partial' にし、main の終了コードを1にする。
RESULTS_COMPLETENESS_THRESHOLD = 0.9

logger = logging.getLogger("keirin_scraper")


# ---------------------------------------------------------------------------
# 汎用ユーティリティ
# ---------------------------------------------------------------------------

def nfkc(s):
    if s is None:
        return None
    return unicodedata.normalize("NFKC", s).strip()


def to_int(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None


def to_float(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group()) if m else None


class PoliteSession:
    """アクセス間隔を必ず空けるrequests.Sessionのラッパー(boatrace_scraper.pyと同型)。"""

    def __init__(self, interval_sec=1.5, timeout=15, max_retries=3):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.interval_sec = interval_sec
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get(self, url):
        wait = self.interval_sec - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                self._last_request_at = time.time()
                if resp.status_code == 200:
                    # apparent_encoding(自動判定)は短い/紛らわしいバイト列で
                    # 誤判定することがあり、文字化けの原因になる(実際に確認済み)。
                    # このサイトは常にUTF-8で配信されるため固定する
                    # (boatrace_scraper.pyと同じ方針)。
                    resp.encoding = "utf-8"
                    return resp.text
                if resp.status_code == 404:
                    return None
                logger.warning("HTTP %s for %s (attempt %d)", resp.status_code, url, attempt)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Request failed for %s (attempt %d): %s", url, attempt, exc)
            time.sleep(2 * attempt)
        if last_exc:
            logger.error("Giving up on %s: %s", url, last_exc)
        else:
            logger.error("Giving up on %s: non-200 responses only", url)
        return None


# ---------------------------------------------------------------------------
# 開催中の場・日程の発見
# ---------------------------------------------------------------------------

# https://keirin.kdreams.jp/racecard/YYYY/MM/DD/ の実HTMLで確認済みの構造:
# <div class="raceinfo_table"><table><tr><td>場名(例:青森競輪)</td>...
#   <td><ul class="raceinfo_table-list">
#     <li><a href=".../racecard/{14桁day_id}/">一覧</a></li>
#     <li><a href=".../racedetail/{16桁race_id}/">1R</a></li> ... (実際に開催される
#     レース数の分だけ、"NR"というリンクテキストで並ぶ)
#   </ul></td></tr>...</table></div>
# レース番号だけでなく実際のレース詳細URLそのものがここに列挙されているため、
# レース数を推測したりURLを組み立てたりする必要がない。
RACECARD_LINK_RE = re.compile(r"/([a-z0-9\-]+)/racecard/(\d{14})/")
RACEDETAIL_LINK_RE = re.compile(r"/([a-z0-9\-]+)/racedetail/(\d{16})/")
RNO_TEXT_RE = re.compile(r"^(\d{1,2})R$")


def get_active_venues(session, date_str):
    """指定日(YYYYMMDD)に開催している場一覧を返す。各要素は
    {"slug", "day_id", "venue_name", "races": [(rno, url), ...]} のdict。
    races はそのページに実際に列挙されていたレースのみ(=その日の本当の
    レース数)なので、MAX_RACES_PER_VENUEのような推測は不要。"""
    y, m, d = date_str[0:4], date_str[4:6], date_str[6:8]
    url = f"{BASE_URL}/racecard/{y}/{m}/{d}/"
    text = session.get(url)
    if text is None:
        return []

    soup = BeautifulSoup(text, "lxml")
    table_div = soup.find("div", class_="raceinfo_table")
    if table_div is None:
        logger.warning("開催一覧ページの構造が想定と異なります(div.raceinfo_tableが見つからない)。")
        return []

    venues = []
    for tr in table_div.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue  # 見出し行(th)
        venue_name_raw = nfkc(tds[0].get_text(strip=True)) or ""
        venue_name = re.sub(r"競輪$", "", venue_name_raw).strip() or venue_name_raw

        ul = tr.find("ul", class_="raceinfo_table-list")
        if ul is None:
            continue

        slug, day_id = None, None
        races = []
        for a in ul.find_all("a"):
            href = a.get("href") or ""
            text_ = nfkc(a.get_text(strip=True)) or ""

            m_card = RACECARD_LINK_RE.search(href)
            if m_card:
                slug, day_id = m_card.group(1), m_card.group(2)
                continue

            m_rno = RNO_TEXT_RE.match(text_)
            m_detail = RACEDETAIL_LINK_RE.search(href)
            if m_rno and m_detail:
                slug = slug or m_detail.group(1)
                races.append((int(m_rno.group(1)), href))

        if slug and races:
            races.sort(key=lambda x: x[0])
            venues.append({"slug": slug, "day_id": day_id, "venue_name": venue_name, "races": races})

    return venues


def race_url(slug, day_id, rno):
    """day_id(14桁, 末尾'00')から特定レースのURLを組み立てる。
    レース番号部分は4桁ゼロ埋め(0001〜0012)。"""
    prefix = day_id[:-2]  # 場コード+開催初日+開催日次 (12桁)
    return f"{BASE_URL}/{slug}/racedetail/{prefix}{rno:04d}/"


# ---------------------------------------------------------------------------
# 表のrowspan/colspanを展開して2Dグリッドにするヘルパー
# (payoutsのrefund_tableのように、複数グループを2行にまとめた表で使う)
# ---------------------------------------------------------------------------

def expand_grid(table):
    grid = []
    carry = {}  # col_index -> [remaining_rows, cell]
    for tr in table.find_all("tr", recursive=True):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        row = []
        col = 0
        idx = 0
        while idx < len(cells) or col in carry:
            if col in carry:
                remaining, cell = carry[col]
                row.append(cell)
                if remaining <= 1:
                    del carry[col]
                else:
                    carry[col] = [remaining - 1, cell]
                col += 1
                continue
            cell = cells[idx]
            idx += 1
            colspan = to_int(cell.get("colspan", "1")) or 1
            rowspan = to_int(cell.get("rowspan", "1")) or 1
            for _ in range(colspan):
                row.append(cell)
                if rowspan > 1:
                    carry[col] = [rowspan - 1, cell]
                col += 1
        grid.append(row)
    return grid


# ---------------------------------------------------------------------------
# レース見出し情報 (タイトル・グレード・種別・天候・場名)
# ---------------------------------------------------------------------------

GRADE_TOKENS = ("GP", "SG", "G1", "G2", "G3", "F1", "F2", "L1", "ガールズ")


def parse_race_header(soup):
    info = {
        "venue_name": None, "title": None, "race_type": None, "grade": None,
        "is_girls": False, "weather": None, "wind_speed": None, "kimarite": None,
    }

    venue_h1 = soup.find("h1", class_="raceinfo_headline")
    if venue_h1:
        venue_full = nfkc(venue_h1.get_text(strip=True)) or ""
        cleaned = re.sub(r"\s*レース詳細$", "", venue_full)
        cleaned = re.sub(r"競輪$", "", cleaned).strip()
        info["venue_name"] = cleaned or venue_full or None

    title_p = soup.find("p", class_="raceinfo_contents-title")
    if title_p:
        grade_span = title_p.find("span", class_="icon_grade")
        text_span = title_p.find("span", class_="text")
        girls_span = title_p.find("span", class_="icon_girls")
        if grade_span:
            info["grade"] = nfkc(grade_span.get_text(strip=True))
        if text_span:
            info["title"] = nfkc(text_span.get_text(strip=True))
        # icon_girls の実際の非ガールズ時の見え方(class差分など)は未検証。
        # タイトル・グレードのテキストに「ガールズ」を含む場合を主判定とする。
        if girls_span and girls_span.get("class") and len(girls_span.get("class")) > 1:
            info["is_girls"] = True

    if info.get("grade") == "ガールズ" or "ガールズ" in (info.get("title") or ""):
        info["is_girls"] = True

    h2_title = soup.find("h2", class_="title")
    if h2_title:
        status_span = h2_title.find("span", class_="status")
        if status_span:
            info["race_type"] = nfkc(status_span.get_text(strip=True))

    weather_p = soup.find("p", class_="weather")
    if weather_p:
        weather_text = nfkc(weather_p.get_text(strip=True)) or ""
        m = re.search(r"天候\s*([^\s/]+)\s*/\s*風速\s*([\d.]+)\s*m", weather_text)
        if m:
            info["weather"] = m.group(1)
            info["wind_speed"] = to_float(m.group(2))

    return info


# ---------------------------------------------------------------------------
# 出走表 (table.racecard_table, class="racecard_table"かつ"none"を含まないもの)
# ---------------------------------------------------------------------------

def find_entries_table(soup):
    for table in soup.find_all("table", class_="racecard_table"):
        classes = table.get("class") or []
        if "none" not in classes:
            return table
    return None


def parse_entries(soup):
    table = find_entries_table(soup)
    if table is None:
        return []

    entries = []
    last_waku = None
    for tr in table.find_all("tr", class_=re.compile(r"^n\d+$")):
        tds = tr.find_all("td")
        rider_idx = next(
            (i for i, td in enumerate(tds) if td.get("class") and "rider" in td.get("class")), None
        )
        if rider_idx is None:
            continue

        bracket_td = tr.find("td", class_="bracket")
        if bracket_td is not None:
            last_waku = to_int(bracket_td.get_text(strip=True))

        tip_td = tr.find("td", class_="tip")
        num_td = tr.find("td", class_="num")
        rider_td = tds[rider_idx]

        home_span = rider_td.find("span", class_="home")
        name = nfkc(rider_td.contents[0]) if rider_td.contents else None
        pref_age_kyu = nfkc(home_span.get_text(strip=True)) if home_span else ""
        parts = [p.strip() for p in pref_age_kyu.split("/")]
        prefecture = re.sub(r"\s+", "", parts[0]) if parts and parts[0] else None
        age = to_int(parts[1]) if len(parts) > 1 else None
        kyu = parts[2] if len(parts) > 2 else None

        # rider td より後ろの列は、枠番セルの有無(rowspanの巻き込み)に関わらず
        # 常に同じ並び(級班,脚質,ギヤ倍数,競走得点,S,B,逃,捲,差,マ,1着,2着,3着,着外,
        # 勝率,2連対率,3連対率)で17列続く前提で、riderからの相対位置で読む。
        tail = tds[rider_idx + 1:]

        def t(i):
            return nfkc(tail[i].get_text(strip=True)) if i < len(tail) else None

        kumiban = to_int(num_td.get_text(strip=True)) if num_td else None
        if kumiban is None:
            continue

        entries.append({
            "waku": last_waku if last_waku is not None else kumiban,
            "kumiban": kumiban,
            "racer_name": name,
            "prefecture": prefecture,
            "age": age,
            "kyu": kyu,
            "racer_class": t(0),
            "kyaku_shitsu": t(1),
            "gear_ratio": to_float(t(2)),
            "keisoku_tokuten": to_float(t(3)),
            "recent_win_rate": to_float(t(14)),
            "recent_2rentai_rate": to_float(t(15)),
            "recent_3rentai_rate": to_float(t(16)),
            "forecast_mark": nfkc(tip_td.get_text(strip=True)) if tip_td else None,
        })
    return entries


# ---------------------------------------------------------------------------
# 結果 (table.result_table)
# ---------------------------------------------------------------------------

def parse_results(soup):
    table = soup.find("table", class_="result_table")
    if table is None:
        return [], None

    results = []
    kimarite_winner = None
    for tr in table.find_all("tr"):
        if tr.find("th"):
            continue
        tds = tr.find_all("td")
        if not tds:
            continue

        tip_idx = next((i for i, td in enumerate(tds) if td.get("class") and "tip" in td.get("class")), None)
        num_idx = next((i for i, td in enumerate(tds) if td.get("class") and "num" in td.get("class")), None)
        rider_idx = next((i for i, td in enumerate(tds) if td.get("class") and "rider" in td.get("class")), None)
        if num_idx is None or rider_idx is None:
            continue

        rank = nfkc(tds[tip_idx + 1].get_text(strip=True)) if tip_idx is not None and tip_idx + 1 < len(tds) else None
        kumiban = to_int(tds[num_idx].get_text(strip=True))
        racer_name = nfkc(tds[rider_idx].get_text(strip=True))

        tail = tds[rider_idx + 1:]

        def t(i):
            return nfkc(tail[i].get_text(strip=True)) if i < len(tail) else None

        margin = t(0)
        agari = to_float(t(1))
        kimarite = t(2)
        sb_mark = t(3)

        if kumiban is None:
            continue

        if kimarite and rank == "1":
            kimarite_winner = kimarite

        results.append({
            "kumiban": kumiban,
            "rank": rank,
            "racer_name": racer_name,
            "margin": margin,
            "agari": agari,
            "sb_mark": sb_mark,
        })

    return results, kimarite_winner


# ---------------------------------------------------------------------------
# 払戻金 (table.refund_table)
# 「2枠連/2車連/3連勝」は複(box)・単(exact)の2行、「ワイド」は単独で
# 3組番(rowspan=2でセル自体が2行にまたがる)、というレイアウト。
# ---------------------------------------------------------------------------

BET_GROUP_MAP = {
    "2枠連": "枠",
    "2車連": "2車",
    "3連勝": "3連",
}


def _extract_dl_payouts(cell, bet_type, payouts):
    for dl in cell.find_all("dl", class_="cf"):
        dt = dl.find("dt")
        dd = dl.find("dd")
        combo = nfkc(dt.get_text(strip=True)) if dt else None
        if not combo or combo in ("未発売", "取消", "不成立"):
            continue
        if not dd:
            continue
        span = dd.find("span")
        popularity = nfkc(span.get_text(strip=True)) if span else None
        amount_text = dd.get_text(strip=True)
        if span:
            amount_text = amount_text.replace(span.get_text(strip=True), "")
        payout_val = to_int(amount_text.replace("円", "").replace(",", ""))
        if payout_val is None:
            continue
        payouts.append({
            "bet_type": bet_type,
            "combination": combo,
            "payout": payout_val,
            "popularity": popularity,
        })


def parse_payouts(soup):
    table = soup.find("table", class_="refund_table")
    if table is None:
        return []

    grid = expand_grid(table)
    if not grid:
        return []

    payouts = []
    ncols = len(grid[0])
    col = 0
    while col < ncols:
        label = nfkc(grid[0][col].get_text(strip=True))
        base = BET_GROUP_MAP.get(label, label)
        next_text = nfkc(grid[0][col + 1].get_text(strip=True)) if col + 1 < ncols else ""
        if next_text in ("複", "単"):
            for row_i, suffix in enumerate(["複", "単"]):
                if row_i >= len(grid) or col + 2 >= len(grid[row_i]):
                    continue
                _extract_dl_payouts(grid[row_i][col + 2], f"{base}{suffix}", payouts)
            col += 3
        else:
            if col + 1 < ncols:
                _extract_dl_payouts(grid[0][col + 1], base, payouts)
            col += 2
    return payouts


# ---------------------------------------------------------------------------
# オッズ(事前オッズ)の取得。
#
# 2026-09-19に彦根1R(?pageType=odds&kakeshikiType=2shatan)の実HTMLをdump_htmlで
# 確認して判明した構造:
#   - 券種ごとに <div id="JS_ODDSCONTENTS_{key}"> という別々のブロックが
#     *全券種分まとめて* 静的HTMLに含まれている(JSの「1着の選択」タブ切替は
#     CSSのdisplay切り替えだけで、データ自体はどのタブでも全部埋め込み済み)。
#     そのためkakeshikiTypeパラメータを変えても実は取れる中身は同じはず
#     (未検証。差が無ければURLパラメータ自体省略できる可能性がある)。
#   - 2車単は <table class="odds_table"> の中に、行=1着候補・列=2着候補の
#     N×N行列がそのまま入っている。行/列のヘッダーは<th class="n{車番}">。
#     自分自身との組み合わせは<td class="empty">。発売なしは"9999.9"。
#   - 3連単は同じ id 配下に「1着の選択」タブ(1〜N)ごとに別々の<table>が
#     並んでいて、行=2着候補・列=3着候補になっている(3次元なので1テーブル
#     では表現できず、1着で分割されている)。3連複も同様の可能性が高い。
#     今回はこの3次元形式は複雑なため未対応(必要になったら追加する)。
# 2車複・ワイド・枠単・枠複はまだ実物のHTMLで確認できていないが、2車単と
# 同じ<table class="odds_table">形式である可能性が高いとみて同じロジックで
# 解析を試みる。もし構造が違えば0件になるだけで例外にはならない(安全側)。
# ---------------------------------------------------------------------------

ODDS_CONTENT_IDS = {
    "2車単": ("JS_ODDSCONTENTS_2shatan", "-"),
    "2車複": ("JS_ODDSCONTENTS_2shahuku", "="),
    "ワイド": ("JS_ODDSCONTENTS_wide", "="),
    "枠単": ("JS_ODDSCONTENTS_niwakutan", "-"),
    "枠複": ("JS_ODDSCONTENTS_niwakufuku", "="),
    # 3連単・3連複は1着ごとに複数テーブルに分かれる3次元形式のため未対応。
}


def _parse_odds_table(table):
    """<table class="odds_table">(行=1着候補・列=2着候補のN×N行列)を
    [(row_num, col_num, odds), ...]のリストに変換する。想定と異なる構造なら
    黙って[]を返す。"""
    rows = table.find_all("tr")
    if len(rows) < 3:
        return []
    # 1行目: 列ヘッダー(車番)。class="n{数字}"を持つ<th>だけを対象にする
    # (rowspan=2の左上・右上の空セルには番号classが付かないので自然に除外される)。
    col_numbers = []
    for th in rows[0].find_all("th"):
        num = None
        for c in th.get("class") or []:
            if re.fullmatch(r"n\d+", c):
                num = int(c[1:])
        if num is not None:
            col_numbers.append(num)
    if not col_numbers:
        return []

    out = []
    # 3行目以降がデータ行(2行目は選手名の行)。
    for tr in rows[2:]:
        ths = tr.find_all("th")
        if not ths:
            continue
        row_num = None
        for c in ths[0].get("class") or []:
            if re.fullmatch(r"n\d+", c):
                row_num = int(c[1:])
        if row_num is None:
            continue
        tds = tr.find_all("td")
        if len(tds) != len(col_numbers):
            continue  # 想定外の列数、安全のためこの行は捨てる
        for col_num, td in zip(col_numbers, tds):
            if "empty" in (td.get("class") or []):
                continue
            if row_num == col_num:
                continue
            odds_text = nfkc(td.get_text(" ", strip=True))
            # ワイドなど一部の券種は「2.5～9.0」のような幅(レンジ)表記になる。
            # nfkc()で全角チルダ(～)は半角(~)に正規化されるので、分割は半角側で行う。
            # その場合は下限側(より保守的な値)をoddsとして採用する。
            odds_text = odds_text.split("~")[0].strip()
            try:
                odds_val = float(odds_text)
            except (TypeError, ValueError):
                continue
            out.append((row_num, col_num, odds_val))
    return out


def parse_odds(soup):
    """事前オッズページ(?pageType=odds)を解析する。上記の注記を参照。
    対応している券種(2車単・2車複・ワイド・枠単・枠複)ごとに
    <div id="JS_ODDSCONTENTS_{key}">内の<table class="odds_table">を探し、
    見つからない券種は静かにスキップする(発売なし、または未対応の3連単・
    3連複形式の場合)。"""
    results = []
    for bt, (div_id, sep) in ODDS_CONTENT_IDS.items():
        div = soup.find(id=div_id)
        if div is None:
            continue
        table = div.find("table", class_="odds_table")
        if table is None:
            continue
        seen = set()
        for row_num, col_num, odds_val in _parse_odds_table(table):
            if sep == "-":
                combo = f"{row_num}-{col_num}"
            else:
                combo = f"{min(row_num, col_num)}={max(row_num, col_num)}"
            if combo in seen:
                continue
            seen.add(combo)
            results.append({"bet_type": bt, "combination": combo, "odds": odds_val})
    if not results:
        logger.warning("オッズページから何も抽出できませんでした(ページ構造が想定と異なる可能性、要検証)。")
    return results


# 実HTML(2026-09-19、彦根1R)を検証した結果判明した券種ごとのURLパラメータ名。
# ページ内の<a class="JS_POST_THROW" href="...?pageType=odds&kakeshikiType=...">
# から採取。枠単・枠複(niwakutan/niwakufuku)はレースによってはリンク自体が
# 存在しない(発売がない)ことがある。
ODDS_KAKESHIKI_TYPES = {
    "3連単": "3rentan",
    "2車単": "2shatan",
    "3連複": "3renhuku",
    "2車複": "2shahuku",
    "ワイド": "wide",
    "枠単": "niwakutan",
    "枠複": "niwakufuku",
}


def odds_url(slug, day_id, rno, kakeshiki_type=None):
    """race_url()と同じレースの、事前オッズページのURLを組み立てる。
    kakeshiki_typeを指定しない場合、サイト側のデフォルト("3rentan"=3連単)が
    表示される点に注意(2車単・2車複・ワイドなどを取るには明示的な指定が必須)。"""
    url = race_url(slug, day_id, rno) + "?pageType=odds"
    if kakeshiki_type:
        url += f"&kakeshikiType={kakeshiki_type}"
    return url


def parse_line_predictions(soup):
    """並び予想(<dl class="racecard_footer-contents"><dt>並び予想</dt>
    <dd><div class="line_position">...)を解析し、ライン構成を返す。
    1ラインは隊列順に並んだ (kumiban, role) のリスト。
    「←」だけの要素(p000, 凡例的なもの)は選手データではないため無視する。
    空の<span class="icon_p space">はライン同士の区切り。"""
    dt = soup.find("dt", string=lambda s: s and nfkc(s) == "並び予想")
    if dt is None:
        return []
    dd = dt.find_next_sibling("dd")
    if dd is None:
        return []
    container = dd.find("div", class_="line_position")
    if container is None:
        return []

    lines = []
    current = []
    for icon in container.find_all("span", class_="icon_p", recursive=False):
        classes = icon.get("class") or []
        if "space" in classes:
            if current:
                lines.append(current)
                current = []
            continue
        inner_spans = icon.find_all("span")
        if len(inner_spans) == 1 and nfkc(inner_spans[0].get_text(strip=True)) == "←":
            continue
        if len(inner_spans) < 2:
            continue
        kumiban = to_int(inner_spans[0].get_text(strip=True))
        role = nfkc(inner_spans[1].get_text(strip=True))
        if kumiban is None:
            continue
        current.append({"kumiban": kumiban, "role": role})
    if current:
        lines.append(current)

    result = []
    for line_no, line in enumerate(lines, start=1):
        for pos, item in enumerate(line, start=1):
            result.append({
                "line_no": line_no,
                "position_in_line": pos,
                "line_size": len(line),
                "kumiban": item["kumiban"],
                "role": item["role"],
            })
    return result


def parse_race_detail(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    race_info = parse_race_header(soup)
    entries = parse_entries(soup)
    results, kimarite = parse_results(soup)
    if kimarite:
        race_info["kimarite"] = kimarite
    payouts = parse_payouts(soup)
    line_predictions = parse_line_predictions(soup)

    for e in entries:
        e["gender"] = "女" if race_info.get("is_girls") else "男"

    return race_info, entries, results, payouts, line_predictions


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_slug TEXT,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    title TEXT,
    race_type TEXT,
    grade TEXT,
    is_girls INTEGER,
    weather TEXT,
    wind_speed REAL,
    kimarite TEXT,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno)
);

CREATE TABLE IF NOT EXISTS entries (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    kumiban INTEGER NOT NULL,
    waku INTEGER,
    racer_name TEXT,
    gender TEXT,
    prefecture TEXT,
    age INTEGER,
    kyu TEXT,
    racer_class TEXT,
    kyaku_shitsu TEXT,
    gear_ratio REAL,
    keisoku_tokuten REAL,
    recent_win_rate REAL,
    recent_2rentai_rate REAL,
    recent_3rentai_rate REAL,
    forecast_mark TEXT,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno, kumiban)
);

CREATE TABLE IF NOT EXISTS results (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    kumiban INTEGER NOT NULL,
    rank TEXT,
    is_incident INTEGER,
    racer_name TEXT,
    margin TEXT,
    agari REAL,
    sb_mark TEXT,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno, kumiban)
);

CREATE TABLE IF NOT EXISTS payouts (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    bet_type TEXT NOT NULL,
    combination TEXT NOT NULL,
    payout INTEGER,
    popularity TEXT,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno, bet_type, combination)
);

CREATE TABLE IF NOT EXISTS line_predictions (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    position_in_line INTEGER NOT NULL,
    line_size INTEGER,
    kumiban INTEGER NOT NULL,
    role TEXT,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno, kumiban)
);

CREATE TABLE IF NOT EXISTS odds (
    race_date TEXT NOT NULL,
    venue_code TEXT NOT NULL,
    venue_name TEXT,
    rno INTEGER NOT NULL,
    bet_type TEXT NOT NULL,
    combination TEXT NOT NULL,
    odds REAL,
    fetched_at TEXT,
    PRIMARY KEY (race_date, venue_code, rno, bet_type, combination)
);

CREATE TABLE IF NOT EXISTS scrape_log (
    race_date TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    venues_count INTEGER,
    races_count INTEGER,
    status TEXT,
    PRIMARY KEY (race_date, started_at)
);
"""


def get_connection(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def save_race(conn, race_date, venue_code, venue_slug, venue_name, rno, race_info, fetched_at):
    conn.execute(
        """INSERT OR REPLACE INTO races
        (race_date, venue_code, venue_slug, venue_name, rno, title, race_type, grade, is_girls,
         weather, wind_speed, kimarite, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (race_date, venue_code, venue_slug, venue_name, rno, race_info.get("title"),
         race_info.get("race_type"), race_info.get("grade"), int(bool(race_info.get("is_girls"))),
         race_info.get("weather"), race_info.get("wind_speed"), race_info.get("kimarite"), fetched_at),
    )


def save_entries(conn, race_date, venue_code, venue_name, rno, entries, fetched_at):
    for e in entries:
        conn.execute(
            """INSERT OR REPLACE INTO entries
            (race_date, venue_code, venue_name, rno, kumiban, waku, racer_name, gender,
             prefecture, age, kyu, racer_class, kyaku_shitsu, gear_ratio, keisoku_tokuten,
             recent_win_rate, recent_2rentai_rate, recent_3rentai_rate, forecast_mark, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race_date, venue_code, venue_name, rno, e["kumiban"], e["waku"], e["racer_name"],
             e.get("gender"), e["prefecture"], e["age"], e["kyu"], e["racer_class"],
             e["kyaku_shitsu"], e["gear_ratio"], e["keisoku_tokuten"], e["recent_win_rate"],
             e["recent_2rentai_rate"], e["recent_3rentai_rate"], e["forecast_mark"], fetched_at),
        )


INCIDENT_KEYWORDS = ("落", "失", "妨", "欠", "棄")


def save_results(conn, race_date, venue_code, venue_name, rno, results, fetched_at):
    for r in results:
        rank = r["rank"]
        is_incident = 1 if rank and not rank.isdigit() and any(k in rank for k in INCIDENT_KEYWORDS) else 0
        conn.execute(
            """INSERT OR REPLACE INTO results
            (race_date, venue_code, venue_name, rno, kumiban, rank, is_incident, racer_name,
             margin, agari, sb_mark, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race_date, venue_code, venue_name, rno, r["kumiban"], rank, is_incident,
             r["racer_name"], r["margin"], r["agari"], r.get("sb_mark"), fetched_at),
        )


def save_payouts(conn, race_date, venue_code, venue_name, rno, payouts, fetched_at):
    for p in payouts:
        if not p.get("bet_type"):
            continue
        conn.execute(
            """INSERT OR REPLACE INTO payouts
            (race_date, venue_code, venue_name, rno, bet_type, combination, payout, popularity, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (race_date, venue_code, venue_name, rno, p["bet_type"], p["combination"], p["payout"],
             p["popularity"], fetched_at),
        )


def save_odds(conn, race_date, venue_code, venue_name, rno, odds_list, fetched_at):
    for o in odds_list:
        conn.execute(
            """INSERT OR REPLACE INTO odds
            (race_date, venue_code, venue_name, rno, bet_type, combination, odds, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (race_date, venue_code, venue_name, rno, o["bet_type"], o["combination"], o["odds"], fetched_at),
        )


def save_line_predictions(conn, race_date, venue_code, venue_name, rno, line_predictions, fetched_at):
    for lp in line_predictions:
        conn.execute(
            """INSERT OR REPLACE INTO line_predictions
            (race_date, venue_code, venue_name, rno, line_no, position_in_line, line_size,
             kumiban, role, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (race_date, venue_code, venue_name, rno, lp["line_no"], lp["position_in_line"],
             lp["line_size"], lp["kumiban"], lp["role"], fetched_at),
        )


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def scrape_day(date_str, db_path, interval_sec=1.5):
    session = PoliteSession(interval_sec=interval_sec)
    conn = get_connection(db_path)
    started_at = datetime.now().isoformat(timespec="seconds")

    venues = get_active_venues(session, date_str)
    if not venues:
        logger.warning("開催中の競輪場が見つかりませんでした (date=%s)", date_str)

    races_count = 0
    for venue in venues:
        slug = venue["slug"]
        day_id = venue.get("day_id")
        venue_name_guess = venue["venue_name"]
        venue_code = (day_id[0:2] if day_id else venue["races"][0][1].rsplit("/racedetail/", 1)[-1][:2])
        logger.info(
            "=== %s会場 (code=%s, slug=%s, レース数=%d) の取得を開始 ===",
            venue_name_guess, venue_code, slug, len(venue["races"]),
        )

        for rno, url in venue["races"]:
            html_text = session.get(url)
            if html_text is None:
                logger.warning("slug=%s %dR取得失敗: %s", slug, rno, url)
                continue

            race_info, entries, results, payouts, line_predictions = parse_race_detail(html_text)
            if not entries:
                logger.warning("slug=%s %dRは出走データを抽出できませんでした(ページ構造が想定と異なる可能性)。", slug, rno)
                continue

            venue_name = race_info.get("venue_name") or venue_name_guess
            fetched_at = datetime.now().isoformat(timespec="seconds")

            save_race(conn, date_str, venue_code, slug, venue_name, rno, race_info, fetched_at)
            save_entries(conn, date_str, venue_code, venue_name, rno, entries, fetched_at)
            if results:
                save_results(conn, date_str, venue_code, venue_name, rno, results, fetched_at)
            if payouts:
                save_payouts(conn, date_str, venue_code, venue_name, rno, payouts, fetched_at)
            if line_predictions:
                save_line_predictions(conn, date_str, venue_code, venue_name, rno, line_predictions, fetched_at)

            conn.commit()
            races_count += 1
            logger.info(
                "slug=%s %dR 保存完了 (entries=%d, results=%d, payouts=%d, line=%d)",
                slug, rno, len(entries), len(results), len(payouts), len(line_predictions),
            )

    races_total = conn.execute(
        "SELECT COUNT(*) FROM races WHERE race_date = ?", (date_str,)
    ).fetchone()[0]
    results_races = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT venue_code, rno FROM results WHERE race_date = ?)",
        (date_str,),
    ).fetchone()[0]
    completeness = (results_races / races_total) if races_total else 1.0
    today_jst = datetime.now(JST).strftime("%Y%m%d")
    expect_complete = date_str < today_jst

    status = "ok"
    if expect_complete and races_total > 0 and completeness < RESULTS_COMPLETENESS_THRESHOLD:
        status = "partial"
        logger.error(
            "結果データが不足しています: %d/%d レース (%.1f%%) が結果取得済み。閾値(%.0f%%)を下回っています。"
            "サイト側のHTML変更やスクレイピング失敗の可能性があります。",
            results_races, races_total, completeness * 100, RESULTS_COMPLETENESS_THRESHOLD * 100,
        )
    else:
        logger.info("結果取得率: %d/%d レース (%.1f%%)", results_races, races_total, completeness * 100)

    finished_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO scrape_log (race_date, started_at, finished_at, venues_count, races_count, status) "
        "VALUES (?,?,?,?,?,?)",
        (date_str, started_at, finished_at, len(venues), races_count, status),
    )
    conn.commit()
    conn.close()
    logger.info("完了: %s (会場数=%d, レース数=%d, status=%s)", date_str, len(venues), races_count, status)
    return len(venues), races_count, status


def scrape_odds_once(date_str, db_path, interval_sec=1.5):
    """指定日の「まだ結果が出ていないレース」の事前オッズを1周分だけ取得して保存する。
    entries/results等と違いオッズはレース直前まで刻一刻と変わるため、この関数を
    GitHub Actionsのcronなどで日中に繰り返し(例: 30分おき)呼び出すことを想定している。
    1回のリクエストで2車単・2車複・ワイド(・発売があれば枠単・枠複)が全部
    まとめて取れることを実HTMLで確認済みなので、kakeshikiTypeは指定しない。
    resultsテーブルに既に結果がある(=レース終了済み)場合はオッズ取得をスキップする
    (無駄なリクエストを避け、かつ「発走直前に近い最後に取れたオッズ」を自然に
    DBに残すため — 終了後に取得すると「発売終了」の値で上書きしてしまう恐れがある)。"""
    session = PoliteSession(interval_sec=interval_sec)
    conn = get_connection(db_path)

    venues = get_active_venues(session, date_str)
    if not venues:
        logger.info("オッズ取得: %s は開催中の競輪場が見つかりませんでした。", date_str)
        conn.close()
        return 0, 0

    fetched_races = 0
    skipped_finished = 0
    total_odds_rows = 0
    for venue in venues:
        slug = venue["slug"]
        day_id = venue.get("day_id")
        venue_code = (day_id[0:2] if day_id else venue["races"][0][1].rsplit("/racedetail/", 1)[-1][:2])

        for rno, _card_url in venue["races"]:
            already_finished = conn.execute(
                "SELECT 1 FROM results WHERE race_date=? AND venue_code=? AND rno=? LIMIT 1",
                (date_str, venue_code, rno),
            ).fetchone()
            if already_finished:
                skipped_finished += 1
                continue

            url = odds_url(slug, day_id, rno)
            html_text = session.get(url)
            if html_text is None:
                logger.warning("オッズ取得失敗: slug=%s %dR %s", slug, rno, url)
                continue

            soup = BeautifulSoup(html_text, "lxml")
            odds_list = parse_odds(soup)
            if not odds_list:
                continue
            fetched_at = datetime.now().isoformat(timespec="seconds")
            venue_name = venue.get("venue_name") or slug
            save_odds(conn, date_str, venue_code, venue_name, rno, odds_list, fetched_at)
            conn.commit()
            fetched_races += 1
            total_odds_rows += len(odds_list)
            logger.info("オッズ保存: slug=%s %dR %d件", slug, rno, len(odds_list))

    conn.close()
    logger.info(
        "オッズ取得完了: %s (取得レース数=%d, 件数=%d, 終了済みでスキップ=%d)",
        date_str, fetched_races, total_odds_rows, skipped_finished,
    )
    return fetched_races, total_odds_rows


def normalize_date_arg(value):
    """'20250918'のほか'2025-09-18'や'2025/09/18'のような区切り文字入りでも
    受け付けられるように正規化する。数字8桁にならない場合はNoneを返す
    (呼び出し側でエラーメッセージを出す)。"""
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) != 8:
        return None
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return None
    return digits


def date_range(start_date, end_date):
    """YYYYMMDD文字列2つの間(両端含む)を1日刻みのYYYYMMDD文字列リストで返す。"""
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    days = []
    cur = start
    while cur <= end:
        days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


def backfill_range(start_date, end_date, db_path, interval_sec=1.5):
    """start_date〜end_date(両端含む, YYYYMMDD)を1日ずつ過去に遡って取得する。
    過去日の取得は現行のscrape_dayをそのまま使い回せる
    (racedetailページは直近数年分なら出走表・結果・払戻金・並び予想が
    1ページに揃っていることを実データで確認済み)。
    1日分の取得に失敗しても(例外・部分取得とも)ログに残して次の日へ進み、
    長時間のバックフィル全体を1日の失敗で止めないようにする。"""
    days = date_range(start_date, end_date)
    logger.info("バックフィル開始: %s 〜 %s (%d日分)", start_date, end_date, len(days))

    ok_days = 0
    partial_days = 0
    failed_days = 0
    total_races = 0

    for i, date_str in enumerate(days, start=1):
        logger.info("--- [%d/%d] %s の取得を開始 ---", i, len(days), date_str)
        try:
            _, races_count, status = scrape_day(date_str, db_path, interval_sec=interval_sec)
            total_races += races_count
            if status == "ok":
                ok_days += 1
            else:
                partial_days += 1
        except Exception:
            failed_days += 1
            logger.exception("%s の取得中に例外が発生しました。この日をスキップして続行します。", date_str)

    logger.info(
        "バックフィル完了: %s 〜 %s / 成功=%d日 partial=%d日 failed=%d日 / 取得レース数合計=%d",
        start_date, end_date, ok_days, partial_days, failed_days, total_races,
    )
    return ok_days, partial_days, failed_days, total_races


def dump_html(venue_slug, day_id, rno, out_dir="data/dump", page_type="card", kakeshiki_type=None):
    """指定レースの生HTMLをファイルに保存するだけのデバッグ用関数。DBには触れない。
    page_type="odds"で事前オッズページ(?pageType=odds)を保存できる
    (parse_odds()の検証用。実HTMLを見て正しくパースできているか確認するのに使う)。
    kakeshiki_type(例: "2shatan")を指定しないと、サイト側のデフォルト(3連単)が
    返ってくる点に注意。"""
    session = PoliteSession()
    url = race_url(venue_slug, day_id, rno)
    suffix = ""
    if page_type == "odds":
        url = odds_url(venue_slug, day_id, rno, kakeshiki_type=kakeshiki_type)
        suffix = f"_odds_{kakeshiki_type}" if kakeshiki_type else "_odds"
    html_text = session.get(url)
    if html_text is None:
        logger.error("取得失敗: %s", url)
        return None
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(out_dir) / f"{venue_slug}_{day_id}_{rno:02d}{suffix}.html"
    out_path.write_text(html_text, encoding="utf-8")
    logger.info("保存しました: %s (%s)", out_path, url)
    return str(out_path)


def test_odds(venue_slug, day_id, rno, date_str=None):
    """事前オッズページを取得してparse_odds()にかけ、結果件数と冒頭数件をログに
    出すだけの検証用コマンド。DBには一切書き込まない。--with-oddsを本運用で
    有効にする前に、これで実際の抽出結果を目視確認することを想定している。
    venue_slug/day_idを省略しdate_strだけ渡した場合、その日に開催中の最初の
    会場・最初のレースを自動的に選んで検証する(手動でURLからslug/day_idを
    調べる手間を省くため)。"""
    session = PoliteSession()
    if not venue_slug or not day_id:
        if not date_str:
            logger.error("venue_slug/day_idを省略する場合はdate_strが必要です。")
            return
        venues = get_active_venues(session, date_str)
        if not venues:
            logger.error("%s: 開催中の会場が見つかりませんでした。", date_str)
            return
        v = venues[0]
        venue_slug = v["slug"]
        day_id = v.get("day_id")
        if not day_id:
            logger.error("%s会場のday_idが取得できませんでした。", venue_slug)
            return
        rno = v["races"][0][0] if v["races"] else 1
        logger.info("自動選択: venue_slug=%s day_id=%s rno=%d", venue_slug, day_id, rno)
    url = odds_url(venue_slug, day_id, rno)
    html_text = session.get(url)
    if html_text is None:
        logger.error("取得失敗: %s", url)
        return
    soup = BeautifulSoup(html_text, "lxml")
    odds_list = parse_odds(soup)
    logger.info("URL: %s", url)
    logger.info("抽出件数: %d件", len(odds_list))
    by_type = {}
    for o in odds_list:
        by_type.setdefault(o["bet_type"], []).append(o)
    for bt, items in by_type.items():
        logger.info("  %s: %d件 (例: %s)", bt, len(items), items[:5])


def main():
    parser = argparse.ArgumentParser(description="楽天Kドリームスから競輪の日次データを取得しSQLiteに保存する")
    parser.add_argument("--date", help="取得対象日 (YYYYMMDD)。--whenより優先。")
    parser.add_argument("--when", choices=["today", "yesterday"], default="yesterday",
                         help="--date省略時に基準にする日。デフォルトは'yesterday'。")
    parser.add_argument("--start-date", help="バックフィル用: 取得開始日(YYYYMMDD)。--end-dateと併用。")
    parser.add_argument("--end-date", help="バックフィル用: 取得終了日(YYYYMMDD、この日を含む)。--start-dateと併用。")
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "keirin.db"), help="SQLiteファイルのパス")
    parser.add_argument("--interval", type=float, default=1.5, help="リクエスト間隔(秒)。デフォルト1.5秒。")
    parser.add_argument("--log", default=str(Path(__file__).parent / "data" / "scraper.log"), help="ログファイルのパス")

    parser.add_argument("--dump-html", action="store_true",
                         help="DBに保存せず、指定レースの生HTMLをファイルに保存して終了する(検証用)。"
                              "--venue-slug と --day-id を合わせて指定する。")
    parser.add_argument("--page-type", choices=["card", "odds"], default="card",
                         help="--dump-html用: 'odds'を指定すると出走表ではなく事前オッズページ"
                              "(?pageType=odds)を保存する。デフォルトは'card'(通常の出走表ページ)。")
    parser.add_argument("--kakeshiki-type",
                         choices=list(ODDS_KAKESHIKI_TYPES.values()),
                         help="--dump-html --page-type odds用: 券種を指定する"
                              "(例: 2shatan=2車単, 2shahuku=2車複, wide=ワイド, 3rentan=3連単)。"
                              "省略時はサイト側のデフォルト(3連単)が返る。")
    parser.add_argument("--test-odds", action="store_true",
                         help="DBに保存せず、指定レースの事前オッズページを取得してparse_odds()に"
                              "かけ、抽出結果をログに出すだけで終了する(検証用)。"
                              "--venue-slug と --day-id を合わせて指定する。")
    parser.add_argument("--venue-slug", help="--dump-html/--test-odds用: 例 gifu")
    parser.add_argument("--day-id", help="--dump-html/--test-odds用: 14桁の開催日ID(末尾00)。例 43202608240300")
    parser.add_argument("--rno", type=int, default=1, help="--dump-html/--test-odds用: レース番号")

    parser.add_argument("--scrape-odds", action="store_true",
                         help="--date/--whenで指定した日の、まだ結果が出ていないレースの"
                              "事前オッズを1周分だけ取得してDBに保存し終了する。オッズは"
                              "発走直前まで変わり続けるため、この1回だけの実行を日中"
                              "繰り返し呼び出す(GitHub Actionsのcron等)運用を想定している。"
                              "通常のentries/results等の取得(--start-date/--end-date指定なし"
                              "の通常実行)とは別物。")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    elif args.when == "today":
        date_str = datetime.now(JST).strftime("%Y%m%d")
    else:
        date_str = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d")

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(args.log, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    if args.dump_html:
        if not args.venue_slug or not args.day_id:
            parser.error("--dump-html には --venue-slug と --day-id が必要です。")
        dump_html(args.venue_slug, args.day_id, args.rno, page_type=args.page_type,
                  kakeshiki_type=args.kakeshiki_type)
        return

    if args.test_odds:
        test_odds(args.venue_slug, args.day_id, args.rno, date_str=date_str)
        return

    if args.scrape_odds:
        logger.info("オッズ取得開始: date=%s db=%s interval=%.1fs", date_str, args.db, args.interval)
        scrape_odds_once(date_str, args.db, interval_sec=args.interval)
        return

    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            parser.error(
                f"--start-date と --end-date は両方一緒に指定してください "
                f"(受け取った値: --start-date={args.start_date!r} --end-date={args.end_date!r})。"
            )
        start_norm = normalize_date_arg(args.start_date)
        end_norm = normalize_date_arg(args.end_date)
        if start_norm is None:
            parser.error(
                f"--start-date の形式が正しくありません(受け取った値: {args.start_date!r})。"
                f"YYYYMMDD形式(例: 20250918)で指定してください。"
            )
        if end_norm is None:
            parser.error(
                f"--end-date の形式が正しくありません(受け取った値: {args.end_date!r})。"
                f"YYYYMMDD形式(例: 20251211)で指定してください。"
            )
        if start_norm > end_norm:
            parser.error(
                f"--start-date({start_norm})は --end-date({end_norm})以前の日付にしてください。"
            )
        logger.info(
            "KEIRINバックフィル開始: %s 〜 %s db=%s interval=%.1fs",
            start_norm, end_norm, args.db, args.interval,
        )
        ok_days, partial_days, failed_days, _ = backfill_range(
            start_norm, end_norm, args.db, interval_sec=args.interval
        )
        if failed_days > 0 or partial_days > 0:
            logger.warning(
                "一部の日でデータが不完全でした(partial=%d日, failed=%d日)。"
                "スクレイパー自体は正常終了しますが、ログを確認してください。",
                partial_days, failed_days,
            )
        return

    logger.info("KEIRINデータ取得開始: date=%s db=%s interval=%.1fs", date_str, args.db, args.interval)
    _, _, status = scrape_day(date_str, args.db, interval_sec=args.interval)
    if status != "ok":
        logger.error("データが不完全なまま終了しました (status=%s)。終了コード1で終了します。", status)
        sys.exit(1)


if __name__ == "__main__":
    main()
