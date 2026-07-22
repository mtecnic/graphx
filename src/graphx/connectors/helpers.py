"""Runtime helpers for function-based connectors (SMTP / Postgres / S3).

Third-party deps are imported lazily so importing this module never
requires psycopg/boto3 — only running the corresponding connector does.
Secret values arrive already resolved (the function node resolves
secret:// in its args before calling).
"""

from __future__ import annotations

from typing import Any


def smtp_send(host: str, port: int, username: str, password: str,
              from_addr: str, to_addr: str, subject: str, body: str,
              use_tls: bool = True) -> dict[str, Any]:
    """Send a plaintext email via SMTP (stdlib, STARTTLS on 587)."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as server:
            if use_tls:
                server.starttls()
            server.login(username, password)
            server.send_message(msg)
    return {"sent": True, "to": to_addr}


def pg_query(dsn: str, query: str, params: list | None = None) -> dict[str, Any]:
    """Run a query and return rows (needs the [postgres] extra: psycopg v3)."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "postgres connector needs the [postgres] extra: "
            "pip install 'graphx[postgres]'") from exc

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(query, params or None)
        columns = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
    return {"columns": columns,
            "rows": [dict(zip(columns, r)) for r in rows],
            "count": len(rows)}


def s3_put(bucket: str, key: str, body: Any,
           access_key_id: str | None = None, secret_access_key: str | None = None,
           region: str | None = None) -> dict[str, Any]:
    """Upload an object to S3 (needs the [s3] extra: boto3)."""
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "s3 connector needs the [s3] extra: pip install 'graphx[s3]'") from exc

    kwargs: dict[str, Any] = {}
    if access_key_id and secret_access_key:
        kwargs.update(aws_access_key_id=access_key_id,
                      aws_secret_access_key=secret_access_key)
    if region:
        kwargs["region_name"] = region
    client = boto3.client("s3", **kwargs)
    data = body.encode() if isinstance(body, str) else body
    client.put_object(Bucket=bucket, Key=key, Body=data)
    return {"uploaded": True, "bucket": bucket, "key": key}
