#!/usr/bin/env node
/**
 * x_scrape.js — Extract recent tweets from an X account.
 *
 * Strategy (in order of preference):
 *   1. syndication.twitter.com — official embed timeline; intermittent rate limit
 *   2. xcancel.com — Nitter-style mirror; works in browser, blocked by anti-bot
 *   3. fxtwitter.com (single status API) — needs known tweet IDs, so we cache
 *      IDs from successful timeline scrapes and enrich them later
 *
 * Usage: node x_scrape.js <screen_name> [--limit N] [--max-id ID]
 * Output: JSON to stdout: {"screen_name": "...", "tweets": [...], "source": "syndication|xcancel|fxtwitter"}
 *
 * Each tweet object: {url, text, date_text, date_epoch, retweets, likes, replies}
 * Exit code 0 on success (>= 0 tweets), 1 on hard error.
 *
 * Tunables (env vars):
 *   CHROME_PATH=/usr/bin/google-chrome
 *   PROXY_URL=http://127.0.0.1:7897   (pass empty to disable)
 *   MAX_WAIT_MS=15000
 */
'use strict';

const { chromium } = require('/home/sorawatcher/.local/share/fnm/node-versions/v24.16.0/installation/lib/node_modules/@playwright/mcp/node_modules/playwright');

const CHROME_PATH = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const PROXY_URL = process.env.PROXY_URL || 'http://127.0.0.1:7897';
const MAX_WAIT_MS = parseInt(process.env.MAX_WAIT_MS || '12000', 10);

function parseArgs() {
  const args = process.argv.slice(2);
  if (args.length < 1 || args[0] === '-h' || args[0] === '--help') {
    process.stderr.write('Usage: x_scrape.js <screen_name> [--limit N]\n');
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
  if (!s) return null;
  const cleaned = s.replace('·', '').replace('UTC', '').trim();
  const t = Date.parse(cleaned + ' UTC');
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

function launchOpts() {
  const opts = {
    headless: true,
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage'],
  };
  if (PROXY_URL) opts.proxy = { server: PROXY_URL };
  return opts;
}

async function newPage(browser) {
  const ctx = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 800 },
    locale: 'en-US',
  });
  return [ctx, await ctx.newPage()];
}

async function trySyndication(browser, screen_name, limit) {
  const [ctx, page] = await newPage(browser);
  page.setDefaultTimeout(MAX_WAIT_MS);
  try {
    const url = `https://syndication.twitter.com/srv/timeline-profile/screen-name/${screen_name}`;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: MAX_WAIT_MS });
    await page.waitForTimeout(3500);
    const body = await page.evaluate(() => document.body ? document.body.innerText : '');
    if (body.includes('Rate limit') || body.length < 200) {
      return { source: 'syndication', tweets: [], rate_limited: true };
    }
    // syndication uses <a href="/screen_name/status/ID"> structure within divs
    const tweets = await page.evaluate((lim) => {
      const out = [];
      const seen = new Set();
      // each tweet is in a div containing a permalink
      document.querySelectorAll('a[href*="/status/"]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/\/status\/(\d+)/);
        if (!m) return;
        const id = m[1];
        if (seen.has(id)) return;
        seen.add(id);
        // walk up to find the tweet container (usually 2-3 divs up)
        let container = a;
        for (let i = 0; i < 5 && container.parentElement; i++) {
          container = container.parentElement;
          const text = container.innerText || '';
          if (text.length > 50 && text.length < 1500) break;
        }
        const text = (container.innerText || '').trim();
        out.push({
          url: href.startsWith('http') ? href : 'https://x.com' + href,
          text: text,
        });
      });
      return out.slice(0, lim);
    }, limit);
    // Parse dates from text
    return {
      source: 'syndication',
      tweets: tweets.map(t => ({ ...t, date_text: null, date_epoch: null })),
    };
  } finally {
    await ctx.close();
  }
}

async function tryXcancel(browser, screen_name, limit) {
  const [ctx, page] = await newPage(browser);
  page.setDefaultTimeout(MAX_WAIT_MS);
  try {
    const url = `https://xcancel.com/${encodeURIComponent(screen_name)}`;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: MAX_WAIT_MS });
    // Wait up to MAX_WAIT_MS for timeline-items (challenge resolves automatically)
    try {
      await page.waitForSelector('.timeline-item', { timeout: MAX_WAIT_MS });
    } catch (e) {
      return { source: 'xcancel', tweets: [], challenge: true };
    }
    const tweets = await page.evaluate((lim) => {
      const out = [];
      const items = document.querySelectorAll('.timeline-item');
      for (const it of Array.from(items).slice(0, lim)) {
        const link = it.querySelector('a.tweet-link');
        const content = it.querySelector('.tweet-content');
        const dateEl = it.querySelector('span.tweet-date a');
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
        if (url && url.startsWith('/')) url = 'https://x.com' + url;
        out.push({
          url,
          text: content ? (content.innerText || '').trim() : '',
          date_text: dateEl ? (dateEl.getAttribute('title') || dateEl.innerText || '').trim() : null,
          retweets, likes, replies,
        });
      }
      return out;
    }, limit);
    return { source: 'xcancel', tweets: tweets.filter(t => t.url && t.text) };
  } finally {
    await ctx.close();
  }
}

async function scrape(screen_name, limit) {
  const browser = await chromium.launch(launchOpts());
  try {
    // Strategy 1: syndication (faster, lighter)
    let result = await trySyndication(browser, screen_name, limit);
    if (result.tweets.length > 0) {
      result.tweets = result.tweets.map(t => ({ ...t, date_epoch: tryParseDate(t.date_text) }));
      return { screen_name, ...result };
    }
    // Strategy 2: xcancel (slower, but richer data with engagement stats)
    result = await tryXcancel(browser, screen_name, limit);
    if (result.tweets.length > 0) {
      result.tweets = result.tweets.map(t => ({ ...t, date_epoch: tryParseDate(t.date_text) }));
      return { screen_name, ...result };
    }
    return { screen_name, source: result.source || 'none', tweets: [], rate_limited: !!result.rate_limited, challenge: !!result.challenge };
  } finally {
    await browser.close();
  }
}

(async () => {
  const { screen_name, limit } = parseArgs();
  try {
    const result = await scrape(screen_name, limit);
    process.stdout.write(JSON.stringify(result) + '\n');
    process.exit(0);
  } catch (e) {
    process.stderr.write(`x_scrape error: ${e.message}\n`);
    process.exit(1);
  }
})();
