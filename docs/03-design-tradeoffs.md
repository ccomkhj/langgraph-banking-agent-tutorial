# 03 · Design choices and trade-offs

This app optimises for **being easy to read**. Below: what is simplified, what a real system would do,
and which mock-case topic it relates to. Nothing here claims to describe any real bank's systems.

## Simplified on purpose

| Here | Real system | Why simplified |
|---|---|---|
| **SQLite** for bank + checkpoints (`SqliteSaver`) | Postgres (`PostgresSaver` / `AsyncPostgresSaver`), separate bank service | Zero setup. Restart-survival works the same; swap is ~3 lines (exercise 3). [Q27] |
| Sync FastAPI routes + sync graph | async routes + `AsyncPostgresSaver` | Fewer concepts. FastAPI runs sync routes in a threadpool. |
| Bank is a Python module in the same process | separate service behind authenticated REST/MCP | Keeps the boundary visible in code without networking. Agent still only calls `bank.*` functions. [Q07, Q13] |
| "Approve" button = trusted approval | device-bound signature over (id, recipient, amount, expiry), verified by the bank | Device crypto is out of scope (mock-case topic Q21). [Q15–Q17] |
| One fake user, no auth | OAuth/OIDC; identity from trusted runtime context, not from state | Not a learning goal here. [Q03, Q14] |
| Worker runs when you click a button | background worker / queue consumer | Makes "async" *visible*. [Q09, Q10] |
| Outbox table polled by the worker | outbox + Kafka/relay | Kafka is an optional extension of the mock case. [Q06] |
| Mock regex extractor | real LLM with structured output (included, optional) | Tests must be deterministic and free. [Q31] |
| Templated responses | optional LLM phrasing over validated data | Numbers can't be hallucinated. [Q31] |
| EUR only, two periods (this/last month) | full currency + date handling with time zones | Keep validation readable. |
| One thread per request | long-lived conversation threads + sub-workflows | The simple rule from Q29; avoids two approvals sharing one checkpoint. |
| Approved transfers aren't re-checked against limits/fraud | limits, risk scoring, replay protection | Out of scope. [Q14, Q15] |

## Decisions worth noticing

**Clarification policy lives in Python, not the LLM** (`validate`). Ask when: category is vague or missing,
recipient is missing / unknown / matches several contacts, or amount is missing. Proceed when a
read-only request just lacks a period — but *say* the assumption ("I assumed this month"). Money
movement never proceeds on a guess. [Q33]

**Two pause points, two different resumers.** `clarify` is resumed by chat; `await_approval` is resumed
only by `Assistant.decide`, which is only called after `bank.decide_transfer` succeeded. `Assistant.chat`
explicitly refuses to resume an approval interrupt. [Q03, Q26]

**Defence in depth on resume.** `await_approval` checks the resume payload's `transfer_id` matches its own.
The mapping table should already guarantee this; the check makes a bug loud instead of silent. [Q30]

**Reconcile, don't trust.** After resume, `reconcile` re-reads the transfer from the bank. The response is
built from that, which is why it says "APPROVED (queued)" rather than "done". [Q08, Q09]

**Idempotency is a database fact, not a hope.** `UPDATE … WHERE status='APPROVED'` (payment) and a
`UNIQUE` constraint (notifications) make duplicates no-ops — independent of how many times the
worker/event fires. [Q05, Q11]

**Prompt-injection safety is structural.** No detector model. Tool output is shown, never routed on;
writes need a bank-side approval. [Q12]

## Known limitations

- No auth at all — anyone who can reach the server can click Approve. Fine for localhost learning.
- A single shared SQLite connection per DB with a simple lock; not built for concurrency.
- The mock extractor is brittle on purpose; real phrasing variety is what the eval set is for.
- Seed data is relative to today's date; on the 1st of a month "this month" has little data.
