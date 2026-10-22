# 校园讲座自动预约

面向「统一身份认证 + 教务微应用」这类校园讲座预约系统的命令行工具：账号密码登录、自动识别图形验证码、按开放时间精确抢位、满员时持续候补，支持线上/线下筛选、邮件通知与状态持久化。

适用于国内高校常见的 EDP/ehall 微应用体系（`/gsapp/sys/<应用名>/*default/index.do` + CAS 认证 + 四位数字验证码）。接入地址全部写在配置里，不绑定任何具体学校。

## 一、装环境

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv sync
```

首次会下载 Python 3.12 与依赖（含 onnxruntime，约几百 MB）。国内网络慢的话先换镜像：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

## 二、填配置

```bash
cp .env.example .env
```

最少要填四项：接入地址 + 账号密码。

```dotenv
LEC_ENTRY_URL=https://<教务域名>/gsapp/sys/<应用名>/*default/index.do
LEC_CAS_BASE=https://<认证域名>/auth/casback/
LEC_USERNAME=你的账号
LEC_PASSWORD=你的密码
```

**接入地址怎么找**：浏览器登录教务系统，打开讲座预约页面，把地址栏里的地址复制到 `LEC_ENTRY_URL`；打开登录页按 F12 看 Network 里认证请求的前缀填到 `LEC_CAS_BASE`。未登录状态下访问入口地址会跳转到认证页，这是判断地址填对没对的依据。

### 引号规则：账号密码一律不加引号

| 变量 | 正确写法 |
| --- | --- |
| `LEC_USERNAME` | `LEC_USERNAME=20240001` |
| `LEC_PASSWORD` | `LEC_PASSWORD=你的密码` |
| `SMTP_USERNAME` | `SMTP_USERNAME=you@qq.com` |
| `SMTP_PASSWORD` | `SMTP_PASSWORD=授权码` |

**唯一例外：密码首尾本身带空格**，此时用单引号包住整个值：`LEC_PASSWORD=' 你的密码 '`。密码含单引号时改用双引号。

另外注意：

- `#`、`$`、中间空格这些字符在**不加引号**时也能正确读取，不必为它们加引号。
- JSON 数组那几项（`LEC_TARGET_KEYWORDS` 等）**要**用单引号包住，因为值里有双引号：`'["讲座"]'`。

### 其余配置

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `LEC_GATEWAY` | 统一认证网关，部分学校的会话经网关签发 | 空（直连 CAS） |
| `LEC_GATEWAY_IP` | 网关域名的备用 IP（域名解析不到时用） | 空 |
| `LEC_API_BASES` | 额外的业务接口基址，逗号分隔 | 空 |
| `LEC_TARGET_KEYWORDS` | 标题关键词，任一命中即抢 | `[]` 不限 |
| `LEC_PREFERRED_CAMPUS` | 只抢指定校区 | `[]` 不限 |
| `LEC_REQUIRED_CATEGORIES` | 按讲座分类过滤 | `[]` 不限 |
| `LEC_ENABLE_ONLINE` | 是否抢线上 | `true` |
| `LEC_ENABLE_OFFLINE` | 是否抢线下 | `true` |
| `LEC_POLL_SECONDS` | 候补轮询间隔（秒） | `0.2` |
| `LEC_BURST_ATTEMPTS` | 开放瞬间连击次数 | `5` |
| `LEC_MAX_ATTEMPTS` | 单场最多提交次数，0 不限 | `0` |
| `LEC_USE_PROXY` | 是否跟随系统代理 | `false` 直连 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | 邮件通知（授权码，非登录密码） | 空则不发送 |
| `LEC_MAIL_REPORT_HOUR` | 每天几点汇报，0 关闭 | `20` |

> **关于代理**：默认不走系统代理。Clash / V2Ray 之类的全局代理经常把校园域名打到 `502 Bad Gateway`，所以默认直连；确实需要代理才改成 `true`。

## 三、先自检

```bash
uv run main.py --check
```

依次验证：登录 → 接口探测 → 拉列表 → 验证码识别，**不提交任何预约**。
输出会列出按当前筛选条件命中的讲座；没命中时打印列表前几条供核对关键词。

邮件单独测：

```bash
uv run main.py --test-mail
```

## 四、正式运行

```bash
uv run main.py
```

程序读取讲座的预约开放时间，提前 2 秒预取验证码，到点自动提交；满员则持续候补到截止。

模拟运行（不真提交）：

```bash
uv run main.py --dry-run
```

查看已抢到的记录：

```bash
uv run main.py --status
```

## 五、目录说明

```
main.py               命令行入口
lecture_auto/
  config.py           环境变量解析与校验
  client.py           CAS 登录 + 业务接口（含网关直连与接口自动探测）
  captcha.py          验证码识别
  model.py            讲座数据模型与筛选规则
  app.py              调度与抢位主循环
  state.py            状态文件（原子写 + 进程互斥）
  mailer.py           邮件通知
  log.py              日志
state/                运行时生成：state.json、logs/、run.lock
```

## 六、常见问题

**提示「同一账号已有实例在运行」**
同一账号只能跑一个实例。关掉其他窗口，或删除 `state/run.lock`。

**报「系统代理拦下了请求」或 502**
关掉代理软件，或把 `LEC_USE_PROXY` 设为 `false`（默认即是）。

**报「无法连接服务器」**
先确认 `LEC_ENTRY_URL` 的域名能正常访问。若认证网关域名在公共 DNS 下解析不到，把可用 IP 填进 `LEC_GATEWAY_IP`，请求仍按域名校验证书。

**登录被拒绝：用户名或密码错误**
先在浏览器手动登录一次，确认账号正常、没有被要求改密码或短信二次认证。脚本不支持短信二次认证，遇到图形验证码会尝试本地 OCR。

**接口探测全部失败**
系统改版了。日志会列出每个候选接口的失败原因；接口路径不同就把它加进 `LEC_API_BASES`。

**频繁 503**
系统在限流，把 `LEC_POLL_SECONDS` 调大到 `0.5` 或 `1`。

**验证码老识别错**
OCR 对 `9/5`、`7/1` 这类形近数字有误识率，程序会自动换码重试，连续失败 30 次才放弃。

## 七、注意

- 仅供本人正常预约使用，请遵守所在学校的规定，不要用于代抢或分发。
- `.env` 含账号密码，已在 `.gitignore` 中排除，不要提交到任何仓库。
- 电脑需保持不休眠、网络不断；笔记本建议插电并把睡眠设为「从不」。
