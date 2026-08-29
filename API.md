# Vinted Better Search — API

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
| `order`        | string | no       | One of `relevance` (default), `newest_first`, `price_low_to_high`, `price_high_to_low`. Only affects the order Vinted's own search hands back candidates — final results are still filtered strictly by keyword. |
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
      "favourite_count": 3
    }
  ],
  "scanned": 96,
  "fetched": 41,
  "truncated": false,
  "blocked": false,
  "elapsed_seconds": 38.2
}
```

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
