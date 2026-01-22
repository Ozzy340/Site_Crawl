# Site_Crawl

This repository contains two scanner implementations:

- `scanner.py`: the original Python-based crawler with sitemap-first discovery.
- `scanner_node.js`: a Node.js implementation with a matching feature set.

Both scanners accept the same CLI flags and produce the same output files so you can choose the runtime that best fits your environment.

## Python scanner (`scanner.py`)

The Python version is the original implementation and includes:

- Sitemap discovery via `robots.txt` plus `/sitemap.xml` fallback.
- Recursive sitemap parsing (including nested sitemap indexes).
- Page fetching with concurrency control, optional HTML body scanning, and binary URL skipping.
- Pattern normalization (URL variants) and matching with optional Aho-Corasick acceleration.
- CSV output that maps each input URL to pages where it was found.

### Usage

```bash
python3 scanner.py \
  --input domains_and_subdomains_to_search_for.csv \
  --output outputlist.csv \
  --domains example.com
```

### Test mode

```bash
python3 scanner.py \
  --test-url https://example.com \
  --input domains_and_subdomains_to_search_for.csv
```

### Python outputs

- `outputlist.csv`: consolidated match results.
- `discovered_sitemaps_initial.txt`: sitemap URLs found from `robots.txt` and `/sitemap.xml`.
- `discovered_sitemaps_all.txt`: all sitemap documents processed.
- `discovered_pages.txt`: page URLs discovered from sitemaps.
- `test_results.csv`: results for test mode.

## Node.js scanner (`scanner_node.js`)

The Node.js version mirrors the Python workflow and is useful when you want to run the scanner without Python dependencies. It performs:

- Sitemap discovery and nested sitemap traversal.
- Page fetching with concurrency limits and a politeness delay.
- URL pattern normalization and matching.
- CSV output compatible with the Python scanner.

### Usage

```bash
node scanner_node.js \
  --input domains_and_subdomains_to_search_for.csv \
  --output outputlist.csv \
  --domains example.com
```

### Crawl-only mode (ignore input list)

```bash
node scanner_node.js \
  --ignore-inputs \
  --domains example.com
```

In this mode the crawler still records every successfully fetched page and its size in `scanned_pages.csv`.

### Test mode

```bash
node scanner_node.js \
  --test-url https://example.com \
  --input domains_and_subdomains_to_search_for.csv
```

### Node.js outputs

The Node.js scanner writes the same output files as the Python scanner:

- `outputlist.csv`
- `scanned_pages.csv`: list of scanned pages with their byte sizes.
- `discovered_sitemaps_initial.txt`
- `discovered_sitemaps_all.txt`
- `discovered_pages.txt`
- `test_results.csv`

## Shared CLI options

Both scanners support the following flags:

- `--test-url <url>`: scan a single page instead of crawling sitemaps.
- `--input <path>`: input CSV of URL patterns to search for.
- `--output <path>`: output CSV for match results.
- `--no-body`: only scan anchor tags instead of raw HTML.
- `--ignore-inputs`: ignore the input URL list (crawl without searching for any patterns).
- `--max-pages <n>`: cap the number of pages scanned.
- `--concurrency <n>`: concurrent requests while scanning pages.
- `--domains <domain ...>`: one or more domains to include.
