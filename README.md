# XZInfra Voice Guide

运行在 Raspberry Pi 5 上的展厅 AI 语音讲解服务。它将访客提问依次发送到远程 ASR、XZKB 知识库和 TTS 服务，支持网页多人会话，也提供面向树莓派物理设备的语音闭环接口。

## 功能

- 网页端文字提问、知识库回答和语音播放
- 基于 HttpOnly Cookie 的多人会话隔离
- 每个会话独立保存短期上下文、状态和临时音频
- 设备端 WAV 上传、ASR、XZKB、TTS 和播放完成回执
- 设备接口使用独立密钥鉴权
- XZKB 与 TTS 并发门控和排队超时
- TTS 不可用时保留文字回答
- 用户级 systemd 开机自启模板

## 处理流程

```text
网页用户 ──文字问题──────────────┐
                                 ├──> XZKB ──> TTS ──> 网页播放
树莓派设备 ──WAV ──> ASR ───────┘
```

网页访客之间不共享会话。物理设备使用单独的长期会话，因此网页访问不会污染设备的追问上下文。

## 环境要求

- Raspberry Pi 5 或其他 Linux 主机
- Python 3.11 或更高版本
- 可访问兼容接口的 XZKB、ASR 和 TTS 服务
- 设备语音输入必须是单声道、16-bit、16 kHz PCM WAV

当前代码已完成 HTTP 语音闭环。真实麦克风、扬声器和按键需要在树莓派上完成硬件接入后，再由本地控制程序调用设备接口。

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
| `GUIDE_ASR_BASE_URL` | 语音服务根地址；程序会追加 `/audio/transcriptions` |
| `GUIDE_ASR_API_KEY` | ASR API 密钥 |
| `GUIDE_ASR_MODEL` | ASR 模型名称 |
| `GUIDE_TTS_BASE_URL` | 语音服务根地址；程序会追加 `/audio/speech` |
| `GUIDE_TTS_API_KEY` | TTS API 密钥 |
| `GUIDE_TTS_MODEL` | TTS 模型名称 |
| `GUIDE_DEVICE_API_KEY` | 设备专用接口密钥，建议使用高熵随机值 |

其他可调项及默认值见 [.env.example](.env.example)。不要把真实 `.env`、API Key 或设备密钥提交到 Git。

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

响应包含识别文本、知识库回答和临时 TTS 音频地址。播放结束后调用 `/api/device/playback-finished`，开始新讲解任务前可调用 `/api/device/reset` 清空设备上下文。

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

- 仓库不保存生产密钥、访客问题、回答或语音文件。
- 网页会话和临时音频只保存在进程内存，服务重启后清空。
- 设备接口密钥用于阻止未授权调用，但不能替代 HTTPS、网络隔离和入口限流。
- 面向非可信网络部署时，应在反向代理层启用 HTTPS 并限制来源。
- 本项目只调用 XZKB API，不修改 XZKB 源码。
