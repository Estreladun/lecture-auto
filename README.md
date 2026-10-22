# 东南大学讲座自动预约

东南大学「人文与科学素养系列讲座」等讲座的自动预约工具：一卡通账号密码登录（不再手动复制 Cookie）、自动识别四位数字验证码、按预约开放时间精确抢位、满员时持续候补，支持线上/线下筛选、邮件通知与状态持久化。

适用于东大 ehall 微应用体系（`ehall.seu.edu.cn` + `auth.seu.edu.cn` CAS 认证 + 验证码）。其他学校的同构系统改一下 `.env` 里的接入地址也能用。

## 一、装环境

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv sync
```

首次会下载 Python 3.12 与依赖（ddddocr 含 onnxruntime，约几百 MB）。国内网络慢的话换镜像，10 秒能装完：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

## 二、填配置

```bash
cp .env.example .env
```

`.env.example` 里的接入地址已经按东大填好了，**你只需要填两项**：

```dotenv
SEU_USERNAME=你的一卡通号
SEU_PASSWORD=你的密码
```

其余按需。

### 引号规则：账号密码一律不加引号

| 变量 | 正确写法 | 说明 |
| --- | --- | --- |
| `SEU_USERNAME` | `SEU_USERNAME=101012345` | 一卡通号，纯数字 |
| `SEU_PASSWORD` | `SEU_PASSWORD=你的密码` | 只有一种例外，见下 |
| `SMTP_USERNAME` | `SMTP_USERNAME=you@qq.com` | 邮箱地址 |
| `SMTP_PASSWORD` | `SMTP_PASSWORD=授权码` | 复制带来的首尾空格会自动清掉 |

**唯一例外：密码首尾本身带空格**，这时才用单引号把整个值包住，否则行尾空格会被吞掉：

```dotenv
SEU_PASSWORD=' 你的密码 '
```

密码内含单引号时改用双引号包：`SEU_PASSWORD="ab'cd"`。

另外注意：

- `#`、`$`、中间空格这些字符在**不加引号**时也能正确读取，不必为此加引号。
- JSON 数组那几项（`SEU_TARGET_KEYWORDS` 等）**要**用单引号包住，因为值里有双引号：`'["法律"]'`。
- Gmail 应用专用密码粘成 `abcd efgh ijkl mnop` 也能识别，程序会自动去掉分组空格。

### 全部配置项

| 变量 | 作用 | 默认 / 示例 |
| --- | --- | --- |
| `SEU_USERNAME` | 一卡通号 | 必填 |
| `SEU_PASSWORD` | 密码 | 必填 |
| `SEU_ENTRY_URL` | 讲座预约微应用入口 | 已填东大地址 |
| `SEU_CAS_BASE` | 统一认证接口前缀 | `https://auth.seu.edu.cn/auth/casback/` |
| `SEU_GATEWAY` | 认证网关，会话经它签发 | `https://vpn.seu.edu.cn` |
| `SEU_GATEWAY_IP` | 网关备用 IP（域名解析不到时用） | `121.249.14.2` |
| `SEU_API_BASES` | 额外候选接口基址，逗号分隔 | 已填东大新版应用 |
| `SEU_TARGET_KEYWORDS` | 标题关键词，任一命中即抢 | `'["法律"]'` |
| `SEU_PREFERRED_CAMPUS` | 只抢指定校区 | `'["九龙湖校区"]'` |
| `SEU_REQUIRED_CATEGORIES` | 按讲座分类过滤 | `'["人文与科学素养系列讲座_法律"]'` |
| `SEU_ENABLE_ONLINE` | 是否抢线上 | `true` |
| `SEU_ENABLE_OFFLINE` | 是否抢线下 | `true` |
| `SEU_POLL_SECONDS` | 候补轮询间隔（秒） | `0.2` |
| `SEU_BURST_ATTEMPTS` | 开放瞬间连击次数 | `5` |
| `SEU_MAX_ATTEMPTS` | 单场最多提交次数，0 不限 | `0` |
| `SEU_USE_PROXY` | 是否跟随系统代理 | `false` 直连 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | 邮件通知 | 留空则不发送 |
| `SEU_MAIL_REPORT_HOUR` | 每天几点汇报，0 关闭 | `20` |

> **关于代理**：默认不走系统代理。Clash / V2Ray 之类的全局代理会把 `ehall.seu.edu.cn` 打到 `502 Bad Gateway`，所以默认直连。

> **关于网关 IP**：`vpn.seu.edu.cn` 在多数公共 DNS 下没有记录（`nslookup` 返回 Non-existent domain），只能靠 IP 直连。程序用 `SEU_GATEWAY_IP` 指定的地址连接，但 Host 头与 TLS 证书仍按域名处理，不降低安全性。这个 IP 变了就在 `.env` 里换一个。

## 三、先自检

```bash
uv run main.py --check
```

依次验证：登录 → 接口探测 → 拉列表 → 验证码识别，**不提交任何预约**。
正常输出大致是：

```
[1/4] 登录统一身份认证 … 登录成功
[2/4] 业务接口已锁定：yddjzxxtjappseu/modules（当前 4 条讲座）
[3/4] 拉取讲座列表 … 按当前筛选条件命中 N 个目标
[4/4] 验证码识别 … 验证码识别成功：6528
```

没命中目标时会打印列表前几条，方便核对关键词。

邮件单独测：

```bash
uv run main.py --test-mail
```

## 四、正式运行

```bash
uv run main.py
```

程序读取讲座的预约开放时间，提前 2 秒预取验证码，到点自动提交；满员则持续候补到截止。

模拟运行（跑到「可以提交」为止但不真提交）：

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
seu_lecture/
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
关掉代理软件，或把 `SEU_USE_PROXY` 设为 `false`（默认即是）。

**报「无法连接服务器」**
先确认 `ehall.seu.edu.cn` 能正常访问。若是网关解析不到，检查 `SEU_GATEWAY_IP` 是否还有效。

**登录被拒绝：用户名或密码错误**
先在浏览器手动登录一次 `https://ehall.seu.edu.cn`，确认账号正常、没有被要求改密码或短信二次认证。脚本不支持短信二次认证；遇到 CAS 图形验证码时会尝试本地 OCR。

**接口探测全部失败**
系统改版了。日志会列出每个候选接口的失败原因；接口路径不同就把它加进 `SEU_API_BASES`。

**频繁 503**
系统在限流，把 `SEU_POLL_SECONDS` 调大到 `0.5` 或 `1`。

**验证码识别准不准**
程序对验证码做了四层处理：beta 模型识别、按 CTC 解码概率张量、只在 0-9 这十个字符里取最优、用 top1 与次优数字的概率差判断把握。把握不足时**直接换一张重试，不会把可疑的码提交上去**——识别错一次就浪费一次提交机会，还不如换码。

实测数据（来自 1023 张真实样本）：

| 方式 | 准确率 |
| --- | --- |
| 直接用 `text` 字段 | 79%（14 张标注里错 3 张） |
| 本方案 | **93%**（14 张标注里错 1 张） |

1023 张样本里 91.4% 能解出四位且把握足够，剩下的会自动换码。形近数字（`8/5`、`2/9`、`0/6`）仍是主要误差来源，无法完全消除。

## 七、注意

- 仅供本人正常预约使用，请遵守学校规定，不要用于代抢、分发或商业用途。
- `.env` 含一卡通号与密码，已在 `.gitignore` 中排除，不要提交到任何仓库。
- 电脑需保持不休眠、网络不断；笔记本建议插电并把睡眠设为「从不」。
