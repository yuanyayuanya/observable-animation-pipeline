(function () {
  "use strict";

  const API_BASE = "/api";
  const POLL_INTERVAL_MS = 1000;
  const BACKOFF_MS = [1000, 2000, 4000, 8000];
  const TERMINAL_TASK_STATES = new Set(["SUCCEEDED", "FAILED", "PAUSED"]);
  const STEP_DEFINITIONS = [
    { key: "validate_input", label: "校验视频" },
    { key: "extract_frames", label: "提取关键帧" },
    { key: "analyze_frames", label: "Mock AI 分析" },
    { key: "build_contact_sheet", label: "生成预览图" },
    { key: "package_result", label: "打包结果" },
  ];
  const STATUS_LABELS = {
    PENDING: "等待中",
    READY: "可执行",
    RUNNING: "运行中",
    RETRY_WAIT: "等待重试",
    SUCCEEDED: "已成功",
    FAILED: "已失败",
    PAUSED: "已暂停",
    SKIPPED: "缓存命中",
    LOST: "Worker 失联",
    TIMED_OUT: "已超时",
  };

  const state = {
    api: null,
    mode: "api",
    tasks: [],
    currentTask: null,
    selectedStepKey: null,
    events: [],
    lastSequence: 0,
    pollTimer: null,
    pollFailures: 0,
    selectionVersion: 0,
  };

  const dom = {};

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  class ApiClient {
    constructor(baseUrl = API_BASE) {
      this.baseUrl = baseUrl;
    }

    async request(path, options = {}) {
      const headers = new Headers(options.headers || {});
      if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }

      const response = await fetch(`${this.baseUrl}${path}`, {
        ...options,
        headers,
      });
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json")
        ? await response.json()
        : await response.text();

      if (!response.ok) {
        const apiMessage = payload && payload.error && payload.error.message;
        const detail = payload && payload.detail;
        throw new ApiError(apiMessage || detail || `请求失败（HTTP ${response.status}）`, response.status, payload);
      }
      return payload;
    }

    listTasks() {
      return this.request("/tasks?limit=50");
    }

    getTask(taskId) {
      return this.request(`/tasks/${encodeURIComponent(taskId)}`);
    }

    getEvents(taskId, afterSequence) {
      return this.request(`/tasks/${encodeURIComponent(taskId)}/events?after_sequence=${afterSequence}`);
    }

    createTask(formData) {
      return this.request("/tasks", { method: "POST", body: formData });
    }

    retryStep(taskId, stepKey, idempotencyKey) {
      return this.request(`/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepKey)}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      });
    }

    rerunTask(taskId, ignoreCache) {
      const query = `?ignore_cache=${ignoreCache ? "true" : "false"}`;
      return this.request(`/tasks/${encodeURIComponent(taskId)}/rerun${query}`, { method: "POST" });
    }

    artifactUrl(artifactId) {
      return `${this.baseUrl}/artifacts/${encodeURIComponent(artifactId)}`;
    }
  }

  class MockApiClient {
    constructor() {
      this.tasks = createMockTasks();
      this.events = createMockEvents();
    }

    async pause() {
      await new Promise((resolve) => window.setTimeout(resolve, 90));
    }

    async listTasks() {
      await this.pause();
      return {
        items: this.tasks.map((task) => ({
          id: task.id,
          status: task.status,
          progress: task.progress,
          filename: task.filename,
          created_at: task.created_at,
          updated_at: task.updated_at,
        })),
        total: this.tasks.length,
        limit: 50,
        offset: 0,
      };
    }

    async getTask(taskId) {
      await this.pause();
      const task = this.tasks.find((item) => item.id === taskId);
      if (!task) {
        throw new ApiError("Mock 任务不存在", 404, null);
      }
      this.advanceRunningTask(task);
      return clone(task);
    }

    async getEvents(taskId, afterSequence) {
      await this.pause();
      const items = (this.events[taskId] || []).filter((event) => event.sequence > afterSequence);
      return { items: clone(items), next_sequence: items.length ? items[items.length - 1].sequence : afterSequence };
    }

    async createTask(formData) {
      await this.pause();
      const file = formData.get("file");
      const taskId = `tsk_mock_${Date.now()}`;
      const now = new Date().toISOString();
      const task = {
        id: taskId,
        status: "PENDING",
        progress: 0,
        filename: file && file.name ? file.name : "new-animation.mp4",
        input_sha256: "mock-upload-sha256",
        created_at: now,
        updated_at: now,
        params: {
          frame_count: Number(formData.get("frame_count")) || 8,
          analysis_profile: formData.get("analysis_profile") || "default",
          fault_plan: formData.get("fault_plan") || "none",
        },
        steps: makePendingSteps(),
        artifacts: [],
      };
      task.steps[0].status = "READY";
      this.tasks.unshift(task);
      this.events[taskId] = [makeEvent(1, taskId, null, "task_created", "Mock 任务已创建，等待 Worker 领取。")];
      return { task_id: taskId, status: "PENDING", created: true, links: { self: `/api/tasks/${taskId}` } };
    }

    async retryStep(taskId, stepKey) {
      await this.pause();
      const task = this.requireTask(taskId);
      const step = task.steps.find((item) => item.key === stepKey);
      if (!step || step.status !== "FAILED") {
        throw new ApiError("该步骤当前不能重试", 409, { error: { code: "STEP_NOT_RETRYABLE" } });
      }
      step.status = "READY";
      step.progress = 0;
      step.last_error = null;
      task.status = "RUNNING";
      task.updated_at = new Date().toISOString();
      this.appendEvent(taskId, stepKey, "manual_retry_requested", "操作员已请求手动重试。", { attempt_count: step.attempt_count });
      return clone(task);
    }

    async rerunTask(taskId, ignoreCache) {
      await this.pause();
      const original = this.requireTask(taskId);
      const newId = `tsk_mock_rerun_${Date.now()}`;
      const now = new Date().toISOString();
      const task = {
        ...clone(original),
        id: newId,
        status: "PENDING",
        progress: 0,
        created_at: now,
        updated_at: now,
        params: { ...original.params, ignore_cache: Boolean(ignoreCache) },
        steps: makePendingSteps(),
        artifacts: [],
      };
      task.steps[0].status = "READY";
      this.tasks.unshift(task);
      this.events[newId] = [makeEvent(1, newId, null, "task_rerun_created", ignoreCache ? "已创建忽略缓存的重跑任务。" : "已创建允许缓存的重跑任务。")];
      return { task_id: newId, status: "PENDING", created: true };
    }

    artifactUrl() {
      return "#mock-artifact";
    }

    requireTask(taskId) {
      const task = this.tasks.find((item) => item.id === taskId);
      if (!task) {
        throw new ApiError("Mock 任务不存在", 404, null);
      }
      return task;
    }

    appendEvent(taskId, stepKey, eventType, message, payload = {}) {
      const taskEvents = this.events[taskId] || [];
      const next = taskEvents.length ? taskEvents[taskEvents.length - 1].sequence + 1 : 1;
      taskEvents.push(makeEvent(next, taskId, stepKey, eventType, message, payload));
      this.events[taskId] = taskEvents;
    }

    advanceRunningTask(task) {
      if (!new Set(["PENDING", "RUNNING"]).has(task.status)) return;
      task.status = "RUNNING";
      const step = task.steps.find((item) => ["READY", "RUNNING", "RETRY_WAIT"].includes(item.status));
      if (!step) return;

      if (step.status === "READY" || step.status === "RETRY_WAIT") {
        step.status = "RUNNING";
        step.progress = 5;
        step.attempt_count += 1;
        step.last_heartbeat_at = new Date().toISOString();
        step.attempts.push({
          id: `att_${task.id}_${step.key}_${step.attempt_count}`,
          attempt_no: step.attempt_count,
          status: "RUNNING",
          worker_id: "worker-mock-01",
          started_at: new Date().toISOString(),
          duration_ms: null,
        });
        this.appendEvent(task.id, step.key, "step_claimed", `${stepLabel(step.key)}已被 Mock Worker 领取。`);
      } else {
        step.progress = Math.min(100, step.progress + 12);
        step.last_heartbeat_at = new Date().toISOString();
        this.appendEvent(task.id, step.key, "progress_changed", `执行进度更新为 ${step.progress}%。`, { progress: step.progress });
        if (step.progress >= 100) {
          step.status = "SUCCEEDED";
          const attempt = step.attempts[step.attempts.length - 1];
          if (attempt) {
            attempt.status = "SUCCEEDED";
            attempt.finished_at = new Date().toISOString();
            attempt.duration_ms = 2300;
          }
          const nextStep = task.steps[step.ordinal];
          if (nextStep) nextStep.status = "READY";
          this.appendEvent(task.id, step.key, "step_succeeded", `${stepLabel(step.key)}执行成功。`);
        }
      }

      task.progress = weightedProgress(task.steps);
      task.updated_at = new Date().toISOString();
      if (task.steps.every((item) => ["SUCCEEDED", "SKIPPED"].includes(item.status))) {
        task.status = "SUCCEEDED";
        task.progress = 100;
      }
    }
  }

  function createMockTasks() {
    const failedTask = {
      id: "tsk_mock_failed_01",
      status: "FAILED",
      progress: 25,
      filename: "episode-01-opening.mp4",
      input_sha256: "mock-failed-input-sha256",
      created_at: "2026-10-07T08:00:00Z",
      updated_at: "2026-10-07T08:01:54Z",
      params: { frame_count: 8, analysis_profile: "default", fault_plan: "analyze_fail_once_at_50" },
      steps: [
        makeStep(1, "validate_input", "SUCCEEDED", 100, 1, "2026-10-07T08:00:12Z", [
          makeAttempt(1, "SUCCEEDED", "worker-local-01", "2026-10-07T08:00:08Z", 4200),
        ]),
        makeStep(2, "extract_frames", "SUCCEEDED", 100, 1, "2026-10-07T08:00:38Z", [
          makeAttempt(1, "SUCCEEDED", "worker-local-01", "2026-10-07T08:00:14Z", 24000),
        ]),
        {
          ...makeStep(3, "analyze_frames", "FAILED", 50, 2, "2026-10-07T08:01:53Z", [
            { ...makeAttempt(1, "LOST", "worker-local-01", "2026-10-07T08:00:40Z", 19000), error_type: "WorkerLost", error_message: "心跳超过 15 秒未更新" },
            { ...makeAttempt(2, "FAILED", "worker-local-02", "2026-10-07T08:01:10Z", 43000), error_type: "InjectedTransientError", error_message: "故障计划要求在 50% 处失败" },
          ]),
          max_attempts: 3,
          last_error: { type: "InjectedTransientError", message: "故障计划要求在 50% 处失败；自动重试额度已耗尽。" },
        },
        makeStep(4, "build_contact_sheet", "PENDING", 0, 0, null, []),
        makeStep(5, "package_result", "PENDING", 0, 0, null, []),
      ],
      artifacts: [
        makeArtifact("art_metadata_01", "input_metadata.json", "validate_input", "application/json", 1840, "73f18b64511a"),
        makeArtifact("art_frame_01", "frames/frame-001.jpg", "extract_frames", "image/jpeg", 184320, "044ac3b989bc"),
        makeArtifact("art_frame_02", "frames/frame-002.jpg", "extract_frames", "image/jpeg", 176128, "e27df7cda02f"),
      ],
    };

    const runningTask = {
      id: "tsk_mock_running_02",
      status: "RUNNING",
      progress: 53,
      filename: "character-turnaround.mov",
      input_sha256: "mock-running-input-sha256",
      created_at: "2026-10-07T09:14:00Z",
      updated_at: "2026-10-07T09:15:12Z",
      params: { frame_count: 8, analysis_profile: "detailed", fault_plan: "none" },
      steps: [
        makeStep(1, "validate_input", "SKIPPED", 100, 0, null, []),
        makeStep(2, "extract_frames", "SUCCEEDED", 100, 1, "2026-10-07T09:14:32Z", [makeAttempt(1, "SUCCEEDED", "worker-local-01", "2026-10-07T09:14:08Z", 24000)]),
        makeStep(3, "analyze_frames", "RUNNING", 62, 1, "2026-10-07T09:15:12Z", [makeAttempt(1, "RUNNING", "worker-local-01", "2026-10-07T09:14:34Z", null)]),
        makeStep(4, "build_contact_sheet", "PENDING", 0, 0, null, []),
        makeStep(5, "package_result", "PENDING", 0, 0, null, []),
      ],
      artifacts: [makeArtifact("art_metadata_02", "input_metadata.json", "validate_input", "application/json", 1712, "8b17b4c0f2d1")],
    };

    const successTask = {
      id: "tsk_mock_success_03",
      status: "SUCCEEDED",
      progress: 100,
      filename: "short-scene.mp4",
      input_sha256: "mock-success-input-sha256",
      created_at: "2026-10-06T13:22:00Z",
      updated_at: "2026-10-06T13:25:41Z",
      params: { frame_count: 8, analysis_profile: "fast", fault_plan: "none" },
      steps: STEP_DEFINITIONS.map((definition, index) => makeStep(index + 1, definition.key, index === 0 ? "SKIPPED" : "SUCCEEDED", 100, index === 0 ? 0 : 1, "2026-10-06T13:25:38Z", index === 0 ? [] : [makeAttempt(1, "SUCCEEDED", "worker-local-01", "2026-10-06T13:22:10Z", 18000 + index * 900)])),
      artifacts: [
        makeArtifact("art_metadata_03", "input_metadata.json", "validate_input", "application/json", 1650, "d819ab8a111a"),
        makeArtifact("art_analysis_03", "analysis.json", "analyze_frames", "application/json", 9942, "a918bcd73241"),
        makeArtifact("art_sheet_03", "contact-sheet.jpg", "build_contact_sheet", "image/jpeg", 824128, "bb712ea16c90"),
        makeArtifact("art_result_03", "result.zip", "package_result", "application/zip", 3561472, "09f16d087cc1"),
      ],
    };
    return [failedTask, runningTask, successTask];
  }

  function createMockEvents() {
    return {
      tsk_mock_failed_01: [
        makeEvent(1, "tsk_mock_failed_01", null, "task_created", "任务已创建，输入文件保存完成。"),
        makeEvent(2, "tsk_mock_failed_01", "validate_input", "step_succeeded", "格式、时长与文件哈希校验通过。"),
        makeEvent(3, "tsk_mock_failed_01", "extract_frames", "artifact_committed", "8 张关键帧已原子提交。", { artifact_count: 8 }),
        makeEvent(4, "tsk_mock_failed_01", "analyze_frames", "step_claimed", "worker-local-01 已领取 AI 分析步骤。"),
        makeEvent(5, "tsk_mock_failed_01", "analyze_frames", "lease_expired", "Worker 心跳超时，attempt #1 标记为 LOST。"),
        makeEvent(6, "tsk_mock_failed_01", "analyze_frames", "retry_scheduled", "1 秒退避结束，开始 attempt #2。"),
        makeEvent(7, "tsk_mock_failed_01", "analyze_frames", "progress_changed", "已分析 4/8 张关键帧，进度 50%。", { progress: 50 }),
        makeEvent(8, "tsk_mock_failed_01", "analyze_frames", "step_failed", "注入故障触发，自动重试额度已耗尽。"),
      ],
      tsk_mock_running_02: [
        makeEvent(1, "tsk_mock_running_02", null, "task_created", "任务已创建。"),
        makeEvent(2, "tsk_mock_running_02", "validate_input", "cache_hit", "输入校验产物命中缓存。"),
        makeEvent(3, "tsk_mock_running_02", "extract_frames", "step_succeeded", "关键帧提取完成。"),
        makeEvent(4, "tsk_mock_running_02", "analyze_frames", "step_claimed", "已获取 Mock GPU 资源 gpu:0。"),
      ],
      tsk_mock_success_03: [
        makeEvent(1, "tsk_mock_success_03", null, "task_created", "任务已创建。"),
        makeEvent(2, "tsk_mock_success_03", "package_result", "artifact_committed", "result.zip 已提交。"),
        makeEvent(3, "tsk_mock_success_03", null, "task_succeeded", "全部五个步骤已完成。"),
      ],
    };
  }

  function makePendingSteps() {
    return STEP_DEFINITIONS.map((definition, index) => makeStep(index + 1, definition.key, "PENDING", 0, 0, null, []));
  }

  function makeStep(ordinal, key, status, progress, attemptCount, heartbeat, attempts) {
    return {
      id: `stp_${key}`,
      key,
      ordinal,
      status,
      progress,
      attempt_count: attemptCount,
      max_attempts: key === "analyze_frames" ? 3 : 2,
      last_heartbeat_at: heartbeat,
      last_error: null,
      attempts,
    };
  }

  function makeAttempt(attemptNo, status, workerId, startedAt, durationMs) {
    return {
      id: `att_mock_${attemptNo}_${workerId}`,
      attempt_no: attemptNo,
      status,
      worker_id: workerId,
      started_at: startedAt,
      duration_ms: durationMs,
    };
  }

  function makeArtifact(id, name, stepKey, mediaType, sizeBytes, sha256) {
    return {
      id,
      step_id: `stp_${stepKey}`,
      kind: name,
      media_type: mediaType,
      sha256,
      size_bytes: sizeBytes,
      download_url: `/api/artifacts/${id}`,
      metadata: { display_name: name },
      created_at: new Date().toISOString(),
    };
  }

  function makeEvent(sequence, taskId, stepKey, eventType, message, payload = {}) {
    return {
      sequence,
      task_id: taskId,
      step_id: stepKey ? `stp_${stepKey}` : null,
      event_type: eventType,
      payload: { message, ...payload },
      created_at: new Date(Date.now() - (12 - sequence) * 5000).toISOString(),
    };
  }

  function cacheDom() {
    [
      "connectionDot", "connectionMode", "devModeBanner", "retryConnectionButton", "globalMessage",
      "newTaskForm", "taskFile", "frameCount", "analysisProfile", "faultPlan", "createTaskButton", "formMessage",
      "refreshTasksButton", "taskList", "taskListEmpty", "taskCount", "noTaskState", "taskDetail", "taskId",
      "taskTitle", "taskStatus", "taskProgressValue", "taskProgressBar", "taskCreatedAt", "taskUpdatedAt",
      "taskCurrentStep", "taskParams", "rerunTaskButton", "rerunNoCacheButton", "pollingState",
      "stepTimeline", "selectedStepKey", "stepInspectorTitle", "retryStepButton", "selectedStepStatus",
      "selectedStepProgress", "selectedStepAttempts", "selectedStepHeartbeat", "stepErrorPanel", "stepErrorType",
      "stepErrorMessage", "attemptTableBody", "attemptEmpty", "eventSequence", "logOutput", "artifactCount",
      "artifactTableBody", "artifactEmpty", "confirmDialog", "confirmDialogTitle", "confirmDialogMessage",
      "confirmDialogAction",
    ].forEach((id) => {
      dom[id] = document.getElementById(id);
    });
  }

  function bindEvents() {
    dom.newTaskForm.addEventListener("submit", handleCreateTask);
    dom.refreshTasksButton.addEventListener("click", () => loadTaskList({ preserveSelection: true }));
    dom.retryConnectionButton.addEventListener("click", reconnectApi);
    dom.retryStepButton.addEventListener("click", handleRetryStep);
    dom.rerunTaskButton.addEventListener("click", () => requestRerun(false));
    dom.rerunNoCacheButton.addEventListener("click", () => requestRerun(true));
    window.addEventListener("popstate", handleUrlSelection);
  }

  async function bootstrap() {
    cacheDom();
    bindEvents();
    state.api = new ApiClient();
    setConnection("connecting", "正在连接 API");
    try {
      await loadTaskList({ preserveSelection: false, allowFallback: true });
      setConnection("online", "API 已连接");
    } catch (error) {
      activateMockMode(error);
      await loadTaskList({ preserveSelection: false });
    }
  }

  async function reconnectApi() {
    clearPolling();
    state.api = new ApiClient();
    state.mode = "api";
    state.events = [];
    state.lastSequence = 0;
    dom.devModeBanner.hidden = true;
    setConnection("connecting", "正在重新连接 API");
    try {
      await loadTaskList({ preserveSelection: true, allowFallback: true });
      setConnection("online", "API 已连接");
      showGlobalMessage("");
    } catch (error) {
      activateMockMode(error);
      await loadTaskList({ preserveSelection: false });
    }
  }

  function activateMockMode(error) {
    state.mode = "mock";
    state.api = new MockApiClient();
    state.events = [];
    state.lastSequence = 0;
    dom.devModeBanner.hidden = false;
    setConnection("offline", "Mock 数据");
    const reason = error instanceof Error ? error.message : "后端连接失败";
    dom.devModeBanner.querySelector("span").textContent = `后端暂不可用（${reason}），当前展示内置 Mock 数据；操作不会写入数据库。`;
  }

  async function loadTaskList(options = {}) {
    const { preserveSelection = true } = options;
    dom.refreshTasksButton.disabled = true;
    try {
      const response = await state.api.listTasks();
      state.tasks = normalizeTaskList(response);
      renderTaskList();
      const urlTaskId = new URLSearchParams(window.location.search).get("task_id");
      const existingId = preserveSelection && state.currentTask ? state.currentTask.id : null;
      const targetId = urlTaskId
        || (existingId && state.tasks.some((task) => task.id === existingId) ? existingId : null)
        || (state.tasks[0] && state.tasks[0].id);
      if (targetId) {
        await selectTask(targetId, { updateUrl: !urlTaskId });
      } else {
        showEmptyState();
      }
    } finally {
      dom.refreshTasksButton.disabled = false;
    }
  }

  function normalizeTaskList(response) {
    const items = Array.isArray(response) ? response : (response && (response.items || response.tasks)) || [];
    return items.map((task) => ({
      ...task,
      id: task.id || task.task_id,
      filename: task.filename || task.input_filename,
      progress: clampProgress(task.progress),
    }));
  }

  async function selectTask(taskId, options = {}) {
    const { updateUrl = true } = options;
    const selectionVersion = ++state.selectionVersion;
    clearPolling();
    state.events = [];
    state.lastSequence = 0;
    showGlobalMessage("");
    dom.pollingState.textContent = "正在读取任务";
    if (updateUrl) setTaskIdInUrl(taskId);
    renderTaskList(taskId);

    try {
      const [taskResponse, eventResponse] = await Promise.all([
        state.api.getTask(taskId),
        state.api.getEvents(taskId, 0),
      ]);
      if (selectionVersion !== state.selectionVersion) return;
      state.currentTask = normalizeTask(taskResponse);
      applyEvents(eventResponse, true);
      chooseDefaultStep();
      updateTaskSummary(state.currentTask);
      renderTaskDetail();
      schedulePolling();
    } catch (error) {
      if (selectionVersion !== state.selectionVersion) return;
      showGlobalMessage(userErrorMessage(error));
      dom.pollingState.textContent = "读取失败";
    }
  }

  function normalizeTask(response) {
    const task = response && response.task ? response.task : response;
    const steps = (Array.isArray(task.steps) ? task.steps : []).map((step, index) => ({
      ...step,
      key: step.key || step.step_key,
      ordinal: step.ordinal || index + 1,
      progress: clampProgress(step.progress),
      attempt_count: Number(step.attempt_count || 0),
      max_attempts: Number(step.max_attempts || 0),
      attempts: Array.isArray(step.attempts) ? step.attempts : [],
    }));
    const stepKeysById = new Map(steps.map((step) => [step.id, step.key]));
    return {
      ...task,
      id: task.id || task.task_id,
      filename: task.filename || task.input_filename,
      progress: clampProgress(task.progress),
      params: task.params || task.parameters || task.params_json || {},
      steps,
      artifacts: (Array.isArray(task.artifacts) ? task.artifacts : []).map((artifact) => ({
        ...artifact,
        step_key: artifact.step_key || stepKeysById.get(artifact.step_id) || null,
      })),
    };
  }

  function applyEvents(response, replace) {
    const items = Array.isArray(response) ? response : (response && (response.items || response.events)) || [];
    const stepKeysById = new Map((state.currentTask && state.currentTask.steps || []).map((step) => [step.id, step.key]));
    const normalized = items.map((event) => ({
      ...event,
      step_key: event.step_key || stepKeysById.get(event.step_id) || null,
      message: event.message || (event.payload && (event.payload.message || event.payload.detail)) || null,
    }));
    const combined = replace ? normalized : state.events.concat(normalized);
    state.events = Array.from(new Map(combined.map((event) => [event.sequence, event])).values())
      .sort((a, b) => Number(a.sequence) - Number(b.sequence));
    if (items.length) {
      state.lastSequence = Math.max(...normalized.map((event) => Number(event.sequence || 0)), state.lastSequence);
    } else if (response && response.next_sequence !== undefined) {
      state.lastSequence = Number(response.next_sequence);
    }
  }

  function chooseDefaultStep() {
    const steps = state.currentTask.steps;
    const currentStillExists = steps.some((step) => step.key === state.selectedStepKey);
    if (currentStillExists) return;
    const preferred = steps.find((step) => ["FAILED", "RUNNING", "RETRY_WAIT", "READY"].includes(step.status));
    state.selectedStepKey = (preferred || steps[0] || {}).key || null;
  }

  function renderTaskList(selectedId) {
    dom.taskList.replaceChildren();
    dom.taskCount.textContent = `${state.tasks.length} 项`;
    dom.taskListEmpty.hidden = state.tasks.length > 0;
    const activeId = selectedId || (state.currentTask && state.currentTask.id);

    state.tasks.forEach((task) => {
      const button = element("button", "task-list-item");
      button.type = "button";
      button.dataset.taskId = task.id;
      button.setAttribute("aria-current", String(task.id === activeId));
      button.addEventListener("click", () => selectTask(task.id));

      const name = element("span", "task-list-name", task.filename || `任务 ${shortId(task.id)}`);
      const status = element("span", "mini-status", statusLabel(task.status));
      status.dataset.status = task.status;
      const meta = element("span", "task-list-meta", `${clampProgress(task.progress)}% · ${formatDateTime(task.updated_at || task.created_at)}`);
      button.append(name, status, meta);
      dom.taskList.append(button);
    });
  }

  function showEmptyState() {
    clearPolling();
    state.currentTask = null;
    dom.noTaskState.hidden = false;
    dom.taskDetail.hidden = true;
  }

  function renderTaskDetail() {
    const task = state.currentTask;
    if (!task) return showEmptyState();
    dom.noTaskState.hidden = true;
    dom.taskDetail.hidden = false;
    dom.taskId.textContent = task.id;
    dom.taskTitle.textContent = task.filename || `任务 ${shortId(task.id)}`;
    setStatusBadge(dom.taskStatus, task.status);
    dom.taskProgressValue.textContent = `${task.progress}%`;
    dom.taskProgressBar.style.width = `${task.progress}%`;
    const progressTrack = dom.taskProgressBar.parentElement;
    progressTrack.setAttribute("aria-valuenow", String(task.progress));
    dom.taskCreatedAt.textContent = formatDateTime(task.created_at);
    dom.taskUpdatedAt.textContent = formatDateTime(task.updated_at);
    const currentStep = task.steps.find((step) => ["RUNNING", "RETRY_WAIT", "READY", "FAILED"].includes(step.status));
    dom.taskCurrentStep.textContent = currentStep ? stepLabel(currentStep.key) : task.status === "SUCCEEDED" ? "全部完成" : "-";
    dom.taskParams.textContent = formatParams(task.params);
    dom.rerunTaskButton.disabled = false;
    dom.rerunNoCacheButton.disabled = false;

    renderTimeline();
    renderStepInspector();
    renderArtifacts();
    renderTaskList(task.id);
  }

  function renderTimeline() {
    dom.stepTimeline.replaceChildren();
    state.currentTask.steps
      .slice()
      .sort((a, b) => a.ordinal - b.ordinal)
      .forEach((step) => {
        const item = document.createElement("li");
        const button = element("button", "step-row");
        button.type = "button";
        button.dataset.stepKey = step.key;
        button.setAttribute("aria-current", String(step.key === state.selectedStepKey));
        button.setAttribute("aria-label", `${stepLabel(step.key)}，${statusLabel(step.status)}，进度 ${step.progress}%`);
        button.addEventListener("click", () => {
          state.selectedStepKey = step.key;
          renderTimeline();
          renderStepInspector();
        });

        const number = element("span", "step-number", String(step.ordinal));
        const name = element("span", "step-name", stepLabel(step.key));
        const meta = element("span", "step-meta", `${statusLabel(step.status)} · ${step.attempt_count}/${step.max_attempts || "-"} 次`);
        const miniProgress = element("span", "step-mini-progress");
        const miniFill = document.createElement("span");
        miniFill.style.width = `${step.progress}%`;
        miniProgress.append(miniFill);
        button.append(number, name, meta, miniProgress);
        item.append(button);
        dom.stepTimeline.append(item);
      });
  }

  function renderStepInspector() {
    const step = state.currentTask.steps.find((item) => item.key === state.selectedStepKey);
    if (!step) return;
    dom.selectedStepKey.textContent = step.key;
    dom.stepInspectorTitle.textContent = stepLabel(step.key);
    dom.selectedStepStatus.textContent = statusLabel(step.status);
    dom.selectedStepProgress.textContent = `${step.progress}%`;
    dom.selectedStepAttempts.textContent = `${step.attempt_count} / ${step.max_attempts || "-"}`;
    dom.selectedStepHeartbeat.textContent = formatDateTime(step.last_heartbeat_at);
    dom.retryStepButton.disabled = step.status !== "FAILED";
    dom.retryStepButton.dataset.stepKey = step.key;

    const error = step.last_error || latestAttemptError(step.attempts);
    dom.stepErrorPanel.hidden = !error;
    if (error) {
      const isTextError = typeof error === "string";
      dom.stepErrorType.textContent = isTextError ? "步骤执行失败" : error.type || error.error_type || "步骤执行失败";
      dom.stepErrorMessage.textContent = isTextError ? error : error.message || error.error_message || "未提供错误摘要。";
    }
    renderAttempts(step.attempts);
    renderLogs(step.key);
  }

  function renderAttempts(attempts) {
    dom.attemptTableBody.replaceChildren();
    dom.attemptEmpty.hidden = attempts.length > 0;
    attempts.forEach((attempt) => {
      const row = document.createElement("tr");
      appendCell(row, `#${attempt.attempt_no}`);
      appendCell(row, statusLabel(attempt.status));
      appendCell(row, attempt.worker_id || "-");
      appendCell(row, formatDateTime(attempt.started_at || attempt.created_at));
      appendCell(row, formatDuration(attempt.duration_ms, attempt.started_at, attempt.finished_at));
      dom.attemptTableBody.append(row);
    });
  }

  function renderLogs(stepKey) {
    const relevant = state.events.filter((event) => !event.step_key || event.step_key === stepKey);
    dom.eventSequence.textContent = `sequence: ${state.lastSequence || "-"}`;
    dom.logOutput.textContent = relevant.length
      ? relevant.map(formatEventLine).join("\n")
      : "暂无日志。等待 Worker 写入事件。";
    dom.logOutput.scrollTop = dom.logOutput.scrollHeight;
  }

  function renderArtifacts() {
    const artifacts = state.currentTask.artifacts;
    dom.artifactTableBody.replaceChildren();
    dom.artifactCount.textContent = `${artifacts.length} 项`;
    dom.artifactEmpty.hidden = artifacts.length > 0;

    artifacts.forEach((artifact) => {
      const row = document.createElement("tr");
      appendCell(row, artifact.name || artifact.kind || artifact.path || "未命名产物");
      appendCell(row, stepLabel(artifact.step_key));
      appendCell(row, formatBytes(artifact.size_bytes));
      appendCell(row, shortHash(artifact.sha256));
      const actionCell = document.createElement("td");
      const link = element("a", "artifact-link", state.mode === "mock" ? "预览" : "下载");
      link.href = artifact.download_url || state.api.artifactUrl(artifact.id);
      if (state.mode !== "mock") {
        link.download = artifact.name || artifact.kind || "artifact";
      } else {
        link.addEventListener("click", (event) => {
          event.preventDefault();
          showGlobalMessage("开发模式不提供真实产物下载。连接后端后可使用此入口。", "info");
        });
      }
      actionCell.append(link);
      row.append(actionCell);
      dom.artifactTableBody.append(row);
    });
  }

  function schedulePolling() {
    clearPolling();
    const task = state.currentTask;
    if (!task || TERMINAL_TASK_STATES.has(task.status)) {
      dom.pollingState.textContent = "终态，已停止轮询";
      return;
    }
    const delay = state.pollFailures ? BACKOFF_MS[Math.min(state.pollFailures - 1, BACKOFF_MS.length - 1)] : POLL_INTERVAL_MS;
    dom.pollingState.textContent = state.pollFailures ? `网络异常，${delay / 1000} 秒后重试` : "每 1 秒自动刷新";
    const taskId = task.id;
    dom.pollingState.dataset.delay = String(delay);
    state.pollTimer = window.setTimeout(() => pollTask(taskId), delay);
  }

  async function pollTask(taskId) {
    if (!state.currentTask || state.currentTask.id !== taskId) return;
    try {
      const [taskResponse, eventResponse] = await Promise.all([
        state.api.getTask(taskId),
        state.api.getEvents(taskId, state.lastSequence),
      ]);
      if (!state.currentTask || state.currentTask.id !== taskId) return;
      state.currentTask = normalizeTask(taskResponse);
      applyEvents(eventResponse, false);
      state.pollFailures = 0;
      setConnection(state.mode === "mock" ? "offline" : "online", state.mode === "mock" ? "Mock 数据" : "API 已连接");
      chooseDefaultStep();
      updateTaskSummary(state.currentTask);
      renderTaskDetail();
      showGlobalMessage("");
      schedulePolling();
    } catch (error) {
      state.pollFailures += 1;
      setConnection("offline", "连接不稳定");
      showGlobalMessage(`刷新失败，已保留最后一次成功数据。${userErrorMessage(error)}`, "warning");
      schedulePolling();
    }
  }

  function clearPolling() {
    if (state.pollTimer !== null) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function handleCreateTask(event) {
    event.preventDefault();
    const file = dom.taskFile.files[0];
    if (!file) return setFormMessage("请选择 MP4 或 MOV 文件。", "error");
    if (file.size > 50 * 1024 * 1024) return setFormMessage("文件超过 50 MB 限制。", "error");

    const extension = file.name.split(".").pop().toLowerCase();
    if (!["mp4", "mov"].includes(extension)) return setFormMessage("仅支持 MP4 或 MOV 文件。", "error");

    const formData = new FormData();
    formData.set("file", file);
    formData.set("frame_count", dom.frameCount.value);
    formData.set("analysis_profile", dom.analysisProfile.value);
    formData.set("fault_plan", dom.faultPlan.value);
    formData.set("idempotency_key", createIdempotencyKey("create"));
    dom.createTaskButton.disabled = true;
    dom.newTaskForm.setAttribute("aria-busy", "true");
    setFormMessage("正在创建任务…", "info");
    try {
      const response = await state.api.createTask(formData);
      const taskId = response.task_id || response.id;
      setFormMessage(state.mode === "mock" ? "Mock 任务已创建。" : "任务已创建。", "success");
      dom.newTaskForm.reset();
      dom.frameCount.value = "8";
      await loadTaskList({ preserveSelection: false });
      if (taskId) await selectTask(taskId);
    } catch (error) {
      setFormMessage(userErrorMessage(error), "error");
    } finally {
      dom.createTaskButton.disabled = false;
      dom.newTaskForm.removeAttribute("aria-busy");
    }
  }

  async function handleRetryStep() {
    const task = state.currentTask;
    const stepKey = dom.retryStepButton.dataset.stepKey;
    if (!task || !stepKey) return;
    dom.retryStepButton.disabled = true;
    try {
      await state.api.retryStep(task.id, stepKey, createIdempotencyKey("retry"));
      await refreshSelectedTask();
      showGlobalMessage("重试请求已提交。", "success");
    } catch (error) {
      showGlobalMessage(userErrorMessage(error));
      renderStepInspector();
    }
  }

  async function requestRerun(ignoreCache) {
    if (!state.currentTask) return;
    const confirmed = await confirmOperation(
      ignoreCache ? "忽略缓存重跑" : "重新运行任务",
      ignoreCache
        ? "所有步骤都会重新计算，已有有效缓存不会被复用。"
        : "将创建一个新任务，并复用校验通过的已有产物。",
      ignoreCache ? "忽略缓存并重跑" : "创建重跑任务",
    );
    if (!confirmed) return;
    try {
      const response = await state.api.rerunTask(state.currentTask.id, ignoreCache);
      const taskId = response.task_id || response.id;
      await loadTaskList({ preserveSelection: false });
      if (taskId) await selectTask(taskId);
      showGlobalMessage("重跑任务已创建。", "success");
    } catch (error) {
      showGlobalMessage(userErrorMessage(error));
    }
  }

  async function refreshSelectedTask() {
    if (!state.currentTask) return;
    const taskId = state.currentTask.id;
    const [taskResponse, eventResponse] = await Promise.all([
      state.api.getTask(taskId),
      state.api.getEvents(taskId, state.lastSequence),
    ]);
    state.currentTask = normalizeTask(taskResponse);
    applyEvents(eventResponse, false);
    state.pollFailures = 0;
    updateTaskSummary(state.currentTask);
    renderTaskDetail();
    schedulePolling();
  }

  function handleUrlSelection() {
    const taskId = new URLSearchParams(window.location.search).get("task_id");
    if (taskId && (!state.currentTask || state.currentTask.id !== taskId)) {
      selectTask(taskId, { updateUrl: false });
    }
  }

  function updateTaskSummary(task) {
    const index = state.tasks.findIndex((item) => item.id === task.id);
    const summary = {
      id: task.id,
      status: task.status,
      progress: task.progress,
      filename: task.filename || task.input_filename,
      created_at: task.created_at,
      updated_at: task.updated_at,
    };
    if (index >= 0) state.tasks[index] = { ...state.tasks[index], ...summary };
    else state.tasks.unshift(summary);
  }

  function setTaskIdInUrl(taskId) {
    const url = new URL(window.location.href);
    url.searchParams.set("task_id", taskId);
    window.history.pushState({ taskId }, "", url);
  }

  function confirmOperation(title, message, actionLabel) {
    if (typeof dom.confirmDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(`${title}\n\n${message}`));
    }
    dom.confirmDialogTitle.textContent = title;
    dom.confirmDialogMessage.textContent = message;
    dom.confirmDialogAction.textContent = actionLabel;
    dom.confirmDialog.showModal();
    return new Promise((resolve) => {
      dom.confirmDialog.addEventListener("close", () => resolve(dom.confirmDialog.returnValue === "confirm"), { once: true });
    });
  }

  function setConnection(status, label) {
    dom.connectionDot.dataset.state = status === "online" ? "online" : status === "offline" ? "offline" : "connecting";
    dom.connectionMode.textContent = label;
  }

  function showGlobalMessage(message, type = "error") {
    dom.globalMessage.hidden = !message;
    dom.globalMessage.textContent = message || "";
    dom.globalMessage.dataset.state = type;
    dom.globalMessage.setAttribute("role", type === "error" || type === "warning" ? "alert" : "status");
    dom.globalMessage.setAttribute("aria-live", type === "error" || type === "warning" ? "assertive" : "polite");
  }

  function setFormMessage(message, type) {
    dom.formMessage.textContent = message;
    dom.formMessage.dataset.state = type;
  }

  function setStatusBadge(target, status) {
    target.textContent = statusLabel(status);
    target.dataset.status = status || "PENDING";
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "未知";
  }

  function stepLabel(key) {
    const definition = STEP_DEFINITIONS.find((item) => item.key === key);
    return definition ? definition.label : key || "-";
  }

  function weightedProgress(steps) {
    const weights = { validate_input: 5, extract_frames: 20, analyze_frames: 45, build_contact_sheet: 20, package_result: 10 };
    return Math.round(steps.reduce((total, step) => total + ((weights[step.key] || 0) * clampProgress(step.progress)) / 100, 0));
  }

  function clampProgress(value) {
    return Math.max(0, Math.min(100, Number(value) || 0));
  }

  function formatDateTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).format(date);
  }

  function formatDuration(durationMs, startedAt, finishedAt) {
    let milliseconds = Number(durationMs);
    if (!Number.isFinite(milliseconds) && startedAt && finishedAt) {
      milliseconds = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
    }
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "运行中";
    if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
    return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} 秒`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value)) return "-";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatParams(params) {
    const frameCount = params.frame_count ?? "-";
    const profile = params.analysis_profile ?? "-";
    const fault = params.fault_plan && params.fault_plan !== "none" ? params.fault_plan : "无故障";
    return `${frameCount} 帧 · ${profile} · ${fault}`;
  }

  function formatEventLine(event) {
    const time = event.created_at ? new Date(event.created_at).toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
    const sequence = String(event.sequence || "-").padStart(4, "0");
    const type = event.event_type || "event";
    const message = event.message || (event.payload && event.payload.message) || JSON.stringify(event.payload || {});
    return `${sequence}  ${time}  ${type}  ${message}`;
  }

  function latestAttemptError(attempts) {
    const reversed = attempts.slice().reverse();
    return reversed.find((attempt) => attempt.error_type || attempt.error_message) || null;
  }

  function shortHash(value) {
    if (!value) return "-";
    return `${String(value).slice(0, 12)}…`;
  }

  function shortId(value) {
    return value ? String(value).slice(-10) : "-";
  }

  function createIdempotencyKey(prefix) {
    const suffix = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${suffix}`;
  }

  function userErrorMessage(error) {
    if (error instanceof ApiError) return error.message;
    if (error instanceof TypeError) return "无法连接后端服务。";
    return error && error.message ? error.message : "发生未知错误。";
  }

  function appendCell(row, value) {
    const cell = document.createElement("td");
    cell.textContent = value;
    row.append(cell);
  }

  function element(tagName, className, textContent) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (textContent !== undefined) node.textContent = textContent;
    return node;
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
