#!/usr/bin/env python3
"""One-time diagnostics: list only aggregate cache namespaces and their verified source labels.
Never print raw R2 object bytes, metric values, user data, secrets or S3 request signatures.
"""
import os
import re
import sys

from sync_from_r2 import ROOT, SOURCE, _parse, fetch, InvalidSnapshot

KEY = re.compile(r"^" + re.escape(ROOT) + r"/([0-9a-f]{24})/statistics/overall_weekly\.json$")


def main():
    account = os.getenv("R2_ACCOUNT_ID", "")
    bucket = os.getenv("R2_BUCKET", "")
    access = os.getenv("R2_ACCESS_KEY_ID", "")
    secret = os.getenv("R2_SECRET_ACCESS_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{32}", account) or not access or not secret:
        print("Missing R2 diagnostic configuration")
        return 2
    import boto3
    from botocore.config import Config
    client = boto3.client("s3",endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        region_name="auto",aws_access_key_id=access,aws_secret_access_key=secret,
        config=Config(signature_version="s3v4",connect_timeout=5,read_timeout=10,retries={"max_attempts":2}))
    try:
        result = client.list_objects_v2(Bucket=bucket,Prefix=f"{ROOT}/",MaxKeys=500)
        if result.get("IsTruncated"):
            print("Too many R2 cache objects; aborting discovery")
            return 1
        matches=[]
        for item in result.get("Contents",[]):
            match = KEY.fullmatch(item.get("Key",""))
            if match: matches.append((match.group(1),item["Key"]))
        if not matches:
            print("No weekly statistics snapshots found in R2")
            return 1
        for namespace,key in matches:
            try:
                doc=_parse(fetch(client,bucket,key))
                data=doc.get("data") if isinstance(doc,dict) else None
                if not isinstance(data,dict):
                    label="invalid_shape"
                elif data.get("available") is not True:
                    label="unavailable"
                elif data.get("source")==SOURCE and data.get("platform")=="overall" and data.get("period")=="weekly":
                    label="basic_statistics"
                elif data.get("source")=="first-party stored aggregate projections":
                    label="analytics_section"
                else:
                    label="other_source"
                print("R2 namespace "+namespace+" classification="+label)
            except (InvalidSnapshot,ValueError,KeyError,TypeError):
                print("R2 namespace "+namespace+" classification=invalid")
        return 0
    except Exception as exc:
        print("R2 namespace probe failed: "+type(exc).__name__)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
