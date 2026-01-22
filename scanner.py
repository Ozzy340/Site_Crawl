# scanner.py
# Quadient URL Scanner — sitemap-first, nested sitemap aware, polite, with progress bars + test mode.

import asyncio
import csv
import gzip
import html
import time
import urllib.parse
import argparse
from contextlib import suppress
from typing import Dict, List, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET
import sys

import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup  # pip install beautifulsoup4
import urllib.robotparser as robotparser

# Silence "XML parsed as HTML" warnings from bs4 (we only use HTML parser here)
from bs4 import XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Optional accelerator (Aho-Corasick). If not installed, we fallback automatically.
with suppress(Exception):
    import ahocorasick  # type: ignore

# Optional progress bars via tqdm
HAS_TQDM = False
with suppress(Exception):
    from tqdm import tqdm
    HAS_TQDM = True


# ===================== CONFIG (defaults; can be overridden by CLI flags) =====================
DOMAINS = ["example.com" ]   # domains to include
SCHEMES = ["https"]                                # https first

INPUT_URL_LIST = "all-subdomains-and-domains.csv"                        # one target URL per line
OUTPUT_CSV = "outputlist.csv"

MAX_PAGES = 20000          # cap total pages scanned
CONCURRENCY = 4           # concurrent HTTP requests
REQUEST_TIMEOUT_SECS = 25
POLITENESS_DELAY_SECS = 0.2
USER_AGENT = "URL-Scanner/1.0 (+contact: your@email.com)"
MAX_MATCH_PAGES_PER_QUERY = 100

# If True, scan raw HTML (finds strings in text and attributes). If False, only scan <a href>s.
SCAN_HTML_BODY_TOO = True

# Skip obvious binaries
BINARY_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".rar",
    ".7z", ".mp4", ".mp3", ".mov", ".avi", ".mkv", ".woff", ".woff2", ".ttf",
    ".eot", ".otf", ".ico"
}

# Safety cap on number of sitemap XML docs to parse
SITEMAP_CAP = 5000
# ======================================================================


# --------------- helpers ---------------

def save_list_to_file(filename: str, items: Set[str]):
    """Write a set of URLs to a text file."""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            for item in sorted(items):
                f.write(item + "\n")
        print(f"Saved {len(items)} items to {filename}")
    except Exception as e:
        print(f"⚠️ Failed to save {filename}: {e}")

def same_domain(url: str, domains: List[str]) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in domains)
    except Exception:
        return False

def is_binary_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in BINARY_EXTS)

def is_sitemap_like(u: str) -> bool:
    """Treat URLs that look like sitemaps as sitemap docs to recurse into."""
    try:
        low = (u or "").lower()
        if low.endswith(".xml") or low.endswith(".xml.gz"):
            return True
        if "sitemap" in low:
            return True
        q = parse_qs(urlparse(u).query)
        return any("sitemap" in k.lower() for k in q.keys())
    except Exception:
        return False

def normalize_variants(target: str) -> Set[str]:
    """
    Build robust match variants for a URL-like string:
    - as-is, HTML-unescaped, defragmented
    - without scheme (drop https://)
    - without leading www.
    - URL-encoded versions of each variant
    """
    variants = set()
    s = (target or "").strip()
    if not s:
        return variants

    variants.add(s)

    s_unesc = html.unescape(s)
    variants.add(s_unesc)

    s_defrag, _ = urldefrag(s_unesc)
    variants.add(s_defrag)

    with suppress(Exception):
        p = urlparse(s_defrag)
        if p.netloc:
            if p.scheme:
                variants.add(s_defrag.replace(f"{p.scheme}://", "", 1))
            if p.netloc.startswith("www."):
                no_www = s_defrag.replace(p.netloc, p.netloc[4:], 1)
                variants.add(no_www)
                if p.scheme:
                    variants.add(no_www.replace(f"{p.scheme}://", "", 1))

    # URL-encoded forms
    enc = set()
    for v in list(variants):
        enc.add(urllib.parse.quote(v, safe=":/?#[]@!$&'()*+,;=%"))
    variants |= enc

    # avoid tiny tokens
    return {v for v in variants if len(v) >= 6}


class Matcher:
    """
    Fast multi-pattern matcher.
    Uses Aho-Corasick if available; otherwise falls back to simple substring checks.
    """
    def __init__(self, patterns: Set[str]):
        self.patterns_list = list(patterns)
        self.use_aho = False
        self.aho = None
        if 'ahocorasick' in globals():
            try:
                automaton = ahocorasick.Automaton()
                for idx, pat in enumerate(self.patterns_list):
                    automaton.add_word(pat, (idx, pat))
                automaton.make_automaton()
                self.aho = automaton
                self.use_aho = True
            except Exception:
                self.use_aho = False

    def find_in(self, text: str) -> Set[str]:
        if not text:
            return set()
        if self.use_aho and self.aho:
            found = set()
            for _, (idx, pat) in self.aho.iter(text):
                found.add(self.patterns_list[idx])
            return found
        # Fallback
        found = set()
        for p in self.patterns_list:
            if p in text:
                found.add(p)
        return found


# --------------- HTTP + sitemaps ---------------

def possible_base_urls(domain: str) -> List[str]:
    return [f"{scheme}://{domain}" for scheme in SCHEMES]

async def fetch_text_bytes(session: aiohttp.ClientSession, url: str) -> Tuple[int, bytes, Dict[str, str]]:
    """Fetch raw bytes (handles .gz files)."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            status = resp.status
            raw = await resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            if status != 200:
                return status, b"", headers
            ctype = headers.get("content-type", "").lower()
            is_gz_file = url.lower().endswith(".gz") or "application/gzip" in ctype or "application/x-gzip" in ctype
            if is_gz_file:
                with suppress(OSError):
                    raw = gzip.decompress(raw)
            return status, raw, headers
    except Exception:
        return 0, b"", {}

def decode_text(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return raw.decode("latin-1", errors="ignore")

async def discover_sitemap_urls(session: aiohttp.ClientSession, domain: str) -> Set[str]:
    """Find sitemap URLs via robots.txt and fallback to /sitemap.xml."""
    sitemap_urls: Set[str] = set()
    for base in possible_base_urls(domain):
        robots_url = urljoin(base, "/robots.txt")
        status, raw, _ = await fetch_text_bytes(session, robots_url)
        if status == 200 and raw:
            text = decode_text(raw)
            for line in text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm = line.split(":", 1)[1].strip()
                    if sm:
                        sitemap_urls.add(sm)
        sitemap_urls.add(urljoin(base, "/sitemap.xml"))
    return sitemap_urls

def _findall_any(elem: ET.Element, path_tail: str):
    # e.g., "sitemap/loc" or "url/loc" — ignore namespaces
    parts = path_tail.split("/")
    query = ".//" + "/".join([f"{{*}}{p}" for p in parts])
    return elem.findall(query)

def parse_sitemap_xml(xml_text: str) -> Tuple[List[str], List[str]]:
    """
    Returns (sitemap_urls, page_urls) from a given sitemap XML.
    - Works for <sitemapindex> and <urlset>
    - Reclassifies any <loc> that *looks like a sitemap* back into sitemap_urls
    """
    if not xml_text.strip():
        return [], []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []

    tag = root.tag
    if "}" in tag:
        tag = tag.split("}", 1)[1]

    sitemap_urls: List[str] = []
    page_urls: List[str] = []

    if tag.lower() == "sitemapindex":
        for loc in _findall_any(root, "sitemap/loc"):
            if loc.text:
                url = loc.text.strip()
                sitemap_urls.append(url)
    else:
        # Treat as urlset (some feeds sneak sitemap pages in here)
        for loc in _findall_any(root, "url/loc"):
            if not loc.text:
                continue
            url = loc.text.strip()
            if is_sitemap_like(url):
                sitemap_urls.append(url)  # recurse into it later
            else:
                page_urls.append(url)

    return sitemap_urls, page_urls

async def gather_pages_from_sitemaps(session: aiohttp.ClientSession, domains: List[str]) -> Set[str]:
    pages: Set[str] = set()

    # discover initial sitemap URLs
    candidate_sitemaps: Set[str] = set()
    for d in domains:
        candidate_sitemaps |= await discover_sitemap_urls(session, d)

    print(f"Discovered {len(candidate_sitemaps)} candidate sitemap URLs")
    save_list_to_file("discovered_sitemaps_initial.txt", candidate_sitemaps)

    # BFS through sitemap indexes (no reliance on file extension)
    to_process = list(candidate_sitemaps)
    seen_sitemaps: Set[str] = set()
    processed_count = 0

    # Progress bar for sitemap docs (best-effort total = SITEMAP_CAP)
    if HAS_TQDM:
        pbar = tqdm(total=SITEMAP_CAP, desc="Sitemaps", unit="doc")
    else:
        pbar = None
        last_log = time.time()

    try:
        while to_process and len(pages) < MAX_PAGES and processed_count < SITEMAP_CAP:
            sm_url = to_process.pop()
            if sm_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sm_url)
            processed_count += 1

            status, raw, headers = await fetch_text_bytes(session, sm_url)
            if status != 200 or not raw:
                if HAS_TQDM: pbar.update(1)
                else:
                    if time.time() - last_log > 2:
                        print(f"Sitemaps processed: {processed_count} | Pages collected: {len(pages)}")
                        last_log = time.time()
                continue

            # Only treat as sitemap if content looks like XML
            xml_text = decode_text(raw)
            if not xml_text.strip().startswith("<"):
                if HAS_TQDM: pbar.update(1)
                else:
                    if time.time() - last_log > 2:
                        print(f"Sitemaps processed: {processed_count} | Pages collected: {len(pages)}")
                        last_log = time.time()
                continue

            nested_sitemaps, url_pages = parse_sitemap_xml(xml_text)

            # enqueue nested sitemaps
            for u in nested_sitemaps:
                if u not in seen_sitemaps:
                    to_process.append(u)

            # collect page URLs
            for u in url_pages:
                if same_domain(u, domains) and not is_binary_url(u):
                    pages.add(urldefrag(u)[0])
                    if len(pages) >= MAX_PAGES:
                        break

            if HAS_TQDM: pbar.update(1)
            else:
                if time.time() - last_log > 2:
                    print(f"Sitemaps processed: {processed_count} | Pages collected: {len(pages)}")
                    last_log = time.time()
    finally:
        if HAS_TQDM and pbar is not None:
            pbar.close()

    print(f"Processed {processed_count} sitemap documents; collected {len(pages)} page URLs.")
    save_list_to_file("discovered_sitemaps_all.txt", seen_sitemaps)
    save_list_to_file("discovered_pages.txt", pages)
    return pages


# --------------- page fetching & scanning ---------------

def soup_from_html(html_text: str) -> BeautifulSoup:
    # Always use HTML parser for web pages; no external deps required.
    return BeautifulSoup(html_text, "html.parser")

class SiteScanner:
    def __init__(self, domains: List[str], patterns: Set[str]):
        self.domains = domains
        self.patterns = patterns
        self.matcher = Matcher(patterns)
        self.matches: Dict[str, Set[str]] = {p: set() for p in patterns}
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.session: aiohttp.ClientSession | None = None
        self.rp_by_domain: Dict[str, robotparser.RobotFileParser] = {}

    async def init(self):
        timeout = ClientTimeout(total=REQUEST_TIMEOUT_SECS)
        self.session = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT})

        # Load robots.txt parsers
        for d in self.domains:
            rp = robotparser.RobotFileParser()
            base = possible_base_urls(d)[0]
            robots_url = urljoin(base, "/robots.txt")
            with suppress(Exception):
                rp.set_url(robots_url)
                rp.read()
            self.rp_by_domain[d] = rp

    async def close(self):
        if self.session:
            await self.session.close()

    def allowed(self, url: str) -> bool:
        if not same_domain(url, self.domains):
            return False
        if is_binary_url(url):
            return False
        try:
            host = urlparse(url).netloc.lower()
            dom = next((d for d in self.domains if host == d or host.endswith("." + d)), None)
            if dom and dom in self.rp_by_domain:
                return self.rp_by_domain[dom].can_fetch(USER_AGENT, url)
        except Exception:
            pass
        return True

    async def fetch_html(self, url: str) -> str:
        assert self.session is not None
        async with self.sem:
            await asyncio.sleep(POLITENESS_DELAY_SECS)
            try:
                async with self.session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        return ""
                    raw = await resp.read()
                    return decode_text(raw)
            except Exception:
                return ""

    def extract_hrefs(self, base_url: str, html_text: str) -> List[str]:
        soup = soup_from_html(html_text)
        hrefs: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not href:
                continue
            absu = urljoin(base_url, href)
            absu, _ = urldefrag(absu)
            hrefs.append(absu)
        return hrefs

    async def scan_page(self, url: str):
        if not self.allowed(url):
            return
        html_text = await self.fetch_html(url)
        if not html_text:
            return

        blobs: List[str] = []

        # scan hrefs
        hrefs = self.extract_hrefs(url, html_text)
        if hrefs:
            blobs.append("\n".join(hrefs))

        # optionally scan raw HTML
        if SCAN_HTML_BODY_TOO:
            blobs.append(html_text)

        blob = "\n".join(blobs)
        found = self.matcher.find_in(blob)
        for pat in found:
            if len(self.matches[pat]) < MAX_MATCH_PAGES_PER_QUERY:
                self.matches[pat].add(url)

    async def run(self, page_urls: List[str]):
        # Filter disallowed upfront to get a stable total for the progress bar
        filtered = [u for u in page_urls if self.allowed(u)]
        total = len(filtered)
        if total == 0:
            return

        # Create all tasks at once; semaphore in fetch_html limits concurrency.
        tasks = [asyncio.create_task(self.scan_page(u)) for u in filtered]

        if HAS_TQDM:
            pbar = tqdm(total=total, desc="Scanning pages", unit="page")
        else:
            pbar = None
            last_log = time.time()
            done = 0

        try:
            for fut in asyncio.as_completed(tasks):
                await fut
                if HAS_TQDM:
                    pbar.update(1)
                else:
                    done += 1
                    now = time.time()
                    if now - last_log > 1.5:
                        pct = (done / total) * 100
                        print(f"Pages: {done}/{total} ({pct:.1f}%)")
                        last_log = now
        finally:
            if HAS_TQDM and pbar is not None:
                pbar.close()


def rebuild_input_variant_map(inputs: List[str]) -> Dict[str, Set[str]]:
    return {u: normalize_variants(u) for u in inputs}

def write_results(output_csv: str, inputs: List[str], matches: Dict[str, Set[str]]):
    input_to_variants = rebuild_input_variant_map(inputs)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        headers = ["queried_url", "found", "match_count"] + \
                  [f"match_page_{i}" for i in range(1, MAX_MATCH_PAGES_PER_QUERY + 1)]
        w.writerow(headers)

        for u in inputs:
            pages: Set[str] = set()
            for var in input_to_variants[u]:
                pages |= matches.get(var, set())
            page_list = sorted(pages)[:MAX_MATCH_PAGES_PER_QUERY]
            w.writerow([
                u,
                bool(page_list),
                len(page_list),
                *page_list,
                *([""] * (MAX_MATCH_PAGES_PER_QUERY - len(page_list)))
            ])


# --------------- TEST MODE ---------------

async def run_test_mode(test_url: str, inputs: List[str], scan_body: bool):
    """
    Fetch a single page and check if any of the input URLs (with variants) appear.
    Writes test_results.csv and prints a summary.
    """
    all_patterns: Set[str] = set()
    for u in inputs:
        all_patterns |= normalize_variants(u)

    timeout = ClientTimeout(total=REQUEST_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        # no robots / domain checks in test mode; it's explicit
        try:
            async with session.get(test_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    print(f"Test mode: HTTP {resp.status} for {test_url}")
                    return
                raw = await resp.read()
                html_text = decode_text(raw)
        except Exception as e:
            print(f"Test mode: fetch error for {test_url}: {e}")
            return

    # Build blob
    soup = soup_from_html(html_text)
    hrefs = []
    for a in soup.find_all("a", href=True):
        absu = urljoin(test_url, a.get("href"))
        absu, _ = urldefrag(absu)
        hrefs.append(absu)

    blobs = ["\n".join(hrefs)]
    if scan_body:
        blobs.append(html_text)
    blob = "\n".join(blobs)

    # Match
    matcher = Matcher(all_patterns)
    found_patterns = matcher.find_in(blob)

    # Join matches back to original inputs
    variant_map = rebuild_input_variant_map(inputs)
    results_rows = []
    matched_inputs = set()
    for original in inputs:
        pages = set()
        for var in variant_map[original]:
            if var in found_patterns:
                pages.add(test_url)
        if pages:
            matched_inputs.add(original)
        # Write one row per input (found True/False)
        results_rows.append([original, bool(pages), len(pages), *(list(pages)[:1])])

    # Save CSV
    with open("test_results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["queried_url", "found", "match_count", "match_page_1"])
        w.writerows(results_rows)

    print(f"Test mode scanned: {test_url}")
    print(f"Inputs matched: {len(matched_inputs)} out of {len(inputs)}")
    if matched_inputs:
        preview = list(sorted(matched_inputs))[:10]
        print("Example matches (up to 10):")
        for m in preview:
            print("  -", m)
    print("Wrote test_results.csv")


# --------------- CLI + main ---------------

def parse_args():
    p = argparse.ArgumentParser(description="Quadient URL Scanner (sitemap-first) with optional test mode")
    p.add_argument("--test-url", type=str, default=None,
                   help="Fetch just this URL and check for any input URLs on it (skips sitemap crawl).")
    p.add_argument("--input", type=str, default=INPUT_URL_LIST,
                   help=f"Path to input URL list (default: {INPUT_URL_LIST})")
    p.add_argument("--output", type=str, default=OUTPUT_CSV,
                   help=f"Path to output CSV (default: {OUTPUT_CSV})")
    p.add_argument("--no-body", action="store_true",
                   help="Do not scan raw HTML body (only <a href> links).")
    p.add_argument("--max-pages", type=int, default=MAX_PAGES, help=f"Max pages to scan (default: {MAX_PAGES})")
    p.add_argument("--concurrency", type=int, default=CONCURRENCY, help=f"Concurrency (default: {CONCURRENCY})")
    p.add_argument("--domains", type=str, nargs="*", default=DOMAINS,
                   help=f"Domains to include (default: {', '.join(DOMAINS)})")
    return p.parse_args()

async def main():
    args = parse_args()

    # apply CLI overrides
    global INPUT_URL_LIST, OUTPUT_CSV, SCAN_HTML_BODY_TOO, MAX_PAGES, CONCURRENCY, DOMAINS
    INPUT_URL_LIST = args.input
    OUTPUT_CSV = args.output
    SCAN_HTML_BODY_TOO = not args.no_body
    MAX_PAGES = args.max_pages
    CONCURRENCY = args.concurrency
    DOMAINS = args.domains

    # Load input URLs
    with open(INPUT_URL_LIST, "r", encoding="utf-8") as f:
        inputs = [line.strip() for line in f if line.strip()]

    # --- TEST MODE ---
    if args.test_url:
        await run_test_mode(args.test_url, inputs, SCAN_HTML_BODY_TOO)
        return

    # --- FULL CRAWL MODE ---
    all_patterns: Set[str] = set()
    for u in inputs:
        all_patterns |= normalize_variants(u)

    scanner = SiteScanner(DOMAINS, all_patterns)
    await scanner.init()

    start = time.time()
    try:
        assert scanner.session is not None
        # 1) discover page URLs from sitemaps (recursing through nested indexes)
        pages = await gather_pages_from_sitemaps(scanner.session, DOMAINS)
        page_list = list(pages)[:MAX_PAGES]

        # 2) scan pages for patterns (progress shown here)
        await scanner.run(page_list)
    finally:
        await scanner.close()

    elapsed = time.time() - start
    print(f"Scanned {len(page_list)} pages across {len(DOMAINS)} domains in {elapsed:.1f}s")

    # 3) write consolidated results
    write_results(OUTPUT_CSV, inputs, scanner.matches)
    print(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)

