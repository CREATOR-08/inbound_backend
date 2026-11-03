# Assistant API integration

The existing endpoint remains `POST /api/v1/assistant/command`. It still uses
the existing Google bearer-token authentication. The backend derives the user
from the verified Google profile; clients must not send a user ID.

## Request

```json
{
  "message": "Show me high-priority emails from Acme last week",
  "session_id": "session-123",
  "page_context": {
    "page": "inbox",
    "selected_email_id": "gmail-message-id",
    "active_filter": "ALL"
  },
  "conversation_history": [
    { "role": "user", "content": "I am reviewing a vendor message." },
    { "role": "assistant", "content": "Which message?" }
  ],
  "query_context": null
}
```

Conversation history and page context are request-provided, bounded, and
treated as untrusted input. There is no persistent assistant-session table;
the frontend must send any conversation history it wants the assistant to use.
Only page, selected email ID, and active filter are retained from page context.

To confirm a workflow that contains a send step, submit the returned plan
unchanged and explicitly set `confirm_workflow`:

```json
{
  "message": "Send the reply we planned",
  "session_id": "session-123",
  "confirm_workflow": true,
  "approved_workflow": [
    {
      "tool": "send_email_reply",
      "parameters": {
        "email_id": "gmail-message-id",
        "body": "Thanks for the details. I will follow up shortly."
      },
      "description": "Send the reply"
    }
  ]
}
```

Confirmation uses the authenticated user's account and rechecks ownership of
every email ID. Never infer confirmation from conversational text alone. The
explicit boolean and validated plan are required.

## Response envelope

```json
{
  "status": "success",
  "intent": "DATA_QUERY",
  "message": "Here are the matching locally stored emails.",
  "data": {
    "resource": "emails",
    "items": [
      {
        "id": "gmail-message-id",
        "thread_id": "gmail-thread-id",
        "sender": "Pat Example <pat@example.com>",
        "subject": "Project update",
        "preview": "The latest project update...",
        "received_at": "2026-10-28T10:30:00+00:00",
        "category": "ACTION_REQUIRED",
        "priority": "HIGH",
        "is_archived": false
      }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
    "next_offset": null,
    "query_spec": {
      "resource": "emails",
      "filters": [],
      "sort_by": "received_at",
      "sort_order": "desc",
      "limit": 20,
      "offset": 0
    }
  },
  "actions": [],
  "requires_confirmation": false,
  "requires_clarification": false
}
```

Errors use the same envelope and a `status` of `error`, `invalid_request`,
`rate_limited`, or `unsupported`. A `rate_limited` response can be retried
after a delay. A `requires_clarification` response should be shown as a prompt
for more detail. Do not render error messages as successful results.

Workflow plans that need confirmation use `status: "awaiting_confirmation"`,
`requires_confirmation: true`, and `data.approved_workflow`. Completed workflow
steps have `status: "completed"`; failed and subsequent unexecuted steps are
reported separately. The API never marks a failed external operation complete.

## TypeScript contract

```ts
export type AssistantIntent =
  | "DATA_QUERY"
  | "UI_ACTION"
  | "QUESTION"
  | "WORKFLOW"
  | "CLARIFICATION";

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AssistantRequest {
  message: string;
  session_id: string;
  page_context?: {
    page?: string;
    selected_email_id?: string;
    active_filter?: string;
  };
  conversation_history?: ConversationMessage[];
  query_context?: EmailQuerySpec;
  confirm_workflow?: boolean;
  approved_workflow?: WorkflowStep[];
}

export type AssistantAction =
  | { type: "NAVIGATE"; parameters: { page: "inbox" | "sent" | "drafts" | "calendar" }; description: string }
  | { type: "OPEN_EMAIL"; parameters: { email_id: string }; description: string }
  | { type: "OPEN_COMPOSE"; parameters: Record<string, never> | { to: string; subject: string; body: string }; description: string }
  | { type: "FILTER_EMAILS"; parameters: { filter: "ALL" | "URGENT" }; description: string }
  | { type: "CLEAR_FILTERS"; parameters: Record<string, never>; description: string };

export type WorkflowTool =
  | "search_emails"
  | "read_email"
  | "draft_email"
  | "send_email_reply";

export interface WorkflowStep {
  tool: WorkflowTool;
  parameters: Record<string, unknown>;
  description: string;
}

export interface EmailQueryResult {
  resource: "emails";
  items: Array<{
    id: string;
    thread_id: string | null;
    sender: string;
    subject: string;
    preview: string;
    received_at: string | null;
    category: string | null;
    priority: string | null;
    is_archived: boolean;
  }>;
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  query_spec: EmailQuerySpec;
}

export interface EmailQuerySpec {
  resource: "emails";
  filters: Array<{
    field: "sender" | "subject" | "priority" | "category" | "received_at" | "is_archived";
    operator: "eq" | "contains" | "gt" | "gte" | "lt" | "lte" | "in" | "between";
    value: string | boolean | string[] | boolean[];
  }>;
  sort_by: "received_at" | "sender" | "subject" | "priority";
  sort_order: "asc" | "desc";
  limit: number;
  offset: number;
}

export interface AssistantResponse<TData = unknown> {
  status:
    | "success"
    | "error"
    | "invalid_request"
    | "awaiting_confirmation"
    | "confirmed"
    | "rate_limited"
    | "unsupported";
  intent: AssistantIntent;
  message: string;
  data: TData | null;
  actions: AssistantAction[];
  requires_confirmation: boolean;
  requires_clarification: boolean;
}
```

`EmailQueryResult` filter specifications are constructed by the assistant.
The backend accepts only local email fields `sender`, `subject`, `priority`,
`category`, `received_at`, and `is_archived`; operators and result limits are
validated server-side. The database has no read/unread flag, so unread queries
are explicitly unsupported. Searching the local store never fetches new Gmail
messages. Use the existing `/api/v1/emails/sync-initial` or Pub/Sub sync flow to
populate local data.

## Frontend behavior

- Start the initial Gmail import with `POST /api/v1/emails/sync-initial`.
  A newly inserted account returns `202 Accepted` with status `pending` (or
  `running` if its initial import is already queued). Poll
  `GET /api/v1/emails/sync-initial/status` for `not_started`, `pending`,
  `running`, `completed`, or `failed`; refresh the inbox after it completes.
  For an existing account the POST completes an incremental catch-up and returns
  its existing initial status without scheduling another initial import.
- Initial import eligibility is recorded on `UserAccount` only when a row is
  first inserted. Existing rows, including legacy rows with `pending` or
  `running` initial statuses, are not re-imported with the three-day query. The
  same endpoint renews a missing or expired watch and processes Gmail history
  from the saved cursor. Its response additively includes `account_status`,
  `sync_mode`, `stored_email_count`, and `incremental_sync_status`. Poll the
  status endpoint for those fields while incremental synchronization is running
  or after it fails.
- Initial eligibility is consumed on the first attempt. If that first import
  fails after account creation, the existing account is not automatically
  eligible for another three-day import; recovery must not silently bypass
  this rule. Incremental failures retain the previous history cursor.
- A `pending` initial import whose account `updated_at` is more than five
  minutes old may be claimed again by a later sync/status request. Five minutes
  allows normal request scheduling and worker startup time while recovering a
  task lost during the enqueue/process-crash window. The stale claim refreshes
  `updated_at` atomically; the worker then atomically transitions `pending` to
  `running`. This is best-effort recovery for in-process BackgroundTasks, not a
  durable queue, and it does not reclaim `running`, `completed`, or `failed`
  imports.
- Send the Google bearer token in `Authorization`; do not provide a user ID.
- For `DATA_QUERY`, render `data.items` as email cards. When
  `data.next_offset` is non-null, request the next page with message `"next
  page"` and `query_context: data.query_spec`. The backend increments the
  validated offset; the per-request maximum is 50 rows.
- For `UI_ACTION`, dispatch only the allow-listed `actions`. Map `NAVIGATE`,
  `OPEN_EMAIL`, `OPEN_COMPOSE`, `FILTER_EMAILS`, and `CLEAR_FILTERS` to existing
  Next.js navigation/state handlers. Never evaluate action text as code.
- For `QUESTION`, render the grounded `message`; the backend retrieves the
  selected email from the user's local database when the question refers to it.
- For `WORKFLOW`, render each per-step result. Show a review/confirm UI before
  resubmitting `data.approved_workflow` with `confirm_workflow: true`. Sending
  a reply uses the existing Gmail reply service only after this confirmation.
- Render `requires_clarification` as a follow-up question and
  `requires_confirmation` as a confirmation prompt. Do not treat either as a
  completed operation.
- The endpoint is unchanged; request fields are additive. Existing clients can
  continue sending `message`, `session_id`, `page_context`, and
  `conversation_history`. `query_context` is optional and is used only for
  validated next-page requests. Clients must adopt the new response envelope
  and uppercase action names.

## Implemented capabilities and limits

- Local email search, authenticated-user isolation, date/category/priority/
  sender/subject/archive filters, stable ordering, and bounded pagination.
- UI action proposals are allow-listed and parameter-validated. The frontend
  performs the actions; the backend never runs generated JavaScript.
- Selected-email question context is loaded only when the question refers to
  email content and the email belongs to the authenticated user.
- Workflows support local email search, reading a scoped email, drafting a
  reply, and explicitly confirmed reply sending. Drafts open in the frontend
  compose UI; they are not sent automatically.
- Fetching new Gmail data, unread filtering, deleting email, and calendar-event
  creation from assistant workflows are unsupported. Existing standalone Gmail
  sync and calendar routes remain available.
- Gemini model names are configurable but have not been availability-checked
  through external API calls. Set `GEMINI_FAST_MODEL` and optionally
  `GEMINI_REASONING_MODEL` to model identifiers enabled for the deployment.

## Backend configuration

No dependency changes are required. The existing Google GenAI LangChain
integration is used. Configure `GOOGLE_API_KEY` and `DATABASE_URL` as already
required by the application. The assistant model registry defaults
`GEMINI_FAST_MODEL` to the project's previously configured
`gemini-3.5-flash-lite` and `GEMINI_REASONING_MODEL` to the assistant's
previously configured `gemini-3.8-flash`; these identifiers are configurable,
not asserted to be available or free in every account. Assistant model calls
use one bounded retry and a 25-second request timeout.
`GEMINI_REQUEST_TIMEOUT_SECONDS` can override the timeout. Gmail continues to
use the existing `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` settings.

## Google token persistence

Set the same high-entropy `GOOGLE_TOKEN_SYNC_SECRET` in the Next.js server
environment and the FastAPI environment. Do not expose it through a
`NEXT_PUBLIC_` variable. Better Auth sends Google account tokens to
`PUT /api/v1/auth/google-tokens` server-to-server; the endpoint verifies the
access token's Google email and preserves an existing refresh token when no
replacement is provided. If the FastAPI secret is unset, the endpoint returns
503 rather than accepting an unauthenticated sync. Keep this endpoint on HTTPS
outside local development.

The existing `user_accounts.access_token`, `refresh_token`, and `token_expiry`
columns are reused; no schema migration is required. Existing users can select
**Reconnect Google** in the inbox to repeat the offline-consent flow.
