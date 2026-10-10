# Design Decisions

## 1. Concurrency strategy for punch-in and punch-out

**Punch-in** uses a unique compound index on `(emp_code, date)` and a plain
`insert_one`. If two simultaneous requests race for the same employee on the
same calendar day, MongoDB's index guarantees exactly one succeeds; the other
gets a `DuplicateKeyError` which is mapped to **409 Conflict**. No
application-level lock or pessimistic read is needed.

**Punch-out** uses a **compare-and-set** (`find_one_and_update` with the full
prior state — `_id`, `punch_in`, `punch_out`, `status`, and `date` — embedded
in the filter). Only the request that saw the record with `punch_out = null`
can match and write; concurrent requests find `punch_out != null` and receive
**409 Conflict**. This avoids a read-modify-write race without a distributed
lock and produces no false negatives under high concurrency.

**Regularization** likewise embeds all current field values (including the
full history array) in the `find_one_and_update` filter. Concurrent
corrections operate on the same snapshot so exactly one wins; the other
receives **409 Conflict** and must retry with fresh data.

## 2. Why Decimal128 / ROUND_HALF_UP instead of MongoDB's `$round`

`$round` uses **ties-to-even** (banker's rounding). R4 and R8 explicitly
require **half-up** rounding. For `work_hours`, a session of exactly 5.625 h
would become 5.62 with ties-to-even but 5.63 with half-up — a one-cent
difference that HR software cannot tolerate. The `mongo_round_half_up` helper
converts values to `decimal` (BSON Decimal128), applies the
`$floor($add(scaled, 0.5)) / factor` identity (which is always half-up for
non-negative values), then converts back to `double` only in the final
`$project`. All intermediate arithmetic stays exact.

For Python-side `work_hours`, `decimal.ROUND_HALF_UP` with `Decimal("0.01")`
quantization provides the same guarantee without floating-point drift.

## 3. Overnight shift and IST calendar date (R1)

Timestamps are stored and compared in UTC; the IST attendance date is derived
only when labelling a record. `attendance_date_for` converts the UTC punch-in
to IST, reads the local clock minute, and — when the shift crosses midnight
(`shift_end <= shift_start` in minute-of-day terms) and the punch arrives
before `shift_end` in IST — subtracts one day. This means a 21:55 IST
punch-in for a 22:00–06:00 shift is labelled as the *previous* calendar day,
matching human expectation without any database-side date arithmetic.

## 4. Index selection strategy

Eight indexes cover all query patterns without redundant storage:

| Index | Used by |
|---|---|
| `ux_employees_emp_code` (unique) | Every employee look-up, `employee_monthly` |
| `ix_employees_department_emp_code` | `list_employees` with `?department=` |
| `ix_employees_department_joined` | `department_summary`, `department_trend` |
| `ix_employees_joined` | `department_summary` (all departments) |
| `ux_attendance_emp_date` (unique) | `regularize`, `list_attendance` with `emp_code` |
| `ix_attendance_date_emp` | `list_attendance` (no `emp_code`), `late_leaderboard` |
| `ix_attendance_status_date_emp` | `list_attendance` with `?status=` only |
| `ix_attendance_emp_punch_in` | `punch_out` (find latest open record) |

Stable index names are passed as `hint=` on every query so `explain` output
reliably shows **IXSCAN** and the `/admin/explain/{endpoint}` route returns
trustworthy plans regardless of the optimizer's statistics.

## 5. Analytics pipeline design and gap-filling

All analytics run as **single MongoDB aggregation pipelines** — no Python
post-processing loops — so the work scales with the server's parallelism and
benefits from index-prefix matching on the first `$match` stage.

**department_trend** requires a row for *every* calendar day in `[from, to]`,
even days with no attendance records. Gap-filling uses `$range` + `$map` on
the `employees` collection to generate the date series, then a `$lookup` into
`attendance_logs` per employee-day. This is semantically equivalent to
`$densify` but runs on MongoDB 5.0+ (not just 6.0+) and keeps the pipeline
in a single `aggregate` call with an IXSCAN-backed first stage.

The **7-day moving average** uses `$setWindowFields` with a document-based
`[-6, 0]` window. `$avg` in `$setWindowFields` natively skips `null` values,
so weekend rows (where `attendance_rate` is `null`) are excluded from the
numerator and denominator automatically — no conditional branching needed.
