# Hospital Engagement V1

Independent hospital business follow-up module. Hospital remains the sole source of
hospital names, addresses and public contact details. No order, invoice, shipment,
pricing, settlement or payment logic participates in engagement stage decisions.

## Models

| Model | Fields / rules |
| --- | --- |
| HospitalEngagement | One-to-one hospital; stage (stage_1/2/3), priority (A/B/C), nullable owner, demand_summary, special_requirements, next_action, next_follow_up_date, created_at, updated_at. |
| HospitalDepartment | hospital, name, notes, is_active, created_at, updated_at. Case-insensitive active name unique within each hospital. |
| HospitalContact | hospital, optional department, name, contact_type (doctor/purchasing/admin/nurse/other), title, email, phone, is_primary, notes, is_active, created_at, updated_at. |
| HospitalProductInterest | engagement, optional department, optional product (SET_NULL), product_text, notes, created_at, updated_at. Product or nonblank free text required by model/form validation; both allowed. No database check that would prevent SET_NULL when a referenced Product is deleted. |
| HospitalFollowUp | engagement, activity_type (communication/stage_change/note), optional contact/department, occurred_at, channel (phone/email/whatsapp/visit/meeting/other), summary, outcome, next_action, next_follow_up_date, stage_from/to, nullable created_by, created_at. |

Department/contact references are checked against the engagement's hospital.
Departments and contacts are deactivated, not deleted by Portal actions.
Disabling a hospital preserves its entire engagement history and excludes it from
default stage counts/lists. Re-enabling it makes that history visible again.

## Authorization

The module permission is `hospital_engagements.access_hospital_engagement`,
displayed as **Can access Hospital Engagement**.

All module views use the centralized `engagement_access` decorator, composing
Django's `staff_member_required` with `permission_required(..., raise_exception=True)`.
An authenticated active staff user without this permission receives 403 on direct
module GET and POST requests. Anonymous, inactive and nonstaff users follow the
existing Admin login behavior. User and Group permissions work natively;
superusers retain Django's normal all-permissions semantics.

The Portal homepage inserts exactly one card between Library and Hospital Orders
only when `request.user.has_perm(...)` is true. Its badge counts active engagements.
Owner is business metadata, never an access rule.

### Grant access in Admin

1. Sign in to Django Admin with permission to manage users.
2. Users → acoeurs → User permissions.
3. Select **Can access Hospital Engagement** and save.
4. Ensure the user is active and staff.

Alternatively grant the same permission to a Django Group and add staff users to
that group. No usernames are hardcoded, and migrations never grant access to users
or groups. This implementation did not grant access to any local account.

## Routes and UI

- Module home: `/portal/hospital-engagements/`
- Stage 1: `/portal/hospital-engagements/stage/stage-1/`
- Stage 2: `/portal/hospital-engagements/stage/stage-2/`
- Stage 3: `/portal/hospital-engagements/stage/stage-3/`
- Hospital detail: `/portal/hospital-engagements/<hospital_id>/`

Stage slugs are whitelisted; unknown values return 404. The home has only three
stage cards with live active-hospital counts and soft blue-gray, amber and green
backgrounds. Lists provide hospital/contact/product/free-text search, owner and
priority filters, 40 engagements per page, preserved GET filters, stacked contact
cells, abbreviated department/doctor/product summaries and single/bulk stage moves.
Address summaries prefer default_shipping_address, then billing_address.

Detail pages show read-only hospital master data with the existing library link,
editable business information, departments, contacts, product needs, communication
entry and a 30-entry paginated timeline. Edit forms may be opened with GET; all
writes require POST and CSRF. Cross-hospital edits/references are rejected.
The module's stylesheet is scoped and reuses the existing Portal buttons.
Small screens stack cards/forms; the wide business table scrolls within its wrapper.

## Write services and performance

- `change_hospital_engagement_stage` locks the engagement in an atomic transaction,
  validates the target, skips unchanged stages and records machine-valued
  stage_from/to with actor and timestamp. Stage labels are translated on display.
- Bulk moves invoke that same service for each selected engagement inside one
  transaction, locking in primary-key order. There is no stage bulk_update.
- `save_contact` locks the parent hospital even when it has no contacts yet,
  unsets other active primaries and saves the requested contact atomically.
  A conditional database uniqueness constraint also enforces one active primary.
- `add_communication` saves the actor and communication plus supplied nonempty
  next-action/date updates to the engagement in one transaction.
- Timeline ordering is occurred_at DESC, pk DESC. Latest-business-communication
  subqueries explicitly exclude stage_change and note records.
- Stage queries select_related hospital/owner and prefetch active departments,
  active contacts and product interests. Summary decoration performs no per-row
  query. A regression assertion verifies four queries for related row loading,
  independent of row count (pagination/auth/filter queries are additional).

## Initialization and migrations

- `0001_initial`: only the five new tables, indexes, constraints and custom permission.
- `0002_initialize_engagements`: idempotent get_or_create for all existing hospitals,
  always defaulting to stage_1, priority B, owner NULL; historic orders are ignored.
  Re-execution preserves existing engagement choices. Reverse is intentionally a
  no-op so reversing only the data migration does not remove business records.
- New Hospital saves create an engagement via a new-app post_save receiver using
  get_or_create. It skips raw fixture saves. Bulk inserts do not emit Django signals;
  future bulk-import workflows must explicitly initialize their engagements.
- GET requests never create missing engagements.

Local initialization on 2026-10-06 created **112 engagements for 112 hospitals**,
all active and default stage_1/B/unassigned. Before/after snapshots confirmed that
all Hospital fields and user/group permission assignments/memberships were unchanged.
Only the two migrations above were applied. No deployment, commit or push occurred.

## Localization and verification

Chinese source strings and English/French gettext translations cover the new UI.
80 precise entries were added to each catalog and compiled. Existing translations
were preserved. Business names, addresses, product codes and entered text are never
translated. The existing Django language selector/set_language behavior is unchanged.

Verification commands (from the project root, using the existing virtualenv):

```sh
python manage.py check
python manage.py test hospital_engagements portal.tests --settings=config.settings_test --noinput
python manage.py test hospital_engagements --keepdb --noinput
python manage.py compilemessages -l en -l fr --ignore=.venv
python manage.py makemigrations --check --dry-run
git diff --check
```

The module includes permissions, group/superuser behavior, homepage regression,
existing/future/inactive hospital initialization, historic-order independence,
stage history/rollback/bulk changes, contacts/departments, product needs, follow-up
atomicity and ordering, search/filter/pagination, query-count, three-language and
representative Hospital library page coverage. Two additional PostgreSQL-only
concurrency tests verify competing first-primary contacts and identical stage
changes. SQLite skips only those row-lock tests.

Final results: SQLite module + full Portal suite: 238 tests, 236 passed and the
2 PostgreSQL-only tests skipped. PostgreSQL module suite: 80/80 passed, including
both concurrency tests. Django check, compilemessages, migration drift check and
git diff --check passed. Translation audit found zero missing module UI strings
in either English or French. Browser screenshot/visual QA was not performed:
the available browser runtime could not initialize, and no browser dependency was installed.
