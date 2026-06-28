# tests/test_api.py
"""
Тесты REST API.
Запускать с работающим Docker Compose: docker-compose up -d
"""
import os
import tempfile
import pytest
import requests

from format import SliceWriter, LayerData, COMPRESS_ZSTD

BASE_URL = "http://localhost:8000"


def make_sls_bytes(n_layers: int = 2) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".sls", delete=False) as f:
        path = f.name
    try:
        writer = SliceWriter(path, compression=COMPRESS_ZSTD)
        writer.set_metadata({"job_name": "api_test"})
        for i in range(n_layers):
            layer = LayerData(index=i, height_um=200)
            layer.add_contour([(0, 0), (10, 0), (10, 10), (0, 10)])
            writer.add_layer(layer)
        writer.save()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


@pytest.fixture(scope="module")
def uploaded_id():
    content = make_sls_bytes(n_layers=3)
    resp = requests.post(
        f"{BASE_URL}/slices/",
        files={"file": ("test.sls", content, "application/octet-stream")},
        data={"name": "API Test File", "material": "PLA", "tags": '["test","api"]'},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_health():
    resp = requests.get(f"{BASE_URL}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload(uploaded_id):
    assert uploaded_id is not None


def test_get_metadata(uploaded_id):
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/metadata")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "API Test File"
    assert "test" in data["tags"]


def test_download(uploaded_id):
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}")
    assert resp.status_code == 200
    assert resp.content[:9] == b"SLSSTORE!"


def test_versions(uploaded_id):
    new_content = make_sls_bytes(n_layers=5)
    resp = requests.post(
        f"{BASE_URL}/slices/{uploaded_id}/versions",
        files={"file": ("v2.sls", new_content, "application/octet-stream")},
        data={"message": "Added 2 more layers", "author": "test_user"},
    )
    assert resp.status_code == 201

    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/versions")
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) >= 2


def test_search():
    resp = requests.get(f"{BASE_URL}/slices/search?q=API+Test")
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1


def test_upload_gcode():
    gcode_content = b";LAYER:0\nG1 X0 Y0\n;LAYER:1\nG1 X1 Y1\n"
    resp = requests.post(
        f"{BASE_URL}/slices/gcode",
        files={"file": ("mini.gcode", gcode_content, "text/plain")},
        data={"printer_type": "fdm", "name": "GCode Test", "material": "PLA", "tags": "[]"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data.get("format") == "gcode"
    assert data.get("printer_type") == "fdm"


def test_delete(uploaded_id):
    resp = requests.delete(f"{BASE_URL}/slices/{uploaded_id}")
    assert resp.status_code == 204
    resp = requests.get(f"{BASE_URL}/slices/{uploaded_id}/metadata")
    assert resp.status_code == 404
