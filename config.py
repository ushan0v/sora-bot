import os
import json

from dotenv import load_dotenv 

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PROXY_URL = os.getenv("PROXY_URL", "")

COOKIES = json.loads(open("cookies.json", "r", encoding="utf-8").read())