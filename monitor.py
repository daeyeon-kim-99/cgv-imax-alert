"""
CGV 용산 특별관 시간표 오픈 알리미 (v6 — 간소화판)

무엇을 하나:
  용산아이파크몰의 IMAX / ULTRA 4DX / SCREENX 관에
  "그 날짜 회차가 열렸는가"만 감지해서 텔레그램으로 알림.
  잔여석은 안 봄. 날짜 + 관 + (참고용) 상영작 목록만 보냄.

v5 대비:
  - 잔여석 관련 필드 전부 제거 (검증 안 된 필드였음)
  - 영화 키워드 필터 기본 해제 → 관 단위로 감지 (키워드 오타로 놓치는 사고 방지)
  - state 키가 "관|날짜" 로 단순화
  - 응답 구조가 바뀌어도 --dump 로 바로 확인 가능

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

사용:
  python monitor.py                    # 1회 실행
  python monitor.py --loop             # 상주 실행 (권장)
  python monitor.py --interval 30      # loop 간격(초)
  python monitor.py --dump             # 응답 원본 구조 확인
  python monitor.py --test             # 텔레그램 연결만 테스트
"""

from __future__ import annotations

import argparse
import base64
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
# 순서 = 매칭 우선순위이자 알림 헤드라인 우선순위
WATCH_FORMATS: list[tuple[str, tuple[str, ...]]] = [
    # 주의: CGV API는 용산 3관을 "4DX관"으로만 표기한다 (ULTRA 4DX는 브랜드명일 뿐).
    # 용산 전용이므로 일반 "4DX"도 여기에 매핑한다. 다른 지점으로 바꾸면 재검토 필요.
    ("ULTRA 4DX", ("ULTRA4DX", "4DXSCREEN", "4DXWITH", "울트라4DX", "4DX")),
    ("IMAX",      ("IMAX",)),
    ("SCREENX",   ("SCREENX", "스크린X")),
]

# 카드 이미지 설정
CARD_ENABLED = True
FONT_BOLD = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"
FONT_REG = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"

# 특정 날짜만 볼 거면 여기에 나열 (비워두면 오늘부터 DAYS_AHEAD일)
TARGET_DATES: list[date] = [
    # date(2026, 8, 8),
    # date(2026, 8, 9),
]
DAYS_AHEAD = 21

# 특정 영화만 볼 거면 키워드 지정 (빈 리스트 = 전부)
MOVIE_KEYWORDS: list[str] = []

EMPTY_STREAK_STOP = 2            # 빈 날짜 연속 N개면 그 뒤는 안 봄
INTERVAL_SEC = 60
JITTER_SEC = 15
STATE_FILE = "state.json"

# ---- 응답 필드명. --dump 로 확인 후 맞지 않으면 여기만 고치면 됨 ----
FIELD_HALL = ("scnsNm", "scnsEnm", "screenNm", "theaterNm")   # 관 이름 후보
FIELD_TITLE = ("expoProdNm", "movNm", "movieNm", "prodNm")    # 영화 제목 후보
FIELD_TIME = ("scnsrtTm", "scnStartTm", "startTime")          # 상영 시작 시각 후보
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
def load_state() -> set[str]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"[warn] state 읽기 실패, 새로 시작: {e}", file=sys.stderr)
    return set()


def save_state(state: set[str]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(state), f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def state_key(fmt: str, day: date) -> str:
    return f"{fmt}|{day.isoformat()}"


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


# --------------------- 카드 이미지 ---------------------
_FONTS: dict | None = None


def _load_fonts():
    """폰트를 한 번만 로드해 재사용. 실패하면 None (카드 비활성)."""
    global _FONTS
    if _FONTS is not None:
        return _FONTS or None
    try:
        from PIL import ImageFont
        _FONTS = {
            "lbl": ImageFont.truetype(FONT_BOLD, 22),
            "head": ImageFont.truetype(FONT_BOLD, 42),
            "big": ImageFont.truetype(FONT_BOLD, 58),
            "body": ImageFont.truetype(FONT_REG, 30),
            "sm": ImageFont.truetype(FONT_REG, 24),
        }
    except Exception as e:
        print(f"[warn] 폰트 로드 실패, 텍스트 알림으로 대체: {e}", file=sys.stderr)
        _FONTS = {}
    return _FONTS or None


def flatten(alerts) -> list[tuple[date, str, str, str, dict]]:
    """(날짜, 관라벨, 관이름, 영화, row) 리스트. WATCH_FORMATS 순 → 날짜 순."""
    order = {lbl: i for i, (lbl, _) in enumerate(WATCH_FORMATS)}
    out = []
    for day in alerts:
        for label, triples in alerts[day].items():
            for hall, title, row in triples:
                out.append((day, label, hall, title, row))
    out.sort(key=lambda x: (order.get(x[1], 99), x[0]))
    return out


def render_card(items: list[tuple[date, str, str, str]]) -> bytes | None:
    fonts = _load_fonts()
    if not fonts:
        return None
    try:
        import io
        from PIL import Image, ImageDraw
    except Exception:
        return None

    day, label, hall, title, _row = items[0]
    rest = items[1:5]
    W = 800
    H = 340 + (52 * len(rest) + 30 if rest else 0)

    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 120], fill="#501313")
    d.text((40, 30), "CGV 용산아이파크몰", font=fonts["lbl"], fill="#F09595")
    d.text((40, 62), f"{label} 오픈", font=fonts["head"], fill="#FCEBEB")

    d.text((40, 155), f"{day.month}/{day.day}", font=fonts["big"], fill="#111111")
    wd = WEEKDAY_KR[day.weekday()] + "요일"
    d.text((40 + d.textlength(f"{day.month}/{day.day}", font=fonts["big"]) + 16, 180),
           wd, font=fonts["body"], fill="#666666")
    d.text((40, 240), title[:26], font=fonts["body"], fill="#111111")
    d.text((40, 285), hall[:34], font=fonts["sm"], fill="#666666")

    if rest:
        d.line([40, 345, W - 40, 345], fill="#DDDDDD", width=2)
        for i, (d2, lb2, _h2, t2, _r2) in enumerate(rest):
            y = 375 + i * 52
            tw = int(d.textlength(lb2, font=fonts["sm"])) + 24
            d.rounded_rectangle([40, y, 40 + tw, y + 38], radius=6, fill="#E6F1FB")
            d.text((52, y + 7), lb2, font=fonts["sm"], fill="#0C447C")
            d.text((60 + tw, y + 7), f"{d2.month}/{d2.day} · {t2[:16]}",
                   font=fonts["sm"], fill="#444444")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------- 텔레그램 ---------------------
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_caption(items: list[tuple[date, str, str, str, dict]]) -> str:
    day, label, hall, title, _row = items[0]
    lines = [
        f"\U0001F6A8 <b>{esc(label)} 열렸습니다</b>",
        "",
        esc(title),
        f"{day.month}/{day.day} ({WEEKDAY_KR[day.weekday()]}) · {esc(hall)}",
    ]
    rest = items[1:]
    if rest:
        lines += ["", "━━ 같이 열린 것"]
        for d2, lb2, _h2, t2, _r2 in rest[:8]:
            lines.append(f"· {esc(lb2)} · {d2.month}/{d2.day}({WEEKDAY_KR[d2.weekday()]}) · {esc(t2)}")
        if len(rest) > 8:
            lines.append(f"· 외 {len(rest) - 8}건")
    text = "\n".join(lines)
    return text[:1000]          # 캡션 상한 1024자


def _chat_ids() -> list[str]:
    return [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]


# CGV 예매 SPA가 sessionStorage.query에서 실제로 읽는 필드만 추려서 URL 길이를 줄인다.
# (--dump로 확인한 searchMovScnInfo row 필드와 대부분 이름이 겹침)
QUERY_FIELDS = (
    "coCd", "siteNo", "scnsNo", "scnYmd", "scnSseq", "scnsrtTm", "scnendTm",
    "prodNo", "salsTznCd", "movkndCd", "tcscnsGradCd", "sascnsGradCd",
    "movTirCd", "siteGradCd", "srvltKindCd", "movfNo", "prdcmpTypCd",
    "prdtypCd", "prddtlTypCd", "dblfrNo", "dblfrRpsntYn", "videoAddexpCd",
    "bzplcNo", "cxprdYn", "scnsGradCd", "speclIndctTypCd", "prcrulDivCd",
    "cratgClsCd", "cndSalYnList", "vatincYn", "slddKindCd", "iceconYn",
    "arthsYn", "srlsYn", "childnMovYn", "movNo", "movNm", "movkndDsplEnm",
    "expoProdNm",
)


def build_deeplink(row: dict) -> str | None:
    """
    row(searchMovScnInfo 원본)에서 CGV 예매 SPA의 sessionStorage.query가
    실제로 쓰는 필드만 추려 base64url 인코딩해 URL 해시에 담는다.
    해시는 서버로 전송되지 않으므로 CGV 쪽 라우팅/서버엔 영향이 없다.
    cgv-auto-seat.user.js (Tampermonkey/Userscripts)가 이 해시를 읽어서
    sessionStorage에 주입 후 좌석선택 화면으로 바로 이동시킨다.
    """
    try:
        payload = {k: row.get(k) for k in QUERY_FIELDS}
        payload["soldierJoinStus"] = "N"
        physc_path = row.get("physcFilePathnm")
        payload["prodImg"] = (
            f"https://cdn.cgv.co.kr/cgvpomsfilm/Movie/Thumbnail/Poster/{physc_path}"
            if physc_path else None
        )
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"{BOOK_URL}#auto={encoded}"
    except Exception as e:
        print(f"[warn] 딥링크 생성 실패: {e}", file=sys.stderr)
        return None


def build_keyboard(row: dict) -> dict:
    url = build_deeplink(row) or BOOK_URL
    return {"inline_keyboard": [[{"text": "\U0001F3AB 좌석선택 바로가기", "url": url}]]}


def _post(method: str, payload: dict, files=None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        resp = _session.post(url, data=payload, files=files, timeout=30)
    else:
        resp = _session.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def send_alert(alerts) -> None:
    items = flatten(alerts)
    caption = build_caption(items)
    png = render_card(items) if CARD_ENABLED else None
    keyboard = build_keyboard(items[0][4])

    for chat_id in _chat_ids():
        try:
            if png:
                _post("sendPhoto",
                      {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                       "reply_markup": json.dumps(keyboard)},
                      files={"photo": ("cgv.png", png, "image/png")})
            else:
                _post("sendMessage",
                      {"chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                       "disable_web_page_preview": True, "reply_markup": keyboard})
        except Exception as e:
            print(f"[warn] {chat_id} 전송 실패: {e}", file=sys.stderr)


def send_telegram(message: str) -> None:
    """--test 용 평문 전송."""
    for chat_id in _chat_ids():
        _post("sendMessage", {"chat_id": chat_id, "text": message,
                              "disable_web_page_preview": True})


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
def scan(state: set[str], verbose: bool = True) -> dict[date, dict[str, list[tuple[str, str, dict]]]]:
    """
    반환: {날짜: {관라벨: [(관이름, 영화, row), ...]}}  — 새로 열린 것만
    row는 딥링크 생성용 원본 API row (build_deeplink 참고).
    """
    alerts: dict[date, dict[str, list[tuple[str, str, dict]]]] = {}
    empty_streak = 0
    calls = 0

    for target in target_dates():
        pending = [
            label for label, _ in WATCH_FORMATS
            if state_key(label, target) not in state
        ]
        if not pending:
            continue

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

        # 관별로 상영작 수집 (같은 관+영화면 첫 회차 row를 대표로 보관)
        by_fmt: dict[str, dict[tuple[str, str], dict]] = {}
        for r in rows:
            label = detect_format(r)
            if not label:
                continue
            title = pick(r, FIELD_TITLE) or "(제목없음)"
            if MOVIE_KEYWORDS and not any(k in title for k in MOVIE_KEYWORDS):
                continue
            hall = pick(r, FIELD_HALL) or label
            by_fmt.setdefault(label, {}).setdefault((hall, title), r)

        if verbose:
            summary = ", ".join(f"{k}({len(v)})" for k, v in sorted(by_fmt.items())) or "-"
            print(f"[debug] {target} rows={len(rows)} → {summary}")

        for label in pending:
            entries = by_fmt.get(label)
            if entries:
                state.add(state_key(label, target))
                alerts.setdefault(target, {})[label] = [
                    (hall, title, row) for (hall, title), row in sorted(entries.items())
                ]

    if verbose:
        print(f"[debug] API 호출 {calls}회")
    return alerts


def build_message(alerts: dict[date, dict[str, list[tuple[str, str, dict]]]]) -> str:
    blocks = []
    for day in sorted(alerts):
        lines = [f"📅 {fmt_day(day)}"]
        for label in [lbl for lbl, _ in WATCH_FORMATS if lbl in alerts[day]]:
            lines.append(f"  • {label}")
            for hall, title, _row in alerts[day][label]:
                lines.append(f"      {hall} — {title}")
        blocks.append("\n".join(lines))
    return (
        "🚨 CGV 용산 특별관 시간표 오픈!\n\n"
        + "\n\n".join(blocks)
        + f"\n\n▶ {BOOK_URL}"
    )


def run_once(state: set[str], verbose: bool = True) -> bool:
    alerts = scan(state, verbose=verbose)
    stamp = datetime.now().strftime("%H:%M:%S")
    if not alerts:
        print(f"[{stamp}] no new openings")
        return False
    send_alert(alerts)
    save_state(state)
    print(f"[{stamp}] alert sent — {len(alerts)}개 날짜")
    return True


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
