# Content Studio (retired)

Content Studio turned visibility gaps into briefs and drafted content for
them. No screen uses it, so the feature is retired: its services, Celery
tasks and REST API are deleted.

What remains:

- `models.py` (`ContentBrief`, `ContentDraft`) and `migrations/`, so
  existing rows and the migration graph stay valid.
- `admin.py`, so the historical rows can still be inspected.

Nothing creates or reads these rows any more.
