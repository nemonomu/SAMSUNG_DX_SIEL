"""Pure helpers shared by the detail crawler and the null-only recovery job."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

FIELDS = ('star_rating', 'count_of_star_ratings', 'count_of_reviews',
          'detailed_review_content')


def missing(value):
    return value is None or isinstance(value, str) and not value.strip()


def product_pid(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or parsed.hostname not in ('www.flipkart.com', 'flipkart.com'):
        return None
    values = parse_qs(parsed.query).get('pid', [])
    return values[0] if len(values) == 1 and re.fullmatch(r'[A-Z0-9]+', values[0]) else None


def same_review_url(source, href):
    """Only unfiltered reviews of this exact product; never recommendation/aspect links."""
    pid = product_pid(source)
    parts = urlsplit(href or '')
    query = parse_qs(parts.query, keep_blank_values=True)
    return bool(pid and product_pid(href) == pid and '/product-reviews/' in parts.path
                and 'buynow' not in href.lower() and 'an' not in query)


def normalize_value(field, value):
    if missing(value) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if field == 'star_rating':
        if re.fullmatch(r'\d(?:\.\d+)?', text) and 0 <= float(text) <= 5:
            return text
        return None
    if field in ('count_of_star_ratings', 'count_of_reviews'):
        if re.fullmatch(r'\d[\d,]*', text):
            return format(int(text.replace(',', '')), ',')
        return None
    return text if field == 'detailed_review_content' else None


def aggregate_fields(scripts, pid):
    """Accept Product JSON-LD with matching SKU only, including @graph/list forms."""
    def nodes(value):
        if isinstance(value, list):
            for child in value:
                yield from nodes(child)
        elif isinstance(value, dict):
            yield value
            yield from nodes(value.get('@graph'))

    result = {}
    for raw in scripts:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for node in nodes(data):
            kind = node.get('@type')
            if not (kind == 'Product' or isinstance(kind, list) and 'Product' in kind):
                continue
            if not pid or node.get('sku') != pid:
                continue
            rating = node.get('aggregateRating')
            if not isinstance(rating, dict):
                continue
            for field, key in [('star_rating', 'ratingValue'),
                               ('count_of_star_ratings', 'ratingCount'),
                               ('count_of_reviews', 'reviewCount')]:
                value = normalize_value(field, rating.get(key))
                if value is not None:
                    result[field] = value
    return result


def fill_missing(record, values):
    changed = {}
    for field in FIELDS:
        value = normalize_value(field, values.get(field))
        if missing(record.get(field)) and value is not None:
            record[field] = value
            changed[field] = value
    return changed
