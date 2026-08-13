"""Generate and execute Futu quote links behind one HTTP endpoint.

The verified desktop deep link requires Futu's internal ``stock_id``.  This
module owns code validation, official web URLs, the verified macOS deep link,
OpenAPI resolution, and the browser-side fallback response.  Calling pages
only open the endpoint and contain no Futu URL or platform rules.
"""

from __future__ import annotations

import html
import json
import re
import socket
from functools import lru_cache

import futu as ft

_FUTU_CODE_RE = re.compile(r"^(?:HK|US|SH|SZ)\.[A-Za-z0-9][A-Za-z0-9.-]*$")
_FUTU_WEB_ORIGIN = "https://www.futunn.com"
FUTU_FAVICON_URL = f"{_FUTU_WEB_ORIGIN}/favicon.ico"


class FutuQuoteLookupError(RuntimeError):
    """Raised when OpenD cannot resolve a Futu security code."""


def validate_futu_code(code: str) -> str:
    """Return a validated Futu security code, preserving ticker suffixes."""
    value = code.strip()
    if not _FUTU_CODE_RE.fullmatch(value):
        raise ValueError(f"invalid Futu security code: {code!r}")
    return value


def create_futu_web_url(code: str) -> str:
    """Build Futu's official quote-page URL from a standard Futu code."""
    code = validate_futu_code(code)
    market, symbol = code.split(".", 1)
    return f"{_FUTU_WEB_ORIGIN}/stock/{symbol}-{market}"


def create_futu_app_url(stock_id: str) -> str:
    """Build the empirically verified Futu desktop quote deep link."""
    value = str(stock_id).strip()
    if not value.isdigit():
        raise ValueError(f"invalid Futu stock ID: {stock_id!r}")
    return f"futunn://quote/stockDetail/{value}/1"


def _opend_alive(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@lru_cache(maxsize=4096)
def resolve_futu_stock_id(host: str, port: int, code: str) -> str:
    """Resolve ``code`` to the stock ID required by Futu's desktop deep link."""
    code = validate_futu_code(code)
    if not _opend_alive(host, port):
        raise FutuQuoteLookupError(f"Futu OpenD is unavailable at {host}:{port}")

    quote_ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        ret, data = quote_ctx.get_stock_basicinfo(
            ft.Market.HK,
            ft.SecurityType.STOCK,
            code_list=[code],
        )
    finally:
        quote_ctx.close()

    if ret != ft.RET_OK:
        raise FutuQuoteLookupError(str(data))
    if data.empty or "stock_id" not in data.columns:
        raise FutuQuoteLookupError(f"Futu stock ID not found for {code}")

    exact = data[data["code"] == code]
    if exact.empty:
        raise FutuQuoteLookupError(f"Futu stock ID not found for {code}")
    return str(int(exact.iloc[0]["stock_id"]))


def create_futu_launch_page(host: str, port: int, code: str) -> str:
    """Return a self-contained launch page with official-web fallback.

    Only macOS attempts the verified custom scheme. iOS and other platforms
    go directly to Futu's official quote page. Failure to resolve ``stock_id``
    also degrades to the official page.
    """
    code = validate_futu_code(code)
    web_url = create_futu_web_url(code)
    try:
        app_url = create_futu_app_url(resolve_futu_stock_id(host, port, code))
    except FutuQuoteLookupError:
        app_url = None

    web_json = json.dumps(web_url)
    app_json = json.dumps(app_url)
    web_html = html.escape(web_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>正在打开富途</title>
<noscript><meta http-equiv="refresh" content="0;url={web_html}"></noscript>
</head>
<body>
<p>正在打开富途，若未自动跳转，请<a href="{web_html}">访问富途网页版</a>。</p>
<script>
(() => {{
  const webUrl = {web_json};
  const appUrl = {app_json};
  const platform = navigator.userAgentData?.platform || navigator.platform || '';
  const userAgent = navigator.userAgent || '';
  const isIOS = /iPhone|iPad|iPod/i.test(userAgent) ||
    (/^Mac/i.test(platform) && Number(navigator.maxTouchPoints || 0) > 1);
  const isMacOS = !isIOS && (/^Mac/i.test(platform) || /Macintosh/i.test(userAgent));

  if (!isMacOS || !appUrl) {{
    location.replace(webUrl);
    return;
  }}

  location.replace(appUrl);
  setTimeout(() => {{
    if (document.visibilityState === 'visible') location.replace(webUrl);
  }}, 1400);
}})();
</script>
</body>
</html>"""
