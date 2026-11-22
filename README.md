# WhisperX Pipeline UI

A small web app that deploys, runs and monitors the
[WhisperX voice-to-text pipeline](https://github.com/halsayed/whisperx-pipeline)
on Kubernetes, for people who do not use `kubectl`.

You give it a kubeconfig and fill in one form. It creates everything the
pipeline needs in the cluster, lets you upload recordings, start a
transcription run, watch its progress, and read or download the results.

```
 Browser ──► WhisperX Pipeline UI ──kubectl──► your cluster  (namespace, secrets, config, Jobs)
 (holds the      (stateless)       ──S3─────► your buckets  (recordings in, transcripts out)
  kubeconfig)
```

## Quick start

**On a cluster**

```bash
kubectl apply -f https://raw.githubusercontent.com/halsayed/whisperx-ui/main/deploy/whisperx-ui.yaml
```

```bash
kubectl -n whisperx-ui port-forward svc/whisperx-ui 8080:80
```

Open <http://localhost:8080>. To give it an address on your network instead of
a port-forward, change the Service type to `LoadBalancer` in
[deploy/whisperx-ui.yaml](deploy/whisperx-ui.yaml).

**Or on any machine with Docker** (the UI does not have to run in the cluster
it manages):

```bash
docker run --rm -p 8080:8080 aksiio/whisperx-ui:0.1.0
```

## Using it

1. **Connect.** Drop the kubeconfig of the target cluster on the page and pick
   a namespace (default `whisperx`).
2. **Setup.** One form, four parts:
   - *Model storage*: the NFS server and share that hold the speech models.
     A button checks that the models are there; another downloads them (needs
     internet and a Hugging Face token, one time only).
   - *Object storage*: S3 address, keys, the recordings bucket and folder, the
     results bucket and folder. **Test connection** checks read and write.
   - *Worker image*: the image name, and a registry sign-in if it is private.
   - *GPU and transcription*: GPU type (read from the cluster, with how many
     are free), model, language, speaker labels. Picking a GPU fills in the
     recommended precision and batch size.

   **Save and apply** writes it all to the cluster and lists what was done.
3. **Recordings.** Drag audio files in to upload them. Each recording shows
   *Waiting* or *Transcribed*; transcribed ones can be read on the page or
   downloaded as text, JSON, SRT or VTT.
4. **Runs.** **Start transcription** starts a run. The page shows whether it
   is waiting (and why, in plain words: GPU busy, image still downloading,
   wrong registry sign-in), a progress bar, a line per recording, and the full
   log on request. Runs can be stopped.

## What "stateless" means here

- The server keeps nothing between requests: no database, no sessions, no
  files. It can be restarted or scaled to several replicas at any time.
- The kubeconfig stays in the browser (forgotten when the tab closes, unless
  *Remember on this browser* is ticked) and is sent with each request. The
  server writes it to an in-memory temp file for that one `kubectl` call and
  deletes it.
- Everything else lives in the target cluster, under the same names the
  pipeline's own manifests use, so a namespace set up by hand and one set up
  here are interchangeable:

| Object | Name | Holds |
|---|---|---|
| ConfigMap | `whisperx-config` | S3 address, buckets, model options (the worker's environment) |
| ConfigMap | `whisperx-ui-settings` | Image, GPU type, storage IP, network lock-down choice |
| Secret | `s3-credentials` | S3 access key and secret key |
| Secret | `dockerhub-pull` | Registry sign-in (optional) |
| Secret | `hf-token` | Hugging Face token (only if models are downloaded from the UI) |
| PV / PVC | `<namespace>-voice-models` / `voice-models` | The NFS model share |
| NetworkPolicy | `whisperx-transcribe-egress` | Optional: runs may reach only S3 and DNS |
| Job | `whisperx-transcribe-xxxxx` | One per run (kept for a week) |
| Job | `model-check`, `model-download` | The model check and download |

## What is in the image

`python:3.12-slim`, `kubectl` 1.36, and a FastAPI app (`boto3` for S3). It runs
as a non-root user with a read-only root filesystem. About 350 MB.

| Setting (environment variable) | Meaning |
|---|---|
| `UI_USERNAME`, `UI_PASSWORD` | If both are set, the UI asks for this sign-in (HTTP basic) |
| `ALLOW_IN_CLUSTER` | `true` adds a "Use the cluster this app runs in" button; see below |

## Security notes

- **The UI has no user accounts.** Anyone who can open it can use it, though
  only with a kubeconfig of their own. Keep it on a trusted network, put it
  behind your ingress authentication, or set `UI_USERNAME` / `UI_PASSWORD`.
  Serve it over HTTPS when it is not on localhost, because kubeconfigs and
  keys pass through it.
- Kubeconfigs are rebuilt from a short list of allowed fields before use.
  Ones that sign in by running a program (`exec`, `auth-provider`) or that
  point to files on disk are refused, so a kubeconfig cannot make the server
  run or read anything. Export a self-contained one with
  `kubectl config view --raw --flatten --minify`.
- Secrets typed into the form go straight into Kubernetes Secrets. The page
  never shows them again (only the last four characters of the S3 key ID).
- The app connects to whatever cluster and S3 address a user enters, from
  wherever it runs. Do not expose it to people who should not reach those
  networks.
- **In-cluster mode** ([deploy/in-cluster-access.yaml](deploy/in-cluster-access.yaml))
  gives the pod its own permissions so users need no kubeconfig. Every visitor
  then acts with those permissions; use it only with a sign-in in front.

## Requirements on the cluster

The same as the pipeline: NVIDIA GPUs offered as `nvidia.com/gpu` (GPU
operator or DRA driver), an NFS export for the models, an S3-compatible
endpoint, and a worker image the cluster can pull. The kubeconfig needs rights
to create a namespace, a PersistentVolume, Secrets, ConfigMaps, Jobs and
NetworkPolicies, and to list nodes (for GPU types).

## Build

```bash
docker build --platform linux/amd64 -t <registry>/whisperx-ui:0.1.0 .
```

Run from source:

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --port 8080
```

(`kubectl` must be on the `PATH`.)

## Layout

| Path | Purpose |
|---|---|
| `app/main.py` | HTTP API |
| `app/kube.py` | kubeconfig clean-up and the `kubectl` wrapper |
| `app/manifests.py` | Builds the pipeline's Kubernetes objects from the form |
| `app/s3.py` | S3 test, upload, listing, results |
| `app/download_models.py` | Model download script (same as the pipeline's) |
| `app/static/` | The page: plain HTML, CSS and JavaScript, no build step |
| `deploy/` | Deployment + Service, and the optional in-cluster access |

## Tested (2026-11-22)

On NKP, Kubernetes 1.36, NVIDIA DRA driver, Nutanix Files (NFS) and Nutanix
Objects (S3): setup from an empty namespace with existing model share, S3
test, model check, upload of two recordings, two runs on an RTX A2000 (the
second found nothing left to do), reading results. Run both from a laptop
and from the Deployment in this repo. In-cluster mode with the sign-in option
was also checked (connect, setup, start a run, log, remove a run).

Not tested: the model download button, deleting recordings, clusters other
than NKP, and kubeconfigs with several contexts.
