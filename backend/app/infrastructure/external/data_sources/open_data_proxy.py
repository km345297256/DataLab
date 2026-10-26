"""Optional operator-configured proxy for Hugging Face official hosts only.

Dataset URLs, redirects, TLS verification and source authentication rules stay
unchanged. Never inherit HTTP(S)_PROXY from the process or proxy other sources.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx

from app.infrastructure.external.data_sources.open_data_ml_expansion_adapters import CDN_HOSTS

HF_PROXY_HOSTS = frozenset({'huggingface.co', *CDN_HOSTS})


def huggingface_proxy_mounts() -> dict:
    value = os.environ.get('OPEN_DATA_HUGGINGFACE_PROXY', '').strip()
    if not value:
        return {}
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.port == 0
                or url.path not in {'', '/'} or url.query or url.fragment
                or any(ord(char) < 33 for char in value)):
            raise ValueError()
    except (ValueError, TypeError):
        # Configuration can contain proxy credentials: never echo it in errors.
        raise ValueError('Invalid OPEN_DATA_HUGGINGFACE_PROXY configuration') from None
    transport = httpx.AsyncHTTPTransport(proxy=value, trust_env=False)
    return {f'https://{host}': transport for host in HF_PROXY_HOSTS}
