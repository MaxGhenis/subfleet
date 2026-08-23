"""subfleet — cross-account usage/quota + auth-stability monitor.

Covers multiple Codex CODEX_HOME lanes (distinct ChatGPT accounts) and Claude
Code subscription lanes. It makes invisible quota and authentication state
observable before a session loses work.

Design rules:
- Server responses are ground truth; gauges and local expiry claims lie.
- Never fabricate a number: anything unreachable is "unknown" plus the last
  observed error, clearly labeled with its observation time.
- Never writes auth.json: a narrowly gated repair lets the vendor CLI refresh
  its own expired token, while revoked credentials always require re-login.
"""

__version__ = "0.1.0"
