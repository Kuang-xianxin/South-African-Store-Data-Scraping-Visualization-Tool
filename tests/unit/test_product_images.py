from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from PIL import Image

from takealot_ops.erp.product_images import (
    ProductImageInputError,
    ProductImageUnavailableError,
    ProductThumbnailCache,
    trusted_product_image_url,
    validated_thumbnail_dimension,
)


TRUSTED_URL = (
    "http://takealot.s3.amazonaws.com/"
    "covers_images/37b5fc661b694ed5969280cc0cea2ce4/s.file"
)


def _large_jpeg() -> bytes:
    image = Image.new("RGB", (2000, 1200), (76, 118, 97))
    output = BytesIO()
    image.save(output, format="JPEG", quality=94)
    return output.getvalue()


def test_thumbnail_cache_resizes_and_reuses_one_download(tmp_path: Path) -> None:
    downloads: list[str] = []

    def fetcher(url: str) -> bytes:
        downloads.append(url)
        return _large_jpeg()

    cache = ProductThumbnailCache(tmp_path, fetcher=fetcher)
    first = cache.thumbnail_path(TRUSTED_URL)
    second = cache.thumbnail_path(TRUSTED_URL)

    assert first == second
    assert first.is_file()
    assert downloads == [
        TRUSTED_URL.replace("http://", "https://"),
    ]
    with Image.open(first) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert max(thumbnail.size) == 192
        assert thumbnail.width <= 192
        assert thumbnail.height <= 192
    assert first.stat().st_size < len(_large_jpeg())


def test_thumbnail_cache_keeps_separate_supported_sizes(tmp_path: Path) -> None:
    downloads: list[str] = []

    def fetcher(url: str) -> bytes:
        downloads.append(url)
        return _large_jpeg()

    cache = ProductThumbnailCache(tmp_path, fetcher=fetcher)
    small = cache.thumbnail_path(TRUSTED_URL, 192)
    detail = cache.thumbnail_path(TRUSTED_URL, 640)

    assert small != detail
    assert small.parent.name == "product-thumbnails-192"
    assert detail.parent.name == "product-thumbnails-640"
    assert downloads == [
        TRUSTED_URL.replace("http://", "https://"),
        TRUSTED_URL.replace("http://", "https://"),
    ]
    with Image.open(detail) as thumbnail:
        assert max(thumbnail.size) == 640


def test_thumbnail_cache_releases_lock_after_concurrent_reuse(tmp_path: Path) -> None:
    fetch_started = Event()
    allow_fetch_to_finish = Event()
    downloads = 0

    def fetcher(_: str) -> bytes:
        nonlocal downloads
        downloads += 1
        fetch_started.set()
        assert allow_fetch_to_finish.wait(timeout=5)
        return _large_jpeg()

    cache = ProductThumbnailCache(tmp_path, fetcher=fetcher)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.thumbnail_path, TRUSTED_URL)
        assert fetch_started.wait(timeout=5)
        second = executor.submit(cache.thumbnail_path, TRUSTED_URL)
        deadline = monotonic() + 5
        users = 0
        while monotonic() < deadline:
            with cache._locks_guard:
                users = max((entry[1] for entry in cache._locks.values()), default=0)
            if users == 2:
                break
            sleep(0.01)
        assert users == 2
        allow_fetch_to_finish.set()
        assert first.result(timeout=5) == second.result(timeout=5)

    assert downloads == 1
    assert cache._locks == {}


def test_thumbnail_cache_releases_lock_after_failure(tmp_path: Path) -> None:
    cache = ProductThumbnailCache(tmp_path, fetcher=lambda _: b"")

    with pytest.raises(ProductImageUnavailableError):
        cache.thumbnail_path(TRUSTED_URL)

    assert cache._locks == {}


def test_thumbnail_cache_does_not_retain_completed_unique_keys(tmp_path: Path) -> None:
    cache = ProductThumbnailCache(tmp_path, fetcher=lambda _: b"")

    for index in range(100):
        url = (
            "https://media.takealot.com/covers_images/"
            f"memory-check-{index}/s.file"
        )
        with pytest.raises(ProductImageUnavailableError):
            cache.thumbnail_path(url)

    assert cache._locks == {}


def test_media_takealot_image_is_trusted_and_normalized() -> None:
    source = "http://media.takealot.com/covers_images/example/s-zoom.file"

    assert trusted_product_image_url(source) == source.replace("http://", "https://")


@pytest.mark.parametrize("size", [0, 191, 193, 512, 641, 10_000])
def test_thumbnail_cache_rejects_unsupported_sizes(size: int) -> None:
    with pytest.raises(ProductImageInputError, match="192、384、640"):
        validated_thumbnail_dimension(size)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///C:/Windows/win.ini",
        "https://example.com/covers_images/example/s.file",
        "https://takealot.s3.amazonaws.com/other/example/s.file",
        "https://takealot.s3.amazonaws.com/covers_images/example/s.file?redirect=1",
        "https://user:pass@takealot.s3.amazonaws.com/covers_images/example/s.file",
    ],
)
def test_thumbnail_cache_rejects_untrusted_sources(url: str) -> None:
    with pytest.raises(ProductImageInputError):
        trusted_product_image_url(url)
