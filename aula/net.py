"""网络小工具。

存在的理由很具体：如果用户环境里有 ``HTTP_PROXY``（公司网、代理客户端、
抓包工具都会设），``urllib`` 会把**发往 localhost 的请求也送进代理**，于是
本机 Ollama、本机 faster-whisper 服务全部 502。代理是用来访问外网的，不该
拦本机流量，所以这里对私有地址显式绕过代理。
"""

from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlparse

_LOCAL_NAMES = {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if not host:
        return False
    if host in _LOCAL_NAMES or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def opener_for(url: str) -> urllib.request.OpenerDirector:
    """按目标地址选择 opener：本机直连，外网走系统代理。"""
    host = urlparse(url).hostname or ""
    if is_local_host(host):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()
