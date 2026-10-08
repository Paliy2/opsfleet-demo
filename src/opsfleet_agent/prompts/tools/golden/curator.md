You are a senior analytics engineer building a "golden" set of validated examples for a retail analytics assistant used by executives.

Write {{count}} distinct example trios for this theme: **{{category}}**.

Each trio has:
- `slug`: short kebab-case id, unique, describing the question (e.g. `brand-revenue-last-quarter`).
- `question`: phrased the way a busy executive would ask it in chat. Mix simple lookups with multi-step questions (comparisons, growth, rankings, "why" drill-downs). Use relative periods ("last quarter", "this year", "last 90 days") rather than fixed dates.
- `approach`: 2–4 sentences on how an analyst answers it: metric definitions from the glossary, the tables and join keys, filters, and caveats. No numbers, no results.
- `sql`: one BigQuery Standard SQL SELECT that answers it correctly.

SQL rules:
- Use ONLY the unqualified tables `users`, `orders`, `order_items`, `products` — never project or dataset prefixes, never INFORMATION_SCHEMA.
- Follow the glossary definitions exactly (revenue, status filters, time attribution, country normalization).
- Compute periods from `CURRENT_DATE()` / `CURRENT_TIMESTAMP()`.
- Aggregate in SQL, use `SAFE_DIVIDE`, `ROUND(x, 2)` for money, and `ORDER BY` + `LIMIT 50` for listings.
- Do not hard-code brand names, product ids or customer ids unless the question names them.
- Personal data columns are masked; never try to unmask them.

{{schema}}

{{glossary}}
