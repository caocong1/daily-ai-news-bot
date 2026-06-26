#!/usr/bin/env node
/**
 * fxtwitter_scrape.js — Extract recent tweets from an X account via xcancel.com
 * (a Nitter-style mirror that still works as of 2026 with a real Chrome UA).
 *
 * Usage: node fxtwitter_scrape.js <screen_name> [--limit N] [--json]
 * Output: JSON to stdout: {"screen_name": "...", "tweets": [...]}
 *
 * Each tweet object: {url, text, date_text, date_iso, retweets, likes, replies}
 * Exit code 0 on success (>= 0 tweets), 1 on hard error.
 *
 * Tunables (env vars):
 *   CHROME_PATH=/usr/bin/google-chrome  (default)
 *   PROXY_URL=http://127.0.0.1:7897     (default; pass empty to disable)
 *   MAX_WAIT_MS=15000                   (default; per-attempt wait)
 *   MAX_ATTEMPTS=3                      (default)
 */
'use strict';

const { chromium } = require('/home/sorawatcher/.local/share/fnm/node-versions/v24.16.0/installation/lib/node_modules/@playwright/mcp/node_modules/playwright');

const CHROME_PATH = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const PROXY_URL = process.env.PROXY_URL || 'http://127.0.0.1:7897';
const MAX_WAIT_MS = parseInt(process.env.MAX_WAIT_MS || '15000', 10);
const MAX_ATTEMPTS = parseInt(process.env.MAX_ATTEMPTS || '3', 10);

function parseArgs() {
  const args = process.argv.slice(2);
  if (args.length < 1 || args[0] === '-h' || args[0] === '--help') {
    console.error('Usage: fxtwitter_scrape.js <screen_name> [--limit N]');
    process.exit(1);
  }
  const screen_name = args[0];
  let limit = 20;
  for (let i = 1; i < args.length; i++) {
    if (args[i] === '--limit' && args[i + 1]) limit = parseInt(args[++i], 10);
  }
  return { screen_name, limit };
}

function tryParseDate(s) {
  // xcancel emits "Jun 22, 2026 · 5:04 PM UTC" — turn into epoch.
  if (!s) return null;
  const cleaned = s.replace('·', '').replace('UTC', '').trim();
  const t = Date.parse(cleaned + ' UTC');
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

async function scrape(screen_name, limit) {
  const url = `https://xcancel.com/${encodeURIComponent(screen_name)}`;
  const launchOpts = {
    headless: true,
    executablePath: CHROME_PATH,
    args: [
      '--no-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--disable-dev-shm-usage',
    ],
  };
  if (PROXY_URL) {
    launchOpts.proxy = { server: PROXY_URL };
  }
  const browser = await chromium.launch(launchOpts);
  try {
    const ctx = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      viewport: { width: 1280, height: 800 },
      locale: 'en-US',
    });
    const page = await ctx.newPage();
    page.setDefaultTimeout(MAX_WAIT_MS);

    let timeline = 0;
    let lastUrl = url;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: MAX_WAIT_MS });
      } catch (e) {
        if (attempt === MAX_ATTEMPTS) throw e;
        continue;
      }
      lastUrl = page.url();
      // wait up to MAX_WAIT_MS for timeline-items to appear (Cloudflare challenge resolves automatically)
      try {
        await page.waitForSelector('.timeline-item', { timeout: MAX_WAIT_MS });
        timeline = await page.locator('.timeline-item').count();
        break;
      } catch (e) {
        timeline = await page.locator('.timeline-item').count();
        if (timeline > 0) break;
        // check if it's the challenge page
        const bodyText = await page.evaluate(() => document.body ? document.body.innerText : '').catch(() => '');
        if (bodyText.includes('Verifying your request') || bodyText.includes('Rate limit')) {
          // wait longer for the JS challenge to clear
          await page.waitForTimeout(7000);
          timeline = await page.locator('.timeline-item').count();
          if (timeline > 0) break;
        }
      }
    }
    if (timeline === 0 && process.env.FXTWITTER_DEBUG) {
      process.stderr.write(`[debug] final url=${lastUrl}\n`);
      const bodyText = await page.evaluate(() => document.body ? document.body.innerText.substring(0, 500) : '').catch(() => '');
      process.stderr.write(`[debug] body: ${bodyText}\n`);
    }

    const tweets = await page.evaluate((lim) => {
      const out = [];
      const items = document.querySelectorAll('.timeline-item');
      for (const it of Array.from(items).slice(0, lim)) {
        const link = it.querySelector('a.tweet-link');
        const content = it.querySelector('.tweet-content');
        const dateEl = it.querySelector('span.tweet-date a');
        // engagement numbers
        const stats = it.querySelectorAll('.tweet-stats .tweet-stat');
        let replies = null, retweets = null, likes = null;
        for (const s of stats) {
          const lbl = (s.getAttribute('class') || '').toLowerCase();
          const num = (s.innerText || '').trim();
          if (lbl.includes('reply')) replies = num;
          else if (lbl.includes('retweet')) retweets = num;
          else if (lbl.includes('like')) likes = num;
        }
        let url = link ? link.getAttribute('href') : null;
        if (url && url.startsWith('/')) {
          url = 'https://x.com' + url;
        }
        out.push({
          url,
          text: content ? (content.innerText || '').trim() : '',
          date_text: dateEl ? (dateEl.getAttribute('title') || dateEl.innerText || '').trim() : null,
          retweets, likes, replies,
        });
      }
      return out;
    }, limit);

    // post-process: filter out empty / no-url
    const clean = tweets.filter(t => t.url && t.text).map(t => ({
      ...t,
      date_epoch: tryParseDate(t.date_text),
    }));
    return { screen_name, url, count: clean.length, tweets: clean };
  } finally {
    await browser.close();
  }
}

(async () => {
  const { screen_name, limit } = parseArgs();
  try {
    const result = await scrape(screen_name, limit);
    process.stdout.write(JSON.stringify(result, null, 0) + '\n');
    process.exit(0);
  } catch (e) {
    process.stderr.write(`fxtwitter_scrape error: ${e.message}\n`);
    process.exit(1);
  }
})();
