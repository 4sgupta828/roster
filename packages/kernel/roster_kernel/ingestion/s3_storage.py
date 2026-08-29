"""S3/R2 content-addressed object store — the production ObjectStore backend.

Same content-addressed contract as InMemoryObjectStore (sha256 key → dedup), but
persists raw fetched artifacts to an S3-compatible bucket (AWS S3 or Cloudflare
R2 via an endpoint URL). boto3 is an optional dep (lazy import). Keys are
`<prefix>/<sha256>` so identical bytes store once regardless of source.
"""
from __future__ import annotations

import os

from roster_kernel.ingestion.storage import content_key


class S3ObjectStore:
    def __init__(self, *, bucket: str, endpoint_url: str | None = None,
                 access_key_id: str | None = None, secret_access_key: str | None = None,
                 region: str = "auto", prefix: str = "raw"):
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = None
        self._cfg = dict(endpoint_url=endpoint_url, region_name=region,
                         aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key)
        self.put_count = 0

    @classmethod
    def from_env(cls, *, prefix: str = "raw") -> "S3ObjectStore":
        return cls(
            bucket=os.environ["R2_BUCKET"],
            endpoint_url=os.environ.get("R2_ENDPOINT"),
            access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
            prefix=prefix,
        )

    def _c(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3", **{k: v for k, v in self._cfg.items() if v})
        return self._client

    def _key(self, sha: str) -> str:
        return f"{self._prefix}/{sha}" if self._prefix else sha

    def put(self, data: bytes) -> str:
        sha = content_key(data)
        key = self._key(sha)
        if not self.exists(sha):
            self._c().put_object(Bucket=self._bucket, Key=key, Body=data)
            self.put_count += 1
        return sha

    def get(self, key_or_sha: str) -> bytes:
        sha = key_or_sha.rsplit("/", 1)[-1]
        return self._c().get_object(Bucket=self._bucket, Key=self._key(sha))["Body"].read()

    def exists(self, key_or_sha: str) -> bool:
        import botocore
        sha = key_or_sha.rsplit("/", 1)[-1]
        try:
            self._c().head_object(Bucket=self._bucket, Key=self._key(sha))
            return True
        except botocore.exceptions.ClientError:
            return False
