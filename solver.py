import os
import re
import time

import requests

LOCAL_SOLVER = os.getenv("LOCAL_SOLVER_URL", "http://127.0.0.1:5000")
NOPECHA_BASE = "https://api.nopecha.com"


class SolveError(RuntimeError):
    pass


def _local_solve_turnstile(website_url, sitekey, action=None, timeout=120):
    params = {"url": website_url, "sitekey": sitekey}
    if action:
        params["action"] = action
    r = requests.get(f"{LOCAL_SOLVER}/turnstile", params=params, timeout=30)
    if r.status_code != 200:
        raise SolveError(f"本地 solver 提交失败 HTTP {r.status_code}")
    data = r.json()
    task_id = data.get("task_id") or data.get("taskId")
    if not task_id:
        raise SolveError(f"本地 solver 响应异常: {data}")
    print(f"    [本地solver] 任务 {task_id[:8]}... 提交", flush=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            res = requests.get(f"{LOCAL_SOLVER}/result", params={"id": task_id}, timeout=15)
            if res.status_code == 200:
                rdata = res.json()
                token = rdata.get("value") or (rdata.get("solution") or {}).get("token")
                if token and token != "CAPTCHA_FAIL" and "CAPTCHA_NOT_READY" not in str(rdata.get("status", "")):
                    print(f"    [本地solver] 完成 token={token[:24]}...", flush=True)
                    return token
                if rdata.get("value") == "CAPTCHA_FAIL" or rdata.get("errorId"):
                    raise SolveError(f"本地 solver 解题失败: {rdata}")
        except SolveError:
            raise
        except Exception:
            continue
    raise SolveError("本地 solver 超时")


def _nopecha_solve(captcha_type, website_url, sitekey, proxy=None, useragent=None, timeout=180):
    api_key = os.getenv("NOPECHA_API_KEY")
    if not api_key:
        raise SolveError("NOPECHA_API_KEY 未配置")
    paths = {
        "turnstile": "/v1/token/turnstile",
        "hcaptcha": "/v1/token/hcaptcha",
        "recaptcha": "/v1/token/recaptcha2",
    }
    path = paths.get(captcha_type)
    if not path:
        raise SolveError(f"不支持的验证码类型: {captcha_type}")

    body = {"key": api_key, "sitekey": sitekey, "url": website_url}
    if captcha_type == "turnstile":
        if not proxy:
            raise SolveError("Nopecha Turnstile 任务必须提供代理")
        body["proxy"] = proxy
    if useragent:
        body["useragent"] = useragent

    resp = requests.post(f"{NOPECHA_BASE}{path}", json=body, timeout=30)
    if resp.status_code != 200:
        raise SolveError(f"Nopecha 提交失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "data" not in data:
        raise SolveError(f"Nopecha 响应异常: {data}")
    job_id = data["data"]
    print(f"    [Nopecha] 任务 {job_id[:16]}... 提交", flush=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        res = requests.get(f"{NOPECHA_BASE}{path}", params={"key": api_key, "id": job_id}, timeout=30)
        if res.status_code == 200:
            rdata = res.json()
            token = rdata.get("data")
            if token and isinstance(token, str) and len(token) > 20:
                print(f"    [Nopecha] 完成 token={token[:24]}...", flush=True)
                return token
        elif res.status_code == 409:
            continue
        elif res.status_code == 429:
            time.sleep(5)
            continue
        else:
            raise SolveError(f"Nopecha 取结果失败 HTTP {res.status_code}: {res.text[:200]}")
    raise SolveError("Nopecha 打码超时")


class NoCaptchaClient:
    def __init__(self, api_key=None, proxy=None, useragent=None):
        self.proxy = proxy
        self.useragent = useragent

    def status(self):
        api_key = os.getenv("NOPECHA_API_KEY")
        r = requests.get(f"{NOPECHA_BASE}/v1/status", params={"key": api_key}, timeout=20)
        return r.json()

    def solve(self, captcha_type, website_url, sitekey, timeout=180, action=None, cdata=None, proxy=None, useragent=None):
        proxy = proxy or self.proxy
        useragent = useragent or self.useragent

        if captcha_type == "turnstile":
            try:
                return _local_solve_turnstile(website_url, sitekey, action=action, timeout=timeout)
            except SolveError as e:
                print(f"    本地 solver 失败（{e}），切 Nopecha", flush=True)
        return _nopecha_solve(captcha_type, website_url, sitekey, proxy=proxy, useragent=useragent, timeout=timeout)


if __name__ == "__main__":
    from oututil import setup_utf8_stdout
    setup_utf8_stdout()
    from dotenv import load_dotenv
    load_dotenv()
    c = NoCaptchaClient()
    print(c.status())
