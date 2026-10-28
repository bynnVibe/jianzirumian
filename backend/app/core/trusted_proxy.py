"""仅接受显式 IP/CIDR 的转发头；启动 uvicorn 时关闭其隐式代理处理。"""
from ipaddress import ip_address, ip_network


class TrustedProxyMiddleware:
    def __init__(self, app, trusted_hosts=""):
        self.app = app
        self.networks = [ip_network(value.strip(), strict=False)
                         for value in trusted_hosts.split(",") if value.strip()]
        if any(network.prefixlen == 0 for network in self.networks):
            raise ValueError("可信代理不允许全网 CIDR")

    def trusted(self, host):
        try:
            address = ip_address(host)
            return any(address in network for network in self.networks)
        except ValueError:
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.trusted((scope.get("client") or ("", 0))[0]):
            scope = dict(scope)
            headers = dict(scope.get("headers", []))
            protocol = headers.get(b"x-forwarded-proto", b"").decode("latin1").strip()
            if protocol in ("http", "https"):
                scope["scheme"] = protocol if scope["type"] == "http" else ("wss" if protocol == "https" else "ws")
            forwarded = headers.get(b"x-forwarded-for", b"").decode("latin1")
            if forwarded:
                try:
                    hosts = [str(ip_address(value.strip())) for value in forwarded.split(",")]
                    # 从最近代理向外检查，遇到非可信跳即停止，不能信任更左侧伪造值。
                    host = next((value for value in reversed(hosts) if not self.trusted(value)), hosts[0])
                    scope["client"] = (host, 0)
                except ValueError:
                    pass
        await self.app(scope, receive, send)
