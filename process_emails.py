

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_json_file(path: Path) -> Any:
    """Load JSON from a file and return the parsed object."""
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def save_json_file(path: Path, data: Any) -> None:
    """Save JSON data to a file with pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    with tmp_path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append a single JSON object as a line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def current_timestamp_iso() -> str:
    """Return the current timestamp in ISO‑8601 format with timezone offset for Asia/Kolkata."""
  
    ist_offset = timedelta(hours=5, minutes=30)
    ist = timezone(ist_offset)
    now = datetime.now(ist)
    return now.isoformat(timespec='seconds')


class EmailParser:
    """Utility class to parse email text into structured fields."""

    def __init__(self, price_list: Dict[str, Dict[str, Any]], config: Dict[str, Any]):
        self.price_list = price_list
        self.config = config
        # Preprocess product names for case‑insensitive search and plural forms.
        # Map lower‑cased names to canonical names.
        self.product_names = {}
        for name in price_list.keys():
            lc = name.lower()
            self.product_names[lc] = name
            # naive plural: append 's' if not already endswith s
            if not lc.endswith('s'):
                self.product_names[lc + 's'] = name

    def parse(self, text: str) -> Dict[str, Any]:
        """Parse a raw email string into a structured event dict."""
        # Generate a stable identifier based on the entire email content.
        email_id = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

        # Initialise event structure.
        event: Dict[str, Any] = {
            'email_id': email_id,
            'from': {'value': None, 'confidence': 0.0, 'notes': ''},
            'subject': {'value': None, 'confidence': 0.0, 'notes': ''},
            'items': [],
            'currency': {'value': None, 'confidence': 0.0, 'notes': ''},
            'missing_fields': []
        }

        # Split header and body roughly by first blank line.
        parts = text.split('\n\n', 1)
        header = parts[0] if parts else ''
        body = parts[1] if len(parts) > 1 else ''

        # Extract From and Subject lines.
        for line in header.splitlines():
            if line.lower().startswith('from:'):
                value = line.split(':', 1)[1].strip()
                event['from'] = {'value': value, 'confidence': 0.95 if value else 0.0, 'notes': '' if value else 'missing sender'}
            elif line.lower().startswith('subject:'):
                value = line.split(':', 1)[1].strip()
                event['subject'] = {'value': value, 'confidence': 0.95 if value else 0.0, 'notes': '' if value else 'missing subject'}

        # Determine currency from body. Look for currency keywords.
        currency_match = None
        body_lower = body.lower()
        if 'usd' in body_lower or '$' in body_lower:
            currency_match = 'USD'
        elif 'inr' in body_lower or '₹' in body_lower or 'rupees' in body_lower:
            currency_match = 'INR'
        if currency_match:
            event['currency'] = {'value': currency_match, 'confidence': 0.8, 'notes': ''}
        else:
            # Use default
            event['currency'] = {'value': self.config.get('currency'), 'confidence': 0.5, 'notes': 'default currency assumed'}

     
        items_found: List[Tuple[str, Optional[int], float, str]] = []

        for key, canonical_name in self.product_names.items():
            # Avoid duplicate detection for canonical and plural entries.
            if canonical_name.lower() != key and canonical_name.lower() + 's' != key:
                continue

            qty_pattern = re.compile(r'\b(\d+)\s+(?:\w+\s+)?' + re.escape(key) + r'\b', re.IGNORECASE)
            for match in qty_pattern.finditer(body_lower):
                qty_str = match.group(1)
                quantity = int(qty_str) if qty_str else None
                # High confidence when quantity is explicitly mentioned
                items_found.append((canonical_name, quantity, 0.9, ''))

            plain_pattern = re.compile(r'\b' + re.escape(key) + r'\b', re.IGNORECASE)
            for match in plain_pattern.finditer(body_lower):
                # Determine if this occurrence overlaps with a quantity match.  We'll skip if there
                # exists a quantity match that spans the same product substring.
                start, end = match.span()
                overlaps_quantity = False
                for qmatch in qty_pattern.finditer(body_lower):
                    qs, qe = qmatch.span()
                    if qs <= start and end <= qe:
                        overlaps_quantity = True
                        break
                if overlaps_quantity:
                    continue
                # No quantity found for this occurrence; treat as missing quantity.
                items_found.append((canonical_name, None, 0.6, ''))

        stopwords = {
            'some', 'any', 'more', 'few', 'asap', 'pricing', 'price', 'cost', 'time',
            'info', 'information', 'availability', 'please', 'could', 'looking', 'items', 'item',
            'product', 'products', 'it', 'them', 'they', 'your', 'our', 'quote', 'yet', 'us', 'about',
            'to', 'and', 'thanks', 'thank', 'get', 'me', 'how', 'many', 'if', 'there', 'let', 'know',
            'hello', 'hi', 'we', 'i', 'my', 'you', 'also', 'regards', 'cheers', 'kind', 'best', 'team'
        }
        unknown_candidates: List[str] = []

        and_pattern = re.compile(r'\b([\w\-]+)\s+and\s+([\w\-]+)\b', re.IGNORECASE)
        for m in and_pattern.finditer(body):
            first, second = m.group(1), m.group(2)
            first_lc, second_lc = first.lower(), second.lower()
            # If exactly one of the pair is known, treat the other as unknown.
            if (first_lc in self.product_names) ^ (second_lc in self.product_names):
                # Determine which is the unknown candidate.
                candidate = second if first_lc in self.product_names else first
                cand_lc = candidate.lower()
                # Only treat as unknown product if plural (ends with 's') to avoid personal names.
                if cand_lc not in self.product_names and cand_lc not in stopwords and cand_lc.endswith('s'):
                    unknown_candidates.append(candidate.title())


        verb_tokens = {'order', 'buy', 'purchase', 'need', 'looking'}
        # Tokenise the body into words while preserving order.
        tokens = re.findall(r'[\w\-]+', body)
        token_count = len(tokens)
        for i, token in enumerate(tokens):
            if token.lower() in verb_tokens:
                # Scan up to 4 tokens ahead for a candidate.
                for offset in range(1, 5):
                    idx = i + offset
                    if idx >= token_count:
                        break
                    candidate = tokens[idx].strip('.,;:\'"!?')
                    cand_lc = candidate.lower()
                    if cand_lc in self.product_names or cand_lc in stopwords:
                        continue
                    # Only record unknown candidates that appear plural to avoid names like 'Charlie'.
                    if not cand_lc.endswith('s'):
                        continue
                    unknown_candidates.append(candidate.title())
                    break

        # Deduplicate unknown candidates while preserving order and append to items_found.
        seen_unknown = set()
        for cand in unknown_candidates:
            if cand not in seen_unknown:
                seen_unknown.add(cand)
                items_found.append((cand, None, 0.2, 'unknown product'))

        # Consolidate items by product name; keep the highest confidence quantity found.
        consolidated: Dict[str, Dict[str, Any]] = {}
        for name, qty, conf, note in items_found:
            key_name = name
            entry = consolidated.get(key_name)
            if entry is None:
                consolidated[key_name] = {
                    'product_name': {'value': key_name, 'confidence': conf, 'notes': note},
                    'quantity': {'value': qty, 'confidence': conf if qty is not None else 0.0, 'notes': '' if qty is not None else 'quantity missing'},
                    'unit': {'value': self.price_list.get(key_name, {}).get('unit_of_measure', self.config.get('default_unit')), 'confidence': 0.8, 'notes': ''}
                }
            else:
                # If quantity exists in this match and missing in existing, update
                if entry['quantity']['value'] is None and qty is not None:
                    entry['quantity']['value'] = qty
                    entry['quantity']['confidence'] = conf
                    entry['quantity']['notes'] = ''
                # Increase confidence if another mention with quantity appears
                entry['product_name']['confidence'] = max(entry['product_name']['confidence'], conf)

        # Build items list
        for item in consolidated.values():
            event['items'].append(item)

        # Determine missing fields
        if not event['from']['value']:
            event['missing_fields'].append('from')
        if not event['subject']['value']:
            event['missing_fields'].append('subject')
        if not event['items']:
            event['missing_fields'].append('items')
        else:
            for item in event['items']:
                if item['quantity']['value'] is None:
                    event['missing_fields'].append(f"quantity for {item['product_name']['value']}")
                # If product not in price list, note missing pricing
                if item['product_name']['value'] not in self.price_list:
                    event['missing_fields'].append(f"price for {item['product_name']['value']}")

        return event


def draft_acknowledgment(event: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an acknowledgment draft referencing extracted details and asking questions."""
    to_addr = event['from']['value'] or 'customer'
    subj = event['subject']['value'] or 'your inquiry'
    # Compose greeting using the part before '@' if available
    name = to_addr.split('@')[0] if to_addr and '@' in to_addr else to_addr
    greeting = f"Hello {name.title()}," if name else "Hello,"  # Personalised greeting

    # Describe what we understood
    body_lines: List[str] = []
    body_lines.append(greeting)
    body_lines.append('')
    if event['items']:
        item_descriptions = []
        for item in event['items']:
            pname = item['product_name']['value']
            qty = item['quantity']['value']
            if qty is not None:
                item_descriptions.append(f"{qty} {pname}(s)")
            else:
                item_descriptions.append(pname)
        items_str = ', '.join(item_descriptions)
        body_lines.append(f"Thank you for reaching out to us regarding {items_str}.")
    else:
        body_lines.append("Thank you for your inquiry.")

    # Ask clarifying questions for missing or ambiguous information
    questions = []
    for field in event.get('missing_fields', [])[:2]:  # ask at most two questions
        if field.startswith('quantity for '):
            product = field.split('quantity for ')[1]
            questions.append(f"Could you please confirm the quantity required for {product}?")
        elif field.startswith('price for '):
            product = field.split('price for ')[1]
            questions.append(f"Could you provide more details about {product} so we can confirm pricing?")
        elif field == 'items':
            questions.append("Could you specify which products and quantities you are interested in?")
        elif field == 'subject':
            questions.append("Could you provide a brief subject for this inquiry?")
        elif field == 'from':
            questions.append("Could you let us know your preferred contact email?")
    if questions:
        body_lines.append('')
        body_lines.append('To help us prepare an accurate quote, could you please clarify the following:')
        for q in questions:
            body_lines.append(f"- {q}")

    # Closing with SLA text
    body_lines.append('')
    body_lines.append(config.get('sla_text', ''))
    body_lines.append('')
    body_lines.append('Kind regards,')
    body_lines.append('Sales Team')

    draft = {
        'email_id': event['email_id'],
        'to': to_addr,
        'subject': f"Re: {subj}",
        'body': '\n'.join(body_lines),
        'missing_fields': event.get('missing_fields', []),
        'questions': questions
    }
    return draft


def apply_discount(quantity: int, discount_rules: List[Dict[str, Any]]) -> float:
    """Return the discount rate for a given quantity based on tiered rules.

    The discount_rules list should be ordered descending by min_quantity so that
    the first matching rule applies.  If no rule matches, returns 0.0.
    """
    for rule in sorted(discount_rules, key=lambda r: r['min_quantity'], reverse=True):
        if quantity >= rule['min_quantity']:
            return float(rule['discount'])
    return 0.0


def generate_quote(event: Dict[str, Any], price_list: Dict[str, Dict[str, Any]], discount_rules: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a quote based on extracted event data and pricing rules."""
    quote: Dict[str, Any] = {
        'email_id': event['email_id'],
        'status': 'complete',
        'line_items': [],
        'subtotal': 0.0,
        'tax': 0.0,
        'total': 0.0,
        'currency': event['currency']['value'],
        'missing_fields': []
    }

    pending = False
    # Process each item
    for item in event['items']:
        pname = item['product_name']['value']
        qty = item['quantity']['value']
        line: Dict[str, Any] = {
            'product_name': pname,
            'unit_price': None,
            'quantity': qty,
            'discount_rate': 0.0,
            'discount_amount': 0.0,
            'subtotal': None
        }
        if pname not in price_list:
            pending = True
            quote['missing_fields'].append(f"price for {pname}")
            quote['line_items'].append(line)
            continue
        unit_price = float(price_list[pname]['unit_price'])
        line['unit_price'] = unit_price
        if qty is None:
            pending = True
            quote['missing_fields'].append(f"quantity for {pname}")
            quote['line_items'].append(line)
            continue
        # Apply discount
        discount_rate = apply_discount(qty, discount_rules)
        line['discount_rate'] = discount_rate
        discount_amount = unit_price * qty * discount_rate
        line['discount_amount'] = round(discount_amount, 2)
        line_subtotal = unit_price * qty - discount_amount
        line['subtotal'] = round(line_subtotal, 2)
        quote['line_items'].append(line)
        quote['subtotal'] += line_subtotal

    # If any pending conditions, mark and return
    if pending:
        quote['status'] = 'pending'
        # Do not compute tax and total when pending
        return quote

    # Apply tax
    tax_rate = float(config.get('tax_rate', 0.0))
    quote['tax'] = round(quote['subtotal'] * tax_rate, 2)
    quote['total'] = round(quote['subtotal'] + quote['tax'], 2)
    # Round subtotal for output
    quote['subtotal'] = round(quote['subtotal'], 2)
    return quote


def process_inbox(inbox_path: Path, data_path: Path) -> None:
    """Process all email files in the inbox directory."""
    # Load configuration and rules relative to script location
    script_dir = Path(__file__).resolve().parent
    price_list = load_json_file(script_dir / 'price_list.json')
    discount_rules = load_json_file(script_dir / 'discount_rules.json')
    config = load_json_file(script_dir / 'config.json')
    parser = EmailParser(price_list, config)

    # Ensure output directories exist
    events_dir = data_path / 'events'
    outbox_dir = data_path / 'outbox'
    quotes_dir = data_path / 'quotes'
    timeline_file = data_path / 'timeline' / 'activity.jsonl'
    events_dir.mkdir(parents=True, exist_ok=True)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    quotes_dir.mkdir(parents=True, exist_ok=True)

    # Iterate through text files in inbox
    for fname in sorted(os.listdir(inbox_path)):
        fpath = inbox_path / fname
        if not fpath.is_file() or not fname.lower().endswith('.txt'):
            continue
        try:
            with fpath.open('r', encoding='utf-8') as f:
                raw_email = f.read()
        except Exception as exc:
            # Log read failure and continue
            append_jsonl(timeline_file, {
                'timestamp': current_timestamp_iso(),
                'email_id': None,
                'step': 'read_email',
                'status': 'error',
                'message': f"Failed to read {fname}: {exc}"
            })
            continue

        # Parse email
        try:
            event = parser.parse(raw_email)
        except Exception as exc:
            # Log parse error and continue
            append_jsonl(timeline_file, {
                'timestamp': current_timestamp_iso(),
                'email_id': None,
                'step': 'parse_email',
                'status': 'error',
                'message': f"Failed to parse {fname}: {exc}"
            })
            continue

        email_id = event['email_id']
        # Check idempotency: skip processing if event file already exists
        event_path = events_dir / f"{email_id}.json"
        if event_path.exists():
            append_jsonl(timeline_file, {
                'timestamp': current_timestamp_iso(),
                'email_id': email_id,
                'step': 'skip_email',
                'status': 'skipped',
                'message': f"Event for {fname} already processed"
            })
            continue

        # Save event JSON
        save_json_file(event_path, event)
        append_jsonl(timeline_file, {
            'timestamp': current_timestamp_iso(),
            'email_id': email_id,
            'step': 'parse_email',
            'status': 'success',
            'message': f"Parsed {fname}"
        })

        # Draft acknowledgment
        ack = draft_acknowledgment(event, config)
        ack_path = outbox_dir / f"{email_id}_ack.json"
        save_json_file(ack_path, ack)
        append_jsonl(timeline_file, {
            'timestamp': current_timestamp_iso(),
            'email_id': email_id,
            'step': 'generate_ack',
            'status': 'success',
            'message': f"Drafted acknowledgment for {fname}"
        })

        # Generate quote
        quote = generate_quote(event, price_list, discount_rules, config)
        quote_path = quotes_dir / f"{email_id}.json"
        save_json_file(quote_path, quote)
        append_jsonl(timeline_file, {
            'timestamp': current_timestamp_iso(),
            'email_id': email_id,
            'step': 'generate_quote',
            'status': 'success' if quote['status'] == 'complete' else 'pending',
            'message': f"Generated {quote['status']} quote for {fname}"
        })


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='Process inquiry emails into structured data, acknowledgment drafts, and quotes.')
    parser.add_argument('--inbox', type=str, required=True, help='Path to the directory containing raw email .txt files')
    parser.add_argument('--data', type=str, default='data', help='Path to the output data directory')
    args = parser.parse_args(argv)

    inbox_path = Path(args.inbox).resolve()
    data_path = Path(args.data).resolve()
    if not inbox_path.exists() or not inbox_path.is_dir():
        print(f"Inbox directory {inbox_path} does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)
    process_inbox(inbox_path, data_path)


if __name__ == '__main__':
    main()