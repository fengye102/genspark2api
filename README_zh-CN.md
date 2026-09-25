# Genspark2API — Windows 单文件版

OpenAI 兼容的 API 桥接器，将 Genspark 网页端封装成 OpenAI 风格的 HTTP 接口。**单文件 exe**，不需要安装 Python，直接双击就能用。

核心功能：

- ✅ **一键获取 Cookie**：管理面板里点按钮就自动弹出浏览器登录，无需手动找 session_id
- ✅ **多账号轮转**：多个账号池 + 失败自动切号 + 限流冷却
- ✅ **API 密钥管理**：生成/删除密钥，调用/v1/*必须带 Bearer 密钥
- ✅ **管理员密码修改**：从管理面板改密码即可
- ✅ **支持流式响应**：stream: true 完全兼容 OpenAI 客户端

---

## 快速上手（Windows）

### 1. 下载 exe

从 Releases 页面下载 `genspark2api-v1.1.0.zip`，解压到一个**单独的文件夹**（比如 `C:\tools\genspark2api`）：

```text
C:\tools\genspark2api\
  ├─ genspark2api.exe
  └─ accounts.example.json  (模板，可以不用管)
```

### 2. 运行 & 登录

双击 `genspark2api.exe` → 会看到命令行窗口显示：

```
[main] serving on :8899
```

浏览器打开：`http://127.0.0.1:8899/`

首次默认密码：**admin123**（进主页后建议立刻修改）

### 3. 添加账号（一键获取 Cookie）

这是最方便的一步，小白也能操作：

1. 左侧导航栏点击 **「账号管理」**
2. 点击右上角的 **「添加账号」** 按钮
3. 你会看到一个下拉框（可选邮箱）和一个文本框（Cookie 字符串）
4. **别手填！** 直接点「**获取 Cookie（自动登录）**」这个按钮
5. 这时会弹出一个专门的浏览器窗口，里面已经打开了 Genspark 的登录页
6. **你只需要在这个弹出的窗口里登录你的 Genspark 账号**（密码验证码都在这窗口里输）
7. 登录成功后，系统会自动抓取 cookie，然后关浏览器，并回到管理后台
8. 此时表单会自动填充邮箱和 cookie 字符串，点「保存」就完成添加

> ⚠️ 注意：这个弹窗浏览器是隔离的，不会干扰你日常用的 Chrome/Edge，也不会污染你的常用配置。

### 4. 获取 API 密钥

所有对 `/v1/*` 的调用都需要 Bearer 密钥：

1. 点击左侧 **「API 密钥」** 页
2. 你会发现第一把密钥已经自动生成好了（前缀 `sk-`）
3. 复制这把密钥到剪贴板（新建的密钥只展示一次，丢了得重新生成）
4. 以后每个客户端请求都要带上它

### 5. 开始调用

```bash
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

返回格式完全兼容 OpenAI，可以直接替换大多数支持 OpenAI API 的工具。

---

## 管理面板说明

访问地址：`http://127.0.0.1:8899/`

### 八个页面

| 页面 | 功能 |
|---|---|
| **仪表盘** | 账号状态（成功数、失败数、冷却时间）、总请求数、成功率、24 小时请求量图表、最近请求记录 |
| **账号管理** | 添加/删除账号、一键获取 Cookie、强制冷却、重置状态、导出/导入账号池（JSON） |
| **API 密钥** | 生成命名密钥、启用/停用开关、改名、删除（至少保留一把）、脱敏列表、最后使用时间 |
| **日志** | 请求日志（按模型/状态/关键词筛选、分页）、24 小时统计（请求数、成功率、平均/P95 延迟）、运行日志（级别筛选、自动刷新）、清空日志 |
| **模型测试** | 直接向上游发送测试对话（可指定账号），验证账号与模型可用性 |
| **访问信息** | 一键复制接入地址（Base URL、chat、models、health）、数据文件位置、Cherry Studio / NextChat / LobeChat 配置示例 |
| **设置** | 运行参数（默认模型、上游超时、重试次数、各类冷却时长，保存到 `config.json`）、修改管理员密码 |
| **关于** | 版本、运行时长、模型数量 |

### 安全提示

- 默认密码是 `admin123`，**不要直接把端口暴露给公网**
- 如果想对外服务，建议先用环境变量设一个强密码：

```powershell
$env:GS_ADMIN_PASSWORD = "YourStrongPass123!"
.\dist\genspark2api.exe
```

或者在面板的 **设置** 里改（改完后会持久化到本地 `config.json`）。

---

## API 密钥说明

### 为什么需要密钥？

因为你的服务可能会跑在你的机器上，如果不加认证，谁都能调你的账号。用 Bearer 密钥保护接口是个标准做法。

### 密钥管理

- 首次运行会自动生成一把密钥（`sk-`开头，长度 51 字符）
- 可以在 **API 密钥** 页生成多把（比如分不同客户端用）
- 每把密钥有独立的**启用/停用开关**，临时冻结某个客户端不用删密钥
- 删除某把密钥后，使用它的所有客户端立即失效
- **至少保留一把**，否则没有密钥就无法调接口了
- 新密钥只会展示一次，复制后就看不到了，请一定先保存好

### 调用示例

```bash
# 非流式
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer sk-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-4-5-haiku","messages":[{"role":"user","content":"hi"}]}'

# 流式
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer sk-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-4-5-haiku","messages":[{"role":"user","content":"hi"}],"stream":true}' \
  --no-buffer
```

### 环境变量覆盖

如果你想用固定的密钥，启动前可以用环境变量锁定：

```powershell
$env:GS_API_KEY = "sk-fixedkeyyouwant"
.\dist\genspark2api.exe
```

---

## Linux 服务器部署

程序是纯 Python 应用，Linux 上直接跑源码即可，无需打包 exe。两种部署方式：

### 方式 A：Docker（推荐）

```bash
docker compose up -d --build
docker logs -f genspark2api
```

- 数据（账号 / Cookie / 配置 / 日志）自动持久化到宿主机 `./data/`。
- 改密码：在 `docker-compose.yml` 里设置 `GS_ADMIN_PASSWORD`，或登录后台 → 设置 里改。

也可以不用 compose：

```bash
docker build -t genspark2api .
docker run -d --name genspark2api -p 8899:8899 \
  -v $(pwd)/data:/data -e GS_ADMIN_PASSWORD=change-me genspark2api
```

### 方式 B：systemd 直跑

```bash
sudo mkdir -p /opt/genspark2api /var/lib/genspark2api
sudo cp genspark2api.py requirements.txt /opt/genspark2api/
sudo cp -r static /opt/genspark2api/
cd /opt/genspark2api && sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt

sudo useradd -r -s /usr/sbin/nologin genspark2api
sudo chown -R genspark2api:genspark2api /var/lib/genspark2api
sudo cp deploy/genspark2api.service /etc/systemd/system/
sudo systemctl enable --now genspark2api
```

### 服务器上怎么添加账号？

服务器没有桌面，「一键获取 Cookie」不可用（后台点它会提示未安装 cloakbrowser）。正确做法是在**你自己的电脑浏览器**里操作：

1. 登录 [Genspark](https://www.genspark.ai)。
2. 在服务器后台管理界面（`http://你的服务器IP:8899`）的「添加账号」弹窗里，复制 bookmarklet（控制台小书签代码）。
3. 回到 Genspark 页面，F12 打开控制台，粘贴执行 —— Cookie 会直接推送到服务器。

### 服务器部署注意

- Cookie 可能和出口 IP / 风控绑定，服务器（尤其海外机房）调用可能触发验证，部署后先在「模型测试」页实测一次。
- 公网暴露时务必改管理员密码、只放行必要端口；建议放 Nginx/Caddy 后面套 HTTPS。
- 环境变量 `GS_DATA_DIR` 指定数据目录、`GS_HOST=0.0.0.0` 对外开放（容器和 systemd 单元已配好）。

---

## 运行时文件说明

exe 所在目录会自动创建这些文件：

| 文件名 | 说明 |
|---|---|
| `accounts.json` | 账号池列表（已添加的账号） |
| `cookies*.json` | 每次从浏览器抓取的 Cookie 文件（按序号递增） |
| `config.json` | 运行态配置：管理员密码、API 密钥列表 |
| `gs_login_profile/` | 用于一键获取 Cookie 的专用浏览器配置文件 |

⚠️ **重要**：这些文件包含你的账号凭证和会话信息，**绝对不能传到 GitHub 或其他公开仓库**。项目 `.gitignore` 已经把它们的父目录全部忽略了。

---

## 常见问题

### Q1: 杀毒软件提示“可疑程序”怎么办？

PyInstaller 打包的单文件 exe 经常被杀毒软件误报（尤其是刚更新完代码的版本）。这属于正常现象。

**解决方案：**
- 如果从官方 Release 页面下载的最新版，可以放心信任
- 被拦截时选“允许运行”或“添加到排除项”
- 如需彻底避免，可以向 Symantec/Microsoft 提交误报申诉（不推荐新手折腾）

### Q2: 端口被占用怎么办？

命令行会提示：“端口 8899 可能已被占用”。解决方法有两种：

**方法 A：换一个端口**
```powershell
$env:GS_PORT = "8888"
.\dist\genspark2api.exe
# 然后访问 http://127.0.0.1:8888/
```

**方法 B：杀掉占用的进程**
```powershell
taskkill /f /im genspark2api.exe
taskkill /f /im python.exe
```

### Q3: 一键获取 Cookie 的浏览器没弹出？

可能的原因：
1. 第一次运行的瞬间弹窗比较慢，等个 1-2 秒再刷新
2. 杀毒软件阻止了浏览器启动
3. 之前有残留的 `gs_login_profile/` 配置损坏

**解决方法：**
```powershell
Remove-Item -Recurse -Force gs_login_profile
# 重新启动 exe
```

### Q4: 如何批量导入多个账号？

可以用脚本批量解析 `cookies*.json` 生成 `accounts.json`，但最简单还是通过管理面板一个个添加（每次点一键获取就自动存到下一个编号的文件）。

### Q5: 账号冷却是什么意思？

Genspark 免费版有限流策略：每分钟最多 6 次请求、每小时 60 次。超过限流就会触发冷却，该账号暂时停止发送请求，切换到其他可用账号。

你可以在面板里：
- 看某个账号的剩余冷却时间
- 手动强制冷却（适合测试）
- 重置冷却计数器（比如你确认该号刚刚休息了很久）

---

## 构建自己的 exe

如果你只想自己编译发行版，而不是用预编译的：

```powershell
pip install pyinstaller
python -m PyInstaller --onefile --console --name genspark2api `
  --add-data "accounts.example.json;." `
  --add-data "static;static" `
  --hidden-import cloakbrowser `
  --hidden-import playwright `
  --collect-all cloakbrowser `
  --collect-all playwright `
  genspark2api.py
```

构建产物在 `dist\genspark2api.exe`，约 70MB。

---

## 支持的模型

已实测 **50+ 个主流模型** 可用（截至 2026-09-23），包括：

- **OpenAI**: gpt-6-luna, gpt-6-sol, gpt-5.x 系列
- **Anthropic**: claude-opus-5-5, claude-sonnet-5, claude-4-5-haiku 等
- **Google**: gemini-3.8-flash, gemini-2.5-flash
- **其他**: grok-4.7, kimi-k3, GLM-5.3, deep-seek-v4, minimax-m3 等

具体列表见 [README.md](README.md) 的 "Supported models" 章节。

---

## 协议与免责声明

本项目是开源的，但**与上游服务无关联**。它自动化的是你**控制的浏览器会话**，使用的是你导出的账号凭证，不会绕过身份验证或给你访问不属于你的内容。

你需遵守上游服务的使用条款。因使用本工具导致的账户限流或封禁风险自负。

详细免责声明见 [DISCLAIMER.md](DISCLAIMER.md)。

---

## License

MIT — see [LICENSE](LICENSE).

---

## 技术支持

有问题请在 GitHub Issues 提，我会及时回复。如果涉及账号安全问题，请勿在此公开讨论。
