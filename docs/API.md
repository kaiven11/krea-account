# Krea 内部 API 参考

> 从 krea.ai 前端逆向得到的关键接口，用于自动化。

## 1. 创建 Checkout 会话（核心）

```
POST https://www.krea.ai/api/payments/create-checkout-session
Content-Type: application/json
Cookie: Clerk 会话 (必须已登录)
```

### 请求体

```json
{
  "checkout_type": "subscription",
  "subscription_interval": "monthly",
  "plan_id": "creator_basic",
  "compute_units": 5000,
  "return_url": "https://www.krea.ai/pricing"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `checkout_type` | string | `subscription`（订阅）|
| `subscription_interval` | string | `monthly` / `yearly` |
| `plan_id` | string | 套餐 ID，见下表 |
| `compute_units` | number | 算力单位，需与 plan 匹配 |
| `return_url` | string | 支付后返回地址 |

### 套餐映射

| plan_id | 档位 | compute_units | 月付 | 年付 |
|---------|------|:---:|:---:|:---:|
| `creator_basic` | Basic | 5000 | $9.00 | — |
| `creator_pro` | Pro | 20000 | $21.00 | — |
| `creator_max` | Max | 40000 | ❌ 500 | ❌ |

> `creator_max` 自助订阅返回 500，需走 Sales。

### 响应

```json
{ "url": "https://pay.krea.ai/c/pay/cs_live_b1..." }
```

`url` 即 Stripe Checkout 链接，直接可支付。有效期通常 24 小时。

### 调用方式（Playwright 页面上下文）

```python
result = page.evaluate("""
    async (args) => {
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
    }
""", {"plan_id": "creator_basic", "compute_units": 5000, "interval": "monthly"})
```

必须同源（krea.ai 域）调用以携带 Clerk cookie。纯 requests 会 401。

---

## 2. 登录/注册相关（Clerk 托管）

Clerk 通过 `www.krea.ai/api/auth/*` 代理。

### 邮件流程

```
POST /api/auth/email-flow
→ {"flow": "login" | "signup", "emailAction": "Log in" | "Sign up"}
```

触发后 Clerk 发送 OTP 邮件（8 位码）。

### 会话 cookie

登录后浏览器持有 Clerk session cookie，`create-checkout-session` 依赖它。

---

## 3. 账户数据

### Billing 数据

```
GET /api/billing-data
→ {
    "userId": "...",
    "planType": "free",
    "plan": { "id": "free", "entitlements": { "computeUnits": 100, ... } }
  }
```

### 订阅状态

```
GET /api/billing/subscription
→ []   (免费用户)
```

### 争议状态

```
GET /api/billing/dispute-status
→ {"hasOpenDispute": false}
```

---

## 4. 前端配置（Turnstile sitekey）

页面内联脚本含 `PUBLIC_*` 配置：

```javascript
// 从 /login 或 /pricing 页面 HTML 提取
"PUBLIC_TURNSTILE_EMAIL_KEY": "0x4AAAAAAARCAQia-P1X7s4C"
"PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_live_..."
"PUBLIC_POSTHOG_API_KEY": "phc_..."
```

`PUBLIC_TURNSTILE_EMAIL_KEY` 即注册/登录表单使用的 Turnstile sitekey。

---

## 5. 埋点接口（忽略）

以下接口用于分析，自动化无需处理：

- `POST /api/diagnostic` — 前端事件上报
- `POST php.krea.ai/i/v0/e/` — PostHog
- `POST /api/auth/track`

---

## 6. 页面路由

| 路由 | 说明 |
|------|------|
| `/login` | 登录/注册入口（唯一，`/signup` 会 404）|
| `/pricing` | 定价页（登录态有 A/B）|
| `/app` | 应用主界面（需登录）|
| `/onboarding?redirect_to=` | 注册后引导向导（1/6 步）|

> `/signup` 返回 404，且会污染 Clerk 会话状态，务必只用 `/login`。
