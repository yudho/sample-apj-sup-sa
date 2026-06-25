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

## Deploy to AWS

The hosted demo is two CloudFormation stacks in one region (default **`us-west-2`**), deployed
in order. The full first run takes ~30–45 min (RDS create + image build dominate). Everything
is idempotent and safe to re-run.

```
gate-enablement.yaml   →   deploy.yaml            →   SPA + Cognito user
(RDS, Bedrock agent,       (ECR, ECS Fargate ×3,      (S3/CloudFront sync,
 Deepgram secret, CW)       ALB, CloudFront, Cognito)  seed a test user)
```

### Prerequisites

1. **AWS credentials** for an account where you are admin (`aws sts get-caller-identity` works).
2. **Bedrock model access** — in the Bedrock console → *Model access*, enable **Claude** in your
   target region **before** deploying (the agent and worker fail with `AccessDenied` otherwise).
   The stacks use `us.anthropic.claude-haiku-4-5` + `amazon.titan-embed-text-v2`.
3. **A Deepgram API key** in `voice-worker/.env` as `DEEPGRAM_API_KEY=...` (never committed; the
   deploy reads it and pushes it into Secrets Manager).
4. Local tooling: AWS CLI v2, Node 18+ / npm, and either **Docker** (for local image builds) or
   nothing extra (to build images in **AWS CodeBuild** — the recommended path on Apple-Silicon /
   arm64 hosts, since Fargate needs `linux/amd64`).

Pick the region once and keep it consistent — every script honours `AWS_REGION` (default
`us-west-2`):

```bash
export AWS_REGION=us-west-2
```

### Step 1 — gate-enablement stack (RDS, Bedrock agent, Deepgram secret)

Provisions the private RDS Postgres, the Bedrock agent, an (empty) Deepgram secret, and the
CloudWatch namespace that the demo stack reuses.

```bash
# Discover the default VPC + its CIDR (the stack creates its own private DB subnets inside it).
aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].{Id:VpcId,Cidr:CidrBlock}' --output table --region $AWS_REGION

aws cloudformation deploy \
  --stack-name interviewcoach-g1 \
  --template-file infra/g1/gate-enablement.yaml \
  --capabilities CAPABILITY_IAM \
  --region $AWS_REGION \
  --parameter-overrides VpcId=<VPC_ID> VpcCidr=<VPC_CIDR>   # e.g. 172.31.0.0/16 for the default VPC
```

Then **disable RDS managed-secret rotation once** (RDS ships it on a 7-day schedule; this demo
keeps the password static — see `infra/g1/README.md` for the full rationale):

```bash
SECRET=$(aws rds describe-db-instances --region $AWS_REGION \
  --db-instance-identifier interviewcoach-g1-latency \
  --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text)
aws secretsmanager cancel-rotate-secret --region $AWS_REGION --secret-id "$SECRET"
```

### Step 2 — demo stack + service images

The demo stack (`deploy.yaml`) creates the ECR repos, ECS services, ALB, CloudFront, the S3 SPA
bucket, and Cognito. Because a Fargate service can't reach steady state with an empty repo, the
deploy is **two passes** (services at 0 → push images → services at 1). Two supported paths:

**Path A — CodeBuild images (recommended; no local Docker, builds `linux/amd64` in the cloud):**

```bash
DEPLOY_PHASE=pass1  infra/g1/deploy.sh c1   # create stack + ECR repos (services at 0) + Deepgram secret
infra/g1/build-images.sh c1                 # build + push all 3 images to ECR via CodeBuild
DEPLOY_PHASE=finish infra/g1/deploy.sh c1   # scale services to 1, force new deploy, build + sync SPA
```

**Path B — local Docker (single command, x86_64 hosts with working Docker):**

```bash
infra/g1/deploy.sh c1                        # pass-1 → build/push all 3 images → secret → pass-2 → SPA
```

> `deploy.sh` populates the Deepgram secret from `voice-worker/.env`, and in the `finish`/default
> phase builds the SPA (writing the live Cognito ids into `frontend/public/config.js`), syncs it to
> S3, and invalidates CloudFront. `DEPLOY_PHASE` (`pass1` / `finish` / default `all`) lets the
> CodeBuild image step slot cleanly between the two CloudFormation passes — required on arm64 /
> Apple-Silicon hosts since Fargate runs `linux/amd64`.

### Step 3 — apply the database schema

The DB is private, so run the idempotent migration as a one-off **in-VPC ECS task** on the worker
task definition (no bastion needed — it reads `DB_SECRET_ARN`):

```bash
VPC_ID=$(aws ec2 describe-vpcs --region $AWS_REGION --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
SUBNET=$(aws ec2 describe-subnets --region $AWS_REGION \
  --filters Name=vpc-id,Values=$VPC_ID Name=map-public-ip-on-launch,Values=true \
  --query 'Subnets[0].SubnetId' --output text)
TASK_SG=$(aws ec2 describe-security-groups --region $AWS_REGION \
  --filters Name=vpc-id,Values=$VPC_ID Name=group-name,Values='*task*' \
  --query 'SecurityGroups[0].GroupId' --output text)

aws ecs run-task --cluster interviewcoach-g1-demo \
  --task-definition interviewcoach-g1-demo-voice-worker --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"voice-worker","command":["python","-m","src.db_migrate"]}]}' \
  --region $AWS_REGION
```

### Step 4 — seed a user and open the portal

```bash
# Print the demo URL:
aws cloudformation describe-stacks --stack-name interviewcoach-g1-demo --region $AWS_REGION \
  --query "Stacks[0].Outputs[?OutputKey=='DemoUrl'].OutputValue" --output text

# The Cognito pool is admin-create-only — seed one user (permanent password, no forced reset):
infra/g1/seed-user.sh you@example.com 'YourPassw0rd'
```

Open the **DemoUrl** in a browser, sign in with the seeded credentials, then: consent → upload a
resume → paste a job description → pick difficulty/length → start the spoken interview. Use the
3-minute "quick test drive" for a fast end-to-end check. After a session ends, the scored report
appears on the session-history page once the report-worker finishes (a few seconds).

### Teardown

```bash
aws cloudformation delete-stack --stack-name interviewcoach-g1-demo --region $AWS_REGION
aws cloudformation delete-stack --stack-name interviewcoach-g1      --region $AWS_REGION
```

`DeletionPolicy: Delete` on the DB, buckets, and CMKs means no PII (resume, audio, transcript,
scores) is retained after teardown — the bounded-blast-radius end state (FR-219).

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
