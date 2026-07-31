"""Create small, same-origin thumbnails for Takealot product images."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Lock, get_ident
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "media.takealot.com",
        "takealot.s3.amazonaws.com",
    }
)
ALLOWED_IMAGE_PATH_PREFIX = "/covers_images/"
DEFAULT_MAX_DIMENSION = 192
SUPPORTED_MAX_DIMENSIONS = frozenset({192, 384, 640})
DEFAULT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SOURCE_PIXELS = 36_000_000


class ProductImageInputError(ValueError):
    """Raised when an image URL is outside the trusted Takealot image origin."""


class ProductImageUnavailableError(RuntimeError):
    """Raised when a trusted product image cannot be downloaded or rendered."""


class ProductThumbnailCache:
    """Download each trusted source once and cache a compact JPEG thumbnail."""

    def __init__(
        self,
        project_root: Path,
        *,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_root = project_root / "data" / "runtime-cache"
        self.max_source_bytes = max_source_bytes
        self.max_source_pixels = max_source_pixels
        self._locks: dict[str, tuple[Lock, int]] = {}
        self._locks_guard = Lock()
        self._network_client: httpx.Client | None = None
        self._direct_client: httpx.Client | None = None
        self._fetcher: Callable[[str], bytes]
        if fetcher is None:
            timeout = httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=10.0)
            self._network_client = httpx.Client(
                follow_redirects=False,
                timeout=timeout,
            )
            self._direct_client = httpx.Client(
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,
            )
            self._fetcher = self._download
        else:
            self._fetcher = fetcher

    def close(self) -> None:
        """Close reusable HTTP clients during application shutdown."""
        if self._network_client is not None:
            self._network_client.close()
        if self._direct_client is not None:
            self._direct_client.close()

    def thumbnail_path(
        self,
        image_url: str,
        max_dimension: int = DEFAULT_MAX_DIMENSION,
    ) -> Path:
        """Return a cached thumbnail, creating it atomically when needed."""
        trusted_url = trusted_product_image_url(image_url)
        dimension = validated_thumbnail_dimension(max_dimension)
        cache_key = hashlib.sha256(
            f"jpeg-v2|{dimension}|{trusted_url}".encode()
        ).hexdigest()
        cache_dir = self.cache_root / f"product-thumbnails-{dimension}"
        target = cache_dir / f"{cache_key}.jpg"
        if _usable_cache_file(target):
            return target

        with self._cache_key_lock(cache_key):
            if _usable_cache_file(target):
                return target
            source = self._fetcher(trusted_url)
            if not source or len(source) > self.max_source_bytes:
                raise ProductImageUnavailableError("商品原图为空或超过安全大小限制")
            thumbnail = self._render_thumbnail(source, dimension)
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{os.getpid()}.{get_ident()}.tmp")
            try:
                temporary.write_bytes(thumbnail)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    @contextmanager
    def _cache_key_lock(self, cache_key: str) -> Iterator[None]:
        """Serialize one cache key and discard its lock after the final user."""
        with self._locks_guard:
            existing = self._locks.get(cache_key)
            lock, users = existing if existing is not None else (Lock(), 0)
            self._locks[cache_key] = (lock, users + 1)

        acquired = False
        try:
            lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            with self._locks_guard:
                current = self._locks.get(cache_key)
                if current is None or current[0] is not lock:
                    raise RuntimeError("缩略图缓存锁状态不一致")
                remaining_users = current[1] - 1
                if remaining_users == 0:
                    self._locks.pop(cache_key)
                else:
                    self._locks[cache_key] = (lock, remaining_users)

    def _download(self, image_url: str) -> bytes:
        clients = [self._network_client, self._direct_client]
        for client in clients:
            if client is None:
                continue
            try:
                with client.stream(
                    "GET",
                    image_url,
                    headers={
                        "Accept": "image/avif,image/webp,image/jpeg,image/*",
                        "User-Agent": "TakealotLocalERP/0.1",
                    },
                ) as response:
                    response.raise_for_status()
                    declared_size = int(response.headers.get("Content-Length") or 0)
                    if declared_size > self.max_source_bytes:
                        raise ProductImageUnavailableError(
                            "商品原图超过安全大小限制"
                        )
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > self.max_source_bytes:
                            raise ProductImageUnavailableError(
                                "商品原图超过安全大小限制"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except ProductImageUnavailableError:
                raise
            except (httpx.HTTPError, OSError, ValueError):
                continue
        raise ProductImageUnavailableError("商品原图暂时无法读取")

    def _render_thumbnail(self, source: bytes, max_dimension: int) -> bytes:
        try:
            with Image.open(BytesIO(source)) as opened:
                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > self.max_source_pixels:
                    raise ProductImageUnavailableError(
                        "商品原图像素尺寸超过安全限制"
                    )
                image = ImageOps.exif_transpose(opened)
                image.load()
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.thumbnail(
                    (max_dimension, max_dimension),
                    Image.Resampling.LANCZOS,
                )
                output = BytesIO()
                image.save(
                    output,
                    format="JPEG",
                    quality=78,
                    optimize=True,
                    progressive=True,
                )
                return output.getvalue()
        except ProductImageUnavailableError:
            raise
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise ProductImageUnavailableError("商品原图不是可识别图片") from exc


def trusted_product_image_url(image_url: str) -> str:
    """Validate and normalize the trusted Takealot image origins."""
    candidate = image_url.strip()
    if not candidate or len(candidate) > 2048:
        raise ProductImageInputError("商品图片地址无效")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ProductImageInputError("商品图片地址无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in ALLOWED_IMAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or not parsed.path.startswith(ALLOWED_IMAGE_PATH_PREFIX)
        or parsed.query
        or parsed.fragment
    ):
        raise ProductImageInputError("只允许读取 Takealot 官方商品图片")
    return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))


def validated_thumbnail_dimension(max_dimension: int) -> int:
    """Reject arbitrary resize work and keep cache variants bounded."""
    if max_dimension not in SUPPORTED_MAX_DIMENSIONS:
        allowed = "、".join(str(value) for value in sorted(SUPPORTED_MAX_DIMENSIONS))
        raise ProductImageInputError(f"缩略图尺寸只支持 {allowed} 像素")
    return max_dimension


def _usable_cache_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
