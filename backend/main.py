import copy
import hmac
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# Support both `uvicorn backend.main:app` (project root) and
# `uvicorn main:app` (backend directory).
try:
    from .audio_analysis import (
        analyze_audio_for_aaec,
        analyze_audio_readiness,
    )
except ImportError:
    from audio_analysis import (
        analyze_audio_for_aaec,
        analyze_audio_readiness,
    )

# --------------------------------------------------
# Configuration
# --------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

WORKFLOW_PATH = ROOT_DIR / "workflows" / "float_api.json"

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
API_KEY = os.getenv("FLOAT_API_KEY", "").strip()
REQUEST_TIMEOUT = 30
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file
MAX_ACTIVE_JOBS = 2
SUBMISSION_LOCK = threading.Lock()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
INTENSITY_MODES = {"manual", "automatic"}

EMOTIONS = {
    "none",
    "angry",
    "disgust",
    "fear",
    "happy",
    "neutral",
    "sad",
    "surprise",
}

# Temporary in-memory job storage.
# Jobs will be cleared if the backend is restarted.
JOBS: dict[str, dict[str, Any]] = {}


# --------------------------------------------------
# FastAPI application
# --------------------------------------------------

app = FastAPI(
    title="SentioGaurdAI Backend",
    description="Backend API for emotion-controlled talking-head generation.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Allow API access only to the trusted Cloudflare Worker/client."""
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Backend API key is not configured.",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
        )


def validate_audio_upload(audio: UploadFile) -> None:
    """Validate the uploaded audio filename extension."""
    if not audio.filename or Path(audio.filename).suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Upload a WAV, MP3, M4A, FLAC, or OGG audio file.",
        )


def read_limited_upload(upload: UploadFile, label: str) -> bytes:
    """Read an upload while enforcing the configured size limit."""
    content = upload.file.read(MAX_FILE_SIZE + 1)
    if not content:
        raise HTTPException(
            status_code=400,
            detail=f"The uploaded {label} file is empty.",
        )
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"The uploaded {label} file must be 50 MB or smaller.",
        )
    return content


def check_comfyui() -> bool:
    try:
        response = requests.get(
            f"{COMFYUI_URL}/system_stats",
            timeout=REQUEST_TIMEOUT,
        )
        return response.ok
    except requests.RequestException:
        return False


def load_workflow() -> dict:
    if not WORKFLOW_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Workflow file not found: {WORKFLOW_PATH}",
        )

    try:
        import json

        with WORKFLOW_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read workflow: {exc}",
        )


def upload_to_comfyui(filename: str, content: bytes) -> str:
    unique_filename = f"{uuid.uuid4().hex}_{Path(filename).name}"

    try:
        response = requests.post(
            f"{COMFYUI_URL}/upload/image",
            files={
                "image": (
                    unique_filename,
                    content,
                    "application/octet-stream",
                )
            },
            data={
                "type": "input",
                "subfolder": "",
                "overwrite": "true",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()

        if "name" not in result:
            raise ValueError("ComfyUI did not return an uploaded filename.")

        return result["name"]

    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI file upload failed: {exc}",
        )


def extract_video_metadata(history_entry: dict) -> dict | None:
    outputs = history_entry.get("outputs", {})

    # The workflow's VHS_VideoCombine node is node 6.
    preferred_output = outputs.get("6", {})

    for key in ("gifs", "videos"):
        files = preferred_output.get(key, [])
        for item in files:
            if item.get("filename"):
                return item

    # Fallback: search all workflow outputs.
    for node_output in outputs.values():
        for key in ("gifs", "videos"):
            for item in node_output.get(key, []):
                if item.get("filename"):
                    return item

    return None


def get_history_entry(prompt_id: str) -> dict | None:
    try:
        response = requests.get(
            f"{COMFYUI_URL}/history/{prompt_id}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        history = response.json()
        return history.get(prompt_id)
    except requests.RequestException:
        return None


def get_job_status(prompt_id: str) -> dict:
    history_entry = get_history_entry(prompt_id)

    if history_entry:
        video = extract_video_metadata(history_entry)

        if video:
            JOBS[prompt_id]["status"] = "completed"
            JOBS[prompt_id]["video"] = video

            return {
                "job_id": prompt_id,
                "status": "completed",
                "video_url": f"/api/generate/{prompt_id}/video",
            }

        status_info = history_entry.get("status", {})
        messages = status_info.get("messages", [])

        for message in messages:
            if (
                isinstance(message, list)
                and len(message) > 1
                and message[0] == "execution_error"
            ):
                JOBS[prompt_id]["status"] = "failed"
                JOBS[prompt_id]["error"] = str(message[1])
                break

        if JOBS[prompt_id]["status"] == "failed":
            return {
                "job_id": prompt_id,
                "status": "failed",
                "error": JOBS[prompt_id].get("error"),
            }

    # Check whether ComfyUI still has this prompt queued or running.
    try:
        response = requests.get(
            f"{COMFYUI_URL}/queue",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        queue = response.json()

        for entry in queue.get("queue_running", []):
            if len(entry) > 1 and entry[1] == prompt_id:
                JOBS[prompt_id]["status"] = "processing"
                return {"job_id": prompt_id, "status": "processing"}

        for entry in queue.get("queue_pending", []):
            if len(entry) > 1 and entry[1] == prompt_id:
                JOBS[prompt_id]["status"] = "queued"
                return {"job_id": prompt_id, "status": "queued"}

    except requests.RequestException:
        pass

    current_status = JOBS[prompt_id].get("status", "queued")

    return {
        "job_id": prompt_id,
        "status": current_status,
        "error": JOBS[prompt_id].get("error"),
    }


# --------------------------------------------------
# API endpoints
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "project": "SentioGaurdAI",
        "message": "Emotion-controlled talking-head generation API",
    }


@app.get("/api/health", dependencies=[Depends(require_api_key)])
def health():
    comfyui_online = check_comfyui()

    return {
        "backend": "online",
        "comfyui": "online" if comfyui_online else "offline",
        "workflow": WORKFLOW_PATH.exists(),
        "audio_analysis": "available",
    }


@app.post("/api/analyze-audio", dependencies=[Depends(require_api_key)])
async def analyze_audio_endpoint(audio: UploadFile = File(...)):
    """Return AAEC intensity advice and technical audio-readiness information."""
    validate_audio_upload(audio)
    audio_content = read_limited_upload(audio, "audio")

    try:
        aaec_result = analyze_audio_for_aaec(audio_content)
        readiness_result = analyze_audio_readiness(audio_content)

        return {
            "aaec": aaec_result,
            "audio_readiness": readiness_result,
            "notice": (
                "The recommended intensity is an acoustic heuristic based on "
                "energy and pitch variation; it is not speech-emotion recognition."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Invalid, unsupported, or corrupted audio should return a client error.
        raise HTTPException(
            status_code=400,
            detail=f"Audio analysis failed. Check that the audio file is valid: {exc}",
        )


@app.post("/api/generate", dependencies=[Depends(require_api_key)])
async def generate_video(
    image: UploadFile = File(...),
    audio: UploadFile = File(...),
    emotion: str = Form(...),
    intensity: float = Form(7.0),
    intensity_mode: str = Form("manual"),
):
    if emotion not in EMOTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid emotion. Choose from: {sorted(EMOTIONS)}",
        )

    if intensity_mode not in INTENSITY_MODES:
        raise HTTPException(
            status_code=400,
            detail="intensity_mode must be either 'manual' or 'automatic'.",
        )

    # Validate manual intensity even when automatic mode is selected,
    # keeping the existing form field safe and predictable.
    if not 1 <= intensity <= 10:
        raise HTTPException(
            status_code=400,
            detail="Intensity must be between 1 and 10.",
        )

    if not image.filename or Path(image.filename).suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Upload a PNG, JPG, JPEG, or WEBP facial image.",
        )

    validate_audio_upload(audio)

    image_content = read_limited_upload(image, "image")
    audio_content = read_limited_upload(audio, "audio")

    # Manual remains the default. Automatic mode computes its value on the
    # server from the actual uploaded audio; the client cannot supply a score.
    aaec_result = None
    readiness_result = None
    selected_intensity = float(intensity)

    if intensity_mode == "automatic":
        try:
            aaec_result = analyze_audio_for_aaec(audio_content)
            readiness_result = analyze_audio_readiness(audio_content)
            selected_intensity = float(aaec_result["recommended_intensity"])
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Automatic intensity analysis failed. Use manual mode or upload valid audio: {exc}",
            )

    if not 1 <= selected_intensity <= 10:
        raise HTTPException(
            status_code=400,
            detail="Calculated intensity must be between 1 and 10.",
        )

    if not check_comfyui():
        raise HTTPException(
            status_code=503,
            detail="ComfyUI is not running. Start ComfyUI on port 8188.",
        )

    if not SUBMISSION_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Another generation request is being submitted. Please try again shortly.",
        )

    try:
        # Refresh existing job states before enforcing the GPU queue limit.
        for existing_id in list(JOBS):
            if JOBS[existing_id].get("status") in {"queued", "processing"}:
                get_job_status(existing_id)

        active_jobs = sum(
            1 for job in JOBS.values()
            if job.get("status") in {"queued", "processing"}
        )
        if active_jobs >= MAX_ACTIVE_JOBS:
            raise HTTPException(
                status_code=429,
                detail="The GPU generation queue is full. Please wait for a current generation to finish.",
            )

        workflow = load_workflow()
        image_name = upload_to_comfyui(image.filename, image_content)
        audio_name = upload_to_comfyui(audio.filename, audio_content)

        # Update the inputs in a separate copy of the workflow.
        prompt = copy.deepcopy(workflow)

        prompt["3"]["inputs"]["image"] = image_name
        prompt["4"]["inputs"]["audio"] = audio_name

        prompt["10"]["inputs"]["emotion"] = emotion
        prompt["10"]["inputs"]["e_cfg_scale"] = selected_intensity

        # Keep the tested project settings.
        prompt["10"]["inputs"]["a_cfg_scale"] = 2.0
        prompt["10"]["inputs"]["r_cfg_scale"] = 1.0
        prompt["10"]["inputs"]["crop"] = True

        prompt["11"]["inputs"]["value"] = 25

        client_id = str(uuid.uuid4())

        response = requests.post(
            f"{COMFYUI_URL}/prompt",
            json={
                "prompt": prompt,
                "client_id": client_id,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get("prompt_id"):
            raise ValueError(
                f"ComfyUI did not return a prompt ID: {result}"
            )

        prompt_id = result["prompt_id"]

        JOBS[prompt_id] = {
            "status": "queued",
            "emotion": emotion,
            "intensity": selected_intensity,
            "requested_intensity": float(intensity),
            "intensity_mode": intensity_mode,
            "aaec": aaec_result,
            "audio_readiness": readiness_result,
            "video": None,
            "error": None,
        }

        return {
            "job_id": prompt_id,
            "status": "queued",
            "message": "Video generation has been submitted to FLOAT.",
            "intensity_mode": intensity_mode,
            "selected_intensity": selected_intensity,
            "aaec": aaec_result,
            "audio_readiness": readiness_result,
        }

    except HTTPException:
        raise

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not communicate with ComfyUI: {exc}",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Video generation request failed: {exc}",
        )
    finally:
        SUBMISSION_LOCK.release()


@app.get("/api/generate/{job_id}", dependencies=[Depends(require_api_key)])
def generation_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(
            status_code=404,
            detail="Generation job not found. The backend may have restarted.",
        )

    return get_job_status(job_id)



@app.post("/api/recover/{job_id}", dependencies=[Depends(require_api_key)])
def recover_completed_job(job_id: str):
    if job_id in JOBS:
        return get_job_status(job_id)

    history_entry = get_history_entry(job_id)

    if not history_entry:
        raise HTTPException(
            status_code=404,
            detail="Job not found in ComfyUI history.",
        )

    JOBS[job_id] = {
        "status": "queued",
        "emotion": "unknown",
        "intensity": None,
        "requested_intensity": None,
        "intensity_mode": "unknown",
        "aaec": None,
        "audio_readiness": None,
        "video": None,
        "error": None,
    }

    status = get_job_status(job_id)

    if status["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail="The existing generation is not completed.",
        )

    return status



@app.get(
    "/api/generate/{job_id}/video",
    dependencies=[Depends(require_api_key)],
)
def get_generated_video(job_id: str):
    """Recover a completed job if necessary, then stream its video."""

    # Recover the job if FastAPI has restarted.
    if job_id not in JOBS:
        history_entry = get_history_entry(job_id)

        if not history_entry:
            raise HTTPException(
                status_code=404,
                detail="Job not found in ComfyUI history.",
            )

        JOBS[job_id] = {
            "status": "queued",
            "emotion": "unknown",
            "intensity": None,
            "requested_intensity": None,
            "intensity_mode": "unknown",
            "aaec": None,
            "audio_readiness": None,
            "video": None,
            "error": None,
        }

    # Refresh status and retrieve video metadata.
    status = get_job_status(job_id)

    if status.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail="The video is not ready yet.",
        )

    video = JOBS[job_id].get("video")

    if not video or not video.get("filename"):
        raise HTTPException(
            status_code=404,
            detail="Generated video metadata not found.",
        )

    try:
        response = requests.get(
            f"{COMFYUI_URL}/view",
            params={
                "filename": video["filename"],
                "subfolder": video.get("subfolder", ""),
                "type": video.get("type", "output"),
            },
            stream=True,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", "video/mp4"
        )

        def stream_video():
            try:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        yield chunk
            finally:
                response.close()

        return StreamingResponse(
            stream_video(),
            media_type=content_type,
            headers={
                "Content-Disposition":
                    'inline; filename="sentio_generated.mp4"'
            },
        )

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not retrieve generated video from ComfyUI: {exc}",
        )
