# AgentCore Observability Dashboard (Python + Flask)

A lightweight dashboard for monitoring Bedrock AgentCore agents. Pure Python backend with vanilla JS frontend — no build step required.

## Features

- Loads all Bedrock AgentCore agents from configured AWS regions
- Queries `aws/spans` CloudWatch Logs for session data
- Click an agent to view detailed metadata + session activity
- Time range selector (1h, 6h, 24h, 7d, 30d)

## Quick Start

```bash

# Install dependencies
pip install -r requirements.txt

# Run (set your AWS profile and regions)
export AWS_ACCESS_KEY_ID="<Acces key id>"
export AWS_SECRET_ACCESS_KEY="<secret acess key>"
export AWS_SESSION_TOKEN="<access token">
python3 app.py
```

Open http://localhost:3000


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

- `bedrock-agentcore-control:List*`
- `bedrock-agentcore-control:Get*`
- `logs:StartQuery`
- `logs:GetQueryResults`
- `pricing:GetProducts`

```

2. Deploy as a container using AWS App Runner / ECS for the Flask backend.
