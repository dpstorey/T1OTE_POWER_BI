# Tenable OT to Power BI export (reference application)

A small, readable Python program that pulls OT assets from **Tenable One OT
Exposure / Enterprise Manager** and loads them into a **Power BI** dataset, so
they can be charted and filtered alongside the rest of your reporting.

It is a *reference implementation*: meant to be run as-is for evaluation, and
read, copied and adapted for your own pipeline. Everything lives in one
commented file, [`t1ote_export.py`](t1ote_export.py).

```text
 Tenable EM  --GraphQL-->  fetch assets  -->  flat rows  -->  Power BI push dataset
 (sites, assets)           (paged, filtered)                  (replaced on every run)
```

Each run takes a **point-in-time snapshot**: the Power BI table is cleared and
refilled, so the report always shows the latest export (not an ever-growing log).

## What you need

| Requirement | Notes |
|---|---|
| Python 3.9 or newer | `python3 --version` |
| Tenable OT Exposure / EM API key | Format `accessKey=...;secretKey=...`; the user must be able to read assets. |
| Network access from this machine to the EM | And to `login.microsoftonline.com` and `api.powerbi.com`. |
| A Power BI **Pro** (or better) user | Needs *Contributor* or *Member* on the target workspace. |
| An Entra ID **app registration** | One-time setup, below. |

## Setup

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create the Entra app registration (once)

Power BI *push datasets* accept rows only from a **signed-in user**, never from
an app-only identity, so the program signs in as you (once) and keeps the
resulting token. That sign-in needs an app registration in your tenant:

1. [Entra admin center](https://entra.microsoft.com) → **App registrations** → **New registration**.
   Name it anything (e.g. `Tenable OT export`); *Accounts in this organizational directory only*; no redirect URI.
2. On the app's **Overview** page, copy the **Directory (tenant) ID** and **Application (client) ID**.
3. **Authentication** → *Advanced settings* → **Allow public client flows = Yes** → Save.
   (This enables the device-code sign-in the program uses.)
4. **API permissions** → **Add a permission** → **Power BI Service** → **Delegated permissions** →
   tick **Dataset.ReadWrite.All** → Add. If your tenant requires it, an admin clicks
   **Grant admin consent**.

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in `TENABLE_URL`, `TENABLE_API_KEY`, `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, and (recommended) `POWERBI_WORKSPACE_ID`, the GUID in the
workspace URL `https://app.powerbi.com/groups/<WORKSPACE_ID>/...`. Leave it
empty to use *My workspace*. `.env` is git-ignored; never commit it.

Check the Tenable side works and see your site names and ids:

```bash
python t1ote_export.py sites
```

### 4. Sign in to Power BI (once)

```bash
python t1ote_export.py login
```

It prints a short code and a URL. Open the URL in any browser, enter the code,
sign in with the Power BI account that has access to the workspace. The refresh
token is saved to `.powerbi_token.json` (owner-readable only, git-ignored).
Treat it like a password.

### 5. Create the dataset (once)

```bash
python t1ote_export.py create-dataset
```

It prints a line like `POWERBI_DATASET_ID=...`. Paste it into `.env`.

## Running an export

With no options, **every asset on the site** is exported:

```bash
python t1ote_export.py export
```

Choose the scope with options:

| Option | Meaning | Example |
|---|---|---|
| `--site-name NAME` | Site to export, by name | `--site-name Houston` |
| `--site-uuid UUID` | Site to export, by id (see `sites`) | `--site-uuid 9b06f7ce-...` |
| `--subnet CIDR` | Assets with an IP inside this subnet | `--subnet 10.253.10.128/25` |
| `--ip-range A-B` | Assets with an IP inside this range | `--ip-range 10.0.0.10-10.0.0.99` |
| `--criticality-at-least L` | `none`, `low`, `medium` or `high` | `--criticality-at-least high` |
| `--limit N` | Stop after N assets | `--limit 100` |
| `--dry-run` | Fetch and summarise; **do not** touch Power BI | |

Options combine (AND): *high-criticality assets in a subnet*:

```bash
python t1ote_export.py export --subnet 10.253.10.128/25 --criticality-at-least high
```

`--subnet` and `--ip-range` are alternatives. The site is chosen from the
command line, else `TENABLE_SITE_UUID` in `.env`, else the only paired site;
if several sites are paired and none is chosen, the program says so.

Try a scope safely first: `python t1ote_export.py export --subnet 10.1.0.0/24 --dry-run`.

### Exported columns

`asset_id`, `name`, `site`, `vendor`, `model`, `criticality`, `ips`,
`last_seen`, `total_risk`, `exported_at` (one timestamp per run, UTC).

## Seeing the data in Power BI

1. In the workspace, open the dataset **Tenable OT Asset Export** → **Create report**, and add a Table or Matrix visual.
2. Save the report, then **pin the visual to a dashboard**.

> Push datasets do not auto-refresh an open *report page*. A **dashboard tile
> pinned from the report** updates automatically after each export; use that for
> anything that should stay current. (Reload the report page to see new data there.)

Run the export on a schedule (cron, Task Scheduler, a CI job) to keep the dashboard current.

## Troubleshooting

| Message | Likely cause and fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` talking to Tenable | The EM uses a certificate your machine doesn't trust. Set `TENABLE_CA_BUNDLE` to your CA's PEM file (preferred), or `TENABLE_TLS_VERIFY=0` for a lab. |
| `Tenable rejected the API key` | Check `TENABLE_API_KEY` format and the user's permissions. |
| `non-JSON response` | `TENABLE_URL` isn't the GraphQL host/port, or the site id isn't a paired site. Run `sites`. |
| `Multiple sites are paired` | Add `--site-name` / `--site-uuid`, or set `TENABLE_SITE_UUID`. |
| Sign-in `invalid_client` / `unauthorized_client` | Step 2.3: *Allow public client flows* must be **Yes**. |
| `AADSTS65001` (consent required) | Step 2.4: an admin must grant consent to *Dataset.ReadWrite.All*. |
| `Power BI sign-in has expired or been revoked` | Run `login` again. |
| Power BI `403` | Your account needs Contributor/Member on the workspace. |
| Power BI `404` | Wrong `POWERBI_WORKSPACE_ID`, `POWERBI_DATASET_ID` or `POWERBI_TABLE_NAME`. |

## Limits worth knowing

- Power BI accepts at most 10,000 rows per request (handled: rows are sent in batches),
  and enforces per-hour / per-minute request limits (not handled; ordinary OT-site exports sit far below them).
- The dataset uses *basic FIFO* retention, capped at 200,000 rows per table.
- Changing the columns means creating a new dataset (a push dataset's schema is fixed at creation).
  Change `TABLE_COLUMNS` and `build_rows()` together.
- At most 50,000 assets are fetched per run (a safety limit, `SAFETY_MAX_ASSETS`).

## For developers

### Code map

| Area | What lives there |
|---|---|
| **Configuration** | `TenableConfig`, `PowerBIConfig`: read and validate `.env`. |
| **Tenable client** | `Tenable`: GraphQL calls, site resolution, paged `fetch_assets()`. |
| **Scope and rows** | `build_filter()`, `parse_ip_range()`, `build_rows()`: pure functions, no I/O. |
| **Power BI** | `device_login()`, `PowerBI`: sign-in, token refresh, dataset and rows API. |
| **CLI** | `cmd_*` functions, `build_parser()`, `main()`. |

Design notes:

- All HTTP clients come from one function, `make_client()`. That is the single seam the tests replace with a fake transport.
- Expected failures raise `ExportError`; `main()` prints them as one line. Anything else is a bug and shows a traceback.
- Azure usually **rotates the refresh token on every use**; `PowerBI._access_token()` saves the new one immediately. Don't remove that.
- Progress and diagnostics go to stderr; only results go to stdout.

### API documentation (Doxygen)

The source is documented with Doxygen tags (`@brief`, `@param`, `@return`,
`@throws`, groups). Generate browsable HTML (README becomes the front page):

```bash
doxygen Doxyfile
open docs/api/html/index.html        # macOS; use xdg-open on Linux
```

Install Doxygen with `brew install doxygen` or `apt install doxygen` (and
Graphviz if you enable `HAVE_DOT` in the `Doxyfile` for diagrams). Generated
docs are git-ignored.

### Tests

The tests use only the standard library and need no network or credentials:

```bash
python -m unittest discover -s tests -v
```

### Project layout

```text
t1ote_export.py       the program (one file, documented)
tests/test_export.py  offline tests against a fake HTTP transport
.env.example          configuration template  ->  copy to .env
requirements.txt      httpx, python-dotenv
Doxyfile              API documentation config
.github/workflows/    CI: runs the tests on push
```

## Security notes

- `.env` (API key) and `.powerbi_token.json` (Power BI refresh token) are credentials. Both are git-ignored; keep their permissions tight.
- The program only **reads** from Tenable. In Power BI it only **creates the dataset and replaces the rows of its own table**.
- `TENABLE_TLS_VERIFY=0` disables certificate checking; use it for lab work only.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). You may use, modify and
redistribute it, including commercially, under the terms of that license. It is
provided "as is", without warranties or conditions of any kind. See [NOTICE](NOTICE) for attribution: the Tenable client logic is adapted from
John Walley's open-source [Tenable OT MCP Server](https://gitlab.com/jwalley/tenable-ot-mcp)
(Apache-2.0).
