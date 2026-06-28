# client/client.py
import json
import requests
from typing import Optional, List, Dict, Any


class SliceStoreClient:
    def __init__(self, base_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def upload(
        self,
        path: str,
        name: str,
        printer_id: str = None,
        material: str = None,
        tags: List[str] = None,
    ) -> str:
        with open(path, "rb") as f:
            resp = self.session.post(
                f"{self.base_url}/slices/",
                files={"file": (path, f, "application/octet-stream")},
                data={
                    "name": name,
                    "printer_id": printer_id or "",
                    "material": material or "",
                    "tags": json.dumps(tags or []),
                },
            )
        resp.raise_for_status()
        return resp.json()["id"]

    def get_metadata(self, slice_id: str) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}/metadata")
        resp.raise_for_status()
        return resp.json()

    def download(self, slice_id: str, dest_path: str) -> None:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}")
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)

    def get_versions(self, slice_id: str) -> List[Dict]:
        resp = self.session.get(f"{self.base_url}/slices/{slice_id}/versions")
        resp.raise_for_status()
        return resp.json()

    def restore_version(self, slice_id: str, version_num: int) -> Dict:
        resp = self.session.post(
            f"{self.base_url}/slices/{slice_id}/restore/{version_num}"
        )
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        q: str = None,
        printer_id: str = None,
        material: str = None,
        tag: str = None,
    ) -> List[Dict]:
        params = {}
        if q:
            params["q"] = q
        if printer_id:
            params["printer_id"] = printer_id
        if material:
            params["material"] = material
        if tag:
            params["tag"] = tag
        resp = self.session.get(f"{self.base_url}/slices/search", params=params)
        resp.raise_for_status()
        return resp.json()

    def delete(self, slice_id: str) -> None:
        resp = self.session.delete(f"{self.base_url}/slices/{slice_id}")
        resp.raise_for_status()
