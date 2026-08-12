/*
 * This file is part of g0v/OpenTransLive.
 * Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
 * Licensed under the GNU AGPL v3.0
 * See LICENSE for details.
 */
/* Fake feed for the RT viewer's demo mode.
 *
 * Synthesizes the payload the SSE endpoint emits (see
 * _process_transcription_update in app/__init__.py) and hands it to the page's
 * real transcription_update handler. Nothing about rendering is special-cased:
 * partial growth, commit, history trimming, language discovery and layout all
 * run through the same code as a live session — only the source of the events
 * differs.
 */

(function () {
  const LANGS = ['zh-Hant-TW', 'en-US', 'ja-JP', 'ko-KR'];

  const SCRIPT_LINES = [
    {
      'zh-Hant-TW': '大家好，歡迎來到今天的開源即時字幕示範。',
      'en-US': "Hello everyone, and welcome to today's open source live captioning demo.",
      'ja-JP': '皆さん、こんにちは。本日のオープンソース・リアルタイム字幕デモへようこそ。',
      'ko-KR': '여러분 안녕하세요, 오늘의 오픈소스 실시간 자막 데모에 오신 것을 환영합니다.',
    },
    {
      'zh-Hant-TW': '這個系統會把講者的語音即時轉成文字，再翻譯成多種語言。',
      'en-US': "This system turns the speaker's voice into text in real time, then translates it into several languages.",
      'ja-JP': 'このシステムは話者の音声をリアルタイムで文字にし、さらに複数の言語へ翻訳します。',
      'ko-KR': '이 시스템은 발표자의 음성을 실시간으로 텍스트로 바꾸고, 여러 언어로 번역합니다.',
    },
    {
      'zh-Hant-TW': '畫面上顏色比較淡的那一行，是還沒有定稿的即時結果。',
      'en-US': 'The lighter line at the bottom is the live partial result, which is not final yet.',
      'ja-JP': '画面の下に薄く表示されている行は、まだ確定していない途中結果です。',
      'ko-KR': '화면 아래쪽에 흐리게 보이는 줄은 아직 확정되지 않은 중간 결과입니다.',
    },
    {
      'zh-Hant-TW': '等到一句話講完，系統就會把它固定下來，往上推進歷史紀錄。',
      'en-US': 'Once a sentence is finished, the system commits it and pushes it up into the history.',
      'ja-JP': '文が終わると、システムはそれを確定し、履歴として上に送ります。',
      'ko-KR': '문장이 끝나면 시스템이 이를 확정하고 위쪽 기록으로 밀어 올립니다.',
    },
    {
      'zh-Hant-TW': '你可以用右上角的按鈕切換主題、調整字級，或選擇要顯示哪些語言。',
      'en-US': 'Use the buttons in the top right to switch themes, adjust the font size, or choose which languages to show.',
      'ja-JP': '右上のボタンで、テーマの切り替え、文字サイズの調整、表示する言語の選択ができます。',
      'ko-KR': '오른쪽 위 버튼으로 테마 전환, 글자 크기 조절, 표시할 언어 선택을 할 수 있습니다.',
    },
    {
      'zh-Hant-TW': '所有設定都會寫進網址，方便你把同一個畫面直接分享出去。',
      'en-US': 'Every setting is written into the URL, so you can share the exact same view with someone else.',
      'ja-JP': 'すべての設定は URL に保存されるので、同じ画面をそのまま共有できます。',
      'ko-KR': '모든 설정은 URL에 저장되므로 동일한 화면을 그대로 공유할 수 있습니다.',
    },
    {
      'zh-Hant-TW': '掃描 QR code，觀眾就能用自己的手機，看自己習慣的語言。',
      'en-US': 'Scan the QR code, and the audience can follow along in their own language on their own phone.',
      'ja-JP': 'QR コードを読み取れば、観客は自分のスマートフォンで好きな言語を読めます。',
      'ko-KR': 'QR 코드를 스캔하면 청중이 자신의 휴대폰으로 원하는 언어를 볼 수 있습니다.',
    },
    {
      'zh-Hant-TW': '這是一個社群協作的專案，程式碼完全公開在 GitHub 上。',
      'en-US': 'This is a community project, and the source code is completely open on GitHub.',
      'ja-JP': 'これはコミュニティで開発しているプロジェクトで、ソースコードは GitHub で公開されています。',
      'ko-KR': '이것은 커뮤니티가 함께 만드는 프로젝트이며, 소스 코드는 GitHub에 공개되어 있습니다.',
    },
    {
      'zh-Hant-TW': '無障礙不應該是額外的功能，而是每一場活動的基本配備。',
      'en-US': 'Accessibility should not be an add-on. It should be standard equipment at every event.',
      'ja-JP': 'アクセシビリティは追加機能ではなく、すべてのイベントの基本装備であるべきです。',
      'ko-KR': '접근성은 부가 기능이 아니라 모든 행사의 기본 장비여야 합니다.',
    },
    {
      'zh-Hant-TW': '歡迎一起加入，讓每一場活動都聽得懂、也看得見。',
      'en-US': "Join us, and let's make every event something everyone can follow and understand.",
      'ja-JP': 'ぜひご参加ください。すべてのイベントを、誰もが理解できるものにしましょう。',
      'ko-KR': '함께 참여해 주세요. 모든 행사를 누구나 이해할 수 있게 만들어 갑시다.',
    },
    {
      'zh-Hant-TW': '提醒一下，現在畫面上的文字全部都是假資料，只是為了展示效果。',
      'en-US': 'One reminder: everything on screen right now is fake data, generated purely for demonstration.',
      'ja-JP': 'ご注意ください。今画面に出ている文字は、すべてデモ用のダミーデータです。',
      'ko-KR': '참고로 지금 화면에 나오는 모든 텍스트는 시연용 가짜 데이터입니다.',
    },
    {
      'zh-Hant-TW': '這個示範會不斷循環播放，你可以放著讓它一直跑。',
      'en-US': 'This demo loops forever, so you can just leave it running.',
      'ja-JP': 'このデモはループし続けるので、そのまま流しておけます。',
      'ko-KR': '이 데모는 계속 반복되므로 그대로 켜 두셔도 됩니다.',
    },
  ];

  const TICK_MS = 240;          // one partial update per tick
  const MIN_STEPS = 6;          // ticks a short sentence takes to fill in
  const MAX_STEPS = 18;
  const MIN_PAUSE = 2;          // idle ticks between sentences (no partial on screen)
  const MAX_PAUSE = 5;

  // Reveal granularity: Latin and Hangul words appear whole, CJK appears one
  // character at a time — roughly how a real streaming translation grows.
  const TOKEN_RE = /[A-Za-z0-9][A-Za-z0-9'’·.,!?%:-]*\s*|[가-힯]+[.,!?]?\s*|\s+|./gsu;
  const tokenCache = new Map();

  function tokensOf(text) {
    let units = tokenCache.get(text);
    if (!units) {
      units = text.match(TOKEN_RE) || [text];
      tokenCache.set(text, units);
    }
    return units;
  }

  function prefixOf(text, ratio) {
    if (ratio >= 1) return text;
    const units = tokensOf(text);
    const n = Math.max(1, Math.round(units.length * ratio));
    return units.slice(0, n).join('').trimEnd();
  }

  function randInt(min, max) {
    return min + Math.floor(Math.random() * (max - min + 1));
  }

  function stepsFor(line) {
    const n = Math.round(line['zh-Hant-TW'].length / 3);
    return Math.min(MAX_STEPS, Math.max(MIN_STEPS, n));
  }

  /* emit(payload) receives objects shaped like the SSE `transcription_update`
   * data field. Runs until the page goes away. */
  window.startDemoFeed = function startDemoFeed(emit) {
    let index = 0;
    let step = 0;
    let steps = stepsFor(SCRIPT_LINES[0]);
    let pause = 0;
    let startTime = Date.now() / 1000;
    let lastCommitted = null;

    // Only the fields rt.html actually reads off an event: start_time (sort and
    // dedupe), partial, result.translated, last_committed. The real payload
    // carries more (id, text, corrected, end_time), but nothing in the viewer
    // consumes it, so synthesizing it would be noise.
    function buildPayload(line, ratio, isPartial) {
      const translated = {};
      for (const lang of LANGS) translated[lang] = prefixOf(line[lang], ratio);
      const payload = {
        start_time: startTime,
        partial: isPartial,
        result: { translated: translated },
      };
      if (lastCommitted) payload.last_committed = lastCommitted;
      return payload;
    }

    function tick() {
      if (pause > 0) { pause -= 1; return; }
      const line = SCRIPT_LINES[index];
      step += 1;

      if (step < steps) {
        emit(buildPayload(line, step / steps, true));
        return;
      }

      const committed = buildPayload(line, 1, false);
      // The server sends the freshly committed segment as its own
      // last_committed; mirror that so the client dedupe path is exercised.
      lastCommitted = Object.assign({}, committed);
      delete lastCommitted.last_committed;
      emit(committed);

      index = (index + 1) % SCRIPT_LINES.length;
      step = 0;
      steps = stepsFor(SCRIPT_LINES[index]);
      pause = randInt(MIN_PAUSE, MAX_PAUSE);
      // Keep start_time strictly increasing so the client's sort and dedupe
      // behave exactly as they do with real segments.
      startTime = Date.now() / 1000 + (pause * TICK_MS) / 1000;
    }

    setInterval(tick, TICK_MS);
  };
})();
