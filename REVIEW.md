# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Imports / `compute_*` helpers | Duplicate imports obscured the actual dependencies; late time subtracted wall clock fields without IST/overnight handling, work hours used binary float and banker's rounding, and overtime always used the same calendar date with no 30-minute minimum. | Shift 22:00–06:00; 09:40:01 against 09:30; a duration at a half-cent rounding boundary. | Replaced with explicit IST/UTC conversion, overnight end-date handling, whole-second truncation, `Decimal` half-up, and R2/R3 thresholds. |
| 2 | `EmployeeIn` / `create_employee` | Inputs were effectively unvalidated, equal shift times were accepted, and `created_at` was naive and returned as a datetime instead of epoch milliseconds. Duplicate detection depended on a racy pre-read. | Invalid email/employee code or parallel creates for one code. | Added contract validation, UTC millisecond response conversion, and a unique index as the duplicate authority. |
| 3 | `list_employees` | `skip` used `page * page_size`, total ignored the department filter, results had no required sort, and pagination had no bounds. | Page 1 skipped the first page; department-filtered total disagreed with items. | Use `(page-1)*page_size`, filtered count, `emp_code` sort, and bounded query parameters. |
| 4 | `punch_in` | Unknown employees caused a server error; timestamps used local host time and seconds, dates ignored IST/overnight shifts, status was unrestricted, and check-then-insert allowed duplicate races. | Unknown code, concurrent same-day punches, or a punch shortly after midnight on an overnight shift. | Validate employee and status, use UTC epoch milliseconds and R1 date derivation, and rely on unique `(emp_code,date)` index. |
| 5 | `list_attendance` | It loaded and sorted the whole collection in Python, making 100k records expensive; results weren't sorted with the required tie-breaker, and total was not paginated through an indexed query. | Large collection latency/memory growth or same-date records returned in arbitrary order. | Mongo filter/count/sort/skip/limit with the date and employee index. |
| 6 | `health` | It always returned 200 without checking MongoDB readiness. | Stop MongoDB and request `/health`. | Ping the database and return 503 on failure. |
| 7 | `create_employee` and `punch_in` | Both duplicate checks relied on read-before-write and could admit duplicates under concurrent requests. | Send parallel requests with the same unique key. | Enforce unique indexes and translate duplicate-key errors to 409. |
| 8 | Missing endpoints | Punch-out, regularization, all analytics, and explain were absent. | Contract requests returned 404. | Implemented them in `app/main.py`, using atomic updates, audit history, indexed Mongo aggregations and explain output. |

Reviewed and retained: the starter's use of `emp_code` as the employee key and inclusive string date comparisons are consistent with the data model; the latter works because dates use zero-padded ISO `YYYY-MM-DD` strings.

