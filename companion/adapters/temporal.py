"""adapters.temporal —— 時間腕（C-2、read-path §4 補強）

クエリ中の日付・相対・季節表現を決定的パーサで [start, end) の ISO 窓に
変換し、records の有効時刻（valid_from 優先・created_at fallback）を
窓で走査して候補を供給する腕。

設計:
- パーサは決定的（LLM 不使用・外部状態不参照）。解析不能なら None で
  腕自体が発動しない
- 「春」や「5月」の年未指定は過去解釈に倒す（想起対象は過去の記憶
  なので、未来に窓が掛かる年は1年戻す）
- 数字の取り違えを避けるため、明示日付 > 相対期間 > 季節 > 曖昧語
  の優先順で最初の1つだけを採用する
"""

import re
from datetime import datetime, timedelta, timezone

from ..kernel import canon

_SEASON_MONTHS = {"春": (3, 5), "夏": (6, 8), "秋": (9, 11), "冬": (12, 2)}
_RELATIVE_DAYS = {
    "今日": 0, "昨日": 1, "一昨日": 2, "おととい": 2,
    "明日": -1, "あした": -1, "明後日": -2, "あさって": -2,
}
_WEEK_WORDS = {"先週": -1, "今週": 0, "来週": 1}
_MONTH_WORDS = {"先月": -1, "今月": 0, "来月": 1}
_YEAR_WORDS = {"一昨年": -2, "去年": -1, "昨年": -1,
               "今年": 0, "来年": 1, "明年": 1}
_NUM = "0123456789０１２３４５６７８９"


def _num(s: str) -> int:
    return int(str(s).translate(
        str.maketrans("０１２３４５６７８９", "0123456789")))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return canon.canon_ts(dt.astimezone(timezone.utc))


def _w(start: datetime, end: datetime, label: str) -> dict:
    return {"start": _iso(start), "end": _iso(end), "label": label}


def _month_add(y: int, m: int, d: int) -> tuple:
    m2 = m + d
    return y + (m2 - 1) // 12, (m2 - 1) % 12 + 1


def _season(y: int, name: str, now: datetime) -> dict:
    m1, m2 = _SEASON_MONTHS[name]
    tz = timezone.utc
    if m1 <= m2:
        start, end = (datetime(y, m1, 1, tzinfo=tz),
                      datetime(y, m2 + 1, 1, tzinfo=tz))
    else:  # 冬は年跨ぎ（12-2月）
        start, end = (datetime(y, m1, 1, tzinfo=tz),
                      datetime(y + 1, m2 + 1, 1, tzinfo=tz))
    # 想起は過去向き —— 終了が未来なら1年戻す
    if end > now and m1 <= m2:
        start, end = (datetime(y - 1, m1, 1, tzinfo=tz),
                      datetime(y - 1, m2 + 1, 1, tzinfo=tz))
    elif end > now:
        start, end = (datetime(y - 1, m1, 1, tzinfo=tz),
                      datetime(y, m2 + 1, 1, tzinfo=tz))
    return _w(start, end, f"{start.year}年{name}")


def parse_time_window(query: str, now_iso: str) -> dict | None:
    """クエリから時間窓を引く。検出できなければ None。

    now_iso: 基準時刻（canonical ISO文字列 or naive ISO可）。
    戻り値: {"start","end","label"}（UTC の ISO Z 文字列）。
    """
    q = query or ""
    try:
        now = canon.parse_ts(now_iso) if isinstance(now_iso, str) \
            else now_iso
    except Exception:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)

    # ---- 明示日付（最優先）
    m = re.search(rf"([{_NUM}]{{4}})\s*[-/年]\s*([{_NUM}]{{1,2}})\s*[-/月]"
                  rf"\s*([{_NUM}]{{1,2}})\s*日?", q)
    if m:
        y, mo, d = _num(m[1]), _num(m[2]), _num(m[3])
        try:
            s = datetime(y, mo, d, tzinfo=timezone.utc)
            return _w(s, s + timedelta(days=1), f"{y}-{mo:02d}-{d:02d}")
        except ValueError:
            pass
    m = re.search(rf"([{_NUM}]{{4}})\s*年\s*([{_NUM}]{{1,2}})\s*月", q)
    if m:
        y, mo = _num(m[1]), _num(m[2])
        if 1 <= mo <= 12:
            y2, m2 = _month_add(y, mo, 1)
            return _w(datetime(y, mo, 1, tzinfo=timezone.utc),
                      datetime(y2, m2, 1, tzinfo=timezone.utc),
                      f"{y}-{mo:02d}")
    m = re.search(rf"([{_NUM}]{{4}})\s*[-/]\s*([{_NUM}]{{1,2}})\b", q)
    if m:
        y, mo = _num(m[1]), _num(m[2])
        if 1 <= mo <= 12:
            y2, m2 = _month_add(y, mo, 1)
            return _w(datetime(y, mo, 1, tzinfo=timezone.utc),
                      datetime(y2, m2, 1, tzinfo=timezone.utc),
                      f"{y}-{mo:02d}")
    # ---- 季節（年指定があればその年、なければ直近の過去の季節。
    # bare 「YYYY年」より先に判定する —— 「2024年の夏」は季節窓）
    m = re.search(
        rf"(?:(?P<y>[{_NUM}]{{4}})\s*年|"
        rf"(?P<yw>一昨年|去年|昨年|今年|来年|明年))?"
        rf"\s*(?:の\s*)?(?P<se>春|夏|秋|冬)", q)
    if m and m.group("se"):
        if m["y"]:
            y = _num(m["y"])
        elif m["yw"]:
            y = now.year + _YEAR_WORDS[m["yw"]]
        else:
            y = now.year
        return _season(y, m["se"], now)

    m = re.search(rf"([{_NUM}]{{4}})\s*年", q)
    if m:
        y = _num(m[1])
        return _w(datetime(y, 1, 1, tzinfo=timezone.utc),
                  datetime(y + 1, 1, 1, tzinfo=timezone.utc), f"{y}年")

    # ---- 年未指定の M月D日 / M月（過去解釈に倒す）
    m = re.search(rf"(?<![{_NUM}\d])([{_NUM}]{{1,2}})\s*月\s*"
                  rf"([{_NUM}]{{1,2}})\s*日", q)
    if m:
        mo, d = _num(m[1]), _num(m[2])
        try:
            s = datetime(now.year, mo, d, tzinfo=timezone.utc)
            if s > now:
                s = s.replace(year=now.year - 1)
            return _w(s, s + timedelta(days=1), f"{s.year}-{mo:02d}-{d:02d}")
        except ValueError:
            pass
    m = re.search(rf"(?<![{_NUM}\d])([{_NUM}]{{1,2}})\s*月", q)
    if m:
        mo = _num(m[1])
        if 1 <= mo <= 12:
            y = now.year if mo <= now.month else now.year - 1
            y2, m2 = _month_add(y, mo, 1)
            return _w(datetime(y, mo, 1, tzinfo=timezone.utc),
                      datetime(y2, m2, 1, tzinfo=timezone.utc), f"{y}-{mo:02d}")

    # ---- 相対期間（長い語を先に —— 「一昨日」⊃「昨日」の substring
    # 誤中を避ける）
    for word, d in sorted(_RELATIVE_DAYS.items(),
                          key=lambda kv: -len(kv[0])):
        if word in q:
            s = today - timedelta(days=d)
            return _w(s, s + timedelta(days=1), word)
    m = re.search(rf"([{_NUM}\d]+)\s*日(前|くらい前|ぐらい前)", q)
    if m:
        s = today - timedelta(days=_num(m[1]))
        return _w(s, s + timedelta(days=1), f"{_num(m[1])}日前")
    for word, dw in _WEEK_WORDS.items():
        if word in q:
            base = today - timedelta(days=today.weekday())
            s = base + timedelta(weeks=dw)
            return _w(s, s + timedelta(weeks=1), word)
    m = re.search(rf"([{_NUM}\d]+)\s*週間?前", q)
    if m:
        s = today - timedelta(weeks=_num(m[1]))
        return _w(s, s + timedelta(days=7), f"{_num(m[1])}週間前")
    for word, dm in _MONTH_WORDS.items():
        if word in q:
            y, mo = _month_add(now.year, now.month, dm)
            y2, m2 = _month_add(y, mo, 1)
            return _w(datetime(y, mo, 1, tzinfo=timezone.utc),
                      datetime(y2, m2, 1, tzinfo=timezone.utc), word)
    m = re.search(rf"([{_NUM}\d]+)\s*(ヶ月|か月|カ月)前", q)
    if m:
        y, mo = _month_add(now.year, now.month, -_num(m[1]))
        y2, m2 = _month_add(y, mo, 1)
        return _w(datetime(y, mo, 1, tzinfo=timezone.utc),
                  datetime(y2, m2, 1, tzinfo=timezone.utc),
                  f"{_num(m[1])}ヶ月前")
    for word, dy in sorted(_YEAR_WORDS.items(),
                           key=lambda kv: -len(kv[0])):
        if word in q:
            y = now.year + dy
            return _w(datetime(y, 1, 1, tzinfo=timezone.utc),
                      datetime(y + 1, 1, 1, tzinfo=timezone.utc), word)
    m = re.search(rf"([{_NUM}\d]+)\s*年前", q)
    if m:
        y = now.year - _num(m[1])
        return _w(datetime(y, 1, 1, tzinfo=timezone.utc),
                  datetime(y + 1, 1, 1, tzinfo=timezone.utc),
                  f"{_num(m[1])}年前")

    # ---- 曖昧語（直近30日の緩い窓。精度より想起優先の最低保証）
    if any(w in q for w in ("最近", "近頃", "この間", "こないだ", "この前")):
        return _w(now - timedelta(days=30), now, "最近30日")

    return None


def coverage_pick(rows, tw, limit, buckets=8):
    """時間窓を buckets 等分して round-robin 選出（窓カバレッジ）。

    「去年の出来事」で類似度・新しさ順に取ると窓の1点に偏る問題への
    対策 —— 各バケットの先頭を順に取るので窓全体がカバーされる。

    rows: [(record_id, ts_iso), ...] 時刻降順。各バケット内の順序は
    rows の順序を継承する。戻り値は record_id のリスト（limit まで）。
    """
    s = canon.parse_ts(tw["start"])
    e = canon.parse_ts(tw["end"])
    span = max((e - s).total_seconds(), 1.0)
    qs = [[] for _ in range(buckets)]
    for rid, ts in rows:
        try:
            t = canon.parse_ts(ts)
        except Exception:
            continue
        b = int((t - s).total_seconds() / span * buckets)
        qs[min(max(b, 0), buckets - 1)].append(rid)
    out = []
    while len(out) < limit and any(qs):
        for q in qs:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out
