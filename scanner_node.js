#!/usr/bin/env node
/* eslint-disable no-console */

const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const zlib = require("zlib");

const DEFAULT_DOMAINS = ["example.com"];
const DEFAULT_SCHEMES = ["https"];

const DEFAULT_INPUT_URL_LIST = "domains_and_subdomains_to_search_for.csv";
const DEFAULT_OUTPUT_CSV = "internallinksearch.csv";
const DEFAULT_SCANNED_PAGES_CSV = "scanned_pages.csv";

const DEFAULT_MAX_PAGES = 20000;
const DEFAULT_CONCURRENCY = 4;
const DEFAULT_REQUEST_TIMEOUT_SECS = 25;
const DEFAULT_POLITENESS_DELAY_SECS = 0.2;
const DEFAULT_USER_AGENT = "URL-Scanner/1.0 (+contact: your@email.com)";
const DEFAULT_MAX_MATCH_PAGES_PER_QUERY = 100;

const BINARY_EXTS = new Set([
  ".jpg",
  ".jpeg",
  ".png",
  ".gif",
  ".webp",
  ".svg",
  ".pdf",
  ".zip",
  ".rar",
  ".7z",
  ".mp4",
  ".mp3",
  ".mov",
  ".avi",
  ".mkv",
  ".woff",
  ".woff2",
  ".ttf",
  ".eot",
  ".otf",
  ".ico",
]);

const SITEMAP_CAP = 5000;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseArgs(argv) {
  const args = {
    testUrl: null,
    input: DEFAULT_INPUT_URL_LIST,
    output: DEFAULT_OUTPUT_CSV,
    noBody: false,
    ignoreInputs: false,
    maxPages: DEFAULT_MAX_PAGES,
    concurrency: DEFAULT_CONCURRENCY,
    domains: [...DEFAULT_DOMAINS],
  };

  const tokens = [...argv];
  while (tokens.length) {
    const token = tokens.shift();
    if (token === "--test-url") {
      args.testUrl = tokens.shift() || null;
    } else if (token === "--input") {
      args.input = tokens.shift() || args.input;
    } else if (token === "--output") {
      args.output = tokens.shift() || args.output;
    } else if (token === "--no-body") {
      args.noBody = true;
    } else if (token === "--ignore-inputs") {
      args.ignoreInputs = true;
    } else if (token === "--max-pages") {
      const value = Number(tokens.shift());
      if (!Number.isNaN(value)) {
        args.maxPages = value;
      }
    } else if (token === "--concurrency") {
      const value = Number(tokens.shift());
      if (!Number.isNaN(value)) {
        args.concurrency = value;
      }
    } else if (token === "--domains") {
      const domains = [];
      while (tokens.length && !tokens[0].startsWith("--")) {
        domains.push(tokens.shift());
      }
      if (domains.length) {
        args.domains = domains;
      }
    }
  }

  return args;
}

function saveListToFile(filename, items) {
  const sorted = Array.from(items).sort();
  fs.writeFileSync(filename, sorted.join("\n") + (sorted.length ? "\n" : ""));
  console.log(`Saved ${sorted.length} items to ${filename}`);
}

function sameDomain(url, domains) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return domains.some((domain) => host === domain || host.endsWith(`.${domain}`));
  } catch (error) {
    return false;
  }
}

function isBinaryUrl(url) {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    for (const ext of BINARY_EXTS) {
      if (pathname.endsWith(ext)) {
        return true;
      }
    }
    return false;
  } catch (error) {
    return false;
  }
}

function isSitemapLike(url) {
  const lower = (url || "").toLowerCase();
  if (lower.endsWith(".xml") || lower.endsWith(".xml.gz")) {
    return true;
  }
  if (lower.includes("sitemap")) {
    return true;
  }
  try {
    const parsed = new URL(url);
    for (const key of parsed.searchParams.keys()) {
      if (key.toLowerCase().includes("sitemap")) {
        return true;
      }
    }
  } catch (error) {
    return false;
  }
  return false;
}

function normalizeVariants(target) {
  const variants = new Set();
  const trimmed = (target || "").trim();
  if (!trimmed) {
    return variants;
  }

  variants.add(trimmed);

  const defrag = trimmed.split("#")[0];
  variants.add(defrag);

  try {
    const parsed = new URL(defrag);
    if (parsed.protocol && parsed.host) {
      variants.add(defrag.replace(`${parsed.protocol}//`, ""));
      if (parsed.hostname.startsWith("www.")) {
        const noWwwHost = parsed.hostname.slice(4);
        const noWww = defrag.replace(parsed.hostname, noWwwHost);
        variants.add(noWww);
        variants.add(noWww.replace(`${parsed.protocol}//`, ""));
      }
    }
  } catch (error) {
    // ignore URL parsing errors
  }

  for (const value of Array.from(variants)) {
    variants.add(encodeURI(value));
  }

  return new Set(Array.from(variants).filter((value) => value.length >= 6));
}

class Matcher {
  constructor(patterns) {
    this.patterns = Array.from(patterns);
  }

  findIn(text) {
    if (!text) {
      return new Set();
    }
    const found = new Set();
    for (const pattern of this.patterns) {
      if (text.includes(pattern)) {
        found.add(pattern);
      }
    }
    return found;
  }
}

function possibleBaseUrls(domain, schemes) {
  return schemes.map((scheme) => `${scheme}://${domain}`);
}

async function fetchBytes(url, timeoutMs, userAgent) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      headers: { "User-Agent": userAgent },
      redirect: "follow",
      signal: controller.signal,
    });
    const status = response.status;
    const headers = Object.fromEntries(response.headers.entries());
    if (status !== 200) {
      return { status, body: Buffer.alloc(0), headers };
    }
    const arrayBuffer = await response.arrayBuffer();
    let body = Buffer.from(arrayBuffer);
    const contentType = (headers["content-type"] || "").toLowerCase();
    const isGzip = url.toLowerCase().endsWith(".gz") || contentType.includes("gzip");
    if (isGzip) {
      try {
        body = zlib.gunzipSync(body);
      } catch (error) {
        // ignore gunzip errors, return raw
      }
    }
    return { status, body, headers };
  } catch (error) {
    return { status: 0, body: Buffer.alloc(0), headers: {} };
  } finally {
    clearTimeout(timeout);
  }
}

async function discoverSitemapUrls(domains, schemes, timeoutMs, userAgent) {
  const sitemapUrls = new Set();
  for (const domain of domains) {
    for (const base of possibleBaseUrls(domain, schemes)) {
      const robotsUrl = new URL("/robots.txt", base).toString();
      const { status, body } = await fetchBytes(robotsUrl, timeoutMs, userAgent);
      if (status === 200 && body.length) {
        const text = body.toString("utf-8");
        for (const line of text.split(/\r?\n/)) {
          if (line.toLowerCase().startsWith("sitemap:")) {
            const sitemap = line.split(":")[1]?.trim();
            if (sitemap) {
              sitemapUrls.add(sitemap);
            }
          }
        }
      }
      sitemapUrls.add(new URL("/sitemap.xml", base).toString());
    }
  }
  return sitemapUrls;
}

function extractSitemapLocs(xmlText) {
  const sitemapUrls = [];
  const pageUrls = [];
  const locRegex = /<loc>([^<]+)<\/loc>/gi;
  let match;
  while ((match = locRegex.exec(xmlText)) !== null) {
    const url = match[1].trim();
    if (!url) {
      continue;
    }
    if (isSitemapLike(url)) {
      sitemapUrls.push(url);
    } else {
      pageUrls.push(url);
    }
  }
  return { sitemapUrls, pageUrls };
}

async function gatherPagesFromSitemaps(domains, schemes, timeoutMs, userAgent, maxPages) {
  const pages = new Set();
  const candidateSitemaps = await discoverSitemapUrls(domains, schemes, timeoutMs, userAgent);

  console.log(`Discovered ${candidateSitemaps.size} candidate sitemap URLs`);
  saveListToFile("discovered_sitemaps_initial.txt", candidateSitemaps);

  const toProcess = Array.from(candidateSitemaps);
  const seen = new Set();
  let processedCount = 0;

  while (toProcess.length && pages.size < maxPages && processedCount < SITEMAP_CAP) {
    const sitemapUrl = toProcess.pop();
    if (seen.has(sitemapUrl)) {
      continue;
    }
    seen.add(sitemapUrl);
    processedCount += 1;

    const { status, body } = await fetchBytes(sitemapUrl, timeoutMs, userAgent);
    if (status !== 200 || !body.length) {
      continue;
    }

    const xmlText = body.toString("utf-8");
    if (!xmlText.trim().startsWith("<")) {
      continue;
    }

    const { sitemapUrls, pageUrls } = extractSitemapLocs(xmlText);
    for (const nested of sitemapUrls) {
      if (!seen.has(nested)) {
        toProcess.push(nested);
      }
    }

    for (const url of pageUrls) {
      if (sameDomain(url, domains) && !isBinaryUrl(url)) {
        pages.add(url.split("#")[0]);
        if (pages.size >= maxPages) {
          break;
        }
      }
    }
  }

  console.log(`Processed ${processedCount} sitemap documents; collected ${pages.size} page URLs.`);
  saveListToFile("discovered_sitemaps_all.txt", seen);
  saveListToFile("discovered_pages.txt", pages);
  return pages;
}

function extractHrefs(baseUrl, htmlText) {
  const hrefs = [];
  const hrefRegex = /<a\s+[^>]*href=["']?([^"' >]+)["']?[^>]*>/gi;
  let match;
  while ((match = hrefRegex.exec(htmlText)) !== null) {
    try {
      const abs = new URL(match[1], baseUrl).toString();
      hrefs.push(abs.split("#")[0]);
    } catch (error) {
      // ignore invalid URLs
    }
  }
  return hrefs;
}

async function fetchHtml(url, timeoutMs, userAgent, politenessDelaySecs) {
  await sleep(politenessDelaySecs * 1000);
  const { status, body } = await fetchBytes(url, timeoutMs, userAgent);
  if (status !== 200 || !body.length) {
    return { html: "", sizeBytes: 0 };
  }
  return { html: body.toString("utf-8"), sizeBytes: body.length };
}

function rebuildInputVariantMap(inputs) {
  const map = new Map();
  for (const input of inputs) {
    map.set(input, normalizeVariants(input));
  }
  return map;
}

function writeResults(outputCsv, inputs, matches, maxMatchPages) {
  const inputVariantMap = rebuildInputVariantMap(inputs);
  const headers = [
    "queried_url",
    "found",
    "match_count",
    ...Array.from({ length: maxMatchPages }, (_, idx) => `match_page_${idx + 1}`),
  ];

  const rows = [headers.join(",")];
  for (const input of inputs) {
    const pages = new Set();
    const variants = inputVariantMap.get(input) || new Set();
    for (const variant of variants) {
      const matchSet = matches.get(variant);
      if (matchSet) {
        for (const page of matchSet) {
          pages.add(page);
        }
      }
    }
    const pageList = Array.from(pages).sort().slice(0, maxMatchPages);
    const row = [
      input,
      String(pageList.length > 0),
      String(pageList.length),
      ...pageList,
      ...Array.from({ length: maxMatchPages - pageList.length }, () => ""),
    ];
    rows.push(row.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(","));
  }

  fs.writeFileSync(outputCsv, rows.join("\n") + "\n");
}

function writeScannedPages(outputCsv, scannedPages) {
  const headers = ["url", "size_bytes"];
  const rows = [headers.join(",")];
  for (const page of scannedPages) {
    const row = [page.url, String(page.sizeBytes)];
    rows.push(row.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(","));
  }
  fs.writeFileSync(outputCsv, rows.join("\n") + "\n");
}

async function runTestMode(testUrl, inputs, scanBody, timeoutMs, userAgent) {
  const allPatterns = new Set();
  for (const input of inputs) {
    for (const variant of normalizeVariants(input)) {
      allPatterns.add(variant);
    }
  }

  const { html } = await fetchHtml(testUrl, timeoutMs, userAgent, 0);
  if (!html) {
    console.log(`Test mode: failed to fetch ${testUrl}`);
    return;
  }

  const hrefs = extractHrefs(testUrl, html);
  const blobs = [hrefs.join("\n")];
  if (scanBody) {
    blobs.push(html);
  }
  const blob = blobs.join("\n");

  const matcher = new Matcher(allPatterns);
  const foundPatterns = matcher.findIn(blob);

  const variantMap = rebuildInputVariantMap(inputs);
  const resultRows = [];
  const matchedInputs = new Set();
  for (const original of inputs) {
    const variants = variantMap.get(original) || new Set();
    const found = Array.from(variants).some((variant) => foundPatterns.has(variant));
    if (found) {
      matchedInputs.add(original);
    }
    resultRows.push([original, String(found), found ? "1" : "0", found ? testUrl : ""]);
  }

  const csvLines = ["queried_url,found,match_count,match_page_1"];
  for (const row of resultRows) {
    csvLines.push(row.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(","));
  }
  fs.writeFileSync("test_results.csv", csvLines.join("\n") + "\n");

  console.log(`Test mode scanned: ${testUrl}`);
  console.log(`Inputs matched: ${matchedInputs.size} out of ${inputs.length}`);
  if (matchedInputs.size) {
    console.log("Example matches (up to 10):");
    Array.from(matchedInputs)
      .slice(0, 10)
      .forEach((match) => console.log(`  - ${match}`));
  }
  console.log("Wrote test_results.csv");
}

async function runScanner(args) {
  const inputPath = path.resolve(args.input);
  const inputLines = args.ignoreInputs
    ? []
    : fs
        .readFileSync(inputPath, "utf-8")
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);

  if (args.testUrl) {
    await runTestMode(args.testUrl, inputLines, !args.noBody, DEFAULT_REQUEST_TIMEOUT_SECS * 1000, DEFAULT_USER_AGENT);
    return;
  }

  const allPatterns = new Set();
  for (const input of inputLines) {
    for (const variant of normalizeVariants(input)) {
      allPatterns.add(variant);
    }
  }

  const matcher = new Matcher(allPatterns);
  const matches = new Map();
  for (const pattern of allPatterns) {
    matches.set(pattern, new Set());
  }

  const scannedPages = [];

  const pages = await gatherPagesFromSitemaps(
    args.domains,
    DEFAULT_SCHEMES,
    DEFAULT_REQUEST_TIMEOUT_SECS * 1000,
    DEFAULT_USER_AGENT,
    args.maxPages,
  );

  const pageList = Array.from(pages).slice(0, args.maxPages);
  const queue = [...pageList];
  const active = new Set();
  const progress = {
    completed: 0,
    lastLogged: 0,
    total: pageList.length,
  };

  function renderProgressBar(completed, total) {
    const safeTotal = Math.max(total, 1);
    const ratio = Math.min(completed / safeTotal, 1);
    const percent = Math.round(ratio * 100);
    const width = 30;
    const filled = Math.round(width * ratio);
    const bar = `${"█".repeat(filled)}${"░".repeat(width - filled)}`;
    const line = `Pages processed: [${bar}] ${percent}% (${completed}/${total})`;

    if (process.stdout.isTTY) {
      process.stdout.write(`\r${line}`);
      if (completed >= total) {
        process.stdout.write("\n");
      }
      return;
    }

    if (completed >= total || completed - progress.lastLogged >= 100) {
      console.log(line);
      progress.lastLogged = completed;
    }
  }

  async function worker(url) {
    try {
      if (!sameDomain(url, args.domains) || isBinaryUrl(url)) {
        return;
      }
      const { html, sizeBytes } = await fetchHtml(
        url,
        DEFAULT_REQUEST_TIMEOUT_SECS * 1000,
        DEFAULT_USER_AGENT,
        DEFAULT_POLITENESS_DELAY_SECS,
      );
      if (!html) {
        return;
      }

      scannedPages.push({ url, sizeBytes });
      const hrefs = extractHrefs(url, html);
      const blobs = [hrefs.join("\n")];
      if (!args.noBody) {
        blobs.push(html);
      }
      const found = matcher.findIn(blobs.join("\n"));
      for (const pattern of found) {
        const set = matches.get(pattern);
        if (set && set.size < DEFAULT_MAX_MATCH_PAGES_PER_QUERY) {
          set.add(url);
        }
      }
    } finally {
      active.delete(url);
      progress.completed += 1;
      renderProgressBar(progress.completed, progress.total);
    }
  }

  while (queue.length || active.size) {
    while (queue.length && active.size < args.concurrency) {
      const url = queue.shift();
      active.add(url);
      worker(url).catch(() => {});
    }

    await sleep(25);
  }

  if (!args.ignoreInputs) {
    writeResults(args.output, inputLines, matches, DEFAULT_MAX_MATCH_PAGES_PER_QUERY);
    console.log(`Wrote ${args.output}`);
  } else {
    console.log("Skipped writing internal link search results because inputs were ignored.");
  }
  writeScannedPages(DEFAULT_SCANNED_PAGES_CSV, scannedPages);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  await runScanner(args);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
