// src/main.js
import { Actor, log } from 'apify';
import { chromium } from 'playwright';
import { selectAudioOnly } from './audio-picker.js';

await Actor.init();

try {
  // ---------- INPUT ----------
  const input = (await Actor.getInput()) ?? {};

  const sanitizeUrl = (u) =>
    String(u ?? '')
      .trim()
      .replace(/^[=\s]+/, '')
      .replace(/^[\u200B\u200C\u200D\uFEFF]+/, '')
      .replace(/[)"'\]\u00BB]+$/, '')
      .replace(/[\u00A0\u1680\u180E\u2000-\u200D\u202F\u205F\u3000]/g, '');

  const decodeHtmlEntities = (s) =>
    String(s ?? '')
      .replace(/&amp;/g, '&')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>');

  const normalize = (v) => sanitizeUrl(decodeHtmlEntities(v ?? ''));

  const shareUrlRaw = input.shareUrl || input.url || '';
  const shareUrl = sanitizeUrl(shareUrlRaw);

  // ВАЖНО: больше не срезаем ведущий "?" сразу.
  const passcodeRaw = String(input.passcode ?? '').trim();

  const passcodeCandidates = [
    ...new Set(
      [
        passcodeRaw,
        passcodeRaw.replace(/^\?/, ''),
      ].filter(Boolean)
    ),
  ];

  const maskSecret = (s) => {
    const v = String(s ?? '');
    if (!v) return '(empty)';
    if (v.length <= 2) return '*'.repeat(v.length);
    return `${v[0]}***${v[v.length - 1]}`;
  };

  log.info(`INPUT.shareUrl(raw): ${JSON.stringify(shareUrlRaw)}`);
  log.info(`INPUT.shareUrl(clean): ${JSON.stringify(shareUrl)}`);
  log.info(`INPUT.passcode.candidates=${JSON.stringify(passcodeCandidates.map(maskSecret))}`);

  let validUrl = null;
  try {
    validUrl = new URL(shareUrl).toString();
  } catch {}

  if (!validUrl) {
    await Actor.pushData({
      ok: false,
      error: 'INVALID_SHARE_URL',
      shareUrl,
    });
    await Actor.exit();
    process.exit(0);
  }

  // ---------- HELPERS ----------
  const isM4A = (u) => /\.m4a(\?|$)/i.test(u || '');
  const isMP4 = (u) => /\.mp4(\?|$)/i.test(u || '');
  const isVTT = (u) => /\.vtt(\?|$)/i.test(u || '');
  const isZip = (u) => /\.zip(\?|$)/i.test(u || '');

  const hasAudioHint = (u) =>
    /(\.m4a(\?|$)|audio_only|response-content-type=audio%2F|response-content-type=audio\/)/i.test(u || '');

  const hasVideoHint = (u) =>
    /(\.mp4(\?|$)|response-content-type=video%2F|response-content-type=video\/)/i.test(u || '');

  const hasSubtitleHint = (u) =>
    /(\.vtt(\?|$)|response-content-type=text%2Fvtt|response-content-type=text\/vtt)/i.test(u || '');

  const withTimeout = async (promise, ms, tag) => {
    let t;
    try {
      return await Promise.race([
        promise,
        new Promise((_, reject) => {
          t = setTimeout(() => reject(new Error(`${tag}_TIMEOUT`)), ms);
        }),
      ]);
    } finally {
      clearTimeout(t);
    }
  };

  const getAllFrames = (page) => [...new Set(page.frames())];
  const getAllContexts = (page) => [page, ...getAllFrames(page)];

  const firstVisible = async (ctx, locators) => {
    for (const locator of locators) {
      const count = await locator.count().catch(() => 0);
      for (let i = 0; i < count; i++) {
        const el = locator.nth(i);
        if (await el.isVisible().catch(() => false)) return el;
      }
    }
    return null;
  };

  const clickSmart = async (locator) => {
    if (!locator) return false;

    try {
      await locator.click({ timeout: 0 });
      await page.waitForTimeout(150);
      return true;
    } catch {}

    try {
      await locator.scrollIntoViewIfNeeded();
      await locator.click({ force: true });
      await page.waitForTimeout(150);
      return true;
    } catch {}

    try {
      await locator.evaluate((el) => el.click());
      await page.waitForTimeout(150);
      return true;
    } catch {}

    return false;
  };

  const getFileNameFromUrl = (u) => {
    try {
      const parsed = new URL(u);
      return parsed.pathname.split('/').pop() || null;
    } catch {
      const raw = String(u ?? '');
      return raw.split('/').pop()?.split('?')[0] || null;
    }
  };

  const saveDebugPage = async (page, keyPrefix) => {
    const html = await page.content().catch(() => '');
    const png = await page.screenshot({ fullPage: true }).catch(() => null);

    if (png) {
      await Actor.setValue(`${keyPrefix}.png`, png, { contentType: 'image/png' });
    }
    await Actor.setValue(`${keyPrefix}.html`, html, { contentType: 'text/html' });
  };

  const dumpVisibleInputs = async (page) => {
    const dump = [];

    for (const ctx of getAllContexts(page)) {
      const inputs = ctx.locator('input, textarea');
      const count = await inputs.count().catch(() => 0);

      for (let i = 0; i < count; i++) {
        const el = inputs.nth(i);
        const visible = await el.isVisible().catch(() => false);
        if (!visible) continue;

        const type = await el.getAttribute('type').catch(() => null);
        const name = await el.getAttribute('name').catch(() => null);
        const id = await el.getAttribute('id').catch(() => null);
        const placeholder = await el.getAttribute('placeholder').catch(() => null);

        dump.push({ type, name, id, placeholder });
      }
    }

    log.info(`VISIBLE_INPUTS=${JSON.stringify(dump)}`);
    return dump;
  };

  const extractMediaLinksFromHtml = async (page) => {
    const htmlRaw = await page.content().catch(() => '');
    const html = decodeHtmlEntities(htmlRaw);
    const base = page.url();
    const urls = new Set();

    for (const m of html.matchAll(/https?:\/\/[^\s"'<>]+?\.(?:m4a|mp4|vtt)(?:\?[^\s"'<>]*)?/ig)) {
      urls.add(normalize(m[0]));
    }

    for (const m of html.matchAll(/https?:\/\/[^\s"'<>]+?(?:\/rec\/download|\/cmr\/replay)[^\s"'<>]*/ig)) {
      urls.add(normalize(m[0]));
    }

    for (const m of html.matchAll(/href="([^"]+)"/ig)) {
      const href = decodeHtmlEntities(m[1] || '');
      if (/(\.m4a|\.mp4|\.vtt|\/rec\/download|\/cmr\/replay)/i.test(href)) {
        try {
          urls.add(new URL(href, base).toString());
        } catch {}
      }
    }

    return [...urls];
  };

  const getBodyText = async (page) => {
    return ((await page.textContent('body').catch(() => '')) || '').trim();
  };

  const findPasscodeField = async (ctx) => {
    return firstVisible(ctx, [
      ctx.getByPlaceholder(/код доступа|passcode/i),
      ctx.locator('input[placeholder*="Код доступа" i]'),
      ctx.locator('input[placeholder*="Passcode" i]'),
      ctx.locator('input[name*="passcode" i]'),
      ctx.locator('input[id*="password" i]'),
      ctx.locator('input[type="password"]'),
      ctx.locator('input[type="text"]'),
      ctx.locator('input:not([type="hidden"]):not([disabled])'),
    ]);
  };

  const findRecordingIndicator = async (ctx) => {
    return firstVisible(ctx, [
      // RU exact
      ctx.getByRole('button', { name: /загрузить\s*\(\s*\d+\s*файл/i }),
      ctx.getByRole('link', { name: /загрузить\s*\(\s*\d+\s*файл/i }),
      ctx.locator('a,button,[role="button"]').filter({ hasText: /загрузить\s*\(\s*\d+\s*файл/i }),
      ctx.getByText(/загрузить\s*\(\s*\d+\s*файл/i),

      // EN exact
      ctx.getByRole('button', { name: /download\s*\(\s*\d+\s*files?\s*\)/i }),
      ctx.getByRole('link', { name: /download\s*\(\s*\d+\s*files?\s*\)/i }),
      ctx.locator('a,button,[role="button"]').filter({ hasText: /download\s*\(\s*\d+\s*files?\s*\)/i }),
      ctx.getByText(/download\s*\(\s*\d+\s*files?\s*\)/i),

      // New Zoom player keeps bulk download inside More.
      ctx.getByRole('tab', { name: /^(recording files|файлы записи)$/i }),
      ctx.getByRole('button', { name: /^(download recording|скачать запись|загрузить запись)/i }),

      // Transcript panel
      ctx.getByText(/расшифровка аудиосвязи/i),
      ctx.getByText(/audio transcript/i),
    ]);
  };

  const detectPageState = async (page) => {
    const bodyText = await getBodyText(page);
    const title = await page.title().catch(() => '');
    const currentUrl = page.url();

    if (/Эта запись не существует|This recording does not exist/i.test(bodyText)) {
      return {
        state: 'recording_not_found',
        bodyText,
        title,
        currentUrl,
      };
    }

    for (const ctx of getAllContexts(page)) {
      const recordingIndicator = await findRecordingIndicator(ctx);
      if (recordingIndicator) {
        const indicatorText = await recordingIndicator.textContent().catch(() => '');
        return {
          state: 'recording',
          bodyText,
          title,
          currentUrl,
          indicatorText,
        };
      }
    }

    const looksLikePasscodeText =
      /Введите код доступа|Код доступа|Просмотр записи|Enter passcode|Passcode|View recording/i.test(bodyText);

    if (looksLikePasscodeText) {
      return {
        state: 'need_password',
        bodyText,
        title,
        currentUrl,
      };
    }

    for (const ctx of getAllContexts(page)) {
      const field = await findPasscodeField(ctx);
      if (field) {
        const placeholder = await field.getAttribute('placeholder').catch(() => null);
        const type = await field.getAttribute('type').catch(() => null);
        return {
          state: 'need_password',
          bodyText,
          title,
          currentUrl,
          placeholder,
          type,
        };
      }
    }

    return {
      state: 'unknown',
      bodyText,
      title,
      currentUrl,
    };
  };

  // ---------- BROWSER ----------
  log.info('STEP 1: launch chromium');

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    screen: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    acceptDownloads: true,
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  });

  await context.setExtraHTTPHeaders({
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Upgrade-Insecure-Requests': '1',
  });

  const page = await context.newPage();
  page.setDefaultNavigationTimeout(180_000);
  page.setDefaultTimeout(60_000);

  const blockHosts = [
    'googletagmanager',
    'google-analytics',
    'doubleclick',
    'hotjar',
    'mixpanel',
    'stats.zoom',
  ];

  await page.route('**/*', (route) => {
    const url = route.request().url();
    if (blockHosts.some((h) => url.includes(h))) return route.abort();
    return route.continue();
  });

  // ---------- NETWORK SNIFFER ----------
  const sniffedAudio = new Set();
  const sniffedVideo = new Set();
  const sniffedSubtitle = new Set();

  page.on('response', async (resp) => {
    try {
      const url = normalize(resp.url());
      const headers = await resp.allHeaders().catch(() => ({}));
      const contentType = String(headers['content-type'] || '');
      const contentDisposition = decodeHtmlEntities(String(headers['content-disposition'] || ''));

      if (
        hasAudioHint(url) ||
        /audio\/(mp4|x-m4a|aac|mpeg)/i.test(contentType) ||
        /\.m4a/i.test(contentDisposition)
      ) {
        sniffedAudio.add(url);
        return;
      }

      if (
        hasSubtitleHint(url) ||
        /text\/vtt/i.test(contentType) ||
        /\.vtt/i.test(contentDisposition)
      ) {
        sniffedSubtitle.add(url);
        return;
      }

      if (
        hasVideoHint(url) ||
        /^video\//i.test(contentType) ||
        /\.mp4/i.test(contentDisposition)
      ) {
        sniffedVideo.add(url);
      }
    } catch {}
  });

  // ---------- NAVIGATE ----------
  log.info('STEP 2: goto page');

  let gotoOk = false;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      await page.goto(validUrl, { waitUntil: 'commit', timeout: 180_000 });
      await page.waitForLoadState('domcontentloaded', { timeout: 60_000 }).catch(() => {});
      await page.waitForTimeout(1200);
      gotoOk = true;
      break;
    } catch (e) {
      if (attempt === 2) throw e;
      await page.waitForTimeout(3000);
    }
  }

  if (!gotoOk) throw new Error('NAVIGATION_FAILED');

  // ---------- COOKIE BANNERS ----------
  log.info('STEP 3: close possible cookie banners');

  for (let round = 0; round < 3; round++) {
    let clickedAny = false;

    for (const ctx of getAllContexts(page)) {
      const btn = await firstVisible(ctx, [
        ctx.getByRole('button', { name: /принять файлы cookie|accept cookies|accept all|принять|agree/i }),
        ctx.getByRole('button', { name: /отклонить файлы cookie|reject cookies|decline/i }),
        ctx.getByRole('button', { name: /закрыть|close/i }),
        ctx.locator('[aria-label*="close" i]'),
      ]);

      if (btn) {
        const clicked = await clickSmart(btn);
        if (clicked) clickedAny = true;
      }
    }

    if (!clickedAny) break;
    await page.waitForTimeout(700);
  }

  // ---------- DETECT PAGE ----------
  log.info('STEP 4: detect page state');

  let stateInfo = await detectPageState(page);
  log.info(
    `PAGE_STATE=${stateInfo.state}, url=${stateInfo.currentUrl}, title=${JSON.stringify(stateInfo.title)}, snippet=${JSON.stringify((stateInfo.bodyText || '').slice(0, 300))}`
  );

  if (stateInfo.state === 'recording_not_found') {
    await saveDebugPage(page, 'debug_recording_not_found');
    await Actor.pushData({
      ok: false,
      error: 'RECORDING_NOT_FOUND',
      finalUrl: page.url(),
    });
    await browser.close();
    await Actor.exit();
    process.exit(0);
  }

  if (stateInfo.state === 'unknown') {
    await page.waitForTimeout(5000);
    stateInfo = await detectPageState(page);
    log.info(
      `PAGE_STATE_RECHECK=${stateInfo.state}, url=${stateInfo.currentUrl}, title=${JSON.stringify(stateInfo.title)}, snippet=${JSON.stringify((stateInfo.bodyText || '').slice(0, 300))}`
    );
  }

  if (stateInfo.state === 'unknown') {
    await dumpVisibleInputs(page);
    await saveDebugPage(page, 'debug_unknown_initial_state');
    await Actor.pushData({
      ok: false,
      error: 'UNKNOWN_INITIAL_PAGE_STATE',
      finalUrl: page.url(),
    });
    await browser.close();
    await Actor.exit();
    process.exit(0);
  }

  // ---------- PASSCODE FLOW ----------
  if (stateInfo.state === 'need_password') {
    log.info('STEP 5: passcode flow');

    let passField = null;
    let passCtx = null;

    for (const ctx of getAllContexts(page)) {
      passField = await findPasscodeField(ctx);
      if (passField) {
        passCtx = ctx;
        break;
      }
    }

    if (!passField) {
      await saveDebugPage(page, 'debug_passcode_field_not_found');
      await Actor.pushData({
        ok: false,
        error: 'PASSCODE_FIELD_NOT_FOUND',
        finalUrl: page.url(),
      });
      await browser.close();
      await Actor.exit();
      process.exit(0);
    }

    if (!passcodeCandidates.length) {
      await Actor.pushData({
        ok: false,
        error: 'PASSCODE_REQUIRED',
        finalUrl: page.url(),
      });
      await browser.close();
      await Actor.exit();
      process.exit(0);
    }

    const ph = await passField.getAttribute('placeholder').catch(() => null);
    const tp = await passField.getAttribute('type').catch(() => null);
    log.info(`Passcode field found. placeholder=${JSON.stringify(ph)}, type=${JSON.stringify(tp)}`);

    let passcodeAccepted = false;

    for (const candidate of passcodeCandidates) {
      log.info(`Trying passcode candidate=${maskSecret(candidate)}`);

      await passField.fill('').catch(() => {});
      await page.waitForTimeout(100);
      await passField.fill(candidate).catch(() => {});
      await page.waitForTimeout(300);

      try {
        await passField.press('Enter');
        log.info('Passcode submitted via Enter');
      } catch (e) {
        log.warning(`Passcode Enter failed: ${String(e)}`);
      }

      await page.waitForTimeout(2000);

      let afterEnter = await detectPageState(page);
      // During navigation the new player briefly has no recording controls.
      for (let poll = 0; afterEnter.state === 'unknown' && poll < 20; poll++) {
        await page.waitForTimeout(500);
        afterEnter = await detectPageState(page);
      }
      log.info(`PAGE_STATE_AFTER_ENTER=${afterEnter.state}`);

      if (afterEnter.state !== 'recording') {
        const submitBtn = await firstVisible(passCtx, [
          passCtx.getByRole('button', { name: /^(confirm|подтвердить)$/i }),
          passCtx.getByRole('button', { name: /просмотр записи|view recording|access|view|open/i }),
          passCtx.getByText(/просмотр записи|view recording/i),
          passCtx.locator('button[type="submit"]'),
          passCtx.locator('button'),
        ]);

        if (submitBtn) {
          const clicked = await clickSmart(submitBtn);
          log.info(`Passcode submit button click result=${clicked}`);
          await page.waitForTimeout(2500);
        }
      }

      const afterPasscode = await detectPageState(page);
      log.info(`PAGE_STATE_AFTER_PASSCODE=${afterPasscode.state}`);

      if (afterPasscode.state === 'recording') {
        passcodeAccepted = true;
        break;
      }

      // если страница обновилась/перерисовалась — заново найдем поле
      for (const ctx of getAllContexts(page)) {
        const f = await findPasscodeField(ctx);
        if (f) {
          passField = f;
          passCtx = ctx;
          break;
        }
      }
    }

    if (!passcodeAccepted) {
      await saveDebugPage(page, 'debug_still_need_password');
      await Actor.pushData({
        ok: false,
        error: 'STILL_ON_NEED_PASSWORD_PAGE',
        finalUrl: page.url(),
        passcodeCandidatesTried: passcodeCandidates.map(maskSecret),
      });
      await browser.close();
      await Actor.exit();
      process.exit(0);
    }
  }

  // ---------- WAIT RECORDING PAGE ----------
  log.info('STEP 6: wait for recording page');

  let finalState = await detectPageState(page);
  if (finalState.state !== 'recording') {
    await page.waitForTimeout(5000);
    finalState = await detectPageState(page);
  }

  log.info(`FINAL_RECORDING_STATE=${finalState.state}`);

  if (finalState.state !== 'recording') {
    await saveDebugPage(page, 'debug_recording_page_not_ready');
    await Actor.pushData({
      ok: false,
      error: 'RECORDING_PAGE_NOT_READY',
      finalUrl: page.url(),
    });
    await browser.close();
    await Actor.exit();
    process.exit(0);
  }

  // ---------- FIND EXACT BULK DOWNLOAD BUTTON ----------
  log.info('STEP 7: find exact bulk download button');

  const findBulkDownloadButton = async () => {
    for (const ctx of getAllContexts(page)) {
      const btn = await firstVisible(ctx, [
        // RU exact only
        ctx.getByRole('button', { name: /загрузить\s*\(\s*\d+\s*файл/i }),
        ctx.getByRole('link', { name: /загрузить\s*\(\s*\d+\s*файл/i }),
        ctx.locator('a,button,[role="button"]').filter({ hasText: /загрузить\s*\(\s*\d+\s*файл/i }),
        ctx.getByText(/загрузить\s*\(\s*\d+\s*файл/i),

        // EN exact only
        ctx.getByRole('button', { name: /download\s*\(\s*\d+\s*files?\s*\)/i }),
        ctx.getByRole('link', { name: /download\s*\(\s*\d+\s*files?\s*\)/i }),
        ctx.locator('a,button,[role="button"]').filter({ hasText: /download\s*\(\s*\d+\s*files?\s*\)/i }),
        ctx.getByText(/download\s*\(\s*\d+\s*files?\s*\)/i),
      ]);

      if (btn) {
        const txt = await btn.textContent().catch(() => '');
        log.info(`Exact bulk download button found. text=${JSON.stringify(txt)}`);
        return btn;
      }
    }

    log.warning('Exact bulk download button NOT found');
    return null;
  };

  // ---------- CLICK & COLLECT DOWNLOADS ----------
  log.info('STEP 8: click exact bulk download and collect files');

  const collectDownloadsAfterSingleClick = async () => {
    const collected = [];
    const seen = new Set();

    const onDownload = (download) => {
      try {
        const suggestedFilename = normalize(download.suggestedFilename() || '');
        const url = normalize(download.url() || '');
        const key = `${suggestedFilename}__${url}`;
        if (seen.has(key)) return;
        seen.add(key);

        collected.push({
          suggestedFilename,
          url,
        });

        log.info(`DOWNLOAD event: ${JSON.stringify({ suggestedFilename, url })}`);
      } catch {}
    };

    page.on('download', onDownload);

    try {
      let btn = await findBulkDownloadButton();
      if (!btn) {
        const more = page.getByRole('button', { name: /^(more|ещё|еще)$/i });
        if (await more.isVisible().catch(() => false)) {
          await more.click({ timeout: 5000 });
          btn = await findBulkDownloadButton();
        }
      }
      if (!btn) return collected;

      const clicked = await clickSmart(btn);
      log.info(`Exact bulk download click result=${clicked}`);

      if (!clicked) {
        try {
          await btn.evaluate((el) => el.click());
          log.info('Exact bulk download JS click fallback used');
        } catch (e) {
          log.warning(`Exact bulk download JS click fallback failed: ${String(e)}`);
        }
      }

      await selectAudioOnly(page, log);
      await page.waitForTimeout(8000);
    } finally {
      page.off('download', onDownload);
    }

    return collected;
  };

  const bulkDownloads = await withTimeout(
    collectDownloadsAfterSingleClick(),
    60_000,
    'COLLECT_BULK_DOWNLOADS'
  ).catch((e) => {
    log.warning(`collectDownloads timeout/error: ${String(e)}`);
    return [];
  });

  log.info(`bulkDownloads.length=${bulkDownloads.length}`);
  log.info(`sniffedAudio.size=${sniffedAudio.size}`);
  log.info(`sniffedVideo.size=${sniffedVideo.size}`);
  log.info(`sniffedSubtitle.size=${sniffedSubtitle.size}`);

  // ---------- HTML FALLBACK ----------
  log.info('STEP 9: html/network fallback');

  let htmlLinks = [];
  if (!sniffedAudio.size && !bulkDownloads.some(item => hasAudioHint(item.url) || isM4A(item.suggestedFilename))) {
    htmlLinks = await extractMediaLinksFromHtml(page);
    log.info(`htmlLinks.length=${htmlLinks.length}`);
    log.info(`htmlLinks.preview=${JSON.stringify(htmlLinks.slice(0, 10))}`);
  }

  // ---------- PICK URLS ----------
  const audioCandidates = [];
  const videoCandidates = [];
  const subtitleCandidates = [];

  for (const item of bulkDownloads) {
    const name = normalize(item.suggestedFilename || '');
    const url = normalize(item.url || '');

    if (!url) continue;

    if (hasAudioHint(url) || isM4A(name) || isM4A(url)) {
      audioCandidates.push(url);
      continue;
    }

    if (hasSubtitleHint(url) || isVTT(name) || isVTT(url)) {
      subtitleCandidates.push(url);
      continue;
    }

    if (hasVideoHint(url) || isMP4(name) || isMP4(url)) {
      videoCandidates.push(url);
      continue;
    }
  }

  if (!audioCandidates.length && sniffedAudio.size) {
    audioCandidates.push(...sniffedAudio);
  }

  if (!videoCandidates.length && sniffedVideo.size) {
    videoCandidates.push(...sniffedVideo);
  }

  if (!subtitleCandidates.length && sniffedSubtitle.size) {
    subtitleCandidates.push(...sniffedSubtitle);
  }

  if (!audioCandidates.length && htmlLinks.length) {
    for (const url of htmlLinks) {
      if (hasAudioHint(url) || isM4A(url)) {
        audioCandidates.push(url);
        continue;
      }

      if (hasSubtitleHint(url) || isVTT(url)) {
        subtitleCandidates.push(url);
        continue;
      }

      if (hasVideoHint(url) || isMP4(url) || isZip(url)) {
        videoCandidates.push(url);
      }
    }
  }

  const audioUrl = audioCandidates[0] || null;
  const videoUrl = videoCandidates[0] || null;
  const subtitleUrl = subtitleCandidates[0] || null;

  // ---------- COOKIES ----------
  const cookies = await context.cookies();
  const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join('; ');

  const cloudFrontCookies = cookies.filter((c) => c.name.startsWith('CloudFront-'));
  const cloudFrontCookieHeader = cloudFrontCookies.map((c) => `${c.name}=${c.value}`).join('; ');

  // ---------- RESULT ----------
  const primaryUrl = audioUrl || videoUrl || subtitleUrl;

  if (!primaryUrl) {
    await saveDebugPage(page, 'debug_no_downloads');

    await Actor.pushData({
      ok: false,
      error: 'NO_FILES_CAPTURED',
      finalUrl: page.url(),
      bulkDownloadsCount: bulkDownloads.length,
      bulkDownloadsPreview: bulkDownloads.slice(0, 10),
      sniffedAudioCount: sniffedAudio.size,
      sniffedVideoCount: sniffedVideo.size,
      sniffedSubtitleCount: sniffedSubtitle.size,
      htmlLinksCount: htmlLinks.length,
      htmlLinksPreview: htmlLinks.slice(0, 10),
      audioCandidatesPreview: audioCandidates.slice(0, 10),
      videoCandidatesPreview: videoCandidates.slice(0, 10),
      subtitleCandidatesPreview: subtitleCandidates.slice(0, 10),
    });

    await browser.close();
    await Actor.exit();
    process.exit(0);
  }

  const result = {
    ok: true,
    finalUrl: page.url(),
    audio_url: audioUrl,
    video_url: videoUrl,
    subtitle_url: subtitleUrl,
    mediaType: audioUrl ? 'audio' : (videoUrl ? 'video' : 'subtitle'),
    filename: getFileNameFromUrl(primaryUrl),
    cookieHeader,
    cloudFrontCookieHeader,
    hasPolicyInAudioUrl: /(?:\?|&)Policy=/.test(audioUrl || ''),
    hasSignatureInAudioUrl: /(?:\?|&)Signature=/.test(audioUrl || ''),
    hasKeyPairIdInAudioUrl: /(?:\?|&)Key-Pair-Id=/.test(audioUrl || ''),
    hasCfPolicyCookie: /CloudFront-Policy=/.test(cloudFrontCookieHeader || ''),
    hasCfSignatureCookie: /CloudFront-Signature=/.test(cloudFrontCookieHeader || ''),
    hasCfKeyPairCookie: /CloudFront-Key-Pair-Id=/.test(cloudFrontCookieHeader || ''),
    bulkDownloadsCount: bulkDownloads.length,
    bulkDownloadsPreview: bulkDownloads.slice(0, 10),
    sniffedAudioCount: sniffedAudio.size,
    sniffedVideoCount: sniffedVideo.size,
    sniffedSubtitleCount: sniffedSubtitle.size,
    htmlLinksCount: htmlLinks.length,
    htmlLinksPreview: htmlLinks.slice(0, 10),
    audioCandidatesPreview: audioCandidates.slice(0, 10),
    videoCandidatesPreview: videoCandidates.slice(0, 10),
    subtitleCandidatesPreview: subtitleCandidates.slice(0, 10),
    passcodeCandidatesTried: passcodeCandidates.map(maskSecret),
  };

  log.info(`RESULT.mediaType=${result.mediaType}`);
  log.info(`RESULT.filename=${result.filename}`);
  log.info(`RESULT.bulkDownloads=${result.bulkDownloadsCount}`);
  log.info(`RESULT.sniffedAudio=${result.sniffedAudioCount}`);
  log.info(`RESULT.sniffedVideo=${result.sniffedVideoCount}`);
  log.info(`RESULT.sniffedSubtitle=${result.sniffedSubtitleCount}`);
  log.info(`RESULT.htmlLinks=${result.htmlLinksCount}`);
  log.info(`RESULT.hasPolicyInAudioUrl=${result.hasPolicyInAudioUrl}`);
  log.info(`RESULT.hasSignatureInAudioUrl=${result.hasSignatureInAudioUrl}`);
  log.info(`RESULT.hasKeyPairIdInAudioUrl=${result.hasKeyPairIdInAudioUrl}`);
  log.info(`RESULT.hasCfPolicyCookie=${result.hasCfPolicyCookie}`);
  log.info(`RESULT.hasCfSignatureCookie=${result.hasCfSignatureCookie}`);
  log.info(`RESULT.hasCfKeyPairCookie=${result.hasCfKeyPairCookie}`);

  await Actor.pushData(result);

  await browser.close();
} catch (err) {
  log.exception(err, 'UNCAUGHT');
  await Actor.pushData({
    ok: false,
    error: 'UNCAUGHT',
    message: String(err),
  });
} finally {
  await Actor.exit();
}
