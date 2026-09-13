# Seven more cases, aimed at the matching

`test_cases/` covers the shape of the pipeline: is this an RFQ, can the files be
opened, does the form get filled. **This set covers the part that changed** -
the three matching branches, the 80% threshold that chooses between them, and
the places where the sheet itself is the problem.

```bash
uv run python test_cases_2/build.py    rebuild all seven from the catalogue
uv run python test_cases_2/run.py 1    run one case
uv run python test_cases_2/run.py all  run all seven
```

The run uses the **same handler** the Graph webhook does: the same model, the
same catalogue, the same record store. Exactly two things are replaced - the
mailbox serves files from the folder instead of calling Graph, and **forwarding
is off**. The result lands in `Database/`, so it shows on the RFQ screens like
any real email. **One model call per case** now that matching chooses nothing.

---

## What each case is for

| # | Case | The question it asks |
|---|---|---|
| 1 | `threshold_edge` | does 80% actually decide anything? |
| 2 | `one_code_many_products` | one customer code, several products behind it |
| 3 | `sizes_that_differ_by_one_token` | M12 against M8, size 27 against 25 |
| 4 | `purchase_order_not_rfq` | an order that looks exactly like an RFQ |
| 5 | `long_requisition` | 60 lines for the price of one |
| 6 | `messy_numbers_and_units` | `1,5`, `2 coil`, `0000512`, a blank quantity |
| 7 | `hostile_filenames` | a filename is untrusted input that becomes a path |

### 1. `threshold_edge`

The same bolt, the same code, twice - once word for word as the sheet has it,
once rewritten the way an engineer would say it. Nothing else differs, so if
both lines come back the same way the threshold is doing nothing.

### 2. `one_code_many_products`

`79 54 96` is filed against **two** item codes, one per size; `312374` is filed
twice against **one** item code, two sizes apart. Both are real rows of the
customer's sheet. This case is expected to look bad - it documents a limit of
the data, not a bug in the code.

### 3. `sizes_that_differ_by_one_token`

No codes at all, against products whose descriptions are identical except for
one number. The first candidate always reads 100%, so **read the order, not the
number**: a wrong size in first place is the failure this case exists to catch.

### 4. `purchase_order_not_rfq`

A PO with a vessel, a port, a line list and prices. Everything that makes an RFQ
look like an RFQ, and it is a different piece of work for a different team.
Nothing forwarded, no form, no matching - and the record still kept.

### 5. `long_requisition`

Sixty lines. Nothing here is hard to match; the point is that a long RFQ costs
the same as a short one and comes back in the order it arrived.

### 6. `messy_numbers_and_units`

Quantities as a storekeeper types them. `1,5` must not become `15`, `2 coil`
must not be split, `0000512` must keep its zeros, the duplicated line must stay
duplicated, and a missing quantity must stay missing rather than become 1.

### 7. `hostile_filenames`

A dot-file, both of the record store's reserved names, a name in another script
with an emoji, and a 188-character name. One of them holds the real requisition.
Aimed squarely at `_safe_name` in `src/infrastructure/storage/records.py`.

---

## Why these folders are not in git

They are built from the customer's real product catalogue. `.gitignore` holds
`test_cases_2/*/` for the same reason it holds `test_cases/*/`, `data/` and
`Database/`. `build.py`, `run.py` and this file stay in git, so one command
reproduces the set on any machine that has a catalogue snapshot.
