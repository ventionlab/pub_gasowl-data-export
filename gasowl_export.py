#!/usr/bin/env python3
"""
GasOwl data export — standalone demo
====================================

Pulls your GasOwl gas-level data out of the GasOwl API and writes it to CSV
(stdout or a file) so you can drop it into a spreadsheet, on a schedule.

It authenticates exactly like the GasOwl Home Assistant plugin does:
  1. Log in to AWS Cognito with your GasOwl app email + password  -> JWT.
  2. Send that JWT as a Bearer token to the GasOwl GraphQL API.
The API is row-level scoped to your own account, so you only ever see your
own installations and readings.

Two modes:
  latest   (default) One row per scale with the most recent reading. Ideal for
           appending a snapshot to a sheet on a cron/Task Scheduler timer.
  history  Per-scale time-series over the last N days (--days), for backfilling.

Requirements:
  pip install boto3 requests

Credentials (never hard-code them — pass via env vars or flags):
  export GASOWL_USERNAME="you@example.com"      # your GasOwl app login
  export GASOWL_PASSWORD="..."                  # your GasOwl app password

Examples:
  # Current levels -> screen
  python gasowl_export.py

  # Current levels -> append to a CSV every hour (cron)
  python gasowl_export.py --mode latest --output levels.csv --append

  # Last 30 days of readings -> file
  python gasowl_export.py --mode history --days 30 --output history.csv

  # Daily aggregate (one weight per installation per day)
  python gasowl_export.py --mode daily --days 90

Pipe into Google Sheets: schedule this to write a CSV, then have Sheets import
it (IMPORTDATA on a published CSV URL), or push rows with the Sheets API /
Apps Script. The CSV shape here is deliberately flat and header-first.
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import requests

# --- Defaults (public, safe to ship) ----------------------------------------
# These match the values in the Home Assistant plugin's configuration.yaml.
DEFAULT_API_URL = "https://api.gasowl.co.nz/api/v1/graphql"
DEFAULT_CLIENT_ID = "5ilu9qjm10kr6703ku0hj3nj0q"  # GasOwl integrations app client: reads only
DEFAULT_REGION = "ap-southeast-2"
REQUEST_TIMEOUT = 30  # seconds


# --- Auth --------------------------------------------------------------------
def get_id_token(username, password, client_id, region):
    """Exchange GasOwl credentials for a Cognito JWT (IdToken)."""
    client = boto3.client("cognito-idp", region_name=region)
    resp = client.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["IdToken"]


def run_query(api_url, token, query, variables, role=None):
    """POST a GraphQL query with the Bearer token; return the `data` object.

    If `role` is given, an `X-Hasura-Role` header is sent so the request runs
    under that Hasura role. Use `export` (a select-only, read-only role) for a
    credential that physically cannot mutate anything.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if role:
        headers["X-Hasura-Role"] = role
    resp = requests.post(
        api_url,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data", {})


# --- Queries -----------------------------------------------------------------
# No account filter is needed: every role (user / export) is already scoped
# server-side to your own installations, so the API only ever returns your data.
# (The read-only `export` role can't reference the `account` relationship in a
# client filter anyway — and doesn't need to.)

# Latest reading per scale (mirrors the Home Assistant plugin's query).
LATEST_QUERY = """
query latest {
  installation {
    id
    name
    address
    tube {
      dev_eui
      serial_number
      scales(order_by: {index: asc}) {
        index
        serial_number
        processed_data(limit: 1, order_by: {time: desc}) {
          time
          bottle_level
          gas_weight
          gross_weight
        }
      }
    }
  }
}
"""

# Full reading history per scale over a time window.
HISTORY_QUERY = """
query history($since: timestamptz!) {
  installation {
    id
    name
    address
    tube {
      dev_eui
      scales(order_by: {index: asc}) {
        index
        serial_number
        processed_data(where: {time: {_gte: $since}}, order_by: {time: asc}) {
          time
          bottle_level
          gas_weight
          gross_weight
        }
      }
    }
  }
}
"""

# Pre-rolled daily aggregate (one weight per installation per day).
DAILY_QUERY = """
query daily($since: date!) {
  daily_aggregate(
    where: {date: {_gte: $since}}
    order_by: {date: asc}
  ) {
    installation_id
    date
    weight
    installation {
      name
    }
  }
}
"""


# --- Row builders ------------------------------------------------------------
def rows_from_installations(data, pulled_at):
    """Flatten installation -> tube -> scales -> processed_data into CSV rows.

    Works for both the `latest` and `history` queries (both share the shape;
    `latest` just caps processed_data at 1 row per scale).
    """
    rows = []
    for inst in data.get("installation", []):
        tube = inst.get("tube") or {}
        for scale in tube.get("scales", []):
            for reading in scale.get("processed_data", []):
                rows.append(
                    {
                        "pulled_at": pulled_at,
                        "installation_id": inst.get("id"),
                        "installation_name": inst.get("name"),
                        "address": inst.get("address"),
                        "dev_eui": tube.get("dev_eui"),
                        "scale_index": scale.get("index"),
                        "scale_serial": scale.get("serial_number"),
                        "reading_time": reading.get("time"),
                        "bottle_level_pct": reading.get("bottle_level"),
                        "gas_weight_kg": reading.get("gas_weight"),
                        "gross_weight_kg": reading.get("gross_weight"),
                    }
                )
    return rows


def rows_from_daily(data, pulled_at):
    rows = []
    for agg in data.get("daily_aggregate", []):
        rows.append(
            {
                "pulled_at": pulled_at,
                "installation_id": agg.get("installation_id"),
                "installation_name": (agg.get("installation") or {}).get("name"),
                "date": agg.get("date"),
                "weight_kg": agg.get("weight"),
            }
        )
    return rows


# --- Output ------------------------------------------------------------------
def write_csv(rows, fieldnames, output, append):
    if output:
        exists = append and os.path.exists(output) and os.path.getsize(output) > 0
        f = open(output, "a" if append else "w", newline="")
        write_header = not exists
    else:
        f = sys.stdout
        write_header = True
    try:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    finally:
        if output:
            f.close()


# --- CLI ---------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Export GasOwl data to CSV.")
    p.add_argument(
        "--mode",
        choices=["latest", "history", "daily"],
        default="latest",
        help="latest = current reading per scale (default); "
        "history = per-scale readings over --days; "
        "daily = pre-rolled daily aggregate over --days.",
    )
    p.add_argument("--days", type=int, default=30, help="Look-back window for history/daily (default 30).")
    p.add_argument("--output", help="CSV file to write. Omit to print to stdout.")
    p.add_argument("--append", action="store_true", help="Append to --output instead of overwriting.")
    p.add_argument("--username", default=os.environ.get("GASOWL_USERNAME"), help="GasOwl login (or GASOWL_USERNAME env).")
    p.add_argument("--password", default=os.environ.get("GASOWL_PASSWORD"), help="GasOwl password (or GASOWL_PASSWORD env).")
    p.add_argument("--api-url", default=os.environ.get("GASOWL_API_URL", DEFAULT_API_URL))
    p.add_argument("--client-id", default=os.environ.get("GASOWL_CLIENT_ID", DEFAULT_CLIENT_ID))
    p.add_argument("--region", default=os.environ.get("GASOWL_REGION", DEFAULT_REGION))
    p.add_argument(
        "--role",
        default=os.environ.get("GASOWL_ROLE"),
        help="Hasura role to run as (sends X-Hasura-Role). Use 'export' for a "
        "read-only, select-only role once the export role is deployed.",
    )
    args = p.parse_args()

    if not args.username or not args.password:
        p.error("Provide credentials via --username/--password or GASOWL_USERNAME/GASOWL_PASSWORD env vars.")

    pulled_at = datetime.now(timezone.utc).isoformat()

    try:
        token = get_id_token(args.username, args.password, args.client_id, args.region)
    except Exception as e:  # noqa: BLE001 - surface a clean message to the user
        sys.exit(f"Login failed: {e}")

    since = (datetime.now(timezone.utc) - timedelta(days=args.days))

    try:
        if args.mode == "latest":
            data = run_query(args.api_url, token, LATEST_QUERY, {}, role=args.role)
            rows = rows_from_installations(data, pulled_at)
            fields = [
                "pulled_at", "installation_id", "installation_name", "address",
                "dev_eui", "scale_index", "scale_serial", "reading_time",
                "bottle_level_pct", "gas_weight_kg", "gross_weight_kg",
            ]
        elif args.mode == "history":
            data = run_query(
                args.api_url, token, HISTORY_QUERY,
                {"since": since.isoformat()},
                role=args.role,
            )
            rows = rows_from_installations(data, pulled_at)
            fields = [
                "pulled_at", "installation_id", "installation_name", "address",
                "dev_eui", "scale_index", "scale_serial", "reading_time",
                "bottle_level_pct", "gas_weight_kg", "gross_weight_kg",
            ]
        else:  # daily
            data = run_query(
                args.api_url, token, DAILY_QUERY,
                {"since": since.date().isoformat()},
                role=args.role,
            )
            rows = rows_from_daily(data, pulled_at)
            fields = ["pulled_at", "installation_id", "installation_name", "date", "weight_kg"]
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Query failed: {e}")

    write_csv(rows, fields, args.output, args.append)
    if args.output:
        print(f"Wrote {len(rows)} row(s) to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
