# Source attribution contract

Where an AI answer most likely got its information, with the exact passage on
each source page, and a suggested edit for passages on the brand's own site.

Computed on demand the first time a response is opened, then cached per
response (`LLMRankingResult`). All endpoints are under `/api/v1/citations/` and
use the standard envelope (`{success, data}`); the UI client unwraps `data`.

## 1. Attribution for one response

`GET  /citations/websites/<website_id>/chats/<result_id>/attribution/`
`POST /citations/websites/<website_id>/chats/<result_id>/attribution/`

`result_id` is the response's `public_id` (the same id the chat modal uses).

- `GET` returns the stored attribution, or `status: "none"` when it has never
  been computed. It never starts work.
- `POST` starts the analysis (or re-runs it with `{"refresh": true}`) and
  returns the same shape. With a Celery worker it returns `pending`; in dev
  (eager tasks) it returns the finished result. Poll `GET` every 2 s while
  `pending`. A `ready` attribution is returned as is unless `refresh` is set.

```json
{
  "result_id": "uuid",
  "status": "none | pending | ready | failed",
  "error": null,
  "updated_at": "ISO-8601 | null",
  "summary": {
    "brand": "Acme",
    "mentioned": true,
    "rank": 2,
    "brands_ahead": ["Globex"],
    "sources_total": 5,
    "sources_yours": 2,
    "confirmed": 3,
    "likely": 2,
    "by_type": [{"type": "reddit", "label": "Reddit", "count": 1}]
  },
  "findings": [Finding]
}
```

### Finding

```json
{
  "id": "f1",
  "confidence": "confirmed | likely",
  "answer_quote": "verbatim text from the response",
  "answer_start": 120,
  "answer_end": 188,
  "page_quote": "verbatim text from the source page, or null",
  "page_anchor": {"block": 12, "item": null, "start": 40, "end": 108},
  "source": {
    "url": "https://acme.com/blog/pricing-guide",
    "final_url": "https://acme.com/blog/pricing-guide",
    "title": "Pricing guide",
    "domain": "acme.com",
    "owner": "you | competitor | third_party",
    "type": "blog",
    "type_label": "Your blog"
  },
  "reason": "Short explanation of why this passage is the likely source.",
  "suggestion": {
    "before": "same as page_quote",
    "after": "proposed replacement text",
    "why": "what the change fixes in the AI answer"
  }
}
```

- `answer_start`/`answer_end` are offsets (Python code points, which equal
  JavaScript UTF-16 indices for text without astral characters) into the
  response's `response_text`, as returned by the chat detail endpoint.
  `response_text.slice(answer_start, answer_end) === answer_quote`.
  Findings never overlap in the answer; several findings may share a source.
- `confidence`:
  - `confirmed`: the engine itself cited this page for this part of the
    answer (web-search citations).
  - `likely`: matched by analysis; the passage is verified to appear word for
    word on the page.
- `page_quote`/`page_anchor` are null when the page could not be read
  (the engine's cited text, if any, is still shown as `page_quote` with
  `page_anchor: null`).
- `page_anchor` locates `page_quote` in the snapshot's `blocks`
  (section 2): `blocks[block].text` (or `blocks[block].items[item]` for list
  blocks) `.slice(start, end)` equals `page_quote`.
- `suggestion` is only present for `owner: "you"`; otherwise null.

### Source types

`source.type` is one of:

| Owner | Types |
|---|---|
| `you` | `homepage`, `blog`, `docs`, `pricing`, `product`, `about`, `terms`, `privacy`, `legal`, `support`, `page` |
| `competitor` | `competitor` |
| `third_party` | `reddit`, `review`, `forum`, `qa`, `news`, `wikipedia`, `social`, `video`, `blog`, `docs`, `gov`, `edu`, `other` |

`type_label` is the display label ("Your blog", "Your terms page", "Reddit",
"Review site", ...). The UI should show `type_label` and use `type` only for
styling.

## 2. Page snapshot

`GET /citations/websites/<website_id>/pages/snapshot/?url=<source.url>`

Only URLs that appear as a `source.url` in one of this website's
attributions are served (404 otherwise), so this is not an open proxy.

```json
{
  "url": "https://acme.com/blog/pricing-guide",
  "final_url": "https://acme.com/blog/pricing-guide",
  "title": "Pricing guide",
  "captured_at": "ISO-8601",
  "blocks": [
    {"type": "heading", "level": 2, "text": "..."},
    {"type": "paragraph", "text": "..."},
    {"type": "quote", "text": "..."},
    {"type": "list", "ordered": false, "items": ["...", "..."]},
    {"type": "code", "text": "..."},
    {"type": "table", "rows": [["...", "..."]]}
  ]
}
```

Blocks are the reader-view blocks from `apps/agent/services/browse.py`.
Page text is untrusted: render it as text nodes, never as HTML.

## 3. Opening the original page

The UI opens the real page in a new tab with a URL text fragment so Chrome,
Edge and Safari scroll to and highlight the passage:
`<final_url>#:~:text=<encoded start>,<encoded end>` (short quotes use the
whole quote). Browsers without text-fragment support open the page normally.
