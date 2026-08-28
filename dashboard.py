"""
KEIRINデータの動作確認用ダッシュボード。

取得したデータ(出走表・結果・払戻金)が正しく保存されているかを
目視確認することが目的。boatrace版dashboard.pyにあったような
「イン逃げ狙い目レース分析」に相当する競輪独自の必勝パターン分析は
まだ実装していない(データが溜まってから、脚質×級班×競走得点差などで
同様の分析を追加する想定)。

使い方:
    streamlit run dashboard.py
"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "keirin.db"

st.set_page_config(page_title="KEIRIN データ確認ダッシュボード", layout="wide")


@st.cache_resource
def get_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def load_df(query, params=()):
    return pd.read_sql_query(query, get_connection(), params=params)


def format_yen(v):
    if pd.isna(v):
        return "-"
    return f"¥{int(v):,}"


def fmt_date(d):
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


st.title("🚲 KEIRIN データ確認ダッシュボード")
st.caption("取得済みデータの中身をレース単位で目視確認するための簡易ツールです。")

conn = get_connection()

dates = load_df("SELECT DISTINCT race_date FROM races ORDER BY race_date DESC")["race_date"].tolist()

if not dates:
    st.warning("データがまだありません。keirin_scraper.py を実行してデータを取得してください。")
    st.stop()

st.sidebar.header("レース選択")
selected_date = st.sidebar.selectbox("日付", dates, format_func=fmt_date)

venues_df = load_df(
    """SELECT DISTINCT venue_code, venue_name FROM races
       WHERE race_date = ? ORDER BY venue_code""",
    (selected_date,),
)

if venues_df.empty:
    st.info(f"{fmt_date(selected_date)} の開催データがありません。")
    st.stop()

venue_options = {f"{row.venue_name} ({row.venue_code})": row.venue_code for row in venues_df.itertuples()}
selected_venue_label = st.sidebar.selectbox("場", list(venue_options.keys()))
selected_venue_code = venue_options[selected_venue_label]

races_df = load_df(
    """SELECT race_date, rno, title, race_type, grade, is_girls, weather, wind_speed, kimarite
       FROM races WHERE race_date = ? AND venue_code = ? ORDER BY rno""",
    (selected_date, selected_venue_code),
)

race_options = {f"{row.rno}R": row.rno for row in races_df.itertuples()}
selected_race_label = st.sidebar.selectbox("レース", list(race_options.keys()))
selected_rno = race_options[selected_race_label]

st.subheader(f"📅 {fmt_date(selected_date)} 開催場一覧")
venue_summary = load_df(
    """SELECT venue_code AS 場コード, venue_name AS 場名, COUNT(*) AS レース数
       FROM races WHERE race_date = ? GROUP BY venue_code, venue_name ORDER BY venue_code""",
    (selected_date,),
)
st.dataframe(venue_summary, hide_index=True, use_container_width=True)

st.subheader(f"🏁 {selected_venue_label} レース一覧")
race_list_display = races_df.drop(columns=["race_date"]).rename(columns={
    "rno": "R", "title": "タイトル", "race_type": "種別", "grade": "グレード",
    "is_girls": "ガールズ", "weather": "天候", "wind_speed": "風速", "kimarite": "決まり手",
})
race_list_display["ガールズ"] = race_list_display["ガールズ"].map({1: "○", 0: ""})
st.dataframe(race_list_display, hide_index=True, use_container_width=True)

st.divider()
race_row = races_df[races_df["rno"] == selected_rno].iloc[0]
st.header(f"{selected_venue_label} {selected_rno}R の詳細")
st.write(
    f"**{race_row['title'] or ''}** / {race_row['race_type'] or ''} / "
    f"グレード: {race_row['grade'] or '不明'} / ガールズ: {'○' if race_row['is_girls'] else ''}"
)

cols = st.columns(3)
cols[0].metric("天候", race_row["weather"] or "-")
cols[1].metric("風速", f"{race_row['wind_speed']}m" if pd.notna(race_row["wind_speed"]) else "-")
cols[2].metric("決まり手", race_row["kimarite"] or "-")

tab_entries, tab_results, tab_payouts = st.tabs(["出走表", "結果", "払戻金"])

with tab_entries:
    entries_df = load_df(
        """SELECT waku AS 枠番, kumiban AS 車番, racer_name AS 選手名, gender AS 性別, racer_class AS 級班,
                  prefecture AS 府県, age AS 年齢, kyu AS 期別, kyaku_shitsu AS 脚質,
                  gear_ratio AS ギヤ倍数, keisoku_tokuten AS 競走得点,
                  recent_win_rate AS 勝率, recent_2rentai_rate AS 2連対率, recent_3rentai_rate AS 3連対率,
                  forecast_mark AS 予想印
           FROM entries WHERE race_date = ? AND venue_code = ? AND rno = ? ORDER BY kumiban""",
        (selected_date, selected_venue_code, selected_rno),
    )
    if entries_df.empty:
        st.info("出走表データがありません。")
    else:
        st.dataframe(entries_df, hide_index=True, use_container_width=True)

with tab_results:
    results_df = load_df(
        """SELECT rank AS 着順, kumiban AS 車番, racer_name AS 選手名, margin AS 着差, agari AS 上りタイム,
                  sb_mark AS "S/B", is_incident AS 事故
           FROM results WHERE race_date = ? AND venue_code = ? AND rno = ? ORDER BY rank""",
        (selected_date, selected_venue_code, selected_rno),
    )
    if results_df.empty:
        st.info("結果データがありません(レース未実施、または未取得の可能性があります)。")
    else:
        st.dataframe(results_df, hide_index=True, use_container_width=True)

with tab_payouts:
    payouts_df = load_df(
        """SELECT bet_type AS 賭式, combination AS 組番, payout AS 金額, popularity AS 人気
           FROM payouts WHERE race_date = ? AND venue_code = ? AND rno = ?""",
        (selected_date, selected_venue_code, selected_rno),
    )
    if payouts_df.empty:
        st.info("払戻金データがありません(レース未実施、または未取得の可能性があります)。")
    else:
        payouts_df["金額"] = payouts_df["金額"].apply(format_yen)
        st.dataframe(payouts_df, hide_index=True, use_container_width=True)

st.divider()
st.caption(f"DB: {DB_PATH}")
