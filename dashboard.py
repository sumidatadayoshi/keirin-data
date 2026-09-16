"""
KEIRINデータの動作確認用ダッシュボード。

取得したデータ(出走表・結果・払戻金)が正しく保存されているかを
目視確認することが目的。加えて、boatrace版dashboard.pyにあった
「イン逃げ狙い目レース分析」に相当する競輪独自の必勝パターン分析として、
「🎯 狙い目レース分析」「💰 回収率シミュレーター」を実装している
(いずれも実験的機能)。

「狙い目」の条件(全期間の実データで検証済み。2026/08/24〜2026/09/10の
1083レースの時点で、条件合致311レース・勝率54.7%・3連対率83.6%):
    ①同一レース内で競走得点が最も高い(得点1位)
    ②予想印が◎(サイトの本命印)と一致
    ③2位との得点差が2.0以上
    ④脚質が「逃げ」または「両」(先行できるタイプ。「追込」のみの選手は除外)
の4条件をすべて満たす選手を「本命」として抽出する。

なお「今日のおすすめ」(レース前の当日出走表だけで判定する機能)は未実装。
現状のkeirin_scraper.pyは`--when yesterday`(結果確定後)でしか
取得していないため、boatrace版のような当日エントリーのみの事前取得
(`--entries-only`相当)を別途実装する必要がある。

使い方:
    streamlit run dashboard.py
"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "keirin.db"

# 「狙い目レース分析」の判定条件・関連定数。
# 2026/08/24〜2026/09/10の実データ(1083レース)で検証し、単独条件より
# 明確に成績が良かった組み合わせを採用している(詳細はモジュールdocstring参照)。
HONMEI_MARK = "◎"
TOKUTEN_GAP_THRESHOLD = 2.0
QUALIFYING_KYAKUSHITSU = {"逃", "両"}
SAMPLE_SIZE_WARNING_THRESHOLD = 5
BET_AMOUNT = 100

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


def compute_pick_races(entries_all, results_all):
    """全期間のentries/resultsから、レースごとに競走得点が最も高い選手(=得点1位)を
    抽出し、2位との得点差・実際の着順(未確定ならNaN)を付与して返す。
    1行=1レースの「その他レースの本命候補」データフレーム。"""
    cols = [
        "race_date", "venue_code", "rno", "pick_kumiban", "pick_name", "kyaku_shitsu",
        "forecast_mark", "keisoku_tokuten", "second_kumiban", "tokuten_2nd", "gap",
        "pick_rank", "qualifies",
    ]
    if entries_all.empty:
        return pd.DataFrame(columns=cols)

    df = entries_all.dropna(subset=["keisoku_tokuten"]).copy()
    df["race_id"] = df["race_date"] + "_" + df["venue_code"].astype(str) + "_" + df["rno"].astype(str)
    df["tokuten_rank"] = df.groupby("race_id")["keisoku_tokuten"].rank(ascending=False, method="first")

    top1 = df[df["tokuten_rank"] == 1].rename(
        columns={"kumiban": "pick_kumiban", "racer_name": "pick_name"}
    )
    top2 = df[df["tokuten_rank"] == 2][["race_id", "kumiban", "keisoku_tokuten"]].rename(
        columns={"kumiban": "second_kumiban", "keisoku_tokuten": "tokuten_2nd"}
    )
    picks = top1.merge(top2, on="race_id", how="left")
    picks["gap"] = picks["keisoku_tokuten"] - picks["tokuten_2nd"]

    if not results_all.empty:
        picks = picks.merge(
            results_all.rename(columns={"kumiban": "pick_kumiban", "rank": "pick_rank"}),
            on=["race_date", "venue_code", "rno", "pick_kumiban"], how="left",
        )
    else:
        picks["pick_rank"] = None
    picks["pick_rank"] = pd.to_numeric(picks["pick_rank"], errors="coerce")

    picks["qualifies"] = (
        (picks["forecast_mark"] == HONMEI_MARK)
        & (picks["gap"] >= TOKUTEN_GAP_THRESHOLD)
        & (picks["kyaku_shitsu"].isin(QUALIFYING_KYAKUSHITSU))
    )
    return picks[cols]


def rate_label(hits, n):
    label = f"{hits / n * 100:.1f}% (n={n})"
    if n < SAMPLE_SIZE_WARNING_THRESHOLD:
        label += " ⚠️参考データ不足"
    return label


st.title("🚲 KEIRIN データ確認ダッシュボード")
st.caption("取得済みデータの中身をレース単位で目視確認するための簡易ツールです。")

conn = get_connection()

dates = load_df("SELECT DISTINCT race_date FROM races ORDER BY race_date DESC")["race_date"].tolist()

if not dates:
    st.warning("データがまだありません。keirin_scraper.py を実行してデータを取得してください。")
    st.stop()

st.sidebar.header("表示モード")
view_mode = st.sidebar.radio(
    "表示モード", ["レース別確認", "統計サマリー", "狙い目レース分析"], label_visibility="collapsed"
)
st.sidebar.divider()

CLASS_ORDER = ["SS", "S1", "S2", "A1", "A2", "A3", "L1"]
KYAKUSHITSU_ORDER = ["逃", "両", "追"]


def render_rate_table(group_col, group_label, order=None):
    """指定カラムでグルーピングし、出走数・1着数・連対数と各種確率を集計して表示する。"""
    df = load_df(
        f"""
        SELECT e.{group_col} AS grp,
               COUNT(*) AS 出走数,
               SUM(CASE WHEN r.rank = 1 THEN 1 ELSE 0 END) AS "1着数",
               SUM(CASE WHEN r.rank <= 2 THEN 1 ELSE 0 END) AS "2連対数",
               SUM(CASE WHEN r.rank <= 3 THEN 1 ELSE 0 END) AS "3連対数"
        FROM entries e
        LEFT JOIN results r
            ON e.race_date = r.race_date AND e.venue_code = r.venue_code
           AND e.rno = r.rno AND e.kumiban = r.kumiban
        WHERE e.{group_col} IS NOT NULL AND TRIM(e.{group_col}) != ''
        GROUP BY e.{group_col}
        """
    )
    if df.empty:
        st.info(f"{group_label}のデータがありません。")
        return

    df["勝率(%)"] = (df["1着数"] / df["出走数"] * 100).round(1)
    df["2連対率(%)"] = (df["2連対数"] / df["出走数"] * 100).round(1)
    df["3連対率(%)"] = (df["3連対数"] / df["出走数"] * 100).round(1)

    if order:
        df["_order"] = df["grp"].apply(lambda v: order.index(v) if v in order else len(order))
        df = df.sort_values("_order").drop(columns="_order")
    else:
        df = df.sort_values("grp")

    df = df.rename(columns={"grp": group_label}).reset_index(drop=True)
    st.dataframe(df, hide_index=True, width="stretch")
    st.bar_chart(df.set_index(group_label)["勝率(%)"])


if view_mode == "統計サマリー":
    st.header("📊 統計サマリー(全期間の集計)")

    total_races = int(load_df("SELECT COUNT(*) AS n FROM races")["n"].iloc[0])
    total_days = int(load_df("SELECT COUNT(DISTINCT race_date) AS n FROM races")["n"].iloc[0])
    st.caption(
        f"集計対象: {total_days}日分・{total_races}レース。"
        "サンプル数が少ないうちは偶然のブレが大きいので、あくまで参考値です。"
        "日々データが増えるほど数字の信頼度が上がっていきます。"
    )

    if total_races == 0:
        st.info("まだ集計できるデータがありません。")
        st.stop()

    st.subheader("🎯 枠番別 成績")
    st.caption("枠番(車の並び位置)によって有利不利があるかを見る集計です。")
    render_rate_table("waku", "枠番", order=list(range(1, 10)))

    st.subheader("🚴 脚質別 成績")
    st.caption("逃げ(先頭で押し切るタイプ)・追込(後ろから差すタイプ)・両(どちらもできるタイプ)の成績比較です。")
    render_rate_table("kyaku_shitsu", "脚質", order=KYAKUSHITSU_ORDER)

    st.subheader("🏅 級班別 成績")
    st.caption("SS・S1・S2・A1・A2・A3・L1(ガールズケイリン)の実力区分ごとの成績です。")
    render_rate_table("racer_class", "級班", order=CLASS_ORDER)

    st.divider()
    st.caption(f"DB: {DB_PATH}")
    st.stop()

if view_mode == "狙い目レース分析":
    st.header("🎯 狙い目レース分析(実験的機能)")
    st.caption(
        "①同一レース内で競走得点が最も高い(得点1位)、②予想印が◎(本命印)と一致、"
        "③2位との得点差が2.0以上、④脚質が「逃げ」または「両」(先行できるタイプ)、"
        "の4条件をすべて満たす選手を「本命」として抽出し、全期間の実績を集計します。"
        "競艇DBの「イン逃げ狙い目レース分析」に相当する実験的機能です。"
        "データが増えるほど母数(n)が増え、数字の信頼度が上がります。"
    )

    entries_all = load_df(
        "SELECT race_date, venue_code, rno, kumiban, racer_name, kyaku_shitsu, "
        "keisoku_tokuten, forecast_mark FROM entries"
    )
    results_all = load_df("SELECT race_date, venue_code, rno, kumiban, rank FROM results")
    races_meta = load_df("SELECT race_date, venue_code, venue_name, rno FROM races")

    picks = compute_pick_races(entries_all, results_all)
    qualifying = picks[picks["qualifies"]].copy()

    st.subheader(f"条件に合致したレース: {len(qualifying)}件")
    if qualifying.empty:
        st.info("条件に合致するレースはまだありません。")
    else:
        settled = qualifying.dropna(subset=["pick_rank"])
        n = len(settled)
        if n == 0:
            st.info("条件に合致したレースはありますが、結果がまだ判明していません。")
        else:
            win = int((settled["pick_rank"] == 1).sum())
            top2 = int((settled["pick_rank"] <= 2).sum())
            top3 = int((settled["pick_rank"] <= 3).sum())
            m1, m2, m3 = st.columns(3)
            m1.metric("勝率", rate_label(win, n))
            m2.metric("2連対率", rate_label(top2, n))
            m3.metric("3連対率", rate_label(top3, n))
            if n < SAMPLE_SIZE_WARNING_THRESHOLD:
                st.warning("⚠️ 母数が少なく、参考データ不足です。")

        display_df = settled.merge(
            races_meta[["race_date", "venue_code", "venue_name"]].drop_duplicates(),
            on=["race_date", "venue_code"], how="left",
        )
        display_df["日付"] = display_df["race_date"].apply(fmt_date)
        display_df["結果"] = display_df["pick_rank"].apply(
            lambda v: f"{int(v)}着" if pd.notna(v) else "未確定"
        )
        display_df = display_df.rename(
            columns={
                "venue_name": "場", "rno": "R", "pick_kumiban": "車番", "pick_name": "本命選手",
                "kyaku_shitsu": "脚質", "keisoku_tokuten": "競走得点", "gap": "得点差",
            }
        )[["日付", "場", "R", "本命選手", "車番", "脚質", "競走得点", "得点差", "結果"]]
        st.dataframe(
            display_df.sort_values("日付", ascending=False), hide_index=True, width="stretch"
        )

    st.divider()

    # -----------------------------------------------------------------------
    # 💰 回収率シミュレーター(実験的機能)
    #
    # 競輪には競艇の「1号艇」のような固定の枠番的な必勝パターンがないため、
    # 「本命(得点1位)が実際に1着だった場合、2着には得点2位の選手がどれくらい
    # 来るか」を全期間データから確認し、それを固定の買い目(2車単 本命→得点2位)
    # として、条件に合致した全レースに100円ずつ賭けていたと仮定した場合の
    # 回収率を計算する。
    # -----------------------------------------------------------------------
    st.header("💰 回収率シミュレーター(実験的機能)")
    st.caption(
        "「狙い目レース分析」の条件に合致した本命選手を1着、得点2位の選手を2着に固定した"
        "2車単を、条件に合致し結果が判明している全レースに100円ずつ賭けていたと"
        "仮定した場合の回収率を計算します。"
    )

    if qualifying.empty:
        st.info("条件に合致するレースがないため、シミュレーションできません。")
    else:
        settled = qualifying.dropna(subset=["pick_rank", "second_kumiban"]).copy()
        if settled.empty:
            st.info("結果が判明しているレースがまだありません。")
        else:
            settled["bet_combo"] = (
                settled["pick_kumiban"].astype(int).astype(str)
                + "-"
                + settled["second_kumiban"].astype(int).astype(str)
            )
            payouts_2tan = load_df(
                "SELECT race_date, venue_code, rno, combination, payout FROM payouts WHERE bet_type = '2車単'"
            )
            concluded = settled.merge(payouts_2tan, on=["race_date", "venue_code", "rno"], how="inner")
            hits = concluded[concluded["combination"] == concluded["bet_combo"]]

            total_races = len(settled)
            hit_count = len(hits)
            total_return = int(hits["payout"].sum())
            total_stake = total_races * BET_AMOUNT
            recovery_rate = (total_return / total_stake * 100) if total_stake > 0 else 0.0
            hit_rate = (hit_count / total_races * 100) if total_races > 0 else 0.0

            st.write("買い目(固定): **2車単 本命(得点1位) → 得点2位選手**")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("対象レース数(母数)", f"{total_races}件")
            m2.metric("的中回数", f"{hit_count}回")
            m3.metric("的中率", f"{hit_rate:.1f}%")
            m4.metric("回収率", f"{recovery_rate:.1f}%")

            st.caption(
                f"賭け金合計: {BET_AMOUNT}円 × {total_races}件 = {total_stake:,}円 / "
                f"払戻金合計(的中分): {total_return:,}円"
            )
            if total_races < SAMPLE_SIZE_WARNING_THRESHOLD:
                st.warning("⚠️ 対象レース数(母数)が少なく、参考データ不足です。")
            st.caption(
                "回収率が100%を下回っていても、控除率(寺銭)を考えれば異常ではありません。"
                "「本命の勝率自体が高いか」を見る「狙い目レース分析」と合わせて参考にしてください。"
            )

    st.divider()
    st.caption(f"DB: {DB_PATH}")
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
st.dataframe(venue_summary, hide_index=True, width="stretch")

st.subheader(f"🏁 {selected_venue_label} レース一覧")
race_list_display = races_df.drop(columns=["race_date"]).rename(columns={
    "rno": "R", "title": "タイトル", "race_type": "種別", "grade": "グレード",
    "is_girls": "ガールズ", "weather": "天候", "wind_speed": "風速", "kimarite": "決まり手",
})
race_list_display["ガールズ"] = race_list_display["ガールズ"].map({1: "○", 0: ""})
st.dataframe(race_list_display, hide_index=True, width="stretch")

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

tab_entries, tab_results, tab_payouts, tab_line = st.tabs(["出走表", "結果", "払戻金", "ライン予想"])

with tab_entries:
    entries_df = load_df(
        """SELECT waku AS 枠番, kumiban AS 車番, racer_name AS 選手名, gender AS 性別, racer_class AS 級班,
                  prefecture AS 府県, age AS 年齢, kyu AS 期別, kyaku_shitsu AS 脚質,
                  gear_ratio AS ギヤ倍数, keisoku_tokuten AS 競走得点,
                  recent_win_rate AS 勝率, recent_2rentai_rate AS "2連対率", recent_3rentai_rate AS "3連対率",
                  forecast_mark AS 予想印
           FROM entries WHERE race_date = ? AND venue_code = ? AND rno = ? ORDER BY kumiban""",
        (selected_date, selected_venue_code, selected_rno),
    )
    if entries_df.empty:
        st.info("出走表データがありません。")
    else:
        st.dataframe(entries_df, hide_index=True, width="stretch")

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
        st.dataframe(results_df, hide_index=True, width="stretch")

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
        st.dataframe(payouts_df, hide_index=True, width="stretch")

with tab_line:
    line_df = load_df(
        """SELECT lp.line_no AS ライン, lp.position_in_line AS 隊列順, lp.kumiban AS 車番,
                  lp.role AS 役割, e.racer_name AS 選手名, e.kyaku_shitsu AS 脚質,
                  e.keisoku_tokuten AS 競走得点, e.forecast_mark AS 予想印
           FROM line_predictions lp
           LEFT JOIN entries e
             ON e.race_date = lp.race_date AND e.venue_code = lp.venue_code
            AND e.rno = lp.rno AND e.kumiban = lp.kumiban
           WHERE lp.race_date = ? AND lp.venue_code = ? AND lp.rno = ?
           ORDER BY lp.line_no, lp.position_in_line""",
        (selected_date, selected_venue_code, selected_rno),
    )
    if line_df.empty:
        st.info(
            "ライン予想データがありません(未取得、またはこのレースは対象外の可能性があります)。\n\n"
            "※ライン予想は2026/09/16以降に取得したレースから収集しています。それ以前のレースにはデータがありません。"
        )
    else:
        for line_no, group in line_df.groupby("ライン"):
            members = " → ".join(
                f"{row.車番}番{row.選手名 or ''}({row.役割})" for row in group.itertuples()
            )
            st.write(f"**ライン{line_no}**: {members}")
        st.dataframe(line_df, hide_index=True, width="stretch")

st.divider()
st.caption(f"DB: {DB_PATH}")
