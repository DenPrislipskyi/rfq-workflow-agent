# Five cases for the three fixes

`test_cases/` covers the shape of the pipeline. `test_cases_2/` covers the
matching. **This set checks that the three things just fixed actually stayed
fixed**, and adds two routing shapes neither of the others has.

```bash
uv run python test_cases_3/build.py    rebuild all five from the catalogue
uv run python test_cases_3/run.py 1    run one case
uv run python test_cases_3/run.py all  run all five
```

Same handler as the Graph webhook, same model, same catalogue, same record
store. Two things are replaced: the mailbox serves files from the folder, and
**forwarding is off**. Results land in `Database/`. **One model call per case**
for the matching, plus the reading.

---

| # | Case | What it proves |
|---|---|---|
| 1 | `words_the_sheet_already_knows` | the agreement is measured on the customer's own sentence |
| 2 | `port_only_in_the_attachment` | routing reads the full header, not the verdict's guess |
| 3 | `quantities_that_are_missing` | an uncounted line survives; a section heading still does not |
| 4 | `two_ports_one_email` | two desks match at once — guess neither |
| 5 | `a_revision_in_a_reply` | route on the newest message, not on the port quoted below it |

### 1. `words_the_sheet_already_knows` — the main regression

Three lines written **word for word** as the sheet files those products, and
three that are not. Until the fix, a line matching the sheet exactly had its
code dropped: we compared our restatement of it (`RULE CONVEX`) against their
sentence (`Convex rulers`) and called the 50% difference disagreement.

**Lines 1–3 must be `code_confirmed`.** If any comes back `code_rejected`, the
fix did not hold. Lines 4 and 5 must still be rejected — the same code against a
different product, and a code against a fire hose.

### 2. `port_only_in_the_attachment` — the routing regression

Neither subject nor body names a port. `Port Rashid` is in the rows above the
table in the attachment. This used to go nowhere: routing asked the verdict pass
for the port, and the verdict pass is asked for a verdict.

**Must be `SENT` to the UAE desk**, with no `SSG Not Sent` label.

### 3. `quantities_that_are_missing` — the dropped-line regression

Three catalogue headings (`FASTENERS [69]`, `SAFETY [85]`, `MEASURING [65]`), a
spacer row, and **two lines nobody put a quantity against**. The headings must be
dropped and the uncounted lines must not.

**Exactly 4 lines, with lines 2 and 3 carrying an empty quantity.**

### 4. `two_ports_one_email` — not sending is the right answer

The vessel is in Singapore and sails for Dubai; the customer says the delivery
place is undecided and asks to quote both. Two desks match at once, and
`resolve_region` stops rather than falling through to a weaker signal.

**Must NOT be forwarded**: `NO_REGION`, labels `SSG RFQ` + `SSG Not Sent`, form
still filled, lines still matched. Sending this to one desk would be the failure.

### 5. `a_revision_in_a_reply` — the newest message wins

A reply moving delivery from Singapore to Fujairah, with the original message
quoted underneath still saying Singapore.

**Must go to the UAE desk.** Routing to Singapore is what this case exists to
catch.

---

## Why these folders are not in git

Built from the customer's real product catalogue, like the other two sets.
`.gitignore` holds `test_cases_3/*/`; `build.py`, `run.py` and this file stay in
git, so one command reproduces the set anywhere there is a catalogue snapshot.
