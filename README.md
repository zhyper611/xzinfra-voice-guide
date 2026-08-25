# XZInfra Voice Guide

运行在 Raspberry Pi 5 上的展厅 AI 语音讲解服务。它支持远程 ASR、XZKB 知识库和 TTS 服务，并通过高频问答文本与预生成语音缓存缩短固定讲解的响应时间。项目支持网页多人会话，也提供面向树莓派物理设备的语音闭环接口。

## 功能

- 网页端文字提问、知识库回答和语音播放
- 基于 HttpOnly Cookie 的多人会话隔离
- 每个会话独立保存短期上下文、状态和临时音频
- 设备端 WAV 上传、ASR、XZKB、TTS 和播放完成回执
- 设备测试页直接控制树莓派本地麦克风录音和扬声器播放
- 本地录音、上传、处理和播放使用同一套设备互斥流程
- 设备接口使用独立密钥鉴权
- XZKB 与 TTS 并发门控和排队超时
- 高频问答精确匹配与主题词、意图词规则匹配
- 高频回答命中后跳过知识库，预生成语音有效时同时跳过在线 TTS
- 独立的高频问答文本、语音生成、试听和审批维护页
- TTS 不可用时保留文字回答
- 用户级 systemd 开机自启模板

## 处理流程

```text
网页用户 ──文字问题──────────────┐
                                 ├──> 高频问答匹配 ──┬──> 固定回答 + 预生成语音
树莓派设备 ──WAV ──> ASR ───────┘                    ├──> 固定回答 + 在线 TTS
                                                      └──> XZKB + 在线 TTS
```

网页访客之间不共享会话。物理设备使用单独的长期会话，因此网页访问不会污染设备的追问上下文。

高频问答按照“精确 alias → 主题词与意图词规则 → XZKB”匹配。命中后直接使用人工审核的固定回答；对应预生成 WAV 校验有效时直接播放，否则回退在线 TTS。未命中、存在歧义或包含实时/比较等排除意图的问题继续使用原有知识库链路。维护规则见 [高频问答缓存维护说明](docs/faq-cache-maintenance.md)。

## 环境要求

- Raspberry Pi 5 或其他 Linux 主机
- Python 3.11 或更高版本
- 可访问兼容接口的 XZKB、ASR 和 TTS 服务
- 设备语音输入必须是单声道、16-bit、16 kHz PCM WAV

当前代码已完成网页文字问答、WAV 上传和树莓派本地麦克风语音闭环。GPIO 物理按键将在本地语音流程验收稳定后接入。

## 安装

```bash
git clone https://github.com/<your-account>/xzinfra-voice-guide.git
cd xzinfra-voice-guide
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

开发与测试依赖：

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

## 配置

复制示例配置并填入实际值：

```bash
cp .env.example .env
chmod 600 .env
```

必填配置：

| 变量 | 用途 |
| --- | --- |
| `GUIDE_XZKB_BASE_URL` | XZKB 后端根地址；程序会追加知识库聊天路径 |
| `GUIDE_XZKB_API_KEY` | XZKB API 密钥 |
| `GUIDE_XZKB_EMPTY_SEARCH_RESPONSE` | 必须与 XZKB 应用中配置的空回复逐字一致；匹配时作为正常回答并合成语音 |
| `GUIDE_ASR_BASE_URL` | 语音服务根地址；程序会追加 `/audio/transcriptions` |
| `GUIDE_ASR_API_KEY` | ASR API 密钥 |
| `GUIDE_ASR_MODEL` | ASR 模型名称 |
| `GUIDE_TTS_BASE_URL` | 语音服务根地址；程序会追加 `/audio/speech` |
| `GUIDE_TTS_API_KEY` | TTS API 密钥 |
| `GUIDE_TTS_MODEL` | TTS 模型名称 |
| `GUIDE_FAQ_CACHE_ENABLED` | 是否启用高频问答文本缓存，默认开启 |
| `GUIDE_FAQ_CACHE_FILE` | 高频问答 YAML 路径，默认 `config/faq_cache.yaml` |
| `GUIDE_FAQ_PREPARED_AUDIO_ENABLED` | 是否加载已审批的预生成语音，默认开启 |
| `GUIDE_FAQ_ADMIN_ENABLED` | 是否启用 `/faq-cache` 维护接口，默认关闭 |
| `GUIDE_FAQ_ADMIN_API_KEY` | 高频问答维护页独立管理密钥 |
| `GUIDE_DEVICE_API_KEY` | 设备专用接口密钥，建议使用高熵随机值 |
| `GUIDE_KNOWLEDGE_CAPTURE_ENABLED` | 是否启用知识补充；启用后必须配置下列 XZKB 专用账号和知识库 ID |
| `GUIDE_XZKB_USERNAME` | 拥有目标知识库写入权限的 XZKB 专用本地账号 |
| `GUIDE_XZKB_PASSWORD` | XZKB 专用账号密码；仅保存在权限为 `600` 的运行环境文件中 |
| `GUIDE_XZKB_KNOWLEDGE_BASE_ID` | 知识补充写入的目标知识库 ID |
| `GUIDE_XZKB_KNOWLEDGE_FOLDER_ID` | 可选的目标文件夹 ID |
| `GUIDE_CAPTURE_DEVICE` | PipeWire 输入目标；`default` 使用系统默认麦克风 |
| `GUIDE_PLAYBACK_DEVICE` | PipeWire 输出目标；`default` 使用系统默认扬声器 |
| `GUIDE_LOCAL_RECORDING_MAX_SECONDS` | 本地单次录音最长时间，默认 60 秒 |
| `GUIDE_LOCAL_RECORDING_MIN_SECONDS` | 可提交的最短录音时间，默认 0.5 秒 |

其他可调项及默认值见 [.env.example](.env.example)。不要把真实 `.env`、API Key 或设备密钥提交到 Git。

知识补充不使用需要人工定期更换的固定写入 Token。后台同步会用专用账号自动登录，并把 Access Token 仅缓存在进程内存中；遇到 `401` 时自动重新登录并重试一次。登录、上传或处理失败不会丢失已确认知识，本地 Outbox 会保留条目并按退避策略继续同步。

生成设备密钥的一种方式：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 启动

`Settings` 默认从 `/etc/showroom-guide/showroom-guide.env` 读取配置。开发时可以先导出 `.env`：

```bash
set -a
. ./.env
set +a
.venv/bin/python -m uvicorn showroom_guide.main:create_configured_app \
  --factory --app-dir src --host 0.0.0.0 --port 8765
```

启动后可以访问：

- `/`：多人网页问答页
- `/device-test`：设备语音 HTTP 测试页
- `/faq-cache`：高频问答文本与语音维护页；需要单独启用并配置管理密钥
- `/docs`：FastAPI 接口文档

## 设备接口

所有 `/api/device/*` 请求都需要请求头：

```http
X-Device-Key: <GUIDE_DEVICE_API_KEY>
```

一次设备问答：

```bash
curl -X POST http://127.0.0.1:8765/api/device/turn \
  -H "X-Device-Key: $GUIDE_DEVICE_API_KEY" \
  -F "file=@question.wav;type=audio/wav"
```

响应包含识别文本、最终回答和临时音频地址。回答可能来自高频问答缓存或 XZKB，音频可能来自预生成 WAV 或在线 TTS。播放结束后调用 `/api/device/playback-finished`，开始新讲解任务前可调用 `/api/device/reset` 清空设备上下文。

### 本地麦克风与扬声器

树莓派需要安装 PipeWire 命令行工具，并能找到以下命令：

```bash
command -v pw-record
command -v pw-play
```

在 `/device-test` 填写设备密钥后，通过同一个主按钮控制现场交互：短按开始或停止当前模式的录音，长按 1.5 秒在正常对话与知识补充之间切换；等待知识确认时，短按重新录入，长按保存并返回正常对话。WAV 对话测试和 WAV 知识补充位于页面对应区域。

正常对话由树莓派采集默认麦克风，后端先执行 ASR，再尝试高频问答缓存；缓存未命中时访问 XZKB，命中但缺少有效预生成音频时调用在线 TTS，最后通过树莓派默认扬声器自动播放回答。网页只发送控制指令；WAV 知识补充的复述音频仅在当前浏览器播放，不会同时从树莓派扬声器播放。

停止录音后，测试页会启用“播放刚才的录音”。该按钮通过树莓派默认扬声器播放最近一次麦克风 WAV，成功和失败录音都可回放。录音只在进程内存中保留一份，下一次本地录音、设备重置或服务重启后清除，不提供浏览器下载接口。

本地录音接口：

```text
POST /api/device/recording/start
POST /api/device/recording/stop
POST /api/device/recording/replay
```

三个接口都需要 `X-Device-Key`。录音最长 60 秒，到达上限后自动结束并处理；过短或没有可识别语言的录音不会查询知识库。

网页知识补充还会使用以下受保护接口；除了 `X-Device-Key`，控制操作还需要页面获取的 `X-Knowledge-Lease`：

```text
POST /api/device/knowledge/acquire
POST /api/device/knowledge/short-press
POST /api/device/knowledge/long-press
POST /api/device/knowledge/upload
GET  /api/device/knowledge/review-audio
POST /api/device/knowledge/release
GET  /api/device/knowledge/state
```

上传的 WAV 只用于生成当前知识草稿，不会写入 XZKB；用户确认后，规范化文字才进入本地 Outbox 并由后台同步。

更换专用麦克风和扬声器时，将它们设置为 PipeWire 默认输入和默认输出即可，不需要修改项目代码。也可以通过 `GUIDE_CAPTURE_DEVICE`、`GUIDE_PLAYBACK_DEVICE` 指定稳定的 PipeWire 节点名称。

### 查看链路耗时

完成设备问答后，在项目根目录运行：

```powershell
$key = ((Get-Content .env | Where-Object { $_ -match '^GUIDE_DEVICE_API_KEY=' }) -split '=', 2)[1]
(Invoke-RestMethod -Headers @{'X-Device-Key'=$key} http://127.0.0.1:8765/api/device/metrics).metrics | Format-List

$result = Invoke-RestMethod `
    -Headers @{'X-Device-Key'=$key} `
    http://127.0.0.1:8765/api/device/metrics

$result.latest | Format-List
```

device-test 页面会在每次问答后显示最近一次实际耗时。

结果包含 ASR、知识库、TTS 和服务端总耗时的样本数、P50、P95；最多统计最近 500 次成功请求，服务重启后清空。

### 三链路基准测试

基准工具可使用五条标准语音，交错比较“预生成语音”“高频文本 + 在线 TTS”和“XZKB + 在线 TTS”。命令会调用真实上游服务，结果写入已忽略的 `benchmark-results/`：

```powershell
.\.venv\Scripts\python.exe -m showroom_guide.benchmark `
  --env-file .env --standard-fixtures `
  --output-dir benchmark-results/multi-audio-smoke `
  --scenarios prepared_audio faq_online_tts xzkb_online_tts `
  --mode reliability --execution-order interleaved `
  --runs 5 --warmup 1 --interval-seconds 10 `
  --confirm-live-requests
```

## 测试

```bash
.venv/bin/python -m pytest
```

自动测试使用替身服务，不会调用真实 XZKB、ASR 或 TTS。

## systemd 开机自启

模板按仓库位于 `$HOME/xzinfra-voice-guide` 设计：

```bash
mkdir -p ~/.config/xzinfra-voice-guide ~/.config/systemd/user
cp .env.example ~/.config/xzinfra-voice-guide/xzinfra-voice-guide.env
chmod 600 ~/.config/xzinfra-voice-guide/xzinfra-voice-guide.env
cp deploy/systemd/showroom-guide.service ~/.config/systemd/user/showroom-guide.service
systemctl --user daemon-reload
systemctl --user enable --now showroom-guide.service
```

编辑 `~/.config/xzinfra-voice-guide/xzinfra-voice-guide.env` 后重启：

```bash
systemctl --user restart showroom-guide.service
systemctl --user status showroom-guide.service
```

需要在未登录时启动用户服务，可由管理员执行：

```bash
sudo loginctl enable-linger <linux-user>
```

## 安全边界

- 仓库不保存生产密钥、访客现场问题或临时录音；仅保存经审核的高频问答文本和预生成讲解语音。
- 网页会话和临时音频只保存在进程内存，服务重启后清空。
- 设备接口密钥用于阻止未授权调用，但不能替代 HTTPS、网络隔离和入口限流。
- 面向非可信网络部署时，应在反向代理层启用 HTTPS 并限制来源。
- 本项目只调用 XZKB API，不修改 XZKB 源码。
