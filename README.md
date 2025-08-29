# Minimal AI Workflow – Email → Structured Data → Reply → Quote JSON

This repository contains a self‑contained Python implementation of a simple AI
workflow that ingests raw inquiry emails, extracts structured information,
drafts an acknowledgment response, generates a quote based on a local price
list and tiered discount rules, and records an activity log.  The workflow
matches the specification described in the assignment brief for a deterministic
offline system that processes an inbox directory and persists artefacts to the
filesystem.

## Project structure

```text
ai_workflow/
├── process_emails.py      # Entry‑point script implementing the workflow
├── price_list.json        # Sample price list (unit prices and units of measure)
├── discount_rules.json    # Tiered discount definitions (min quantity and discount)
├── config.json            # Configuration defaults (tax rate, currency, SLA text)
├── samples/
│   └── inbox/             # Folder containing example email inquiries (.txt)
├── data/
│   ├── events/            # Parsed event JSON outputs (one per email)
│   ├── outbox/            # Acknowledgment draft JSON outputs
│   ├── quotes/            # Quote JSON outputs
│   └── timeline/          # Append‑only activity log (`activity.jsonl`)
└── README.md
```

## Running the workflow

The workflow is triggered via a single command.  From the repository root
(`ai_workflow`), run:

```bash
python3 process_emails.py --inbox samples/inbox --data data
```

The script will:

1. Load `price_list.json`, `discount_rules.json` and `config.json` from the
   repository directory.
2. Iterate over all `.txt` files in the specified `inbox` directory.
3. For each email:
   - Generate a stable identifier (`email_id`) by hashing the entire email
     content.  Idempotent processing is achieved by checking whether an
     existing `events/<email_id>.json` already exists and skipping if so.
   - Parse the email into a strict event schema with per‑field confidence
     values and notes.  Missing or ambiguous fields are recorded in
     `missing_fields`.
   - Draft a professional acknowledgment that summarises the request and
     asks up to two clarifying questions based on `missing_fields`.  The
     message never invents unavailable facts (e.g., pricing for unknown
     products).
   - Compute a quote using the local price list and discount rules.  Each
     line item includes quantity, unit price, discount rate, discount amount
     and line subtotal.  Totals are aggregated and tax is applied using the
     configured tax rate.  If any required information is missing (such as
     quantity or unknown products), the quote is marked `pending` and no
     totals are computed.
   - Append a record of each step to `data/timeline/activity.jsonl` with an
     ISO‑8601 timestamp (Asia/Kolkata timezone), email ID, step name,
     status (`success`, `pending`, `error` or `skipped`) and a brief message.

Re‑running the command on the same input directory will not create duplicate
outputs thanks to the email ID–based idempotency check.

## Event schema

Each parsed email is persisted as `data/events/<email_id>.json` using the
following schema:

| Field               | Description                                                                                                        |
|---------------------|--------------------------------------------------------------------------------------------------------------------|
| `email_id`          | Stable hexadecimal identifier (first 16 characters of a SHA‑256 hash of the email content)                         |
| `from.value`        | Sender email address extracted from the `From:` header                                                              |
| `from.confidence`   | Confidence (0–1) that the sender was correctly extracted                                                            |
| `from.notes`        | Notes (e.g., reasons if missing)                                                                                   |
| `subject.value`     | Subject line extracted from the `Subject:` header                                                                   |
| `subject.confidence`| Confidence that the subject was correctly extracted                                                                 |
| `subject.notes`     | Notes (e.g., reasons if missing)                                                                                   |
| `items`             | Array of requested items; each item contains fields described below                                                 |
| `currency.value`    | Detected currency (e.g., `INR`, `USD`); defaults to configuration if not found in the email                        |
| `currency.confidence`| Confidence for the currency inference                                                                              |
| `currency.notes`    | Notes (e.g., “default currency assumed”)                                                                           |
| `missing_fields`    | List of missing or ambiguous fields detected during parsing (e.g., `quantity for Widget`, `price for Doohickeys`)   |

Each entry in `items` has this structure:

| Item field           | Description                                                                                                       |
|----------------------|-------------------------------------------------------------------------------------------------------------------|
| `product_name.value` | Canonical product name; unknown names are captured with a note “unknown product”                                  |
| `product_name.confidence`| Confidence (0–1) that the product name was correctly identified                                                |
| `product_name.notes` | Notes (e.g., “unknown product”)                                                                                   |
| `quantity.value`     | Parsed quantity as an integer or `null` when not specified                                                         |
| `quantity.confidence`| Confidence associated with the quantity                                                                            |
| `quantity.notes`     | Notes when quantity is missing                                                                                     |
| `unit.value`         | Unit of measure (from price list or configuration default)                                                         |
| `unit.confidence`    | Confidence for the unit                                                                                            |
| `unit.notes`         | Additional notes                                                                                                   |

## Quote schema

Quotes are saved under `data/quotes/<email_id>.json` using this schema:

| Field            | Description                                                                                                            |
|------------------|------------------------------------------------------------------------------------------------------------------------|
| `email_id`       | Same stable identifier as in the event JSON                                                                             |
| `status`         | Either `complete` when all data is present, or `pending` if any required information is missing                         |
| `line_items`     | Array of objects describing each requested product.  Missing quantities or unknown products retain `null` fields        |
| `subtotal`       | Sum of line item subtotals before tax (rounded to two decimal places)                                                   |
| `tax`            | Tax computed from the subtotal using the configured tax rate                                                            |
| `total`          | Subtotal plus tax (rounded to two decimal places)                                                                      |
| `currency`       | Currency used (propagated from the event)                                                                               |
| `missing_fields` | List of missing inputs preventing completion (e.g., `quantity for Gadget`, `price for Doohickeys`)                       |

Each line item in `line_items` includes:

| Line item field   | Description                                                                                                           |
|-------------------|-----------------------------------------------------------------------------------------------------------------------|
| `product_name`    | Canonical product name                                                                                                |
| `unit_price`      | Unit price from `price_list.json` or `null` if unavailable                                                             |
| `quantity`        | Requested quantity or `null` if unknown                                                                               |
| `discount_rate`   | Discount rate applied based on `discount_rules.json` (e.g., `0.05` for 5 %)                                            |
| `discount_amount` | Absolute amount discounted (unit price × quantity × discount rate)                                                     |
| `subtotal`        | Line subtotal after discount (`unit_price × quantity – discount_amount`) or `null` if incomplete                      |

## Design choices and assumptions

* **Offline and deterministic.**  The workflow runs entirely on local files
  without network access and produces deterministic output.  The only state
  external to the function is the filesystem; no in‑memory caches are used.

* **Stable identifiers and idempotency.**  Each email is keyed by a SHA‑256
  hash of its raw contents.  Re‑processing the same file will skip work if
  corresponding outputs already exist, preventing duplicate artefacts.

* **Heuristic extraction.**  Parsing uses regular expressions and simple
  heuristics instead of ML models.  Quantities are detected when a number
  appears immediately before a recognised product name (allowing one
  optional descriptive word).  Product names are recognised case
  insensitively from `price_list.json`.  Unknown names are detected when
  they appear alongside known products (e.g., “widgets and doohickeys”) or
  immediately after ordering verbs (e.g., “need doohickeys”).  To avoid
  capturing personal names or filler words, unknown candidates must appear
  plural (end with “s”) and pass a stop‑word filter.

* **Confidence scoring.**  Each extracted field includes a confidence
  between 0.0 and 1.0.  Explicitly stated values (e.g., quantity “15
  widgets”) receive high confidence (≥ 0.9); inferred or missing values get
  lower confidence.  Missing or ambiguous data is recorded in
  `missing_fields` to drive follow‑up questions and pending quotes.

* **Acknowledgment drafting.**  The draft message references the
  understood products and quantities and asks up to two clarifying
  questions based on missing fields.  It uses the sender’s address to
  personalise the greeting and includes the SLA text from `config.json`.

* **Deterministic pricing.**  Unit prices and units of measure are sourced
  from `price_list.json`.  Tiered discounts are applied using
  `discount_rules.json`, which is sorted in descending order of minimum
  quantity to select the largest applicable discount.  All monetary
  calculations use floats with explicit rounding to two decimal places to
  avoid floating‑point drift.  Unknown products or missing quantities
  produce `pending` quotes without totals.

* **Error handling.**  The workflow tolerates malformed inputs: any
  exception during reading or parsing results in an error entry in the
  timeline and processing proceeds to the next email.  Logging continues
  even when later stages encounter problems.

* **Limitations.**  Natural language understanding is heuristic and not
  exhaustive.  For example, complex phrasings (“I need between five and
  ten widgets”) or units other than simple counts may not be parsed
  correctly.  Currency conversion is not supported; if a different
  currency is detected, the same numeric prices are used and flagged in
  the event.  Only a single unit of measure per product is currently
  supported.

## Example outputs

Below are excerpts of the artefacts generated for the sample email
“Request for Widget” in `samples/inbox/email1.txt`:

**Parsed event** (`data/events/5fa7d8f193847365.json`):

```json
{
  "email_id": "5fa7d8f193847365",
  "from": { "value": "john@example.com", "confidence": 0.95, "notes": "" },
  "subject": { "value": "Request for Widget", "confidence": 0.95, "notes": "" },
  "items": [
    {
      "product_name": { "value": "Widget", "confidence": 0.9, "notes": "" },
      "quantity": { "value": 15, "confidence": 0.9, "notes": "" },
      "unit": { "value": "piece", "confidence": 0.8, "notes": "" }
    }
  ],
  "currency": { "value": "INR", "confidence": 0.5, "notes": "default currency assumed" },
  "missing_fields": []
}
```

**Acknowledgment draft** (`data/outbox/5fa7d8f193847365_ack.json`):

```json
{
  "email_id": "5fa7d8f193847365",
  "to": "john@example.com",
  "subject": "Re: Request for Widget",
  "body": "Hello John,\n\nThank you for reaching out to us regarding 15 Widget(s).\n\nWe aim to respond to all inquiries within 24 hours.\n\nKind regards,\nSales Team",
  "missing_fields": [],
  "questions": []
}
```

**Quote** (`data/quotes/5fa7d8f193847365.json`):

```json
{
  "email_id": "5fa7d8f193847365",
  "status": "complete",
  "line_items": [
    {
      "product_name": "Widget",
      "unit_price": 100,
      "quantity": 15,
      "discount_rate": 0.05,
      "discount_amount": 75,
      "subtotal": 1425
    }
  ],
  "subtotal": 1425,
  "tax": 256.5,
  "total": 1681.5,
  "currency": "INR",
  "missing_fields": []
}
```

These examples illustrate how the workflow extracts structured data, generates a
human‑readable acknowledgment, and computes a deterministic quote using the
local price list and discount rules.