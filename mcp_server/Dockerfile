# AgentCore Runtime requires linux/arm64 container images.
FROM public.ecr.aws/docker/library/python:3.12-slim

# AgentCore Runtime routes MCP traffic to 0.0.0.0:8000/mcp.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# boto3 is the only runtime dependency; the MCP transport uses the stdlib.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py kb_client.py engines.py mcp_content.py usage_log.py agentcore_app.py ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["python", "agentcore_app.py"]
