# GTB DLP KB → Claude project knowledge

Exports every article and attachment from the GTB DLP knowledge base (`https://kb.gttb.com`,
login required), removes duplicates, and builds a folder you can upload straight into a
Claude project's knowledge.

```
login ─▶ crawl ─▶ extract ─▶ dedup ─▶ (you review near-duplicates) ─▶ bundle ─▶ upload/
```

Run it on your own machine: the portal needs your login, and your password never goes
into this repo or into a chat. Only session cookies are saved, under `kb_data/auth/`, which
is git-ignored along with everything else downloaded.

## Setup (once)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

**1. Log in.** A Chromium window opens on the login page. Log in as you normally would and
the window closes itself once you're in.

```bash
python -m gtbkb login
```

Prefer not to type it? Set `KB_USERNAME` and `KB_PASSWORD` in your shell for that one command
and add `--headless`. They're read from the environment and never written anywhere.

**2. Crawl.** Downloads every page and attachment, about 0.5s apart so it's gentle on the
portal. For a few hundred items that's roughly 5–10 minutes.

```bash
python -m gtbkb crawl
```

It's resumable: if it stops (network blip, session timeout), log in again and re-run, and
it picks up where it left off. It never follows logout, rating, print/export, email,
search or sort links (see `exclude_patterns` in `gtbkb/config.py`).

When it finishes, have a look at `kb_data/crawl_report.txt`. It holds only URL shapes and
counts, no content, so it's safe to share if the numbers look off. The portal's category
counts add up to about 400 articles, so expect roughly that many `article.php?id=N` pages.

**3. Extract, dedup, bundle.**

```bash
python -m gtbkb extract
python -m gtbkb dedup
```

Check that a few articles were read properly:

```bash
python -m gtbkb inspect
```

It shows, for three sample articles, the title, category and which block of the page was
taken as the article body, with structure and counts only, so it's safe to share. Add
`--preview` to see the start of the extracted text yourself.

Then review `kb_data/review/near_duplicates.csv` (see below), and build the upload folder:

```bash
python -m gtbkb bundle
```

**4. Upload** everything in `kb_data/upload/` to the Claude project's knowledge.

## How duplicates are handled

| Layer | What it catches | What happens |
|---|---|---|
| URL normalisation | the same page via `utm_` params, `#anchors`, parameter order, `:443`, `//` | fetched once |
| Exact file match (SHA-256 of bytes) | the same PDF linked from several articles or mirrored on another URL | one copy kept automatically |
| Exact text match (hash of normalised wording, date stamps ignored) | the same article under two IDs, or re-published with a new "Last updated" | newest kept automatically |
| Near-duplicate (5-word shingle similarity) | revised copies, v7.0 vs v7.1 guides, an article copied out of a PDF | **reported, never dropped on its own** |

Exact drops are listed in `kb_data/review/exact_duplicates.csv` for the record.

For near-duplicates, open `kb_data/review/near_duplicates.csv` in Excel. Each row is a
pair (`a_…`, `b_…`) with its similarity scores and a suggestion, for example:

- *different product versions? keep both unless the older one is obsolete*
- *revised copy? b is newer, drop_a if it supersedes*
- *a is mostly contained in the other, drop_a if nothing unique in it*

Fill the `decision` column with `keep_both`, `drop_a` or `drop_b`. A blank means keep both.
Decisions are preserved when you re-run `dedup`, then `bundle` applies them.
`kb_data/review/final_inventory.csv` lists every item as kept or dropped, with the reason.

## What ends up in `upload/`

```
INDEX.md                 every kept item: title, type, category, date, which file, source URL
articles/<category>.md   articles bundled per category, each starting with Source / Category / Last updated
pdfs/<title>.md          text extracted from each PDF (with page markers)
pdfs_original/           scanned PDFs with no text layer (and all PDFs if pdf_mode is original/both)
other_files/             any non-PDF attachments, as downloaded
```

Bundling per category keeps the file count down, and the `Source:` line on every article
means Claude can point you back to the original page.

## How article text is cleaned

The portal runs PHPKB. Every page has the full category tree and a table of contents in a
side column, and that has to stay out of the article text. Otherwise every article would
share the same few hundred words, which wastes project space and makes unrelated articles
look alike. Three things handle it:

- **Body selection by score.** The extractor looks at the blocks around the article title and
  scores each one as prose words minus link words. The article column wins; the
  link-heavy tree and TOC lose.
- **Template lines.** Any line that appears on at least half of all articles (like
  "Was this article helpful? Yes No") is removed. Headings such as "Summary" are kept. What was
  removed is listed in `kb_data/review/boilerplate_lines.txt`.
- **Meta bar.** The "402 · Security Events · 0" bar under each title is dropped (the category
  is in the article header anyway). Without this, the article's own ID would stop two
  identical articles from matching as exact duplicates.

Titles come from the article heading, so titles that contain " - " stay whole.

## Tuning for the real portal

After the first crawl, `extract` and `inspect` tell you if something needs adjusting;
all of it goes in `config.yaml`:

- **Articles missed or listing pages included:** add the article URL shape from
  `crawl_report.txt` to `extra_article_patterns`, e.g. `'article\.php\?id=\d+'`.
- **Article text includes menus/sidebars**, or `inspect` shows the wrong "Body taken":
  add the article container's CSS selector to `extra_content_selectors`
  (in the browser: right-click the article text → Inspect).
- **Noisy URLs in the report:** add a regex to `extra_exclude_patterns`.
- **Article body empty in `kb_data/raw/pages/*.html`:** the portal renders with JavaScript;
  set `render_js: true`.
- **Too many / too few near-duplicate pairs:** adjust `near_duplicate_threshold`
  (default 0.80) and `containment_threshold` (0.90).

After changing the config, re-run `extract`, `dedup` and `bundle`. There's no need to crawl again.

## Tests

```bash
python -m pytest -q
```

Runs the whole pipeline against a local mock KB laid out like the real PHPKB portal
(category tree and TOC beside every article, meta bar, two-level breadcrumbs, Glossary),
with a login wall, trap links (logout, rate, print, export, email, subscribe, search,
Ask a Question, the Account menu) and planted duplicates of every kind above.
