# Takeback – personal data archive

Import, parse and browse the "download your data" exports the platforms give
you (Facebook, Instagram, Google Takeout, Amazon, LinkedIn, Seazon, and more to
come) — a single self-hosted app.

## Scope

Takeback reads the **"Download your data" archives** platforms provide, turns
them into a clean, de-duplicated history per platform you can browse in the app
and, **optionally**, pushes numbers drawn from that data into InfluxDB so you
can cross them with everything else in Grafana ([InfluxDB
export](#influxdb-export-grafana)). Without InfluxDB the app is complete.

Takeback calls no service's API (no live sync, no stored platform credentials),
does not gather data from other tools, and does no analysis of its own: that is
Grafana's job.

Where a new source goes:

| The source is... | It goes... | Examples |
|---|---|---|
| a platform's whole "your data" archive: many categories, text, made to be browsed | into Takeback, as an import module | Meta, Google Takeout, Amazon (Kindle and Audible included), LinkedIn, Seazon, the Spotify export |
| measurements to plot or cross-reference, from the source's API and/or its export file | into a dedicated sync tool, which writes to InfluxDB | Renpho, Whoop, MyTherapy, MyFitnessPal, Audiobookshelf (see [Tributary](https://github.com/vchatela-org/tributary)) |
| a tool that already has its own storage and interface | nowhere: it stays as it is | garmin-grafana, Tautulli, Your Spotify |

Manual or automatic is not the dividing line, only how data arrives; three
rules settle the rest:

1. **One writer per series.** A source with an API and a richer export goes
   entirely through the same tool: both fill the same series and must be
   merged. A Whoop export therefore ships with the Whoop API, never to
   Takeback.
2. **Takeback sends a measurement only if no dedicated tool provides it.**
   Kindle sessions exist only in Amazon's archive, so Takeback sends them. If
   Amazon ever opened a reading API, the dedicated tool would take over and
   Takeback would stop sending them.
3. **A maintained tool already exists for the source? Use it.** A connector is
   written only for a source that has none.

## Running it

```bash
# 1. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Copy the config
copy .env.example .env

# 4. Run
uvicorn app.main:app --reload
```

The app is then available on http://localhost:8000
The API documentation (Swagger, not linked from the UI): http://localhost:8000/docs

### Takeback has no login

There is no authentication and no user account: every visitor sees every
imported item. Run it **locally**, or **behind an auth proxy** (Authelia,
Authentik, oauth2-proxy, an nginx `auth_basic` block…) if it listens on
anything but `localhost`. An archive you import is your entire history with a
platform — treat the app as being as sensitive as the exports you feed it.

### Docker

```bash
docker compose up -d
```

The app is then available on http://localhost:8010.
The SQLite database and the extracted exports are persisted in `./data`
(`data/db/`, `data/uploads/`, `data/imports/`).

## Usage

1. On the **Imports** page, drop the `.zip` archive downloaded from Amazon,
   Facebook, Instagram (JSON format), Google Takeout, LinkedIn or Seazon. For
   an export split across several zips (common for Facebook on large accounts),
   select them all at once: each file is sent in its own request (rather than
   one multi-gigabyte bundle) via `/imports/start` → `/imports/{id}/upload-file`
   × N → `/imports/{id}/finalize`, so nothing depends on a single very large
   upload that could hit a network limit.
2. The app extracts the archive (every zip merged into one directory),
   detects the matching import module automatically, then parses the content in
   a background job (the status updates live on the import's page).
3. The data is then browsed **by platform** (`/p/amazon`, `/p/facebook`...),
   across every import of that platform: an export imported every year adds to
   the same history, with the same dashboards, rather than opening a new
   analysis per file.

## One platform, several imports (deduplication)

Each export from a platform contains **the account's whole history** up to its
date, not only what changed since the previous one: importing Amazon's 2024
export and then its 2025 one brings every order from before 2024 a second time.
The per-platform views therefore count each item once (`app/dedup.py`):

- every item gets a **fingerprint** computed from its normalised columns (type,
  category, date, title, content, author, context, structured fields) — not from
  the raw JSON, which a later export can enrich with columns without the item
  changing;
- fields that legitimately change from one export to the next for the same item
  (an order's status going "Shipped" → "Closed", a shipping date, a pinned Keep
  note, a LinkedIn contact's current company, an accepted endorsement…) are
  excluded from the fingerprint (`dedup.VOLATILE_FIELDS`), as is the date of an
  item dated by its last occurrence (a LinkedIn Learning course, dated by its
  last view: `dedup.VOLATILE_TIMESTAMP_TYPES`); that list is indexed by **source
  and type** (`dedup.VOLATILE_FIELDS_BY_SOURCE`) because the same type can be
  exported differently per platform: where Amazon writes a complete order as
  soon as it appears, Seazon fills it in over its life (tracking number at
  dispatch, review after delivery, refund after cancellation) — for Seazon only
  the type, the date and the order identifier (`context`) identify the record,
  every other field being left out;
- two strictly identical rows **within one import** remain two items (occurrence
  suffix `:2`, `:3`...): as many copies are kept as the import containing the
  most of them, never the sum;
- among the copies of one item, the one from the most recent import is kept
  (`items.is_duplicate = 0`), the others hidden from the platform views. Deleting
  or re-analysing an import re-elects the kept copies: deleting the 2025 export
  makes those of the 2024 export visible again.

An import still being parsed appears in its platform only once finished
(`is_duplicate` stays `NULL` until then). On the first start after the upgrade,
existing imports are fingerprinted automatically (a catch-up logged at
startup). An import's page says how many of its items appear in no other import,
and `/browse?batch_id=…` still browses one import alone, duplicates included.

## Anonymising an import to share it safely

Once an import has finished, its page offers **Generate an anonymised
version**: the app rebuilds the extracted archive identically (same tree, same
JSON schema) so it stays usable as a realistic test set, but its security model
is a **default deny-list**: every text value is treated as sensitive and
replaced, *except* a handful of technical fields the app itself needs to work:

- **every JSON text field** (name, content, title, description, address and any
  unrecognised field) → replaced by a coherent fake value (one person keeps the
  same fake name throughout the archive); no list of fields to leave alone
  exists — it is the other way round: only `uri` (a media path, rewritten
  coherently) and date fields (shifted by one offset, so chronological order is
  preserved) escape replacement;
- **folder and file names** (conversations, albums, contacts…) → replaced too,
  except the fixed category names specific to Facebook/Instagram/Google
  (`messages`, `inbox`, `Google Photos`...) and the file names import modules
  need to work (`your_posts_1.json`, `message_1.json`, `MyActivity.json`...);
- **unrecognised `.html`/`.txt` files** (common in Google Takeout) → their
  content emptied entirely, as a precaution, rather than partially filtered;
- **`.csv` files** → headers kept (structural), cells replaced; the "Notes:"
  preamble LinkedIn puts before the `Connections.csv` header is kept in its
  shape (the note itself replaced), so that the header stays a header;
- **photos/videos/audio** → replaced by a minimal file.

The result is downloadable (`GET /imports/{id}/anonymize/download`) and can be
shared (support, development, an AI assistant) without exposing your real data.
See `app/anonymize.py`. This "everything is sensitive unless proven otherwise"
model is deliberate: a field the code does not know about must be anonymised by
default, never disclosed by omission.

An individual file that causes trouble (unexpected format, encoding) is emptied
and skipped rather than failing the whole job — on a very large export,
re-running the entire job for one offending file would be costly. If the job
fails anyway, or if files were skipped, the detail (the real error message / the
list of files concerned) is shown directly on the import's page, not only logged
server-side.

## Hiding the screen while sharing it

To share your screen (a call, a demo) without exposing names or messages, the
**crossed-out eye** button at the bottom of the sidebar turns on hiding mode:

- page text (tables, lists and rankings, dates, names) is blurred;
- charts stay sharp so their shape can be shared, but without ticks or values,
  without a legend (series names) and without tooltips;
- data tables, the "Values" / "Table" toggles and opening an item's detail by
  clicking are disabled;
- the detail panel and the search field are blurred.

The sidebar stays usable. The setting is remembered in the browser
(`localStorage`) and survives navigation. It changes no data and does not
replace the export anonymisation above: it is a display-only mask, in the
browser.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TAKEBACK_DATABASE_URL` | `sqlite:///./data/db/takeback.db` | Database URL |
| `TAKEBACK_DATA_DIR` | `./data` | Storage root (db / uploads / extracted imports) |
| `TAKEBACK_DEBUG` | `false` | Debug mode |
| `TAKEBACK_LANG` | `fr` | Language the UI is served in when the request asks for none (`fr`, `en`; see [`docs/i18n.md`](docs/i18n.md)) |
| `TAKEBACK_INFLUXDB_URL` | — | InfluxDB 1.x to feed for Grafana (e.g. `http://influxdb:8086`); without it, no push |
| `TAKEBACK_INFLUXDB_USER` | — | `user` tag of every point (shared-database convention) |
| `TAKEBACK_INFLUXDB_TZ` | `Europe/Paris` | Time zone of the `local_hour` / `local_weekday` fields |
| `TAKEBACK_INFLUXDB_PUSH_HOUR` | `3` | Hour (UTC) of the daily full push |
| `TAKEBACK_INFLUXDB_CREATE_DATABASES` | `true` | Create missing databases (year-long shards) |

## REST API

The API stays available but is no longer surfaced in the interface. Without a
`batch_id`, `/api/items` returns the de-duplicated view (one copy per item, see
above).

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/imports` | List imports |
| GET | `/api/imports/{id}` | Import detail |
| POST | `/api/imports` | Create an import (multipart upload, one or several merged `.zip`) |
| DELETE | `/api/imports/{id}` | Delete an import and its data |
| POST | `/api/imports/{id}/reanalyze` | Re-run detection + parsing on the files already extracted, without re-uploading |
| POST | `/api/imports/{id}/anonymize` | Start generating an anonymised version |
| GET | `/api/imports/{id}/anonymize` | Anonymisation state (`none`/`building`/`ready`/`error`) |
| GET | `/api/imports/{id}/anonymize/download` | Download the anonymised archive |
| GET | `/api/items` | Paginated search (`source`, `category`, `item_type`, `batch_id`, `q`, `date_from`, `date_to`) |
| GET | `/api/items/{id}` | Item detail (content, structured fields, media, raw JSON) |
| GET | `/api/sync/runs` | Latest pushes (`kind`, `limit`): status, result, counters |
| POST | `/api/sync/{kind}` | Run a configured push right away, in the background: `influxdb` (everything, `source=` one platform rebuilt, `rebuild=true` everything rebuilt) |
| GET | `/api/sync/influxdb/preview` | The points (line protocol) the InfluxDB push would write (`measurement`, `source`, `limit`), even without InfluxDB |

## InfluxDB export (Grafana)

Takeback stays the reference (books, sessions, library, the detail of every
item); Grafana receives enough to plot and **cross-reference** that data with
everything else (sleep, sport, health...). With `TAKEBACK_INFLUXDB_URL`, a push
(`app/sinks/influxdb.py`) writes into InfluxDB 1.x the points
`app/influx_export.py` builds, **one database per domain**:

| Database | Measurement | Items | Tags | Fields |
|---|---|---|---|---|
| `DigitalStats` | `item` | any dated item, all platforms | `source` (= platform), `category`, `item_type` | `count` (i) |
| `MediaStats` | `reading_session` | Kindle and Audible sessions | `source` (`kindle`/`audible`), `kind` (`ebook`/`audiobook`), `device` | `minutes` (f), `sessions` (i), `pages` (i, Kindle), `title` (s) |
| `MediaStats` | `book_finished` | finished Kindle books | `source`, `kind` | `count` (i), `title` (s) |
| `ShoppingStats` | `purchase` | purchase lines (Amazon and Seazon orders, digital, Audible) | `source`, `kind` (`retail`/`digital`/`audible`/`meal`), `department`, `currency` | `amount` (f), `lines` (i), `orders` (i) |

On **every** point: the tags `platform` (the Takeback platform), `via=takeback`,
`user` (if configured) and `time_precision` (`second`, or `day` for an item
dated to the day only), and, except for the latter, the fields `local_hour` and
`local_weekday` (1 = Monday) in `TAKEBACK_INFLUXDB_TZ` — InfluxQL cannot convert
a time zone itself; these are what make "evening reading" possible
(`local_hour >= 20 OR local_hour < 3`).

Seazon orders are their own type (`seazon_order`, not Amazon's `order_item`):
they therefore arrive under `kind=meal`, with `department=Repas` (what their
importer names under "Catégorie") and `source=seazon` — enough to separate them
from Amazon in Grafana, `kind` and `source` both being tags.

Rules, all covered by `tests/test_influx_export.py`:

- **Privacy**: InfluxDB has no authentication, so Takeback belongs behind a
  login. Only numbers and low-cardinality tags leave; never a message, a
  search, a URL or a name. A text field exists only if a series declares it:
  book titles, nothing else (the test fails otherwise).
- **Fixed types**: InfluxDB 1.x refuses a point whose field changes type; every
  field is declared once (integer, float, text) and an undeclared field is an
  error.
- **Per-second aggregation**: a point is identified by measurement + tags +
  time, so an order's three lines, or two messages in the same second, would
  overwrite each other; they are summed into one point. `orders` counts each
  order once, in the first point holding it: the sum stays exact even for an
  order spanning two categories.
- **Times** are UTC on the way in (as in Takeback); items with no real date and
  duplicates from other imports are left out.

When: **two minutes after every start**, everything is rebuilt (a new version
may have changed a series: no point of the old shape is left); **after every
import, re-analysis or deletion**, for that platform only — its series are
wiped then rewritten, with no stale point; **daily** at
`TAKEBACK_INFLUXDB_PUSH_HOUR` everything is rewritten (a safety net); on demand
from the Imports page ("Push now", "Rebuild"). The wipe touches only
`via=takeback` series: a domain database other tools write to keeps their data
(`MediaStats` also receives, for example, the listening history a sync tool
writes there with `source=audiobookshelf`).
Missing databases are created with **year-long shards**: by default InfluxDB
creates one per week, i.e. 600 for a first push covering a few years; an
existing database is never modified.

The push runs in a background thread of the application (`app/scheduler.py`),
one job at a time, never at startup; every pass is recorded (`SyncRun`) and
shown on the Imports page. The application assumes a single instance (a single
SQLite database): two instances would each push.

### Adding a series

For a new correlation in Grafana: an entry in `SERIES`
(`app/influx_export.py`: database, measurement, item types, tags, fields and
their types), a test, a release. `GET /api/sync/influxdb/preview?measurement=…`
shows the points before any push. A new database also needs its datasource on
the Grafana side (one InfluxQL source per database).

## Import module architecture

Each data source is an independent module under `app/importers/`, implementing
`BaseImporter` (`app/importers/base.py`):

- `detect(root: Path) -> bool`: recognises whether an extracted archive matches
  this format.
- `parse(root: Path) -> Iterator[NormalizedItem]`: turns the raw content into
  normalised items (`category`, `item_type`, text, author, date, media, and
  `fields` for structured label/value pairs). The original JSON is always kept
  (`raw_json`) so nothing is lost even if the mapping is imperfect.

The import pipeline (`app/processing.py`) tries each registered module until it
finds the one that recognises the archive.

### Adding a new import module

1. Create `app/importers/my_service.py` with a class inheriting from
   `BaseImporter`, decorated with `@registry.register`.
2. Import it in `app/importers/__init__.py` (`from . import my_service`).

Nothing else is needed: the new module appears automatically in detection,
parsing, and as a platform in the interface. To give it a name, an icon, a
colour and where to ask for the export, add an entry in `app/platforms.py`
(otherwise a generic display is used).

### The generic Meta parser (`app/importers/meta_common.py`)

A real Facebook/Instagram export contains a handful of files with a dedicated
schema (posts, messages) and **dozens** of others (followers, ads viewed,
searches, connections, security, preferences...) that all reuse the same
generic "label → value" shapes internal to Meta (`label_values`,
`string_map_data`, `string_list_data`). Rather than writing one parser per file
(Meta adds them regularly), `meta_common.py` recognises these shapes and infers
`category`/`item_type` directly from the file's path (e.g.
`connections/followers_and_following/followers_1.json` → category
`connections`, type `followers`) — so a new category file appears
automatically, with no code. Used by `facebook.py` and `instagram.py` on top of
their dedicated posts/messages parsing.

### Provided modules

- **Facebook**: posts (`your_posts_<n>.json`), messages (`message_*.json`), +
  every generic category through `meta_common`.
- **Instagram**: posts (`posts_<n>.json`), messages (same format as Facebook on
  the Meta side), + every generic category (followers/following, ads, searches,
  story interactions, security, etc.) through `meta_common`.
- **Google Takeout**: unlike Meta, each Google service has its own format — no
  single generic parser is possible. Dedicated modules for "My Activity" (`My
  Activity.json`, generic across dozens of services: Search, YouTube,
  Discover...), visited places (`Semantic Location History` — Google removed
  that export from Takeout in late 2023, positions are no longer exportable
  except encrypted server-side, so this module will produce nothing on a recent
  export; that is not a bug), Chrome history, Keep notes, liked YouTube videos,
  Google Play Store installs, and Google Photos photo metadata
  (`*.supplemental-metadata.json`). File paths and JSON schemas are based on the
  open-source
  [purarue/google_takeout_parser](https://github.com/purarue/google_takeout_parser)
  parser and on the
  [official Google schema reference](https://developers.google.com/data-portability/schema-reference)
  rather than guessed — Google publishes no stable specification.

  **Localised file names**: Takeout's folders/files are translated according to
  the export's language (e.g. "My Activity" → "Mon activité" in French, "Meine
  Aktivitäten" in German; "BrowserHistory.json" → "Historique.json" in French)
  whereas the internal JSON keys (`header`, `titleUrl`, `time`, `Browser
  History`...) stay in English in every case — confirmed on real EN/DE/FR
  exports. Each `_parse_*` in `google_takeout.py` therefore lists several
  candidate file names (one per confirmed language) rather than a single one; a
  category that returns no item on a non-English export is first a file name to
  widen, not a parser error.

  **Non-breaking space in "brand" folder names**: Google inserts a
  non-breaking space (U+00A0) instead of a regular space in every folder named
  after a brand ("Google Play Store", "Google Photos", "YouTube Music"...) —
  invisible in a file manager, but a `"google play store" in str(path).lower()`
  test with a regular space never matches. `_normalized_path_str()` in
  `google_takeout.py` neutralises that character before any path comparison —
  use it systematically for a new Google module rather than `str(path).lower()`
  directly.

  **"My Activity" in HTML**: Google Takeout lets you choose the format (HTML or
  JSON) per category at export time; if "My Activity" was requested as HTML,
  `_parse_my_activity_html` parses that HTML directly via BeautifulSoup (+
  `lxml`, far faster than the standard Python parser on a file with several
  thousand entries), on Google's fixed CSS structure
  (`outer-cell`/`content-cell`/`mdl-typography--title`, etc.) — not localised,
  unlike the text — rather than requiring a re-export as JSON.
  `_parse_html_activity_dt` recognises several date formats (English 12-hour
  AM/PM, French 24-hour, French month names with or without an accent).
  Some categories (conversation transcripts under "Gemini Apps" in particular)
  have rich multi-paragraph content with no plain-text date at the end of the
  block; in that case the item is still kept (content flattened via
  `get_text()`, first link found) rather than lost for a missing date. Two real
  traps met in practice, both with an item silently falling back to `None`
  without raising: an invisible "spacer" cell shares the same CSS class as the
  real content cell (explicitly excluded since) and a trailing `<br>` can
  follow the date's text instead of preceding it (the last real child is looked
  for after stripping trailing `<br>`/whitespace, not simply the last raw
  child).

  **Classifying by usage**: "My Activity" dominates a full Takeout export by
  volume — tens of thousands of items, split across around thirty service
  folders ("Google Play Store", "Recherche", "Maps"...) that say little: the
  Play Store folder mostly holds app launches, "Recherche" mixes searches,
  opened results and notifications. Every entry is therefore classified from
  its sentence (`classify_activity`, French phrasings confirmed on a real
  export, English ones from Takeout): its category is a usage family
  (Applications, Searches, Videos & TV, Web, Routes & places, Assistant & AI,
  Third-party tracking, Other), its type is the action ("Application utilisée :
  Gmail" → `app_used`, "Vous avez recherché …" → `search`, "Vous avez regardé …"
  → `video_watched`, "Itinéraire pour …" → `directions`...), its title the
  object of the action (the app, the query, the video, the page, the
  destination) and its context the service that recorded it. A video's author
  is its channel (the line under the title, lost until now), a page's is its
  site (unfolded Google redirect link). "Third-party tracking" gathers what
  Google Analytics and Ads Solutions received from third-party apps and sites
  ("Vous avez utilisé Scorely"). A Google results page consulted is not a
  visited site but the search itself (`results_page`); a business listing
  opened from the results (`local.google.com`) is a place viewed. The displayed
  type names are French (`BaseImporter.type_labels`); see
  [`docs/i18n.md`](docs/i18n.md) for translating them. An unrecognised sentence
  stays in its service's family, as plain "Activité". An existing Google import
  must be **re-analysed** to benefit.

  **Times**: Google writes every date in an export with the UTC offset of the
  day of the export ("1 janv. 2025, 17:00:00 CEST" in an October export): the
  date is converted from the stated offset to the server's local time, like
  Chrome history timestamps — without which winter activity was off by an hour.
  French legend headers ("Produits :", "Lieux :", "Informations détaillées :")
  are recognised, and a Gemini entry whose answer follows the date (rich
  content) keeps its date and its answer.

  **`.mbox` format** (Gmail export): no dedicated parsing module yet — treated
  by the anonymiser as an unrecognised file (emptied, never read, so no memory
  risk even on a very large export). A dedicated Gmail module
  (sender/subject/thread) remains to be built if needed.

- **Amazon** ("Request My Data" under account Privacy): a CSV export. Two
  different file names exist for the same categories depending on the
  region/export version: `Retail.OrderHistory.<n>.csv`/
  `Retail.OrdersReturned.Payments.<n>.csv` (dot-namespaced, numbered; columns
  based on an open-source tool that really parses this format,
  [holocronweaver/amazon-orders-to-transactions](https://github.com/holocronweaver/amazon-orders-to-transactions)),
  or `Order History.csv`/`Refund Details.csv` (no prefix, no number). Both are
  tried; the second was **confirmed on a real anonymised French export** (real
  columns read straight from the CSV — the anonymiser preserves headers) —
  which fixed two real bugs: the order total is called `Total Amount`, not
  `Total Owed` (guessed from the reference tool, which describes the other
  variant), and the quantity `Original Quantity`, not `Quantity`.

  Categories covered, all verified on the same real export: **Orders** (`Order
  History.csv`), **Returns** (`Refund Details.csv` + `Returns Status.csv`, two
  distinct files not yet confirmed 1:1 so kept as two separate `item_type`s),
  **Digital orders** (`Digital Content Orders.csv` + `Purchase History.csv` on
  the Audible side), **Cart** (`Cart History.csv`), **Searches** (`Search
  Queries.csv`), **Fire TV**/**Fire Tablet** (`Total Usage.csv` — *exact*
  session duration, unlike the heuristic estimate used for
  Facebook/Instagram, Amazon records session start/end directly; and `App
  Engagement.csv`, usage per application), **Audible** (`Listening History.csv`,
  real listening sessions) and **Kindle**
  (`Kindle.Devices.ReadingSession.csv`, under `Additional Data/`, real reading
  duration). **Alexa** (`questionUnderstandings*.csv`/`questionAnswering*.csv`,
  also under `Additional Data/`) exposes the voice commands themselves
  (`Utterance text`) with their timestamps.

  Amazon ships **one archive per requested category** (`Your Orders.zip`,
  `Kindle.zip`...): each is recognised as an Amazon export and adds to the same
  platform. The **Kindle** archive alone is mostly device telemetry; what is
  read from it:
  - **reading sessions** (`Kindle.Devices.ReadingSession.csv`), **named after
    their book**: a session carries only the book's identifier (ASIN, or a
    personal document's identifier — the vast majority of sessions), and the
    title and author are found in the other Kindle files that name them
    (`DocumentMetadata`, `reading-insights` sessions, `whispersync`,
    `Digital.Content.Ownership`...);
  - **finished books** (`Kindle.UserUniqueTitlesCompleted.csv`, whose first
    column groups ASIN, day and mode: `B00KZ0L5FE_2024-02-20_AUTOMATIC`);
  - **highlights, notes and bookmarks** (`whispersync.csv`) — the export does
    **not** contain their text, only the date and the colour
    (`{mchl_color:yellow}`); highlight lines that repeat another to the second
    (sync copies) are merged; synchronised reading positions are ignored;
  - the **library**: purchased books (`Digital.Content.Ownership.*.json`, one
    entry per ASIN, excluding the dictionaries installed by the device) and
    **personal documents** sent to the Kindle
    (`Kindle.KindleDocs.DocumentMetadata.csv`: title, author, file, format,
    size);
  - **reading badges** (`Kindle.BookRewards.Achievements.2.csv`).

  `Kindle.ReadingActivity.csv` (pages and minutes per day) and
  `reading-insights-sessions_with_adjustments.csv` total exactly the same
  reading time as the sessions: they serve only for titles; importing them as
  items would count every minute twice. The anonymiser also replaces
  enumeration values (`Annotation Type`, `originType`...): an anonymised copy
  therefore loses highlights and finished books.

  The `Additional Data/` folder of a full export contains 100+ dot-namespaced
  subfolders — overwhelmingly internal telemetry (UI interaction counters,
  behavioural sync metrics, notification logs) with no real personal browsing
  value, not a uniform seam of categories to mine.
  `SearchHistory.RufusConversations` (Amazon's AI shopping assistant,
  substantial real content) remains a genuine documented gap rather than an
  oversight — a conversation format of its own, deserving its own design rather
  than a rushed addition.

  Parsing is deliberately tolerant throughout (several alternative column
  names tried, `.get()` everywhere): a category confirmed on one real export is
  no guarantee that a future regional variant will use exactly the same
  headers.

  **Row-by-row deduplication** (`_parse_csv_category`): Amazon has been seen
  delivering the same small category file at more than one path depending on
  how "Request My Data" was split across several archives — a row strictly
  identical across all columns is ignored rather than counted twice (a
  legitimate multi-item order often shares the same Order ID but never a
  byte-identical row, so nothing real is lost by this rule).

  The CSV anonymiser (`_anonymize_csv` in `app/anonymize.py`) is aware of column
  names for dates (a header containing "date" or "time" — widened from "date"
  alone after finding real "Start Time"/"First Search Time"/"start_timestamp"
  columns that escaped it and were turned into useless text — or ending in "
  on"/" at", LinkedIn's convention: "Connected On", "Sent At"), also shifts
  LinkedIn's spelled-out dates while keeping their format
  (`TEXT_DATE_FORMATS`), and **detects amounts by the cell's value rather than
  by a keyword in the header**: any cell that is a raw number becomes a
  plausible number of a similar shape (`_pseudo_amount`, falling back to the
  standard text replacement if the cell turns out not to be numeric) — more
  robust than a keyword list ("total"/"price"/"quantity"...) that had to be
  widened with every new numeric column encountered (Duration Ms, Book Length
  Milliseconds, total_reading_millis...).

- **LinkedIn** (Settings › Data privacy › Get a copy of your data): LinkedIn
  ships two archives — a "Basic" one within minutes (profile, connections,
  invitations, endorsements, positions, education, LinkedIn Learning...) then a
  "Complete" one about a day later, repeating the same files and adding
  messages, posts, comments, reactions, searches, connections... Both are flat
  folders of CSV (one per category, headers in English whatever the account's
  language); importing the second after the first double-counts nothing
  (deduplication). Every file of the Basic archive and every column used is
  **confirmed on a real export**; those of the Complete archive (`messages.csv`,
  `Shares.csv`, `Comments.csv`, `Reactions.csv`, `Votes.csv`,
  `SearchQueries.csv`, `Logins.csv`...) are read from their documented headers,
  not yet verified. Any other CSV is imported row by row by a generic parser
  (each column filled as a field, dated by its first readable date column)
  rather than ignored.

  Traps from a real export: `Connections.csv` starts with a "Notes:" paragraph
  and a blank line before its header; each file has its own date format ("08 Oct
  2026", "Oct 2020", "2020", "Thu Oct 08 10:12:34 UTC 2020", "10/8/26, 9:45 AM" —
  month first —, "2020/10/08 10:12:34 UTC", a range for events, and a sentence
  for `Rich_Media.csv`: "You uploaded a feed photo on July 12, 2020 at 9:45 PM
  (GMT)"); a date known only to the month or the year (a position, a degree) is
  read as the 1st at midnight, hence with no time; `Ad_Targeting.csv` repeats
  some column names; a member who hides their profile from exports appears in
  `Connections.csv` only with the date they connected (counted, with no name).
  `Invitations.csv` holds only recent invitations — presumably those still
  pending, an accepted invitation becoming a connection. Times are UTC, as in
  the export. A conversation in `messages.csv` is named by its title, else by
  its participants other than the account holder (read from `Profile.csv`).

- **Seazon** (My account › My personal data › Request my data, under the GDPR):
  a **flat** zip of `seazon_*.json` files — plus `foodcheri_user.json`, still
  under the parent company FoodCheri's name, which carries taste preferences.
  One file per domain: `seazon_order.json` (each delivery slot the plan
  created), `seazon_opinion.json` (the review left after a delivery),
  `seazon_plan.json` (**the whole** subscription history, one record per
  change, not just the current subscription), `seazon_notification.json` (what
  Seazon sent), `seazon_log.json` (the back-office journal),
  `seazon_session.json` (logins), `seazon_user.json` (profile, addresses,
  coupons, loyalty) and `seazon_gorgias_ticket.json` (support — Gorgias is
  Seazon's helpdesk). No documented schema exists for this export: everything
  is taken from a **real export**, and an incomplete record is skipped rather
  than guessed.

  **What counts as an order.** The plan creates a slot every week, but only the
  ones actually charged (`prices` present, state `paid`/`shipped`) are real
  orders — Seazon's own profile states an `order_count` matching exactly those.
  The rest were cancelled **before any payment**, some of them with the dishes
  already chosen. A charged slot therefore becomes a `seazon_order` (what the
  Purchases tab counts), a cancelled slot an `order_cancelled` — never a
  purchase, no money having changed hands — and the plan's current slot is not
  imported at all: nothing is chosen or paid for it yet, and it will come back
  as an order or a cancellation once settled. The dishes themselves are separate
  `meal` items: the export gives **one price per order, never per dish**, so
  dish lines have no amount, do not weigh on spending, and their page links back
  to the whole order (the dishes of a slot cancelled before payment are kept
  too — what was chosen is part of the history).

  A Seazon order is **its own item type** (`seazon_order`), not the `order_item`
  shared with Amazon: it carries nothing that makes up an Amazon order line (no
  department, no ASIN, no product state), and that is what lets its purchases
  page offer its own dimensions in its own vocabulary rather than Amazon's
  (`stats.purchase_group_dimensions`).

  **When an order took place.** `created_at` is the moment the **plan** opened
  the slot, not when anything was paid for or eaten: it precedes delivery by
  about three months on average, and lands in a different calendar month from
  the delivery for most charged orders. Dating items on that would shift every
  reading over time ("what did I spend in June", "what did I eat in June") by a
  quarter: a charged order is therefore dated by its `charge_date` (the day the
  money was taken) and a dish by its `shipping_date` (the day the box arrived —
  the actual delivery of `delivery_dates` is that same day or up to three days
  later, and is missing on slots never delivered). Both fall back to
  `created_at`, and a cancelled slot keeps its own cancellation date. The
  timestamp is part of an item's identity (for Seazon, only `fields` are
  volatile, see deduplication): an already-imported export must be re-analysed,
  or redone, so that the same dish is not counted twice.

  **An order's price.** `prices.value` is the value the export declares;
  `discount`, `shipping` and `addonPrice` stay separate fields rather than being
  merged into it, the export never saying what `value` already includes. An
  order charged then cancelled remains an order — the money was taken — and
  carries `Annulée le`, `Motif d'annulation` and `Remboursé` in addition; the
  cancellation must not erase the spending, only explain it.

  **Refunds are separate items** (`seazon_refund`, one per operation, partial
  refunds producing several) and not a mere field: that is what lets the
  "Deduct refunds" switch on the Purchases tab arrive at the figure Seazon
  states itself — the net of what was charged and what was refunded matching its
  own `total_revenue` exactly. Those refunds share their order's identifier
  (`context`), so the refund list points back to what had been ordered.

  The rest of the export is labelled in French: cancellation reasons (which the
  export writes in English whatever the account's language), journal type and
  action, notification reason and recipient, voucher and ticket statuses. The
  journal (`account_log`) keeps the original sentence, and an order's dishes are
  repeated there (`meal_selections`) — the **only** place in the export that
  ties a dish's identifier to its name, which is what lets taste preferences and
  the dishes behind a review be named: a preference never ordered stays under
  its identifier. Those preferences carry no date — the platform's only undated
  items.

  Details verified on the real export: a plan's `day_of_week` is ISO (1 =
  Monday), and order delivery dates do fall on the announced day; an address is
  named only by its identifier in an order, resolved from `seazon_user.json`;
  `canceling_date` and `cancelled_date` coexist (the cancellation request and
  its acknowledgement, a charged order sometimes having only the latter);
  `initial_meals` (the proposed menu) is kept as a field only when the customer
  changed it. `state_change_events`, the RFM notes, `scoring_churn` and
  `pricing.extras` stay in the raw JSON.

Facebook/Instagram exports vary with the export's version and language; the
parsers are deliberately tolerant (an unreadable post/message/record is skipped
rather than failing the whole import) and always keep the raw JSON per item — a
field mapping to adjust therefore never causes data loss.

## Navigation

The interface starts from **platforms** (the sidebar); imports are no longer
anything but a way to feed a platform.

- **`/`**: one card per supported platform — item count, period covered, number
  of imports, shortcuts to its dashboards. Platforms not yet imported are listed
  with where to ask for the export. A platform announced but with no import
  module (`platforms._PLANNED`, empty for now) appears greyed out, "Soon", here
  and in the sidebar.
- **`/p/{platform}`**: overview — content over time (by year, or by month on a
  short history, stacked by category, counted by SQLite to stay fast), with the
  basic content filter; data categories (count per type, instant filter) and the
  imports feeding it, with the period each covers and the number of items kept
  (the others already appear in a more recent import). The tabs shown depend on
  the data actually imported (no "Reading" tab without a Kindle/Audible session,
  no "Formulas" or "Dishes" without Seazon boxes and dishes):
  `platforms.available_views`.
- **`/p/{platform}/purchases`** (Amazon, Seazon): spending over time, spending by
  dimension, most expensive purchases, orders and returns. The order count
  counts **distinct order identifiers**, not lines — a multi-item order produces
  several `order_item` items sharing the same `context` (the Order ID), so
  counting lines would have inflated the order count by the average number of
  items per order. A return present in both `Refund Details.csv` and `Returns
  Status.csv` for the same order is counted once — and a Seazon refund
  (`seazon_refund`, money returned on a charged order) counts as a return the
  same way, which brings the net total down to Seazon's own figure. For Seazon an
  order is already a line: the export gives one price per order, never per dish
  (see the import modules), so splitting dishes into lines would have meant
  inventing their share of the total. The dimension selector drives **both** the
  over-time chart (stacked by that dimension) and the "spending by …" chart —
  both go through `stats.purchase_group` so they can never disagree. The
  dimensions offered, and the word used for the category, are **specific to each
  platform** (`stats.purchase_group_dimensions`): Amazon keeps its department,
  its two addresses, its site and its product state, whereas a Seazon order
  splits by **formula** (10 dishes, 14 dishes), the address it was delivered to,
  a payment method and a status — offering it Amazon's dimensions would only
  ever fill them with "Non renseigné". Its "Catégorie" is "Repas" for all its
  orders and always has been: that was not a dimension, it is no longer offered.
  A dimension that only makes sense for one platform is registered separately
  (`stats._PURCHASE_EXTRA_DIMENSIONS_BY_SOURCE`) so it never lands on the
  others. Only the category has an equivalent for digital purchases
  ("Numérique") besides; for the other dimensions they fall under "Non
  renseigné". Amounts are shown in their currency when all purchases share one
  (`stats.detect_currency`), otherwise as raw numbers (no conversion). Added to
  that are **cumulative spending** over the period and, as soon as the period
  spans several years, a **year-by-year cumulative** (each year on the same
  1 January – 31 December calendar, the most recent year and the previous one
  in colour, the others grey; the last year stops at the last known purchase or
  refund, for lack of an export date) — both gross, or net of refunds with the
  "Deduct refunds" switch. Finally the **purchasing rhythm**: spending per day of
  the week (stacked by the chosen dimension) and a heatmap of orders by day ×
  hour. Times are the export's (UTC for Amazon); an order dated with no time
  (timestamp exactly at midnight, `stats.has_time_of_day`) counts for its day of
  the week but not in the heatmap, rather than inflating the "0 h" cell.
- **`/p/{platform}/purchases/list`**: each individual purchase, filterable by
  period, dimension value, text (product or order number) and amount range,
  sortable by date, product, dimension or amount, with the selection's total.
- **`/p/{platform}/formulas`** (Seazon, "Formules"): what a box contains and what
  it costs. A Seazon box is not a basket but N dishes of a chosen formula ("10
  dishes", "14 dishes"), billed as a single amount: the tab counts boxes per
  formula over time and tracks the **dish price** (what was paid divided by the
  number of dishes), the only unit reading that is not invented, the export
  never giving a price per dish. A table summarises, per formula, the boxes, the
  dishes, the spend, the price of a box and of a dish, and the last order.
  Clicking a bar, a point or a row opens the boxes they count.
- **`/p/{platform}/dishes`** (Seazon, "Plats"): the recipes, from the most eaten
  to the least, with the number of dishes eaten — every portion counts (three
  portions of the same dish in a box is eaten three times) and only **delivered**
  boxes count. What had been chosen for a slot cancelled before payment, or for
  a box paid then cancelled, was never eaten: it is shown separately ("+ N not
  delivered", and recipes never delivered at the end of the list) without ever
  adding to it. The chart stacks dishes eaten over time, one bar per recipe, the
  "Autres" hidden; a click opens a dish's plates.
- **`/p/{platform}/reading`** (Amazon): reading/listening time, sessions and
  pages turned over time (Kindle + Audible), reading days and the longest run of
  consecutive days, books finished per period, most read/listened titles
  (finished books marked), and a heatmap of minutes read by day × session start
  hour (UTC, as in the export). Those durations are **real** (Amazon records
  session start/end). A Kindle session whose export does not name the book keeps
  its ASIN: its title is then found in the orders or the library of another
  import (`crud.get_titles_by_asin`).
- **`/p/{platform}/library`** (Amazon, "Bibliothèque"): every Kindle book —
  bought, sent as a personal document, or only read — with its author, its
  arrival date, its reading time, its last activity, its finish date and its
  annotations, and a status: **finished**, **started** (at least one session or
  annotation) or **never opened** (in the library, with nothing else). Arrivals
  in the library over time, status by origin, a list filterable by status, origin
  and text; a row opens everything the export holds about that book. Over the
  whole history, with no period: it is a catalogue, "what did I read in 2025" is
  the Reading tab's business.
- **`/p/{platform}/usage`** (Google, "Usages"): what you do, whichever service
  recorded it. One tile per usage family (count of actions and of distinct
  objects over the period: app launches and applications, searches and distinct
  queries, videos and channels, pages and sites...), which also selects the
  detailed family below, plus items per **active day** (a YouTube history going
  back to 2016 does not dilute the average of an 18-month history). For the
  chosen family: its rankings (most opened applications, most searched queries
  and **words**, most watched channels and videos, sites and pages, destinations
  and places, assistant requests, third-party apps feeding Google) — ignoring
  case and spacing, under the most frequent spelling, stop words removed for
  words — and, where it speaks, the top 7 of the whole history over time, the
  rest folded into "Autres" hidden by default. Chrome visits from "My Activity"
  double the Chrome history: they count only without it. Then, for all families:
  the **rhythm of the day** (one line per family, each line read against its own
  maximum) and the **history kept per service** (first → last item of each
  service — an export holds only what the account still keeps). Every row, bar or
  cell opens its items (`/p/{platform}/usage/panel`, same grouping rule as the
  ranking). A category named "Autres" never takes a colour: it joins the grey
  series.
- **`/p/{platform}/activity`** (any platform): dated items over time (stacked by
  category, or by content type to see one category's detail), content filter,
  day × hour heatmap, and an estimated time spent — an approximation inferred
  from the timestamps (activities less than 15 min apart grouped into
  "sessions"), not a real measurement: see `app/stats.py`. An item dated with no
  time (timestamp exactly at midnight, `stats.has_time_of_day`: most LinkedIn
  dates, an Amazon order dated to the day only) counts in the over-time chart,
  but neither in the heatmap, nor in the most active hour, nor in the estimated
  time spent — rather than inflating the "0 h" cell; the page says how many are
  in that case.
- **`/p/{platform}/posts`** (Facebook/Instagram, as soon as there are
  publications): publications over time and by month of the year (seasonality,
  all years combined), stacked by media type (photo, video — a publication
  containing a video counts as video —, no media), day × hour heatmap, and the
  last 24 publications with media as thumbnails. Filter by media type
  (multi-select).
- **`/p/{platform}/messaging`** (Facebook/Instagram): message statistics — sent
  / received over time, most active conversations ("group" when more than two
  people wrote in it), conversations active and new per period (a conversation
  is new where its first message of the whole history falls, not of the period),
  heatmap of messages sent and **reply delay** in both directions. A reply is a
  message following the other party's in the same conversation, within 24 h
  (`stats.REPLY_WINDOW`) — beyond that, the conversation resumes rather than
  being replied to. Two other members of a group replying to each other do not
  count. Medians, and distribution as shares (not counts: you and your contacts
  do not reply the same number of times). "You" is the same inferred participant
  as in the conversations. Filter private / group conversations, which keeps or
  sets aside whole conversations (`stats.filter_threads`).
- **`/p/{platform}/network`** (Instagram followers/following, Facebook friends,
  LinkedIn connections and followed companies): a list of relationships is a
  **snapshot**, not a history — someone who no longer follows you is simply
  absent from the next export. The current network is therefore the list from
  the **last import that contains it**, not the union of all imports (which
  would still count everyone who ever followed you); the chosen period does not
  apply to it. It holds: the size of the current network cumulated by connection
  date (accounts that left do not appear, the page says so), **reciprocity**
  (mutual, not following you back, you not following them back) and **departures**
  : accounts present in a previous export but no longer in the last one — which
  is where importing an export every year brings information no single export
  holds. Accounts are matched by name (title, value or end of the profile URL
  depending on the list and the export version), not by fingerprint, so that a
  mere URL format change between two exports does not look like a departure.
  Finally all dated relationships (requests, blocks, close friends...) over
  time, by type. Clicking a number opens the list of accounts, with a link to
  each profile. Two namesakes in one list stay two accounts when their profile
  links differ, and an account with no name (a hidden LinkedIn member) is never
  counted as departed, for lack of being able to find it from one export to the
  next. For LinkedIn, invitations received and sent over time are added, and
  **where your connections work**: the employers most represented in the last
  export (spellings differing by case or spacing grouped, "Globex-Corp" =
  "Globex - Corp"), each opening the list of people concerned.
- **`/p/{platform}/career`** (LinkedIn, "Parcours"): the career timeline —
  positions, education and volunteering on one year axis, from most recent to
  oldest, an ongoing position fading out to today —, total experience (months
  covered by at least one position: two simultaneous positions do not add up),
  most endorsed skills, who endorses you and whom you endorse most, endorsements
  given and received over time, recommendations, certifications and LinkedIn
  Learning courses consulted (dated by their last view). The timeline,
  experience and certifications cover the whole career; the chosen period
  applies to endorsements, recommendations and courses.
- **`/p/{platform}/conversations`**: a messaging-style view, threads from all
  imports merged (the same thread present in two exports makes one, each message
  once). The participant appearing in the most distinct threads is treated as
  "me"; a best-effort heuristic.
- **`/browse`**: free search across all platforms (or one, or a single import with
  `batch_id`) by category, type, text and period; columns and sorting by field
  when a type is selected.
- **`/items/{id}`**: item detail — content, structured fields (clickable links),
  media, raw JSON, origin import and the number of imports containing it; a link
  to the whole order for an Amazon item/return, a dish, a cancelled slot or a
  Seazon review.
- **`/imports`**, **`/imports/{id}`**: uploading archives and managing imported
  files (status, what the file contains, re-analysis, anonymised version,
  deletion), and, as soon as the InfluxDB push is configured, its **passes**
  (frequency, last push and its result, "Push now", "Rebuild").

The old per-import analysis addresses (`/imports/{id}/stats`, `/reading-stats`,
`/purchase-stats`, `/purchases`, `/conversations`) redirect to their
per-platform equivalent.

### Filters and exploration

- A single row of filters above every dashboard: period first (one chip per year
  present in the data — the natural preset when importing one export a year — or
  free dates), then precision and dimension, applied to every chart and figure on
  the page.
- **Content filter** (overview and activity): a checkbox list of categories, with
  their count of dated items, all checked by default. Unchecking the one that
  crushes the others (messages, often) makes the rest readable: the freed
  colours pass to the following categories instead of leaving them in "Autres".
  The per-type tabs have their own filter (private/group conversations, media
  type).
- **Dates at 1970**: for lack of a date, exports write a timestamp of 0, read as
  01/01/1970. Anything before 1971 (`crud.NO_DATE_BEFORE`) is treated as "no
  date": outside the charts, heatmaps, year chips and covered periods, but still
  counted in the totals and findable in search. The "Include 1970" switch, shown
  only when the platform holds any, puts them back as they are.
- **Every aggregate figure opens**: a click on a bar, a segment, a ranking row or
  a heatmap cell opens a side panel with the items making it up
  (`/p/{platform}/panel`, `/p/{platform}/purchases/panel` for purchases: total,
  number of items and orders, spending by period, purchases from most to least
  expensive; `/p/{platform}/formulas/panel` and `/p/{platform}/dishes/panel` for
  Seazon), with a link to the full list or search. Example: "10 dishes" in
  "Spending over time, by formula" or in the formulas table → the boxes billed as
  such over the period.
- Every chart has a **table view** (same figures, readable without hovering or
  distinguishing colours) and, where useful, a "Values" switch (the total at the
  top of each column, or the value at the end of each bar of a ranking).

### Charts and theme

Chart.js, Bootstrap, Bootstrap Icons and HTMX are all served locally from
`static/` — never from an external CDN, to stay consistent with the self-hosted
principle. Every chart goes through the same constructor (`TB.bar` in
`static/app.js`):

- a categorical palette of 8 shades, declined in light and dark, in an order
  validated for colour blindness (adjacent pairs); a series keeps its colour
  whatever the filters, because the colour follows the entity (its rank over the
  platform's whole history), not its rank in the current view. Beyond 7 series,
  the tail is grouped into "Autres" (grey) rather than inventing a 9th colour;
- thin bars (24 px max) with rounded ends, 2 px of space between stacked
  segments, a discreet grid, a tooltip listing every series of a column with its
  total;
- a time axis with no gaps (an empty month appears empty) as long as that stays
  reasonable (`stats.MAX_FILLED_BUCKETS`).

Light/dark theme: a button at the bottom of the sidebar, the system preference by
default, the choice remembered in the browser.

The dashboards share the same aggregation logic (`app/stats.py`): amounts and
durations are stored already formatted for display in `fields_json` (e.g.
`"17.10 EUR"`, `"10.0 min"`) rather than twice (a raw copy plus a formatted one)
— a dashboard that needs to sum them extracts the number at the head of the
string (`stats.extract_leading_number`) instead of the importer keeping two
copies of the same value just for that.

## CI/CD

| Workflow | What it does |
|---|---|
| `.github/workflows/tests.yml` | `pytest` on every push and pull request, Python 3.14 (the runtime the image ships) |
| `.github/workflows/codeql.yml` | CodeQL for `python` and `actions`, `security-extended` queries, on push, PR and weekly |
| `.github/workflows/privacy.yml` | the privacy deny-list scan (working tree **and** full history) plus gitleaks, and a self-test proving the deny-list bites |
| `.github/workflows/dependency-review.yml` | refuses a PR introducing a vulnerable or badly licensed dependency |
| `.github/workflows/pr-title.yml` | the PR title must be a conventional commit (squash merges make it the release note) |
| `.github/workflows/release.yml` | release-please: one release PR carries the next version and its changelog |
| `.github/workflows/docker-build-push.yml` | builds the image, pushes it to Harbor and scans it (fails above 0 critical / 5 high CVEs) |
| `.github/workflows/ghcr-publish.yml` | publishes the image (amd64/arm64) to GHCR in parallel |

### Releases

Releases are cut by **release-please**, never by hand. Every push to `main`
updates one open release PR with the next version and its `CHANGELOG.md` entry,
built from the conventional commits merged since the last release; merging that
PR is the release — it tags `vX.Y.Z`, publishes the GitHub Release, and starts
the image builds on the tag. Never edit `app/__version__.py` by hand: the
version line sits between `x-release-please-start-version` /
`x-release-please-end` markers and is rewritten by that PR.

The GHCR workflow checks that the tag matches `app/__version__.py`, so a tag
that does not correspond fails the build rather than publishing a mislabelled
image.

### Image publishing

`.github/workflows/docker-build-push.yml` builds the image through the reusable
`vchatela-org/shared-workflows` workflow (`docker-build-push-harbor.yml`), pushes
it to Harbor, then has it scanned:

- push to `main` → tag `:dev`;
- tag `v*` → tags `:vX.Y.Z` and `:latest`.

`.github/workflows/ghcr-publish.yml` publishes the image (amd64/arm64) to GHCR
(`ghcr.io/vchatela-org/takeback`) in parallel: push to `main` → `:latest` and
`:sha-…`; tag `v*.*.*` → `:X.Y.Z`, `:X.Y` and `:X` (the tag must match
`app/__version__.py`, otherwise the job fails).

Repository variables: `HARBOR_REGISTRY`, `HARBOR_PROJECT`, `HARBOR_IMAGE`;
secrets: `HARBOR_USERNAME`, `HARBOR_PASSWORD` (a Harbor robot account), and
`PRIVACY_DENYLIST_LOCAL` (the private half of the privacy deny-list — see
[`docs/privacy.md`](docs/privacy.md)).
