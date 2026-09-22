import os
import re
import sys
import time
import random
import json

from oututil import setup_utf8_stdout
setup_utf8_stdout()

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from proxy_rotator import ProxyRotator

load_dotenv()

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BASE = "https://www.krea.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("email", nargs="?", default=None, help="账号邮箱（默认取 accounts.txt 最新一条）")
    parser.add_argument("--plan", default="Basic", help="套餐：Basic / Pro / Max")
    parser.add_argument("--cycle", default="monthly", choices=["monthly", "yearly"], help="计费周期")
    args = parser.parse_args()

    email = args.email
    if not email:
        with open("accounts.txt", "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        email = lines[-1].split("|")[0]
        log(f"使用最新账号: {email}")

    checkout_links = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        ctx = b = None

        rotator = None
        try:
            rotator = ProxyRotator()
            proxy_url = rotator.next()
        except Exception as e:
            log(f"代理不可用（直连）: {e}")
            proxy_url = None

        ctx_kwargs = {
            "user_agent": UA,
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
        }
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}
        context = browser.new_context(**ctx_kwargs)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        def on_request(req):
            u = req.url
            if ("checkout.stripe.com" in u or "buy.stripe.com" in u) and req.method == "GET":
                checkout_links.append(u)

        def on_response(resp):
            u = resp.url
            if resp.status in (302, 303) or "checkout.stripe.com" in u:
                if "checkout.stripe.com" in u and resp.url not in checkout_links:
                    checkout_links.append(resp.url)

        page.on("request", on_request)
        page.on("response", on_response)

        clerk_resp = []
        def on_any_resp(resp):
            if resp.request.method == "POST":
                try:
                    clerk_resp.append(f"POST {resp.status} {resp.url[:140]} :: {resp.text()[:200]}")
                except Exception:
                    clerk_resp.append(f"POST {resp.status} {resp.url[:140]}")
        page.on("response", on_any_resp)

        log(f"登录 {email}")
        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        email_input = page.locator('input[type="email"]').first
        email_input.wait_for(state="visible", timeout=20000)
        email_input.click()
        email_input.type(email, delay=random.randint(40, 80))
        page.locator('button[type="submit"]').first.click()
        page.wait_for_timeout(4000)

        otp_input = None
        pwd_input = None
        for sel in ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]']:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=20000)
                otp_input = loc
                break
            except Exception:
                continue
        if not otp_input:
            try:
                loc = page.locator('input[type="password"]').first
                loc.wait_for(state="visible", timeout=5000)
                pwd_input = loc
            except Exception:
                pass

        if otp_input:
            log("登录走邮箱 OTP")
        elif pwd_input:
            log("密码表单出现，填密码提交")
            pwd_input.click()
            pwd_input.type(os.getenv("KREA_PASSWORD", ""), delay=random.randint(40, 80))
            page.wait_for_timeout(1000)
            try:
                page.locator('button[type="submit"]').first.click()
            except Exception:
                page.keyboard.press("Enter")
            log("等待登录结果（轮询 URL/OTP 框，最长 60s）...")
            deadline = time.time() + 60
            while time.time() < deadline:
                if "login" not in page.url:
                    break
                try:
                    otp_cand = page.locator('input[autocomplete="one-time-code"]').first
                    if otp_cand.is_visible():
                        otp_input = otp_cand
                        log("OTP 框出现")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2000)

        if otp_input:
            log("登录需 OTP，走邮箱收码")
            from mail import fetch_verification_code
            code = fetch_verification_code(timeout=180, recipient=email)
            if not code:
                log("收码失败，退出")
                sys.exit(2)
            log(f"验证码 {code}，填入")
            cells = page.locator('input[autocomplete="one-time-code"]')
            if cells.count() > 1:
                for i, ch in enumerate(code[:cells.count()]):
                    cells.nth(i).press_sequentially(ch, delay=120)
            else:
                otp_input.click()
                otp_input.press_sequentially(code, delay=120)
            page.wait_for_timeout(3000)
            try:
                sub = page.locator('button[type="submit"]').first
                if sub.is_visible():
                    sub.click()
            except Exception:
                page.keyboard.press("Enter")

        try:
            page.wait_for_url(lambda u: "login" not in u, timeout=30000)
        except PWTimeout:
            pass
        log(f"登录后 URL: {page.url}")
        if "login" in page.url:
            if clerk_resp:
                log("Clerk POST 响应 >>>")
                for e in clerk_resp[-8:]:
                    log(f"  | {e}")
            try:
                body_text = page.evaluate("() => document.body.innerText")
                log("登录卡住，页面文本 >>>")
                for line in body_text.splitlines():
                    line = line.strip()
                    if line:
                        log(f"  | {line}")
            except Exception:
                pass
            page.screenshot(path="login_fail.png", full_page=True)
            sys.exit(2)

        log(f"打开 pricing 页，选择 {args.plan} / {args.cycle}")
        page.goto(f"{BASE}/pricing", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        if args.cycle == "monthly":
            try:
                tab = page.get_by_text("Monthly", exact=True).first
                if tab.is_visible():
                    tab.click()
                    page.wait_for_timeout(1500)
                    log("已切换 Monthly")
            except Exception:
                pass

        client = NoCaptchaSolverShim()

        log(f"点击 Get {args.plan}")
        clicked = False
        for btn in page.get_by_role("button", name=f"Get {args.plan}").all():
            try:
                if btn.is_visible():
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            for btn in page.get_by_text(f"Get {args.plan}", exact=True).all():
                try:
                    if btn.is_visible():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            log("未找到套餐按钮，退出")
            sys.exit(3)

        log("等待跳转 checkout...")
        page.wait_for_timeout(8000)
        for pg in context.pages:
            log(f"  [tab] {pg.url}")
        cur = page.url
        log(f"当前 URL: {cur}")

        if "checkout" not in cur and not checkout_links:
            try:
                body_text = page.evaluate("() => document.body.innerText")
                log("点击后页面文本 >>>")
                for line in body_text.splitlines()[:30]:
                    line = line.strip()
                    if line:
                        log(f"  | {line}")
            except Exception:
                pass
            dialogs = page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('[role=dialog], [class*=modal], [class*=overlay], [class*=popup]').forEach(d => {
                    if (d.offsetParent !== null) out.push(d.innerText.slice(0, 300));
                });
                return out;
            }
            """)
            for d in dialogs:
                log(f"  [弹窗] {d}")

        stripe_url = None
        if "checkout.stripe.com" in cur:
            stripe_url = cur
        else:
            if checkout_links:
                stripe_url = checkout_links[-1]
            else:
                try:
                    body_text = page.evaluate("() => document.body.innerText")
                    m = re.search(r"https://checkout\.stripe\.com[^\s\"']*", body_text)
                    if m:
                        stripe_url = m.group(0)
                except Exception:
                    pass

        if stripe_url:
            log(f"✅ Stripe 支付链接: {stripe_url[:120]}...")
            with open("checkout_links.txt", "a", encoding="utf-8") as f:
                f.write(f"{email}|{args.plan}|{args.cycle}|{stripe_url}\n")
            log("已写入 checkout_links.txt")

            amount = None
            try:
                txt = page.evaluate("() => document.body.innerText")
                m = re.search(r"\$\s*(\d+[\.]\d{2})", txt)
                if m:
                    amount = m.group(1)
            except Exception:
                pass
            plan_title = args.plan
            try:
                txt2 = page.evaluate("() => document.body.innerText")
                m2 = re.search(r"Subscribe to ([^\n]+)", txt2)
                if m2:
                    plan_title = m2.group(1).strip()
            except Exception:
                pass

            payload = {
                "url": stripe_url,
                "product": f"Krea {args.plan}",
                "merchant": "Krea AI",
                "amount": amount or ("9.00" if args.plan.lower() == "basic" else None),
                "currency": "USD",
                "plan": plan_title,
                "account": email,
                "tags": ["krea", args.plan.lower(), args.cycle],
                "priority": 0,
                "limit": 1,
                "type": "full",
                "meta": {"cycle": args.cycle, "registered_by": "krea-register"},
            }
            try:
                import requests as _rq
                r = _rq.post("http://127.0.0.1:4000/api/links", json=payload, timeout=10)
                log(f"KIMHub 上传: {r.status_code} {r.text[:200]}")
            except Exception as e:
                log(f"KIMHub 上传失败: {e}")
        else:
            log("未捕获到 Stripe 链接，页面文本 >>>")
            try:
                body_text = page.evaluate("() => document.body.innerText")
                log(body_text[:800])
            except Exception:
                pass
            page.screenshot(path="checkout_fail.png", full_page=True)

        browser.close()


def NoCaptchaSolverShim():
    return None


if __name__ == "__main__":
    main()
