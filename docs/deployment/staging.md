# ECS staging application contract

This runbook defines the application boundary for the next deployment phase. It
does not create or configure Alibaba Cloud resources, DNS, or TLS.

## Required runtime contract

- Use `config.settings_production` and provide every variable documented in
  `.env.example`. Secrets must come from the deployment secret mechanism, not
  the image or source tree.
- Point `DATABASE_*` at an isolated staging PostgreSQL database. PostgreSQL 5432
  must not be exposed publicly.
- Mount durable storage at the host path named by `MEDIA_ROOT_HOST`; Compose
  mounts it as `/app/media` in both web and OCR. Set Django `MEDIA_ROOT=/app/media`.
- Run web and OCR from the same immutable image. OCR is reachable only on the
  internal network at `http://ocr:8765/ocr`; it has no host port.
- Terminate HTTPS at the reverse proxy and pass `X-Forwarded-Proto: https`.
  Proxy only to the loopback-bound web port. Start HSTS at zero and raise it only
  after HTTPS and rollback behavior have been verified.

## Release and startup

Build and scan the image, then run exactly one release job before replacing web
instances:

```sh
docker compose -f docker-compose.production.yml --profile tools run --rm release
```

The release script waits for the configured database, runs Django deployment
checks, and applies migrations. Static assets are collected deterministically at
image build time. Normal web startup does not migrate or collect static files.

After a successful release, start or replace services:

```sh
docker compose -f docker-compose.production.yml up -d ocr web
```

OCR eagerly initializes PaddleOCR before opening `/health`; its model cache uses
the `ocr_models` volume. Web health is `/healthz/`. Wait for both health checks
before sending staging traffic.

## Verification

1. Run `python manage.py check_media_integrity --fail-on-error` in a one-off web
   container against the staging database and media mount.
2. Confirm anonymous and authenticated non-staff users cannot access Finance or
   `/portal/files/...`; confirm staff can stream a known non-business test file.
3. Confirm `/media/...` returns 404 and OCR/PostgreSQL have no public listeners.
4. Exercise one synthetic PDF through OCR; do not use a historical order.
5. Review application and OCR logs without exposing them to browser clients.

## Rollback concept

Retain the previous immutable image and the matching pre-release database/media
backup. For an application-only failure with a backward-compatible schema, drain
traffic and restore the previous image. Do not automatically reverse migrations.
For an incompatible schema or data failure, stop writes, restore the matching
database and media snapshots using `backup_restore.md`, verify integrity, and
then start the previous image. Record and investigate every failed release.
