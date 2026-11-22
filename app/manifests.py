"""Builds the Kubernetes objects of the WhisperX pipeline from UI settings.

Object names match https://github.com/halsayed/whisperx-pipeline so a
namespace set up by hand and one set up by this app look the same.
"""
import base64
import json
from pathlib import Path
from urllib.parse import urlparse

APP_LABEL = "whisperx-transcribe"
CONFIG_CM = "whisperx-config"
SETTINGS_CM = "whisperx-ui-settings"
S3_SECRET = "s3-credentials"
PULL_SECRET = "dockerhub-pull"
HF_SECRET = "hf-token"
PVC = "voice-models"
NETPOL = "whisperx-transcribe-egress"
DOWNLOAD_JOB = "model-download"
CHECK_JOB = "model-check"
MANAGED = {"app.kubernetes.io/managed-by": "whisperx-ui"}

DEFAULT_IMAGE = "docker.io/aksiio/whisperx-worker:3.7.9-1"

# (path below /models, needed for, required)
MODEL_PATHS = [
    ("hf/hub/models--Systran--faster-whisper-large-v3", "Transcription model large-v3", "large-v3"),
    ("hf/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo", "Transcription model large-v3-turbo", "large-v3-turbo"),
    ("hf/hub/models--jonatasgrosman--wav2vec2-large-xlsr-53-arabic", "Word timing, Arabic", "always"),
    ("torch/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth", "Word timing, English", "always"),
    ("hf/hub/models--pyannote--speaker-diarization-3.1", "Speaker detection", "diarize"),
    ("hf/hub/models--pyannote--segmentation-3.0", "Speaker detection (segmentation)", "diarize"),
    ("hf/hub/models--pyannote--wespeaker-voxceleb-resnet34-LM", "Speaker detection (voice print)", "diarize"),
]


def meta(name, ns=None, labels=None):
    m = {"name": name, "labels": {**MANAGED, **(labels or {})}}
    if ns:
        m["namespace"] = ns
    return m


def namespace(ns):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}


def pv_name(ns):
    return f"{ns}-voice-models"


def storage(ns, server, path):
    pv = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": meta(pv_name(ns)),
        "spec": {
            "capacity": {"storage": "50Gi"},
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "mountOptions": ["nfsvers=4", "hard"],
            "nfs": {"server": server, "path": path},
        },
    }
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": meta(PVC, ns),
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "storageClassName": "",
            "volumeName": pv_name(ns),
            "resources": {"requests": {"storage": "50Gi"}},
        },
    }
    return pv, pvc


def secret(ns, name, data, type_="Opaque"):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": meta(name, ns),
        "type": type_,
        "stringData": data,
    }


def pull_secret(ns, server, user, token):
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    cfg = {"auths": {server: {"username": user, "password": token, "auth": auth}}}
    return secret(ns, PULL_SECRET, {".dockerconfigjson": json.dumps(cfg)}, "kubernetes.io/dockerconfigjson")


def config_map(ns, name, data):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(name, ns), "data": data}


def endpoint_host_port(endpoint):
    u = urlparse(endpoint)
    return u.hostname, u.port or (80 if u.scheme == "http" else 443)


def egress_policy(ns, ip, port):
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta(NETPOL, ns),
        "spec": {
            "podSelector": {"matchLabels": {"app": APP_LABEL}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
                {"to": [{"ipBlock": {"cidr": f"{ip}/32"}}], "ports": [{"protocol": "TCP", "port": port}]},
            ],
        },
    }


def _pod_base(settings):
    spec = {
        "restartPolicy": "Never",
        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists"}],
        "imagePullSecrets": [{"name": PULL_SECRET}],
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True},
    }
    if settings.get("gpuProduct"):
        spec["nodeSelector"] = {"nvidia.com/gpu.product": settings["gpuProduct"]}
    return spec


def transcribe_job(ns, settings, endpoint):
    spec = _pod_base(settings)
    host, _ = endpoint_host_port(endpoint or "")
    if settings.get("s3HostIp") and host:
        spec["hostAliases"] = [{"ip": settings["s3HostIp"], "hostnames": [host]}]
    spec["containers"] = [
        {
            "name": "worker",
            "image": settings.get("image") or DEFAULT_IMAGE,
            "envFrom": [{"configMapRef": {"name": CONFIG_CM}}, {"secretRef": {"name": S3_SECRET}}],
            "resources": {
                "requests": {"cpu": str(settings.get("cpu") or "4"), "memory": settings.get("memory") or "12Gi"},
                "limits": {"nvidia.com/gpu": 1, "memory": settings.get("memoryLimit") or "24Gi"},
            },
            "volumeMounts": [
                {"name": "models", "mountPath": "/models", "readOnly": True},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
    ]
    spec["volumes"] = [
        {"name": "models", "persistentVolumeClaim": {"claimName": PVC, "readOnly": True}},
        {"name": "tmp", "emptyDir": {}},
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"generateName": "whisperx-transcribe-", "namespace": ns, "labels": {**MANAGED, "app": APP_LABEL}},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 604800,
            "template": {"metadata": {"labels": {"app": APP_LABEL}}, "spec": spec},
        },
    }


def model_check_job(ns, settings, model, diarize):
    """Looks at the model share with the worker image (no extra image needed,
    and it pre-pulls the worker image on the GPU node)."""
    lines = ["rc=0"]
    for path, label, need in MODEL_PATHS:
        required = need == "always" or need == model or (need == "diarize" and diarize)
        fail = "rc=1" if required else "true"
        tag = "MISSING" if required else "OPTIONAL-MISSING"
        lines.append(f'if [ -e "/models/{path}" ]; then echo "FOUND {label}"; else echo "{tag} {label}"; {fail}; fi')
    lines += ['echo "---"', "cat /models/MANIFEST.txt 2>/dev/null || true", "exit $rc"]
    spec = _pod_base(settings)
    spec["containers"] = [
        {
            "name": "check",
            "image": settings.get("image") or DEFAULT_IMAGE,
            "command": ["sh", "-c", "\n".join(lines)],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}],
        }
    ]
    spec["volumes"] = [{"name": "models", "persistentVolumeClaim": {"claimName": PVC, "readOnly": True}}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": meta(CHECK_JOB, ns),
        "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": {"app": CHECK_JOB}}, "spec": spec}},
    }


def model_download(ns):
    script = (Path(__file__).parent / "download_models.py").read_text()
    cm = config_map(ns, "model-download-script", {"download_models.py": script})
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": meta(DOWNLOAD_JOB, ns),
        "spec": {
            "backoffLimit": 1,
            "template": {
                "metadata": {"labels": {"app": DOWNLOAD_JOB}},
                "spec": {
                    "restartPolicy": "Never",
                    # Nutanix Files and similar exports squash root; write as a normal user.
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                    "containers": [
                        {
                            "name": "download",
                            "image": "python:3.12-slim",
                            "command": ["sh", "-c"],
                            "args": [
                                'pip install --quiet --no-cache-dir --user "huggingface_hub>=0.34" && '
                                "python /scripts/download_models.py && "
                                'echo "--- share usage ---" && du -sh /models/hf/hub/* /models/torch/hub/checkpoints/*'
                            ],
                            "env": [
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "HF_HOME", "value": "/models/hf"},
                                {"name": "TORCH_HOME", "value": "/models/torch"},
                                {"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": HF_SECRET, "key": "token"}}},
                            ],
                            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "4Gi"}},
                            "volumeMounts": [
                                {"name": "models", "mountPath": "/models"},
                                {"name": "script", "mountPath": "/scripts"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "models", "persistentVolumeClaim": {"claimName": PVC}},
                        {"name": "script", "configMap": {"name": "model-download-script"}},
                    ],
                },
            },
        },
    }
    return cm, job
