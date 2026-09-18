from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
import zipfile
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    if request.path.startswith("/api/"):
        return jsonify(error="The server could not complete that request."), 500
    raise error


def normalize_playlist_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        return normalized

    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized

    query = parse_qs(parsed.query, keep_blank_values=True)
    playlist_id = None
    for key, values in query.items():
        if key == "list":
            playlist_id = values[0] if values else None
            break

    if playlist_id:
        host = parsed.netloc.lower()
        canonical_host = host
        if host == "youtu.be":
            canonical_host = "www.youtube.com"
        elif host.startswith("music."):
            canonical_host = host
        elif host.startswith("m."):
            canonical_host = host.replace("m.", "www.", 1)
        return f"https://{canonical_host}/playlist?list={playlist_id}"

    return normalized


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)


def progress_hook(job_id: str, data: dict[str, Any]) -> None:
    status = data.get("status")
    if status == "downloading":
        downloaded = data.get("downloaded_bytes", 0)
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        percent = round(downloaded * 100 / total) if total else 0
        update_job(job_id, status="downloading", progress=min(percent, 99), detail=f"Downloading {data.get('filename', 'video').split('/')[-1]}")
    elif status == "finished":
        update_job(job_id, detail="Processing downloaded file...")


def has_audio_stream(file_path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "audio" in result.stdout


def is_final_media_file(file_path: Path) -> bool:
    parts = file_path.name.split(".")
    return ".temp" not in file_path.name and not any(part.startswith("f") and part[1:].isdigit() for part in parts[:-1])


def safe_archive_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return cleaned or "youtube-playlist"


def run_download(job_id: str, url: str, output_dir: Path, media_format: str, quality: str) -> None:
    height = {"1080p": 1080, "720p": 720, "480p": 480}.get(quality, 1080)
    ffmpeg_path = shutil.which("ffmpeg")
    if media_format == "mp3":
        format_selector = "bestaudio/best"
        postprocessors = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        format_selector = f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/best[ext=mp4][height<={height}]/best[height<={height}]/best"
        postprocessors = []

    options = {
        "format": format_selector,
        "outtmpl": str(output_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
        "noplaylist": False,
        "ignoreerrors": True,
        "merge_output_format": "mp4",
        "keepvideo": False,
        "prefer_ffmpeg": True,
        "ffmpeg_location": ffmpeg_path,
        "progress_hooks": [lambda data: progress_hook(job_id, data)],
        "postprocessors": postprocessors,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        update_job(job_id, status="downloading", detail="Starting downloads...", progress=1)
        with yt_dlp.YoutubeDL(options) as downloader:
            playlist_info = downloader.extract_info(url, download=False)
            playlist_name = str(playlist_info.get("title") or "youtube-playlist")
            downloader.download([url])
        media_files = [file_path for file_path in output_dir.rglob("*") if file_path.is_file() and not file_path.name.endswith(('.part', '.ytdl'))]
        if media_format == "mp4":
            media_files = [file_path for file_path in media_files if file_path.suffix.lower() == ".mp4" and is_final_media_file(file_path) and has_audio_stream(file_path)]
        else:
            media_files = [file_path for file_path in media_files if file_path.suffix.lower() == ".mp3" and is_final_media_file(file_path)]
        if not media_files:
            available_files = ", ".join(file_path.name for file_path in output_dir.rglob("*") if file_path.is_file())
            detail = "No playable files were produced."
            if available_files:
                detail += f" Downloaded files: {available_files}"
            raise RuntimeError(detail)
        archive_name = safe_archive_name(playlist_name)
        archive_path = DOWNLOAD_DIR / f"{archive_name}-{job_id}.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in media_files:
                archive.write(file_path, Path(archive_name) / file_path.name)
        update_job(job_id, status="complete", detail="Playlist ready", progress=100, file=str(archive_path), download_name=f"{archive_name}.zip")
    except Exception as error:
        update_job(job_id, status="error", detail=str(error), error="Download failed")
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "yt.html")


@app.post("/api/playlist")
def inspect_playlist():
    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url", "")).strip()
    url = normalize_playlist_url(raw_url)
    if "list=" not in url or not ("youtube.com" in url or "youtu.be" in url or "music.youtube.com" in url):
        return jsonify(error="Enter a public YouTube playlist URL."), 400
    try:
        options = {"extract_flat": True, "quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        entries = [entry for entry in (info.get("entries") or []) if entry]
        return jsonify(title=info.get("title") or "YouTube playlist", count=len(entries))
    except Exception:
        return jsonify(error="Could not read that playlist. Make sure it is public and the URL is correct."), 422


@app.post("/api/download")
def create_download():
    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url", "")).strip()
    url = normalize_playlist_url(raw_url)
    media_format = payload.get("format", "mp4")
    quality = payload.get("quality", "1080p")
    if "list=" not in url or not ("youtube.com" in url or "youtu.be" in url or "music.youtube.com" in url):
        return jsonify(error="Enter a public YouTube playlist URL."), 400
    if media_format not in {"mp4", "mp3"}:
        return jsonify(error="Unsupported download format."), 400
    if not shutil.which("ffmpeg"):
        return jsonify(error="FFmpeg is required to combine video and audio. Install FFmpeg, add it to PATH, then restart the server."), 503
    job_id = uuid.uuid4().hex
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir()
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "detail": "Preparing playlist..."}
    worker = threading.Thread(target=run_download, args=(job_id, url, output_dir, media_format, quality), daemon=True)
    worker.start()
    return jsonify(job_id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Download job not found."), 404
    response = {key: value for key, value in job.items() if key not in {"file", "download_name"}}
    if job.get("status") == "complete":
        response["download_url"] = f"/api/jobs/{job_id}/file"
    return jsonify(response)


@app.get("/api/jobs/<job_id>/file")
def download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "complete":
        return jsonify(error="The download is not ready yet."), 404
    return send_file(job["file"], as_attachment=True, download_name=job.get("download_name", "youtube-playlist.zip"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)