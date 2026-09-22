# Krea Account — 全自动注册与支付链接工厂

> Krea.ai 批量注册账号 → 自动获取指定套餐 Stripe 支付链接 → 自动上传 KIMHub 链接池。
> 单账号全流程约 55 秒，实测成功率 100%（3/3）。

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Clash 代理轮换  │ ──▶ │  krea-account     │ ──▶ │    KIMHub       │
│  (健康节点探测)  │     │  (本工具)         │     │  (链接池 :4000) │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                             │        ▲
                    IMAP IDLE│        │本地 Turnstile Solver (:5000)
                             ▼        │
                       QQ 邮箱实时收码   同出口 IP 解题
```

## 核心能力

| 能力 | 说明 |
|------|------|
| 批量注册 | Playwright 真浏览器 + 随机域名邮箱（catch-all → QQ 收码）|
| 验证码 | Clerk 隐形 Turnstile 自然解出 → 本地 Solver 兜底 → Nopecha 兜底 |
| 实时收码 | IMAP IDLE（RFC 2177），服务器推送，延迟 1~3 秒 |
| 支付链接 | 直接调 Krea 内部 API 创建 checkout，绕开页面 A/B，5 秒出链 |
| 自动上传 | 按统一规范 POST 到 KIMHub，商品/会员/金额齐全 |
| 自愈重试 | 失败自动换邮箱 + 换节点重注册；Basic 档缺失直接丢弃换新号 |

## 快速开始

```powershell
git clone https://github.com/kaiven11/krea-account.git
cd krea-account
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env   # 编辑 .env 填入邮箱/密码/密钥
python register.py       # ACCOUNT_COUNT 控制批量数
```

## 依赖服务

| 服务 | 端口 | 作用 | 必需 |
|------|:---:|------|:---:|
| Clash (mihomo) | 9090 / 7891 | REST API 节点轮换 + SOCKS5 出口 | ✅ |
| 本地 Turnstile Solver | 5000 | 真浏览器解 Turnstile（同出口 IP）| 可选（有兜底）|
| KIMHub | 4000 | 链接池 | 上传时需要 |
| QQ 邮箱 IMAP | 993 | IDLE 实时收验证码 | ✅ |

---

# 技术细节

## 1. 注册流程逆向

Krea 登录/注册由 **Clerk** 托管，流程：

```
GET /login
  → Clerk 组件渲染 (Svelte + Clerk Elements)
  → 第一步: input[type=email] + button[type=submit]
  → 第二步: input[type=password] × 2 (new-password + confirm)
  → 提交按钮初始 disabled, 等 Clerk 隐形 Turnstile 解出后才 enabled
  → Clerk POST /api/auth/email-flow (发 OTP 邮件)
  → OTP 输入框 input[autocomplete="one-time-code"]
  → 回填后跳转 /onboarding?redirect_to=/app
```

### 1.1 Turnstile 时序（关键坑）

提交按钮 `disabled` 状态由 Clerk 的隐形 Turnstile 控制：

- **不能提前点击** — 按钮禁用时点击无效，Clerk 收不到请求，页面永远不动
- **正确做法**：轮询 `button[type=submit].disabled === false`（实测 6~15 秒解出）
- Turnstile 组件由 Clerk 懒加载，仅渲染 `input[name="cf-turnstile-response"]` 隐藏字段
- 真实 sitekey（从页面 JS 配置 `PUBLIC_TURNSTILE_EMAIL_KEY` 提取）：

```
0x4AAAAAAARCAQia-P1X7s4C
```

### 1.2 Clerk 的 "Continue with Google" 陷阱

页面按钮文本是子串关系：`Continue` / `Continue with Google` / `Continue with Apple`。
Playwright 的 `get_by_role(name="Continue")` 是**子串匹配**，会误点 Google OAuth。
**必须用** `button[type=submit]` 优先选择器。

### 1.3 邮箱策略

注册用域名邮箱（`reg<随机6位>@cmzw.cloud`），QQ 邮箱 catch-all 转发。
验证码邮件特征：

```
From: Krea AI <auth@krea.ai>
Subject: Confirm Your Email
验证码: 8 位数字, 在 HTML <div class="code-box"> 中
```

**提取陷阱**：邮件 HTML 的 CSS 颜色值 `#333333` 会被 `\d{6}` 正则误命中。
提取顺序：先剥 `<style>/<script>/<head>` → `code-box` 精准匹配 → 再 `\d{8}` → 最后 `\d{6}`。

## 2. IMAP IDLE 实时收码

QQ IMAP 在**同一 TCP 连接内缓存 SEARCH 结果**，轮询必须断开重连（1~2 分钟延迟）。
改用 **IMAP IDLE (RFC 2177)**：服务器主动推送 `EXISTS` 通知，延迟 1~3 秒。

```python
client = IMAPClient("imap.qq.com", ssl=True)
client.select_folder("INBOX")
baseline = max(client.search("ALL"))       # 基线 UID
client.idle()                              # 进入等待
responses = client.idle_check(timeout=29)  # 29s 心跳重置 (QQ 30s 断开)
client.idle_done()
new_uids = [u for u in client.search("ALL") if u > baseline]
```

关键点：
- `idle_check` 超时 ≤29 秒（QQ 服务器 30 秒无响应断连）
- 基线取注册开始后的邮件，按收件人地址过滤（多账号并发安全）

## 3. 本地 Turnstile Solver

自建服务（Quart + patchright 真浏览器池），接口：

```
GET /turnstile?url=<页面>&sitekey=<key>[&action=]   → {"task_id": uuid}
GET /result?id=<task_id>                            → {"value": token} | CAPTCHA_FAIL
```

**Turnstile token 与 IP 绑定** — solver 浏览器必须与注册浏览器走**同一 Clash 出口**
（`socks5://127.0.0.1:7891`，`proxies.txt` 配置）。IP 不一致 token 即作废。

解题原理：加载目标页面 → 注入 `.cf-turnstile` 组件 + Turnstile JS →
轮询 `input[name="cf-turnstile-response"]` 的值 → 点击策略兜底。

> Nopecha 等云打码对 Krea 不可行的原因：它们要求**公网可达**的代理，
> 本机 Clash 回环地址无法提供，且解题 IP ≠ 提交 IP 会被 CF 拒绝。

## 4. Onboarding 跳过（Skip without credits）

注册后 Krea 强制进入 1/6 多步引导向导，拦截一切路由导航。

| 尝试 | 结果 |
|------|------|
| 反复点右上 Skip | ❌ 无效 — 弹出确认框 |
| 点选卡片 → Continue 循环 | ❌ 15 次点击页面不动（Continue 初始 disabled）|
| **Skip → 确认框 "Skip without credits"** | ✅ 3 秒过完 |

```javascript
// 优先级: 确认框 → 变体 → 主按钮
1. /skip without credits/i      // 确认框按钮
2. /skip (my|anyway|now)/i      // 变体兜底
3. "Skip"                       // 右上角触发确认框
循环直到 URL 离开 /onboarding
```

## 5. Checkout API 直连（绕开页面 A/B）

### 5.1 为什么不走页面按钮

登录态 pricing 页存在 A/B 问题：
- 未登录态：按钮是 `Get Basic / Get Pro / Get Max`
- 登录免费态：**Basic 档随机隐藏**，只剩 `Get Pro / Get Max`，另有两个 `Upgrade`（Business 区）
- Svelte 事件绑定对 JS `.click()` 无响应，必须 Playwright 原生点击
- 点击后还可能弹确认框

### 5.2 解决：直接调内部 API

点击按钮时抓包发现 checkout 创建端点（页面上下文内 `fetch`，带会话 cookie）：

```
POST https://www.krea.ai/api/payments/create-checkout-session
Content-Type: application/json

{
  "checkout_type": "subscription",
  "subscription_interval": "monthly",    // monthly | yearly
  "plan_id": "creator_basic",            // 见下表
  "compute_units": 5000,
  "return_url": "https://www.krea.ai/pricing"
}

→ 200 {"url": "https://pay.krea.ai/c/pay/cs_live_..."}
```

| plan_id | 档位 | compute_units | 月付 |
|---------|------|:---:|:---:|
| `creator_basic` | Basic | 5000 | $9.00 |
| `creator_pro` | Pro | 20000 | $21.00 |
| `creator_max` | Max | 40000 | ❌ 500（不支持自助）|

耗时 40+ 秒且常失败的页面流程 → **5.5 秒稳定出链**。

## 6. Clash 健康节点轮换

每个账号注册前切换 Clash 节点（mihomo REST API）：

```
GET  http://127.0.0.1:9090/proxies           # 拉全部节点
PUT  http://127.0.0.1:9090/proxies/GLOBAL    # 切换 {"name": "节点名"}
```

`healthy_next()` 流程：
1. 过滤 `SKIP_KEYWORDS`（"官网/公告/剩余/更新/全红"等垃圾节点）
2. 切换节点 → 通过该节点 `GET https://www.krea.ai/login` 探活（<500 状态码）
3. 不健康 → 下一个，最多 8 次
4. 全失败 → 回退普通轮换

注册浏览器与本地 Solver 共用 `socks5://127.0.0.1:7891`，出口 IP 一致。

## 7. KIMHub 上传规范

```json
POST http://127.0.0.1:4000/api/links
{
  "url": "https://pay.krea.ai/c/pay/cs_live_...",
  "product": "Krea Basic",
  "merchant": "Krea AI",
  "amount": "9.00",
  "currency": "USD",
  "plan": "Basic",
  "account": "regxxx@cmzw.cloud",
  "tags": ["krea", "basic", "monthly"],
  "type": "full",
  "meta": {"cycle": "monthly", "plan_id": "creator_basic"}
}
```

KIMHub 侧支持：会员可参与性过滤、`plan_rr`/`plan_one_each` 下发策略、
三级 BIN 绑定与隔离。详见 [KIMHub 仓库](https://github.com/kaiven11/KimHub)。

## 8. 项目结构

```
krea-account/
├── register.py        # 主流程: 注册 → onboarding → checkout → 上传
├── solver.py          # 打码客户端: 本地 Solver 优先 → Nopecha 兜底
├── mail.py            # IMAP IDLE 实时收码 + 验证码提取
├── proxy_rotator.py   # Clash 节点轮换 + 健康探测
├── get_checkout.py    # 独立工具: 对已有账号补抓支付链接
├── oututil.py         # Windows GBK 控制台 UTF-8 包装
├── requirements.txt
└── .env.example
```

### register.py 关键函数

| 函数 | 职责 |
|------|------|
| `register_one()` | 单账号注册全流程，返回账号信息 |
| `wait_submit_enabled()` | 轮询等待 Clerk Turnstile 解出（按钮解锁）|
| `skip_onboarding()` | Skip → Skip without credits 秒过引导 |
| `create_checkout_via_api()` | 页面上下文内 fetch 调 create-checkout-session |
| `grab_checkout()` | checkout 创建 + KIMHub 上传 |
| `attempt_register()` | 注册 + checkout 编排，失败存 session |
| `main()` | 批量循环：失败换邮箱+节点重注册；无 Basic 直接丢弃 |

### mail.py 验证码提取优先级

```python
1. 剥离 <style>/<script>/<head>        # 防 CSS 颜色值误判
2. class="code-box" 内容精准匹配        # Krea 模板
3. \d{8}                               # Krea 是 8 位码
4. \d{6}                               # 通用兜底
```

## 9. 配置参考

见 [.env.example](.env.example)。核心项：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ACCOUNT_COUNT` | 1 | 批量注册数 |
| `MAX_RETRIES` | 3 | 每账号最大重试 |
| `CHECKOUT_PLAN` | Basic | 目标套餐，只认该档 |
| `CHECKOUT_CYCLE` | monthly | 计费周期 |
| `CHECKOUT_ENABLED` | true | 是否抓支付链接 |
| `HEADLESS` | false | 显示浏览器窗口 |

## 10. 输出

| 文件 | 内容 |
|------|------|
| `accounts.txt` | `邮箱\|密码\|落地URL` |
| `checkout_links.txt` | `邮箱\|套餐\|周期\|Stripe链接` |
| `result_*.png` | 每账号最终页面截图 |
| `session_*.json` | Playwright storage_state（补抓用）|
