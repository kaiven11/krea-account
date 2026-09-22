# krea-account 架构文档

> 深入各模块的实现细节、时序图与踩坑记录。

## 1. 模块依赖图

```
register.py (主入口)
    ├── proxy_rotator.py   # Clash API 节点轮换 (healthy_next)
    ├── mail.py            # IMAP IDLE 收码 (fetch_verification_code)
    ├── solver.py          # NoCaptchaClient (本地 Solver → Nopecha)
    │       └── LOCAL_SOLVER_URL (:5000) / NOPECHA_API_KEY
    └── oututil.py         # UTF-8 stdout (Windows GBK 兼容)
```

依赖方向单向，无循环。

## 2. 注册时序（单账号完整生命周期）

```
t=0s    healthy_next() → Clash 切健康节点
t=1s    launch chromium (--disable-blink-features=AutomationControlled)
        + init_script: webdriver=undefined, turnstile.render hook
t=3s    goto /login → Clerk 渲染
t=8s    type(email) → click submit (button[type=submit], 防 Google 误点)
t=10s   密码框出现 → type(password) ×2
t=12s   wait_submit_enabled() 轮询按钮解锁
        ← Clerk 隐形 Turnstile 自然解出 (6~15s)
        ← [兜底] 本地 Solver 解 0x4AAAAAAARCAQia-P1X7s4C → 注入 + __ts_cb 回调
t=20s   click submit → Clerk POST /api/auth/email-flow (发 OTP)
t=22s   OTP 框出现 → mail.py IDLE 等待
t=30s   [邮件] 服务器 EXISTS 推送 → 提取 8 位码 → 分格/整格回填
t=38s   提交 → 302 /onboarding?redirect_to=/app
t=42s   skip_onboarding(): Skip → "Skip without credits" (3s)
t=45s   create_checkout_via_api(): fetch POST /api/payments/create-checkout-session
        {plan_id: creator_basic, subscription_interval: monthly}
t=50s   → {"url": "https://pay.krea.ai/c/pay/cs_live_..."}
t=52s   POST KIMHub /api/links → {"added": 1}
t=55s   写 accounts.txt / checkout_links.txt → browser.close()
```

## 3. 关键实现细节

### 3.1 wait_submit_enabled (Clerk Turnstile 时序)

```python
def wait_submit_enabled(page, timeout=60):
    deadline = time.time() + timeout
    btn = page.locator('button[type="submit"]').first
    while time.time() < deadline:
        if btn.is_visible() and not btn.is_disabled():
            return True
        if page.locator('input[autocomplete="one-time-code"]').first.is_visible():
            return True    # 已进入 OTP 阶段
        time.sleep(1)
    return False
```

为什么必须等：
- Clerk 在 Turnstile token 就绪前把 submit 按钮设为 `disabled`
- `disabled` 状态下 Playwright `click()` 会重试 30s 后超时（或 JS click 静默无效）
- 曾误判为「风控拦截 / Clerk 会话污染」，实际只是时序问题

### 3.2 turnstile.render Hook（兜底打码用）

```javascript
// init_script, 页面加载前注入
Object.defineProperty(window, 'turnstile', {
    set(v) {
        const orig = v.render;
        v.render = function(el, params) {
            window.__ts_render_params = {sitekey: params.sitekey, ...};
            if (params.callback) window.__ts_cb = params.callback;
            return orig.apply(this, arguments);
        };
        _obj = v;
    },
    get() { return wrap(_obj); }
});
```

- `__ts_render_params` — 捕获 Clerk 实际使用的 sitekey/action（可能与静态配置不同）
- `__ts_cb` — 捕获 Clerk 注册的回调，外部打码后调 `__ts_cb(token)` 触发 Clerk 状态机

### 3.3 create_checkout_via_api

```python
result = page.evaluate("""
    async (args) => {
        const r = await fetch('/api/payments/create-checkout-session', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                checkout_type: 'subscription',
                subscription_interval: args.interval,   // monthly|yearly
                plan_id: args.plan_id,                  // creator_basic
                compute_units: args.compute_units,      // 5000
                return_url: 'https://www.krea.ai/pricing'
            })
        });
        return {status: r.status, url: (await r.json()).url};
    }
""", args)
```

必须在 **krea.ai 页面上下文**内调用（同源 cookie + Clerk session）。
纯 requests 直调会 401。

### 3.4 IMAP IDLE 心跳

QQ 服务器 30 秒无数据断开 IDLE 连接：

```python
while time.time() - start < timeout:
    responses = client.idle_check(timeout=min(remaining, 29))  # ≤29s
    if not responses:
        client.idle_done()
        client.idle()          # 重置 IDLE
        continue
    client.idle_done()
    # search ALL → 过滤 uid > baseline → fetch RFC822 → 提取
```

### 3.5 健康探测

```python
def healthy_next(self, probe_url="https://www.krea.ai/login", timeout=12, max_tries=8):
    for _ in range(max_tries):
        name = self.nodes[self.index]; self.index += 1
        self.switch_to(name)                                  # PUT /proxies/GLOBAL
        r = requests.get(probe_url, proxies=proxies, timeout=timeout)
        if r.status_code < 500:
            return self.proxy_url()
    raise RuntimeError("连续切换节点均不健康")
```

SKIP_KEYWORDS 过滤机场公告节点：
`官网 / 公告 / 剩余 / 重置 / 到期 / 更新 / 订阅 / 全红 / 失联 / 流量 / 群 / 频道 / test`

## 4. 踩坑记录（重要度排序）

| # | 坑 | 现象 | 解法 |
|---|----|------|------|
| 1 | Clerk 按钮子串匹配 | `get_by_role("Continue")` 误点 "Continue with Google" 进入 OAuth | 优先 `button[type=submit]` |
| 2 | Turnstile 未解出就提交 | 按钮点击无效，页面纹丝不动，误判为风控 | `wait_submit_enabled()` 轮询解锁 |
| 3 | CSS 颜色值伪验证码 | `\d{6}` 命中 `#333333`，填入 333333 失败 | 先剥 style 标签，code-box 优先 |
| 4 | Basic 档 A/B 隐藏 | 登录态 pricing 只有 Get Pro/Max，点 Upgrade 不跳 | 弃页面按钮，直连 checkout API |
| 5 | Svelte 不响应 JS click | `el.click()` 无效 | Playwright 原生 `locator.click()` |
| 6 | onboarding Skip 无反应 | Skip 点了弹确认框，不点确认框页面不动 | Skip → "Skip without credits" 两连击 |
| 7 | QQ IMAP 轮询延迟 | 同连接内 SEARCH 结果缓存，1~2 分钟才见新邮件 | IMAP IDLE 服务器推送 |
| 8 | GBK 控制台崩溃 | 节点名 emoji 导致 UnicodeEncodeError | stdout UTF-8 TextIOWrapper |
| 9 | Clash 公告节点 | 切到 "联通移动用中转，全红更新订阅" 全断 | SKIP_KEYWORDS + 健康探测 |
| 10 | 假 sitekey 打码失败 | `0x4AAAAAAA` 兜底值被 CF 拒 | 从页面 JS 配置提取真实 sitekey |
| 11 | Nopecha IP 不一致 | token 解出但提交被 CF 拒，或需公网代理 | 弃云打码，本地 Solver 同出口 |
| 12 | /signup 404 污染会话 | 先访问 404 页再进 login，Clerk 状态异常 | 只走 /login |

## 5. 失败自愈策略

```
attempt_register(p, rotator, email):
    ├─ 注册失败 (无跳转/OTP 超时)     → return None → 换邮箱+节点重试
    ├─ checkout API 失败 (500/无url)  → return None → 整号丢弃
    ├─ info 有但无 checkout          → 视为失败, 换新号重注册
    └─ 成功                           → 写 accounts.txt + KIMHub
```

设计原则：**只要拿不到目标套餐（Basic）的链接，整个账号就废弃**。
半成品账号没有价值，快速失败比补救更省时间。

## 6. 性能数据

| 阶段 | 耗时 | 备注 |
|------|:---:|------|
| 节点切换+探测 | 1~3s | 健康节点命中时 |
| 打开登录页+渲染 | 4~5s | 首次 Clerk 加载 |
| 邮箱+密码填写 | 3s | 人类节奏 delay |
| Turnstile 解出 | 6~15s | 自然解出为主 |
| OTP 收码+回填 | 10~13s | IDLE 推送 1~3s + 提交跳转 |
| onboarding 跳过 | 3s | Skip 两连击 |
| checkout API | 1~5s | 页面上下文 fetch |
| KIMHub 上传 | <1s | 本地 HTTP |
| **单账号合计** | **~55s** | 实测 23:29~23:31 三连 |

批量吞吐：`ACCOUNT_COUNT=60` 理论约 1 小时/60 账号（受节点池与 IMAP 并发影响）。
