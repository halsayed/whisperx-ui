"""S3 access for the UI: test the connection, list and upload recordings,
read results. Uses the same settings the transcription Job gets."""
import contextvars
import ipaddress
import socket
from pathlib import PurePosixPath

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, SSLError

from .kube import UserError

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".aac", ".wma", ".webm", ".amr"}
RESULT_EXT = (".dialog.txt", ".json", ".srt", ".vtt")
MAX_LIST = 5000

# Per-request "host -> IP" override, for sites where DNS does not know the S3
# host name (the same case the Job solves with hostAliases).
_dns_override = contextvars.ContextVar("dns_override", default=None)
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, *args, **kwargs):
    o = _dns_override.get()
    if o and host == o[0]:
        host = o[1]
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo


def explain(e):
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        return {
            "AccessDenied": "access denied for this access key",
            "403": "access denied for this access key",
            "InvalidAccessKeyId": "the access key ID is not known to the storage system",
            "SignatureDoesNotMatch": "the secret access key is wrong",
            "NoSuchBucket": "the bucket does not exist",
            "404": "the bucket or file does not exist",
            "NoSuchKey": "the file does not exist",
        }.get(code, f"{code}: {e.response.get('Error', {}).get('Message', '')}")
    if isinstance(e, SSLError):
        return "the storage certificate is not trusted (turn off 'Verify certificate' for self-signed certificates)"
    if isinstance(e, EndpointConnectionError):
        return "cannot reach the storage address from this app"
    return str(e)[:300]


class Store:
    def __init__(self, cfg):
        """cfg keys: endpoint, region, verifySsl, hostIp, accessKey, secretKey,
        inputBucket, inputPrefix, outputBucket, outputPrefix"""
        self.cfg = cfg
        if not cfg.get("endpoint"):
            raise UserError("The object storage address is not set yet. Fill in the Setup page first.")
        if not cfg.get("accessKey") or not cfg.get("secretKey"):
            raise UserError("The object storage access key is not set yet. Fill in the Setup page first.")
        self.host = None
        if cfg.get("hostIp"):
            from .manifests import endpoint_host_port

            try:
                ipaddress.ip_address(cfg["hostIp"])
            except ValueError:
                raise UserError(f"'{cfg['hostIp']}' is not a valid IP address.")
            self.host = (endpoint_host_port(cfg["endpoint"])[0], cfg["hostIp"])
        self.s3 = boto3.client(
            "s3",
            endpoint_url=cfg["endpoint"],
            region_name=cfg.get("region") or "us-east-1",
            aws_access_key_id=cfg["accessKey"],
            aws_secret_access_key=cfg["secretKey"],
            verify=bool(cfg.get("verifySsl", True)),
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 2},
                connect_timeout=6,
                read_timeout=60,
            ),
        )

    def _call(self, fn, *a, **kw):
        token = _dns_override.set(self.host)
        try:
            return fn(*a, **kw)
        finally:
            _dns_override.reset(token)

    # ---- helpers ----
    def in_key(self, name):
        return (self.cfg.get("inputPrefix") or "") + name

    def out_key(self, rel):
        p = (self.cfg.get("outputPrefix") or "").strip("/")
        return f"{p}/{rel}" if p else rel

    def _list(self, bucket, prefix):
        out, token = [], None
        while len(out) < MAX_LIST:
            kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = self._call(self.s3.list_objects_v2, **kw)
            out += page.get("Contents", [])
            token = page.get("NextContinuationToken")
            if not token:
                break
        return out

    # ---- operations (blocking; call through asyncio.to_thread) ----
    def test(self):
        c, results = self.cfg, []
        try:
            self._call(self.s3.list_objects_v2, Bucket=c["inputBucket"], Prefix=c.get("inputPrefix") or "", MaxKeys=1)
            results.append({"check": f"Read recordings from '{c['inputBucket']}'", "ok": True})
        except (ClientError, BotoCoreError) as e:
            results.append({"check": f"Read recordings from '{c['inputBucket']}'", "ok": False, "message": explain(e)})
        probe = self.out_key(".whisperx-ui-write-test")
        try:
            self._call(self.s3.put_object, Bucket=c["outputBucket"], Key=probe, Body=b"ok")
            self._call(self.s3.delete_object, Bucket=c["outputBucket"], Key=probe)
            results.append({"check": f"Write results to '{c['outputBucket']}'", "ok": True})
        except (ClientError, BotoCoreError) as e:
            results.append({"check": f"Write results to '{c['outputBucket']}'", "ok": False, "message": explain(e)})
        return results

    def files(self):
        c = self.cfg
        try:
            ins = self._list(c["inputBucket"], c.get("inputPrefix") or "")
            outs = self._list(c["outputBucket"], (c.get("outputPrefix") or ""))
        except (ClientError, BotoCoreError) as e:
            raise UserError("Could not list the files in object storage: " + explain(e))
        have = {o["Key"] for o in outs}
        rows = []
        for o in ins:
            key = o["Key"]
            if PurePosixPath(key).suffix.lower() not in AUDIO_EXT:
                continue
            rel = key[len(c.get("inputPrefix") or ""):].lstrip("/")
            stem = str(PurePosixPath(rel).with_suffix(""))
            results = [ext for ext in RESULT_EXT if self.out_key(stem + ext) in have]
            rows.append({
                "name": rel,
                "size": o["Size"],
                "modified": o["LastModified"].isoformat(),
                "done": ".json" in results,
                "results": results,
            })
        rows.sort(key=lambda r: r["name"])
        return {"files": rows, "truncated": len(ins) >= MAX_LIST}

    def upload(self, name, fileobj):
        name = PurePosixPath(name.replace("\\", "/")).name
        if PurePosixPath(name).suffix.lower() not in AUDIO_EXT:
            raise UserError(f"'{name}' is not a supported audio type.")
        try:
            self._call(self.s3.put_object, Bucket=self.cfg["inputBucket"], Key=self.in_key(name), Body=fileobj)
        except (ClientError, BotoCoreError) as e:
            raise UserError(f"Could not upload '{name}': " + explain(e))
        return name

    def delete_recording(self, name, with_results):
        if ".." in name.split("/") or name.startswith("/"):
            raise UserError("Invalid file name.")
        try:
            self._call(self.s3.delete_object, Bucket=self.cfg["inputBucket"], Key=self.in_key(name))
            if with_results:
                stem = str(PurePosixPath(name).with_suffix(""))
                for ext in RESULT_EXT:
                    self._call(self.s3.delete_object, Bucket=self.cfg["outputBucket"], Key=self.out_key(stem + ext))
        except (ClientError, BotoCoreError) as e:
            raise UserError(f"Could not delete '{name}': " + explain(e))

    def result(self, name, ext):
        """Returns (bytes, content type) of one result file of recording `name`."""
        if ext not in RESULT_EXT or ".." in name.split("/") or name.startswith("/"):
            raise UserError("Invalid result file.")
        key = self.out_key(str(PurePosixPath(name).with_suffix("")) + ext)
        try:
            obj = self._call(self.s3.get_object, Bucket=self.cfg["outputBucket"], Key=key)
            return self._call(obj["Body"].read), obj.get("ContentType") or "application/octet-stream"
        except (ClientError, BotoCoreError) as e:
            raise UserError("Could not read the result file: " + explain(e))
