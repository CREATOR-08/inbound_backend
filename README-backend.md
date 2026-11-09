# Inbound AI --- Backend

FastAPI backend and asynchronous processing system for Inbound AI: an
AI-powered Gmail workspace with email synchronization, AI-assisted
workflows, calendar integration, and a LangGraph-based assistant.

> **Repository:** Backend\
> **Frontend:** [Inbound AI Frontend](../inbound) --- replace this with
> the actual frontend repository URL\
> **Status:** Active development

## Overview

The backend is responsible for authenticated API operations, Gmail
integration, email persistence, AI orchestration, calendar workflows,
and background processing.

The API server handles HTTP requests. Long-running Gmail synchronization
and AI tasks are placed on Redis queues and consumed by separate worker
processes. The workers use the same backend codebase, database, and
service layer, but they do not expose HTTP endpoints.

## Architecture

``` mermaid
flowchart TB
    FE[Next.js Frontend] -->|HTTPS / REST| API[FastAPI API on Render]
    API --> AUTH[Auth and token validation]
    API --> DB[(Neon PostgreSQL)]
    API -->|enqueue jobs| REDIS[(Upstash Redis)]
    REDIS --> GQ[gmail queue]
    REDIS --> AQ[ai queue]
    GQ --> GW[Gmail RQ Worker]
    AQ --> AW[AI RQ Worker]
    GW --> GMAIL[Gmail API]
    GW --> DB
    AW --> GRAPH[LangGraph / Gemini]
    AW --> DB
    API --> GRAPH
    API --> CAL[Google Calendar API]
    GMAIL -->|Pub/Sub notifications| PS[Google Cloud Pub/Sub]
    PS -->|push webhook| API
```

### Production services

  -----------------------------------------------------------------------
  Component               Responsibility          Deployment
  ----------------------- ----------------------- -----------------------
  FastAPI API             HTTP endpoints, auth,   Render
                          webhook handling,       
                          request validation      

  Gmail worker            Initial sync,           Vercel Daemons
                          incremental sync,       
                          catch-up, Pub/Sub sync  
                          jobs                    

  AI worker               Background AI email     Vercel Daemons
                          tagging jobs            

  PostgreSQL              Users, connected        Neon
                          accounts, emails, agent 
                          state and application   
                          data                    

  Redis                   RQ job queues and job   Upstash
                          state                   

  Gmail API               Mailbox access,         Google
                          history, sending,       
                          labels/read state,      
                          watch                   

  Google Pub/Sub          Gmail push notification Google Cloud
                          delivery                

  Gemini                  Model inference         Google AI

  LangSmith               Tracing / observability LangSmith
                          where configured        
  -----------------------------------------------------------------------

## Background workers

Two RQ workers are deployed as separate Vercel Daemons.

### Gmail worker

``` bash
python -m app.worker.gmail
```

Consumes the `gmail` queue and executes:

-   `run_initial_gmail_sync_job`
-   `run_pubsub_gmail_sync_job`
-   `run_existing_account_catchup_job`
-   `run_incremental_recovery_job`

### AI worker

``` bash
python -m app.worker.ai
```

Consumes the `ai` queue and executes:

-   `tag_recent_untagged_emails_job`

Queue definitions are centralized in `app/core/queues.py`. The `default`
queue is defined but is not currently consumed by the two worker
entrypoints; the current enqueue call sites use the Gmail and AI queues.

Both Render and Vercel workers must use the same `REDIS_URL` and
`DATABASE_URL`. The API does not call the Vercel worker URL. It enqueues
jobs in Redis, and workers consume them from Redis.

## Gmail synchronization flow

``` mermaid
sequenceDiagram
    participant UI as Frontend
    participant API as FastAPI
    participant R as Redis
    participant W as Gmail Worker
    participant G as Gmail API
    participant DB as PostgreSQL

    UI->>API: Connect / request initial sync
    API->>R: Enqueue initial sync job
    R-->>W: Worker consumes job
    W->>G: Fetch mailbox messages
    G-->>W: Message data
    W->>DB: Persist emails and sync state
    UI->>API: Request inbox data
    API->>DB: Query stored emails
    DB-->>API: Email records
    API-->>UI: Inbox response

    G-->>API: Pub/Sub notification
    API->>R: Enqueue incremental sync job
    R-->>W: Worker consumes job
    W->>G: Fetch Gmail history changes
    W->>DB: Persist updates and cursor
```

The API acknowledges webhook deliveries according to the webhook's
validation and enqueueing behavior. Worker execution and job retry
handling are separate from the HTTP request lifecycle.

## LangGraph and AI architecture

The project uses LangGraph to organize AI behavior into task-specific
graph flows and service functions. Keep the distinction between the
automatic email triage path and user-triggered assistant workflows.

### Email triage

The existing triage graph is intentionally small:

``` mermaid
flowchart TD
    S[EmailAgentState] --> T[triage_node]
    T --> E[END]
```

The graph receives email state, runs the triage node, and returns the
resulting state.

### Reply drafting

Reply drafting is a separate user-triggered path rather than a node in
the minimal triage graph:

``` mermaid
flowchart TD
    U[User requests a reply] --> C[Selected email and conversation context]
    C --> P[Reply drafting prompt]
    P --> M[Gemini model]
    M --> STR[Stream generated draft]
    STR --> UI[Frontend compose experience]
    UI --> APPROVE[User reviews and edits]
    APPROVE --> SEND[Explicit send action]
```

### Calendar extraction and scheduling

The scheduler path uses cleaned email text and the email's received
timestamp as context. It produces a structured `CalendarEventDraft`,
then resolves the draft into a calendar event proposal. Relative dates
and timezone handling require careful validation, especially for
Asia/Kolkata.

``` mermaid
flowchart TD
    E[Email content] --> CLEAN[Clean email text]
    CLEAN --> CTX[Include received timestamp and timezone context]
    CTX --> NODE[scheduler_node]
    NODE --> DRAFT[Structured CalendarEventDraft]
    DRAFT --> RESOLVE[resolve_calendar_event_draft]
    RESOLVE --> REVIEW[User reviews scheduling proposal]
    REVIEW --> CONFIRM[Calendar confirmation]
    CONFIRM --> GC[Google Calendar API]
```

### Context-aware assistant

The assistant API is exposed through:

``` text
POST /api/v1/assistant/command
```

The assistant architecture supports distinct request types and response
behaviors:

``` mermaid
flowchart TD
    U[Authenticated user command] --> CTX[Collect selected email and inbox context]
    CTX --> G[LangGraph assistant]
    G --> ROUTE{Intent routing}
    ROUTE -->|Question| Q[Answer user question]
    ROUTE -->|Data query| SQL[Query stored PostgreSQL email data]
    ROUTE -->|UI action| UI[Return supported UI action]
    ROUTE -->|Workflow| PLAN[Plan multi-step workflow]
    PLAN --> APPROVAL[Human approval gate]
    APPROVAL -->|Approved| EXEC[Execute approved workflow steps]
    APPROVAL -->|Not approved| HOLD[Wait for clarification or approval]
    Q --> OUT[Structured assistant response]
    SQL --> OUT
    UI --> OUT
    EXEC --> OUT
    HOLD --> OUT
```

The assistant uses authenticated user context and can receive the active
inbox filter and selected email from the frontend. Data-query behavior
uses stored email data; it should not be described as automatically
fetching arbitrary Gmail history on every question.

Sensitive actions such as sending or archiving must remain behind
explicit approval/confirmation. Supported UI actions are constrained by
the frontend's implemented action handlers.

LangSmith tracing is used where configured to help inspect model and
graph execution.

## Known API routes

The backend API is versioned under `/api/v1`.

  ------------------------------------------------------------------------------------
  Method                  Route                                Purpose
  ----------------------- ------------------------------------ -----------------------
  `POST`                  `/api/v1/assistant/command`          Assistant command /
                                                               workflow request

  `POST`                  `/api/v1/emails/watch`               Register or renew Gmail
                                                               watch

  `POST`                  `/api/v1/emails/sync-initial`        Initiate initial Gmail
                                                               synchronization

  `POST`                  `/api/v1/webhooks/gmail/pubsub`      Receive Gmail Pub/Sub
                                                               push notifications

  `GET`                   `/api/v1/emails`                     Retrieve stored emails

  `GET`                   `/api/v1/emails/{email_id}/body`     Retrieve email body

  `POST`                  `/api/v1/actions/draft`              Draft an email reply

  `POST`                  `/api/v1/actions/send`               Send an email

  `POST`                  `/api/v1/actions/schedule`           Create a scheduling
                                                               proposal

  `POST`                  `/api/v1/actions/calendar/confirm`   Confirm a calendar
                                                               action
  ------------------------------------------------------------------------------------

Routes may require authentication and request-specific payloads. Consult
the source schemas and API docs for exact request/response contracts.

## Tech stack

  Area                Technology
  ------------------- ----------------------
  API                 FastAPI
  Language            Python
  ORM                 SQLAlchemy
  Database            PostgreSQL / Neon
  Job queue           RQ
  Queue broker        Redis / Upstash
  Email integration   Gmail API
  Push events         Google Cloud Pub/Sub
  Calendar            Google Calendar API
  AI orchestration    LangGraph
  Models              Gemini
  Observability       LangSmith
  Container           Docker
  API hosting         Render
  Worker hosting      Vercel Daemons

## Environment variables

Set secrets in the hosting provider's environment settings. Never commit
`.env` files or paste secret values into logs or issues.

  -------------------------------------------------------------------------------------
  Variable                                          Required / purpose
  ------------------------------------------------- -----------------------------------
  `DATABASE_URL`                                    PostgreSQL connection string

  `REDIS_URL`                                       Redis connection string
                                                    (`rediss://` for TLS Upstash
                                                    connections)

  `GOOGLE_API_KEY`                                  Gemini API key

  `GOOGLE_CLIENT_ID`                                Google OAuth client ID

  `GOOGLE_CLIENT_SECRET`                            Google OAuth client secret,
                                                    including Gmail token refresh

  `GOOGLE_TOKEN_SYNC_SECRET`                        Shared secret used by the Google
                                                    token-sync API route, if enabled

  `GMAIL_PUBSUB_TOPIC`                              Gmail Pub/Sub topic; code has a
                                                    default

  `GEMINI_FAST_MODEL`                               Optional fast-task model override

  `GEMINI_REASONING_MODEL`                          Optional reasoning-task model
                                                    override

  `GEMINI_REQUEST_TIMEOUT_SECONDS`                  Optional; defaults to `25`

  `INITIAL_GMAIL_FETCH_CONCURRENCY`                 Optional; defaults to `5`

  `GOOGLE_PROFILE_VERIFICATION_CACHE_TTL_SECONDS`   Optional; defaults to `30`
  -------------------------------------------------------------------------------------

The Render API and Vercel workers must share the same production
`DATABASE_URL`, `REDIS_URL`, and relevant Google credentials. Configure
worker-specific variables in the `inbound-workers` Vercel project as
well.

## Local development

### Prerequisites

-   Python 3.12
-   PostgreSQL database
-   Redis instance
-   Google OAuth credentials and enabled Gmail API
-   Gemini API key
-   Google Cloud Pub/Sub topic and subscription for realtime Gmail sync

### Create and activate a virtual environment (Windows CMD)

``` cmd
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
```

### Install dependencies

``` cmd
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Configure environment

Create a local `.env` file with the required variables. Do not commit
it.

``` env
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379/0
GOOGLE_API_KEY=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GMAIL_PUBSUB_TOPIC=projects/your-project/topics/gmail-notifications
```

Use your actual local database and Redis settings. For local
development, the frontend's API base URL should point to the local
FastAPI server.

### Run the API

Use the project's configured ASGI entrypoint. For example, if
`app.main:app` is the FastAPI application:

``` bash
uvicorn app.main:app --reload
```

### Run workers in separate terminals

``` bash
python -m app.worker.gmail
```

``` bash
python -m app.worker.ai
```

On Windows, the worker entrypoints use `SimpleWorker`; the Linux
container deployment uses the standard RQ `Worker`.

## Docker and worker deployment

The Docker image installs `requirements.txt`, copies the `app` package,
and defines the Gmail worker as its default command. Vercel Daemons
override the command per daemon using `vercel.toml`.

The current deployment defines two daemon processes:

``` toml
[daemons.gmail-worker]
root = "./"
entrypoint = "Dockerfile"
command = ["python", "-m", "app.worker.gmail"]
instances.bom1 = 1

[daemons.ai-worker]
root = "./"
entrypoint = "Dockerfile"
command = ["python", "-m", "app.worker.ai"]
instances.bom1 = 1
```

Production services:

-   API: `https://inbound-backend-9kmc.onrender.com`
-   Workers project: `inbound-workers` on Vercel
-   Redis: Upstash
-   Database: Neon

The Vercel worker project URL is not an API endpoint. The Render API
enqueues jobs into Redis; the Vercel daemons consume them from the same
Redis queues.

## Gmail Pub/Sub setup

Gmail notifications are published to the configured topic:

``` text
projects/inbound-511714/topics/gmail-notifications
```

The Gmail API push service account must have the Pub/Sub Publisher role
on the topic:

``` text
gmail-api-push@system.gserviceaccount.com
```

The push subscription should deliver to the deployed backend webhook:

``` text
https://inbound-backend-9kmc.onrender.com/api/v1/webhooks/gmail/pubsub
```

Keep the subscription's authentication configuration consistent with the
webhook's validation logic. Do not point production Pub/Sub
subscriptions to localhost or the worker deployment.

## Planned: hybrid RAG inbox search

> **Roadmap item --- not implemented yet.** The current email assistant
> uses existing SQL/basic retrieval. A dedicated semantic and hybrid
> inbox retrieval system is planned.

Target architecture:

``` mermaid
flowchart TD
    Q[Natural-language inbox query] --> QE[Query embedding]
    Q --> KW[PostgreSQL keyword search]
    QE --> VS[Vector similarity search]
    KW --> META[Apply user and metadata filters]
    VS --> META
    META --> HY[Hybrid fusion / ranking]
    HY --> DEDUP[Deduplicate and select relevant emails]
    DEDUP --> CTX[Build grounded context with email IDs]
    CTX --> LG[LangGraph assistant]
    LG --> ANS[Answer with source references]
```

Proposed implementation:

1.  **Indexing:** create embeddings for normalized email subject and
    body after ingestion.
2.  **Storage:** evaluate PostgreSQL `pgvector` to keep vectors close to
    existing email records and metadata.
3.  **Metadata filters:** enforce user ownership, date ranges, sender,
    category, priority, read/unread, and other supported filters.
4.  **Hybrid retrieval:** combine PostgreSQL full-text / keyword search
    with vector similarity.
5.  **Ranking:** deduplicate results and evaluate a fusion strategy such
    as Reciprocal Rank Fusion.
6.  **Grounding:** pass only relevant email excerpts and identifiers
    into the assistant.
7.  **Citations:** return source email IDs so the frontend can open the
    original messages.
8.  **Evaluation:** build a query set and measure retrieval relevance
    before making semantic search the default.

Security requirement: every retrieval path must apply the authenticated
user's ownership filter. Never allow vector similarity search to return
another user's email content.

## Reliability and operational notes

-   Gmail sync uses persisted history cursors; preserve and advance them
    carefully.
-   Queue failures should be surfaced rather than silently acknowledged.
-   RQ jobs have retry configuration in the centralized queue module.
-   Initial and incremental sync jobs use execution IDs to help reject
    stale or duplicate work.
-   Keep API and worker deployments on compatible versions of the same
    codebase.
-   Monitor worker logs separately from Render API logs.
-   Do not run duplicate production consumers unintentionally during a
    worker migration.

## Roadmap

-   [ ] Complete Google OAuth verification for production use.
-   [ ] Verify Gmail authorization, token refresh, and Pub/Sub delivery
    end to end.
-   [ ] Validate relative date/time extraction and Asia/Kolkata
    scheduling.
-   [ ] Complete production worker processing tests for both queues.
-   [ ] Implement email embeddings and vector indexing.
-   [ ] Implement hybrid PostgreSQL + vector inbox retrieval.
-   [ ] Add grounded answers with source-email references.
-   [ ] Add retrieval evaluation and relevance monitoring.

## Repository separation

This repository contains the **backend**: FastAPI routes, authentication
dependencies, database models, Gmail and Calendar services, LangGraph
orchestration, RQ jobs, and worker entrypoints.

The Next.js UI and browser-side assistant experience live in the
separate frontend repository.
