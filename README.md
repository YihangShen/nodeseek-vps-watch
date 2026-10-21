# nodeseek-vps-watch

盯住 NodeSeek 上「美西 + 优质线路」的 VPS 情报：抢购、补货、上架类帖子即时发邮件提醒，其余命中每天汇总成一份日报。

写给想抢搬瓦工、DMIT、VMISS 这类线路机、又不想整天泡论坛的人。

- 只用 Python 标准库，零第三方依赖，`python3 watch.py` 即可运行
- 数据源是 NodeSeek 公开 RSS（主站和 API 都在 Cloudflare 后面，抓不到）
- 标题命中权重高于正文摘要，品牌 / 线路 / 地区 / 动作 / 排除词都能自己改
- 抢购补货类帖子即时发信，普通命中进每日汇总，没有命中的日子不发信
- 单实例锁、RSS 断层检测：机器休眠期间漏掉的帖子会给出链接
- 不需要任何 AI API key，也不需要注册任何服务

## 为什么用 RSS

| 入口 | 状态 |
| --- | --- |
| `https://www.nodeseek.com/` 主站 | Cloudflare 拦截，脚本访问返回 403 |
| `https://www.nodeseek.com/api/...` 接口 | 同样被 Cloudflare 拦截 |
| `https://rss.nodeseek.com/` | 可用，每次返回最新 20 条 |

论坛高峰期每分钟大约 3 帖，20 条的窗口大约覆盖 7 分钟，所以轮询间隔默认 60 秒，
既不会漏也不会给论坛造成压力。

## 快速开始

### 方案 A：部署到 GitHub Actions（推荐，不需要电脑开机）

监控跑在 GitHub 的服务器上，每 30 分钟抓一次 NodeSeek；有值得抢的就发邮件，没有就安静。
状态存在仓库的 `state` 分支里，不需要服务器、不需要本机常开。

1. Fork 或 clone 这个仓库到自己的账号
2. 在仓库的 **Settings → Secrets and variables → Actions** 添加三个 Secret：

   | Secret | 说明 |
   | --- | --- |
   | `SMTP_EMAIL` | 发件邮箱，例如 `you@qq.com` |
   | `SMTP_PASSWORD` | SMTP 授权码（不是登录密码） |
   | `RECEIVER_EMAIL` | 收件邮箱，可以填多个，用逗号分隔 |

3. 打开 **Actions** 标签页，先手动跑一次 `NodeSeek Watch`（建立基线，不会发历史告警）
4. 之后 `NodeSeek Watch` 每 30 分钟自动跑，`NodeSeek Daily Digest` 每天 21:30（北京时间）
   在有命中时发一封 AI 分析日报；没有情报的日子不发信

日报的 AI 分析是可选的：**不配任何 key 也能用**，只是日报会退化成「分档 + 命中理由 + 清洗后摘要」
的关键词版。要让它写成带判断的分析，加这几项：

| 名称 | 类型 | 例子 |
| --- | --- | --- |
| `AI_API_KEY` | Secret | `sk-...` |
| `AI_BASE_URL` | Variable | `https://api.deepseek.com/v1` |
| `AI_MODEL` | Variable | `deepseek-chat` |
| `AI_PROVIDER` | Variable（可省） | `openai` |

常见 OpenAI 兼容服务：

| 服务 | AI_BASE_URL | AI_MODEL |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问（百炼） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 魔搭 ModelScope | `https://api-inference.modelscope.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |

AI 调用失败（超时、额度、网络）时会自动回退成关键词版日报并照常发信，不会漏。
`AI_PROVIDER=github-models` 也支持，但 2026-10 实测 `models.github.ai` 只返回占位响应，
这条路径暂时不可用。

几点要知道的：

- GitHub 的 `schedule` 是「尽力而为」，高峰期可能延迟几分钟到几十分钟，秒杀型抢购不一定赶得上，
  但**抢购预告**通常提前几小时发布，这条链路足够用
- 私有仓库按 Actions 分钟计费，30 分钟一次大约 1400 分钟/月，在免费额度内；
  公开仓库不计量，可以把 `watch.yml` 里的 cron 改成 `*/10` 换更快的响应
- 定时工作流在仓库连续 60 天没有活动时会被自动停用，`state` 分支的定期提交正好避免这一点

### 方案 B：本机运行（可选，延迟更低）

```bash
git clone https://github.com/<you>/nodeseek-vps-watch.git
cd nodeseek-vps-watch
./install.sh          # macOS 装 LaunchAgent，Linux 写 crontab
```

安装脚本会把程序放到运行时目录（macOS 是 `~/Library/Application Support/NodeSeekVPSWatch`，
因为 macOS 的 TCC 不允许 launchd 访问 `~/Documents` 等受保护目录），并注册两个任务：

- 每 60 秒轮询一次，命中高优先级帖子立刻发邮件
- 每天 09:00 发一封汇总邮件（可改时间或关掉）

只想手动跑，不装定时任务：

```bash
cp config.example.json config.json
python3 watch.py run --dry-run   # 看命中效果，不写状态、不发信
python3 watch.py run             # 真正轮询
python3 watch.py digest          # 手动发一次汇总
```

## 配置邮件

编辑 `config.json`，把 `smtp.enabled` 改成 `true`：

```json
{
  "smtp": {
    "enabled": true,
    "host": "smtp.qq.com",
    "port": 465,
    "security": "ssl",
    "username": "you@qq.com",
    "password": "SMTP 授权码",
    "from": "you@qq.com",
    "to": ["you@qq.com"],
    "subject_prefix": "[NodeSeek 情报]"
  }
}
```

常见服务商：

| 服务商 | 服务器 | 端口 | security |
| --- | --- | --- | --- |
| QQ 邮箱 | `smtp.qq.com` | 465 | `ssl` |
| 163 邮箱 | `smtp.163.com` | 465 | `ssl` |
| Outlook | `smtp-mail.outlook.com` | 587 | `starttls` |
| Gmail | `smtp.gmail.com` | 587 | `starttls` |

`password` 填的是 **SMTP 授权码**，不是邮箱登录密码。QQ 邮箱在
「设置 → 账号 → POP3/IMAP/SMTP 服务」里生成。

不想把授权码写进文件，可以用环境变量代替：

```bash
export NODESEEK_SMTP_PASS="授权码"
```

配好后测试：

```bash
python3 watch.py test-email
```

`config.json` 建议权限 600（安装脚本会自动设置），里面只放授权码，不要放邮箱主密码。

## 每天一封，还是即时告警

两个任务分工不同：

- `watch.py run`：每 60 秒轮询。只有分数够高、且标题里带线路关键词（CN2 GIA、CMIN2、
  9929、4837、DC6 等）的帖子才即时发信，避免变成骚扰。
- `watch.py digest`：把上次汇总之后的所有命中打包成一封日报。`digest_even_if_empty` 控制
  没有命中时是否也发一封（默认 `false`，只在有命中时发）。

常见分工是两层：AI 助手在 21:00 写一封分析日报（更懂买家需求），
脚本在 21:30 检查是否还有没汇总的命中，有就补发一封关键词版；没有就不打扰。
助手自己发完信后调用 `watch.py mark-digested` 推进水位线，避免兜底重复发信。

日报里的抢购时间、库存和优惠码都来自 RSS 摘要，摘要里没写的信息不会替你猜。

## 定时任务

`install.sh` 参数：

| 参数 | 说明 |
| --- | --- |
| `--dir <路径>` | 指定运行时目录 |
| `--interval <秒>` | 轮询间隔，默认 60 |
| `--daily <HH:MM>` | 每日汇总时间，默认 09:00 |
| `--no-daily` | 不装每日汇总 |
| `--python <路径>` | 指定 python3 解释器 |
| `--uninstall` | 卸载定时任务，保留配置和数据 |

Linux 下如果不想用安装脚本写的 crontab，也可以用 systemd user timer：

```ini
# ~/.config/systemd/user/nodeseek-vps-watch.service
[Unit]
Description=NodeSeek VPS watch

[Service]
Type=oneshot
WorkingDirectory=%h/.local/share/nodeseek-vps-watch
ExecStart=/usr/bin/python3 watch.py run
```

```ini
# ~/.config/systemd/user/nodeseek-vps-watch.timer
[Unit]
Description=NodeSeek VPS watch timer

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now nodeseek-vps-watch.timer
```

## 配合 AI 助手（可选）

不需要 AI API key，脚本自己就能发日报。如果你在用 Codex 这类助手，可以让它读
`python3 watch.py digest --json` 的结果，写成带判断的摘要（哪些值得抢、哪些是二手溢价），
再用 `python3 watch.py send --subject "..." --body-file body.txt` 发出去。提示词可以直接写：

> 运行 `watch.py run` 做兜底轮询，再运行 `watch.py digest --print` 取出新命中。
> 没有命中就保持安静。有命中时按「值得立刻看 / 可以慢慢看」分类，用中文写一封简短邮件，
> 价格和库存只能引用摘要里出现过的内容，最后用 `watch.py send` 发出。

### 让日报自带 AI 摘要

想让每天那封邮件本身就用模型写成判断，而不是关键词列表，可以在 `config.json` 里打开 `ai`：

```json
{
  "ai": {
    "enabled": true,
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "sk-...",
    "timeout": 60
  }
}
```

任何 OpenAI 兼容的 `POST /chat/completions` 接口都能用：

| 服务 | base_url | 模型示例 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里云百炼（通义） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

几点说明：

- **只有 `digest` 会调用 AI**，每分钟的轮询不发任何 API 请求，所以一天最多一次调用
- 调用失败、超时、没配 key 都会自动回退到普通关键词日报，不会漏发邮件
- key 也可以放环境变量 `NODESEEK_AI_KEY`，避免写进配置文件
- 提示词已固定要求「信息不足就写需要点开确认」，模型不会替你编价格和库存

## 评分规则

同一组关键词只取最高权重，避免同义词堆叠把分数刷高：

| 组 | 标题命中 | 正文命中 |
| --- | --- | --- |
| 品牌（`brands`） | 6 | 2 |
| 抢购动作（`deal_actions`） | 4 | 1 |
| 线路（`routes`） | 3 | 1 |
| 地区（`regions`） | 2 | 1 |
| 需求画像（`needs`） | 2 | 1 |

`needs` 放的是买家画像相关的词（解锁、原生、IP 干净、联通、流媒体、TRI 等），只加分，
不单独构成告警；`brands` / `routes` / `regions` 命中任意一组才算跟线路机有关。

附加规则：分类为 `info` 加 1 分，标题含「优惠 / 促销」加 1 分。
另外两组过滤词：

- `penalty`：标题命中扣 5 分，但仍然可见（溢价、转让、二手、原油账单）
- `exclude`：标题命中整条丢弃（含「收」「换」「求购」「拼车」「出租」等字样的换机、求购贴）

- 分数 ≥ `threshold`（默认 6）且命中品牌 / 线路 / 地区之一 → 进入汇总
- 分数 ≥ `instant_threshold`（默认 9）且标题里带线路关键词 → 立刻发信

想把「圣何塞」也纳入，或者加一家厂商，改 `config.json` 的 `rules` 就行，不用动代码。

## 命令

```bash
python3 watch.py run                  # 轮询一次，必要时发即时告警
python3 watch.py run --dry-run        # 只看命中，不写状态不发信
python3 watch.py digest               # 发送上次汇总之后的日报
python3 watch.py digest --print       # 只打印，方便接 AI 助手
python3 watch.py digest --json        # 输出 JSON
python3 watch.py digest --hours 24    # 指定统计窗口
python3 watch.py send --subject "标题" --body-file body.txt
python3 watch.py mark-digested        # 外部发信后标记已汇总
python3 watch.py status               # 查看状态和最近命中
python3 watch.py test-email           # 测试 SMTP
python3 watch.py --config /path/to/config.json status
```

运行目录可以用环境变量覆盖：`NODESEEK_WATCH_HOME=/path/to/dir`，
数据源可以用 `NODESEEK_FEED_URL` 覆盖（测试时指向本地文件）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 数据与隐私

所有状态都存在本地：`state.json`（已见帖子与帖子 ID 水位线）、`hits.jsonl`（命中记录）、
`logs/`。脚本只访问 NodeSeek 的 RSS 和你的 SMTP 服务器，不经过任何第三方服务。

## 已知限制

- RSS 只有最新 20 条。机器长时间休眠可能漏帖，脚本会检测帖子 ID 断层，把漏掉的帖子链接
  一并列出来。
- 帖子正文抓不到（Cloudflare 拦截），邮件里的摘要来自 RSS 的 description 字段，
  想看完整内容要点进链接。
- 帖子里的价格、库存、优惠码都在正文中，时效性也强，邮件只负责把你叫起来。
- 只适配 NodeSeek 的 RSS 结构；换论坛需要改解析。
- macOS 上运行时目录必须避开 `~/Documents`、`~/Desktop` 这类受 TCC 保护的目录，
  否则 launchd 会报 `Operation not permitted`。安装脚本已经处理。

## License

[MIT](LICENSE)
