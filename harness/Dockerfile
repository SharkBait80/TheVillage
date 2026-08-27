# Melbourne Agent Village — Agent Harness (Bedrock AgentCore Runtime container).
# AgentCore Runtime containers MUST be linux/arm64 and expose port 8080 with
# GET /ping + POST /invocations (see .orchestrator/research/agentcore.md §1a).
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_REGION=ap-southeast-2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir starlette uvicorn boto3

COPY harness.py .

EXPOSE 8080

# Non-root runtime user.
RUN useradd -m -u 10001 harness && chown -R harness:harness /app
USER harness

CMD ["uvicorn", "harness:app", "--host", "0.0.0.0", "--port", "8080"]
