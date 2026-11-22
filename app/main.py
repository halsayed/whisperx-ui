"""WhisperX Pipeline UI: a stateless web app that deploys, runs and monitors
https://github.com/halsayed/whisperx-pipeline on a Kubernetes cluster.

Nothing is stored on the server. The browser sends the kubeconfig with every
request; all pipeline state lives in the target cluster (ConfigMaps, Secrets,
Jobs) and in the S3 buckets.
"""
import asyncio
import base64
import ipaddress
import json
import os
import re
import secrets
import socket
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import manifests as m
from .kube import Kube, UserError, check_name, in_cluster_allowed
from .s3 import AUDIO_EXT, Store

VERSION = "0.1.0"
STATIC = Path(__file__).parent / "static"
JOB_RE = re.compile(r"^(whisperx-transcribe-[a-z0-9]{1,10}|model-download|model-check)$")

app = FastAPI(title="WhisperX Pipeline UI", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(UserError)
async def user_error(_, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.middleware("http")
async def guard(request: Request, call_next):
    user, pw = os.environ.get("UI_USERNAME"), os.environ.get("UI_PASSWORD")
    if user and pw and request.url.path != "/healthz":
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)
            except Exception:
                ok = False
        if not ok:
            return Response("Sign in required", 401, {"WWW-Authenticate": 'Basic realm="WhisperX Pipeline"'})
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/info")
async def info():
    return {"version": VERSION, "inCluster": in_cluster_allowed(), "defaultImage": m.DEFAULT_IMAGE,
            "audioTypes": sorted(AUDIO_EXT)}


# ------------------------------------------------------------- helpers ----
def b64(s):
    return base64.b64decode(s).decode() if s else ""


def by_kind(items):
    out = {}
    for it in items:
        out.setdefault(it["kind"], {})[it["metadata"]["name"]] = it
    return out


def load_settings(cm):
    try:
        return json.loads(((cm or {}).get("data") or {}).get("settings.json") or "{}")
    except ValueError:
        return {}


def s3_cfg(config, settings, secret, override=None):
    d = (config or {}).get("data") or {}
    sd = (secret or {}).get("data") or {}
    cfg = {
        "endpoint": d.get("S3_ENDPOINT_URL"),
        "region": d.get("AWS_DEFAULT_REGION"),
        "verifySsl": (d.get("S3_VERIFY_SSL") or "true").lower() != "false",
        "hostIp": settings.get("s3HostIp"),
        "accessKey": b64(sd.get("AWS_ACCESS_KEY_ID")),
        "secretKey": b64(sd.get("AWS_SECRET_ACCESS_KEY")),
        "inputBucket": d.get("INPUT_BUCKET"),
        "inputPrefix": d.get("INPUT_PREFIX") or "",
        "outputBucket": d.get("OUTPUT_BUCKET"),
        "outputPrefix": d.get("OUTPUT_PREFIX") or "",
    }
    for k, v in (override or {}).items():
        if k in cfg and v not in (None, ""):
            cfg[k] = v
    if override and "verifySsl" in override:
        cfg["verifySsl"] = bool(override["verifySsl"])
    if override and "hostIp" in override:
        cfg["hostIp"] = override["hostIp"] or None
    for k in ("inputPrefix", "outputPrefix"):
        if override and k in override:
            cfg[k] = override[k] or ""
    return cfg


async def store_for(k, override=None):
    got = by_kind(await k.items(f"configmap/{m.CONFIG_CM}", f"configmap/{m.SETTINGS_CM}", f"secret/{m.S3_SECRET}"))
    cms = got.get("ConfigMap", {})
    cfg = s3_cfg(cms.get(m.CONFIG_CM), load_settings(cms.get(m.SETTINGS_CM)),
                 got.get("Secret", {}).get(m.S3_SECRET), override)
    if not cfg.get("inputBucket") or not cfg.get("outputBucket"):
        raise UserError("The buckets are not set yet. Fill in the Setup page first.")
    return Store(cfg)


def explain_wait(reason, message):
    r, msg = reason or "", message or ""
    if r in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        return "The cluster cannot download the worker image. Check the image name and the registry sign-in on the Setup page."
    if r == "ContainerCreating" or r == "PodInitializing":
        return "Starting. The first start on a node downloads the worker image (about 5 GB), which can take several minutes."
    if r == "Unschedulable":
        low = msg.lower()
        if "claim" in low or "nvidia.com/gpu" in low or "insufficient" in low:
            return "Waiting for a free GPU of the selected type (it is busy or does not exist). Details: " + msg[:300]
        if "persistentvolumeclaim" in low:
            return "Waiting for the model storage. Details: " + msg[:300]
        return "No machine in the cluster can take this run yet. Details: " + msg[:300]
    if r == "CreateContainerConfigError":
        return "A required setting or secret is missing. Details: " + msg[:300]
    return (r + (": " + msg[:300] if msg else "")) or "Waiting to start"


def job_view(job, pods):
    name = job["metadata"]["name"]
    st = job.get("status") or {}
    mine = [p for p in pods if (p["metadata"].get("labels") or {}).get("job-name") == name
            or (p["metadata"].get("labels") or {}).get("batch.kubernetes.io/job-name") == name]
    mine.sort(key=lambda p: p["metadata"].get("creationTimestamp", ""))
    pod = mine[-1] if mine else None
    conds = {c["type"]: c for c in st.get("conditions") or [] if c.get("status") == "True"}
    view = {
        "name": name,
        "created": job["metadata"].get("creationTimestamp"),
        "started": st.get("startTime"),
        "finished": st.get("completionTime") or (conds.get("Failed") or {}).get("lastTransitionTime"),
        "node": (pod or {}).get("spec", {}).get("nodeName"),
        "detail": "",
    }
    if "Complete" in conds or st.get("succeeded"):
        view["state"] = "succeeded"
    elif "Failed" in conds or (st.get("failed") and not st.get("active")):
        view["state"] = "failed"
        view["detail"] = (conds.get("Failed") or {}).get("message", "")
    else:
        view["state"] = "waiting"
        view["detail"] = "Waiting to start"
        if pod:
            ps = pod.get("status") or {}
            if ps.get("phase") == "Running":
                view["state"], view["detail"] = "running", ""
            else:
                waiting = None
                for cs in ps.get("containerStatuses") or []:
                    waiting = (cs.get("state") or {}).get("waiting") or waiting
                if waiting:
                    view["detail"] = explain_wait(waiting.get("reason"), waiting.get("message"))
                else:
                    for c in ps.get("conditions") or []:
                        if c.get("type") == "PodScheduled" and c.get("status") == "False":
                            view["detail"] = explain_wait(c.get("reason"), c.get("message"))
    if pod and view["state"] == "failed":
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            term = (cs.get("state") or {}).get("terminated") or {}
            if term.get("reason") == "OOMKilled":
                view["detail"] = "The worker ran out of memory."
    return view


# ------------------------------------------------------------- cluster ----
@app.post("/api/connect")
async def connect(body: dict):
    async with Kube(body.get("conn")) as k:
        ver = json.loads(await k.run("version", "-o", "json", namespaced=False))
        out = {
            "server": k.info["server"],
            "context": k.info["context"],
            "contexts": k.info["contexts"],
            "suggestedNamespace": k.info.get("namespace"),
            "kubernetes": (ver.get("serverVersion") or {}).get("gitVersion"),
            "gpus": [],
            "nodes": 0,
            "warnings": [],
        }
        try:
            nodes = await k.items("nodes", namespaced=False)
        except UserError:
            out["warnings"].append("This kubeconfig cannot list the cluster machines, so GPU types are not shown.")
            return out
        used = {}
        try:
            pods = await k.items("pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
                                 namespaced=False, timeout=60)
            for p in pods:
                node = p["spec"].get("nodeName")
                for c in p["spec"].get("containers", []):
                    n = ((c.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu")
                    if node and n:
                        used[node] = used.get(node, 0) + int(n)
        except (UserError, ValueError):
            used = None
        gpus = {}
        for n in nodes:
            labels = n["metadata"].get("labels") or {}
            product = labels.get("nvidia.com/gpu.product")
            if not product:
                continue
            try:
                count = int(labels.get("nvidia.com/gpu.count") or (n["status"].get("allocatable") or {}).get("nvidia.com/gpu") or 0)
                mem = int(labels.get("nvidia.com/gpu.memory") or 0)
            except ValueError:
                count, mem = 0, 0
            g = gpus.setdefault(product, {"product": product, "memoryMiB": mem, "total": 0, "inUse": 0 if used is not None else None, "nodes": 0})
            g["total"] += count
            g["nodes"] += 1
            if used is not None:
                g["inUse"] += used.get(n["metadata"]["name"], 0)
        out["gpus"] = sorted(gpus.values(), key=lambda g: -g["memoryMiB"])
        out["nodes"] = len(nodes)
        return out


@app.post("/api/state")
async def state(body: dict):
    async with Kube(body.get("conn")) as k:
        ns = await k.one("namespace", k.ns, namespaced=False)
        out = {"namespace": k.ns, "namespaceExists": bool(ns), "storage": {"exists": False},
               "secrets": {"s3": False, "pull": False, "hf": False}, "config": None, "settings": {},
               "egress": False, "models": {"download": None, "check": None}}
        if not ns:
            return out
        got = by_kind(await k.items("configmap,secret,persistentvolumeclaim,networkpolicy,job,pod"))
        cms, secs = got.get("ConfigMap", {}), got.get("Secret", {})
        pods = list(got.get("Pod", {}).values())
        out["config"] = (cms.get(m.CONFIG_CM) or {}).get("data")
        out["settings"] = load_settings(cms.get(m.SETTINGS_CM))
        out["egress"] = m.NETPOL in got.get("NetworkPolicy", {})
        if m.S3_SECRET in secs:
            key = b64((secs[m.S3_SECRET].get("data") or {}).get("AWS_ACCESS_KEY_ID"))
            out["secrets"]["s3"] = True
            out["secrets"]["accessKeyHint"] = ("…" + key[-4:]) if len(key) > 8 else "saved"
        if m.PULL_SECRET in secs:
            out["secrets"]["pull"] = True
            try:
                auths = json.loads(b64(secs[m.PULL_SECRET]["data"][".dockerconfigjson"]))["auths"]
                server, entry = next(iter(auths.items()))
                out["secrets"]["pullServer"], out["secrets"]["pullUser"] = server, entry.get("username")
            except Exception:
                pass
        out["secrets"]["hf"] = m.HF_SECRET in secs
        pvc = got.get("PersistentVolumeClaim", {}).get(m.PVC)
        if pvc:
            out["storage"] = {"exists": True, "phase": (pvc.get("status") or {}).get("phase"),
                              "volume": pvc["spec"].get("volumeName")}
            if pvc["spec"].get("volumeName"):
                try:
                    pv = await k.one("persistentvolume", pvc["spec"]["volumeName"], namespaced=False)
                    nfs = ((pv or {}).get("spec") or {}).get("nfs") or {}
                    out["storage"].update({"nfsServer": nfs.get("server"), "nfsPath": nfs.get("path")})
                except UserError:
                    pass
        jobs = got.get("Job", {})
        for key, name in (("download", m.DOWNLOAD_JOB), ("check", m.CHECK_JOB)):
            if name in jobs:
                out["models"][key] = job_view(jobs[name], pods)
        chk = out["models"]["check"]
        if chk and chk["state"] in ("succeeded", "failed"):
            log = await k.run("logs", f"job/{m.CHECK_JOB}", "--tail=200", check=False)
            chk["items"] = [{"ok": ln.startswith("FOUND"), "optional": ln.startswith("OPTIONAL"), "label": ln.split(" ", 1)[1]}
                            for ln in log.splitlines() if ln.startswith(("FOUND ", "MISSING ", "OPTIONAL-MISSING "))]
        return out


def clean(v):
    return (v or "").strip() if isinstance(v, str) else v


@app.post("/api/setup")
async def setup(body: dict):
    st, s3, img, opt = (body.get(x) or {} for x in ("storage", "s3", "image", "options"))
    steps = []

    def done(step, message, ok=True):
        steps.append({"step": step, "ok": ok, "message": message})

    # ---- validate before touching the cluster ----
    endpoint = clean(s3.get("endpoint"))
    if not re.match(r"^https?://[^\s/]+", endpoint or ""):
        raise UserError("The object storage address must start with https:// (or http://).")
    for field, label in (("inputBucket", "input bucket"), ("outputBucket", "output bucket")):
        if not clean(s3.get(field)):
            raise UserError(f"The {label} name is required.")
    host_ip = clean(s3.get("hostIp"))
    if host_ip:
        try:
            ipaddress.ip_address(host_ip)
        except ValueError:
            raise UserError(f"'{host_ip}' is not a valid IP address.")
    try:
        batch = int(opt.get("batchSize") or 16)
        min_sp = int(opt["minSpeakers"]) if opt.get("minSpeakers") not in (None, "") else ""
        max_sp = int(opt["maxSpeakers"]) if opt.get("maxSpeakers") not in (None, "") else ""
    except (TypeError, ValueError):
        raise UserError("Batch size and speaker counts must be whole numbers.")
    if opt.get("model") not in (None, "large-v3", "large-v3-turbo"):
        raise UserError("Unknown transcription model.")
    if opt.get("computeType") not in (None, "float16", "int8_float16", "int8"):
        raise UserError("Unknown precision setting.")
    language = clean(opt.get("language")) or ""
    if language and not re.match(r"^[a-z]{2,3}$", language):
        raise UserError("Language must be a two-letter code such as 'en' or 'ar', or empty.")
    image = clean(img.get("name")) or m.DEFAULT_IMAGE
    if not re.match(r"^[\w.\-/:@]+$", image):
        raise UserError("The worker image name contains characters that are not allowed.")
    gpu = clean(body.get("gpuProduct")) or ""
    if gpu and not re.match(r"^[\w.\-]{1,63}$", gpu):
        raise UserError("Invalid GPU type.")

    def prefix(p):
        p = (clean(p) or "").lstrip("/")
        return p + "/" if p and not p.endswith("/") else p

    async with Kube(body.get("conn")) as k:
        ns = k.ns
        await k.apply(m.namespace(ns))
        done("Namespace", f"'{ns}' is ready")

        # ---- model storage ----
        pvc = await k.one("persistentvolumeclaim", m.PVC)
        server, path = clean(st.get("nfsServer")), clean(st.get("nfsPath"))
        if pvc:
            done("Model storage", "already connected (left unchanged)")
        elif server and path:
            if not re.match(r"^[\w.\-:]+$", server) or not path.startswith("/"):
                raise UserError("NFS server must be a name or IP, and the share path must start with '/'.")
            pv = await k.one("persistentvolume", m.pv_name(ns), namespaced=False)
            if pv and (pv["spec"].get("nfs") or {}).get("server") != server:
                raise UserError(f"A volume named '{m.pv_name(ns)}' already exists and points to another server.")
            if pv and (pv.get("status") or {}).get("phase") == "Released":
                # left over from a deleted namespace: let the new claim bind to it
                await k.run("patch", "persistentvolume", m.pv_name(ns), "--type=json",
                            "-p", '[{"op":"remove","path":"/spec/claimRef"}]', namespaced=False)
            await k.apply(*m.storage(ns, server, path))
            done("Model storage", f"connected to {server}:{path}")
        else:
            done("Model storage", "not set: enter the NFS server and share path", ok=False)

        # ---- secrets ----
        existing = by_kind(await k.items("secret")).get("Secret", {})
        ak, sk = clean(s3.get("accessKey")), clean(s3.get("secretKey"))
        if ak and sk:
            await k.apply(m.secret(ns, m.S3_SECRET, {"AWS_ACCESS_KEY_ID": ak, "AWS_SECRET_ACCESS_KEY": sk}))
            done("Object storage keys", "saved in the cluster")
        elif m.S3_SECRET in existing:
            done("Object storage keys", "kept as they were")
        else:
            done("Object storage keys", "missing: enter the access key and secret key", ok=False)
        ru, rt = clean(img.get("registryUser")), clean(img.get("registryToken"))
        if ru and rt:
            server_url = clean(img.get("registryServer")) or "https://index.docker.io/v1/"
            await k.apply(m.pull_secret(ns, server_url, ru, rt))
            done("Image registry sign-in", "saved in the cluster")
        elif m.PULL_SECRET in existing:
            done("Image registry sign-in", "kept as it was")
        else:
            done("Image registry sign-in", "none (fine for a public image)")

        # ---- settings ----
        config = {
            "S3_ENDPOINT_URL": endpoint,
            "S3_VERIFY_SSL": "true" if s3.get("verifySsl", True) else "false",
            "AWS_DEFAULT_REGION": clean(s3.get("region")) or "us-east-1",
            "INPUT_BUCKET": clean(s3["inputBucket"]),
            "INPUT_PREFIX": prefix(s3.get("inputPrefix")),
            "OUTPUT_BUCKET": clean(s3["outputBucket"]),
            "OUTPUT_PREFIX": prefix(s3.get("outputPrefix")),
            "WHISPER_MODEL": opt.get("model") or "large-v3",
            "COMPUTE_TYPE": opt.get("computeType") or "float16",
            "BATCH_SIZE": str(batch),
            "LANGUAGE": language,
            "DIARIZE": "true" if opt.get("diarize", True) else "false",
            "MIN_SPEAKERS": str(min_sp),
            "MAX_SPEAKERS": str(max_sp),
            "OVERWRITE": "true" if opt.get("overwrite") else "false",
        }
        restrict = bool(body.get("restrictEgress"))
        settings = {"image": image, "gpuProduct": gpu, "s3HostIp": host_ip or "", "restrictEgress": restrict}
        await k.apply(m.config_map(ns, m.CONFIG_CM, config),
                      m.config_map(ns, m.SETTINGS_CM, {"settings.json": json.dumps(settings)}))
        done("Pipeline settings", "saved in the cluster")

        # ---- network lock-down ----
        if restrict:
            host, port = m.endpoint_host_port(endpoint)
            ip = host_ip
            if not ip:
                try:
                    ip = str(ipaddress.ip_address(host))
                except ValueError:
                    try:
                        ip = await asyncio.to_thread(socket.gethostbyname, host)
                    except OSError:
                        ip = None
            if ip:
                await k.apply(m.egress_policy(ns, ip, port))
                done("Network lock-down", f"runs may only reach {ip}:{port} and cluster DNS")
            else:
                done("Network lock-down", "not applied: enter the storage IP address so the rule can be written", ok=False)
        else:
            await k.run("delete", "networkpolicy", m.NETPOL, "--ignore-not-found")
            done("Network lock-down", "off")
    return {"steps": steps, "ok": all(s["ok"] for s in steps)}


# ------------------------------------------------------------------ S3 ----
@app.post("/api/s3/test")
async def s3_test(body: dict):
    override = {k: clean(v) for k, v in (body.get("s3") or {}).items()}
    async with Kube(body.get("conn")) as k:
        try:
            store = await store_for(k, override)
        except UserError:
            # nothing saved yet (or no namespace): test with the form values only
            cfg = s3_cfg(None, {}, None, override)
            if not cfg.get("inputBucket") or not cfg.get("outputBucket"):
                raise UserError("Enter the bucket names first.")
            store = Store(cfg)
    return {"results": await asyncio.to_thread(store.test)}


@app.post("/api/s3/files")
async def s3_files(body: dict):
    async with Kube(body.get("conn")) as k:
        store = await store_for(k)
    out = await asyncio.to_thread(store.files)
    out.update({"inputBucket": store.cfg["inputBucket"], "inputPrefix": store.cfg["inputPrefix"],
                "outputBucket": store.cfg["outputBucket"], "outputPrefix": store.cfg["outputPrefix"]})
    return out


@app.post("/api/s3/upload")
async def s3_upload(conn: str = Form(...), files: list[UploadFile] = File(...)):
    async with Kube(json.loads(conn)) as k:
        store = await store_for(k)
    names = []
    for f in files:
        names.append(await asyncio.to_thread(store.upload, f.filename or "recording", f.file))
    return {"uploaded": names}


@app.post("/api/s3/delete")
async def s3_delete(body: dict):
    async with Kube(body.get("conn")) as k:
        store = await store_for(k)
    await asyncio.to_thread(store.delete_recording, str(body.get("name") or ""), bool(body.get("withResults", True)))
    return {"ok": True}


@app.post("/api/s3/result")
async def s3_result(body: dict):
    async with Kube(body.get("conn")) as k:
        store = await store_for(k)
    data, ctype = await asyncio.to_thread(store.result, str(body.get("name") or ""), str(body.get("ext") or ""))
    return Response(data, media_type="application/octet-stream" if body.get("download") else "text/plain; charset=utf-8")


# -------------------------------------------------------------- models ----
async def replace_job(k, job):
    await k.run("delete", "job", job["metadata"]["name"], "--ignore-not-found", "--wait=true", timeout=90)
    await k.apply(job)


@app.post("/api/models/check")
async def models_check(body: dict):
    async with Kube(body.get("conn")) as k:
        got = by_kind(await k.items(f"configmap/{m.CONFIG_CM}", f"configmap/{m.SETTINGS_CM}", f"persistentvolumeclaim/{m.PVC}"))
        if m.PVC not in got.get("PersistentVolumeClaim", {}):
            raise UserError("Connect the model storage first (Setup page).")
        cms = got.get("ConfigMap", {})
        d = (cms.get(m.CONFIG_CM) or {}).get("data") or {}
        await replace_job(k, m.model_check_job(k.ns, load_settings(cms.get(m.SETTINGS_CM)),
                                               d.get("WHISPER_MODEL") or "large-v3",
                                               (d.get("DIARIZE") or "true") == "true"))
    return {"ok": True}


@app.post("/api/models/download")
async def models_download(body: dict):
    token = clean(body.get("hfToken"))
    async with Kube(body.get("conn")) as k:
        got = by_kind(await k.items(f"secret/{m.HF_SECRET}", f"persistentvolumeclaim/{m.PVC}"))
        if m.PVC not in got.get("PersistentVolumeClaim", {}):
            raise UserError("Connect the model storage first (Setup page).")
        if token:
            await k.apply(m.secret(k.ns, m.HF_SECRET, {"token": token}))
        elif m.HF_SECRET not in got.get("Secret", {}):
            raise UserError("A Hugging Face token is needed to download the models.")
        cm, job = m.model_download(k.ns)
        await k.apply(cm)
        await replace_job(k, job)
    return {"ok": True}


# ---------------------------------------------------------------- runs ----
@app.post("/api/runs")
async def runs(body: dict):
    async with Kube(body.get("conn")) as k:
        got = by_kind(await k.items("job,pod", "-l", f"app={m.APP_LABEL}"))
    pods = list(got.get("Pod", {}).values())
    views = [job_view(j, pods) for j in got.get("Job", {}).values()]
    views.sort(key=lambda v: v["created"] or "", reverse=True)
    return {"runs": views}


@app.post("/api/runs/start")
async def runs_start(body: dict):
    async with Kube(body.get("conn")) as k:
        got = by_kind(await k.items("configmap,secret,persistentvolumeclaim,job"))
        cms = got.get("ConfigMap", {})
        missing = []
        if m.CONFIG_CM not in cms:
            missing.append("pipeline settings")
        if m.S3_SECRET not in got.get("Secret", {}):
            missing.append("object storage keys")
        if m.PVC not in got.get("PersistentVolumeClaim", {}):
            missing.append("model storage")
        if missing:
            raise UserError("Setup is not complete yet. Missing: " + ", ".join(missing) + ".")
        active = [j["metadata"]["name"] for j in got.get("Job", {}).values()
                  if (j["metadata"].get("labels") or {}).get("app") == m.APP_LABEL
                  and job_view(j, [])["state"] not in ("succeeded", "failed")]
        if active and not body.get("force"):
            raise UserError(f"A run is already in progress ({active[0]}). Wait for it to finish or stop it first.")
        job = await k.create(m.transcribe_job(k.ns, load_settings(cms.get(m.SETTINGS_CM)),
                                              (cms[m.CONFIG_CM].get("data") or {}).get("S3_ENDPOINT_URL")))
    return {"name": job["metadata"]["name"]}


@app.post("/api/runs/delete")
async def runs_delete(body: dict):
    name = str(body.get("name") or "")
    if not JOB_RE.match(name):
        raise UserError("Unknown run.")
    async with Kube(body.get("conn")) as k:
        await k.run("delete", "job", name, "--ignore-not-found", "--wait=false")
    return {"ok": True}


@app.post("/api/logs")
async def logs(body: dict):
    name = str(body.get("name") or "")
    if not JOB_RE.match(name):
        raise UserError("Unknown run.")
    async with Kube(body.get("conn")) as k:
        text = await k.run("logs", f"job/{check_name(name)}", "--tail=4000", check=False, timeout=60)
    return {"log": text}


# -------------------------------------------------------------- static ----
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC), name="static")
