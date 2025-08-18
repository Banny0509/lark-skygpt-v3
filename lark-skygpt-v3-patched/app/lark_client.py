from __future__ import annotations
import time
import json
import logging
from typing import Optional, Tuple
import requests

from .config import settings

logger = logging.getLogger(__name__)

class LarkClient:
    def __init__(self):
        self._tenant_token: Optional[str] = None
        self._expire_at: float = 0.0
        self.base = settings.LARK_BASE.rstrip("/")
        self.session = requests.Session()

    # --- token ---
    def _tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_token and now < self._expire_at - 60:
            return self._tenant_token
        url = f"{self.base}/open-apis/auth/v3/tenant_access_token/internal"
        r = self.session.post(url, json={"app_id": settings.APP_ID, "app_secret": settings.APP_SECRET}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {data}")
        self._tenant_token = data["tenant_access_token"]
        self._expire_at = now + data.get("expire", 7200)
        return self._tenant_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._tenant_access_token()}"}

    # --- send text ---
    def send_text(self, chat_id: str, text: str) -> None:
        url = f"{self.base}/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        r = self.session.post(url, headers=self._headers(), json=body, timeout=10)
        if r.status_code >= 300:
            logger.warning("send_text status=%s body=%s", r.status_code, r.text)
        else:
            data = r.json()
            if data.get("code") != 0:
                logger.warning("send_text code!=0 resp=%s", data)

    # --- download resource in message ---
    def get_message_resource(self, message_id: str, file_key: str, *, typ: str) -> Tuple[bytes, str | None]:
        """
        typ: 'file' or 'image'
        returns: (bytes, filename?)
        """
        url = f"{self.base}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        params = {"type": typ}
        r = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        r.raise_for_status()
        filename = None
        cd = r.headers.get("Content-Disposition") or r.headers.get("content-disposition")
        if cd and "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"; ')
        return r.content, filename
