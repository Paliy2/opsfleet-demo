## Business glossary (analyst-approved definitions)

- **Revenue**: `SUM(order_items.sale_price)` excluding items with status `Cancelled` or `Returned`.
- **Gross margin**: `sale_price - products.cost`, same status exclusions as revenue.
- **Orders**: count `DISTINCT order_id`. **AOV** (average order value) = revenue / orders.
- **Active customer**: placed at least one order in the last 90 days.
- **Churned customer**: had at least one order before the last 90 days and none in the last 90 days. **Churn rate** for a period = churned / customers active at the start of the period.
- **Underspending**: customer AOV below the median AOV of their state (or of the comparison group named by the user).
- **Return rate**: items with status `Returned` / all items, for the same filter.
- **Time attribution**: revenue and items are dated by `order_items.created_at`; order counts by `orders.created_at`. The two can differ slightly — this is expected, do not investigate it.
- **Time anchor**: today is `CURRENT_DATE()`. "Last month" = the previous full calendar month. Quarters are calendar quarters (Q1 = Jan–Mar). Use `DATE_TRUNC` on `created_at`.
- **Geography**: `users.state` is a region inside `users.country` and names can repeat across countries — always group or filter by country as well. Countries are normalized English names.
- **Status values** are capitalized exactly: `Processing`, `Shipped`, `Complete`, `Cancelled`, `Returned`.
