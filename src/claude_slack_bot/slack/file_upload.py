from __future__ import annotations

import re
from pathlib import Path

import aiohttp
import structlog

logger = structlog.get_logger()

# File extensions we look for in command output
UPLOADABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".webm", ".pdf", ".zip"}

# Patterns that suggest a file was written
FILE_OUTPUT_PATTERNS = [
    re.compile(r"(?:saved?|writ(?:ten|e)|output|created|generated)\s+(?:to\s+)?['\"]?(/tmp/[^\s'\"]+)", re.IGNORECASE),
    re.compile(r"(/tmp/\S+\.(?:png|jpg|jpeg|gif|mp4|webm|pdf|svg|zip))\b"),
]


async def upload_file_to_thread(
    client: object,
    channel_id: str,
    thread_ts: str,
    file_bytes: bytes,
    filename: str,
    initial_comment: str = "",
) -> str | None:
    """Upload a file to a Slack thread using the modern upload flow."""
    try:
        # Step 1: Get upload URL
        upload_info = await client.files_getUploadURLExternal(  # type: ignore[attr-defined]
            length=len(file_bytes),
            filename=filename,
        )

        upload_url = upload_info["upload_url"]
        file_id = upload_info["file_id"]

        # Step 2: Upload file content
        async with aiohttp.ClientSession() as session:
            async with session.post(upload_url, data=file_bytes) as resp:
                if resp.status != 200:
                    logger.error("file_upload.upload_failed", status=resp.status)
                    return None

        # Step 3: Complete the upload and share to thread
        await client.files_completeUploadExternal(  # type: ignore[attr-defined]
            files=[{"id": file_id, "title": filename}],
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_comment=initial_comment,
        )

        logger.info("file_upload.success", filename=filename, file_id=file_id)
        return file_id

    except Exception:
        logger.exception("file_upload.error", filename=filename)
        return None


async def scan_and_upload_files(
    client: object,
    channel_id: str,
    thread_ts: str,
    command: str,
    output: str,
) -> None:
    """Scan bash command output for files that should be uploaded to Slack."""
    candidates: set[str] = set()
    for pattern in FILE_OUTPUT_PATTERNS:
        for match in pattern.finditer(output):
            candidates.add(match.group(1))
        for match in pattern.finditer(command):
            candidates.add(match.group(1))

    for file_path_str in candidates:
        file_path = Path(file_path_str)
        if not file_path.exists():
            continue
        if file_path.suffix.lower() not in UPLOADABLE_EXTENSIONS:
            continue
        if file_path.stat().st_size > 50 * 1024 * 1024:  # 50MB limit
            logger.warning("file_upload.too_large", path=file_path_str)
            continue

        file_bytes = file_path.read_bytes()
        await upload_file_to_thread(
            client,
            channel_id,
            thread_ts,
            file_bytes,
            file_path.name,
            initial_comment=f"Generated: `{file_path.name}`",
        )
