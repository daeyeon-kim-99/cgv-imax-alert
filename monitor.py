"""
CGV 용산 특별관 오픈 / 취소표 알리미 (v7 — 간소화판)

무엇을 하나:
  용산아이파크몰의 IMAX / ULTRA 4DX / SCREENX 관을 감시해서
    1) "그 날짜 회차가 새로 열렸는가" (오픈 감지)
    2) "매진이었던 회차에 취소표가 생겼는가" (취소표 감지)
  두 가지를 텍스트로 텔레그램 알림.

v6 대비:
  - 좌석선택 딥링크 기능 전부 제거 (build_deeplink/build_keyboard, QUERY_FIELDS,
    cgv-auto-seat.user.js 연동) — 알림만 받고 예매는 직접 CGV 앱/사이트에서 진행
  - 카드 이미지(PNG) 렌더링 제거 (render_card, PIL/폰트 의존성) — 텍스트 메시지만 전송
    * 어차피 GitHub Actions 러너엔 Pillow가 설치돼있지 않아 실전에서는 항상
      텍스트로 폴백되고 있었음
  - 취소표 감지 추가: frSeatCnt(실시간 잔여석) 필드를 회차별로 추적하다가
    "잔여 0 → 잔여 N>0" 으로 바뀌는 순간만 알림 (매진 후 취소표 발생 시점).
    처음 보는 회차가 원래부터 빈 좌석이 있는 경우는 알림 대상 아님(오픈 감지가 이미 처리).

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

사용:
  python monitor.py                    # 1회 실행 (GitHub Actions 기본 실행 방식)
  python monitor.py --loop             # 상주 실행
  python monitor.py --interval 30      # loop 간격(초)
  python monitor.py --dump             # 응답 원본 구조 확인
  python monitor.py --test             # 텔레그램 연결만 테스트
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ===================== 설정 =====================
CO_CD = "A420"
SITE_NO = "0013"                 # 용산아이파크몰

# 감시할 특별관. 위에서부터 먼저 매칭됨 (순서 중요)
# 비교는 공백·하이픈 제거 + 대문자 기준
WATCH_FORMATS: list[tuple[str, tuple[str, ...]]] = [
    # 주의: CGV API는 용산 3관을 "4DX관"으로만 표기한다 (ULTRA 4DX는 브랜드명일 뿐).
    # 용산 전용이므로 일반 "4DX"도 여기에 매핑한다. 다른 지점으로 바꾸면 재검토 필요.
    ("ULTRA 4DX", ("ULTRA4DX", "4DXSCREEN", "4DXWITH", "울트라4DX", "4DX")),
    ("IMAX",      ("IMAX",)),
    ("SCREENX",   ("SCREENX", "스크린X")),
]

# 특정 날짜만 볼 거면 여기에 나열 (비워두면 오늘부터 DAYS_AHEAD일)
TARGET_DATES: list[date] = [
    # date(2026, 8, 8),
    # date(2026, 8, 9),
]
DAYS_AHEAD = 21

# 특정 영화만 볼 거면 키워드 지정 (빈 리스트 = 전부)
MOVIE_KEYWORDS: list[str] = []

EMPTY_STREAK_STOP = 2            # 빈 날짜 연속 N개면 그 뒤는 안 봄 (예매 지평선 바깥으로 판단)
INTERVAL_SEC = 60
JITTER_SEC = 15
STATE_FILE = "state.json"

# ---- 응답 필드명. --dump 로 확인 후 맞지 않으면 여기만 고치면 됨 ----
FIELD_HALL = ("scnsNm", "scnsEnm", "screenNm", "theaterNm")   # 관 이름 후보
FIELD_TITLE = ("expoProdNm", "movNm", "movieNm", "prodNm")    # 영화 제목 후보
FIELD_TIME = ("scnsrtTm", "scnStartTm", "startTime")          # 상영 시작 시각 후보
FIELD_SEATS = ("frSeatCnt", "frtmpSeatCnt")                   # 실시간 잔여석 후보
# ================================================

API_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
BOOK_URL = "https://cgv.co.kr/cnm/movieBook/cinema"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": BOOK_URL,
}
WEEKDAY_KR = "월화수목금토일"
KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    """GitHub 러너는 UTC라 date.today()가 한국보다 하루 전일 수 있다."""
    return datetime.now(KST).date()

_session = requests.Session()
_session.headers.update(HEADERS)


# --------------------- state ---------------------
def load_state() -> dict[str, set[str]]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):                 # v6 이전 state 마이그레이션
                return {"opened": set(raw), "soldout": set()}
            return {
                "opened": set(raw.get("opened", [])),
                "soldout": set(raw.get("soldout", [])),
            }
        except Exception as e:
            print(f"[warn] state 읽기 실패, 새로 시작: {e}", file=sys.stderr)
    return {"opened": set(), "soldout": set()}


def save_state(state: dict[str, set[str]]) -> None:
    tmp = STATE_FILE + ".tmp"
    payload = {"opened": sorted(state["opened"]), "soldout": sorted(state["soldout"])}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def open_key(fmt: str, day: date) -> str:
    return f"{fmt}|{day.isoformat()}"


def show_key(row: dict, day: date) -> str:
    """회차를 유일하게 식별하는 키 (관번호 + 그 관의 상영 순번)."""
    return f"{day.isoformat()}|{row.get('scnsNo', '')}|{row.get('scnSseq', '')}"


# --------------------- util ---------------------
def _flat(s: str) -> str:
    return "".join(s.split()).replace("-", "").replace("_", "").upper()


def pick(row: dict, candidates: tuple[str, ...]) -> str:
    """후보 필드명 중 값이 있는 첫 번째를 반환."""
    for key in candidates:
        v = row.get(key)
        if v:
            return str(v)
    return ""


def seats_left(row: dict) -> int:
    for key in FIELD_SEATS:
        v = row.get(key)
        if v not in (None, ""):
            try:
                return int(v)
            except ValueError:
                pass
    return 0


def detect_format(row: dict) -> str | None:
    """관 이름에서 특별관 라벨을 뽑는다. 감시 대상 아니면 None."""
    flat = _flat("".join(str(row.get(k) or "") for k in FIELD_HALL))
    for label, keys in WATCH_FORMATS:
        if any(_flat(k) in flat for k in keys):
            return label
    return None


def fmt_day(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}({WEEKDAY_KR[d.weekday()]})"


def fmt_time(t: str) -> str:
    return f"{t[:2]}:{t[2:]}" if t and len(t) == 4 and t.isdigit() else t


def target_dates() -> list[date]:
    if TARGET_DATES:
        return sorted(d for d in TARGET_DATES if d >= today_kst())
    today = today_kst()
    return [today + timedelta(days=i) for i in range(DAYS_AHEAD + 1)]


# --------------------- 텔레그램 ---------------------
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chat_ids() -> list[str]:
    return [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]


def _post(payload: dict) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = _session.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def send_telegram(text: str) -> None:
    for chat_id in _chat_ids():
        try:
            _post({
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as e:
            print(f"[warn] {chat_id} 전송 실패: {e}", file=sys.stderr)


def build_open_text(opens: dict[date, dict[str, list[tuple[str, str, str]]]]) -> str | None:
    if not opens:
        return None
    lines = ["\U0001F6A8 <b>CGV 용산 특별관 예매 오픈!</b>"]
    for day in sorted(opens):
        lines.append(f"\n\U0001F4C5 {fmt_day(day)}")
        for label in [lbl for lbl, _ in WATCH_FORMATS if lbl in opens[day]]:
            titles = ", ".join(sorted({title for _hall, title, _t in opens[day][label]}))
            lines.append(f"  • {esc(label)} — {esc(titles)}")
    lines.append(f"\n▶ {BOOK_URL}")
    return "\n".join(lines)


def build_cancel_text(cancels: list[tuple[date, str, str, str, str, int]]) -> str | None:
    if not cancels:
        return None
    lines = ["\U0001F3AB <b>취소표 발생!</b>"]
    for day, label, hall, title, start_time, left in cancels:
        lines.append(
            f"{fmt_day(day)} {fmt_time(start_time)} · {esc(label)} · {esc(hall)} "
            f"· {esc(title)} (잔여 {left}석)"
        )
    lines.append(f"\n▶ {BOOK_URL}")
    return "\n".join(lines)


# --------------------- fetch ---------------------
def extract_rows(payload) -> list[dict]:
    """응답에서 회차 리스트를 찾아낸다. 구조가 바뀌어도 웬만하면 잡히도록."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "list", "resultData", "result", "items"):
        v = payload.get(key)
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v
        if isinstance(v, dict):
            found = extract_rows(v)
            if found:
                return found
    # 마지막 수단: dict 값 중 dict 리스트인 것 아무거나
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def fetch_rows(target: date) -> list[dict]:
    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": target.strftime("%Y%m%d"),
        "rtctlScopCd": "08",
    }
    resp = _session.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    return extract_rows(resp.json())


# --------------------- scan ---------------------
def scan(
    state: dict[str, set[str]], verbose: bool = True
) -> tuple[dict[date, dict[str, list[tuple[str, str, str]]]], list[tuple[date, str, str, str, str, int]]]:
    """
    opens: {날짜: {관라벨: [(관이름, 영화, 시작시각), ...]}}  — 이번에 새로 열린 것만
    cancels: [(날짜, 관라벨, 관이름, 영화, 시작시각, 잔여석), ...] — 매진→잔여석 발생 건만
    """
    opened = state["opened"]
    soldout = state["soldout"]
    opens: dict[date, dict[str, list[tuple[str, str, str]]]] = {}
    cancels: list[tuple[date, str, str, str, str, int]] = []
    empty_streak = 0
    calls = 0

    for target in target_dates():
        try:
            rows = fetch_rows(target)
            calls += 1
        except Exception as e:
            print(f"[warn] {target} 조회 실패: {e}", file=sys.stderr)
            time.sleep(3)
            continue

        if not rows:
            empty_streak += 1
            if empty_streak >= EMPTY_STREAK_STOP and not TARGET_DATES:
                break                       # 예매 지평선 바깥
            continue
        empty_streak = 0

        matched = 0
        for r in rows:
            label = detect_format(r)
            if not label:
                continue
            title = pick(r, FIELD_TITLE) or "(제목없음)"
            if MOVIE_KEYWORDS and not any(k in title for k in MOVIE_KEYWORDS):
                continue
            matched += 1
            hall = pick(r, FIELD_HALL) or label
            start_time = pick(r, FIELD_TIME)

            # 오픈 감지: 그 날짜의 해당 관 포맷을 처음 보면 알림
            okey = open_key(label, target)
            if okey not in opened:
                opened.add(okey)
                opens.setdefault(target, {}).setdefault(label, []).append((hall, title, start_time))

            # 취소표 감지: 매진(잔여 0)이었던 회차가 잔여석 > 0 으로 바뀌면 알림
            sk = show_key(r, target)
            left = seats_left(r)
            if left <= 0:
                soldout.add(sk)
            elif sk in soldout:
                soldout.discard(sk)
                cancels.append((target, label, hall, title, start_time, left))

        if verbose:
            print(f"[debug] {target} rows={len(rows)} matched={matched}")

    if verbose:
        print(f"[debug] API 호출 {calls}회")
    return opens, cancels


def run_once(state: dict[str, set[str]], verbose: bool = True) -> bool:
    opens, cancels = scan(state, verbose=verbose)
    stamp = datetime.now().strftime("%H:%M:%S")

    sent = False
    open_text = build_open_text(opens)
    if open_text:
        send_telegram(open_text)
        sent = True
    cancel_text = build_cancel_text(cancels)
    if cancel_text:
        send_telegram(cancel_text)
        sent = True

    if sent:
        save_state(state)
        print(f"[{stamp}] alert sent — open={len(opens)}개 날짜, cancel={len(cancels)}건")
    else:
        print(f"[{stamp}] no changes")
    return sent


# --------------------- dump ---------------------
def dump(target: date) -> None:
    """응답 구조 / 관 이름 / 필드명 확인용."""
    params = {
        "coCd": CO_CD, "siteNo": SITE_NO,
        "scnYmd": target.strftime("%Y%m%d"), "rtctlScopCd": "08",
    }
    resp = _session.get(API_URL, params=params, timeout=15)
    print(f"URL    : {resp.url}")
    print(f"status : {resp.status_code}")
    try:
        payload = resp.json()
    except Exception:
        print("JSON 아님. 응답 앞부분:")
        print(resp.text[:1000])
        return

    if isinstance(payload, dict):
        print(f"최상위 키: {list(payload.keys())}")
    rows = extract_rows(payload)
    print(f"추출된 row 수: {len(rows)}")
    if not rows:
        print("\n-- 응답 전체 (앞부분) --")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        return

    print("\n-- 관 이름 / detect_format 결과 --")
    seen = {}
    for r in rows:
        name = " / ".join(str(r.get(k) or "") for k in FIELD_HALL)
        seen[name] = detect_format(r)
    for name, label in sorted(seen.items()):
        print(f"  {name:40} → {label}")

    print("\n-- row 샘플 (전체 필드) --")
    print(json.dumps(rows[0], ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=INTERVAL_SEC)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="--dump 시 오늘+N일")
    ap.add_argument("--test", action="store_true", help="텔레그램 연결만 확인")
    args = ap.parse_args()

    if args.dump:
        dump(today_kst() + timedelta(days=args.days))
        return
    if args.test:
        send_telegram("✅ CGV 알리미 연결 테스트")
        print("텔레그램 전송 성공")
        return

    state = load_state()

    if not args.loop:
        run_once(state)
        return

    print(f"loop 시작 — {args.interval}초(±{JITTER_SEC}s) 간격, Ctrl+C 종료")
    while True:
        try:
            run_once(state, verbose=False)
        except KeyboardInterrupt:
            print("\n종료")
            return
        except Exception as e:
            print(f"[warn] 사이클 실패: {e}", file=sys.stderr)
        try:
            time.sleep(max(10, args.interval + random.randint(-JITTER_SEC, JITTER_SEC)))
        except KeyboardInterrupt:
            print("\n종료")
            return


if __name__ == "__main__":
    main()
