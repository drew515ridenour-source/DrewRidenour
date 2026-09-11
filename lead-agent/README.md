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

Everything is stored in `leads.xlsx`, one row per business, with a `status`
column that advances as it moves through the pipeline:

`new` -> `phone_only` / `email_found` -> `drafted` -> `ready_to_send` / `skipped` -> `sent` / `send_failed`

`leads.xlsx` is a real Excel workbook (via `openpyxl`) -- open it directly to
sort, filter, or eyeball your leads. It's created automatically the first
time you run `find_leads.py` (bold header row, frozen so it stays visible
while you scroll, auto-widened columns), and it is **not** committed to git
-- it's generated locally and will contain real business contact info once
you start running the pipeline.

`find_leads.py` and `find_emails.py` write to `leads.xlsx` **as they go**
(one row at a time), not just at the end -- so if a long search run gets
interrupted partway through, the leads already found are already saved.

Every send is also logged to `send_log.csv` (a plain append-only CSV log,
not a working dataset, so it stays CSV).

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

### Search area (required, defaults to Iowa City)
`find_leads.py` searches a circle defined by `SEARCH_CENTER_LAT` /
`SEARCH_CENTER_LNG` / `SEARCH_RADIUS_METERS` in `.env` -- change these to
move the search to a new city or resize the radius, no code changes needed.
- To find lat/lng for a new city: search "`[city name] coordinates`" on
  Google, or use [latlong.net](https://www.latlong.net).
- Radius is in **meters**: `8000` ≈ 5 miles, `16000` ≈ 10 miles.
- `SEARCH_LOCATION_MODE` controls how strict the radius is:
  - `bias` (default) -- a soft preference; Google may still return a
    strong match just outside the circle.
  - `restriction` -- a hard cutoff; anything outside the circle is
    excluded entirely.
- To change *what* categories are searched (not just where), edit the
  `CATEGORIES` list in `config.py`.

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
python find_leads.py       # 1. discover leads with no website -> leads.xlsx
python find_emails.py      # 2. try to find a public email for each lead
python generate_emails.py  # 3. draft a personalized outreach email per lead
python review_emails.py    # 4. YOU approve / edit / skip each draft
python send_emails.py      # 5. send only approved (ready_to_send) leads
```

Edit `CATEGORIES` in `config.py` to change what kinds of businesses are
searched (defaults to auto repair, hair salon, tire shop, dentist, and
restaurant). Edit `SEARCH_CENTER_LAT` / `SEARCH_CENTER_LNG` /
`SEARCH_RADIUS_METERS` in `.env` to change *where* -- see "Search area"
above.

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
