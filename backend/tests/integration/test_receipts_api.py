"""The receipts HTTP surface, end to end through the ASGI app."""

import pytest
from httpx import AsyncClient

from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


async def _upload(client: AsyncClient, data: bytes, filename: str = "receipt.jpg") -> dict:
    response = await client.post(
        "/receipts",
        files={"file": (filename, data, "image/jpeg")},
    )
    return {"status": response.status_code, "body": response.json()}


async def test_upload_returns_201_and_a_receipt(client: AsyncClient) -> None:
    result = await _upload(client, make_image(color="#101010"))

    assert result["status"] == 201
    body = result["body"]
    assert body["created"] is True
    assert body["receipt"]["status"] == "uploaded"
    assert body["width"] == 800
    assert body["image_format"] == "JPEG"


async def test_upload_never_exposes_the_storage_path(client: AsyncClient) -> None:
    """Receipt images are personal financial records; a storage key must not
    appear in a response body."""
    result = await _upload(client, make_image(color="#202020"))

    assert "image_path" not in result["body"]["receipt"]
    assert "image_path" not in str(result["body"])


async def test_reupload_reports_created_false(client: AsyncClient) -> None:
    data = make_image(color="#303030")

    first = await _upload(client, data)
    second = await _upload(client, data)

    assert first["body"]["created"] is True
    assert second["body"]["created"] is False
    assert second["body"]["receipt"]["id"] == first["body"]["receipt"]["id"]


async def test_upload_of_a_non_image_returns_422_with_guidance(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/receipts",
        files={"file": ("notes.txt", b"just some text" * 200, "text/plain")},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    # The message is shown to the user verbatim, so it must say what to do.
    assert "JPEG" in detail or "isn't an image" in detail


async def test_get_receipt_returns_detail(client: AsyncClient) -> None:
    uploaded = await _upload(client, make_image(color="#404040"))
    receipt_id = uploaded["body"]["receipt"]["id"]

    response = await client.get(f"/receipts/{receipt_id}")

    assert response.status_code == 200
    assert response.json()["id"] == receipt_id
    assert response.json()["image_sha256"]


async def test_get_missing_receipt_returns_404(client: AsyncClient) -> None:
    response = await client.get("/receipts/99999999")
    assert response.status_code == 404
    assert "99999999" in response.json()["detail"]


async def test_list_receipts_paginates(client: AsyncClient) -> None:
    for shade in ("#010101", "#020202", "#030303"):
        await _upload(client, make_image(color=shade))

    response = await client.get("/receipts", params={"limit": 2, "offset": 0})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 3
    assert body["limit"] == 2


async def test_list_rejects_an_absurd_page_size(client: AsyncClient) -> None:
    response = await client.get("/receipts", params={"limit": 5000})
    assert response.status_code == 422
