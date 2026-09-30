from __future__ import annotations

import base64

import aiohttp


MAX_IMAGE_BYTES = 2_000_000


class AppsScriptImageUploader:
    def __init__(self, webhook_url: str, secret: str) -> None:
        self._webhook_url = webhook_url
        self._secret = secret

    async def insert_image(
        self,
        *,
        image_bytes: bytes,
        spreadsheet_id: str,
        sheet_name: str,
        row_number: int,
        image_key: str,
        mime_type: str = "image/jpeg",
    ) -> None:
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 2 MB Apps Script limit")

        payload = {
            "secret": self._secret,
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": sheet_name,
            "row_number": row_number,
            "image_key": image_key,
            "mime_type": mime_type,
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        }
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self._webhook_url, json=payload) as response:
                result = await response.json(content_type=None)
                if response.status >= 400 or not result.get("ok"):
                    error = result.get("error", f"HTTP {response.status}")
                    raise RuntimeError(f"Apps Script image upload failed: {error}")
