# 高频问答缓存维护说明

## 文件作用

`config/faq_cache.yaml` 是人工审核的固定问答清单。每条记录对应一个稳定主题，包含标准问题 ID、常见口语问法、固定回答和预生成 WAV 路径。

它不是知识库分段文件，也不需要上传到知识库。后续代码应在调用知识库之前读取这份清单并执行匹配。

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
python -m showroom_guide.faq_audio --env-file .env --priority high
python -m showroom_guide.faq_audio --env-file .env --entry eight_workshops_overview
python -m showroom_guide.faq_audio --env-file .env --priority high --dry-run
python -m showroom_guide.faq_audio --env-file .env --priority all --verify-only
```

`--dry-run` 只显示需要生成或跳过的条目，不调用 TTS、不写 WAV；默认只处理启用的 `high` 条目，`--priority medium` 或 `all` 可选择其他优先级。`--entry` 可重复使用，并只处理指定 ID。`--verify-only` 只检查现有 WAV 和 manifest，不能与 `--dry-run` 或 `--force` 同时使用。修改 `answer`、`version`、`audio_file` 或 TTS 的 model、voice、speed 后，manifest 会将条目标记为 stale；`--force` 可强制重新生成。

工具先把 TTS 结果写入目标目录中的临时文件，并读取声明的全部 PCM 帧校验实际字节数，再提交 WAV 和 manifest。覆盖前会保留同目录备份；两者都成功后才删除备份，提交失败会回滚 WAV 和 manifest。manifest 同时记录完整 WAV 的 `wav_sha256`，内容变化会被标记为 stale。单条失败不会覆盖已有有效 WAV，会继续处理后续条目并以非零状态结束。不要把测试生成的 WAV 或 manifest 提交到仓库。

运行时默认启用 `GUIDE_FAQ_PREPARED_AUDIO_ENABLED=true`。启动时只加载同时通过 manifest、答案/version/TTS profile、WAV 元数据和完整文件哈希校验的条目；缺失、损坏或 stale 的单条音频只记录 warning，并回退在线 TTS。设置为 `false` 或关闭 FAQ 缓存时不会读取 manifest 或 WAV。预生成命中会复用现有会话 AudioStore 和受保护音频 URL，并跳过知识库、TTS gate 和在线 TTS。

## 上线顺序

1. 先启用 `priority: high` 的条目。
2. 记录 `cache_hit`、`cache_entry_id` 和 `cache_lookup_ms`。
3. 观察一段时间内的误命中和未命中问题。
4. 修正 aliases 后，再逐步启用 `priority: medium` 的条目。
5. 文本缓存命中时，若预生成 WAV 有效则直接播放；否则继续调用在线 TTS。预生成命中时知识库和 TTS 各阶段耗时均保持 `null`，单次 timing 的 `served_from` 为 `prepared_audio`。
