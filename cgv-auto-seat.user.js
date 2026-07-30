// ==UserScript==
// @name         CGV 좌석선택 바로가기
// @namespace    https://github.com/daeyeon-kim-99/cgv-imax-alert
// @version      1.1.0
// @description  텔레그램 알림의 #auto= 링크로 들어오면 극장/회차를 자동으로 채워넣고 좌석선택 화면으로 바로 이동시킨다.
// @match        https://cgv.co.kr/cnm/movieBook/cinema*
// @match        https://cgv.co.kr/cnm/selectVisitorCnt*
// @run-at       document-end
// @grant        none
// @updateURL    https://raw.githubusercontent.com/daeyeon-kim-99/cgv-imax-alert/main/cgv-auto-seat.user.js
// @downloadURL  https://raw.githubusercontent.com/daeyeon-kim-99/cgv-imax-alert/main/cgv-auto-seat.user.js
// ==/UserScript==

(function () {
  "use strict";

  // monitor.py의 SITE_NO/용산아이파크몰과 짝을 맞춘 상수 (단일 지점 전용).
  const SITE_NO = "0013";
  const SITE_NM = "용산아이파크몰";

  function decodeAuto(hash) {
    const params = new URLSearchParams(hash.replace(/^#/, ""));
    const b64url = params.get("auto");
    if (!b64url) return null;
    try {
      const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
      const pad = "=".repeat((4 - (b64.length % 4)) % 4);
      const binary = atob(b64 + pad);
      const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
      return JSON.parse(new TextDecoder("utf-8").decode(bytes));
    } catch (e) {
      console.warn("[cgv-auto-seat] decode failed", e);
      return null;
    }
  }

  // CGV 앱 설치 유도 배너: 정확한 마크업을 못 구해서 휴리스틱으로 처리.
  // 실기기에서 안 먹히면 실제 배너 스크린샷 보고 셀렉터를 보정해야 함.
  function dismissAppBanner() {
    const candidates = document.querySelectorAll(
      'button, a, [role="button"], [class*="close"], [aria-label*="닫기"]'
    );
    for (const el of candidates) {
      const text = (el.textContent || el.getAttribute("aria-label") || "").trim();
      const cls = el.className || "";
      if (/^(닫기|×|X|닫음)$/i.test(text) || (typeof cls === "string" && /close/i.test(cls))) {
        try {
          el.click();
        } catch (e) {
          /* noop */
        }
      }
    }
  }

  function injectAndGo(data) {
    const movStore = {
      siteNo: SITE_NO,
      siteNm: SITE_NM,
      mainCinema: "N",
      comCd: "all",
      comCdval: "00",
      comCdvalNm: "전체",
      scnYmd: data.scnYmd,
    };
    const siteStore = [
      {
        coCd: "A420",
        siteNo: SITE_NO,
        siteNm: SITE_NM,
        bzplcOperStusNm: "운영중",
        distance: null,
        isActive: true,
        isPrfer: false,
      },
    ];
    // CGV 앱이 "정상적인 단계별 진입"인지 확인하는 데 쓰는 내비게이션 히스토리.
    // 실제로 극장선택 화면을 거쳐 들어온 것처럼 최소 형태로 채워준다.
    // (없으면 앱이 비정상 진입으로 보고 홈으로 되돌리면서 React #185 크래시가 남)
    const navStack = JSON.stringify(["/", "/cnm/movieBook/cinema"]);

    sessionStorage.setItem("movStore", JSON.stringify(movStore));
    sessionStorage.setItem("siteStore", JSON.stringify(siteStore));
    sessionStorage.setItem("query", JSON.stringify(data));
    sessionStorage.setItem("cgvNavigationStack", navStack);
    sessionStorage.setItem("moviBookHistoryBack", navStack);

    // 해시를 지워서 뒤로가기/새로고침 시 재실행되지 않게 함.
    history.replaceState(null, "", location.pathname);

    setTimeout(() => {
      location.href = "/cnm/selectVisitorCnt";
    }, 300);
  }

  // 배너 자동 닫기는 두 페이지(극장선택 → 좌석선택) 모두에서 계속 감시.
  dismissAppBanner();
  const observer = new MutationObserver(dismissAppBanner);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("beforeunload", () => observer.disconnect());

  const data = decodeAuto(location.hash);
  if (data) {
    injectAndGo(data);
  }
})();
