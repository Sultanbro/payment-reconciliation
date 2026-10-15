"""Generate local configuration without overwriting an existing key."""

import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".env"
try:
    with path.open("x") as output:
        path.chmod(0o600)
        output.write("API_KEY=" + secrets.token_hex(32) + "\n")
    print("Created .env with a random local API key.")
except FileExistsError:
    print(".env already exists; keeping it unchanged.")
