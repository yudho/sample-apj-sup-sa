# Virtual Interview Coach

A **voice-first AI mock-interview platform** for undergraduates (18+) preparing to enter the
workforce. A student signs in, consents, uploads a resume, pastes a target job description,
picks a difficulty and length — then has a **spoken** interview with an AI interviewer that
asks personalized questions, listens, and probes follow-ups like a real recruiter. At the end
it speaks a short score-free wrap-up, produces an evidence-anchored written **scored report**,
and over time distills **coach's notes** across all of a student's sessions.

## What's built

- **Real-time voice interview** — sub-second turn-taking (response gap p50 ≈ 0.3s live),
  hands-free or hold-to-talk, voice barge-in, interviews that end themselves at the chosen
  length (3-minute "quick test drive" up to 30 minutes). Pipecat on ECS Fargate, Deepgram
  STT/TTS, Bedrock (Claude) replies, WebRTC direct media.
- **Personalization** — questions grounded in the student's confirmed resume facts + the pasted
  job description; semantic question-bank retrieval (pgvector) with JIT generation for
  uncovered roles; Easy/Moderate/Difficult are behaviorally distinct.
- **Honest scored reports** — async scoring on a fixed, level-independent rubric; every
  competency score anchored to a verbatim quote; self-consistency < 0.5 points; per-question
  feedback with strong-answer examples built from the student's own background; full interview
  transcript; per-answer audio playback (consent-gated).
- **Session history & coaching dashboard** — a picker over all past sessions' reports, and
  cross-session **coach's notes** (recurring strengths/weaknesses, honest trend, prioritized
  next actions) regenerated automatically after each scored session.
- **Privacy by architecture** — explicit consent gates recording; S3 SSE-KMS + RDS are the only
  PII homes; 30-day retention; one-click hard delete with zero residual (audio + transcript +
  scores + coaching notes).

All six constitution capability gates (G1 voice latency … G6 privacy) have been delivered; see
`.specify/memory/constitution.md` and `docs/5-Delivery-Roadmap.md`.

## Repository layout

| Path | What it is |
|---|---|
| `frontend/` | React/TypeScript SPA (Vite), served from S3 via CloudFront |
| `backend/` | FastAPI app API (sessions, resume, consent, reports, guidance) |
| `voice-worker/` | Pipecat real-time voice pipeline (ECS Fargate); owns the DB schema (`src/db_migrate.py`) |
| `report-worker/` | Async SQS worker: report scoring, retention sweep, coaching guidance |
| `bank/` | Offline question-bank tooling (generate / screen / embed) |
| `infra/g1/` | CloudFormation (one demo stack) + CodeBuild image pipeline + deploy scripts |
| `specs/` | Spec-Kit feature specs (001…008): spec → plan → tasks → gate evidence |
| `docs/` | Product/technical specs, delivery roadmap, demo write-up, runbooks |

## Development

Each Python service has its own venv (`backend/.venv`, `report-worker/.venv`,
`voice-worker/.venv-pipecat` — run voice-worker tests in **both** of its venvs). DB-backed
tests use a local pgvector container on port 55432. The frontend uses Vitest + Testing
Library. See `specs/008-session-review-coaching/quickstart.md` for the current dev loop and
the deploy recipe (CodeBuild images → CloudFormation → SPA sync; RDS migrations run as a
one-off in-VPC ECS task).

A periodic **health review** of the deployed stack (logs + metrics vs the product's gates) is
available as the `health-review` skill (`.claude/skills/health-review/`).

## Origin

The original concept — give every student the kind of personalized interview preparation a
professional career coach would provide — included video/body-language analysis and younger
students. V1 deliberately scopes to **voice-first, 18+** (see the constitution's
non-negotiables and deferred list); video analysis, mobile, and institutional dashboards
remain explicitly deferred.
