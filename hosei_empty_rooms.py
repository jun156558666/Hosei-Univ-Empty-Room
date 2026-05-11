#!/usr/bin/env python3
"""
法政大学シラバスから空き教室を調べるバックエンド

【高速化の仕組み】
  listing ページ (web_search_show.php) の tr[data-href] 各行から
    td[0]  = 学部・研究科  → キャンパス推定
    td[7]  = 曜日・時限   → "月2/Mon.2" など
    td[10] = 教室名称     → "市BT-0505" など
  を直接取得。detail ページ (preview.php) は一切叩かない。

  さらに 42 コマ（6曜日×7時限）の listing スキャンを
  ThreadPoolExecutor で並列実行し、全体を 3〜5 分に短縮。

【使い方 (CLI)】
  python hosei_empty_rooms.py                    # 対話モード
  python hosei_empty_rooms.py -d 月 -p 3 -c 市ヶ谷
  python hosei_empty_rooms.py --rebuild-cache
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------
# 定数
# -----------------------------------------------------------------------
LISTING_URL  = "https://syllabus.hosei.ac.jp/web/web_search_show.php"

CURRENT_YEAR = datetime.now().year
YOUBI_LIST   = ["月", "火", "水", "木", "金", "土"]
JIGEN_LIST   = list(range(1, 8))
CAMPUS_LIST  = ["市ヶ谷", "多摩", "小金井"]


def _cache_dir() -> Path:
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).parent / "data"
    else:
        d = Path(__file__).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


CACHE_FILE = _cache_dir() / "hosei_rooms_cache.json"

# -----------------------------------------------------------------------
# 学部名 → キャンパス 対応表
# -----------------------------------------------------------------------
_KOGANEI = (
    "理工学部", "生命科学部", "デザイン工学部", "情報科学部",
    "理工学研究科", "生命科学研究科", "デザイン工学研究科", "情報科学研究科",
)
_TAMA = (
    "現代福祉学部", "スポーツ健康学部",
    "現代福祉研究科", "スポーツ健康研究科",
)


def _faculty_to_campus(faculty: str) -> str:
    for k in _KOGANEI:
        if k in faculty:
            return "小金井"
    for t in _TAMA:
        if t in faculty:
            return "多摩"
    return "市ヶ谷"   # 上記以外は市ヶ谷


# -----------------------------------------------------------------------
# HTTP セッション
# -----------------------------------------------------------------------
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HoseiEmptyRoomFinder/3.0"
)


def _fetch(url: str, params: dict, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"取得失敗: {exc}") from exc
            time.sleep(2 ** attempt)
    return ""


# -----------------------------------------------------------------------
# listing ページ解析
# tr[data-href] の各行から課目データを直接抽出
# -----------------------------------------------------------------------
def _parse_listing_page(html: str) -> tuple[dict[str, dict], int]:
    """
    listing ページ 1 枚から
      (courses, max_page) を返す。

    courses: {no_id: {"no_id", "faculty", "day_period", "campus", "classroom"}}

    td インデックス（確認済み）:
      [0]  学部・研究科
      [7]  曜日・時限
      [10] 教室名称
    """
    soup = BeautifulSoup(html, "lxml")
    courses: dict[str, dict] = {}

    for tr in soup.find_all("tr", attrs={"data-href": True}):
        # jp クラスの行だけ（en は重複）
        if "jp" not in tr.get("class", []):
            continue

        href = tr["data-href"]
        m_id = re.search(r"no_id=(\d+)", href)
        if not m_id:
            continue
        no_id = m_id.group(1)
        if no_id in courses:
            continue

        tds = tr.find_all("td")
        if len(tds) < 11:
            continue

        faculty    = tds[0].get_text(strip=True)
        day_period = tds[7].get_text(strip=True)
        classroom  = tds[10].get_text(strip=True)

        courses[no_id] = {
            "no_id":      no_id,
            "faculty":    faculty,
            "day_period": day_period,
            "campus":     _faculty_to_campus(faculty),
            "classroom":  classroom,
        }

    # 最大ページ番号
    max_page = 1
    for a in soup.find_all("a", href=True):
        m = re.search(r"page=(\d+)", str(a["href"]))
        if m:
            max_page = max(max_page, int(m.group(1)))

    return courses, max_page


# -----------------------------------------------------------------------
# 1コマ（曜日×時限）の全ページをスクレイプ
# -----------------------------------------------------------------------
def _scrape_combo(youbi: str, jigen: int, nendo: int) -> dict[str, dict]:
    params = {
        "search": "show",
        "nendo":  nendo,
        "t_mode": "pc",
        "sort":   "admin26_80",
        "youbi":  youbi,
        "jigen":  jigen,
    }

    html = _fetch(LISTING_URL, {**params, "page": 1})
    courses, total_pages = _parse_listing_page(html)

    for page in range(2, total_pages + 1):
        time.sleep(0.1)   # サーバー負荷軽減
        html = _fetch(LISTING_URL, {**params, "page": page})
        page_courses, _ = _parse_listing_page(html)
        for no_id, data in page_courses.items():
            courses.setdefault(no_id, data)

    return courses


# -----------------------------------------------------------------------
# 曜日・時限 パーサー
# "月2/Mon.2"                     → [("月", 2)]
# "月4/Mon.4・水5/Wed.5"           → [("月", 4), ("水", 5)]
# "集中・その他/intensive・other"   → []
# -----------------------------------------------------------------------
def parse_day_period(text: str) -> list[tuple[str, int]]:
    return [
        (m.group(1), int(m.group(2)))
        for m in re.finditer(r"([月火水木金土])(\d)", text)
    ]


# -----------------------------------------------------------------------
# キャッシュ
# -----------------------------------------------------------------------
def _load_cache(nendo: int) -> dict | None:
    if not CACHE_FILE.exists():
        return None
    with open(CACHE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("nendo") != nendo:
        return None
    # v3 フォーマット確認（"courses" キーが必須）
    if "courses" not in data:
        return None
    return data


def _save_cache(data: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -----------------------------------------------------------------------
# キャッシュ構築
# -----------------------------------------------------------------------
def build_cache(
    nendo: int,
    max_workers: int = 6,
    progress_cb=None,
) -> dict:
    """
    全 42 コマを並列スクレイプして課目データベースを構築・保存する。

    detail ページは叩かず listing ページだけで完結するため高速。
    progress_cb(done, total, msg) があれば進捗を通知する。
    """
    combos = [(y, j) for y in YOUBI_LIST for j in JIGEN_LIST]
    total  = len(combos)
    all_courses: dict[str, dict] = {}
    done_count = 0

    def _prog(msg: str):
        if progress_cb:
            progress_cb(done_count, total, msg)
        else:
            print(f"  [{done_count:2d}/{total}] {msg}")

    print(f"\n=== {nendo}年度 キャッシュ構築 (並列={max_workers}) ===\n")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_scrape_combo, y, j, nendo): (y, j)
            for y, j in combos
        }
        for future in as_completed(future_map):
            y, j = future_map[future]
            done_count += 1
            try:
                combo = future.result()
                new = sum(1 for nid in combo if nid not in all_courses)
                for nid, data in combo.items():
                    all_courses.setdefault(nid, data)
                _prog(f"{y}曜{j}限 完了 - {len(combo)} コース (新規 {new})")
            except Exception as exc:
                _prog(f"{y}曜{j}限 エラー: {exc}")

    courses_list = list(all_courses.values())
    cache_data = {
        "nendo":      nendo,
        "updated_at": datetime.now().isoformat(),
        "courses":    courses_list,
    }
    _save_cache(cache_data)
    print(f"\n完了: {len(courses_list)} コース → {CACHE_FILE}")
    return cache_data


# -----------------------------------------------------------------------
# 空き教室検索
# -----------------------------------------------------------------------
def find_empty_rooms(
    youbi: str,
    jigen: int,
    campus: str | None,
    nendo: int,
) -> tuple[list[str], list[str], list[str]]:
    """
    (空き教室リスト, 使用中教室リスト, 全教室リスト) を返す。
    campus が None または "全キャンパス" の場合はキャンパスフィルタなし。
    """
    cache = _load_cache(nendo)
    if cache is None:
        raise RuntimeError(
            "キャッシュがありません。先に「キャッシュ再構築」を実行してください。"
        )

    courses = cache["courses"]

    # キャンパスフィルタ
    if campus and campus != "全キャンパス":
        courses = [c for c in courses if c.get("campus") == campus]

    # 全教室（URL・空欄・ダッシュを除外）
    all_rooms: set[str] = set()
    for c in courses:
        room = c.get("classroom", "").strip()
        if room and not room.startswith("http") and room not in {"-", "―", ""}:
            all_rooms.add(room)

    # 該当コマで使用中の教室
    used_rooms: set[str] = set()
    for c in courses:
        if (youbi, jigen) in parse_day_period(c.get("day_period", "")):
            room = c.get("classroom", "").strip()
            if room and not room.startswith("http") and room not in {"-", "―", ""}:
                used_rooms.add(room)

    return sorted(all_rooms - used_rooms), sorted(used_rooms), sorted(all_rooms)


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="法政大学シラバスから空き教室を調べる",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  %(prog)s                            対話モード
  %(prog)s -d 月 -p 3 -c 市ヶ谷
  %(prog)s -d 金 -p 5 -y 2025
  %(prog)s --rebuild-cache
        """,
    )
    parser.add_argument("-d", "--youbi",  choices=YOUBI_LIST)
    parser.add_argument("-p", "--jigen",  type=int, choices=JIGEN_LIST)
    parser.add_argument("-c", "--campus", choices=["全キャンパス"] + CAMPUS_LIST,
                        default="全キャンパス")
    parser.add_argument("-y", "--nendo",  type=int, default=CURRENT_YEAR)
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--workers",      type=int, default=6)
    args = parser.parse_args()

    if args.rebuild_cache:
        build_cache(args.nendo, args.workers)
        return

    youbi = args.youbi or input(f"曜日 ({'/'.join(YOUBI_LIST)}): ").strip()
    if youbi not in YOUBI_LIST:
        sys.exit(f"エラー: 曜日は {'/' .join(YOUBI_LIST)} のいずれかです")

    jigen = args.jigen
    if not jigen:
        try:
            jigen = int(input(f"時限 (1〜{max(JIGEN_LIST)}): ").strip())
        except ValueError:
            sys.exit("エラー: 時限は整数で入力してください")
    if jigen not in JIGEN_LIST:
        sys.exit(f"エラー: 時限は 1〜{max(JIGEN_LIST)} の整数です")

    try:
        empty, used, all_rooms = find_empty_rooms(
            youbi, jigen, args.campus, args.nendo
        )
    except RuntimeError as e:
        sys.exit(str(e))

    campus_str = f"【{args.campus}】 " if args.campus != "全キャンパス" else ""
    bar = "═" * 58
    print(f"\n{bar}")
    print(f"  {args.nendo}年度 {campus_str}{youbi}曜{jigen}限 空き教室")
    print(bar)
    print(f"  全登録教室: {len(all_rooms):4d} 室")
    print(f"  使用中    : {len(used):4d} 室")
    print(f"  空き      : {len(empty):4d} 室")
    print(bar)
    if empty:
        w = max(len(r) for r in empty) + 2
        for i in range(0, len(empty), 4):
            print("  " + "".join(f"{r:<{w}}" for r in empty[i:i+4]))
    else:
        print("  空き教室なし")
    print()


if __name__ == "__main__":
    main()
