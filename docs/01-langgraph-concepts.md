# 01 · LangGraph concepts, mapped to this code

Keep `app/graph.py` open. Every concept below points at real code. (Mock-case topic IDs appear in brackets.)

---

## Graph — the blueprint  [Q22, Q23]

A `StateGraph` is a **definition** of a workflow: which steps exist and how they connect.
We build exactly **one** graph and compile it once. It serves every user and every conversation.

```python
g = StateGraph(State)
g.add_node("extract_intent", extract_intent)
...
graph = g.compile(checkpointer=checkpointer)
```

No multi-agent swarm: one graph with clear branches is easier to test and reason about.

## State — the shared notebook  [Q23, Q24, Q25]

`State` is a `TypedDict`. Every node **reads** the whole state and **returns a partial update**
(a plain dict with only the keys it wants to change).

```python
def extract_intent(state: State) -> dict:
    intent = extractor.extract(last_user_text(state))
    return {"intent": intent.intent, "slots": ...}   # only these keys change
```

**Reducers.** By default a returned key *replaces* the old value. `messages` is declared

```python
messages: Annotated[list[dict], operator.add]
```

so updates are *appended* instead. That's the whole reducer idea: "how do I merge an update into
the existing value?"

**What we keep in state:** intent, slots, the route, the resolved recipient, `transfer_id`,
`workflow_status`, `bank_status_snapshot`, the response.
**What we never keep in state:** tokens, keys, balances-as-truth. `bank_status_snapshot` is literally
named "snapshot" to remind you it is a *copy* that can go stale.

## Node — one step  [Q23, Q31]

A node is just a Python function. Some call an LLM, most don't:

| LLM | Plain Python |
|---|---|
| `extract_intent` | `validate`, `read_spending`, `prepare_transfer`, `reconcile`, `respond` |

Rule of thumb for this mock case: **LLM for language, deterministic code for money, math, and policy.**

## Edge & conditional edge — what runs next  [Q22]

```python
g.add_edge("read_spending", "respond")              # always
g.add_conditional_edges("validate", route_after_validate,
                        ["clarify", "read_spending", "prepare_transfer", "respond"])
```

`route_after_validate` is a tiny function that looks at `state["route"]` and returns the **name** of
the next node. Note the design split: the `validate` *node* does the thinking and writes `route`
into state (so you can see it in the inspector); the conditional *edge* only reads it.

In the UI graph, dotted arrows are conditional edges.

## Checkpointer & thread_id — memory that survives  [Q27, Q28]

Compiling with a checkpointer makes LangGraph save the state **after every step**, keyed by a
`thread_id` you pass in:

```python
config = {"configurable": {"thread_id": "thread-3fa2c1"}}
graph.stream(input, config)
```

- `thread_id` = "which conversation/workflow is this?" — **not** "which graph". One graph, many threads.
- We use `SqliteSaver` writing to `data/agent.db`, so a paused thread survives a server restart.
- `graph.get_state(config)` returns the latest checkpoint: `.values` (the state), `.next`
  (nodes that will run on resume), `.interrupts` (why it's paused). That's what the **State inspector**
  panel shows.

## interrupt() & Command(resume=...) — pause and continue  [Q26]

Instead of a `while True: sleep(15)` polling loop, a node calls `interrupt(payload)`:

```python
def await_approval(state):
    signal = interrupt({"kind": "approval", "transfer_id": state["transfer_id"], ...})
    ...                                   # runs only after someone resumes the thread
```

What happens:

1. The graph stops, the checkpoint is saved, `stream()` ends. The HTTP request returns immediately.
2. The payload shows up in `get_state(config).interrupts` — the UI uses it to render a question or
   an approval card.
3. Later (seconds or days, even after a restart) someone calls

   ```python
   graph.stream(Command(resume={"transfer_id": "TX-..."}), same_config)
   ```

4. LangGraph **re-runs the interrupted node from the top**; this time `interrupt()` *returns* the
   resume value instead of pausing.

**Gotcha (step 4):** anything above `interrupt()` in that node runs twice. That's why
`prepare_transfer` is a **separate node before** `await_approval` — otherwise every resume would
create another bank transfer.

We use interrupt twice:

| Node | Resumed by | Resume value |
|---|---|---|
| `clarify` | the user's next chat message | their answer text |
| `await_approval` | the **bank** approval endpoint only | `{"transfer_id": ...}` |

## thread_id ≠ transfer_id  [Q28, Q29, Q30]

| | Owned by | Lives in | Meaning |
|---|---|---|---|
| `thread_id` | agent | `agent.db` checkpoints | "this workflow's saved state" |
| `transfer_id` | bank | `bank.db` transfers | "this money movement" |

`Assistant._remember` stores `transfer_id → thread_id` in its own table. When the bank approves
`TX-B`, we look up *its* thread and resume only that one. Every new chat request gets a new thread,
so two pending transfers can be approved in any order.

## Streaming — how the trace panel works

```python
for step in graph.stream(input, config, stream_mode="updates"):
    # step == {"validate": {...what validate returned...}}
    # or     {"__interrupt__": (Interrupt(value=...),)}
```

`stream_mode="updates"` yields exactly what each node returned — the cleanest way to *see* a graph run.
