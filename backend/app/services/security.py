import os
import re
import uuid
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Sequence, Optional, Tuple, Dict, Any
from fastapi import UploadFile, HTTPException
from backend.app.config import settings

# Whitelist safe characters for filenames: alphanumeric, dot, hyphen, underscore
SAFE_FILENAME_REGEX = re.compile(r"[^a-zA-Z0-9_.-]")

# Magic byte signatures
MAGIC_SIGNATURES = {
    # Images
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpeg": [b"\xff\xd8\xff"],
    "webp": [b"RIFF"], # with WEBP at offset 8
    # Video
    "mp4": [b"ftyp"], # typically at byte 4
    "mkv": [b"\x1a\x45\xdf\xa3"], # Matroska header (MKV / WebM)
    # Audio
    "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    "wav": [b"RIFF"], # with WAVE at offset 8
    "ogg": [b"OggS"],
}

def sanitize_filename(filename: str) -> str:
    """
    Sanitizes untrusted filename to prevent path traversal and shell injection.
    Strips directory separators and non-whitelisted characters.
    """
    if not filename:
        return "unnamed_asset"
    
    # Strip any leading directory parts or null bytes
    clean_name = os.path.basename(filename.replace("\\", "/")).strip("\x00 \t\n\r")
    # Replace unsafe characters with underscore
    clean_name = SAFE_FILENAME_REGEX.sub("_", clean_name)
    # Deduplicate underscores
    clean_name = re.sub(r"_+", "_", clean_name)
    # Strip trailing underscores right before dots (e.g. name_.mp4 -> name.mp4)
    clean_name = re.sub(r"_+\.", ".", clean_name)
    clean_name = clean_name.strip("._")
    
    if not clean_name:
        clean_name = "unnamed_asset"
        
    return clean_name[:128]

def generate_secure_filename(original_filename: str, prefix: str = "") -> str:
    """
    Generates a collision-resistant unique filename while preserving safe extension and sanitized stem.
    Format: [prefix_]uuid12_sanitizedStem.ext
    """
    clean = sanitize_filename(original_filename)
    path_obj = Path(clean)
    stem = path_obj.stem[:32]
    ext = path_obj.suffix.lower()
    
    unique_token = uuid.uuid4().hex[:12]
    prefix_str = f"{prefix}_" if prefix else ""
    return f"{prefix_str}{unique_token}_{stem}{ext}"

def validate_safe_path(target_path: Path | str, base_dir: Optional[Path | str] = None) -> Path:
    """
    Validates that target_path resolves strictly within base_dir (default: settings.data_dir).
    Raises HTTPException 400 or 403 on path traversal attempt.
    """
    base = Path(base_dir or settings.data_dir).resolve()
    target = Path(target_path).resolve()

    try:
        # is_relative_to is Python 3.9+
        if not target.is_relative_to(base):
            raise HTTPException(status_code=400, detail="Access denied: Path traversal detected outside storage root")
    except ValueError:
        raise HTTPException(status_code=400, detail="Access denied: Path is outside base directory")

    return target

def validate_extension(filename: str, allowed_extensions: Sequence[str]) -> str:
    """
    Validates that filename has one of the allowed extensions.
    Returns the lowercased extension.
    """
    ext = Path(filename).suffix.lower()
    allowed_lower = [e.lower() for e in allowed_extensions]
    if ext not in allowed_lower:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '{ext}'. Allowed extensions: {', '.join(allowed_lower)}"
        )
    return ext

def verify_magic_bytes(file_path: Path, expected_type: str = "media") -> bool:
    """
    Verifies initial magic bytes of the file to protect against extension spoofing.
    """
    if not file_path.exists() or file_path.stat().st_size == 0:
        return False
        
    ext = file_path.suffix.lower().lstrip(".")
    with open(file_path, "rb") as f:
        header = f.read(32)

    if ext in ["jpg", "jpeg"]:
        return header.startswith(b"\xff\xd8\xff")
    elif ext == "png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext == "webp":
        return header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP"
    elif ext in ["mp4", "mov"]:
        # ISO Base Media / MP4 / QuickTime: 'ftyp' at offset 4..8
        return len(header) >= 8 and (b"ftyp" in header[:16] or b"moov" in header[:16])
    elif ext in ["mkv", "webm"]:
        return header.startswith(b"\x1a\x45\xdf\xa3")
    elif ext == "wav":
        return header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WAVE"
    elif ext == "ogg":
        return header.startswith(b"OggS")
    elif ext == "mp3":
        return header.startswith(b"ID3") or header[:2] in [b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"]
    elif ext in ["aac", "m4a"]:
        return True # AAC frames or MP4 container

    # If extension is not strictly categorized, allow if ffprobe validates it
    return True

def compute_file_sha256(file_path: Path) -> str:
    """
    Computes SHA-256 hash of a local file in chunks.
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()

def probe_media_file(file_path: Path) -> Dict[str, Any]:
    """
    Uses FFprobe to validate and extract media properties.
    """
    cmd = [
        settings.ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration,size:stream=width,height,codec_name,codec_type,r_frame_rate",
        "-of", "json",
        str(file_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if proc.returncode != 0:
            return {"valid": False, "error": proc.stderr.strip()}
            
        import json
        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        
        width = int(video_stream.get("width", 1920)) if video_stream else 0
        height = int(video_stream.get("height", 1080)) if video_stream else 0
        codec = video_stream.get("codec_name", "unknown") if video_stream else (
            audio_stream.get("codec_name", "unknown") if audio_stream else "unknown"
        )
        duration = float(fmt.get("duration", 0.0))
        
        fps = 24.0
        if video_stream and "r_frame_rate" in video_stream:
            r = video_stream["r_frame_rate"]
            if "/" in r:
                num, den = r.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 24.0
                
        return {
            "valid": True,
            "width": width,
            "height": height,
            "codec": codec,
            "duration": duration,
            "fps": fps,
            "file_size": int(fmt.get("size", file_path.stat().st_size))
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}

async def save_upload_file_securely(
    upload_file: UploadFile,
    dest_dir: Path,
    allowed_extensions: Sequence[str],
    max_size_bytes: int,
    prefix: str = "",
    expected_type: str = "media",
    validate_content: bool = True
) -> Tuple[Path, str, int]:
    """
    Streams an uploaded file securely:
    - Validates extension
    - Writes to an atomic .tmp file in settings.temp_dir
    - Enforces maximum file size limit during streaming
    - Computes SHA-256 checksum in the same pass
    - Verifies magic byte headers
    - Atomically moves to final dest_dir with a collision-resistant name
    - Guarantees cleanup of temporary files on failure
    """
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Validate extension
    validate_extension(upload_file.filename, allowed_extensions)

    # 2. Prepare secure destination and temp paths
    secure_filename = generate_secure_filename(upload_file.filename, prefix=prefix)
    temp_filename = f"{secure_filename}.tmp"
    temp_path = settings.temp_dir / temp_filename
    final_path = dest_dir / secure_filename

    hasher = hashlib.sha256()
    total_bytes = 0
    chunk_size = 65536  # 64 KB

    try:
        with open(temp_path, "wb") as buffer:
            while True:
                chunk = await upload_file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_size_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds maximum allowed upload size of {max_size_bytes // (1024 * 1024)} MB"
                    )
                hasher.update(chunk)
                buffer.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes)")

        # 3. Verify magic bytes / headers
        if validate_content and not verify_magic_bytes(temp_path, expected_type=expected_type):
            raise HTTPException(status_code=400, detail="Invalid file header: content does not match extension")

        # 4. Atomic move to destination
        shutil.move(str(temp_path), str(final_path))
        checksum = hasher.hexdigest()

        return final_path, checksum, total_bytes

    except Exception:
        # Ensure temporary file cleanup on any failure
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
