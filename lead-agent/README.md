# Local Business Lead-Gen & Outreach Agent

A small Python CLI pipeline for a web design agency (starting in Iowa City,
IA) to find local businesses without a website, find a contact email where
one is publicly listed, draft a personalized outreach email with Claude,
review it by hand, and send it -- rate-limited and CAN-SPAM compliant.

## Pipeline

```
find_leads.py  -->  find_emails.py  -->  generate_emails.py  -->  review_emails.py  -->  send_emails.py
 (Google Places)     (Facebook Graph)     (Anthropic API)         (manual approval)      (SMTP/SendGrid/Mailgun)
```

Everything is stored in `leads.csv`, one row per business, with a `status`
column that advances as it moves through the pipeline:

`new` -> `phone_only` / `email_found` -> `drafted` -> `ready_to_send` / `skipped` -> `sent` / `send_failed`

Every send is also logged to `send_log.csv`.

## 1. Setup

```bash
cd lead-agent
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env`:

### Google Places API key (required)
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create (or select) a project, then enable **Places API (New)**.
3. Go to **APIs & Services -> Credentials**, create an **API key**.
4. Restrict the key to the Places API (recommended).
5. Put it in `.env` as `GOOGLE_PLACES_API_KEY`.
6. Note: Places API usage is billed by Google after a monthly free credit --
   check current pricing before running large searches.

### Anthropic API key (required)
1. Create an account at [console.anthropic.com](https://console.anthropic.com/).
2. Create an API key under **API Keys**.
3. Put it in `.env` as `ANTHROPIC_API_KEY`.

### Facebook access token (optional)
Only needed if you want automatic email discovery via the Facebook Graph
API. Without it, `find_emails.py` simply marks every lead `phone_only` and
you follow up by phone.
1. Create an app at [developers.facebook.com](https://developers.facebook.com/).
2. Generate an access token (a basic App Access Token works for public Page
   Search; a User/Page token with `pages_read_engagement` gets you more).
3. Put it in `.env` as `FACEBOOK_ACCESS_TOKEN`.

### Sending email: Gmail App Password (default, `SEND_PROVIDER=smtp`)
1. Turn on 2-Step Verification on the Gmail account you'll send from.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   and generate an App Password for "Mail".
3. In `.env` set:
   - `SMTP_HOST=smtp.gmail.com`
   - `SMTP_PORT=587`
   - `SMTP_USER=` your Gmail address
   - `SMTP_PASS=` the 16-character App Password (not your normal password)

### Sending email: SendGrid or Mailgun (alternative)
Set `SEND_PROVIDER=sendgrid` or `SEND_PROVIDER=mailgun` in `.env` and fill
in `SENDGRID_API_KEY`, or `MAILGUN_API_KEY` + `MAILGUN_DOMAIN`.

### Required for every provider
`FROM_NAME`, `FROM_EMAIL`, `REPLY_TO`, and `MAILING_ADDRESS` (a real
physical address -- required by the CAN-SPAM Act and included in every
email's footer automatically).

## 2. Run the pipeline, in order

```bash
python find_leads.py       # 1. discover leads with no website -> leads.csv
python find_emails.py      # 2. try to find a public email for each lead
python generate_emails.py  # 3. draft a personalized outreach email per lead
python review_emails.py    # 4. YOU approve / edit / skip each draft
python send_emails.py      # 5. send only approved (ready_to_send) leads
```

Edit the `SEARCHES` list at the top of `find_leads.py` to change which
`(category, city)` pairs to search -- it defaults to `auto repair` and
`hair salon` in Iowa City, IA.

Each script is safe to re-run: leads already past a given stage are left
alone (e.g. re-running `find_leads.py` won't duplicate existing rows, and
`generate_emails.py` won't re-draft an email that's already `drafted` or
later).

## 3. Compliance notes (also in code comments)

- **CAN-SPAM**: every sent email includes a real physical mailing address
  and a clear opt-out line in the footer. This isn't optional/configurable
  -- it's built into `send_emails.py`'s footer, always appended.
- **Rate limiting**: at most `DAILY_SEND_LIMIT` (default 25) emails per
  day, with a random 60-120 second delay between sends, to protect the
  sending domain's reputation.
- **Official APIs only**: lead discovery uses Google's Places API (New)
  Text Search + Place Details endpoints; email discovery uses the Facebook
  Graph API. Neither script scrapes Google's or Facebook's HTML directly.
- Subject lines are honest -- no misleading claims or fake reply threads.

## Error handling

Every script checks for its required `.env` values up front (via
`common.require_env`) and exits with a clear message instead of a stack
trace if something's missing. API and network errors are caught per-lead
so one failure doesn't stop the whole batch; failures are printed to
stderr (and, for sends, logged to `send_log.csv` with status `failed`).
