// 사이트별 기본값. 실제 화면에서 검증되지 않은 '출발점'이며 설정 화면에서 바꿀 수 있다.
// 표 행을 넓게 잡아도 희망 조건(시험장·날짜) 글자로 걸러내므로 기본값은 일반적인 표 행으로 둔다.
(function (root) {
  "use strict";

  const COMMON = {
    slotRow: "table tbody tr",
    applyTexts: ["원서접수", "접수하기", "접수 신청", "신청하기"],
    blockers: ["#NetFunnel_Loading_Popup", "iframe[src*='recaptcha']", "img[src*='captcha' i]"],
    fullKeywords: ["마감", "매진", "불가"],
  };

  const SITES = {
    qnet: { name: "Q-Net", hosts: ["q-net.or.kr"], ...COMMON },
    kca: { name: "KCA (CQ-Net)", hosts: ["cq.or.kr"], ...COMMON },
    kisa: { name: "KISA", hosts: ["kisa.or.kr"], ...COMMON },
    // 테스트·연습용(모의 접수 화면). 실제 배포 manifest에는 localhost가 없다.
    mock: {
      name: "모의 접수(연습)",
      hosts: ["localhost", "127.0.0.1"],
      ...COMMON,
      slotRow: "#slots tbody tr.slot",
      blockers: ["#captcha"],
    },
  };

  function siteForHost(hostname) {
    const h = String(hostname || "").toLowerCase();
    for (const [key, s] of Object.entries(SITES)) {
      if (s.hosts.some((d) => h === d || h.endsWith("." + d))) return key;
    }
    return null;
  }

  const api = { SITES, siteForHost };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ExamSites = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
