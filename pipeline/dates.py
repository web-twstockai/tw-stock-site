"""民國／西元日期轉換與交易日工具。"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(TZ).date()


def now_taipei() -> datetime:
    return datetime.now(TZ)


def today_prices_cutoff() -> tuple[int, int]:
    """當日行情納入計畫的台北時間（HH, MM）；環境變數 PIPELINE_TODAY_PRICES_AFTER 優先，不合法則用 config。"""
    import os
    from pipeline import config
    for raw in (os.environ.get("PIPELINE_TODAY_PRICES_AFTER"), config.TODAY_PRICES_AFTER):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(raw or ""))
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return int(m.group(1)), int(m.group(2))
    return 15, 0


def include_today_prices(now: datetime | None = None) -> bool:
    """台北時間已過 today_prices_cutoff() → True（行情計畫終點為今天）。"""
    now = (now or now_taipei()).astimezone(TZ)
    h, m = today_prices_cutoff()
    return (now.hour, now.minute) >= (h, m)


def roc_ymd_to_iso(s: str) -> str | None:
    """1151109 / 115/11/09 / 115年11月09日 / 2026/11/10 → YYYY-MM-DD"""
    if not s:
        return None
    s = s.strip().replace(",", "")
    # 西元優先（四位數年）
    m = re.search(r"(20\d{2}|19\d{2})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    # 純數字 7–8 碼：YYYMMDD / YYYYMMDD
    m = re.fullmatch(r"(\d{7,8})", s)
    if m:
        digits = m.group(1)
        if len(digits) == 8:  # YYYYMMDD
            y, mo, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        else:  # YYYMMDD 民國
            y, mo, d = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def roc_ym_to_month(s: str) -> str | None:
    """11509 / 115/09 → YYYY-MM"""
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"(\d{2,3})(\d{2})", s) or re.search(r"(\d{2,3})\D+(\d{1,2})", s)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if y < 1911:
        y += 1911
    return f"{y:04d}-{mo:02d}"


def iso_to_roc_ym(ym: str) -> tuple[int, int]:
    y, m = ym.split("-")
    return int(y) - 1911, int(m)


def ad_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def ad_slash(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def month_iter(end_ym: str, n: int) -> list[str]:
    y, m = map(int, end_ym.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def quarter_iter(end_year: int, end_q: int, n: int) -> list[tuple[int, int]]:
    y, q = end_year, end_q
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    return out


def parse_number(s: str | None) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace(" ", "")
    if t in ("", "-", "--", "N/A", "n/a", "無", "---"):
        return None
    # strip HTML
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("+", "")
    try:
        return float(t)
    except ValueError:
        return None


def parse_int(s: str | None) -> int | None:
    v = parse_number(s)
    if v is None:
        return None
    return int(round(v))


def month_end(ym: str) -> date:
    """YYYY-MM → 該月最後一天。"""
    y, m = map(int, ym.split("-"))
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def revenue_announce_deadline(ym: str) -> date:
    """資料月 M 的法定公告期限：M+1 月 10 日。"""
    y, m = map(int, ym.split("-"))
    if m == 12:
        return date(y + 1, 1, 10)
    return date(y, m + 1, 10)


def revenue_month_phase(ym: str, as_of: date | None = None) -> str:
    """回傳 too_early／pending／due。
    - too_early：資料月尚未結束（as_of 仍在 M 月內或更早）→ 不應抓取
    - pending：M 已結束且 as_of ≤ 公告期限（M+1 月 10 日）→ 0／少列不算故障
    - due：as_of > 公告期限 → 0 列可視為 source_down
    """
    as_of = as_of or today()
    y, m = map(int, ym.split("-"))
    if (as_of.year, as_of.month) <= (y, m):
        return "too_early"
    if as_of <= revenue_announce_deadline(ym):
        return "pending"
    return "due"
