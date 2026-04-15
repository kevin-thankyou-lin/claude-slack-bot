from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from ..slack.file_upload import upload_file_to_thread

logger = structlog.get_logger()


async def handle_custom_tool(
    tool_name: str,
    tool_input: dict[str, object],
    channel_id: str,
    thread_ts: str,
    client: object,
) -> str:
    """Handle custom tool execution (generate_image, create_video).

    Returns the result string to send back to the agent.
    """
    if tool_name in ("generate_image", "create_video"):
        return await _handle_media_generation(tool_input, channel_id, thread_ts, client)
    return f"Unknown custom tool: {tool_name}"


async def _handle_media_generation(
    tool_input: dict[str, object],
    channel_id: str,
    thread_ts: str,
    client: object,
) -> str:
    """Execute a Python script that generates an image or video, then upload it."""
    script = str(tool_input.get("script", ""))
    output_path = str(tool_input.get("output_path", ""))
    description = str(tool_input.get("description", ""))

    if not script or not output_path:
        return "Error: both 'script' and 'output_path' are required"

    exec_result = await _run_script(script)
    if exec_result is not None:
        return exec_result

    path = Path(output_path)
    if not path.exists():
        return f"Script completed but output file not found: {output_path}"

    file_bytes = path.read_bytes()
    if len(file_bytes) == 0:
        return f"Output file is empty: {output_path}"

    file_id = await upload_file_to_thread(
        client,
        channel_id,
        thread_ts,
        file_bytes,
        path.name,
        initial_comment=description or f"Generated: `{path.name}`",
    )
    if file_id:
        return f"File uploaded successfully: {path.name} ({len(file_bytes)} bytes)"
    return f"File generated at {output_path} but upload to Slack failed"


async def _run_script(script: str) -> str | None:
    """Run a Python script, returning an error string on failure or None on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            return f"Script failed (exit {proc.returncode}):\n{stderr.decode(errors='replace')[:2000]}"
        return None

    except asyncio.TimeoutError:
        return "Script timed out after 120 seconds"
    except Exception as e:
        return f"Script execution error: {e}"
