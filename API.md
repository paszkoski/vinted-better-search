# Vinted Monitor — Control API

Base URL: `http://<host>:<port>/api/v1`

## Authentication

Set `API_KEY` in your Docker environment. Pass it on every request via:

- Header: `X-API-Key: <key>`
- Query param: `?api_key=<key>`

If `API_KEY` is not set, all requests are allowed (a warning is printed at startup).

---

## Queries

### List all queries
```
GET /api/v1/queries
```
Returns all queries with their current status and total findings count.

**Response**
```json
{
  "ok": true,
  "queries": [
    {
      "id": 1,
      "name": "Keychron K3",
      "search_text": "keychron k3",
      "catalog_id": 2994,
      "price_from": null,
      "price_to": 500,
      "brand_ids": "53",
      "size_ids": null,
      "color_ids": null,
      "material_ids": null,
      "status_ids": null,
      "interval_seconds": 300,
      "enabled": 1,
      "status": "ok",
      "last_checked": "2026-03-05T12:00:00",
      "last_seen_id": 123456,
      "findings_count": 7,
      "created_at": "2026-03-05T10:00:00"
    }
  ]
}
```

---

### Add a query

```
POST /api/v1/queries
```

Accepts parameters via JSON body, form data, or query string.

#### Option A — from a Vinted URL (recommended)

Paste any Vinted catalog URL and all filters are extracted automatically.

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | Full Vinted catalog URL |
| `name` | string | no | Human-readable label |
| `interval_seconds` | int | no | Poll interval (default: 300) |

Any extracted field (`catalog_id`, `price_from`, etc.) can be overridden by including it explicitly alongside `url`.

**Example — query string (n8n style)**
```
POST /api/v1/queries?url=https://www.vinted.pl/catalog?catalog[]=5&brand_ids[]=53&name=Test&interval_seconds=180
```

**Example — JSON body**
```json
{
  "url": "https://www.vinted.pl/catalog?search_text=keychron+k3&catalog_ids[]=2994&price_to=500",
  "name": "Keychron K3 Max",
  "interval_seconds": 180
}
```

#### Option B — manual parameters

| Field | Type | Required | Description |
|---|---|---|---|
| `search_text` | string | yes* | Search query text |
| `name` | string | no | Human-readable label |
| `interval_seconds` | int | no | Poll interval (default: 300) |
| `catalog_id` | int | no | Category ID |
| `price_from` | float | no | Minimum price |
| `price_to` | float | no | Maximum price |
| `brand_ids` | int list | no | Brand filter IDs |
| `size_ids` | int list | no | Size filter IDs |
| `color_ids` | int list | no | Color filter IDs |
| `material_ids` | int list | no | Material filter IDs |
| `status_ids` | int list | no | Condition filter IDs |

\* At least `search_text` or one filter is required.

**Response**
```json
{ "ok": true, "id": 3 }
```

---

### Delete a query
```
DELETE /api/v1/queries/<id>
```
Removes the query and all its findings.

**Response**
```json
{ "ok": true }
```

---

### Toggle enable / disable
```
POST /api/v1/queries/<id>/toggle
```
Flips the enabled state. Re-enabling schedules an immediate check.

**Response**
```json
{ "ok": true, "enabled": false }
```

---

### Trigger immediate check
```
POST /api/v1/queries/<id>/check-now
```
Queues the query for polling on the next scheduler cycle (within ~5 seconds).

**Response**
```json
{ "ok": true }
```

---

## Findings

### List recent findings
```
GET /api/v1/findings
```

| Query param | Default | Description |
|---|---|---|
| `query_id` | — | Filter by query ID |
| `limit` | 60 | Max results (cap: 200) |

**Response**
```json
{
  "ok": true,
  "findings": [
    {
      "id": 1,
      "query_id": 1,
      "query_label": "Keychron K3",
      "item_id": 987654,
      "title": "Keychron K3 Max - jak nowa",
      "price": "299 PLN",
      "url": "https://www.vinted.pl/items/987654",
      "image_url": "https://...",
      "found_at": "2026-03-05T12:05:00"
    }
  ]
}
```

---

## Error responses

All errors follow the same shape:
```json
{ "ok": false, "error": "Description of the problem" }
```

| HTTP status | Meaning |
|---|---|
| 400 | Missing required parameters |
| 401 | Invalid or missing API key |
| 404 | Query not found |
