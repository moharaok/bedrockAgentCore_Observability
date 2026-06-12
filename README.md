# AgentCore Observability Dashboard (Python + Flask)

A lightweight dashboard for monitoring Bedrock AgentCore agents. Pure Python backend with vanilla JS frontend — no build step required.

## Features

- Loads all Bedrock AgentCore agents from configured AWS regions
- Queries `aws/spans` CloudWatch Logs for session data
- Click an agent to view detailed metadata + session activity
- Time range selector (1h, 6h, 24h, 7d, 30d)

## Quick Start

```bash
cd agentcore-dashboard-python

# Install dependencies
pip install -r requirements.txt

# Run (set your AWS profile and regions)
AWS_PROFILE=mohan CONFIGURED_REGIONS=us-east-1 python app.py
```

Open http://localhost:3000

## Configuration

| Env Variable | Description | Default |
|---|---|---|
| `AWS_PROFILE` | AWS credential profile to use | default |
| `CONFIGURED_REGIONS` | Comma-separated list of regions | us-east-1 |
| `PORT` | Server port (via Flask) | 3000 |

## Project Structure

```
agentcore-dashboard-python/
├── app.py                  # Flask backend (API + serves frontend)
├── requirements.txt        # Python dependencies
├── templates/
│   └── index.html          # Main HTML page
└── static/
    ├── css/styles.css      # Dashboard styles
    └── js/app.js           # Frontend logic (vanilla JS)
```

## AWS Permissions Required

- `bedrock-agentcore-control:ListAgentRuntimes`
- `bedrock-agentcore-control:GetAgentRuntime`
- `logs:StartQuery`
- `logs:GetQueryResults`

## Deploy to AWS Amplify

1. Add an `amplify.yml` build spec:
```yaml
version: 1
frontend:
  phases:
    build:
      commands:
        - pip install -r requirements.txt
  artifacts:
    baseDirectory: .
    files:
      - '**/*'
```

2. Or deploy as a container using AWS App Runner / ECS for the Flask backend.
