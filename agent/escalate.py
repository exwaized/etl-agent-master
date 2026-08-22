import json
import os
import urllib.request


SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def alert(
    failure_id: int,
    step: str,
    error: str,
    attempts: int,
    webhook_url: str = SLACK_WEBHOOK_URL,
) -> bool:
    if not webhook_url:
        raise ValueError("SLACK_WEBHOOK_URL is not set")

    payload = json.dumps(
        {
            "text": (
                f":red_circle: *Pipeline failure* (attempt {attempts})\n"
                f"*Failure ID:* `{failure_id}`\n"
                f"*Step:* `{step}`\n"
                f"*Error:* {error}"
            )
        }
    ).encode()

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False
