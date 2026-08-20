# 高频问答缓存维护说明

## 文件作用

`config/faq_cache.yaml` 是人工审核的固定问答清单。每条记录对应一个稳定主题，包含标准问题 ID、常见口语问法、固定回答和预生成 WAV 路径。

它不是知识库分段文件，也不需要上传到知识库。应用启动时会读取这份清单，并在调用知识库之前执行匹配。

## 字段说明

- `id`：稳定且唯一的问题 ID，写入代码和日志后不要随意修改。
- `title`：人工查看时使用的主题名称。
- `enabled`：是否启用这条缓存。内容未审核完成时设置为 `false`。
- `priority`：人工安排音频生成和测试顺序，取值为 `high` 或 `medium`。
- `version`：答案或语音配置变化时递增，用于缓存失效。
- `aliases`：用户可能说出的完整问题。匹配时应先去除标点、空格并统一英文大小写，再做精确匹配。
- `answer`：人工审核后的固定讲解内容。
- `audio_file`：预生成 WAV 文件路径。路径相对于 `config/faq_cache.yaml` 所在目录，必须使用正斜杠 `/`、位于目录内且为 `.wav` 文件；工具会写入同目录下的 `prepared_audio/manifest.json`。

## 日常维护流程

1. 从真实 ASR 日志中收集重复出现的问题。
2. 判断该问题是否与现有 `id` 表达相同意图。
3. 同一意图只新增一条 `alias`，不要复制一整条记录。
4. 新主题需要增加新的 `id`、答案和音频路径。
5. 修改答案后递增 `version`，重新生成对应 WAV。
6. 在本地分别测试每个 alias，确认它们都命中正确的 `id`。
7. 未命中的问题继续走原有知识库和 TTS 链路。

## 不能使用固定缓存的问题

以下问题必须继续访问实时系统或知识库，不能返回静态答案：

- 今天消耗了多少 Token。
- 当前算力使用率是多少。
- 当前设备是否正常。
- 现在的温度、能耗、PUE 或告警情况。
- “这个是什么”“它有什么作用”等依赖对话上下文的问题。
- 用户要求比较、重新总结、换一种表达或结合新条件回答的问题。

## 匹配原则

匹配顺序是“精确 alias → 规则匹配 → 知识库”。精确 alias 始终优先于规则和全局排除词。

规则只在单个条目内同时命中至少一个主题词和一个意图词时生效。排除词用于阻止实时、比较、改写等问题使用固定回答，但不拦截已经命中的精确 alias。多个条目的规则同时命中时回退知识库，不按 priority 或配置顺序选择。

规则词也会执行空白、标点、大小写和全角半角标准化，但只对配置的主题词、意图词和排除词做包含判断，不对完整问题做宽泛匹配。新规则应先针对单个条目试点，并同时测试正例和负例。

## 预生成语音

第三阶段工具使用项目现有 Settings 和 TTS 配置生成合法、未压缩的 WAV；第四阶段启动时会校验并加载已生成的有效 WAV，接入在线问答返回链路。

在仓库根目录的 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe -m showroom_guide.faq_audio --env-file .env --priority high
.\.venv\Scripts\python.exe -m showroom_guide.faq_audio --env-file .env --entry eight_workshops_overview
.\.venv\Scripts\python.exe -m showroom_guide.faq_audio --env-file .env --priority high --dry-run
.\.venv\Scripts\python.exe -m showroom_guide.faq_audio --env-file .env --priority all --verify-only
```

`--dry-run` 只显示需要生成或跳过的条目，不调用 TTS、不写 WAV；默认只处理启用的 `high` 条目，`--priority medium` 或 `all` 可选择其他优先级。`--entry` 可重复使用，并只处理指定 ID。`--verify-only` 只检查现有 WAV 和 manifest，不能与 `--dry-run` 或 `--force` 同时使用。修改 `answer`、`version`、`audio_file` 或 TTS 的 model、voice、speed 后，manifest 会将条目标记为 stale；`--force` 可强制重新生成。

工具先把 TTS 结果写入目标目录中的临时文件，并读取声明的全部 PCM 帧校验实际字节数，再提交 WAV 和 manifest。覆盖前会保留同目录备份；两者都成功后才删除备份，提交失败会回滚 WAV 和 manifest。manifest 同时记录完整 WAV 的 `wav_sha256`，内容变化会被标记为 stale。单条失败不会覆盖已有有效 WAV，会继续处理后续条目并以非零状态结束。

临时录音和普通测试音频不能提交到仓库。管理页面生成的待审批 WAV 与配套 JSON 可以提交，便于两人共同试听和调试；试听通过的正式预生成 WAV 和对应 `manifest.json` 也应一起提交，确保树莓派拉取代码后可以直接加载。`.gitignore` 默认忽略其他 WAV，只放行 `config/prepared_audio/*.wav` 和 `config/prepared_audio/.pending/` 下的受管草稿。

运行时默认启用 `GUIDE_FAQ_PREPARED_AUDIO_ENABLED=true`。启动时只加载同时通过 manifest、答案/version/TTS profile、WAV 元数据和完整文件哈希校验的条目；缺失、损坏或 stale 的单条音频只记录 warning，并回退在线 TTS。设置为 `false` 或关闭 FAQ 缓存时不会读取 manifest 或 WAV。预生成命中会复用现有会话 AudioStore 和受保护音频 URL，并跳过知识库、TTS gate 和在线 TTS。

## 上线顺序

1. 先启用 `priority: high` 的条目。
2. 记录 `cache_hit`、`cache_entry_id`、`served_from` 和服务端总耗时。
3. 观察一段时间内的误命中和未命中问题。
4. 修正 aliases 后，再逐步启用 `priority: medium` 的条目。
5. 文本缓存命中时，若预生成 WAV 有效则直接播放；否则继续调用在线 TTS。预生成命中时知识库和 TTS 各阶段耗时均保持 `null`，单次 timing 的 `served_from` 为 `prepared_audio`。

## 专用维护界面

高频问答维护应使用独立管理页，不放入 `/device-test`。设备测试页用于验证 ASR、知识库、TTS 和播放链路；缓存管理页负责修改正式内容和生成部署资产，两者权限和操作风险不同。

页面路由：

```text
/faq-cache
```

管理接口前缀：

```text
/api/faq-cache
```

当前页面已提供管理密钥连接、摘要统计、搜索筛选、条目列表，以及文本、匹配规则和音频状态详情。页面刷新时重新读取 YAML、manifest 和 WAV，管理密钥只保留在页面内存中。

文本条目维护规则：

- 可编辑 `title`、`enabled`、`priority`、`aliases`、`match_rules` 和 `answer`。
- 新建时填写 ID，后端自动设置 `version: 1` 和 `prepared_audio/{id}.wav`；新条目默认应保持停用，确认匹配效果后再启用。
- 已有条目的 ID 和 `audio_file` 不可修改；修改 `answer` 时自动递增 version，其他字段变化不影响音频版本。
- 保存前验证完整 YAML，包括重复 ID、alias 冲突、规则字段和路径约束；验证成功后才原子替换正式配置。
- 页面使用 `edit_token` 检测并发修改；旧页面保存或删除已被他人修改的条目时返回冲突，必须刷新后重试。
- 删除只移除 YAML 条目，正式 WAV、manifest 记录和待审批草稿均保留，避免不可恢复的数据删除。

单条语音维护流程：

1. 选择条目并点击“生成语音草稿”，后端使用 YAML 中的完整 `answer` 调用当前 TTS。
2. 生成结果写入 `config/prepared_audio/.pending/`，不会覆盖正式 WAV，也不会被运行时加载；需要协作试听时，WAV 和同名条目的 JSON 元数据必须一起提交。
3. 完整播放待审批草稿后，页面才启用“审批通过并安装”。
4. 审批时再次校验条目 version、answer、TTS profile、WAV 元数据和哈希。
5. 校验通过后，使用现有事务逻辑写入条目的 `audio_file` 和正式 manifest；失败时保留原正式文件。
6. 正式安装后重启服务，新音频才会进入运行时预生成缓存。

后续阶段计划提供：

- 对选中条目执行预演或批量生成和校验。
- 增加独立的审核记录；当前以“审批后事务安装到正式目录”表示通过。
- 增加音频资产清理工具，处理删除条目后人工确认不再需要的孤立 WAV 和 manifest 记录。
- 实现音频和文本热更新；当前修改后仍需重启服务才能进入运行时缓存。

管理页不得直接调用设备接口，也不能复用设备密钥作为长期管理凭据。建议新增独立配置：

```env
GUIDE_FAQ_ADMIN_ENABLED=false
GUIDE_FAQ_ADMIN_API_KEY=replace-with-admin-key
```

管理功能默认关闭。启用后，页面对应的读写、生成、校验和试听接口都必须验证管理密钥；密钥不能写入 HTML、日志、URL 或仓库。浏览器侧只在当前页面会话中保存密钥，刷新或关闭页面后清除。

写入和生成必须满足以下约束：

- YAML 使用结构化解析和原子替换，写入失败时保留原文件。
- YAML 写入使用同目录临时文件，完整校验并 `fsync` 后原子替换；替换失败时保留原文件。
- 同一时间只允许一个保存或音频生成任务，避免 YAML、WAV 和 manifest 相互覆盖。
- 后端只允许操作 `config/faq_cache.yaml` 及其受管的 `prepared_audio` 目录。
- API 不接受任意文件路径、命令行参数或环境变量名。
- TTS 失败时保留旧的有效 WAV 和 manifest，并向页面返回脱敏错误。

分阶段实现和验证：

1. 只读列表：已完成，展示文本、匹配配置和音频状态，不允许修改。
2. 文本维护：已完成新建、编辑、删除、完整校验、原子保存和并发冲突保护。
3. 语音维护：已完成单条生成、试听和审批安装；独立审核记录待后续补充。
4. 批量操作：增加按优先级生成、任务进度和失败重试。

首版不增加数据库、Redis、多人协同编辑或音频热更新。对于当前两人维护、单机部署的场景，YAML、manifest 和正式 WAV 继续作为唯一数据源，管理页只是这些现有工具的受保护操作界面。
