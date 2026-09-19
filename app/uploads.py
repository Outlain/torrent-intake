"""One bounded multipart upload per request, including requests without a length."""
from __future__ import annotations

import asyncio
from fastapi import HTTPException, Request
from pydantic import ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .metainfo import MAX_TORRENT_BYTES
from .schemas import JobOptions


MAX_UPLOAD_BYTES = MAX_TORRENT_BYTES + 128 * 1024


class _UploadTooLarge(MultiPartException):
    pass


async def read_torrent_upload(request: Request) -> tuple[JobOptions, str, bytes]:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Expected multipart fields: file (.torrent) and settings (JSON)")

    async def bounded_stream():
        received = 0
        try:
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_UPLOAD_BYTES:
                    # MultiPartException also closes already-spooled files.
                    raise _UploadTooLarge("Torrent metadata upload exceeds 32 MiB")
                yield chunk
        except (ClientDisconnect, asyncio.CancelledError) as exc:
            raise MultiPartException("Torrent upload interrupted") from exc

    parser = MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=1, max_part_size=65536)
    try:
        try:
            form = await parser.parse()
        except _UploadTooLarge as exc:
            raise HTTPException(413, exc.message) from exc
        except MultiPartException as exc:
            raise HTTPException(422, exc.message) from exc
        except (MultipartParseError, LookupError, UnicodeError) as exc:
            raise HTTPException(422, "Malformed multipart torrent upload") from exc
        upload, raw_settings = form.get("file"), form.get("settings")
        if (set(form) != {"file", "settings"} or not isinstance(upload, UploadFile)
                or not isinstance(raw_settings, str)):
            raise HTTPException(422, "Provide exactly one file and its settings JSON")
        if not (upload.filename or "").lower().endswith(".torrent"):
            raise HTTPException(422, "Choose a .torrent metadata file")
        try:
            options = JobOptions.model_validate_json(raw_settings)
        except ValidationError as exc:
            # Do not echo uploaded content or private metadata in error responses.
            messages = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()]
            raise HTTPException(422, "; ".join(messages)) from exc
        data = await upload.read(MAX_TORRENT_BYTES + 1)
        if len(data) > MAX_TORRENT_BYTES:
            raise HTTPException(413, "Torrent metadata upload exceeds 32 MiB")
        return options, upload.filename, data
    finally:
        # Also close an unfinished part after a truncated body or parser error;
        # such files are not necessarily present in the returned FormData.
        for handle in parser._files_to_close_on_error:
            handle.close()
