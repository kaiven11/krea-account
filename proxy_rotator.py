import os
import sys
import time

from oututil import setup_utf8_stdout

setup_utf8_stdout()

import requests


SKIP_KEYWORDS = (
    "官网", "公告", "剩余", "重置", "到期", "套餐", "traffic", "expire",
    "info", "官网地址", "更新", "订阅", "全红", "备用", "失联", "过期",
    "流量", "群", "频道", "test", "测试", "剩余流量",
)


class ProxyRotator:
    def __init__(self):
        self.api = os.getenv("CLASH_API", "http://127.0.0.1:9090")
        self.secret = os.getenv("CLASH_SECRET", "")
        self.socks5_port = os.getenv("CLASH_SOCKS5_PORT", "7891")
        self.group = os.getenv("CLASH_GROUP", "GLOBAL")
        self.nodes = []
        self.index = 0
        self.last_refresh = 0
        self.refresh_interval = 300
        self._refresh(force=True)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.secret:
            h["Authorization"] = f"Bearer {self.secret}"
        return h

    def _refresh(self, force=False):
        now = time.time()
        if not force and now - self.last_refresh < self.refresh_interval:
            return
        r = requests.get(f"{self.api}/proxies", headers=self._headers(), timeout=5)
        r.raise_for_status()
        data = r.json()["proxies"]
        group = data.get(self.group)
        if not group:
            raise RuntimeError(f"Clash 中不存在代理组 {self.group}")
        nodes = [
            n for n in group.get("all", [])
            if n not in ("DIRECT", "REJECT")
            and not any(k.lower() in n.lower() for k in SKIP_KEYWORDS)
        ]
        if nodes != self.nodes:
            print(f"[代理] 节点列表刷新: {len(nodes)} 个可用节点", flush=True)
        self.nodes = nodes
        self.last_refresh = now

    def proxy_url(self):
        return f"socks5://127.0.0.1:{self.socks5_port}"

    def switch_to(self, name):
        r = requests.put(
            f"{self.api}/proxies/{self.group}",
            headers=self._headers(),
            json={"name": name},
            timeout=5,
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(f"切换节点失败: {r.status_code} {r.text}")
        return name

    def healthy_next(self, probe_url="https://www.krea.ai/login", timeout=12, max_tries=8):
        self._refresh()
        if not self.nodes:
            raise RuntimeError("无可用节点")
        for _ in range(max_tries):
            if self.index >= len(self.nodes):
                self.index = 0
            name = self.nodes[self.index]
            self.index += 1
            try:
                self.switch_to(name)
                proxies = {"http": self.proxy_url(), "https": self.proxy_url()}
                r = requests.get(probe_url, proxies=proxies, timeout=timeout,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code < 500:
                    print(f"[代理] → {name} ({self.proxy_url()}) [健康] [{self.index}/{len(self.nodes)}]", flush=True)
                    return self.proxy_url()
            except Exception:
                continue
        raise RuntimeError("连续切换节点均不健康")

    def next(self):
        self._refresh()
        if not self.nodes:
            raise RuntimeError("无可用节点")
        if self.index >= len(self.nodes):
            self.index = 0
        name = self.nodes[self.index]
        self.switch_to(name)
        self.index += 1
        print(f"[代理] → {name} ({self.proxy_url()}) [{self.index}/{len(self.nodes)}]", flush=True)
        return self.proxy_url()


if __name__ == "__main__":
    rot = ProxyRotator()
    rot.next()
