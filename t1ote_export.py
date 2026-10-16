#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
## @file t1ote_export.py
#  @brief Tenable One OT Exposure -> Power BI asset export (reference application).
#
#  @details
#  This single-file program is a worked example of how to move OT asset data
#  out of Tenable One OT Exposure / Enterprise Manager (EM) and into Power BI.
#  It is intentionally small and dependency-light so it can be read top to
#  bottom, copied, and adapted.
#
#  ### Data flow
#  @code{.unparsed}
#      Tenable EM  --GraphQL-->  fetch_assets()  -->  build_rows()  -->  PowerBI.push_rows()
#      (sites, assets)           (paged, filtered)    (flat rows)        (Power BI push dataset)
#  @endcode
#
#  ### Commands
#  | Command          | Purpose                                                    |
#  |------------------|------------------------------------------------------------|
#  | `sites`          | List sites (paired ICPs) on the Enterprise Manager.        |
#  | `login`          | One-time Microsoft sign-in (device code flow) for Power BI.|
#  | `create-dataset` | One-time creation of the Power BI push dataset and table.  |
#  | `export`         | Pull assets from Tenable; replace the Power BI table.      |
#
#  All integration settings (URLs, keys, GUIDs) come from a `.env` file; only
#  the *scope* of an export (site, subnet, IP range, criticality, limit) is
#  given on the command line. See README.md and `.env.example`.
#
#  ### Code map
#  - @ref config      : .env loading and validation
#  - @ref tenable     : GraphQL client for Tenable OT Exposure
#  - @ref scope       : pure functions that build filters and shape rows
#  - @ref powerbi     : Microsoft sign-in and the Power BI push-dataset REST API
#  - @ref commands    : the CLI sub-commands and argument parser
#
#  @author Reference implementation, supplied as-is for evaluation.
#  @copyright Licensed under the Apache License, Version 2.0 (see LICENSE).
#  @version 0.1.0

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

## @brief Program version, shown by `--version`.
__version__ = "0.1.0"

# ==========================================================================
# Constants
# ==========================================================================

## @brief Microsoft Entra ID (Azure AD) sign-in authority.
AUTHORITY = "https://login.microsoftonline.com"

## @brief Power BI REST API root. Workspace-scoped paths are appended to it.
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

## @brief OAuth scope requested at sign-in.
#  @details `Dataset.ReadWrite.All` lets us create datasets and write rows.
#  `offline_access` is what makes Entra return a *refresh token*, which is
#  what allows `export` to run unattended after the one-time `login`.
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All offline_access"

## @brief Assets requested per GraphQL page.
PAGE_SIZE = 500

## @brief Circuit breaker: stop paging after this many assets, in case the
#  server ever returns a cursor that never terminates.
SAFETY_MAX_ASSETS = 50_000

## @brief Power BI's hard cap on rows in a single "push rows" request.
PUSH_BATCH_ROWS = 10_000

## @brief Valid values for `--criticality-at-least`, lowest to highest.
CRITICALITY_LEVELS = ["none", "low", "medium", "high"]

## @brief Maps our friendly level names to Tenable's GraphQL enum values.
_CRITICALITY_ENUM = {
    "none": "NoneCriticality",
    "low": "LowCriticality",
    "medium": "MediumCriticality",
    "high": "HighCriticality",
}

## @brief Reverse map (lower-cased enum -> "Low", "High", ...) for display.
_CRITICALITY_DISPLAY = {v.lower(): k.capitalize() for k, v in _CRITICALITY_ENUM.items()}

## @brief The Power BI table schema as `(column name, Power BI data type)`.
#  @details Single source of truth for the table: `PowerBI.create_dataset()`
#  builds the dataset from it, and `build_rows()` must emit exactly these
#  columns. If you add a column, change it here *and* in `build_rows()`,
#  then create a new dataset (a push dataset's schema is fixed at creation).
TABLE_COLUMNS = [
    ("asset_id", "string"),
    ("name", "string"),
    ("site", "string"),
    ("vendor", "string"),
    ("model", "string"),
    ("criticality", "string"),
    ("ips", "string"),
    ("last_seen", "string"),
    ("total_risk", "Double"),
    ("exported_at", "DateTime"),
]

## @brief Compiled pattern for a complete 8-4-4-4-12 hexadecimal UUID.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

## @brief GraphQL: list the sites (paired ICPs) known to the EM. Runs against
#  the EM *root* endpoint.
_QUERY_PAIRED_ICPS = """
query Q($pageSize: Int!) {
  emPairedIcps(first: $pageSize) {
    edges { node { site { machineId name } } }
  }
}
"""

## @brief GraphQL: one page of assets. Runs against a *site* endpoint.
#  @details `$filter` is optional; see `build_filter()` for its shape.
#  Pagination is cursor-based (`after` / `pageInfo.endCursor`).
_QUERY_ASSETS = """
query Q($pageSize: Int!, $after: String, $filter: AssetExpressionsParams) {
  assets(first: $pageSize, after: $after, filter: $filter) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      vendor
      model
      criticality
      ips(first: 10) { nodes }
      lastSeen
      risk { totalRisk }
    }
  }
}
"""


## @brief An error with a clear, user-facing message.
#  @details Everything the program *expects* can go wrong (bad config, a
#  rejected API key, an unreachable server) is raised as `ExportError`.
#  `main()` prints these as a one-line message with no traceback. Anything
#  that is *not* an `ExportError` is a genuine bug and gets a full traceback.
class ExportError(Exception):
    pass


## @brief Print a progress/diagnostic line to stderr.
#  @details Progress goes to stderr so that stdout carries only results
#  (handy for piping or scripting).
#  @param msg The text to print.
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


## @brief Create an HTTP client. The *only* place clients are constructed.
#  @details Keeping construction in one function gives tests a single seam
#  to replace with a fake transport (see `tests/test_export.py`).
#  @param verify `True`/`False` to enable/disable TLS verification, or a
#         filesystem path to a CA bundle (PEM) to trust instead of the system store.
#  @param timeout Per-request timeout in seconds.
#  @return A configured `httpx.Client`.
def make_client(verify: bool | str = True, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(verify=verify, timeout=timeout)


# ==========================================================================
## @defgroup config Configuration
#  @brief Reading and validating settings from the environment / .env file.
#  @{


## @brief Read an environment variable, stripped of whitespace.
#  @param name Variable name.
#  @param default Value if unset.
#  @return The stripped value, or `default`.
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


## @brief Fail with a helpful message if any named setting is missing/empty.
#  @param names One or more environment variable names that must be set.
#  @throws ExportError Listing every missing name at once (so the user can
#          fix them all in one pass rather than one error at a time).
def _require(*names: str) -> None:
    missing = [n for n in names if not _env(n)]
    if missing:
        raise ExportError(
            "Missing required setting(s) in your .env file: "
            + ", ".join(missing)
            + ". See .env.example."
        )


## @brief Settings for talking to Tenable One OT Exposure / EM.
@dataclass
class TenableConfig:
    ## Base URL of the EM, without a trailing slash.
    url: str
    ## API key, format `accessKey=...;secretKey=...`.
    api_key: str
    ## Optional default site machine id (UUID); empty if unset.
    site_uuid: str
    ## Whether to verify the EM's TLS certificate.
    tls_verify: bool
    ## Optional path to a CA bundle to trust; wins over `tls_verify`.
    ca_bundle: str

    ## @brief Build from environment variables.
    #  @return A populated `TenableConfig`.
    #  @throws ExportError If a required setting is missing or the CA bundle
    #          path does not exist.
    @classmethod
    def from_env(cls) -> TenableConfig:
        _require("TENABLE_URL", "TENABLE_API_KEY")
        ca_bundle = _env("TENABLE_CA_BUNDLE")
        if ca_bundle and not Path(ca_bundle).is_file():
            raise ExportError(f"TENABLE_CA_BUNDLE points at {ca_bundle!r}, which is not a file.")
        return cls(
            url=_env("TENABLE_URL").rstrip("/"),
            api_key=_env("TENABLE_API_KEY"),
            site_uuid=_env("TENABLE_SITE_UUID"),
            # Anything other than an explicit "off" value keeps verification ON.
            tls_verify=_env("TENABLE_TLS_VERIFY", "1").lower() not in ("0", "false", "no"),
            ca_bundle=ca_bundle,
        )


## @brief Settings for Microsoft sign-in and the Power BI destination.
@dataclass
class PowerBIConfig:
    ## Entra directory (tenant) ID.
    tenant_id: str
    ## Entra application (client) ID of the customer's app registration.
    client_id: str
    ## Power BI workspace (group) GUID; empty string means "My workspace".
    workspace_id: str
    ## GUID of the push dataset (set after `create-dataset`).
    dataset_id: str
    ## Display name used when creating the dataset.
    dataset_name: str
    ## Table inside the dataset that receives the assets.
    table_name: str
    ## File holding the sign-in refresh token (a credential).
    token_cache: Path

    ## @brief Build from environment variables.
    #  @param need_dataset True for commands that write rows (`export`), which
    #         additionally require `POWERBI_DATASET_ID`. `login` and
    #         `create-dataset` run *before* that ID exists, so they pass False.
    #  @return A populated `PowerBIConfig`.
    #  @throws ExportError If a required setting is missing.
    @classmethod
    def from_env(cls, *, need_dataset: bool) -> PowerBIConfig:
        _require("AZURE_TENANT_ID", "AZURE_CLIENT_ID")
        if need_dataset:
            _require("POWERBI_DATASET_ID")
        return cls(
            tenant_id=_env("AZURE_TENANT_ID"),
            client_id=_env("AZURE_CLIENT_ID"),
            workspace_id=_env("POWERBI_WORKSPACE_ID"),
            dataset_id=_env("POWERBI_DATASET_ID"),
            dataset_name=_env("POWERBI_DATASET_NAME", "Tenable OT Asset Export"),
            table_name=_env("POWERBI_TABLE_NAME", "assets"),
            token_cache=Path(_env("TOKEN_CACHE_FILE", ".powerbi_token.json")).expanduser(),
        )


## @}


# ==========================================================================
## @defgroup tenable Tenable One OT Exposure client
#  @brief A minimal GraphQL client for Tenable OT Exposure / Enterprise Manager.
#  @{


## @brief GraphQL client for Tenable One OT Exposure / Enterprise Manager.
#  @details Tenable's data is split across two kinds of endpoint:
#  - the **EM root**, `<url>/graphql`, knows which sites exist;
#  - each **site** (a paired ICP) serves its own assets at
#    `<url>/<site machine id>/graphql`.
#
#  Asset queries must therefore be sent to a *site* endpoint. Sending one to
#  the EM root fails. `query()` picks the endpoint from its `site_id` argument.
#
#  @note Adapted from the Tenable OT MCP Server (Copyright 2026 OneClearPath,
#  Incorporated; author John Walley; Apache-2.0), as modified by Dominic Storey:
#  rewritten as a synchronous client with site resolution against the paired-site
#  list. See NOTICE.
class Tenable:
    ## @brief Create a client.
    #  @param cfg Connection settings (see `TenableConfig`).
    def __init__(self, cfg: TenableConfig) -> None:
        self.cfg = cfg
        # A configured CA bundle path always wins over the on/off flag.
        verify: bool | str = cfg.ca_bundle if cfg.ca_bundle else cfg.tls_verify
        self._http = make_client(verify=verify)

    ## @brief Run one GraphQL operation and return its `data` payload.
    #  @param query The GraphQL document.
    #  @param variables GraphQL variables for the document.
    #  @param site_id Site machine id to target a site endpoint; `None` targets
    #         the EM root.
    #  @return The `data` object of the GraphQL response (empty dict if none).
    #  @throws ExportError On transport failure, HTTP 4xx/5xx, a non-JSON body
    #          (usually a wrong URL or site id), or a GraphQL `errors` array.
    def query(self, query: str, variables: dict[str, Any], site_id: str | None = None) -> dict[str, Any]:
        endpoint = f"{self.cfg.url}/{site_id}/graphql" if site_id else f"{self.cfg.url}/graphql"
        headers = {
            "X-APIKeys": f"key={self.cfg.api_key}",  # Tenable's API-key header format
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._http.post(endpoint, headers=headers, json={"query": query, "variables": variables})
        except httpx.HTTPError as e:
            raise ExportError(f"Could not reach Tenable at {endpoint}: {e}") from e

        # Check status codes from most to least specific so the message is actionable.
        if resp.status_code in (401, 403):
            raise ExportError(
                f"Tenable rejected the API key (HTTP {resp.status_code}). Check TENABLE_API_KEY "
                "(format is 'accessKey=...;secretKey=...') and that the key's user may read assets."
            )
        if resp.status_code >= 400:
            raise ExportError(f"Tenable returned HTTP {resp.status_code} from {endpoint}: {resp.text[:300]}")
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            # A well-formed request that hits the wrong place (a web UI, a load
            # balancer, or a site id that is not paired) typically answers with HTML.
            raise ExportError(
                f"Tenable returned a non-JSON response from {endpoint} "
                f"(first bytes: {' '.join(resp.text[:150].split())!r}). Check TENABLE_URL points at the "
                "GraphQL-serving host/port, and that the site id is a currently paired site."
            ) from e
        # GraphQL reports query errors with HTTP 200 and an `errors` array.
        if isinstance(body, dict) and body.get("errors"):
            raise ExportError(
                "Tenable GraphQL error: " + "; ".join(e.get("message", str(e)) for e in body["errors"])
            )
        return body.get("data") or {}

    ## @brief List the sites (paired ICPs) on the EM.
    #  @return A list of `{"name": ..., "machine_id": ...}` dicts (possibly empty).
    def paired_sites(self) -> list[dict[str, str]]:
        data = self.query(_QUERY_PAIRED_ICPS, {"pageSize": 500})
        sites = []
        for edge in (data.get("emPairedIcps") or {}).get("edges") or []:
            # Be defensive: skip any edge missing a usable id or name.
            site = ((edge or {}).get("node") or {}).get("site") or {}
            mid, name = site.get("machineId"), site.get("name")
            if isinstance(mid, str) and isinstance(name, str) and mid.strip() and name.strip():
                sites.append({"name": name.strip(), "machine_id": mid.strip("/")})
        return sites

    ## @brief Decide which site to export from.
    #  @details Precedence (first match wins):
    #  1. `site_uuid` argument (`--site-uuid`)
    #  2. `site_name` argument (`--site-name`)
    #  3. `TENABLE_SITE_UUID` from `.env`
    #  4. the only paired site, if exactly one exists
    #
    #  The id is always validated against the EM's real list of paired sites
    #  so a typo produces a clear error here rather than an opaque HTML
    #  response from Tenable later.
    #  @param site_uuid Site machine id from the command line, or `None`.
    #  @param site_name Site name from the command line, or `None`.
    #  @return `(machine_id, display_name)`; the name is written to every row's `site` column.
    #  @throws ExportError If nothing is paired, the site is unknown, the id is
    #          malformed, or several sites exist and none was chosen.
    def resolve_site(self, site_uuid: str | None, site_name: str | None) -> tuple[str, str]:
        sites = self.paired_sites()
        if not sites:
            raise ExportError("No paired sites found on this Enterprise Manager; at least one ICP must be paired.")
        # Fall back to the .env default only if the command line chose nothing.
        if not site_uuid and not site_name:
            site_uuid = self.cfg.site_uuid or None

        if site_uuid:
            site_uuid = site_uuid.strip("/")
            if not _UUID_RE.match(site_uuid):
                raise ExportError(f"Site id {site_uuid!r} is not a complete UUID (8-4-4-4-12 hex).")
            for s in sites:
                if s["machine_id"].lower() == site_uuid.lower():
                    return s["machine_id"], s["name"]
            raise ExportError(f"Site id {site_uuid} is not a paired site. Run `sites` to list them.")
        if site_name:
            for s in sites:
                if s["name"].lower() == site_name.strip().lower():
                    return s["machine_id"], s["name"]
            raise ExportError(
                f"No site named {site_name!r}. Available: " + ", ".join(sorted(s["name"] for s in sites))
            )
        if len(sites) == 1:
            return sites[0]["machine_id"], sites[0]["name"]
        raise ExportError(
            "Multiple sites are paired (" + ", ".join(sorted(s["name"] for s in sites))
            + "). Pass --site-name or --site-uuid, or set TENABLE_SITE_UUID in .env."
        )

    ## @brief Page through a site's assets.
    #  @details Requests `PAGE_SIZE` assets at a time, following the GraphQL
    #  cursor until `hasNextPage` is false, `limit` is reached, or the
    #  `SAFETY_MAX_ASSETS` breaker trips. Progress is logged to stderr.
    #  @param site_id Site machine id (see `resolve_site()`).
    #  @param filt Filter from `build_filter()`, or `None` for every asset.
    #  @param limit Maximum number of assets to return, or `None` for all.
    #  @return `(total_count, nodes)`: Tenable's reported match count (which can
    #          exceed `len(nodes)` when `limit` is used) and the asset dicts.
    #  @throws ExportError Propagated from `query()`.
    def fetch_assets(self, site_id: str, filt: dict | None, limit: int | None) -> tuple[int, list[dict[str, Any]]]:
        nodes: list[dict[str, Any]] = []
        total = 0
        cursor: str | None = None
        while limit is None or len(nodes) < limit:
            # Never over-fetch past the requested limit.
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(nodes))
            variables: dict[str, Any] = {"pageSize": size}
            if cursor:
                variables["after"] = cursor
            if filt is not None:
                variables["filter"] = filt
            conn = self.query(_QUERY_ASSETS, variables, site_id=site_id).get("assets") or {}
            total = conn.get("totalCount") or 0
            page = conn.get("nodes") or []
            nodes.extend(page)
            log(f"  fetched {len(nodes)} / {total if limit is None else min(total, limit)}")
            info = conn.get("pageInfo") or {}
            cursor = info.get("endCursor")
            # Stop on: no more pages, an empty page, or a missing cursor.
            if not info.get("hasNextPage") or not page or not cursor:
                break
            if len(nodes) >= SAFETY_MAX_ASSETS:
                log(f"  stopping at the {SAFETY_MAX_ASSETS}-asset safety limit")
                break
        return total, (nodes if limit is None else nodes[:limit])


## @}


# ==========================================================================
## @defgroup scope Scope and row shaping
#  @brief Pure functions (no network, no I/O) that turn command-line scope into
#         a Tenable filter, and Tenable assets into Power BI rows. Easy to unit test.
#  @{


## @brief Parse an `--ip-range` value such as `10.0.0.10-10.0.0.99`.
#  @param text The raw argument.
#  @return `(start, end)` as normalised address strings.
#  @throws ExportError If the format is wrong, an address is invalid, the IP
#          versions differ, or start is greater than end.
def parse_ip_range(text: str) -> tuple[str, str]:
    parts = [p.strip() for p in text.split("-")]
    if len(parts) != 2 or not all(parts):
        raise ExportError(f"--ip-range must look like START-END (e.g. 10.0.0.10-10.0.0.99), got {text!r}")
    try:
        a, b = ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[1])
    except ValueError as e:
        raise ExportError(f"--ip-range has an invalid address: {e}") from e
    if a.version != b.version or a > b:
        raise ExportError(f"--ip-range {text!r}: start must be <= end and both the same IP version.")
    return str(a), str(b)


## @brief Build the Tenable GraphQL `filter` for the chosen scope.
#  @details Each option contributes one clause; several are combined with AND.
#  - criticality: an `In` clause listing the chosen level *and every level above it*.
#  - subnet / IP range: a `Between` clause on the asset's `ips` field, using the
#    first and last address of the range.
#
#  Example result for `--subnet 10.1.0.0/24 --criticality-at-least high`:
#  @code{.json}
#  {"op": "And", "expressions": [
#     {"field": "criticality", "op": "In", "values": ["HighCriticality"]},
#     {"field": "ips", "op": "Between", "values": ["10.1.0.0", "10.1.0.255"]}]}
#  @endcode
#  @param criticality_at_least One of `CRITICALITY_LEVELS`, or `None`.
#  @param subnet A CIDR such as `10.253.10.128/25`, or `None`.
#  @param ip_range A `START-END` range, or `None`. Mutually exclusive with `subnet`.
#  @return The filter dict, or `None` when no scope option was given (export everything).
#  @throws ExportError On invalid values or when both `subnet` and `ip_range` are given.
def build_filter(
    criticality_at_least: str | None, subnet: str | None, ip_range: str | None
) -> dict | None:
    parts: list[dict] = []
    if criticality_at_least:
        if criticality_at_least not in CRITICALITY_LEVELS:
            raise ExportError(f"--criticality-at-least must be one of {CRITICALITY_LEVELS}")
        idx = CRITICALITY_LEVELS.index(criticality_at_least)
        parts.append(
            {
                "field": "criticality",
                "op": "In",
                "values": [_CRITICALITY_ENUM[lv] for lv in CRITICALITY_LEVELS[idx:]],
            }
        )
    if subnet and ip_range:
        raise ExportError("Use --subnet or --ip-range, not both.")
    if subnet:
        try:
            # strict=False lets users pass a host address with a mask (10.1.2.3/24).
            net = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            raise ExportError(f"--subnet {subnet!r} is not a valid CIDR: {e}") from e
        parts.append(
            {"field": "ips", "op": "Between", "values": [str(net.network_address), str(net.broadcast_address)]}
        )
    if ip_range:
        start, end = parse_ip_range(ip_range)
        parts.append({"field": "ips", "op": "Between", "values": [start, end]})
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"op": "And", "expressions": parts}


## @brief Flatten Tenable asset nodes into Power BI rows.
#  @details The output keys match `TABLE_COLUMNS` exactly. Missing values
#  become empty strings (text columns) or `None` (`total_risk`), so a sparse
#  asset never breaks the push. All rows in one export share the same
#  `exported_at` timestamp (UTC, ISO 8601), which makes it easy to tell in
#  Power BI which run a row came from.
#  @param nodes Asset dicts as returned by `Tenable.fetch_assets()`.
#  @param site_label Site display name written to every row's `site` column
#         (assets are queried per site and carry no site field themselves).
#  @return A list of row dicts, one per asset, in the same order.
def build_rows(nodes: list[dict[str, Any]], site_label: str) -> list[dict[str, Any]]:
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for n in nodes:
        crit_raw = (n.get("criticality") or "").lower()
        rows.append(
            {
                "asset_id": n.get("id") or "",
                "name": n.get("name") or "",
                "site": site_label,
                "vendor": n.get("vendor") or "",
                "model": n.get("model") or "",
                # Show "High" rather than "HighCriticality"; pass unknown values through.
                "criticality": _CRITICALITY_DISPLAY.get(crit_raw, n.get("criticality") or ""),
                # A device can have several IPs; join them into one readable cell.
                "ips": ", ".join((n.get("ips") or {}).get("nodes") or []),
                "last_seen": n.get("lastSeen") or "",
                "total_risk": (n.get("risk") or {}).get("totalRisk"),
                "exported_at": exported_at,
            }
        )
    return rows


## @}


# ==========================================================================
## @defgroup powerbi Power BI
#  @brief Microsoft sign-in (device code flow) and the Power BI push-dataset REST API.
#
#  @details Why a *push dataset* and a *user* sign-in? Push datasets accept rows
#  directly over REST with no refresh step, and they work on a Power BI Pro
#  licence. Microsoft does not allow service principals (app-only auth) to
#  write to push datasets, so a real user signs in once (`login`) and the
#  program keeps working from the stored refresh token afterwards.
#  @{


## @brief Turn an Entra error response into a short readable string.
#  @param resp The failed HTTP response.
#  @return `"<error>: <description>"`, falling back to status and body text.
def _aad_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return f"{body.get('error', resp.status_code)}: {body.get('error_description', '')[:300]}"
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"


## @brief Persist the refresh token, readable only by the current user.
#  @details Written to a temp file then atomically renamed, so a crash
#  mid-write can never leave a truncated (unusable) token file behind.
#  @param path Destination file (the `TOKEN_CACHE_FILE` setting).
#  @param refresh_token The token to store.
def _save_refresh_token(path: Path, refresh_token: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"refresh_token": refresh_token}))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600: it is a credential
    os.replace(tmp, path)


## @brief Interactive one-time Microsoft sign-in using the device code flow.
#  @details Prints a short code and a URL. The user opens the URL in *any*
#  browser, enters the code and signs in; meanwhile this function polls the
#  token endpoint until sign-in completes, then stores the refresh token.
#  The device-code flow suits CLI tools: no local web server or redirect URI.
#  @param cfg Power BI / Entra settings.
#  @throws ExportError If Microsoft rejects the request, the user declines, or
#          the code expires (15 minutes by default).
def device_login(cfg: PowerBIConfig) -> None:
    http = make_client()
    base = f"{AUTHORITY}/{cfg.tenant_id}/oauth2/v2.0"
    # Step 1: ask Microsoft for a device code + user code.
    try:
        r = http.post(f"{base}/devicecode", data={"client_id": cfg.client_id, "scope": POWERBI_SCOPE})
    except httpx.HTTPError as e:
        raise ExportError(f"Could not reach Microsoft sign-in: {e}") from e
    if r.status_code >= 400:
        raise ExportError(
            f"Microsoft rejected the sign-in request ({_aad_error(r)}). Check AZURE_TENANT_ID / "
            "AZURE_CLIENT_ID, and that 'Allow public client flows' is enabled on the app registration."
        )
    dc = r.json()
    print(dc.get("message") or f"Open {dc['verification_uri']} and enter code {dc['user_code']}")

    # Step 2: poll until the user finishes signing in.
    interval = float(dc.get("interval", 5))
    deadline = time.monotonic() + float(dc.get("expires_in", 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        t = http.post(
            f"{base}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": cfg.client_id,
                "device_code": dc["device_code"],
            },
        )
        if t.status_code == 200:
            _save_refresh_token(cfg.token_cache, t.json()["refresh_token"])
            print(f"Signed in. Token saved to {cfg.token_cache}")
            return
        err = (t.json() if t.content else {}).get("error", "")
        if err == "authorization_pending":  # user has not finished yet: keep waiting
            continue
        if err == "slow_down":              # server asks us to poll less often
            interval += 5
            continue
        raise ExportError(f"Sign-in did not complete ({_aad_error(t)})")
    raise ExportError("Sign-in timed out; run `login` again.")


## @brief Client for the Power BI push-dataset REST API.
#  @details Handles access tokens transparently: the first API call exchanges
#  the stored refresh token for a short-lived access token, caches it in
#  memory, and renews it shortly before expiry.
class PowerBI:
    ## @brief Create a client.
    #  @param cfg Power BI / Entra settings.
    def __init__(self, cfg: PowerBIConfig) -> None:
        self.cfg = cfg
        self._http = make_client()
        self._token: str | None = None
        self._token_expires = 0.0  # time.monotonic() value at which the token lapses

    ## @brief Return a valid access token, refreshing it if needed.
    #  @details Entra usually *rotates* the refresh token on every use. The new
    #  one is written back to the token cache immediately; if it were not, the
    #  next run would fail with `invalid_grant`.
    #  @return A bearer access token for the Power BI API.
    #  @throws ExportError If never signed in, or the sign-in expired/was revoked.
    def _access_token(self) -> str:
        # Reuse the cached token unless it is within 30 s of expiring.
        if self._token and self._token_expires > time.monotonic() + 30:
            return self._token
        if not self.cfg.token_cache.is_file():
            raise ExportError("Not signed in to Power BI yet. Run `python t1ote_export.py login` first.")
        refresh = json.loads(self.cfg.token_cache.read_text())["refresh_token"]
        r = self._http.post(
            f"{AUTHORITY}/{self.cfg.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.cfg.client_id,
                "refresh_token": refresh,
                "scope": POWERBI_SCOPE,
            },
        )
        if r.status_code >= 400:
            raise ExportError(
                f"Power BI sign-in has expired or been revoked ({_aad_error(r)}). Run `login` again."
            )
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + float(body.get("expires_in", 3600))
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            _save_refresh_token(self.cfg.token_cache, new_refresh)
        return self._token

    ## @brief Build a full API URL for the configured workspace.
    #  @param path Path below the workspace, e.g. `/datasets`.
    #  @return Full URL. With no `POWERBI_WORKSPACE_ID` the `/groups/<id>`
    #          segment is omitted, which targets "My workspace".
    def _url(self, path: str) -> str:
        prefix = f"/groups/{self.cfg.workspace_id}" if self.cfg.workspace_id else ""
        return f"{POWERBI_API}{prefix}{path}"

    ## @brief Send an authenticated request and turn failures into `ExportError`.
    #  @param method HTTP method.
    #  @param url Full URL.
    #  @param what Short phrase ("creating the dataset") used in error messages.
    #  @param kw Extra arguments for `httpx.Client.request` (e.g. `json=`).
    #  @return The successful (< 400) response.
    #  @throws ExportError On transport errors or HTTP >= 400, with a hint for
    #          the common 401/403 (permissions) and 404 (wrong id) cases.
    def _call(self, method: str, url: str, what: str, **kw: Any) -> httpx.Response:
        try:
            r = self._http.request(method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, **kw)
        except httpx.HTTPError as e:
            raise ExportError(f"Could not reach Power BI while {what}: {e}") from e
        if r.status_code >= 400:
            hint = ""
            if r.status_code in (401, 403):
                hint = " (does your account have Contributor/Member access to the workspace?)"
            elif r.status_code == 404:
                hint = " (check POWERBI_WORKSPACE_ID / POWERBI_DATASET_ID / POWERBI_TABLE_NAME)"
            raise ExportError(f"Power BI error while {what}: HTTP {r.status_code} {r.text[:400]}{hint}")
        return r

    ## @brief Create the push dataset (one table, schema from `TABLE_COLUMNS`).
    #  @details Uses the `basicFIFO` retention policy, a safety net that caps the
    #  table at 200,000 rows (oldest dropped first). Note it is a **query-string**
    #  parameter on this endpoint; putting it in the JSON body is rejected with HTTP 400.
    #  @return The new dataset's GUID (store it as `POWERBI_DATASET_ID`).
    #  @throws ExportError On any Power BI failure.
    def create_dataset(self) -> str:
        url = self._url("/datasets") + "?defaultRetentionPolicy=basicFIFO"
        body = {
            "name": self.cfg.dataset_name,
            "defaultMode": "Push",
            "tables": [
                {
                    "name": self.cfg.table_name,
                    "columns": [{"name": n, "dataType": t} for n, t in TABLE_COLUMNS],
                }
            ],
        }
        return self._call("POST", url, "creating the dataset", json=body).json()["id"]

    ## @brief URL of the configured table's `rows` collection.
    #  @return Full URL for DELETE (clear) and POST (append) of rows.
    def _rows_url(self) -> str:
        return self._url(f"/datasets/{self.cfg.dataset_id}/tables/{self.cfg.table_name}/rows")

    ## @brief Delete every row in the configured table.
    #  @details Called before each export so Power BI always shows the *latest
    #  snapshot* rather than an ever-growing append log.
    #  @throws ExportError On any Power BI failure.
    def clear_table(self) -> None:
        self._call("DELETE", self._rows_url(), f"clearing table {self.cfg.table_name!r}")

    ## @brief Append rows to the configured table.
    #  @details Sends at most `PUSH_BATCH_ROWS` rows per request (Power BI's
    #  limit). Other service limits (rows per hour, requests per minute) are
    #  not handled here; an export at typical OT-site scale stays far below them.
    #  @param rows Row dicts from `build_rows()`.
    #  @return The number of requests (batches) sent.
    #  @throws ExportError On any Power BI failure.
    def push_rows(self, rows: list[dict[str, Any]]) -> int:
        batches = 0
        for i in range(0, len(rows), PUSH_BATCH_ROWS):
            chunk = rows[i : i + PUSH_BATCH_ROWS]
            self._call("POST", self._rows_url(), f"pushing rows {i + 1}-{i + len(chunk)}", json={"rows": chunk})
            batches += 1
        return batches


## @}


# ==========================================================================
## @defgroup commands Command-line interface
#  @brief One `cmd_*` function per sub-command, plus the argument parser.
#  @details Each `cmd_*` takes the parsed `argparse.Namespace` and returns a
#  process exit code (0 = success).
#  @{


## @brief `sites`: print every paired site as `name  machine-id`.
#  @param args Parsed arguments (unused).
#  @return 0 on success, 1 if no sites are paired.
def cmd_sites(args: argparse.Namespace) -> int:
    sites = Tenable(TenableConfig.from_env()).paired_sites()
    if not sites:
        print("No paired sites found.")
        return 1
    width = max(len(s["name"]) for s in sites)  # align the id column
    for s in sorted(sites, key=lambda s: s["name"].lower()):
        print(f"{s['name']:<{width}}  {s['machine_id']}")
    return 0


## @brief `login`: run the one-time Microsoft sign-in.
#  @param args Parsed arguments (unused).
#  @return 0 on success.
def cmd_login(args: argparse.Namespace) -> int:
    device_login(PowerBIConfig.from_env(need_dataset=False))
    return 0


## @brief `create-dataset`: create the Power BI push dataset and print its id.
#  @details Does not edit `.env`; it prints the line to add, so the user stays
#  in control of their configuration file.
#  @param args Parsed arguments (unused).
#  @return 0 on success.
def cmd_create_dataset(args: argparse.Namespace) -> int:
    cfg = PowerBIConfig.from_env(need_dataset=False)
    dataset_id = PowerBI(cfg).create_dataset()
    print(f"Created dataset {cfg.dataset_name!r} with table {cfg.table_name!r}.")
    print("Add this line to your .env file:")
    print(f"POWERBI_DATASET_ID={dataset_id}")
    return 0


## @brief `export`: fetch assets from Tenable and replace the Power BI table.
#  @details Steps: validate the scope -> resolve the site -> page the assets ->
#  shape rows -> (unless `--dry-run`) clear the table and push the rows.
#  Scope is validated *first* so a typo fails instantly, before any network call.
#  @param args Parsed arguments: `site_name`, `site_uuid`, `subnet`, `ip_range`,
#         `criticality_at_least`, `limit`, `dry_run`.
#  @return 0 on success.
#  @throws ExportError On invalid scope or any Tenable / Power BI failure.
def cmd_export(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        raise ExportError("--limit must be a positive number.")
    filt = build_filter(args.criticality_at_least, args.subnet, args.ip_range)
    tcfg = TenableConfig.from_env()
    # A dry run needs no Power BI settings at all, so skip loading them.
    pbi = None if args.dry_run else PowerBI(PowerBIConfig.from_env(need_dataset=True))

    tenable = Tenable(tcfg)
    site_id, site_label = tenable.resolve_site(args.site_uuid, args.site_name)
    log(f"Site: {site_label} ({site_id})")
    log("Fetching assets from Tenable...")
    total, nodes = tenable.fetch_assets(site_id, filt, args.limit)
    rows = build_rows(nodes, site_label)
    log(f"Fetched {len(rows)} asset(s) (Tenable reports {total} matching).")

    if args.dry_run:
        print(f"[dry run] {len(rows)} row(s) would be written to Power BI. First rows:")
        for r in rows[:3]:
            print("  " + json.dumps(r))
        return 0

    assert pbi is not None  # for type checkers: set whenever dry_run is False
    log(f"Replacing contents of Power BI table {pbi.cfg.table_name!r}...")
    pbi.clear_table()
    batches = pbi.push_rows(rows)
    print(f"Done: {len(rows)} asset(s) written to Power BI in {batches} batch(es).")
    return 0


## @brief Build the command-line argument parser.
#  @return A configured `argparse.ArgumentParser` with one sub-parser per command.
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="t1ote_export.py",
        description="Export Tenable OT assets into a Power BI push dataset.",
    )
    p.add_argument("--env-file", default=None, help="Path to the .env file (default: .env next to this script, then the current directory).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("sites", help="List sites (paired ICPs) on the Enterprise Manager.").set_defaults(func=cmd_sites)
    sub.add_parser("login", help="One-time Microsoft sign-in for Power BI.").set_defaults(func=cmd_login)
    sub.add_parser("create-dataset", help="One-time creation of the Power BI push dataset.").set_defaults(func=cmd_create_dataset)

    e = sub.add_parser(
        "export",
        help="Export assets to Power BI, replacing the table's current contents.",
        description="With no scope options, every asset on the site is exported.",
    )
    e.add_argument("--site-name", help="Site to export, by name.")
    e.add_argument("--site-uuid", help="Site to export, by machine id (see `sites`).")
    e.add_argument("--subnet", help="Only assets with an IP in this CIDR, e.g. 10.253.10.128/25.")
    e.add_argument("--ip-range", help="Only assets with an IP in this range, e.g. 10.0.0.10-10.0.0.99.")
    e.add_argument("--criticality-at-least", choices=CRITICALITY_LEVELS, help="Minimum asset criticality.")
    e.add_argument("--limit", type=int, help="Export at most this many assets.")
    e.add_argument("--dry-run", action="store_true", help="Fetch from Tenable and show a summary; do not touch Power BI.")
    e.set_defaults(func=cmd_export)
    return p


## @brief Program entry point.
#  @details Loads the `.env` file, dispatches to the chosen command, and maps
#  outcomes to exit codes: 0 success, 1 expected error (`ExportError`),
#  2 bad `--env-file`, 130 Ctrl-C.
#  @param argv Argument list (defaults to `sys.argv[1:]`); a parameter so tests can call `main([...])`.
#  @return The process exit code.
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.env_file:
        if not Path(args.env_file).is_file():
            print(f"Error: env file {args.env_file!r} not found.", file=sys.stderr)
            return 2
        load_dotenv(args.env_file)
    else:
        # Script directory first, then the current directory. load_dotenv never
        # overrides a variable that is already set, so real environment
        # variables always win over the file.
        load_dotenv(Path(__file__).resolve().parent / ".env")
        load_dotenv(Path.cwd() / ".env")
    try:
        return args.func(args)
    except ExportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


## @}

if __name__ == "__main__":
    sys.exit(main())
