"""aiodahua -- async client for Dahua and Dahua-derived white-label devices.

Dahua builds the hardware and firmware behind Amcrest, Lorex, EmpireTech and
others. They all speak the same CGI API but identify themselves differently and
diverge in which endpoints their firmware actually implements. This library
handles both.

    >>> import asyncio
    >>> from aiodahua import DahuaClient
    >>> async def main():
    ...     async with DahuaClient("192.168.4.4", "admin", "secret") as dev:
    ...         print(await dev.async_identify())        # Amcrest
    ...         print(await dev.async_get_device_type()) # NV4116-HS
    >>> asyncio.run(main())  # doctest: +SKIP
"""

from __future__ import annotations

from .brands import PROFILES
from .brands import SIGNAL_WEIGHTS
from .brands import Brand
from .brands import BrandMatch
from .brands import BrandProfile
from .brands import extract_oem_code
from .brands import identify_brand
from .client import DEFAULT_PORT
from .client import DEFAULT_RTSP_PORT
from .client import DEFAULT_TIMEOUT
from .client import DahuaClient
from .config import build_config_query
from .config import encode_config_value
from .exceptions import DahuaAuthError
from .exceptions import DahuaConnectionError
from .exceptions import DahuaError
from .exceptions import DahuaNotSupportedError
from .exceptions import DahuaResponseError
from .exceptions import DahuaTimeoutError
from .exceptions import DahuaUnsafeOperationError
from .exceptions import DahuaValueError
from .parsers import format_bytes
from .parsers import is_error_response
from .parsers import is_not_supported_response
from .parsers import parse_kv
from .parsers import parse_log_entries
from .parsers import parse_media_files
from .parsers import parse_storage_info

__version__ = "0.4.1"

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_RTSP_PORT",
    "DEFAULT_TIMEOUT",
    "PROFILES",
    "SIGNAL_WEIGHTS",
    "Brand",
    "BrandMatch",
    "BrandProfile",
    "DahuaAuthError",
    "DahuaClient",
    "DahuaConnectionError",
    "DahuaError",
    "DahuaNotSupportedError",
    "DahuaResponseError",
    "DahuaTimeoutError",
    "DahuaUnsafeOperationError",
    "DahuaValueError",
    "__version__",
    "build_config_query",
    "encode_config_value",
    "extract_oem_code",
    "format_bytes",
    "identify_brand",
    "is_error_response",
    "is_not_supported_response",
    "parse_kv",
    "parse_log_entries",
    "parse_media_files",
    "parse_storage_info",
]
