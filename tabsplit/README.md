# The Tab

A small web app for splitting money in a group. Log what everyone paid, log what
everyone is bringing, and get the shortest list of payments that squares everybody up.

Built for a group of 15, works for any size.

## What it does

- **Crew** — everyone's name, pasted in one go.
- **Spending** — who paid, how much, for what, and which people it splits between.
  A taxi only four people took splits four ways, not fifteen.
- **Bringing** — a shared packing list: who carries the ice, the speaker, the chairs.
  If a thing cost money, tick "count that cost in the money split" and it joins the maths.
- **Settle up** — every balance, plus the minimum set of payments to clear them.

Everyone opens the same link (or types the 6-character code), so it stays in sync
across all fifteen phones.

## Run it locally

```bash
pip install -r requirements.txt
python app.py           # http://localhost:5000
```

Data goes to `tabsplit.db` next to `app.py`.

## Deploy on Render

**Blueprint (easiest).** Push this folder to a GitHub repo, then in Render:
New → Blueprint → pick the repo. `render.yaml` creates the web service and a
Postgres database and wires `DATABASE_URL` between them. Hit Apply.

**Manual.** New → Web Service → connect the repo:

| Setting | Value |
| --- | --- |
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60` |
| Health check path | `/healthz` |

Then New → Postgres, and copy its Internal Database URL into the web service as an
environment variable named `DATABASE_URL`.

## About storage

The app reads `DATABASE_URL` if it is set and falls back to SQLite otherwise.

On Render's free plan a web service has no persistent disk, so **SQLite data is
wiped on every restart and deploy**. Use Postgres. Render's free Postgres instance
expires after a fixed trial window — fine for one outing, but for something you keep
around, either move to a paid database or attach a paid disk and set
`SQLITE_PATH=/var/data/tabsplit.db`.

Free web services also sleep after inactivity, so the first visit of the day takes
about a minute to wake up.

## Environment variables

| Name | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. Preferred. |
| `SQLITE_PATH` | Where to put the SQLite file when there is no `DATABASE_URL`. |
| `PORT` | Set by Render automatically. |

## API

| Method | Path |
| --- | --- |
| POST | `/api/groups` |
| GET · PATCH | `/api/groups/<code>` |
| POST | `/api/groups/<code>/members` |
| DELETE | `/api/groups/<code>/members/<id>` |
| POST | `/api/groups/<code>/expenses` |
| DELETE | `/api/groups/<code>/expenses/<id>` |
| POST | `/api/groups/<code>/items` |
| PATCH · DELETE | `/api/groups/<code>/items/<id>` |

Every response returns the whole group state, so the page never needs a second request.

## How the split is worked out

Money is held in integer cents, never floats. Each expense divides by the number of
people sharing it; leftover cents are handed out one at a time so the shares always
add back up to the exact total. A person's balance is what they paid minus what they
owe. Settling is a greedy match of the biggest debtor to the biggest creditor, which
gives at most one payment fewer than there are people, and usually far fewer.
