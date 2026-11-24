"""Company-level settings for a single self-hosted mailbox process."""

import os
from urllib.parse import urlsplit

NAME = os.getenv("INBOX_COMPANY_NAME", "Woodland")
ADDRESS = os.getenv("INBOX_SUPPORT_ADDRESS", "hello@woodland.example").lower()
DOMAIN = ADDRESS.rsplit("@", 1)[-1]
RETURN_PATH = os.getenv("INBOX_RETURN_ADDRESS", "inbox@intake." + DOMAIN).lower()
GROUP_ID = os.getenv("INBOX_GROUP_LIST_ID", ADDRESS.replace("@", "."))
GROUP_DOMAIN = os.getenv("INBOX_GROUP_DKIM_DOMAIN", DOMAIN).lower()
MAILBOX = int(os.getenv("INBOX_INTAKE_MAILBOX", "1"))
HOME = os.getenv("INBOX_COMPANY_WEBSITE", "https://" + DOMAIN + "/")
DOCS = os.getenv("INBOX_DOCUMENTATION_URL", HOME.rstrip("/") + "/docs/")
SOURCE_URL = os.getenv("INBOX_SOURCE_URL", "https://github.com/pixelgrade/inbox")
IGNORE_TAG = os.getenv("INBOX_AGENT_IGNORE_TAG", "agent-ignore").strip().lower()
HELD_TAG = os.getenv("INBOX_AGENT_HELD_TAG", "agent-wrong-address-held").strip().lower()
AUTOMATION_NAME = os.getenv("INBOX_AUTOMATION_NAME", "the agent")
if any(urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc for url in (HOME, DOCS, SOURCE_URL)):
    raise ValueError("Company and source URLs must use HTTP or HTTPS")


LOGIN_COMMAND = os.getenv(
    "INBOX_LOGIN_COMMAND",
    "python operator_ui.py",
)


def agent_name():
    return os.getenv("INBOX_AGENT_NAME", "Support")


def message_id(cid, tid):
    return f"<inbox-{cid}-{tid}@{DOMAIN}>"


def template():
    return {
        "company_name": NAME,
        "support_address": ADDRESS,
        "company_home": HOME,
        "company_docs": DOCS,
        "login_command": LOGIN_COMMAND,
        "source_url": SOURCE_URL,
        "agent_name": agent_name(),
    }
