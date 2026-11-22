import datetime, os, sys, urllib.request
from huggingface_hub import snapshot_download

# Skip TensorFlow / Flax weights; WhisperX only uses PyTorch / CTranslate2.
IGNORE = ["*.msgpack", "*.h5", "*.ot", "*.onnx"]

# (repo, required, purpose)
HF_MODELS = [
    ("Systran/faster-whisper-large-v3", True, "transcription (default)"),
    ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", False, "transcription (faster option)"),
    ("jonatasgrosman/wav2vec2-large-xlsr-53-arabic", True, "alignment: Arabic"),
    ("pyannote/speaker-diarization-3.1", True, "diarization pipeline (gated)"),
    ("pyannote/segmentation-3.0", True, "diarization segmentation (gated)"),
    ("pyannote/wespeaker-voxceleb-resnet34-LM", True, "diarization speaker embedding"),
    ("pyannote/speaker-diarization-community-1", False, "diarization, newer pyannote 4.x (gated)"),
]

# (url, required, purpose) -> $TORCH_HOME/hub/checkpoints/<filename>
TORCH_FILES = [
    ("https://download.pytorch.org/torchaudio/models/wav2vec2_fairseq_base_ls960_asr_ls960.pth",
     True, "alignment: English"),
]

lines, failed = [], []

for repo, required, purpose in HF_MODELS:
    try:
        path = snapshot_download(repo, ignore_patterns=IGNORE)
        rev = os.path.basename(path)
        print(f"OK    {repo} @ {rev}", flush=True)
        lines.append(f"hf     {repo}  rev={rev}  # {purpose}")
    except Exception as e:  # gated / network / not found
        kind = "FAIL " if required else "SKIP "
        print(f"{kind} {repo}: {type(e).__name__}: {str(e).splitlines()[0]}", flush=True)
        lines.append(f"hf     {repo}  NOT DOWNLOADED ({type(e).__name__})  # {purpose}")
        if required:
            failed.append(repo)

ckpt = os.path.join(os.environ["TORCH_HOME"], "hub", "checkpoints")
os.makedirs(ckpt, exist_ok=True)
for url, required, purpose in TORCH_FILES:
    dest = os.path.join(ckpt, os.path.basename(url))
    try:
        if not os.path.exists(dest):
            urllib.request.urlretrieve(url, dest + ".part")
            os.rename(dest + ".part", dest)
        print(f"OK    {url} ({os.path.getsize(dest)} bytes)", flush=True)
        lines.append(f"torch  {os.path.basename(url)}  bytes={os.path.getsize(dest)}  # {purpose}")
    except Exception as e:
        print(f"FAIL  {url}: {e}", flush=True)
        lines.append(f"torch  {os.path.basename(url)}  NOT DOWNLOADED  # {purpose}")
        if required:
            failed.append(url)

with open("/models/MANIFEST.txt", "w") as f:
    f.write(f"# written {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n")
    f.write("\n".join(lines) + "\n")

if failed:
    print("Required downloads failed:", ", ".join(failed))
    sys.exit(1)
print("All required models are on the share.")