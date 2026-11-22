"""Thin kubectl wrapper.

The app keeps no state: every request carries the kubeconfig, which is
written to a private temp file for the duration of the request and removed
afterwards. Kubeconfigs are rebuilt from a whitelist of fields first, so a
kubeconfig can never make the server run a command (exec plugins) or read a
local file.
"""
import asyncio
import base64
import json
import os
import re
import tempfile

import yaml

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

CLUSTER_KEYS = ("server", "certificate-authority-data", "insecure-skip-tls-verify", "tls-server-name")
USER_KEYS = ("client-certificate-data", "client-key-data", "token", "username", "password")


class UserError(Exception):
    """A problem the user can fix; the message is shown in the UI."""


def in_cluster_allowed():
    return (
        os.environ.get("ALLOW_IN_CLUSTER", "").lower() in ("1", "true", "yes")
        and os.path.exists(f"{SA_DIR}/token")
        and bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    )


def in_cluster_kubeconfig():
    if not in_cluster_allowed():
        raise UserError("This app is not set up to use the cluster it runs in. Upload a kubeconfig instead.")
    host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if ":" in host:
        host = f"[{host}]"
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    with open(f"{SA_DIR}/ca.crt", "rb") as f:
        ca = base64.b64encode(f.read()).decode()
    return _build(
        {"server": f"https://{host}:{port}", "certificate-authority-data": ca},
        {"token": token},
        "in-cluster",
    )


def _build(cluster, user, name):
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": name, "cluster": cluster}],
        "users": [{"name": name, "user": user}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
        "current-context": name,
    }


def sanitize_kubeconfig(text, context=None):
    """Returns (clean kubeconfig dict, info dict). Raises UserError."""
    if not text or not text.strip():
        raise UserError("No kubeconfig was provided.")
    if len(text) > 512_000:
        raise UserError("The kubeconfig file is too large.")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise UserError("This file is not valid YAML, so it is not a kubeconfig.")
    if not isinstance(doc, dict) or not doc.get("clusters") or not doc.get("contexts"):
        raise UserError("This file does not look like a kubeconfig (no clusters or contexts in it).")

    def by_name(section, name):
        for e in doc.get(section) or []:
            if isinstance(e, dict) and e.get("name") == name:
                return e
        return None

    names = [c.get("name") for c in doc["contexts"] if isinstance(c, dict) and c.get("name")]
    chosen = context or doc.get("current-context") or (names[0] if names else None)
    ctx = by_name("contexts", chosen)
    if not ctx:
        raise UserError(f"Context '{chosen}' is not in this kubeconfig.")
    ctx = ctx.get("context") or {}
    cl = (by_name("clusters", ctx.get("cluster")) or {}).get("cluster") or {}
    us = (by_name("users", ctx.get("user")) or {}).get("user") or {}

    server = str(cl.get("server") or "")
    if not re.match(r"^https?://", server):
        raise UserError("The kubeconfig has no valid cluster address (server).")
    if "exec" in us or "auth-provider" in us:
        raise UserError(
            "This kubeconfig signs in by running an external program (exec or auth-provider plugin). "
            "This app does not run such programs. Use a kubeconfig that contains a token or a client certificate."
        )
    for k in ("certificate-authority", "client-certificate", "client-key", "tokenFile"):
        if k in cl or k in us:
            raise UserError(
                f"This kubeconfig points to a file on disk ('{k}'). "
                "Use one with the certificates embedded (kubectl config view --raw --flatten)."
            )
    cluster = {k: cl[k] for k in CLUSTER_KEYS if k in cl}
    user = {k: us[k] for k in USER_KEYS if k in us}
    if not user:
        raise UserError("The kubeconfig has no token or client certificate for this context.")
    info = {"server": server, "context": chosen, "contexts": names, "namespace": ctx.get("namespace")}
    return _build(cluster, user, "c"), info


def check_name(value, what="name"):
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise UserError(f"'{value}' is not a valid {what}: use lower-case letters, digits and '-'.")
    return value


def friendly(stderr):
    msg = (stderr or "").strip()
    low = msg.lower()
    if "unauthorized" in low or "must be logged in" in low:
        return "The cluster rejected the sign-in in this kubeconfig (expired or wrong credentials)."
    if "unable to connect to the server" in low or "connection refused" in low or "i/o timeout" in low or "no such host" in low:
        return "Cannot reach the cluster from this app. Check the cluster address and the network. Details: " + msg[-300:]
    if "forbidden" in low:
        return "This kubeconfig does not have permission for that action. Details: " + msg[-400:]
    if "certificate" in low and "x509" in low:
        return "The cluster certificate could not be verified. Details: " + msg[-300:]
    msg = re.sub(r"^(error|Error from server[^:]*):\s*", "", msg)
    return msg[-600:] or "kubectl failed"


class Kube:
    """Use as: async with Kube(conn) as k: ..."""

    def __init__(self, conn):
        conn = conn or {}
        self.ns = check_name(conn.get("namespace") or "whisperx", "namespace")
        if conn.get("inCluster"):
            self.cfg, self.info = in_cluster_kubeconfig(), {"server": "in-cluster", "context": "in-cluster", "contexts": []}
        else:
            self.cfg, self.info = sanitize_kubeconfig(conn.get("kubeconfig"), conn.get("context"))
        self.path = None

    async def __aenter__(self):
        fd, self.path = tempfile.mkstemp(prefix="kc-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self.cfg, f)
        return self

    async def __aexit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    async def run(self, *args, stdin=None, namespaced=True, timeout=45, check=True):
        cmd = ["kubectl", "--kubeconfig", self.path, "--request-timeout=30s"]
        if namespaced:
            cmd += ["-n", self.ns]
        cmd += list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": tempfile.gettempdir()},
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if isinstance(stdin, str) else stdin), timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise UserError("The cluster did not answer in time.")
        if proc.returncode != 0 and check:
            raise UserError(friendly(err.decode(errors="replace")))
        return out.decode(errors="replace")

    async def items(self, *args, **kw):
        """kubectl get ... -o json, always as a list of objects."""
        out = await self.run("get", *args, "-o", "json", "--ignore-not-found", **kw)
        if not out.strip():
            return []
        doc = json.loads(out)
        return doc.get("items", []) if doc.get("kind", "").endswith("List") else [doc]

    async def one(self, *args, **kw):
        got = await self.items(*args, **kw)
        return got[0] if got else None

    async def apply(self, *objs):
        doc = {"apiVersion": "v1", "kind": "List", "items": [o for o in objs if o]}
        if doc["items"]:
            await self.run("apply", "-f", "-", stdin=json.dumps(doc), namespaced=False)

    async def create(self, obj):
        out = await self.run("create", "-f", "-", "-o", "json", stdin=json.dumps(obj), namespaced=False)
        return json.loads(out)
