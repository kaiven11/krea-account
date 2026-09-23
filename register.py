import os
import re
import sys
import time
import random
import string
from urllib.parse import quote

from oututil import setup_utf8_stdout

setup_utf8_stdout()

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from solver import NoCaptchaClient, SolveError
from mail import fetch_verification_code
from proxy_rotator import ProxyRotator

load_dotenv()

EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "cmzw.cloud")
EMAIL_PREFIX = os.getenv("EMAIL_PREFIX", "reg")
PASSWORD = os.getenv("KREA_PASSWORD", "")
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_HOST = os.getenv("IMAP_HOST", "")
ACCOUNT_COUNT = int(os.getenv("ACCOUNT_COUNT", "1"))
PROXY_MODE = os.getenv("PROXY_MODE", "clash")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BASE = "https://www.krea.ai"
TURNSTILE_SITEKEY = "0x4AAAAAAARCAQia-P1X7s4C"
# KIMHub 商品配额控制
KIMHUB_URL = os.getenv("KIMHUB_URL", "http://127.0.0.1:4000")
KREA_PRODUCT = os.getenv("KREA_PRODUCT", "Krea Basic")
QUOTA_ENABLED = os.getenv("QUOTA_ENABLED", "true").lower() == "true"

SIGNUP_URLS = [f"{BASE}/login"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gen_email():
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{EMAIL_PREFIX}{suffix}@{EMAIL_DOMAIN}"


def check_quota():
    """向 KIMHub 查询当前商品的注册配额。
    返回 (allowed, quota, info)。quota: -1=不限, 0=不可注册, >0=还能注册几个。
    KIMHub 离线时返回 (True, -1, {...}) 以便独立运行。"""
    if not QUOTA_ENABLED:
        return True, -1, {"quota": -1, "reason": "quota_disabled"}
    import requests as _rq
    url = f"{KIMHUB_URL}/api/products/{quote(KREA_PRODUCT)}/acquire-quota"
    try:
        r = _rq.get(url, timeout=8)
        d = r.json()
        return bool(d.get("allowed")), d.get("quota", -1), d
    except Exception as e:
        log(f"⚠️ KIMHub 配额查询失败（将独立运行，不受控制）: {e}")
        return True, -1, {"quota": -1, "reason": "kimhub_offline"}


DETECT_JS = """
() => {
    const el = document.querySelector('.cf-turnstile[data-sitekey], [class*="cf-turnstile"][data-sitekey]');
    if (el) return {type: 'turnstile', sitekey: el.getAttribute('data-sitekey')};
    const cfFrame = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (cfFrame) {
        try {
            const u = new URL(cfFrame.src);
            const k = u.searchParams.get('sitekey');
            if (k) return {type: 'turnstile', sitekey: k};
        } catch (e) {}
    }
    const hc = document.querySelector('.h-captcha[data-sitekey]');
    if (hc) return {type: 'hcaptcha', sitekey: hc.getAttribute('data-sitekey')};
    const gr = document.querySelector('.g-recaptcha[data-sitekey]');
    if (gr) return {type: 'recaptcha', sitekey: gr.getAttribute('data-sitekey')};
    const grFrame = document.querySelector('iframe[src*="recaptcha/api2"]');
    if (grFrame) {
        try {
            const u = new URL(grFrame.src);
            const k = u.searchParams.get('k');
            if (k) return {type: 'recaptcha', sitekey: k};
        } catch (e) {}
    }
    return null;
}
"""

INJECT_JS = """
(token) => {
    let count = 0;
    const fire = (el) => {
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    };
    document.querySelectorAll(
        'input[name="cf-turnstile-response"], textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"]'
    ).forEach(el => { el.value = token; fire(el); count++; });
    return count;
}
"""


def detect_captcha(page):
    try:
        return page.evaluate(DETECT_JS)
    except Exception:
        return None


def detect_hidden_turnstile(page):
    try:
        hidden = page.evaluate("""
        () => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (!el) return null;
            const wrap = el.closest('[data-sitekey]') || document.querySelector('[data-sitekey]');
            return {sitekey: wrap ? wrap.getAttribute('data-sitekey') : null};
        }
        """)
        if hidden:
            return {"type": "turnstile", "sitekey": hidden.get("sitekey") or "0x4AAAAAAA"}
    except Exception:
        pass
    return None


def inject_token(page, token):
    for frame in page.frames:
        try:
            frame.evaluate(INJECT_JS, token)
        except Exception:
            pass


def wait_cf_interstitial(page, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "Just a moment" not in page.title():
            return True
        time.sleep(1)
    return False


def find_email_input(page):
    for sel in ['input[type="email"]', 'input[name="email"]',
                'input[autocomplete="email"]', 'input[id*="email" i]']:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=1500)
            return loc
        except Exception:
            continue
    return None


def click_submit(page):
    candidates = [
        page.locator('button[type="submit"]').first,
        page.get_by_role("button", name=re.compile(r"^(continue|sign up|register)$", re.I)),
    ]
    for c in candidates:
        try:
            c.wait_for(state="visible", timeout=1500)
            c.click()
            return True
        except Exception:
            continue
    return False


def wait_submit_enabled(page, timeout=60):
    """等待 Clerk 隐形 Turnstile 解出、提交按钮变为可用。"""
    deadline = time.time() + timeout
    btn = page.locator('button[type="submit"]').first
    while time.time() < deadline:
        try:
            if btn.is_visible() and not btn.is_disabled():
                return True
        except Exception:
            pass
        try:
            if page.locator('input[autocomplete="one-time-code"]').first.is_visible():
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def click_submit_waited(page, timeout=60):
    """等按钮可用再点；不可用则强制点击一次。"""
    if wait_submit_enabled(page, timeout=timeout):
        try:
            page.locator('button[type="submit"]').first.click(timeout=5000)
            return True
        except Exception:
            pass
    return click_submit(page)


def open_signup(page):
    for url in SIGNUP_URLS:
        try:
            log(f"打开 {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            wait_cf_interstitial(page)
            if find_email_input(page):
                return True
            signup_link = page.get_by_role("link", name=re.compile(r"sign up|注册", re.I)).first
            if signup_link.is_visible():
                signup_link.click()
                page.wait_for_timeout(3000)
                if find_email_input(page):
                    return True
        except Exception as e:
            log(f"  {url} 失败: {e}")
    return False


def wait_for_otp(page, timeout=25):
    selectors = [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name*="code" i]',
        'input[id*="code" i]',
        'iframe[src*="clerk"]',
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    log(f"发现验证码输入元素: {sel}")
                    return loc
            except Exception:
                continue
        time.sleep(1)
    return None


def solve_captcha_with_client(page, cf_captcha, client):
    log(f"出现 Turnstile 人机验证，sitekey={cf_captcha['sitekey']}")
    proxy_cfg = {"scheme": "socks5", "host": "127.0.0.1", "port": int(os.getenv("CLASH_SOCKS5_PORT", "7891"))}
    try:
        token = client.solve(
            cf_captcha["type"], page.url, cf_captcha["sitekey"],
            proxy=proxy_cfg, useragent=UA,
        )
        inject_token(page, token)
        page.wait_for_timeout(1500)
        return True
    except SolveError as e:
        log(f"  打码失败: {e}")
        return False


def wait_for_otp_or_challenge(page, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = page.evaluate("""
            () => {
                const otp = document.querySelector('input[autocomplete="one-time-code"], input[inputmode="numeric"]');
                if (otp) return {otp: true};
                const cfFrame = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (cfFrame) {
                    let sitekey = null;
                    try {
                        const u = new URL(cfFrame.src);
                        sitekey = u.searchParams.get('sitekey');
                    } catch (e) {}
                    if (!sitekey) {
                        const w = document.querySelector('[data-sitekey]');
                        if (w) sitekey = w.getAttribute('data-sitekey');
                    }
                    return {otp: false, type: 'turnstile', sitekey: sitekey || '0x4AAAAAAA'};
                }
                const hidden = document.querySelector('input[name="cf-turnstile-response"]');
                if (hidden && !hidden.value) {
                    const w = document.querySelector('[data-sitekey]');
                    const k = w ? w.getAttribute('data-sitekey') : null;
                    if (k) return {otp: false, type: 'turnstile', sitekey: k};
                }
                return null;
            }
            """)
            if result:
                if result.get("otp"):
                    log("发现 OTP 输入框")
                    loc = page.locator('input[autocomplete="one-time-code"]').first
                    try:
                        loc.wait_for(state="visible", timeout=3000)
                    except Exception:
                        loc = page.locator('input[inputmode="numeric"]').first
                    return loc, None
                return None, result
        except Exception:
            pass
        time.sleep(1)
    return None, None


def register_one(page, context, email, rotator=None):
    start_ts = time.time()
    if not open_signup(page):
        page.screenshot(path="error_no_signup.png")
        log("未找到注册表单")
        return None

    log(f"填写邮箱 {email}")
    email_input = find_email_input(page)
    email_input.click()
    email_input.type(email, delay=random.randint(40, 90))

    log("点击继续（第一步）")
    if not click_submit(page):
        page.keyboard.press("Enter")

    pwd_input = None
    for sel in ['input[autocomplete="new-password"]', 'input[type="password"]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=20000)
            pwd_input = loc
            break
        except Exception:
            continue

    if pwd_input:
        log("第二步：填写密码")
        pwds = page.locator('input[type="password"]')
        for i in range(min(pwds.count(), 2)):
            box = pwds.nth(i)
            box.click()
            box.type(PASSWORD, delay=random.randint(40, 90))
        page.wait_for_timeout(2000)
    else:
        log("未出现密码框，按邮箱验证码流程继续")

    client = NoCaptchaClient()

    log("等待人机验证解出、提交按钮可用...")
    if wait_submit_enabled(page, timeout=60):
        log("按钮可用，提交")
    else:
        log("按钮未启用，启用打码兜底")
        try:
            sitekey = TURNSTILE_SITEKEY
            try:
                rp = page.evaluate("() => window.__ts_render_params")
                if rp and rp.get("sitekey"):
                    sitekey = rp["sitekey"]
                    log(f"  捕获真实 sitekey={sitekey} action={rp.get('action')}")
            except Exception:
                pass
            token = client.solve("turnstile", page.url, sitekey)
            res = page.evaluate("""
            (token) => {
                let out = {injected: 0, cbCalled: false};
                document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(el => {
                    el.value = token;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    out.injected++;
                });
                if (window.__ts_cb) { try { window.__ts_cb(token); out.cbCalled = true; } catch (e) { out.err = String(e); } }
                return out;
            }
            """, token)
            log(f"  注入结果: {res}")
            page.wait_for_timeout(2000)
        except SolveError as e:
            log(f"  打码兜底失败: {e}")
    if not click_submit(page):
        page.keyboard.press("Enter")

    clerk_errors = []
    clerk_posts = []
    def _safe_post_data(req):
        try:
            return (req.post_data_buffer or b"").decode("utf-8", "ignore")
        except Exception:
            return ""

    def on_request(req):
        if req.method == "POST":
            clerk_posts.append(f"POST {req.url[:160]}")
    def on_clerk_resp(resp):
        if resp.request.method == "POST" and email.split("@")[0] in _safe_post_data(resp.request):
            try:
                body = resp.text()[:400]
            except Exception:
                body = "<no body>"
            clerk_posts.append(f"  -> {resp.status} {body}")
            if resp.status >= 400:
                clerk_errors.append(f"{resp.status} {resp.url[:120]} -> {body}")
    page.on("request", on_request)
    page.on("response", on_clerk_resp)

    otp_input = None
    for wait_round in range(3):
        log(f"等待结果（第 {wait_round + 1} 轮：OTP 或人机验证）...")
        otp_input, cf_captcha = wait_for_otp_or_challenge(page, timeout=30)
        if otp_input:
            break
        if cf_captcha:
            solved = solve_captcha_with_client(page, cf_captcha, client)
            log("重新提交")
            click_submit_waited(page, timeout=30)
            page.wait_for_timeout(3000)
        else:
            try:
                body_text = page.evaluate("() => document.body.innerText")
                log("页面文本 >>>")
                for line in body_text.splitlines():
                    line = line.strip()
                    if line:
                        log(f"  | {line}")
            except Exception:
                pass
            log("等待按钮可用后重试提交")
            click_submit_waited(page, timeout=30)
            page.wait_for_timeout(3000)

    if otp_input:
        log("等待 QQ 邮箱实时收取验证码...")
        code = fetch_verification_code(timeout=180, since=start_ts - 30, recipient=email)
        if code:
            log(f"获取到验证码 {code}，填入")
            try:
                cells = page.locator('input[autocomplete="one-time-code"]')
                if cells.count() > 1:
                    log(f"  分格输入，共 {cells.count()} 格")
                    for i, ch in enumerate(code[:cells.count()]):
                        cells.nth(i).press_sequentially(ch, delay=120)
                else:
                    otp_input.click()
                    otp_input.press_sequentially(code, delay=120)
            except Exception as e:
                log(f"  输入异常，退回整体输入: {e}")
                otp_input.click()
                otp_input.press_sequentially(code, delay=120)
            page.wait_for_timeout(3000)
            page.screenshot(path="after_otp.png")
            if not click_submit(page):
                page.keyboard.press("Enter")
        else:
            log("未收到验证码，请在浏览器窗口手动处理")
            if not HEADLESS:
                try:
                    page.wait_for_url(lambda u: "login" not in u, timeout=120000)
                except PWTimeout:
                    pass

    try:
        page.wait_for_url(lambda u: not any(s in u for s in ("/signup", "/login")), timeout=60000)
        log(f"注册完成，当前页面: {page.url}")
    except PWTimeout:
        log("等待跳转超时")

    page.wait_for_timeout(3000)
    page.screenshot(path=f"result_{email.split('@')[0]}.png", full_page=True)
    cookies = [c for c in context.cookies() if "krea" in c["domain"]]
    log(f"捕获 {len(cookies)} 条 krea cookies")

    ok = not any(s in page.url for s in ("/login", "/signup"))
    if not ok:
        log(f"注册未完成（仍停留在 {page.url}），判定失败")
        return None
    return {"email": email, "password": PASSWORD, "cookies": len(cookies), "url": page.url}


def skip_onboarding(page, max_rounds=6):
    """快速跳过 onboarding：点右上 Skip → 确认框 'Skip without credits'。"""
    skip_js = """
    () => {
        const all = Array.from(document.querySelectorAll('button, a')).filter(b => b.offsetParent);
        const confirm = all.find(b => /skip without credits/i.test(b.innerText.trim()));
        if (confirm) { confirm.click(); return 'confirm'; }
        const confirm2 = all.find(b => /skip( my| anyway| now)?/i.test(b.innerText.trim())
                                  && !/^(Skip)$/i.test(b.innerText.trim()));
        if (confirm2) { confirm2.click(); return 'confirm-variant'; }
        const skip = all.find(b => b.innerText.trim() === 'Skip');
        if (skip) { skip.click(); return 'skip'; }
        return 'none';
    }
    """
    deadline = time.time() + 20
    rounds = 0
    while "onboarding" in page.url and time.time() < deadline and rounds < max_rounds:
        act = page.evaluate(skip_js)
        if act == "none":
            time.sleep(1)
            continue
        log(f"  onboarding: {act}")
        rounds += 1
        page.wait_for_timeout(1500)
    return "onboarding" not in page.url


PLAN_IDS = {
    "basic": {"plan_id": "creator_basic", "compute_units": 5000, "amount": "9.00"},
    "pro":   {"plan_id": "creator_pro",   "compute_units": 20000, "amount": "21.00"},
    "max":   {"plan_id": "creator_max",   "compute_units": 40000, "amount": "63.00"},
}


def create_checkout_via_api(page, plan, cycle):
    """直接调用 Krea 的 create-checkout-session API，返回 Stripe 链接。
    绕开 pricing 页面的 A/B 按钮问题，秒回、稳定。"""
    key = plan.lower()
    cfg = PLAN_IDS.get(key)
    if not cfg:
        log(f"未知套餐 {plan}")
        return None
    interval = "yearly" if cycle == "yearly" else "monthly"
    result = page.evaluate("""
    async (args) => {
        try {
            const r = await fetch('/api/payments/create-checkout-session', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    checkout_type: 'subscription',
                    subscription_interval: args.interval,
                    plan_id: args.plan_id,
                    compute_units: args.compute_units,
                    return_url: 'https://www.krea.ai/pricing'
                })
            });
            const j = await r.json().catch(() => ({}));
            return {status: r.status, url: j.url || '', message: j.message || ''};
        } catch (e) { return {error: String(e)}; }
    }
    """, {"plan_id": cfg["plan_id"], "compute_units": cfg["compute_units"], "interval": interval})
    if result.get("status") == 200 and result.get("url"):
        return result["url"]
    log(f"  create-checkout-session 失败: {result}")
    return None


def grab_checkout(page, email, plan=None, cycle=None):
    """复用已登录会话，直接调 API 获取指定套餐的 Stripe 支付链接并上传 KIMHub。"""
    plan = plan or os.getenv("CHECKOUT_PLAN", "Basic")
    cycle = cycle or os.getenv("CHECKOUT_CYCLE", "monthly")
    cfg = PLAN_IDS.get(plan.lower(), PLAN_IDS["basic"])

    log(f"直接调 API 创建 {plan} / {cycle} checkout...")
    # 确保处于已登录的 krea.ai 页面上下文（API 需要会话 cookie）
    if "krea.ai" not in page.url:
        try:
            page.goto(f"{BASE}/pricing", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"  打开页面失败: {e}")
            return None
    skip_onboarding(page)

    stripe_url = create_checkout_via_api(page, plan, cycle)
    if not stripe_url:
        log(f"未获取到 {plan} 链接，丢弃换新号")
        return None

    log(f"✅ Stripe 支付链接: {stripe_url[:110]}...")
    with open("checkout_links.txt", "a", encoding="utf-8") as f:
        f.write(f"{email}|{plan}|{cycle}|{stripe_url}\n")

    payload = {
        "url": stripe_url,
        "product": f"Krea {plan}",
        "merchant": "Krea AI",
        "amount": cfg["amount"],
        "currency": "USD",
        "plan": plan,
        "account": email,
        "tags": ["krea", plan.lower(), cycle],
        "priority": 0,
        "limit": 1,
        "type": "full",
        "meta": {"cycle": cycle, "plan_id": cfg["plan_id"], "registered_by": "krea-register"},
    }
    kimhub_url = os.getenv("KIMHUB_URL", "http://127.0.0.1:4000")
    try:
        import requests as _rq
        r = _rq.post(f"{kimhub_url}/api/links", json=payload, timeout=10)
        log(f"KIMHub 上传: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"KIMHub 上传失败: {e}")
    return stripe_url


def attempt_register(p, rotator, email):
    proxy_url = None
    if rotator:
        try:
            proxy_url = rotator.healthy_next()
        except Exception as e:
            log(f"健康节点选取失败，退回普通轮换: {e}")
            try:
                proxy_url = rotator.next()
            except Exception as e2:
                log(f"轮换节点失败（直连）: {e2}")

    browser = p.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx_kwargs = {
        "user_agent": UA,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
    }
    if proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}
    context = browser.new_context(**ctx_kwargs)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    context.add_init_script(r"""
    (function() {
        window.__ts_cb = null;
        window.__ts_render_params = null;
        let _obj = null;
        const wrap = (v) => {
            if (v && v.render && !v.__hooked) {
                const orig = v.render;
                v.render = function(el, params) {
                    try {
                        window.__ts_render_params = {
                            sitekey: params && params.sitekey,
                            action: params && params.action,
                            cdata: params && params.cdata
                        };
                        if (params && params.callback) window.__ts_cb = params.callback;
                    } catch (e) {}
                    return orig.apply(this, arguments);
                };
                try { v.__hooked = true; } catch (e) {}
            }
            return v;
        };
        Object.defineProperty(window, 'turnstile', {
            configurable: true,
            set(v) { wrap(v); _obj = v; },
            get() { return wrap(_obj); }
        });
    })();
    """)
    page = context.new_page()
    try:
        info = register_one(page, context, email)
        if info and os.getenv("CHECKOUT_ENABLED", "true").lower() == "true":
            # 单会话完结：注册后直接 Skip → Skip without credits → 抓链接
            # 失败则保存会话，稍后用 checkout_after_relogin 重试
            try:
                log("跳过 onboarding（Skip → Skip without credits）...")
                skip_onboarding(page)
                info["checkout"] = grab_checkout(page, email)
            except Exception as e:
                log(f"获取支付链接异常: {e}")
                info["checkout"] = None
            if not info["checkout"]:
                try:
                    state_path = os.path.abspath(f"session_{email.split('@')[0]}.json")
                    context.storage_state(path=state_path)
                    info["_state_path"] = state_path
                except Exception:
                    pass
        return info
    except Exception as e:
        log(f"attempt_register 异常: {e}")
        return None
    finally:
        try:
            browser.close()
        except Exception:
            pass


def checkout_after_relogin(p, state_path, email, proxy_url=None, plan=None, cycle=None):
    plan = plan or os.getenv("CHECKOUT_PLAN", "Basic")
    cycle = cycle or os.getenv("CHECKOUT_CYCLE", "monthly")

    browser = p.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx_kwargs = {
        "user_agent": UA,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
        "storage_state": state_path,
    }
    if proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}
    context = browser.new_context(**ctx_kwargs)
    page = context.new_page()
    url = None
    try:
        # 验证登录态
        page.goto(f"{BASE}/app", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if "login" in page.url:
            log("  会话失效，走完整登录")
            do_login(page, email)
        log(f"  当前 URL: {page.url}")

        # 秒过 onboarding：Skip → Skip without credits
        skip_onboarding(page)

        url = grab_checkout(page, email, plan, cycle)
    except Exception as e:
        log(f"checkout 流程异常: {e}")
    finally:
        browser.close()
    return url


def do_login(page, email):
    """已注册账号的完整登录（密码或 OTP）。"""
    from mail import fetch_verification_code
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    ei = page.locator('input[type="email"]').first
    ei.wait_for(state="visible", timeout=20000)
    ei.click()
    ei.type(email, delay=random.randint(40, 80))
    page.locator('button[type="submit"]').first.click()
    page.wait_for_timeout(4000)

    otp_input = None
    try:
        loc = page.locator('input[type="password"]').first
        loc.wait_for(state="visible", timeout=8000)
        loc.click()
        loc.type(PASSWORD, delay=random.randint(40, 80))
        page.wait_for_timeout(800)
        if wait_submit_enabled(page, timeout=40):
            page.locator('button[type="submit"]').first.click(timeout=5000)
        else:
            page.locator('button[type="submit"]').first.click(timeout=5000)
        page.wait_for_timeout(5000)
    except Exception:
        pass

    try:
        loc = page.locator('input[autocomplete="one-time-code"]').first
        loc.wait_for(state="visible", timeout=10000)
        otp_input = loc
    except Exception:
        pass

    if otp_input:
        log("  登录走 OTP")
        code = fetch_verification_code(timeout=150, recipient=email)
        if not code:
            log("  收码失败")
            return False
        log(f"  验证码 {code}")
        cells = page.locator('input[autocomplete="one-time-code"]')
        if cells.count() > 1:
            for i, ch in enumerate(code[:cells.count()]):
                cells.nth(i).press_sequentially(ch, delay=110)
        else:
            otp_input.click()
            otp_input.press_sequentially(code, delay=110)
        page.wait_for_timeout(2000)
        try:
            page.locator('button[type="submit"]').first.click(timeout=4000)
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

    try:
        page.wait_for_url(lambda u: "login" not in u, timeout=25000)
    except PWTimeout:
        pass
    return "login" not in page.url


def main():
    rotator = None
    if PROXY_MODE == "clash":
        try:
            rotator = ProxyRotator()
        except Exception as e:
            log(f"Clash 轮换器初始化失败（将以直连运行）: {e}")

    # ★ 启动前查 KIMHub 配额: 商品禁用 / 池满 → 直接不启动
    allowed, quota, qinfo = check_quota()
    log(f"KIMHub 配额 [{KREA_PRODUCT}]: allowed={allowed} quota={quota} "
        f"inPool={qinfo.get('inPool')} maxLinks={qinfo.get('maxLinks')} reason={qinfo.get('reason')}")
    if not allowed:
        log(f"❌ 商品不可注册（{qinfo.get('reason')}），退出。如需强制运行，设 QUOTA_ENABLED=false")
        return

    target = ACCOUNT_COUNT if quota < 0 else min(ACCOUNT_COUNT, quota)
    if target < ACCOUNT_COUNT:
        log(f"配额限制: 本次最多注册 {target} 个（ACCOUNT_COUNT={ACCOUNT_COUNT}, 剩余配额={quota}）")
    if target <= 0:
        log("❌ 无可用配额，退出")
        return

    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    results = []
    with sync_playwright() as p:
        for i in range(target):
            # ★ 每轮注册前回查配额
            allowed, quota, qinfo = check_quota()
            if not allowed:
                log(f"⛔ 配额已耗尽（{qinfo.get('reason')}），提前停止（已成功 {len(results)} 个）")
                break
            log(f"===== 账号 {i + 1}/{target} =====")
            info = None
            for attempt in range(1, max_retries + 1):
                email = gen_email()
                log(f"--- 尝试 {attempt}/{max_retries}: {email} ---")
                try:
                    info = attempt_register(p, rotator, email)
                except Exception as e:
                    log(f"本次尝试异常: {e}")
                    info = None
                # 注册成功但没拿到 Basic 链接 → 视为失败, 换新号重注册
                if info and os.getenv("CHECKOUT_ENABLED", "true").lower() == "true" and not info.get("checkout"):
                    log("注册成功但无 Basic 链接，丢弃该号，重新注册")
                    info = None
                if info:
                    break
                log("失败，换邮箱+换节点重试")
                time.sleep(random.randint(5, 10))

            if info:
                results.append(info)
                with open("accounts.txt", "a", encoding="utf-8") as f:
                    f.write(f"{info['email']}|{info['password']}|{info['url']}\n")
                log(f"✅ 已写入 accounts.txt: {info['email']}")
            else:
                log(f"❌ 账号 {i + 1} 重试 {max_retries} 次仍失败")

    log(f"===== 批量完成: 成功 {len(results)}/{target} =====")
    for r in results:
        log(f"  {r['email']}")


if __name__ == "__main__":
    main()
