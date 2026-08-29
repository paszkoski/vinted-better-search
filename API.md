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
| `q`            | string | yes      | Comma-separated include keywords. Every keyword must appear in the item's title or description (case-insensitive substring match). |
| `exclude`      | string | no       | Comma-separated exclude keywords. Any match in title or description rejects the item. |
| `catalog_ids`  | string | no       | Comma-separated Vinted category IDs. |
| `price_from`   | float  | no       | Minimum price. |
| `price_to`     | float  | no       | Maximum price. |
| `order`        | string | no       | One of `price_low_to_high` (default), `price_high_to_low`, `newest_first`, `relevance`. Only affects the order Vinted's own search hands back candidates — final results are still filtered strictly by keyword. |
| `max_results`  | int    | no       | Stop once this many matches are found (default 40, max 100). |
| `max_scan`     | int    | no       | Stop after scanning this many candidate items even if `max_results` isn't reached (default 200, max 500). |

**Response**
```json
{
  "ok": true,
  "results": [
    {
      "id": 987654,
      "title": "Keychron K3 Max",
      "description": "Wireless mechanical keyboard, low profile...",
      "price": "299.0",
      "currency": "PLN",
      "url": "https://www.vinted.pl/items/987654",
      "image_url": "https://...",
      "brand": "Keychron",
      "size": "",
      "status": "Very good",
      "favourite_count": 3,
      "listed_at": "2026-08-15",
      "country": "Polska"
    }
  ],
  "scanned": 96,
  "fetched": 41,
  "truncated": false,
  "blocked": false,
  "elapsed_seconds": 38.2
}
```

- `listed_at` — Vinted doesn't expose a real listing date; this is the main photo's upload date, the closest available proxy.
- `country` — the seller's country, resolved from their public profile (fetched only for the final results shown, not every scanned candidate); `null` if it couldn't be resolved.
- `scanned` — how many of Vinted's own candidate results were examined.
- `fetched` — how many of those needed a description fetch (title alone wasn't conclusive).
- `truncated` — `max_scan` was hit before `max_results` — try narrowing keywords or raising `max_scan`.
- `blocked` — Vinted returned a 403 during this search; results may be incomplete.

## Error responses

```json
{ "ok": false, "error": "Enter at least one keyword." }
```

| HTTP status | Meaning |
|---|---|
| 400 | Missing `q` |
| 429 | A search is already running (searches are serialized) |
