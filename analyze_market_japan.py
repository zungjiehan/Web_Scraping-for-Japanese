"""
日股市場強勢分析腳本
計算連續天數、族群分析、排行榜概念股分析、產生 Slack 訊息與 MD 報告

對應資料來源：fetch_japan_stocks.py 產生的
  data/{日期}/日本上市收盤價.csv
  data/{日期}/日本排行榜.csv

使用方式：
    python3 analyze_market_japan.py
"""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

TODAY = date.today().strftime("%Y-%m-%d")
DATA_ROOT = Path("data")
TODAY_DIR = DATA_ROOT / TODAY
OVERRIDE_PATH = Path("category_override_japan.json")


# ─────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────
def parse_pct(s) -> float:
    if pd.isna(s):
        return 0.0
    cleaned = str(s).replace("%", "").replace(",", "").strip()
    if cleaned in ("-", "", "N/A", "na", "nan"):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def clean_symbol(sym: str) -> str:
    """正規化代號：5位數結尾為0則去掉（J-Quants 5碼 -> Yahoo 4碼）"""
    s = str(sym).strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:-1]
    return s


def load_override() -> dict:
    with open(OVERRIDE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────
# 連續天數計算
# ─────────────────────────────────────────────
def compute_consecutive_days() -> dict:
    """
    回傳 {代號: {'limit_streak': int, 'strong_streak': int}}
    漲停：讀 CSV「漲停」欄位 = 「是」
    強勢：3% ~ 漲停幅度之間 且 >= 族群平均 * 0.8
    """
    date_dirs = sorted([d for d in DATA_ROOT.iterdir() if d.is_dir()])

    daily: list[tuple] = []
    for d in date_dirs:
        fpath = d / "日本上市收盤價.csv"
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath)
        df["_sym"] = df["代號"].apply(clean_symbol)
        df["_pct"] = df["漲跌幅(%)"].apply(parse_pct)
        df["_limit"] = df["漲停"].fillna("") == "是"
        records = {}
        for _, row in df.iterrows():
            sym = row["_sym"]
            records[sym] = {"pct": row["_pct"], "group": row["分類"], "limit": row["_limit"]}
        daily.append((d.name, records))

    if not daily:
        return {}

    def group_avg(records_map: dict) -> dict:
        groups: dict[str, list] = {}
        for sym, info in records_map.items():
            groups.setdefault(info["group"], []).append(info["pct"])
        return {g: sum(v) / len(v) for g, v in groups.items()}

    def day_flags(records_map: dict, g_avg: dict) -> dict:
        flags = {}
        for sym, info in records_map.items():
            if info["limit"]:
                flags[sym] = "limit"
                continue
            p = info["pct"]
            avg = g_avg.get(info["group"], 0)
            if 3.0 <= p and p >= avg * 0.8:
                flags[sym] = "strong"
            else:
                flags[sym] = None
        return flags

    all_flags = []
    for date_str, records_map in daily:
        g_avg = group_avg(records_map)
        flags = day_flags(records_map, g_avg)
        all_flags.append((date_str, flags))

    latest_date, latest_flags = all_flags[-1]

    streak = {}
    for sym in set(latest_flags.keys()):
        flag = latest_flags.get(sym)
        if flag is None:
            streak[sym] = {"limit_streak": 0, "strong_streak": 0}
            continue
        count = 1
        for i in range(len(all_flags) - 2, -1, -1):
            prev_flags = all_flags[i][1]
            if prev_flags.get(sym) == flag:
                count += 1
                if count >= 5:
                    break
            else:
                break
        streak[sym] = {
            "limit_streak": min(count, 5) if flag == "limit" else 0,
            "strong_streak": min(count, 5) if flag == "strong" else 0,
        }
    return streak


def compute_ranking_streaks() -> dict:
    """回傳 {代號: {排行類型: 連續天數}}"""
    date_dirs = sorted([d for d in DATA_ROOT.iterdir() if d.is_dir()])
    daily_sets: list[tuple] = []
    for d in date_dirs:
        fpath = d / "日本排行榜.csv"
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath)
        df["_sym"] = df["代號"].apply(clean_symbol)
        rank_sets = {}
        for rtype, grp in df.groupby("排行類型"):
            rank_sets[rtype] = set(grp["_sym"].tolist())
        daily_sets.append((d.name, rank_sets))

    if not daily_sets:
        return {}

    latest_date, latest_rank_sets = daily_sets[-1]
    streaks = {}
    for rtype, syms in latest_rank_sets.items():
        for sym in syms:
            count = 1
            for i in range(len(daily_sets) - 2, -1, -1):
                prev_sets = daily_sets[i][1]
                if rtype in prev_sets and sym in prev_sets[rtype]:
                    count += 1
                    if count >= 5:
                        break
                else:
                    break
            streaks.setdefault(sym, {})[rtype] = min(count, 5)
    return streaks


# ─────────────────────────────────────────────
# 分類函式
# ─────────────────────────────────────────────
def build_classifier(override: dict):
    lookup = {}
    for group_name, members in override.items():
        if group_name.startswith("_"):
            continue
        for sym, info in members.items():
            if sym.startswith("_"):
                continue
            lookup[sym] = (group_name, info.get("子族群", ""))

    def classify(sym: str, fallback_group: str) -> tuple:
        if sym in lookup:
            return lookup[sym]
        return (fallback_group, "")

    return classify


# ─────────────────────────────────────────────
# 族群強勢分析
# ─────────────────────────────────────────────
def analyze_groups(streak: dict, override: dict) -> list:
    fpath = TODAY_DIR / "日本上市收盤價.csv"
    if not fpath.exists():
        return []
    df = pd.read_csv(fpath)
    df["_sym"] = df["代號"].apply(clean_symbol)
    df["_pct"] = df["漲跌幅(%)"].apply(parse_pct)

    classify = build_classifier(override)

    records = []
    for _, row in df.iterrows():
        sym = row["_sym"]
        pct = row["_pct"]
        group, sub = classify(sym, row["分類"])
        st = streak.get(sym, {"limit_streak": 0, "strong_streak": 0})
        records.append({
            "sym": sym, "name": row["名稱"], "pct": pct,
            "group": group, "sub": sub,
            "limit_streak": st["limit_streak"],
            "strong_streak": st["strong_streak"],
        })

    df2 = pd.DataFrame(records)

    group_stats = {}
    for group, gdf in df2.groupby("group"):
        avg_pct = gdf["pct"].mean()
        threshold = avg_pct * 0.8
        limit_stocks = gdf[gdf["limit_streak"] > 0].sort_values("pct", ascending=False)
        strong_mask = (gdf["limit_streak"] == 0) & (gdf["pct"] >= 3.0) & (gdf["pct"] >= threshold)
        strong_stocks = gdf[strong_mask].sort_values("pct", ascending=False)

        if len(limit_stocks) == 0 and len(strong_stocks) == 0:
            continue

        group_stats[group] = {
            "avg_pct": avg_pct,
            "limit_stocks": limit_stocks.to_dict("records"),
            "strong_stocks": strong_stocks.to_dict("records"),
        }

    sorted_groups = sorted(group_stats.items(), key=lambda x: x[1]["avg_pct"], reverse=True)
    return sorted_groups[:5]


# ─────────────────────────────────────────────
# 排行榜概念股分析（支援複數概念）
# ─────────────────────────────────────────────
def analyze_rankings(ranking_streaks: dict, override: dict) -> list:
    fpath = TODAY_DIR / "日本排行榜.csv"
    if not fpath.exists():
        return []
    df = pd.read_csv(fpath)
    df["_sym"] = df["代號"].apply(clean_symbol)
    df["_pct"] = df["漲跌幅(%)"].apply(parse_pct)

    # 今日股價補充
    price_map = {}
    fpp = TODAY_DIR / "日本上市收盤價.csv"
    if fpp.exists():
        tmp = pd.read_csv(fpp)
        tmp["_sym"] = tmp["代號"].apply(clean_symbol)
        tmp["_pct"] = tmp["漲跌幅(%)"].apply(parse_pct)
        for _, r in tmp.iterrows():
            price_map[r["_sym"]] = r["_pct"]

    name_map = {}
    for _, row in df.iterrows():
        name_map[row["_sym"]] = row["名稱"]

    # sym -> [概念清單]
    sym_concepts: dict[str, list] = {}
    for group_name, members in override.items():
        if group_name.startswith("_"):
            continue
        for sym, info in members.items():
            if sym.startswith("_"):
                continue
            concepts = info.get("概念", [])
            if isinstance(concepts, list) and concepts:
                sym_concepts[sym] = concepts

    # _概念股清單 fallback
    concept_members: dict[str, set] = {}
    for concept_name, cdata in override.get("_概念股清單", {}).items():
        if concept_name.startswith("_"):
            continue
        concept_members[concept_name] = set(cdata.get("成員", []))

    # sym -> 今日上榜榜單集合
    sym_ranks: dict[str, set] = {}
    for _, row in df.iterrows():
        sym_ranks.setdefault(row["_sym"], set()).add(row["排行類型"])

    concept_stats: dict[str, dict] = {}
    for sym, ranks in sym_ranks.items():
        pct = price_map.get(sym, 0.0)
        name = name_map.get(sym, sym)
        n_ranks = len(ranks)
        rk_streak = ranking_streaks.get(sym, {})
        max_streak = max(rk_streak.values()) if rk_streak else 1

        concepts = list(sym_concepts.get(sym, []))
        if not concepts:
            for concept_name, members in concept_members.items():
                if sym in members:
                    concepts.append(concept_name)
        if not concepts:
            concepts = ["其他"]

        for concept in concepts:
            concept_stats.setdefault(concept, {})
            concept_stats[concept][sym] = {
                "sym": sym,
                "name": name,
                "pct": pct,
                "ranks": ranks,
                "n_ranks": n_ranks,
                "max_streak": max_streak,
            }

    result = []
    for concept, stocks_dict in concept_stats.items():
        stocks = list(stocks_dict.values())
        three = [s for s in stocks if s["n_ranks"] == 3]
        two = [s for s in stocks if s["n_ranks"] == 2]
        result.append({
            "group": concept,
            "stocks": sorted(stocks, key=lambda x: (-x["n_ranks"], -x["pct"])),
            "three_count": len(three),
            "two_count": len(two),
            "one_count": len([s for s in stocks if s["n_ranks"] == 1]),
            "total": len(stocks),
        })

    result.sort(key=lambda x: (x["group"] == "其他", -x["three_count"], -x["two_count"], -x["total"]))
    return result[:10]


# ─────────────────────────────────────────────
# 市場概況（上漲/下跌/平盤/漲停檔數）
# ─────────────────────────────────────────────
def market_overview() -> dict:
    fpath = TODAY_DIR / "日本上市收盤價.csv"
    if not fpath.exists():
        return {}
    df = pd.read_csv(fpath)
    df["_pct"] = df["漲跌幅(%)"].apply(parse_pct)
    up = int((df["_pct"] > 0).sum())
    down = int((df["_pct"] < 0).sum())
    flat = int((df["_pct"] == 0).sum())
    limit_up = int((df["漲停"].fillna("") == "是").sum())
    return {"up": up, "down": down, "flat": flat, "limit_up": limit_up, "total": len(df)}


# ─────────────────────────────────────────────
# 產生 Slack 訊息（格式比照台股 v2：拿掉漲停/強勢檔數，每檔股票標示實際上榜榜單）
# ─────────────────────────────────────────────
WEEKDAY_MAP = ["一", "二", "三", "四", "五", "六", "日"]


def format_slack(
    group_analysis: list,
    group_themes: dict,
    rank_analysis: list,
    rank_themes: dict,
    overview: dict,
) -> str:
    lines = []
    weekday = WEEKDAY_MAP[date.today().weekday()]
    lines.append(f"【今日日股強勢報告】{TODAY}（週{weekday}）")
    if overview:
        lines.append(
            f"市場概況：上漲 {overview['up']} / 下跌 {overview['down']} / 平盤 {overview['flat']}"
            f"｜漲停 {overview['limit_up']} 檔"
        )
    lines.append("")

    # ── 族群強勢：只列摘要，不展開漲停/強勢檔數 ──────────────────
    lines.append("━━ 族群強勢（收盤價分析）━━")
    lines.append("")
    for group, gdata in group_analysis:
        avg = gdata["avg_pct"]
        rep_stocks = gdata["limit_stocks"][:2] + gdata["strong_stocks"][:max(0, 2 - len(gdata["limit_stocks"][:2]))]
        rep_str = "　代表: " + " ".join(
            f"{s['name']}(+{s['pct']:.1f}%)" for s in rep_stocks
        ) if rep_stocks else ""
        lines.append(f"📌 {group} 均+{avg:.2f}%{rep_str}")
    lines.append("")

    # ── 排行榜熱度：概念分區塊，每檔股票標示實際上榜的榜單 ──
    lines.append("━━ 排行榜熱度（概念分析）━━")
    lines.append("")
    for gdata in rank_analysis:
        g = gdata["group"]
        theme = rank_themes.get(g, "")
        lines.append(f"【{g}】")
        if theme:
            lines.append(f"題材：{theme}")
        if gdata["three_count"] >= 2 or gdata["two_count"] >= 3:
            lines.append(f"族群性：{gdata['total']} 檔上榜 ✅")
        else:
            lines.append(f"族群性：{gdata['total']} 檔上榜 ⚠️多概念交叉")
        lines.append("")
        for s in gdata["stocks"]:
            pct_str = f"+{s['pct']:.1f}%" if s["pct"] >= 0 else f"{s['pct']:.1f}%"
            rank_tags = "".join(f"｜{r}｜" for r in sorted(s["ranks"]))
            streak_str = f" 連續{s['max_streak']}天" if s["max_streak"] > 1 else ""
            lines.append(f"{s['name']}({s['sym']}.T) {pct_str} {rank_tags}{streak_str}")
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# 產生 Markdown 報告
# ─────────────────────────────────────────────
def format_markdown(
    slack_text: str,
    group_analysis: list,
    group_themes: dict,
    rank_analysis: list,
    rank_themes: dict,
) -> str:
    md = []
    md.append(f"# 日股市場分析報告 {TODAY}")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 強勢報告全文")
    md.append("")
    md.append("```")
    md.append(slack_text)
    md.append("```")
    md.append("")
    md.append("---")
    md.append("")

    md.append("## 族群強勢總表")
    md.append("")
    md.append("| 族群 | 平均漲幅 | 漲停檔數 | 強勢檔數 | 題材 |")
    md.append("|------|---------|---------|---------|------|")
    for group, gdata in group_analysis:
        avg = gdata["avg_pct"]
        ls = gdata["limit_stocks"]
        ss = gdata["strong_stocks"]
        theme = group_themes.get(group, "-")
        md.append(f"| {group} | +{avg:.2f}% | {len(ls)} | {len(ss)} | {theme} |")
    md.append("")

    md.append("## 排行榜熱度總表")
    md.append("")
    md.append("| 概念 | 上榜檔數 | 三榜檔數 | 二榜檔數 | 題材 |")
    md.append("|------|---------|---------|---------|------|")
    for gdata in rank_analysis:
        g = gdata["group"]
        theme = rank_themes.get(g, "-")
        md.append(f"| {g} | {gdata['total']} | {gdata['three_count']} | {gdata['two_count']} | {theme} |")
    md.append("")

    return "\n".join(md)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    print("[步驟3] 計算連續天數...")
    streak = compute_consecutive_days()
    print(f"  完成，共 {len(streak)} 檔個股")

    ranking_streaks = compute_ranking_streaks()
    print(f"  排行榜連續天數完成，共 {len(ranking_streaks)} 檔")

    print("[步驟4b] 讀取分類並分析族群強勢...")
    override = load_override()
    group_analysis = analyze_groups(streak, override)
    print(f"  前 {len(group_analysis)} 強勢族群：", [g for g, _ in group_analysis])

    print("[步驟4c] 分析排行榜概念股...")
    rank_analysis = analyze_rankings(ranking_streaks, override)
    print(f"  前 {len(rank_analysis)} 個熱門概念：", [g["group"] for g in rank_analysis])

    overview = market_overview()
    print(f"  市場概況：{overview}")

    return group_analysis, rank_analysis, streak, ranking_streaks, override, overview


if __name__ == "__main__":
    main()
