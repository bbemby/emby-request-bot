# 📮 Emby 求片 Bot

用户求片 → 管理员下载 → 自动传 Google Drive → 入库自动通知，一条龙。

## ✨ 功能

| 角色 | 功能 |
|---|---|
| 👤 用户 | `/s 番号` 搜番（遮罩封面 + 中文简介 + 磁力 Top3） |
| 👤 用户 | `/q 番号` 求片，私聊直接发番号也行 |
| 👤 用户 | 私聊发图以图搜番（仅私聊） |
| 👤 用户 | `@Bot 番号` 内联搜索 |
| 🛠️ 管理员 | `/pending` 查看未完结求片 |
| 🛠️ 管理员 | `/progress` 查看下载进度（进度条+速度） |
| 🛠️ 管理员 | 私聊发磁力链接 → 选求片 → **只下载最大视频文件** → 自动传 Drive（可覆盖自动下载） |
| 🤖 自动 | 求片后**自动搜索最优磁力（中字优先）并下载**，多个磁力依次重试，无磁力才等管理员手动提供 |
| 🤖 自动 | 求片通知发到反馈群（谁求的 / 中文标题 / 遮罩封面） |
| 🤖 自动 | 定时检查 Emby 是否入库，入库后在群里通知 |

## 🚀 快速开始

```bash
git clone https://github.com/你的用户名/emby-request-bot.git
cd emby-request-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填好配置
python bot.py
```

### 依赖工具

```bash
# 下载工具（推荐 aria2，轻量快速，支持磁力选择性下载）
apt install aria2        # Debian/Ubuntu
# yum install aria2      # CentOS
# brew install aria2     # macOS

# Drive 上传
curl https://rclone.org/install.sh | sudo bash
rclone config            # 按向导添加 Google Drive

# 搜番数据源（可选，不装则 /s /q 的影片信息不可用）
# https://github.com/FlanChanXwO/javdb-cli
```

### systemd 常驻

```ini
[Unit]
Description=Emby Request Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/emby-request-bot
EnvironmentFile=/root/emby-request-bot/.env
ExecStart=/root/emby-request-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## ⚙️ 配置说明

| 变量 | 说明 |
|---|---|
| `BOT_TOKEN` | @BotFather 申请的 Token |
| `ADMIN_IDS` | 管理员 TG user id，逗号分隔 |
| `FEEDBACK_GROUP_ID` | 求片反馈群组 id（通知发到这里） |
| `EMBY_URL` / `EMBY_API_KEY` | Emby 服务器地址和 API Key |
| `RCLONE_REMOTE` / `RCLONE_DEST` | rclone remote 名 / Drive 目标文件夹 |
| `CHECK_INTERVAL_MIN` | 入库检查间隔（分钟），默认 30 |

## 🔧 实现细节

- **求片去重**：同一番号未完结只保留一条；已在 Emby 的直接提示"已在库"
- **选择性下载**：`aria2c --bt-metadata-only` 取元数据 → 纯 Python 解析 torrent → `--select-file` 只下最大视频（>50MB，广告文件自动跳过）
- **封面**：JavDB 图床图片加密，用 `javdb assets download` 解密后 `has_spoiler=True` 发送；TG file_id 缓存复用
- **入库检查**：`GET /emby/Items?SearchTerm=<番号>`，标题命中才算数
- **数据**：SQLite 单文件，求片状态机 pending → downloading → uploaded → fulfilled

## 📝 开源协议

MIT
