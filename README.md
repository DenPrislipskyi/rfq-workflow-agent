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
```

An ordinary RFQ costs **three model calls**, whatever its size: a requisition of
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

## Layout

```
src/
├── api/                 HTTP endpoints and the Graph webhook
├── core/                config, logging, the composition root
├── domain/              the business rules. No I/O, no model, no framework
├── infrastructure/      Graph, the models, document parsers, the xlsx writer
│   └── documents/       bytes → text, tables, images. After this, formats do not exist
└── services/
    ├── classification/  is this an RFQ?
    └── extraction/      read the RFQ, fill the header, map it onto the cells
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
```

## Documentation

`docs/agent/FLOW.md` is the living document: how every step works, what was
measured, what each decision cost. `docs/agent/DECISIONS.md` records why the
design is what it is and what is still open.

Both live under `docs/`, which is **not in git**: that folder also holds the
customer's own workbooks, their finished RFQs and real emails, and none of that
belongs in a repository.
