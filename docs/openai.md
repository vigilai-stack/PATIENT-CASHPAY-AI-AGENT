## **The OpenAI interpreter**

Data note: runs entirely on synthetic data; no real patient or clinic data is used.

This is how the live demo at https://cashpay.vigilaisolutions.com uses an OpenAI model, and the limits we put around it. The model understands what the patient is asking; the server decides everything that touches money. Without an API key, the app falls back to a built-in rule-based engine, so it still runs.

#### **What the model is allowed to do**

- Read the current patient message when that whole message fits the excerpt budget, plus a bounded excerpt of earlier saved lines, plus a service catalog that has codes, names, and included items, and no posted prices.
- Return one JSON object: an allowlisted intent, an optional catalog service code, and a short note.
- The server then calls the same clinic methods the rule-based engine uses. Those methods still go through the spending firewall.

#### **What the model cannot do**

- It is not given tools, functions, or a way to call PayPal. A tool call in the response is discarded and the patient is told the model is unavailable.
- It cannot set a price, a refund amount, a discount percent, or a capture. Those values come from the database, the patient's own text (for a refund amount or an explicit percent), and clinic policy.
- It cannot choose a Botox quantity. Any message that looks like a unit quantity stays on the server parser. The parser still refuses a missing, ranged, or ambiguous quantity. 200 is a demo checkout ceiling, not a medical limit.
- It cannot approve a refund, a discount, or a deposit capture. Those stay staff-only.
- It cannot change its role. Every request it queues is recorded as coming from the agent, never from staff.
- There is one HTTP request per new patient question, and one more only when that person presses the saved-message retry. A duplicate submit of the same client id does not call again. There is no automatic retry and no tool loop.
- If the key is missing, the call times out, or the response is an error or malformed, the chat says so honestly instead of guessing. Button commands such as `pay full` keep working, because they never needed the model.

#### **Model**

The model id is set by `OPENAI_MODEL` (default in `app/config.py`).

The request sets `reasoning_effort` to `none` so the completion budget is not spent on hidden reasoning tokens, and it does not send a `tools` array. Output is capped with `max_completion_tokens`.

#### **Server settings**

These live only in the server environment (never in the repo). The live demo sets `LLM_MODE=openai` and an API key; with no key, the app uses the rule-based engine.

| Variable | Bound |
|---|---|
| `LLM_MODE` | `openai` (as in the live demo) or `mock` (rule-based engine, the default when unset). |
| `OPENAI_MODEL` | Model id; letters, digits, `.`, `_`, `-` only. |
| `OPENAI_API_KEY` | Server only. Never sent to the browser, written into the repo, or returned by `/health`. A key with whitespace is ignored. |
| `OPENAI_BASE_URL` | HTTPS host `api.openai.com` only. Any other host is refused before a request is sent. |
| `OPENAI_TIMEOUT_SECONDS` | 1 to 20, default 8. |
| `OPENAI_MAX_OUTPUT_TOKENS` | 32 to 256, default 120. |
| `OPENAI_MAX_REQUESTS_PER_MINUTE` | 1 to 60, default 12, for the whole process. |
| `OPENAI_MAX_CONCURRENCY` | 1 to 4, default 2. Extra callers get the unavailable reply instead of waiting in a queue. |

`/health` adds `openai_configured` (true or false). When mode is `openai` it also adds `openai_model`. It never adds the key.

#### **Request ceiling**

The catalog prompt is built without prices. An accepted patient or staff message can be up to 8000 characters and is stored unchanged. A longer message is refused before it is saved. The model excerpt is separate: default 900 characters, and a line that does not fit is left out whole. Default output cap is 120 tokens. Model calls are rate-limited to 12 per minute and 2 at a time by default.

#### **Conversation continuity**

Each signed-in synthetic patient has one saved transcript. Lines keep server-side speaker identity (`patient`, `assistant`, or `staff`) and an increasing `seq`. The browser does not get to choose the speaker. Reload and a new session cookie for the same patient restore that transcript. A different synthetic patient does not see it.

The model prompt is separate from the saved transcript:

- Accepted messages are stored exactly, up to 8,000 characters. A longer message is politely refused, not shortened.
- The model sees a bounded excerpt of recent lines (default 900 characters, `CONVERSATION_PROMPT_CHARS`), with patient and staff lines kept ahead of assistant replies. The catalog it sees lists service names and included items, with no prices.
- The model proposes a service and a focus (`price`, `includes`, `excludes`, `other`). **The server chooses the service itself.** When the patient's own words name exactly one catalog service, that wins. A correction such as "I meant the chest x-ray, not the knee MRI" selects the chest X-ray even if the model guessed the knee MRI. If the words match several services, the reply names them and asks, rather than picking a price.
- An includes or excludes question uses the catalog text and the posted price, and quotes the latest staff note when there is one, naming who wrote it.
- Saying "I am staff" in the patient box does not grant staff access. Symptom, diagnosis, and dosing questions are saved for a person to read and are not answered with medical advice. Distress wording gets an acknowledgement before a price.
- A failed model call keeps the patient's message in the transcript, and a retry button sends that same message once.

#### **Example from the live demo**

Ask *"How much is a knee MRI?"* and you get $450.00 from the price list. Then say *"I meant the chest x-ray, not the knee MRI."* and the agent switches to the chest X-ray at $85.00. The server, not the model, picks the service and the price.

Button commands keep working when the model is down because they never required it. Refunds, discounts, and deposit capture still require staff.
