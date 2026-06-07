from io import BytesIO

import pytest
from PIL import Image

import app.services.storage_service as storage_service
from app.config import settings
from app.services.storage_service import StorageDownloadError


def _image_bytes(size=(32, 24), image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(output, format=image_format)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.10", "169.254.10.1", "::1"])
async def test_validate_public_url_rejects_private_loopback_link_local_hosts(monkeypatch, ip):
    async def fake_getaddrinfo(_hostname, *_args, **_kwargs):
        return [(None, None, None, None, (ip, 443))]

    monkeypatch.setattr(storage_service.asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(StorageDownloadError, match="blocked address"):
        await storage_service._validate_public_url("https://images.example/test.jpg")


@pytest.mark.asyncio
async def test_validate_public_url_rejects_non_http_schemes():
    with pytest.raises(StorageDownloadError, match="Only http/https"):
        await storage_service._validate_public_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_validate_public_url_allows_any_public_host_when_domain_allowlist_empty_in_local(monkeypatch):
    async def fake_getaddrinfo(_hostname, *_args, **_kwargs):
        return [(None, None, None, None, ("93.184.216.34", 443))]

    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "allowed_image_domains", None)
    monkeypatch.setattr(storage_service.asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)

    await storage_service._validate_public_url("https://images.example/test.jpg")


@pytest.mark.asyncio
async def test_validate_public_url_requires_domain_allowlist_outside_local(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allowed_image_domains", None)

    with pytest.raises(StorageDownloadError, match="ALLOWED_IMAGE_DOMAINS"):
        await storage_service._validate_public_url("https://images.example/test.jpg")


@pytest.mark.asyncio
async def test_validate_public_url_enforces_allowed_image_domain_exact_and_subdomain(monkeypatch):
    async def fake_getaddrinfo(_hostname, *_args, **_kwargs):
        return [(None, None, None, None, ("93.184.216.34", 443))]

    monkeypatch.setattr(settings, "allowed_image_domains", "images.example, cdn.example.org")
    monkeypatch.setattr(storage_service.asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)

    await storage_service._validate_public_url("https://images.example/test.jpg")
    await storage_service._validate_public_url("https://sub.images.example/test.jpg")

    with pytest.raises(StorageDownloadError, match="not in allowed image domains"):
        await storage_service._validate_public_url("https://evilimages.example/test.jpg")


def test_check_content_type_rejects_non_image_mime():
    class Headers(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    with pytest.raises(StorageDownloadError, match="Unsupported image content type"):
        storage_service._check_content_type(Headers({"content-type": "text/html"}))


def test_check_content_length_rejects_oversized(monkeypatch):
    class Headers(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    monkeypatch.setattr(settings, "max_download_mb", 1)

    with pytest.raises(StorageDownloadError, match="size limit"):
        storage_service._check_content_length(Headers({"content-length": str(2 * 1024 * 1024)}))


def test_verify_image_rejects_invalid_bytes():
    with pytest.raises(StorageDownloadError, match="valid raster image"):
        storage_service._verify_image(b"not an image")


def test_verify_image_rejects_pixel_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_image_pixels", 10)

    with pytest.raises(StorageDownloadError, match="pixel limit"):
        storage_service._verify_image(_image_bytes(size=(4, 4)))


def test_store_uploaded_final_asset_files_rejects_upload_size_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)

    with pytest.raises(StorageDownloadError, match="Uploaded image exceeds"):
        storage_service.store_uploaded_final_asset_files(
            asset=type("Asset", (), {"id": 1, "video_id": 1, "source_type": "own_upload", "rights_status": "own", "source_url": None, "license_note": None, "license_document_ref": None, "author_name": None})(),
            data=_image_bytes(),
            original_filename="too-large.png",
        )



async def _noop_validate_public_url(_url):
    return None


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, _method, url):
        self.requested_urls.append(url)
        if not self.responses:
            raise AssertionError("no fake response configured")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_fetch_image_bytes_revalidates_redirect_target_against_domain_allowlist(monkeypatch):
    validated_urls = []

    async def fake_validate_public_url(url):
        validated_urls.append(url)
        if url.startswith("https://untrusted.example"):
            raise StorageDownloadError("Image host is not in allowed image domains: untrusted.example")

    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(status_code=302, headers={"location": "https://untrusted.example/image.png"}),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", fake_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="not in allowed image domains"):
        await storage_service._fetch_image_bytes("https://images.example/start.png")

    assert validated_urls == ["https://images.example/start.png", "https://untrusted.example/image.png"]
    assert fake_client.requested_urls == ["https://images.example/start.png"]


@pytest.mark.asyncio
async def test_fetch_image_bytes_revalidates_redirect_target(monkeypatch):
    validated_urls = []

    async def fake_validate_public_url(url):
        validated_urls.append(url)
        if url.startswith("http://127.0.0.1"):
            raise StorageDownloadError("blocked redirect target")

    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(status_code=302, headers={"location": "http://127.0.0.1/image.png"}),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", fake_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="blocked redirect target"):
        await storage_service._fetch_image_bytes("https://images.example/start.png")

    assert validated_urls == ["https://images.example/start.png", "http://127.0.0.1/image.png"]
    assert fake_client.requested_urls == ["https://images.example/start.png"]


@pytest.mark.asyncio
async def test_fetch_image_bytes_rejects_unsupported_content_type(monkeypatch):
    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(status_code=200, headers={"content-type": "text/html"}, chunks=[b"<html>"]),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", _noop_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="Unsupported image content type"):
        await storage_service._fetch_image_bytes("https://images.example/not-image")


@pytest.mark.asyncio
async def test_fetch_image_bytes_rejects_missing_content_type(monkeypatch):
    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(status_code=200, headers={}, chunks=[_image_bytes()]),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", _noop_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="Unsupported image content type: missing"):
        await storage_service._fetch_image_bytes("https://images.example/missing-content-type.png")


@pytest.mark.asyncio
async def test_fetch_image_bytes_rejects_content_length_over_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_download_mb", 1)
    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "image/png", "content-length": str(2 * 1024 * 1024)},
            chunks=[_image_bytes()],
        ),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", _noop_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="size limit"):
        await storage_service._fetch_image_bytes("https://images.example/too-large-header.png")


@pytest.mark.asyncio
async def test_fetch_image_bytes_returns_supported_image_bytes(monkeypatch):
    data = _image_bytes()
    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "image/png", "content-length": str(len(data))},
            chunks=[data[:10], data[10:]],
        ),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", _noop_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    assert await storage_service._fetch_image_bytes("https://images.example/image.png") == data
    assert fake_client.requested_urls == ["https://images.example/image.png"]


@pytest.mark.asyncio
async def test_fetch_image_bytes_rejects_stream_over_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_download_mb", 1)
    fake_client = _FakeAsyncClient([
        _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "image/png"},
            chunks=[b"x" * (1024 * 1024), b"y"],
        ),
    ])
    monkeypatch.setattr(storage_service, "_validate_public_url", _noop_validate_public_url)
    monkeypatch.setattr(storage_service.httpx, "AsyncClient", lambda **_kwargs: fake_client)

    with pytest.raises(StorageDownloadError, match="size limit"):
        await storage_service._fetch_image_bytes("https://images.example/too-large.png")


@pytest.mark.parametrize("storage_key", ["../escape.jpg", "/tmp/escape.jpg", "a//b.jpg", "a/../../b.jpg"])
def test_write_storage_key_rejects_path_escape(monkeypatch, tmp_path, storage_key):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")

    with pytest.raises(StorageDownloadError, match="Invalid storage key"):
        storage_service._write_storage_key(storage_key, b"data")

    assert not list(tmp_path.rglob("escape.jpg"))
