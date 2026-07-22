"""The connector catalog. Each entry renders a credential-wired node.

Request shapes are the verified mid-2026 API specs; secrets are always
secret://NAME so the missing-secret flow prompts for them.
"""

from __future__ import annotations

from typing import Any

from .registry import Connector, ConnectorResult, Field, SecretNeed, register

HELPERS = "graphx.connectors.helpers"


def _api(node_id: str, **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "api", **config}


# ------------------------------------------------------------- messaging

def _slack(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST", url="secret://slack_webhook_url",
                  json_body={"text": v["message"]}),
        secret_names=["slack_webhook_url"])


register(Connector(
    key="slack", category="messaging", title="Slack (incoming webhook)",
    description="Post a message to a Slack channel via an incoming webhook.",
    kind="http", render_fn=_slack,
    secrets=(SecretNeed("slack_webhook_url",
                        "Slack > your app > Incoming Webhooks (the whole URL)"),),
    fields=(Field("message", "message text"),)))


def _discord(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST", url="secret://discord_webhook_url",
                  json_body={"content": v["message"]}),
        secret_names=["discord_webhook_url"])


register(Connector(
    key="discord", category="messaging", title="Discord (webhook)",
    description="Post a message to a Discord channel via a webhook.",
    kind="http", render_fn=_discord,
    secrets=(SecretNeed("discord_webhook_url",
                        "Channel > Edit > Integrations > Webhooks (the whole URL)"),),
    fields=(Field("message", "message text (<=2000 chars)"),)))


def _telegram(node_id, v):
    # token lives in the URL path with a literal 'bot' prefix
    return ConnectorResult(
        node=_api(node_id, method="POST",
                  url="https://api.telegram.org/botsecret://telegram_token/sendMessage",
                  json_body={"chat_id": v["chat_id"], "text": v["message"]}),
        secret_names=["telegram_token"])


register(Connector(
    key="telegram", category="messaging", title="Telegram (bot sendMessage)",
    description="Send a message from a Telegram bot to a chat.",
    kind="http", render_fn=_telegram,
    secrets=(SecretNeed("telegram_token", "@BotFather > your bot token"),),
    fields=(Field("chat_id", "numeric chat id or @channelusername"),
            Field("message", "message text"))))


def _webhook(node_id, v):
    node = _api(node_id, method=v.get("method", "POST"), url=v["url"],
                json_body={"text": v["message"]} if v.get("message") else None)
    secrets: list[str] = []
    if v.get("auth"):
        node["headers"] = {"Authorization": "Bearer secret://webhook_token"}
        secrets.append("webhook_token")
    return ConnectorResult(node=node, secret_names=secrets)


register(Connector(
    key="webhook", category="messaging", title="Generic HTTP webhook",
    description="POST a JSON message to any URL, optionally with a bearer token.",
    kind="http", render_fn=_webhook,
    secrets=(SecretNeed("webhook_token", "optional — only if you set auth=true"),),
    fields=(Field("url", "https://example.com/hook"),
            Field("message", "message text", required=False),
            Field("method", "HTTP method", required=False, default="POST"),
            Field("auth", "true to send a bearer token", required=False, default=False))))


# ----------------------------------------------------------------- email

def _sendgrid(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST",
                  url="https://api.sendgrid.com/v3/mail/send",
                  headers={"Authorization": "Bearer secret://sendgrid_key"},
                  json_body={
                      "personalizations": [{"to": [{"email": v["to"]}]}],
                      "from": {"email": v["from_email"]},
                      "subject": v["subject"],
                      "content": [{"type": "text/plain", "value": v["body"]}],
                  }),
        secret_names=["sendgrid_key"])


register(Connector(
    key="sendgrid", category="email", title="SendGrid (send email)",
    description="Send a plaintext email via the SendGrid v3 API.",
    kind="http", render_fn=_sendgrid,
    secrets=(SecretNeed("sendgrid_key", "SendGrid > Settings > API Keys (SG.…)"),),
    fields=(Field("to", "recipient@example.com"),
            Field("from_email", "verified-sender@yourdomain.com"),
            Field("subject", "subject line"),
            Field("body", "email body"))))


def _smtp(node_id, v):
    return ConnectorResult(
        node={"id": node_id, "type": "function",
              "handler": f"{HELPERS}:smtp_send",
              "args": {
                  "host": v["host"], "port": v.get("port", 587),
                  "username": v["username"], "password": "secret://smtp_password",
                  "from_addr": v["from_addr"], "to_addr": v["to_addr"],
                  "subject": v["subject"], "body": v["body"]}},
        secret_names=["smtp_password"])


register(Connector(
    key="smtp", category="email", title="SMTP (send email)",
    description="Send email through any SMTP server (stdlib, no extra deps).",
    kind="function", render_fn=_smtp,
    secrets=(SecretNeed("smtp_password", "your SMTP/app password"),),
    fields=(Field("host", "smtp.example.com"),
            Field("port", "587 (STARTTLS) or 465 (SSL)", required=False, default=587),
            Field("username", "smtp username"),
            Field("from_addr", "sender@example.com"),
            Field("to_addr", "recipient@example.com"),
            Field("subject", "subject line"),
            Field("body", "email body"))))


def _gmail(node_id, v):
    return ConnectorResult(
        node={"id": node_id, "type": "mcp", "server": "gmail",
              "tool": v.get("tool", "search_emails"),
              "args": {"query": v.get("query", "is:unread")}},
        secret_names=[],
        mcp_servers={"gmail": {
            "transport": "stdio",
            "command": ["npx", "-y", "@gongrzhe/server-gmail-autoauth-mcp"]}},
        note=("Gmail uses file-based OAuth, not a graphx secret. One-time setup: "
              "place your Google OAuth client at ~/.gmail-mcp/gcp-oauth.keys.json, "
              "then run: npx @gongrzhe/server-gmail-autoauth-mcp auth"))


register(Connector(
    key="gmail", category="email", title="Gmail (via MCP server)",
    description="Read/search/draft Gmail through the GongRzhe Gmail MCP server.",
    kind="mcp", render_fn=_gmail,
    fields=(Field("tool", "MCP tool (search_emails/send_email/…)", required=False,
                  default="search_emails"),
            Field("query", "Gmail search query", required=False, default="is:unread"))))


# ------------------------------------------------------------------- dev

_GITHUB_HEADERS = {
    "Authorization": "Bearer secret://github_token",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "graphx",
}


def _github_issue(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST",
                  url=f"https://api.github.com/repos/{v['owner']}/{v['repo']}/issues",
                  headers=dict(_GITHUB_HEADERS),
                  json_body={"title": v["title"], "body": v.get("body", "")}),
        secret_names=["github_token"])


register(Connector(
    key="github_issue", category="dev", title="GitHub (create issue)",
    description="Open an issue on a GitHub repository.",
    kind="http", render_fn=_github_issue,
    secrets=(SecretNeed("github_token",
                        "GitHub PAT with Issues: read/write on the repo"),),
    fields=(Field("owner", "org-or-user"), Field("repo", "repo-name"),
            Field("title", "issue title"),
            Field("body", "issue body", required=False, default=""))))


def _github_comment(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST",
                  url=f"https://api.github.com/repos/{v['owner']}/{v['repo']}"
                      f"/issues/{v['issue_number']}/comments",
                  headers=dict(_GITHUB_HEADERS),
                  json_body={"body": v["comment"]}),
        secret_names=["github_token"])


register(Connector(
    key="github_comment", category="dev", title="GitHub (comment on issue/PR)",
    description="Add a comment to a GitHub issue or pull request.",
    kind="http", render_fn=_github_comment,
    secrets=(SecretNeed("github_token", "GitHub PAT with Issues: read/write"),),
    fields=(Field("owner", "org-or-user"), Field("repo", "repo-name"),
            Field("issue_number", "issue or PR number"),
            Field("comment", "comment text"))))


def _gitlab_issue(node_id, v):
    return ConnectorResult(
        node=_api(node_id, method="POST",
                  url=f"https://gitlab.com/api/v4/projects/{v['project']}/issues",
                  headers={"PRIVATE-TOKEN": "secret://gitlab_token"},
                  json_body={"title": v["title"], "description": v.get("body", "")}),
        secret_names=["gitlab_token"])


register(Connector(
    key="gitlab_issue", category="dev", title="GitLab (create issue)",
    description="Open an issue on a GitLab project.",
    kind="http", render_fn=_gitlab_issue,
    secrets=(SecretNeed("gitlab_token", "GitLab PAT with 'api' scope (glpat-…)"),),
    fields=(Field("project", "numeric project id or URL-encoded path"),
            Field("title", "issue title"),
            Field("body", "issue description", required=False, default=""))))


# --------------------------------------------------------- data / storage

def _postgres(node_id, v):
    return ConnectorResult(
        node={"id": node_id, "type": "function",
              "handler": f"{HELPERS}:pg_query",
              "args": {"dsn": "secret://pg_dsn", "query": v["query"]}},
        secret_names=["pg_dsn"])


register(Connector(
    key="postgres_query", category="data", title="Postgres (run query)",
    description="Run a SQL query and return rows.",
    kind="function", render_fn=_postgres, extra="postgres",
    secrets=(SecretNeed("pg_dsn",
                        "postgresql://user:pass@host:5432/db (whole DSN)"),),
    fields=(Field("query", "SELECT ..."),)))


def _s3_put(node_id, v):
    return ConnectorResult(
        node={"id": node_id, "type": "function",
              "handler": f"{HELPERS}:s3_put",
              "args": {"bucket": v["bucket"], "key": v["key"],
                       "body": v.get("body", ""),
                       "access_key_id": "secret://aws_access_key_id",
                       "secret_access_key": "secret://aws_secret_key",
                       "region": v.get("region")}},
        secret_names=["aws_access_key_id", "aws_secret_key"])


register(Connector(
    key="s3_put", category="data", title="S3 (upload object)",
    description="Upload an object to an S3 bucket.",
    kind="function", render_fn=_s3_put, extra="s3",
    secrets=(SecretNeed("aws_access_key_id", "AWS access key id"),
             SecretNeed("aws_secret_key", "AWS secret access key")),
    fields=(Field("bucket", "my-bucket"), Field("key", "path/to/object"),
            Field("body", "content", required=False, default=""),
            Field("region", "us-east-1", required=False))))
