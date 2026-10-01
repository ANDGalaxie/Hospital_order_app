# Backup and restore contract

The orders platform is only recoverable when PostgreSQL and persistent media are
backed up as one consistent application snapshot. PostgreSQL contains business
records and file references; the media mount contains hospital orders, factory
confirmations, generated invoices and purchase orders, source data, OCR JSON,
and page images. A database-only backup leaves those references unusable.

## Backup baseline

- Back up PostgreSQL with a tested, encrypted database backup mechanism.
- Snapshot or back up the durable host path mounted at `/app/media`.
- Record the database backup and matching media snapshot identifiers together.
- Restrict backup access and retention using the same sensitivity as production.
- Periodically test restore into an isolated environment; never use live business
  data for an ad-hoc developer test.

Before a deployment that changes schema or stored documents, take matching
database and media backups after stopping or draining writes. Run
`python manage.py check_media_integrity --fail-on-error` before and after the
snapshot when practical.

## Restore order

1. Stop application writes and keep web/release jobs offline.
2. Restore PostgreSQL to the selected recovery point.
3. Restore the matching media snapshot to the configured durable media path.
4. Start a one-off application container and run
   `python manage.py check_media_integrity --fail-on-error`.
5. Start OCR and web only after integrity verification succeeds.
6. Smoke-test health, staff login, a protected known test document, Finance, and
   a non-business OCR fixture.

Do not regenerate missing historical documents as part of restore. Investigate
snapshot consistency and restore the correct matching media version instead.
