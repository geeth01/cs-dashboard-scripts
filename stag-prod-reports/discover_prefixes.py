#!/usr/bin/env python3
"""
List all top-level S3 prefixes in the sanity reports bucket.

Run this once while connected to VPN to discover what prefix names exist,
then fill in config/sanities.yaml accordingly.

Usage:
    python discover_prefixes.py

Optional env vars (in .env):
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  — omit for anonymous access
    AWS_DEFAULT_REGION                          — defaults to us-east-1
    S3_BUCKET                                   — defaults to sanity-reports-and-screenshots
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import yaml
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / ".env")

BUCKET = os.getenv("S3_BUCKET", "sanity-reports-and-screenshots")
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


def _s3_client():
    key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    if key and secret:
        return boto3.client(
            "s3",
            region_name=REGION,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
        )
    return boto3.client("s3", region_name=REGION, config=Config(signature_version=UNSIGNED))


def list_top_level_prefixes(s3, bucket: str) -> list[str]:
    prefixes: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for cp in page.get("CommonPrefixes") or []:
            prefix = cp["Prefix"].rstrip("/")
            prefixes.append(prefix)
    return sorted(prefixes)


def load_config_sanity_names() -> list[str]:
    cfg_path = _SCRIPT_DIR / "config" / "sanities.yaml"
    if not cfg_path.exists():
        return []
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    return [s["name"] for s in cfg.get("sanities", [])]


def main() -> None:
    s3 = _s3_client()
    print(f"Listing top-level prefixes in s3://{BUCKET} ...\n")

    try:
        prefixes = list_top_level_prefixes(s3, BUCKET)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(f"ERROR: S3 request failed ({code}): {exc}", file=sys.stderr)
        print("Make sure you are connected to VPN.", file=sys.stderr)
        sys.exit(1)
    except NoCredentialsError:
        print("ERROR: No AWS credentials. Either set AWS_ACCESS_KEY_ID/SECRET in .env", file=sys.stderr)
        print("or ensure the bucket allows anonymous access.", file=sys.stderr)
        sys.exit(1)

    if not prefixes:
        print("No prefixes found. Check VPN connection or bucket name.")
        return

    sanity_names = load_config_sanity_names()

    # Group by environment prefix (naStag, naProd, gcpProd, etc.)
    known_envs = [
        "naStag", "naProd", "euProd", "auProd",
        "azureStag", "azureProd", "azureEuProd",
        "gcpStag", "gcpProd", "gcpEuProd",
    ]

    grouped: dict[str, list[str]] = {e: [] for e in known_envs}
    ungrouped: list[str] = []

    for prefix in prefixes:
        matched = False
        for env in known_envs:
            if prefix.startswith(env):
                grouped[env].append(prefix)
                matched = True
                break
        if not matched:
            ungrouped.append(prefix)

    print(f"Found {len(prefixes)} top-level prefixes:\n")

    for env in known_envs:
        if grouped[env]:
            print(f"  [{env}]")
            for p in grouped[env]:
                sanity_part = p[len(env):]
                print(f"    {p}  (sanity code: {sanity_part!r})")
            print()

    if ungrouped:
        print("  [unrecognised environment prefix]")
        for p in ungrouped:
            print(f"    {p}")
        print()

    print("─" * 60)
    print("Next step: copy the prefix values into config/sanities.yaml")
    print("under the matching sanity name and environment key.")
    if sanity_names:
        print("\nSanity names in your config:")
        for n in sanity_names:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
