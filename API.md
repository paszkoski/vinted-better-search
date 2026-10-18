# Vinted Better Search — API

## Categories

```
GET /api/categories
```

Returns Vinted's full category tree, flattened, for the category picker.

```json
{
  "ok": true,
  "categories": [
    { "id": 1904, "title": "Kobiety", "path": "Kobiety" },
    { "id": 3602, "title": "Karty graficzne", "path": "Elektronika › Komputery › Części i podzespoły komputerowe › Karty graficzne" }
  ]
}
```

Fetched from Vinted's homepage (no documented `/catalogs` API endpoint exists) and cached in-memory for 24h.

## Search

```
GET /api/search
```

| Query param    | Type   | Required | Description |
|----------------|--------|----------|-------------|
| `q`            | string | yes      | Comma-separated include keyword groups (AND). Each group may contain `OR`-separated alternatives, e.g. `keychron OR key chron, k3` means (keychron OR key chron) AND k3. Every group must be satisfied by the item's title or description (case-insensitive substring match). |
| `exclude`      | string | no       | Comma-separated exclude keywords. Any match in title or description rejects the item. |
| `catalog_ids`  | string | no       | Comma-separated Vinted category IDs. |
| `price_from`   | float  | no       | Minimum price. |
| `price_to`     | float  | no       | Maximum price. |
| `order`        | string | no       | One of `price_low_to_high` (default), `price_high_to_low`, `newest_first`, `relevance`. Only affects the order Vinted's own search hands back candidates — final results are still filtered strictly by keyword. |
| `partial`      | flag   | no       | `1` (also `true`/`on`/`yes`) additionally returns candidates matching only *some* include groups, flagged `partial` (see below). |

This is a listing-only search: it pages through *every* page Vinted reports (until an empty page or a `MAX_SEARCH_PAGES` safety ceiling), matching on title + brand only. It fetches **nothing per item** — no item pages, no seller profiles — so descriptions aren't available yet and `country` is `null` on these results. Checking descriptions is what `/api/deep-search` does, for the items you select.

**Response**
```json
{
  "ok": true,
  "results": [
    {
      "id": 987654,
      "title": "Keychron K3 Max",
      "description": "",
      "price": "299.0",
      "currency": "PLN",
      "url": "https://www.vinted.pl/items/987654",
      "image_url": "https://...",
      "brand": "Keychron",
      "size": "",
      "status": "Very good",
      "favourite_count": 3,
      "listed_at": "2026-08-15",
      "country": null,
      "partial": false
    }
  ],
  "partial_count": 0,
  "scanned": 192,
  "pages": 2,
  "hit_page_limit": false,
  "blocked": false,
  "elapsed_seconds": 3.1,
  "requests_sent": 2,
  "failed_requests": 0,
  "deep_search_max_items": 60
}
```

- `listed_at` — Vinted doesn't expose a real listing date; this is the main photo's upload date, the closest available proxy.
- `partial` — `true` for a candidate that matched only some include groups; such items also carry `missing`, the list of groups they failed (e.g. `["k3"]`). Only present when `partial=1`, and only when at least one group matched. Full matches come first, then partials.
- `partial_count` — how many of `results` are partials (they're included in `results`, not separate).
- `scanned` — how many of Vinted's own candidate results were examined.
- `pages` — how many catalog pages were fetched.
- `hit_page_limit` — `true` if the `MAX_SEARCH_PAGES` ceiling stopped the scan while Vinted still reported more pages; narrow keywords/category/price to see the rest.
- `blocked` — Vinted returned a 403 during this search; results may be incomplete.
- `requests_sent` / `failed_requests` — HTTP requests this search sent to Vinted (one per catalog page) and how many failed.
- `deep_search_max_items` — the most items one `/api/deep-search` call accepts; the UI uses it to stop over-selection.

## Deep Search

```
POST /api/deep-search
```

Takes the items you selected from a `/api/search` result and checks them properly: it fetches each item's page (for the description) and its seller's profile (for country), then re-runs the include/exclude keywords against title + description.

**Request body**
```json
{
  "q": "keychron OR key chron, k3",
  "exclude": "broken",
  "items": [
    { "id": 987654, "url": "https://www.vinted.pl/items/987654", "title": "Keychron K3", "brand": "Keychron", "seller_id": 123 }
  ]
}
```

`q`/`exclude` are the same keyword strings as the search (the item titles alone can't re-derive them); `items` carries at most `deep_search_max_items` entries.

**Response**
```json
{
  "ok": true,
  "items": [
    {
      "id": 987654,
      "verdict": "match",
      "description": "Wireless mechanical keyboard, low profile...",
      "excluded_by": null,
      "country": "Polska"
    }
  ],
  "elapsed_seconds": 4.6,
  "requests_sent": 2,
  "failed_requests": 0
}
```

- `verdict` — `match` (verified), `excluded` (an exclude term appears in the description; see `excluded_by`), `unverified` (the page couldn't be fetched — retryable, the item stays selected), or `no_match` (fetched, but still missing an include group).
- `country` — the seller's country, `null` if it couldn't be resolved.

## Error responses

```json
{ "ok": false, "error": "Enter at least one keyword." }
```

| HTTP status | Meaning |
|---|---|
| 400 | Missing/empty `q`. `/api/deep-search` also returns 400 for no `items`, more than `deep_search_max_items`, a non-integer `id`, or an `url` that isn't on `VINTED_BASE_URL`. |
| 429 | A search is already running (searches and deep searches are serialized) |
