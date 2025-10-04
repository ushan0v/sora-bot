import os

try:
    # Load variables from a local .env file if present
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    # dotenv is optional at runtime; env vars can be set by the host
    pass

# Read sensitive settings from environment
# BOT_TOKEN must be set in environment or .env
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# PROXY_URL is optional; leave empty to disable proxy
PROXY_URL = os.getenv("PROXY_URL", "")

ADMIN_ID = os.getenv("ADMIN_ID", "")