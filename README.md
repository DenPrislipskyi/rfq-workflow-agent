# rfq-workflow-agent

Reads the shared mailbox of a marine ship chandler, decides what each email is,
and for a customer's Request For Quotation fills in the desk's RFQ form and
forwards it to the regional desk with the form attached.

```
email arrives
  → cheap rules decide the obvious ones          no model, no downloads
  → attachments downloaded and parsed            no model
  → each file read                               1 model call per file
  → is this an RFQ?                              1 model call
  → the form's header                            1 model call
  → values mapped onto what the cells accept     no model
  → the form written, the email forwarded        no model
  → each line said in our own words              1 model call
  → each line looked up in the catalogue         no model
  → which product it is, or none                 1 model call
```

An ordinary RFQ costs **five model calls**, whatever its size: a requisition of
200 rows costs the same as one of five, because the model names the columns and
**code copies the rows**. That division is the load-bearing decision of the whole
service - a loop cannot drop a row or reorder one, and a transcribing model can.

Everything the agent could not do is reported rather than hidden: an empty cell
carries the reason it is empty, a file that could not be opened is named in the
form and in the forwarded email, and every decision is journalled.

## Running it

```bash
uv sync
cp .env.example .env          # then fill it in - every value is documented there
uv run uvicorn src.main:app --reload --port 8000
```

The mailbox is watched over a Microsoft Graph webhook, so a public URL is needed
in development (`NGROK_URL`). With `OUTLOOK_ENABLED=false` the service serves
HTTP only, and `POST /api/v1/emails/classify` classifies a pasted email without
touching a mailbox.

```bash
docker build -t rfq-workflow-agent . && docker run --env-file .env -p 8000:8000 rfq-workflow-agent
```

### The switches worth knowing

| | |
|---|---|
| `FORWARD_ENABLED` | **off by default.** The first action that puts real mail in somebody's inbox |
| `FORWARD_WORKBOOK` | attach the filled form to the forward. Needs `Mail.ReadWrite` on top of `Mail.Send` |
| `EXTRACTION_ENABLED` | read the RFQ and fill the form. Off leaves classification and forwarding untouched |
| `TRIAGE_READS_ATTACHMENTS` | read the files **before** deciding whether the email is an RFQ |
| `MATCHING_ENABLED` | look each line up in the catalogue. Runs after the forward, so nobody waits for it |
| `DATABASE_ENABLED` | write a folder per email under `Database/` - what the front end reads |
| `LOG_LEVEL` / `LOG_LIBRARY_LEVEL` | ours and the libraries', pinned separately |

`.env.example` documents every value and why it has the default it has.

## Reading a run

One line per stage, and every line carries the email it belongs to:

```
INFO | AAMk-1/0b072262 | handlers | Done | NEW_RFQ | FORWARD_TO_DST | 15 item(s), 1 starred cell(s) blank | labelled ['SSG RFQ'] | SENT | 3 call(s), 9.4s
```

`grep Done` is one row per email. The tag pairs the Graph message with the
journal: the decision id in it names both the line in `data/decisions.jsonl` and
the filled form in `data/workbooks/`.

## The catalogue

Our own product list lives in a Google Sheet, because that is where the desk
keeps it and adds to it by hand. The agent never reads it on the path of an
arriving email:

```
Google Sheet          edited by hand - the source of truth
     │  every CATALOG_REFRESH_MINUTES, and once at boot
     ▼
data/catalog/items.csv      snapshot, plus when it was taken and its checksum
     │  indexed in memory, once
     ▼
by code (exact)  +  by description (BM25 over an inverted index)
```

An item typed into the sheet is in the system on the next refresh. Google being
slow, rate-limiting or down costs nothing: the snapshot keeps working, and a
sheet that comes back empty never replaces a catalogue that does not.

The sheet has to be **published** - Share → Anyone with the link → Viewer -
because the export endpoint is fetched without credentials. A private sheet
would need a service account, and `PublishedSheet` would grow an auth header.

```bash
uv run python -m src.tools.sync_catalog      # copy the sheet now
uv run python -m src.tools.describe_lines    # restate the customer wordings
uv run python -m src.tools.catalog_recall    # how often the right item is found
```

`catalog_recall` hides each case's own wording before searching for it -
otherwise it would be looking for a sentence with that sentence in the index,
which scores perfectly and proves nothing.

**A quoted code is a candidate, not an answer.** The desk's own matching notes
say so - *"do not trust the code alone, validate the requested customer
description"* - and the two examples they sent are both codes that lead
somewhere else: one asks for an external hard drive and names a fishing rod.
`Catalog.shortlist` therefore returns the code's item **and** says whether
searching the customer's words found it too. Where the two disagree, the
shortlist says `conflicted`, and a person settles it.

`catalog_recall` is the number that bounds everything built on top of this:
whatever chooses between candidates later - a model, an operator - can only
choose from what the shortlist showed it.

## What the front end reads

Every email the agent sees gets a folder of its own under `Database/`:

```
Database/
└── 2026-09-10T14-22-31Z__a1b2c3d4/
    ├── email.json                the message, the verdict, where it went
    ├── body.txt                  the body as text
    ├── Requisition.xlsx          the customer's files, exactly as they arrived
    └── KASS_RFQ_ALMI_HYDRA.xlsx  the copy of the desk's form we filled
```

One flat folder: opening it shows the email and everything that came with it,
and `email.json` says which file is whose. One file per email, **rewritten** as
the pipeline learns more - which is the opposite of the journal beside it, and
deliberately so. `decisions.jsonl` is append-only and answers "how was this
decided?"; a record answers "where does this email stand?", and a page must not
have to fold three lines to find out.

```
GET /api/v1/quotes?page=&pageSize=&search=   one row per email, newest first
GET /api/v1/quotes/stream                    "the list changed", as it happens
GET /api/v1/quotes/{id}                      one email in full
GET /api/v1/quotes/{id}/files/{path}         an attachment, or the filled form
```

`stream` is server-sent events and carries no payload: whatever changed, the
page's answer is to read the list again. It is in-process, so a record written
by another process - the backfill tool below - is not announced to a running
server.

The rows are mapped field by field: a record holds the customer's own words and
the model's reasoning, and neither belongs in a browser. Most columns come back
empty on purpose - the agent knows what an email is and what it did with it, not
who is responsible for it or how far along it is.

The folder holds real customer mail and real customer files, so it is not in git.
An existing journal can be turned into records without a model or a mailbox:

```bash
uv run python -m src.tools.backfill_database
```

## Layout

```
src/
├── api/                 HTTP endpoints and the Graph webhook
├── core/                config, logging, the composition root
├── domain/              the business rules. No I/O, no model, no framework
├── infrastructure/      Graph, the models, document parsers, the xlsx writer
│   ├── catalog/         the product sheet, and the snapshot of it on disk
│   └── documents/       bytes → text, tables, images. After this, formats do not exist
└── services/
    ├── classification/  is this an RFQ?
    ├── extraction/      read the RFQ, fill the header, map it onto the cells
    └── matching/        say each line in our words, then find it in the catalogue
```

Dependencies point one way only - `domain ← infrastructure ← services ← api` -
and `tests/test_layers.py` fails the build if an import goes the other way.

## Tests

```bash
uv run pytest -q
```

No network and no model: every provider call is faked, and what is under test is
the wiring, the guardrails and the arithmetic. Two of them compare a generated
form against a real one filled in by hand, cell by cell.

Tools that need neither a model nor a mailbox:

```bash
uv run python -m src.tools.inspect_attachments <folder>     # what the parsers make of files
uv run python -m src.tools.fill_template                    # fill a form from a sample
uv run python -m src.tools.make_output_template <rfq.xlsx>  # a blank form from a filled one
uv run python -m src.tools.sync_catalog                     # copy the product sheet
uv run python -m src.tools.describe_lines                   # restate what customers wrote
uv run python -m src.tools.catalog_recall                   # measure the shortlist
uv run python -m src.tools.backfill_database                # records from the journal
```

## Documentation

`docs/agent/FLOW.md` is the living document: how every step works, what was
measured, what each decision cost. `docs/agent/DECISIONS.md` records why the
design is what it is and what is still open.

`docs/agent/BUILD.md` is where to start on the newer half - the `Database/`
folder, the product catalogue, the matching pipeline and the HTTP the front end
reads: what each piece is, why it is shaped that way, and what was measured to
decide it. `docs/agent/MATCHING_PLAN.md` is the plan it was built from.

Both live under `docs/`, which is **not in git**: that folder also holds the
customer's own workbooks, their finished RFQs and real emails, and none of that
belongs in a repository.
