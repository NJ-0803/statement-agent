# Deploying this

Two different things you might want to host, with very different risk. Pick the right one.

| | **Demo** | **Owner** |
| --- | --- | --- |
| `STATEMENT_AGENT_MODE` | `demo` | `owner` |
| Who can open it | anyone with the link | you, with a passphrase |
| What is in it | a throwaway copy of `dataset_public/` (synthetic) per visitor | your real ledger |
| Sign-in | none — it would only add friction | required, every route |
| If it leaks | made-up statements leak | **your bank statements leak** |

The demo is what a public link should point at. Owner mode is for reaching your own ledger from your
phone, and it should not be a link you post anywhere.

## Before anything else

- **Your real `ledger.db` never goes near a host.** It is gitignored, it is not in the image, and a
  demo instance cannot open one even if configured to — `statement_agent/web/demo.py` derives the
  path from the visitor's session and refuses anything outside the demo area.
- **HTTPS is not optional.** Session cookies are marked `Secure` off localhost, so a plain-HTTP
  deployment will silently fail to keep anyone signed in. Every host below terminates TLS for you.
- **Encryption at rest is still not built.** On a host, the ledger and uploaded documents are plain
  files on that host's disk. For the demo that is synthetic data and does not matter. For owner mode
  it means the host operator can read your statements — weigh that before choosing owner mode.

## Demo, on any container host

```sh
docker build -t statement-agent .
docker run -p 8080:8080 \
  -e STATEMENT_AGENT_MODE=demo \
  -e STATEMENT_AGENT_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e ANTHROPIC_API_KEY=...  # optional; see the cost note below
  statement-agent
```

The image builds the synthetic seed at boot and serves with gunicorn on `$PORT` (8080 by default).
`/healthz` is the health check and says nothing about the data.

Environment:

| Variable | Default | What it does |
| --- | --- | --- |
| `STATEMENT_AGENT_MODE` | `local` | `local`, `owner` or `demo`. Anything unrecognised falls back to `owner`, which is the safe direction to fail. |
| `STATEMENT_AGENT_SECRET_KEY` | generated | Signs session cookies. **Set it**, or every visitor is reset on each restart and each worker disagrees. Needs 32+ characters. |
| `STATEMENT_AGENT_DEMO_ROOT` | `/data/demo` in the image | Where visitor sandboxes live. |
| `STATEMENT_AGENT_DEMO_TTL` | `3600` | Seconds before an idle visitor's sandbox — and anything they uploaded — is deleted. |
| `STATEMENT_AGENT_DEMO_ASK_BUDGET` | `200` | Questions the whole instance will answer per day. See below. |
| `STATEMENT_AGENT_PASSPHRASE_HASH` | — | Owner mode, for hosts with no disk to keep a credentials file on. Printed by `set-passphrase`. |
| `ANTHROPIC_API_KEY` | — | Questions and scanned-page OCR. Without it the rest still works. |
| `GROQ_API_KEY` | — | Optional merchant categorisation. |

### The cost note, which matters more than it looks

Every question a visitor asks spends **your** Anthropic credit. A link that does well and a link
being abused look identical until the invoice arrives, and a per-IP rate limit does nothing against
a thousand different IPs. So the whole instance shares one daily allowance
(`STATEMENT_AGENT_DEMO_ASK_BUDGET`, 200/day), counted on disk so workers share it. When it runs out,
questions say so politely and everything else — reading statements, categories, links, CSV export —
keeps working.

If you would rather spend nothing: **don't set `ANTHROPIC_API_KEY`**. The demo still shows the
parsing, the review gate, categories, links and reconciliation, which is most of what is interesting
anyway. Scanned-page OCR and the question box are what you lose.

Set a spend limit in the Anthropic console as well. Treat the app-level cap as the first line, not
the only one.

## Owner mode

```sh
python -m statement_agent.cli set-passphrase       # 12 characters minimum, stored as an scrypt hash
STATEMENT_AGENT_MODE=owner python -m statement_agent.cli serve --port 8080
```

It refuses to start without a passphrase. Put it behind HTTPS.

The cheapest private option, and the one that keeps your statements on your own disk, is a tunnel to
your own machine (Cloudflare Tunnel or Tailscale) rather than a host — free, and nothing sensitive
is copied anywhere. The trade-off is that it is only up while your machine is.

## What is still not built

Listed here so nobody discovers it in production:

- Encryption at rest for the ledger and uploaded documents.
- Multiple accounts. Owner mode is one passphrase for one person; there is no per-user separation,
  so do not give the passphrase to someone you do not want reading everything.
- Durable background jobs. An import interrupted by a restart is marked failed and can be retried,
  but it is not resumed.
- Monitoring and alerting beyond `/healthz`.
- Automated backups. `cli backup --to file.db` is manual.

See `NOT_IMPLEMENTED.md` for the full list.
