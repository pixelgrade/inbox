# Inbox

A self-hosted inbox for a company’s support team and its agents. An operator can turn a customer question into an investigation, choose which product context to share, assign it to a model, review a cited answer and save a draft. The included Field Journal pack is a fictional example you can replace with your own documentation, voice and support practices.

Inbox is the desk half of a pair. Supa, the agent half, will follow in a separate repository. Pixelgrade provides Inbox as is and offers paid setup on request.

Mail transport currently requires AWS SES for sending and receiving mail, S3 for incoming messages, and SQS for delivery notifications and intake events. SMTP and IMAP are not implemented. You can run the desk and work with local drafts before connecting a mail provider.

## Start an inbox

On Linux with Python 3.12 and SQLite FTS5, clone this repository into a new directory. Run the following there. For a production installation, configure an HTTPS reverse proxy for your own origin and run the server under a dedicated service account with persistent private storage. The example uses a local listener.

```sh
umask 077
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
mkdir -p private
export INBOX_DB="$PWD/private/inbox.db"
export INBOX_BASE_URL=http://127.0.0.1:8926
export INBOX_COMPANY_NAME='Woodland Press'
export INBOX_SUPPORT_ADDRESS=support@woodland.example
export INBOX_INTAKE_MAILBOX=1
export INBOX_AGENT_ID=1
export INBOX_AGENT_NAME='Woodland Support'
export INBOX_INITIAL_AUTHORITY=standby
export INBOX_OPERATOR_WRITES=1
python -m workbench.init_mailbox examples/journal.json
python -m workbench.admin token journal agent "$PWD/private/agent.token"
python writer_control.py live --from standby --native-company "$INBOX_COMPANY_NAME"
python -m uvicorn app:app --host 127.0.0.1 --port 8926
```

The initializer requires a new database. Preserve this environment for subsequent server starts. For standard browser sign-in, run `python -m operator_account team@your-company.example` in a second shell with the same environment. Enter the password at the private prompts. Each person using the shared account receives an independent browser session. Re-running the command rotates the password and signs out existing sessions. The host owner can also run `python operator_ui.py` for a five-minute, single-use recovery code. Open **Investigations** for product cases. On an existing conversation, choose **Investigate this case** to share its question. The example supports private drafts. Customer delivery requires a verified sender, incoming and feedback routes, outbound workers and your own mail settings.

For a service installation, adapt `examples/inbox.service` and `examples/inbox.nginx.conf`. Set the service account, code directory, TLS paths and hostname for your installation. Store the variables above in the private service environment file with absolute paths, set `INBOX_DB=/var/lib/inbox/inbox.db`, and set `INBOX_BASE_URL` to the external HTTPS origin. Secure that file and database for the service account.

Set `INBOX_SOURCE_URL` to the corresponding source for the version you serve, including your modifications. It appears on the sign-in page and in the desk footer. Optional mailbox integrations register when the application imports the modules listed in `INBOX_EXTENSIONS`, a comma-separated list. Leave it unset for the standard desk.

## Review, insights and intake checks

**Review** lists every open conversation with an unsent draft beside the message it answers, so a person can send, edit or discard what agents prepared. **Insights** reports volume, response times, how replies were sent, topics, customers and ratings from the mailbox's own data. An agent holding the gateway credential can read one conversation as bounded plain text with `GET /v2/conversations/ID/brief`, find earlier answers to similar questions with `GET /v2/precedents`, and read the same figures with `GET /v2/insights`.

Each new incoming email is checked before it is acknowledged. The check reads the original mail headers, the message shape and the sender's history, and records a verdict with its evidence on **Intake checks**: support, promotion, website-generated mail, delivery failure, automatic reply, or possibly meant for another business. `INBOX_TRIAGE_MODE=observe` (the default) only records. `act` files confident machine mail as closed without an acknowledgment and tags a possible wrong address for a person. `off` disables the check. A message is filed only when the sending side and the content agree, and never from a sender you answered before. Set `INBOX_PRODUCT_WORDS` to your product names, separated by spaces, and `INBOX_OWN_MAIL_DOMAINS` to any further domains whose website mail is your own.

## Give agents product context

Each installation has one company and can have multiple products. Edit a copy of `examples/journal.json` with your product ID and documents. Each document includes a stable key, title, body in `text`, source, revision and a kind of `guide`, `voice`, `practice` or `code`. Install a revised pack with `python -m workbench.admin install PACK.json`. Each agent token is scoped to its product.

```sh
export SUPPORT_PRODUCT_CONFIG="$PWD/private/agent.json"
python - <<'PY'
import json, os
from pathlib import Path
Path(os.environ["SUPPORT_PRODUCT_CONFIG"]).write_text(json.dumps({
    "origin": os.environ["INBOX_BASE_URL"], "product": "journal",
    "token_file": str(Path("private/agent.token").resolve())
}))
PY
python -m workbench.cli context "mobile sync"
python -m workbench.cli open "How can I tell whether my field entry is backed up?" --key first-question
```

To assign investigations to a model, add a `chat` object to the private agent configuration with `endpoint` (a chat-completions URL), `model`, `display_name` and, when required, `key_file`. Run `python -m workbench.investigate "$SUPPORT_PRODUCT_CONFIG" --worker`. The operator sees the worker’s display name when it is available. Assignment shares the case question, selected product sources and test receipts with that model. The model prepares a private proposal for review.

## Test changes in a worktree

Register the repository in the product pack with a `key`, `label`, credential-free HTTPS `url`, full Git commit in `revision`, and an array of named recipes with `key` and `label`. On a host with Git and Docker, provide a local bare mirror containing that commit. Select the source paths agents can read.

```sh
python -m workbench.admin index-code PRODUCT REPO /absolute/path/to/mirror.git src tests
python -m workbench.admin token PRODUCT runner /absolute/path/to/private/runner.token
```

In a separate private runner configuration, set `origin`, `product`, `token_file` and `work_directory`. Under `repositories.REPO`, set `bare_repository`, the same `revision`, and `recipes.RECIPE` with the locally installed Docker image ID in `image`, a command argument array in `argv`, and a timeout in seconds in `timeout`. Run `python -m workbench.runner /absolute/path/to/runner.json`. Each job runs the configured test in a disposable, network-isolated worktree container. The case records the commit, patch digest, output and exit status. The operator can download the tested Git diff from the case for development review.

A mailbox imported with more than 20,000 conversations builds its tag and sender indexes on request: run `python store.py indexes` once, at a quiet moment. Smaller and new mailboxes build them at start-up.

Back up the company database using SQLite’s online backup API, together with your product packs, repository revisions, private configuration, credentials and any separately configured mail or binary storage.
