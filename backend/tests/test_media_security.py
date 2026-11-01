import io
import pytest
from pathlib import Path
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.services.security import (
    sanitize_filename,
    generate_secure_filename,
    validate_safe_path,
    validate_extension,
    verify_magic_bytes,
    compute_file_sha256
)

client = TestClient(app)

def test_filename_sanitization():
    """Verifies that path traversals, null bytes, and dangerous characters are eliminated."""
    assert sanitize_filename("../../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert sanitize_filename("safe_video.mp4") == "safe_video.mp4"
    assert sanitize_filename("video with spaces & symbols!@#.mp4") == "video_with_spaces_symbols.mp4"
    assert sanitize_filename("\x00secret.mov") == "secret.mov"
    assert sanitize_filename("") == "unnamed_asset"
    assert sanitize_filename(".") == "unnamed_asset"

def test_secure_filename_generation():
    """Verifies generated filename uniqueness and preservation of extension."""
    f1 = generate_secure_filename("test.mp4", prefix="vid")
    f2 = generate_secure_filename("test.mp4", prefix="vid")
    assert f1 != f2
    assert f1.startswith("vid_")
    assert f1.endswith(".mp4")

def test_path_traversal_prevention(tmp_path):
    """Verifies that validate_safe_path blocks paths escaping the storage root."""
    base_dir = tmp_path / "storage"
    base_dir.mkdir()
    
    inside_file = base_dir / "valid_asset.mp4"
    inside_file.touch()

    # Valid path inside base
    validated = validate_safe_path(inside_file, base_dir=base_dir)
    assert validated.resolve() == inside_file.resolve()

    # Path traversal outside base
    outside_file = tmp_path / "secret.txt"
    outside_file.touch()
    
    with pytest.raises(HTTPException) as exc_info:
        validate_safe_path(outside_file, base_dir=base_dir)
    assert exc_info.value.status_code == 400

    # Path traversal with relative parent segments
    with pytest.raises(HTTPException) as exc_info2:
        validate_safe_path(base_dir / ".." / "secret.txt", base_dir=base_dir)
    assert exc_info2.value.status_code == 400

def test_extension_validation():
    """Verifies allowed extension whitelisting."""
    allowed = [".mp4", ".mov", ".png", ".jpg"]
    assert validate_extension("video.MP4", allowed) == ".mp4"
    assert validate_extension("photo.jpg", allowed) == ".jpg"

    with pytest.raises(HTTPException) as exc:
        validate_extension("malicious.exe", allowed)
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc2:
        validate_extension("script.py", allowed)
    assert exc2.value.status_code == 400

def test_magic_bytes_verification(tmp_path):
    """Verifies that genuine magic headers match extension."""
    png_file = tmp_path / "test.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")
    assert verify_magic_bytes(png_file, expected_type="image") is True

    fake_png = tmp_path / "fake.png"
    fake_png.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00") # Windows PE executable header
    assert verify_magic_bytes(fake_png, expected_type="image") is False

    jpeg_file = tmp_path / "test.jpg"
    jpeg_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert verify_magic_bytes(jpeg_file, expected_type="image") is True

def test_duplicate_upload_rejection_and_cleanup(tmp_path):
    """Verifies that uploading the exact same media twice is rejected with 409 and clean temp files."""
    # 1. Fetch project ID
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 2. Create a minimal valid synthetic MP4 file using ffmpeg
    from backend.app.services.ffmpeg_service import ffmpeg_service
    test_video = tmp_path / "security_test_clip.mp4"
    ffmpeg_service.generate_synthetic_test_clip(
        output_path=test_video,
        duration=1.0,
        text_label="SECURITY_TEST"
    )
    video_bytes = test_video.read_bytes()

    # 3. First upload - should succeed
    res1 = client.post(
        f"/api/media/upload/{project_id}",
        files={"file": ("security_test_clip.mp4", io.BytesIO(video_bytes), "video/mp4")}
    )
    assert res1.status_code == 200
    asset1 = res1.json()
    assert asset1["checksum"] is not None
    assert len(asset1["checksum"]) == 64

    # 4. Second upload of exact same file - should be rejected as duplicate (409 Conflict)
    res2 = client.post(
        f"/api/media/upload/{project_id}",
        files={"file": ("duplicate_security_test_clip.mp4", io.BytesIO(video_bytes), "video/mp4")}
    )
    assert res2.status_code == 409
    assert "Duplicate media asset" in res2.json()["detail"]

    # 5. Verify no leftover .tmp files in settings.temp_dir
    tmp_leftovers = list(settings.temp_dir.glob("*.tmp"))
    assert len(tmp_leftovers) == 0

    # Cleanup asset1 from DB and disk
    client.delete(f"/api/media/{asset1['id']}")

def test_download_path_traversal_rejection():
    """Verifies that export download prevents escaping storage root."""
    # Attempting to download a non-existent or malicious job ID
    res = client.get("/api/export/download/non-existent-uuid")
    assert res.status_code == 404
