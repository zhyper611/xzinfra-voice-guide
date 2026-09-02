# 独立是非判断模式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有树莓派展厅讲解器中实现可由单按钮切换的独立是非判断模式，每轮只调用一次 XZKB 混合判断应用，并用舵机完成思考、反向蓄势和结果保持动作。

**Architecture:** 保留现有对话控制器和知识补充工作流，把是非判断实现为独立的 `VerdictWorkflow`，由统一三模式状态机编排。混合应用客户端只负责一次请求和严格解析；工作流输出抽象动作事件，网页会话使用动画适配器，实体设备使用 GPIO 舵机适配器。两者共用业务流程但拥有独立状态和轮次，网页默认不访问 GPIO。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、httpx、asyncio、gpiozero、pytest、原生 JavaScript/HTML/CSS。

---

## 文件结构

- `src/showroom_guide/verdict.py`：判断枚举、结构化结果、组合校验和服务降级。
- `src/showroom_guide/clients/verdict.py`：XZKB 混合判断应用的一次性 HTTP 调用与响应解析。
- `src/showroom_guide/verdict_workflow.py`：是非模式单轮 ASR 后的判断编排、轮次失效和提示选择。
- `src/showroom_guide/verdict_motion.py`：统一动作事件、输出协议与网页状态输出适配器。
- `src/showroom_guide/servo_motion.py`：实体舵机输出适配器、思考循环、反向蓄势、结果保持和故障熔断。
- `src/showroom_guide/button_workflow.py`：单按钮三模式状态机和知识补充控制权兼容。
- `src/showroom_guide/models.py`、`state.py`：设备可观察状态。
- `src/showroom_guide/local_device.py`、`device.py`：复用录音/ASR，但按当前模式分派到对话或判断工作流。
- `src/showroom_guide/main.py`、`config.py`：依赖装配、配置校验、设备 API 与生命周期。
- `src/showroom_guide/web/device-test.*`、`servo-simulator.js`：三模式测试和动作可视化。
- `docs/verdict-application.md`：XZKB 应用设置、固定提示词、验证样例。

## 旧原型处理原则

当前工作区中旧舵机方案没有独立 Git 提交，不执行 `git revert`、`git reset` 或整文件恢复。每项任务先保留对应文件中的仍适用内容，再以测试驱动替换旧行为：

- 保留：`Verdict` 枚举、严格 JSON 解析、轮次编号、GPIO 异常熔断、前端模拟器的隔离思路。
- 删除：XZKB 空回复分流、对话回答后台追加判断、日常判断口播、录音前强制回中央。
- 改写：两模式按钮状态机、固定角度舵机控制器、判断模型直连接口。

### Task 1：冻结旧行为并建立三模式领域模型

**Files:**
- Modify: `src/showroom_guide/models.py`
- Modify: `src/showroom_guide/state.py`
- Modify: `src/showroom_guide/verdict.py`
- Test: `tests/unit/test_state.py`
- Test: `tests/unit/test_verdict.py`

- [ ] **Step 1: 写失败测试，定义模式、判断阶段和结构化组合**

```python
def test_snapshot_defaults_to_conversation_without_active_verdict():
    snapshot = GuideSnapshot()
    assert snapshot.interaction_mode is InteractionMode.CONVERSATION
    assert snapshot.verdict_phase is VerdictPhase.IDLE
    assert snapshot.verdict is Verdict.NEUTRAL
    assert snapshot.verdict_reason == ""

def test_exhibition_binary_requires_knowledge_evidence():
    decision = validate_decision({
        "scope": "exhibition", "verdict": "yes", "basis": "general",
        "reason": "模型常识", "evidence": ""
    })
    assert decision.verdict is Verdict.NEUTRAL
    assert decision.failure is VerdictFailure.INSUFFICIENT_EVIDENCE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_state.py tests/unit/test_verdict.py`

Expected: FAIL，缺少 `InteractionMode`、`VerdictPhase` 和新决策字段。

- [ ] **Step 3: 实现最小领域模型**

```python
class InteractionMode(StrEnum):
    CONVERSATION = "conversation"
    VERDICT = "verdict"
    KNOWLEDGE = "knowledge"

class VerdictPhase(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    WINDUP = "windup"
    DECISIVE = "decisive"
    HOLDING = "holding"
    NEUTRAL = "neutral"
    FAILED = "failed"

class VerdictScope(StrEnum):
    EXHIBITION = "exhibition"
    CASUAL = "casual"
    INVALID = "invalid"

class VerdictBasis(StrEnum):
    KNOWLEDGE_BASE = "knowledge_base"
    GENERAL = "general"
    NONE = "none"
```

扩展 `GuideSnapshot`，并给 `GuideStateStore` 增加原子更新模式、开始判断、发布结果和清空判断详情的方法。`answer` 仍只放讲解口播，判断理由不得写入 `answer`。

- [ ] **Step 4: 运行领域测试**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_state.py tests/unit/test_verdict.py`

Expected: PASS。

- [ ] **Step 5: 提交领域模型**

```powershell
git add src/showroom_guide/models.py src/showroom_guide/state.py src/showroom_guide/verdict.py tests/unit/test_state.py tests/unit/test_verdict.py
git commit -m "feat: 建立独立判断模式状态模型"
```

### Task 2：实现单次 XZKB 混合判断应用客户端

**Files:**
- Modify: `src/showroom_guide/clients/verdict.py`
- Modify: `src/showroom_guide/config.py`
- Modify: `.env.example`
- Test: `tests/unit/test_verdict_client.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: 写失败测试，锁定一次调用和严格协议**

```python
@pytest.mark.asyncio
async def test_decide_posts_one_question_to_xzkb_application(httpx_mock):
    httpx_mock.add_response(json={"choices": [{"message": {"content": json.dumps({
        "scope": "casual", "verdict": "yes", "basis": "general",
        "reason": "低风险日常判断", "evidence": ""
    }, ensure_ascii=False)}}]})
    client = VerdictClient("http://kb.test", "secret-key", timeout=15)
    decision = await client.decide("今天适合喝咖啡吗？")
    assert decision.verdict is Verdict.YES
    assert len(httpx_mock.get_requests()) == 1
    assert httpx_mock.get_requests()[0].url.path.endswith(
        "/kb-matrix/data-infra/v1/chat/completions"
    )
```

同时覆盖非 JSON、Markdown 代码围栏、未知枚举、字段缺失、超长 `reason/evidence`、HTTP 错误和超时。

- [ ] **Step 2: 运行客户端和配置测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_client.py tests/unit/test_config.py`

Expected: FAIL，旧客户端仍调用通用 `/chat/completions` 并要求 `model`。

- [ ] **Step 3: 改写客户端与配置**

```python
class VerdictClient:
    async def decide(self, question: str) -> VerdictDecision:
        response = await self._client.post(
            self._url,
            json={"messages": [{"role": "user", "content": question}], "stream": False},
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return validate_decision(json.loads(content))
```

将判断配置改为 `base_url/api_key/timeout_seconds`，删除 `verdict_model`；启用时要求 URL 和 Key 完整。`.env.example` 只放占位值，不放真实凭据。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_client.py tests/unit/test_config.py`

Expected: PASS。

- [ ] **Step 5: 提交客户端**

```powershell
git add .env.example src/showroom_guide/clients/verdict.py src/showroom_guide/config.py tests/unit/test_verdict_client.py tests/unit/test_config.py
git commit -m "feat: 接入混合是非判断应用"
```

### Task 3：建立统一动作协议并实现舵机适配器

**Files:**
- Create: `src/showroom_guide/verdict_motion.py`
- Modify: `src/showroom_guide/servo_motion.py`
- Test: `tests/unit/test_verdict_motion.py`
- Test: `tests/unit/test_servo_motion.py`

- [ ] **Step 1: 写失败测试覆盖思考、蓄势、快速判断和保持**

```python
@pytest.mark.asyncio
async def test_yes_moves_to_no_side_then_immediately_to_yes_and_holds():
    driver = FakeServoDriver()
    motion = ServoMotionOutput(driver, yes_angle=20, neutral_angle=75, no_angle=130)
    await motion.thinking(generation=1)
    await motion.show_verdict(Verdict.YES, generation=1)
    assert driver.last_two_angles == [130, 20]
    assert driver.disable_calls == 1
    assert driver.current_angle == 20

@pytest.mark.asyncio
async def test_recording_keeps_previous_result_without_motion():
    await motion.show_verdict(Verdict.NO, generation=1)
    before = list(driver.angles)
    await motion.prepare_recording()
    assert driver.angles == before
```

再覆盖：思考循环可取消、中立小幅左右后回中、离开模式回中、关闭回中、动作异常熔断、新动作取消旧动作。

- [ ] **Step 2: 运行舵机测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_motion.py tests/unit/test_servo_motion.py`

Expected: FAIL，旧实现只有固定位置并且录音前回中。

- [ ] **Step 3: 实现命令式动作 API**

```python
class VerdictMotionOutput(Protocol):
    async def thinking(self, generation: int) -> None: ...
    async def show_verdict(self, verdict: Verdict, generation: int) -> None: ...
    async def show_neutral(self, generation: int) -> None: ...
    async def reset(self) -> None: ...

class ServoMotionOutput:
    async def enter_mode(self) -> None: ...
    async def prepare_recording(self) -> None: ...  # 只取消运动，不改变角度
    async def thinking(self, generation: int) -> None: ...
    async def show_verdict(self, verdict: Verdict, generation: int) -> None: ...
    async def show_neutral(self, generation: int) -> None: ...
    async def leave_mode(self) -> None: ...
```

同时实现 `WebSimulationOutput`，它只把动作事件写入传入的网页会话状态，绝不导入 gpiozero。`show_verdict(YES)` 的实体角度序列固定为 `no_angle -> yes_angle`，`show_verdict(NO)` 固定为 `yes_angle -> no_angle`，两次写角度之间不调用 sleep。最终到位只等待稳定时间再 `disable()`，不写回中央。

- [ ] **Step 4: 运行舵机测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_motion.py tests/unit/test_servo_motion.py`

Expected: PASS。

- [ ] **Step 5: 提交动作控制器**

```powershell
git add src/showroom_guide/verdict_motion.py src/showroom_guide/servo_motion.py tests/unit/test_verdict_motion.py tests/unit/test_servo_motion.py
git commit -m "feat: 建立判断动作输出协议"
```

### Task 4：实现独立判断工作流

**Files:**
- Create: `src/showroom_guide/verdict_workflow.py`
- Test: `tests/unit/test_verdict_workflow.py`

- [ ] **Step 1: 写失败测试覆盖单轮编排与降级**

```python
@pytest.mark.asyncio
async def test_run_starts_thinking_calls_application_once_and_holds_yes():
    result = VerdictDecision.exhibition_yes("有知识依据", "知识片段")
    workflow = make_workflow(decision=result)
    await workflow.run("该产品支持国产算力吗？")
    assert workflow.motion.calls == [("thinking", 1), ("show_verdict", Verdict.YES, 1)]
    assert workflow.client.questions == ["该产品支持国产算力吗？"]
    assert workflow.state.snapshot.verdict_phase is VerdictPhase.HOLDING
```

覆盖 invalid、依据不足、高风险中立、客户端超时、舵机失败、旧轮次晚返回、退出模式取消任务和短提示播放失败。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_workflow.py`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现工作流**

```python
async def run(self, transcript: str) -> VerdictDecision:
    generation = self._next_generation()
    await self._state.begin_verdict(transcript)
    await self._motion.thinking(generation)
    try:
        async with asyncio.timeout(self._timeout_seconds):
            decision = await self._client.decide(transcript)
    except (TimeoutError, VerdictClientError):
        decision = VerdictDecision.service_failure()
    if generation != self._generation:
        raise asyncio.CancelledError
    if decision.verdict is Verdict.NEUTRAL:
        await self._motion.show_neutral(generation)
    else:
        await self._motion.show_verdict(decision.verdict, generation)
    await self._state.finish_verdict(decision)
    await self._play_neutral_prompt_if_needed(decision)
    return decision
```

提示音按 `invalid/insufficient_evidence/high_risk/service_failure` 映射到本地命名资源；明确 `yes/no` 不口播。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_verdict_workflow.py`

Expected: PASS。

- [ ] **Step 5: 提交工作流**

```powershell
git add src/showroom_guide/verdict_workflow.py tests/unit/test_verdict_workflow.py
git commit -m "feat: 编排独立是非判断流程"
```

### Task 5：将单按钮改为三模式状态机

**Files:**
- Modify: `src/showroom_guide/button_workflow.py`
- Modify: `src/showroom_guide/knowledge_mode.py`
- Test: `tests/unit/test_button_workflow.py`
- Test: `tests/unit/test_knowledge_mode.py`

- [ ] **Step 1: 写失败测试锁定按钮语义**

```python
@pytest.mark.asyncio
async def test_long_press_cycles_conversation_verdict_knowledge():
    workflow = make_button_workflow()
    await workflow.long_press()
    assert workflow.mode is InteractionMode.VERDICT
    await workflow.long_press()
    assert workflow.mode is InteractionMode.KNOWLEDGE

@pytest.mark.asyncio
async def test_verdict_mode_short_press_toggles_recording():
    workflow = make_button_workflow()
    await workflow.enter_verdict()
    await workflow.short_press()
    await workflow.short_press()
    assert workflow.verdict_device.calls == ["start_recording", "stop_recording"]
```

覆盖处理中忽略按键、结果保持仍可短按/长按、知识保存返回对话、知识取消返回对话、网页知识控制权只可从对话空闲获取。

- [ ] **Step 2: 运行按钮测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_button_workflow.py tests/unit/test_knowledge_mode.py`

Expected: FAIL，当前只有 dialogue/knowledge 两模式。

- [ ] **Step 3: 实现三模式切换**

```python
if self._mode is InteractionMode.CONVERSATION:
    await self._verdict.enter()
    await self._set_mode(InteractionMode.VERDICT)
elif self._mode is InteractionMode.VERDICT:
    await self._verdict.leave()
    await self._knowledge.enter()
    await self._set_mode(InteractionMode.KNOWLEDGE)
else:
    result = await self._knowledge.long_press()
    if result.exited:
        await self._set_mode(InteractionMode.CONVERSATION)
```

模式提示音由 `_set_mode()` 成功后调用本地播放器；操作失败时保留原模式。不要让提示音失败回滚已经完成的模式切换。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_button_workflow.py tests/unit/test_knowledge_mode.py`

Expected: PASS。

- [ ] **Step 5: 提交按钮状态机**

```powershell
git add src/showroom_guide/button_workflow.py src/showroom_guide/knowledge_mode.py tests/unit/test_button_workflow.py tests/unit/test_knowledge_mode.py
git commit -m "feat: 支持单按钮三模式切换"
```

### Task 6：复用录音和 ASR，并按模式分派

**Files:**
- Modify: `src/showroom_guide/local_device.py`
- Modify: `src/showroom_guide/device.py`
- Modify: `src/showroom_guide/controller.py`
- Test: `tests/unit/test_local_device.py`
- Test: `tests/unit/test_text_controller.py`

- [ ] **Step 1: 写失败测试证明模式互斥**

```python
@pytest.mark.asyncio
async def test_verdict_recording_uses_asr_but_never_calls_dialogue_controller():
    device = make_device(mode=InteractionMode.VERDICT, transcript="这是国产芯片吗？")
    await device.process_recorded_wav(make_wav())
    assert device.verdict_workflow.transcripts == ["这是国产芯片吗？"]
    device.dialogue_controller.ask_text.assert_not_awaited()
    device.tts.synthesize.assert_not_awaited()
```

同时写回归测试：对话 FAQ 命中、预制音频、XZKB 未命中、多轮上下文均不调用判断客户端；删除旧 `_synthesize_with_verdict()` 和后台判断任务断言。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_local_device.py tests/unit/test_text_controller.py`

Expected: FAIL，当前判断挂在 `GuideController` 的讲解末端。

- [ ] **Step 3: 改写分派边界**

ASR 公共流程输出 transcript 后，根据设备工作流当前模式分派：对话调用 `GuideController.ask_text()`；判断调用 `VerdictWorkflow.run()`。从 `GuideController` 删除所有判断服务、判断任务和空回复日常分流，使其恢复为纯讲解控制器。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_local_device.py tests/unit/test_text_controller.py`

Expected: PASS。

- [ ] **Step 5: 提交设备分派**

```powershell
git add src/showroom_guide/local_device.py src/showroom_guide/device.py src/showroom_guide/controller.py tests/unit/test_local_device.py tests/unit/test_text_controller.py
git commit -m "refactor: 隔离讲解与是非判断流程"
```

### Task 7：装配运行时、API 和生命周期

**Files:**
- Modify: `src/showroom_guide/main.py`
- Modify: `src/showroom_guide/models.py`
- Test: `tests/unit/test_runtime.py`
- Test: `tests/integration/test_web_app.py`

- [ ] **Step 1: 写失败测试覆盖启停和设备隔离**

```python
def test_runtime_uses_separate_motion_outputs_for_web_and_physical_device():
    runtime = create_runtime(make_settings(verdict_enabled=True))
    web_session = runtime.web_session_factory()
    assert isinstance(runtime.device.verdict_workflow.motion, ServoMotionOutput)
    assert isinstance(web_session.verdict_workflow.motion, WebSimulationOutput)
    assert web_session.state is not runtime.device.state

def test_device_state_exposes_mode_and_verdict_details(client, device_key):
    payload = client.get("/api/device/state", headers=device_key).json()
    assert payload["interaction_mode"] == "conversation"
    assert payload["verdict_phase"] == "idle"
```

覆盖启动回中、退出回中、客户端关闭、舵机初始化失败旁路、判断未配置时仅禁用是非模式。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_runtime.py tests/integration/test_web_app.py`

Expected: FAIL，运行时仍把旧 `VerdictService` 注入讲解控制器。

- [ ] **Step 3: 完成依赖装配**

创建物理设备专用 `VerdictWorkflow` 并注入 `ServoMotionOutput`；为每个设备测试网页会话创建独立 `VerdictWorkflow` 并注入 `WebSimulationOutput`。两者可以复用线程安全的 `VerdictClient`，但不得共享状态、模式或轮次。应用关闭顺序先取消判断任务，再让实体舵机回中，最后关闭 HTTP 客户端。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest -q -o filterwarnings= tests/unit/test_runtime.py tests/integration/test_web_app.py`

Expected: PASS。

- [ ] **Step 5: 提交运行时装配**

```powershell
git add src/showroom_guide/main.py src/showroom_guide/models.py tests/unit/test_runtime.py tests/integration/test_web_app.py
git commit -m "feat: 装配物理设备判断模式"
```

### Task 8：在网页对话区加入三模式测试和动画适配

**Files:**
- Modify: `src/showroom_guide/web/device-test.html`
- Modify: `src/showroom_guide/web/device-test.css`
- Modify: `src/showroom_guide/web/device-test.js`
- Modify: `src/showroom_guide/web/servo-simulator.js`
- Modify: `tests/js/servo-simulator.test.cjs`
- Modify: `tests/integration/test_web_app.py`

- [ ] **Step 1: 写失败测试覆盖三模式显示和动作序列**

```javascript
test("yes result winds up right, snaps left, and stays left", async () => {
  const servo = new ServoSimulator({ sleep: async () => {} });
  await servo.think();
  await servo.show("yes");
  assert.deepEqual(servo.history.slice(-2), ["windup-no", "holding-yes"]);
  assert.equal(servo.snapshot().angle, 20);
});
```

集成测试断言页面提供同级“对话 / 是非判断 / 知识补充”切换，并显示 `interaction_mode`、`scope`、`basis`、`reason`、`evidence`、动作阶段和耗时。网页是非请求必须调用会话级工作流，运行时不得创建或调用实体舵机适配器。

- [ ] **Step 2: 运行 JS 和页面测试确认失败**

Run: `node --test tests/js/*.test.cjs`

Run: `python -m pytest -q -o filterwarnings= tests/integration/test_web_app.py`

Expected: FAIL，当前模拟器只有固定三态，没有模式切换和思考动作。

- [ ] **Step 3: 实现测试页**

前端使用当前网页会话状态作为唯一来源。切到是非判断后，浏览器录音和 WAV 上传调用真实 ASR 与混合判断应用，请求期间显示思考动画，结果返回后播放蓄势、快速判断和保持动画。独立动作预览只修改浏览器动画，不调用 ASR、AI 或实体 API。动作类名固定为 `thinking/windup/decisive/holding/neutral/failed`，并支持 `prefers-reduced-motion`。

- [ ] **Step 4: 运行前端测试确认通过**

Run: `node --test tests/js/*.test.cjs`

Run: `python -m pytest -q -o filterwarnings= tests/integration/test_web_app.py`

Expected: PASS。

- [ ] **Step 5: 提交测试页**

```powershell
git add src/showroom_guide/web/device-test.html src/showroom_guide/web/device-test.css src/showroom_guide/web/device-test.js src/showroom_guide/web/servo-simulator.js tests/js/servo-simulator.test.cjs tests/integration/test_web_app.py
git commit -m "feat: 模拟独立是非判断模式"
```

### Task 9：编写 XZKB 应用提示词与操作说明

**Files:**
- Create: `docs/verdict-application.md`
- Modify: `README.md`

- [ ] **Step 1: 写完整应用配置文档**

文档必须包含可直接粘贴的系统提示词，核心约束如下：

```text
你是实体 AI 是非判断器。你只处理一个用户问题，并且只能输出一个 JSON 对象。
先判断 scope：exhibition、casual 或 invalid；再输出 verdict：yes、no 或 neutral。
展厅、公司、产品、技术、能力、案例和性能问题必须优先使用关联知识库。
展厅 yes/no 必须同时令 basis=knowledge_base 并提供非空 evidence；没有明确知识依据必须 neutral。
普通低风险日常问题可以使用通用能力，basis=general，并尽量明确选择 yes 或 no。
医疗、法律、投资、借贷、博彩、人身安全、危险操作、人事评价和违法行为必须 neutral。
开放式问题、信息不足或无法理解时 scope=invalid、verdict=neutral、basis=none。
固定字段只有 scope、verdict、basis、reason、evidence；禁止 Markdown 和额外文本。
```

补充 XZKB 设置：关联展厅知识库、关闭“仅关联知识库”、关闭图表分析、设置低随机性和短输出；记录如何获得应用 Key，但不记录真实 Key。

- [ ] **Step 2: 增加人工验证表**

至少列出 10 个展厅明确题、10 个展厅无依据题、10 个日常题和 10 个无效/高风险题，每条写出预期 `scope/verdict/basis`，用于上线前验证混合应用不会把知识缺失当成“否”。

- [ ] **Step 3: 更新 README 导航**

在配置和设备功能章节链接 `docs/verdict-application.md`，明确是非判断是独立模式，不属于普通讲解多轮对话。

- [ ] **Step 4: 检查文档无密钥和占位任务**

Run: `rg -n "sk-[A-Za-z0-9]|待补充|待完成|真实.*key" README.md docs/verdict-application.md`

Expected: 没有密钥或未完成项；示例 Key 只使用明显的占位值。

- [ ] **Step 5: 提交文档**

```powershell
git add README.md docs/verdict-application.md
git commit -m "docs: 补充混合判断应用配置"
```

### Task 10：全量回归、真实接口验证和安全整合

**Files:**
- Modify only if tests expose defects in files already in this plan.

- [ ] **Step 1: 检查工作区归属和差异**

Run: `git status --short`

Run: `git diff --check`

Expected: 只有本功能预期文件；没有空白错误。若发现来源不明的改动，停止整合并逐文件确认，不覆盖。

- [ ] **Step 2: 运行 Python 全量测试**

Run: `python -m pytest -q -o filterwarnings=`

Expected: 全部通过；Starlette 1.0 环境继续使用命令行覆盖既存 warning 配置。

- [ ] **Step 3: 运行 JavaScript 和编译检查**

Run: `node --test tests/js/*.test.cjs`

Run: `python -m compileall -q src tests`

Expected: 全部通过，无编译错误。

- [ ] **Step 4: 使用已配置的 XZKB 混合应用做只读验证**

用不含敏感信息的测试问题验证固定 JSON、单次请求、展厅依据不足中立和日常明确判断。日志只记录耗时、枚举和截断后的原因，不记录 Key。

- [ ] **Step 5: 本地测试页验收**

启动测试服务，验证对话模式缓存不受影响、三模式切换、WAV 判断、思考动画、反向快速动作和结果保持。没有实体舵机时只使用模拟器，不伪报 GPIO 验收通过。

- [ ] **Step 6: 处理回归缺陷**

如果全量测试暴露缺陷，回到引入该行为的 Task，在对应测试中先增加可复现用例，再只修改该 Task 已列出的源文件，重新运行该 Task 的定向测试与本 Task 的全量测试；修复随对应功能提交，不创建内容边界不明的汇总提交。

- [ ] **Step 7: 请求代码审查后再决定合并部署**

使用 `superpowers:requesting-code-review` 检查设计符合性、并发取消、硬件故障隔离和现有功能回归。审查与验证通过后，按照 `superpowers:finishing-a-development-branch` 提供合并、保留分支或清理选项；不得自动推送、合并或部署。
