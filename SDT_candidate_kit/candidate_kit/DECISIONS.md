# DECISIONS.md

1. Indexes

I created unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` because those are the two business identities used by the API. They also make duplicate employee creation and duplicate punch-in attempts safe under concurrent requests. For reads, I added compound indexes that match the real query shapes: department and joining date for employee-based analytics, date and employee code for attendance listing, employee and punch time for punch-out lookup, and date, employee code, and late minutes for monthly analytics. I used compound indexes instead of separate single-field indexes because the contract consistently filters and sorts on combinations of fields.

2. Punch-in race

The punch-in endpoint does not rely on a separate "check then insert" decision. It looks up the employee, calculates the IST attendance date, builds the document, and inserts it directly. If two identical requests arrive at the same time, MongoDB allows one insert through the unique `(emp_code, date)` index and rejects the other. The rejected insert raises `DuplicateKeyError`, which the API converts into HTTP 409.

3. Ties

The late leaderboard uses MongoDB's `$rank` window operator. This gives standard competition ranking: tied employees receive the same rank, and the next rank is skipped. The `limit` is applied to the rank value, not the number of rows, so employees tied at the cutoff are all returned. This matches the OpenAPI rule and avoids the common mistake of ranking by array position.

4. Headcount

Department summary starts from the `employees` collection, not from attendance logs. It filters employees whose `joined_on` is on or before the month end, then looks up their logs for that month. Because employees are selected before logs are joined, people with zero attendance records still count in headcount. This is verifiable in the pipeline builder used by `/analytics/departments/summary`.

5. One thing I would change at 100x data

At much larger scale, I would keep raw attendance logs as the source of truth but add an incrementally refreshed reporting collection for daily attendance facts. The current aggregation pipelines are correct and index-backed, but pre-aggregating stable daily facts would reduce repeated monthly and trend calculations without changing the write contract.
