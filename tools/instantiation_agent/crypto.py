from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sign_payload(payload: Any, key: str | None = None) -> str:
    secret = (key or os.environ.get("INSTANTIATION_AGENT_SIGNING_KEY") or "dev-only-agent-signing-key").encode()
    digest = hmac.new(secret, canonical_json(payload).encode(), hashlib.sha256).digest()
    return "hmac-sha256:" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def synthetic_did(namespace: str, subject: str) -> str:
    return f"did:nfh:{namespace}:{hashlib.sha256(subject.encode()).hexdigest()[:24]}"

