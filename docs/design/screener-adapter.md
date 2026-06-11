# Screener.in Adapter — Verified Access Reality + Build Spec (research 2026-06-11)

All access facts below were verified live on 2026-06-11 with a Chrome-like UA.

## Verified access (anonymous, no login)

- **robots.txt (200)**: company pages PERMITTED. Disallowed: `/user/*`, `/*?q=`, `/*?sort=`, `/*?limit=`, `/*?page=`, `/company/source/quarter/*`. Note: the `/*?q=` wildcard catches the search API (`/api/company/search/?q=`) AND chart API — our robots-respecting fetcher will (correctly) refuse them.
- **Company page** `https://www.screener.in/company/RELIANCE/consolidated/` → 200, ~223 KB, **fully server-rendered**: top ratios (P/E, ROCE, ROE, mcap), 13 quarters of results, ~12y P&L/balance-sheet/cash-flow, quarterly shareholding pattern, and the ENTIRE **Documents section** with direct external links (83 bseindia.com links on RELIANCE): announcements (`bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname={uuid}.pdf`), annual reports FY13–FY26, credit ratings (crisil.com / icra.in / careratings.com), concall Transcript/PPT PDFs back to 2016 (REC = YouTube). Embedded ids: `data-company-id`, BSE code, NSE symbol.
- **Anonymous JSON/fragments verified 200**: `/api/company/{id}/peers/` (HTML fragment, real rows), `/api/company/{id}/schedules/?parent=Sales&section=quarters` (row expansion; robots-clean), sitemap-companies.xml (586 KB of slugs, monthly refresh material).
- **Login-gated**: AI concall summaries, site-wide /announcements/ feed, export, "hidden values", premium ratios. Avoid authenticated automation entirely (everything needed is anonymous; cleaner ToS posture).
- **ToS**: "personal, non-commercial transitory viewing"; no explicit scraping/bot clause. Risk: technical low (no Cloudflare/bot challenge observed), legal low-but-not-zero. Mitigations: low volume (daily per watched company), ingest actual documents from BSE/CRISIL/ICRA primary sources (screener is the *index*), no public redistribution of screener-derived data.

## Verified supplement: BSE announcements API (primary source, lower latency)

`https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=YYYYMMDD&strScrip={bse_code}&strSearch=P&strToDate=YYYYMMDD&strType=C&subcategory=-1` → 200 clean JSON (browser UA + `Referer: https://www.bseindia.com/` required). PDFs via `AnnPdfOpen.aspx?Pname={ATTACHMENTNAME}` — same URLs screener links. Use the `AnnSubCategoryGetData` variant (the older `AnnGetData` returns "No Record Found!").
NSE API worked anonymously today but is Akamai-fronted and notoriously unstable from datacenter IPs — do NOT architect around it; BSE covers the same filings. Moneycontrol robots-disallows financials pages (skip). Tickertape = viable fallback, less document-centric.

## Build spec

1. **`screener` source type** (config: `{slug: str, bse_code?: str, poll_interval_minutes: 1440}`) — one source row per watched company. Adapter mirrors RssAdapter (pure `parse_company_page(html)` over fetcher bytes):
   - Fetch `/company/{slug}/consolidated/` (fallback `/company/{slug}/`) via the polite Fetcher (≥5–10s between screener requests; no cache validators exist, so just rate-limit + cache).
   - **Structured snapshot**: parse the `data-table` sections (plain semantic HTML) → CompanySnapshot stored as a document (media_type `text`, office-extractor-style table-to-text: `## Quarterly results` + tab-joined rows, numbers verbatim) + diff vs previous poll (new quarter / shareholding change → discovered "update" item; payload archived as raw blob).
   - **Documents → DiscoveredItems**: every external href in the documents sections (BSE PDFs, rating pages, company-site links; YouTube RECs stored as links, not fetched) — these flow into the existing document_link + link-follower machinery; **fetched from BSE/CRISIL/etc., never from screener**. Dedupe on URL (BSE UUIDs stable).
   - **Skip** `/company/source/quarter/*` (robots-disallowed redirectors to the same BSE PDFs).
   - Entity wiring: get-or-create the company entity (type company), attach screener slug + BSE/NSE codes as aliases, edge document→entity mentions as usual via T1.
2. **`bse_announcements` supplement** (optional second adapter or same source row config flag): poll the BSE API per scrip every 1–6h for low-latency filings; items → ingest the PDFs directly.
3. **`company_lookup` investigation tool**: on-demand fetch (cache 15–60 min) returning the parsed snapshot for a company entity — grounds who-benefits/impacts analysis in actual financials.
4. **Name→slug resolution**: monthly sitemap ingest into a local name↔slug↔code table (robots-clean); slug = NSE symbol for NSE-listed, BSE code otherwise; `/consolidated/` presence in sitemap tells which view exists. The search API is robots-gray — policy decision: stick to the sitemap table (default) or allow user-triggered lookups.
5. **Politeness**: honor robots exactly (fetcher already does), browser UA, ≥5–10s per screener request, daily cadence per company, never crawl the universe, back off on 429/403, degrade gracefully if blocking appears.
6. **Frontend**: SourceForm screener variant (company autocomplete from the slug table), company entity pages show the latest snapshot (financial summary card) + Documents-derived corpus links.

## Unverified / watch-outs
Logged-in features (not needed), screener rate-limit thresholds (not probed), stability of frontend `/api/...` endpoints over time, behavior from other egress IPs.
