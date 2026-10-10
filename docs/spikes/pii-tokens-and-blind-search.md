# Spike: numbered PII tokens and blind search

Status: design spike, kept as the detailed reference. Phases 0–2 are built; [DECISIONS §11](../DECISIONS.md) describes what was built.
- Phase 0, the parameter guard and random salt name.
- Phase 1, numbered placeholders.
- Phase 2, report search and deletion by a typed name.

Two choices differ from this spike:
- The raw value is kept in a shared, AES-GCM-sealed Redis store with a TTL, not in process memory, because the API runs several replicas and a deletion resumes through a separate request.
- The lookup is exposed only through `search_reports` / `delete_reports`, not as a separate tool.

The analyst-side blind search in §4 and §10 (phase 3) is still future work.

**Invariant (confirmed by the hiring manager): PII never reaches the LLM.** This covers the guard, the agents, the summarizer, history, traces and checkpoints, not only the final answer. Every option below is judged against it. An option that needs the model to see a raw value is rejected.

## 1. Problems

1. **Distinct people collapse into one.** "Compare orders of John Doe (john.doe@example.com) and Jane Roe (jane.roe@example.org)" becomes `[PERSON_NAME] ([EMAIL_ADDRESS]) and [PERSON_NAME] ([EMAIL_ADDRESS])`. The model cannot tell the two people apart.
2. **Search by a value the model never sees.** The user types a real name or email. The agent should find that customer without the model ever holding the value.
3. **Typo-tolerant search.** When an exact match finds nothing, the agent should retry with close spellings, a name part or a prefix, still without seeing the value.
4. **"Delete all reports mentioning Client X"** when X is a customer. Reports are written from masked data, so they contain pseudonyms, not the name the user types.

## 2. What exists today (relevant facts from the code)

- `ChatTurn` redacts first (`LayeredRedactor`: local rules, then DLP). Each finding becomes `[TYPE]`, and the values are discarded (`entities/privacy.py` `Redaction`).
- Customer pseudonyms are derived from **`u.id`, not from the name**: `pseudonym_sql` = first 16 hex characters of `SHA256(@pii_salt || id)`. The masked email is `user_<first 8 of that hash>@<real domain>`. Surrogate names are drawn from 20 first × 20 last names.
- **Surrogate names collide.** There are 400 combinations for 100,000 users, so each surrogate name is shared by about 250 customers. Only the pseudonymous id and the masked email identify one customer.
- Report search and deletion reject `[TYPE]` placeholders with `E_MASKED_SEARCH`. The regex `\[[A-Z_]+\]` **does not match numbered tokens** such as `[PERSON_NAME_1]`, so numbering must widen it (`NUMBERED_TOKEN` in the prototype).
- `_TracedProgress.emit` copies every progress event into the Langfuse trace. Anything that carries a raw value back to the UI must not be a `ProgressEvent`.

## 3. Options compared

### 3.1 Where the token → value mapping lives

| Option | Stable across turns | Raw value at rest | Supports fuzzy | Cross-conversation linkage | Cost / latency | Verdict |
|---|---|---|---|---|---|---|
| A. Per-turn memory only | No | None | Same turn only | None | None | Not enough on its own: numbering would restart every turn |
| B. **Postgres, fingerprints only** (HMAC of the normalized value with a per-session key; no value) | Yes | None | No (a fingerprint can't be fuzzed) | None (per-session key) | One small table | **Recommended for numbering** |
| C. Postgres, value encrypted (AES-GCM, Cloud KMS envelope) | Yes | Ciphertext | Yes, in any later turn | None | KMS decrypt per search | Only if cross-turn fuzzy retry is needed (phase 3) |
| D. DLP `CryptoDeterministicConfig` (AES-SIV, KMS-wrapped key, surrogate info type) | Yes, without storage | Inside the token | Needs a `reidentify` call first | **Same value gives the same token in every conversation and for every user** | DLP round trip on every redaction and every resolve | Rejected: long ciphertext tokens in the prompt, global linkage, numbering still needs storage |
| E. DLP `CryptoReplaceFfxFpeConfig` (format-preserving) | Yes | Inside the token | No | Same as D | Same as D | Rejected: output looks like a real email or number, which would trip our output PII scan and mislead the model |

The DLP rows come from the de-identify/re-identify documentation, not from a live call.

**Chosen: B plus A.** The vault stores only `(session_id, owner_hash, info_type, ordinal, fingerprint)`. The raw value lives in process memory for the turn in which the user typed it, as a `SecretStr`, so `repr`, JSON and logs show `**********` (tested). Once a value has been resolved to a customer, the book keeps the **pseudonymous id**, which is already visible in masked data, so later turns need neither the value nor a reversible vault. Option C is added only if users need typo retries in a later turn without retyping.

Retention and deletion: rows are keyed by session with `ON DELETE CASCADE` from `chat_sessions`, so `DeleteConversation` removes them. The book never enters LangGraph state, checkpoints, Langfuse, logs or the result store. The model and every agent see only labels and pseudonyms.

### 3.2 How the agent asks for a search

| Option | How the model refers to the value | Exfiltration surface | Verdict |
|---|---|---|---|
| 1. Free SQL with the token as a parameter (`WHERE name = @PERSON_NAME_1`) | Writes the parameter in SQL | `SELECT @p`, `LENGTH(@p)`, `SUBSTR`, `CASE WHEN @p > 'm'` comparisons used as an oracle | Rejected: cannot be made safe without a full SQL analyser |
| 2. Raw columns exposed to the model's SQL | Writes predicates on raw columns | The model can project the raw column | Rejected: breaks the invariant |
| 3. **Restricted predicate tool → match set of pseudonymous ids** | `{operator, token, length?}` in JSON; no literals, no expressions | Count and pseudonymous ids of matches only | **Recommended** |

## 4. Recommended design: name → pseudonym in the trusted layer

```mermaid
sequenceDiagram
    actor U as User
    participant T as ChatTurn
    participant R as Redactor (local + DLP)
    participant V as PiiTokenVault (Postgres)
    participant L as LLM (guard, orchestrator, analyst)
    participant B as Blind search (interactor + BQ gateway)
    participant Q as BigQuery
    U->>T: "How much did John Doe (john.doe@example.com) spend?"
    T->>R: redact(text) → spans
    T->>V: number spans (fingerprint lookup)
    V-->>T: [PERSON_NAME_1], [EMAIL_ADDRESS_1]; values kept in turn memory
    T->>L: "How much did [PERSON_NAME_1] ([EMAIL_ADDRESS_1]) spend?"
    L->>B: find_customer {operator: exact, token: EMAIL_ADDRESS_1}
    B->>B: policy (role, budget), resolve token, derive bound values
    B->>Q: WITH scoped/masked CTEs, blind_1 AS (SELECT pseudonym(raw.id) … WHERE LOWER(raw.email) = @bm_<nonce>_1_0)
    Q-->>B: id 1a2b3c4d…, Morgan Costa, user_1a2b3c4d@example.com, match_kind=exact
    B->>V: remember PERSON_NAME_1 → pseudonymous id
    B-->>L: 1 customer: Morgan Costa (user_1a2b3c4d@example.com), exact email match
    L->>Q: run_sql … WHERE user_id = '1a2b3c4d…' (ordinary masked SQL)
    L-->>T: "[PERSON_NAME_1] (shown as Morgan Costa) spent $412 …"
    T-->>U: answer (UI may show the user's own value for the token; see §7)
```

What makes it safe:

1. **The model passes a token and an operator, never a value.** `BlindPredicate` is `{operator, token, length}` with `extra="forbid"`. The token must match `^[A-Z][A-Z_]*_[1-9]\d*$`, and `length` must be 3–6. A literal (`"john.doe@example.com"`), an extra `value` field, an unknown operator or a bracketed token all fail validation (tested).
2. **Values are derived by the server** from the hidden value: the whole value, the email local part, its first *n* characters, the first or last word, or a typo budget from the length (0 for fewer than 5 characters, 1 up to 10, 2 above, the same rule as the brand typos in DECISIONS §10). **No operator combines the hidden value with a string chosen by the model**, so no query can compare the value against a guess. Binary search on characters is impossible.
3. **Values are bound as BigQuery query parameters** named `bm_<per-query random nonce>_<set>_<index>`. They are never interpolated. A hostile value such as `x' OR 1=1 --` stays inside the parameter (tested). `STARTS_WITH` replaces `LIKE`, so `%` and `_` in a value are not wildcards.
4. **The match CTE projects only `pseudonym(raw.id)` and a constant `match_kind`.** It reads the raw `users` table inside the trusted gateway and is intersected with the scoped `users` CTE (`… IN (SELECT id FROM users)`), so a manager only finds customers of their own brands.
5. **The model's SQL cannot address parameters at all.** `ensure_no_query_parameters` rejects any `@` outside a quoted masked-email literal (`'user_xxxxxxxx@domain'`). Server parameter names always contain `_`, which the allowed literal pattern excludes, so the exception cannot hide a parameter. Random parameter names are a second barrier: a guessed name is an undeclared parameter, and the query fails.
6. **Lookup output is the masked identity only:** `users.id`, surrogate first and last name, masked email, `match_kind`, at most 25 rows (`pseudonym_lookup_sql`).

**Operator vocabulary (prototype).**

| Operator | EMAIL_ADDRESS | PERSON_NAME | Compiled predicate on the raw column |
|---|---|---|---|
| `exact` | ✓ | ✓ | `LOWER(TRIM(email)) = @p` · `LOWER(CONCAT(first, ' ', last)) = @p` |
| `prefix(n)` | ✓ (local part) | | `STARTS_WITH(LOWER(local part), @p)` |
| `first_name` / `last_name` | | ✓ | `LOWER(first_name) = @p` |
| `fuzzy` | ✓ (local part) | ✓ | `EDIT_DISTANCE(col, @p, max_distance => 3) <= k` |
| `sounds_like` | | ✓ | `SOUNDEX(first) = SOUNDEX(@p0) AND SOUNDEX(last) = SOUNDEX(@p1)` |

In production the model should not pick the ladder itself. `find_customer(token)` runs **exact → case-insensitive → name part → sounds-like → fuzzy** on the server and stops at the first tier with results. The result reports the tier, so the agent can say "found 1 customer matching PERSON_NAME_1 by close spelling". Explicit operators stay available for "try the first name only".

**Coexistence with the placeholder-literal check.** `run_sql` already rejects placeholder literals so the analyst fails fast. Keep it, and widen it to numbered tokens. A token used as a SQL literal (`WHERE first_name = 'PERSON_NAME_1'`) is always a mistake. The error should point the model to `find_customer` and then to the pseudonymous id, instead of only asking the user to retype. Report search and deletion change in the same way: `E_MASKED_SEARCH` stays for unnumbered `[TYPE]` placeholders, and numbered tokens are resolved to pseudonyms (§6).

## 5. Security analysis

### 5.1 Attacks tested

Unit tests are in `tests/unit/test_blind_search.py` and `tests/unit/test_pii_tokens.py`. Live BigQuery dry runs of the compiled operators billed no bytes.

| # | Attack | Result |
|---|---|---|
| 1 | `SELECT @bm_<nonce>_1_0` | **BigQuery accepts it** (dry run OK). Blocked only by `ensure_no_query_parameters` (unit) |
| 2 | `LENGTH(@p)`, `SUBSTR(@p,1,1)`, `CASE WHEN STARTS_WITH(@p,'j')`, `WHERE @p > 'm'` (oracle) | Blocked by the parameter guard (unit) |
| 3 | Parameter hidden in a comment `/* */ , @p`, after a raw string `r'\', @p`, or glued to a fake masked email `'x'user_00000000@pii_salt'` | Blocked (unit) |
| 4 | `@@project_id` system variable; positional `?` | Blocked (unit) |
| 5 | `SELECT raw.email FROM blind_1` (read the raw alias through the match set) | BigQuery rejects it: `Unrecognized name: raw` (dry run) |
| 6 | `SELECT email FROM users JOIN blind_1` | Allowed, but returns the masked email only (dry run OK, same 4 tables) |
| 7 | Literal value instead of a token, extra `value` field, `length` 1 or 40, unknown operator | Rejected by pydantic validation (unit) |
| 8 | Token not in this conversation (`PERSON_NAME_7`), or a token from another user's conversation | `E_QUERY_REJECTED` "unknown token" (unit) |
| 9 | Operator not valid for the type (`prefix` on a name, `sounds_like` on an email), `length` on a non-prefix operator | Rejected (unit) |
| 10 | Errors as a side channel | No error detail or user message contains the value (asserted on every failure path). `prefix(n)` longer than the value does not fail, so it reveals no length |
| 11 | Hostile value `x' OR 1=1 --` | Bound as a parameter; the SQL text is unchanged (unit) |
| 12 | Manager role; budget exhausted | `E_BLIND_SEARCH_DENIED` (unit) |
| 13 | Match set or nonce name injection (`users`, `blind_x`) | `ValueError` (unit) |

All 8 operators compile to valid BigQuery SQL: `EDIT_DISTANCE … max_distance`, `SOUNDEX` and `STARTS_WITH` were all checked by dry run. Each query scans 6.9–8.8 MB and references only the 4 allowed tables, so the existing dry-run policy (`RunQuery._enforce`) still passes.

### 5.2 Existing gap found (fix now, independent of this feature)

`ensure_safe_sql_text` accepts `SELECT @pii_salt AS salt`, and BigQuery runs it, because `@pii_salt` is declared on every job. The masking salt would then reach the model, the result store and Langfuse, and anyone holding the salt can link pseudonyms back to `thelook` user ids. The fix is the same `@` rule as `ensure_no_query_parameters` in `RunQuery`, plus random parameter names in `BigQueryEngine`. It is about 1 hour of work, and it should land before or together with `fix/masked-sql-values`.

### 5.3 What the model can and cannot learn

| The model can learn | The model cannot learn |
|---|---|
| That a token matches *k* customers in scope, and the tier (exact, fuzzy, …) | Any character, the length or a hash of the hidden value |
| The pseudonymous id, surrogate name and masked email of the matches | Real names or emails of anyone, matched or not |
| The real email **domain** of a match (masked emails keep the domain, as today) | Values in other conversations or of other users |

### 5.4 The membership oracle: the real residual risk

The person who learns something is the **user**, not the model. Typing a real name and getting "1 customer, spent $412" tells the user that this person is a customer and links them to their purchase history. That is re-identification by an authorized human. It is the feature's purpose, and it is also its risk. k-anonymity cannot apply: a lookup of one person yields a result set of one by design. Mitigations:

- **Role gate.** Off by default. `BlindSearchPolicy.allowed_roles` is CEO only in the prototype. Managers would see only customers of their own brands.
- **Budget.** At most 10 lookups per conversation (prototype), plus a daily per-user cap in Redis next to the existing rate limit, so the user cannot enumerate a contact list.
- **Audit log.** Metadata only: user hash, session, token label, operator, tier, match count. Never the value.
- **Bounded fuzzy output.** At most 25 candidates. If more match, the tool answers "too broad, add the email" instead of a list.
- **Deletion uses exact tiers only** (§6).
- **Guard update.** "Reveal the real name behind Morgan Costa" stays injection (unmask direction). "Find [PERSON_NAME_1]" (lookup direction, a value the user supplied) is allowed for entitled roles. Both need red-team cases.

### 5.5 Robustness: how each piece avoids keyword false positives

- **Data side.** Masking is an explicit, reviewed classification of every column of the 4 tables in the scoped CTEs: direct identifiers get surrogates, quasi-identifiers are generalized, analytics columns pass through. It does not depend on column names. In production, BigQuery policy tags plus dynamic data masking enforce it in the engine (DECISIONS §9).
- **Input side.** DLP machine-learning detectors with per-type likelihood thresholds (`LIKELY` for names and addresses, `POSSIBLE` for structured types). A name finding is dropped only when it lies entirely inside a known brand or pseudonym, or within a small edit distance of one ("Calvin Klein" kept, "Calvin Smith" redacted). Brands and categories are never mistaken for people.
- **New matching.** Lookups are typed: an email token only matches the email column, a name token only name columns. They use exact tiers first and fuzzy only as a fallback, with the tier reported. Reports are matched on **pseudonymous ids or masked emails, never on surrogate names**, because a surrogate name such as "Morgan Costa" is shared by about 250 customers, and a substring match on it would delete reports about other people.

## 6. PII vs. name-based deletion (for the README/HLD)

> **"Delete all reports mentioning Client X."** In this dataset X can be a brand, a category or a customer. Brands and categories are not personal data: they reach the model unredacted (brand allow-list) and reports are matched by text with typo fallback (DECISIONS §21). Customers are different: the model never sees the name the user types, and reports never contain it, because they are written from masked data.
>
> **Trade-off.** Storing real names alongside reports would make name-based deletion trivial, but it puts PII at rest in a second store and in front of the model. We store and match **pseudonymous identifiers** instead. Every customer mention in a report carries the masked email (`user_1a2b3c4d@…`), and a `report_entities` row records `(report_id, kind, key)` with the brand, category or pseudonymous id. At deletion time the trusted layer translates the typed name to a pseudonym: raw value → bound-parameter lookup on the raw users table → `u.id` → the same salted pseudonym the masking produces. Reports are then matched on that pseudonym. The model sees only the token and the pseudonym, both already safe.
>
> **Strict confirmation is unchanged.** Translation uses exact and case-insensitive tiers only. A fuzzy candidate never widens a deletion; the agent asks the user to pick a customer first. The plan stores exact report ids, and the confirmation lists every report title together with the customer it matched ("Morgan Costa, user_1a2b3c4d@…, exact email match"). Approval comes only from the Delete button. The cost: one lookup per deletion, and reports written before the change can be matched only through the masked email in their text.

## 7. UX

- **Explaining matches.** The tool result carries the tier, so the agent says "found 1 customer matching [PERSON_NAME_1] by exact email", "… by close spelling", or "no customer matches; 3 share the first name". With no match it says so, and it never offers names.
- **Showing the user their own values.** Recommended: owner-only and live-session-only. With the turn's response, the API sends the owner a legend `{PERSON_NAME_1: <the value the user just typed>}` on the same authenticated SSE stream, as its own event type that does not go through `_TracedProgress`. The Chainlit process keeps it in the socket's user session and substitutes it for display only. It is never written to the API or Langfuse, and Chainlit persists nothing (DECISIONS §24). After a reload, tokens show as tokens.
- **Is it safe across the UI/API boundary?** The UI already receives that exact text from the user's browser, so the legend adds no new exposure for the UI. The new element is that the API sends a raw value outbound once. The risk is acceptable for the owner on their own stream. It is not acceptable for any shared view, report, PDF or trace. Reports keep tokens or pseudonyms, never rehydrated values.

## 8. Numbering details and open questions

- **Stability.** Same normalized value → same number for the whole conversation (case and whitespace for names and emails, digits only for phones; tested). History windowing and summaries only ever contain labels, and the book outlives the window, so `[PERSON_NAME_1]` in a summary still resolves. The summarizer and writer prompts need one rule: keep `[TYPE_N]` tokens verbatim.
- **Multiple users.** Books are keyed by `(session_id, owner_hash)`, and the fingerprint key is derived per session. The same person gets unrelated fingerprints in two conversations, so the vault itself enables no cross-conversation linkage (tested).
- **Concurrency.** Two parallel turns in one session can race for the same ordinal. Postgres `UNIQUE (session_id, info_type, ordinal)` and `UNIQUE (session_id, fingerprint)` with a retry solve it. The fake does not model this.
- **Redactor change.** Redactors must return spans over the original text instead of rewritten text. `LayeredRedactor` then merges local and DLP spans, preferring the longer one. As a result DLP also sees emails that the local layer used to remove first. DLP already receives names, so the exposure class does not change.
- **Open.**
  - "John" and "John Doe" get different tokens. Should a name part link to a full name typed earlier?
  - Should Python `casefold` and BigQuery `LOWER` normalization be aligned for non-ASCII names? This needs a `-m bq` parity test with real execution.
  - Would the client accept the membership oracle for the CEO role at all? This is a product and legal decision.
  - Daily cap values.

## 9. Code (as built)

| File | Content |
|---|---|
| `entities/privacy.py` | `RedactedSpan`, `Redaction.spans`, `substitute`, numbered-placeholder recognition |
| `entities/pii_tokens.py` | `TokenEntry` (fingerprint only), `TokenBook`, `TurnValue` (`SecretStr`), `BlindOperator`, `BlindTerm`, `CustomerMatch`, `MatchedCustomer`, token-term parsing |
| `ports/pii_token_vault.py` | `PiiTokenVault`, `PiiValueVault`, `CustomerDirectory`, `PiiLookupLog` |
| `interactors/pii_tokens.py` | `tokenize`, HMAC fingerprints with a per-session key, `PrivacyTokens` with race retry |
| `interactors/customer_lookup.py` | `ResolveCustomerTokens`: role gate, caps, audit, exact term |
| `interactors/reports.py` | `ResolveCustomerTerms`: token → pseudonymous search terms, ambiguity check, confirmation customers |
| `gateways/bigquery/blind_sql.py` | match CTE compiler (all operators), `pseudonym_lookup_sql`, `compose_blind_query` |
| `gateways/bigquery/customers.py` | `BigQueryCustomerDirectory` |
| `gateways/pii_vault/` | AES-GCM `ValueSealer`, `SealedPiiValueVault` over Redis or an in-process store (local only) |
| `repos/pii.py`, migration `9a6d3c1e5f20` | `pii_tokens`, `pii_lookups` |

## 10. Phases and effort

**Build a minimal subset now; design the rest.**

| Phase | Scope | Effort | Status |
|---|---|---|---|
| 0 | Parameter guard in `RunQuery` + random parameter names (§5.2). Security fix | 1–2 h | Built |
| 1 | Numbered tokens: redactors return spans, Postgres fingerprint book (cascade delete), wire into `ChatTurn` and feedback, widen `is_masked`, summarizer/writer token rule, tests | 0.75–1 day | Built |
| 2 | Name → pseudonym for report search/deletion: exact tiers, masked email per customer in reports plus `report_entities`, confirmation shows the matched customer, audit log, role gate, unit red-team | 1.5 days | Built |
| 3 | Analyst `find_customer` with the server-side ladder, match sets in `run_sql`, cross-turn fuzzy (encrypted vault, option C), UI legend, guard prompt and live red-team/evals | 3–4 days | Design only |

Phase 3 adds a new personal-data capability (looking people up by name) that the client has not asked for. It needs a product and legal decision before it is built.
