// 确保 DOM 加载完毕后执行
document.addEventListener("DOMContentLoaded", () => {

    // --- 1. DOM 元素获取 ---
    const loginOverlay = document.getElementById("login-overlay");
    const loginButton = document.getElementById("login-button");
    const adminKeyInput = document.getElementById("admin-key-input");
    const loginError = document.getElementById("login-error");

    const topBar = document.getElementById("top-bar");
    const appContainer = document.getElementById("app-container");
    const logoutButton = document.getElementById("logout-button");

    const tabs = document.querySelectorAll(".tab-button");
    const tabContents = document.querySelectorAll(".tab-content");

    // 配置 Tab
    const configSchemesContainer = document.getElementById("config-schemes-container");
    const configForm = document.getElementById("config-form");
    const formTitle = document.getElementById("form-title");
    const configIdInput = document.getElementById("config-id");
    const configSchemeInput = document.getElementById("config-scheme");
    const configPriorityInput = document.getElementById("config-priority");
    const configUrlInput = document.getElementById("config-url");
    const configKeyInput = document.getElementById("config-key");
    const configModelInput = document.getElementById("config-model");
    const queryModelsButton = document.getElementById("query-models-button");
    const modelPickerSelect = document.getElementById("model-picker-select");
    const modelQueryStatus = document.getElementById("model-query-status");
    const configUserAgentModeInput = document.getElementById("config-user-agent-mode");
    const configCustomUserAgentInput = document.getElementById("config-custom-user-agent");
    const configFailureThresholdInput = document.getElementById("config-failure-threshold");
    const configDisableDurationInput = document.getElementById("config-disable-duration");
    const configMaxRetriesInput = document.getElementById("config-max-retries");
    const configRequestOverridesInput = document.getElementById("config-request-overrides");
    const configInjectionPositionInput = document.getElementById("config-injection-position");
    const configStreamModeStrategyInput = document.getElementById("config-stream-mode-strategy");
    const injectedMessagesEditor = document.getElementById("injected-messages-editor");
    const addInjectedMessageButton = document.getElementById("add-injected-message-button");
    const presetTableBody = document.getElementById("preset-table-body");
    const saveButton = document.getElementById("save-button");
    const cancelButton = document.getElementById("cancel-button");

    // 统计 Tab
    const statTotalSuccess = document.getElementById("stat-total-success");
    const statTotalFail = document.getElementById("stat-total-fail");
    const statTodaySuccess = document.getElementById("stat-today-success");
    const statTodayFail = document.getElementById("stat-today-fail");
    const statsByConfigBody = document.getElementById("stats-by-config-body");
    const statsTodayByConfigBody = document.getElementById("stats-today-by-config-body"); // 新增

    // 日志 Tab
    const logsContent = document.getElementById("logs-content");
    const toggleFullRequestLogCheckbox = document.getElementById("toggle-full-request-log");
    const fullRequestLogStatus = document.getElementById("full-request-log-status");

    let adminKey = sessionStorage.getItem("catfishAdminKey");
    let statsInterval, logsInterval;
    let allSchemesCache = {}; // 缓存配置数据，用于统计显示
    let isSyncingLogToggle = false;

    const CONFIG_COLLAPSE_STORAGE_KEY = "catfish_config_scheme_collapsed";
    const ALLOWED_INJECT_ROLES = ["system", "user", "assistant", "tool"];

    const INJECTION_POSITION_LABEL_MAP = {
        prepend: "最前",
        append: "最后"
    };

    const USER_AGENT_MODE_LABEL_MAP = {
        aggregator: "聚合器 UA",
        external: "外部应用 UA",
        claude_code: "Claude Code UA",
        sillytavern: "SillyTavern UA",
        custom: "自定义 UA"
    };

    const STREAM_STRATEGY_LABEL_MAP = {
        passthrough: "不变动（透传）",
        force_fake_non_stream: "假非流",
        force_fake_stream: "假流式"
    };

    const PARAMETER_FIELD_PRESETS = [
        { key: "temperature", type: "number", description: "采样温度，越高越发散", defaultValue: null },
        { key: "top_p", type: "number", description: "核采样阈值 (0~1)", defaultValue: null },
        { key: "max_tokens", type: "integer", description: "最大输出 token 数", defaultValue: null },
        { key: "presence_penalty", type: "number", description: "存在惩罚", defaultValue: null },
        { key: "frequency_penalty", type: "number", description: "频率惩罚", defaultValue: null },
        { key: "reasoning_effort", type: "string", description: "思维强度，如 low/medium/high", defaultValue: null },
        { key: "reasoning", type: "object", description: "推理配置对象，如 { effort: \"high\" }", defaultValue: null },
        { key: "stream_options", type: "object", description: "流式附加参数，如 { include_usage: true }", defaultValue: null },
        { key: "response_format", type: "object", description: "输出格式控制", defaultValue: null },
        { key: "seed", type: "integer", description: "随机种子", defaultValue: null },
        { key: "tools", type: "array", description: "工具调用定义数组", defaultValue: null },
        { key: "tool_choice", type: "string", description: "工具选择策略", defaultValue: null }
    ];

    // --- 2. 核心功能函数 ---

    async function authedFetch(url, options = {}) {
        if (!adminKey) {
            console.error("No admin key found");
            showLogin("会话已过期，请重新登录");
            return;
        }
        const headers = { ...options.headers, 'Authorization': `Bearer ${adminKey}` };
        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
        }
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) {
            showLogin("认证失败，请重新登录");
            return;
        }
        return response;
    }

    function showLogin(errorMsg = "") {
        adminKey = null;
        sessionStorage.removeItem("catfishAdminKey");
        loginOverlay.classList.remove("hidden");
        topBar.classList.add("hidden");
        appContainer.classList.add("hidden");
        loginError.textContent = errorMsg;
        if (statsInterval) clearInterval(statsInterval);
        if (logsInterval) clearInterval(logsInterval);
    }

    function showApp() {
        loginOverlay.classList.add("hidden");
        topBar.classList.remove("hidden");
        appContainer.classList.remove("hidden");
        loginError.textContent = "";
        loadAllData();
        statsInterval = setInterval(loadStats, 5000);
        logsInterval = setInterval(loadLogs, 5000);
    }

    async function handleLogin() {
        const key = adminKeyInput.value;
        if (!key) {
            loginError.textContent = "请输入密钥";
            return;
        }
        try {
            const response = await fetch("/admin/logs", { headers: { 'Authorization': `Bearer ${key}` } });
            if (response.status === 401) {
                loginError.textContent = "密钥不正确";
            } else if (response.ok) {
                adminKey = key;
                sessionStorage.setItem("catfishAdminKey", key);
                showApp();
            } else {
                loginError.textContent = `登录失败 (状态: ${response.status})`;
            }
        } catch (err) {
            loginError.textContent = `登录时发生网络错误: ${err.message}`;
        }
    }

    function loadAllData() {
        loadConfigs();
        loadStats();
        loadLogs();
        loadLogSettings();
    }

    // [重构] 加载并渲染所有方案配置
    async function loadConfigs() {
        try {
            const response = await authedFetch("/admin/config");
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const schemes = await response.json();
            allSchemesCache = schemes; // 缓存数据

            configSchemesContainer.innerHTML = ""; // 清空
            const schemeNames = Object.keys(schemes);

            if (schemeNames.length === 0) {
                configSchemesContainer.innerHTML = `<p>尚未添加任何配置项。</p>`;
                return;
            }

            const collapsedStateMap = getSchemeCollapseStateMap();

            schemeNames.sort().forEach(schemeName => {
                const configs = schemes[schemeName];
                const schemeBlock = document.createElement("div");
                schemeBlock.className = "scheme-block";

                const isCollapsed = !!collapsedStateMap[schemeName];

                let tableRows = '';
                if (configs.length > 0) {
                    configs.forEach(config => {
                        tableRows += `
                            <tr data-config-id="${config.id}" data-scheme-name="${schemeName}">
                                <td>${config.priority}</td>
                                <td><small>${config.url}</small></td>
                                <td><small>sk-*****${config.api_key.slice(-4)}</small></td>
                                <td>${config.model || '<em>(使用原始)</em>'}</td>
                                <td>
                                    ${config.consecutive_failure_threshold ? `<strong>${config.consecutive_failure_threshold}次</strong> / ${config.disable_duration_seconds}s` : '<em>(未设置)</em>'}
                                </td>
                                <td>${config.max_retries ?? 0}</td>
                                <td><small>${formatStreamModeStrategy(config.stream_mode_strategy)}</small></td>
                                <td><small>${formatUserAgentMode(config)}</small></td>
                                <td><small>${formatInjectionSummary(config)}</small></td>
                                <td><small>${formatOverridesSummary(config.request_overrides)}</small></td>
                                <td><small>${config.id}</small></td>
                                <td>
                                    <button class="button edit-btn">编辑</button>
                                    <button class="button danger delete-btn">删除</button>
                                </td>
                            </tr>
                        `;
                    });
                } else {
                    tableRows = `<tr><td colspan="12">该方案下没有配置项</td></tr>`;
                }

                schemeBlock.innerHTML = `
                    <div class="scheme-header">
                        <h3 class="scheme-title">${schemeName} <small>(Model Name)</small></h3>
                        <button type="button" class="button button-secondary scheme-toggle-btn" data-scheme-name="${schemeName}">
                            ${isCollapsed ? "展开" : "收起"}
                        </button>
                    </div>
                    <div class="table-container scheme-content ${isCollapsed ? "hidden" : ""}">
                        <table>
                            <thead>
                                <tr>
                                    <th>优先级</th>
                                    <th>URL</th>
                                    <th>Key (遮罩)</th>
                                    <th>覆盖 Model</th>
                                    <th>熔断设置 (失败/时长)</th>
                                    <th>重试次数</th>
                                    <th>流模式策略</th>
                                    <th>UA 模式</th>
                                    <th>注入策略</th>
                                    <th>强制覆盖参数</th>
                                    <th>ID</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>${tableRows}</tbody>
                        </table>
                    </div>
                `;
                configSchemesContainer.appendChild(schemeBlock);
            });

            configSchemesContainer.querySelectorAll('.scheme-toggle-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const schemeName = btn.dataset.schemeName;
                    const block = btn.closest('.scheme-block');
                    const content = block.querySelector('.scheme-content');
                    const nowCollapsed = !content.classList.contains('hidden');
                    content.classList.toggle('hidden');
                    btn.textContent = nowCollapsed ? '展开' : '收起';
                    setSchemeCollapsed(schemeName, nowCollapsed);
                });
            });
            
            // 为所有新生成的按钮添加事件监听
            configSchemesContainer.querySelectorAll('.edit-btn').forEach(btn => {
                const row = btn.closest('tr');
                btn.addEventListener('click', () => {
                    const configId = row.dataset.configId;
                    const schemeName = row.dataset.schemeName;
                    populateFormForEdit(allSchemesCache[schemeName].find(c => c.id === configId), schemeName);
                });
            });
            configSchemesContainer.querySelectorAll('.delete-btn').forEach(btn => {
                const row = btn.closest('tr');
                btn.addEventListener('click', () => handleDeleteConfig(row.dataset.configId));
            });


        } catch (err) {
            console.error("加载配置失败:", err);
            configSchemesContainer.innerHTML = `<p class="fail-text">加载配置失败: ${err.message}</p>`;
        }
    }

    // [重构] 加载统计数据
    async function loadStats() {
        try {
            const response = await authedFetch("/admin/stats");
            if (!response || !response.ok) return;
            const stats = await response.json();

            statTotalSuccess.textContent = stats.total.success || 0;
            statTotalFail.textContent = stats.total.fail || 0;
            statTodaySuccess.textContent = stats.today.success || 0;
            statTodayFail.textContent = stats.today.fail || 0;

            const allConfigsFlat = Object.values(allSchemesCache).flat();

            // 渲染历史总计
            renderStatsTable(statsByConfigBody, allConfigsFlat, stats.by_config_id, true);
            // 渲染今日统计
            renderStatsTable(statsTodayByConfigBody, allConfigsFlat, stats.today.by_config_id, false);

        } catch (err) {
            console.error("加载统计失败:", err);
        }
    }
    
    // [新增] 渲染统计表格的辅助函数
    function renderStatsTable(tbody, configs, statsData, isTotal) {
        tbody.innerHTML = "";
        if (configs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${isTotal ? 6 : 4}">没有配置项</td></tr>`;
            return;
        }

        configs.forEach(config => {
            const configStat = statsData[config.id] || { success: 0, fail: 0 };
            const tr = document.createElement("tr");
            let rowHTML = `
                <td><small>${config.id}</small></td>
                <td><small>${config.url}</small></td>
                <td class="success-text">${configStat.success || 0}</td>
                <td class="fail-text">${configStat.fail || 0}</td>
            `;
            if (isTotal) {
                const disabledUntil = configStat.disabled_until ? new Date(configStat.disabled_until).toLocaleString() : '<em>-</em>';
                rowHTML += `
                    <td>${configStat.consecutive_fails || 0}</td>
                    <td><small>${disabledUntil}</small></td>
                `;
            }
            tr.innerHTML = rowHTML;
            tbody.appendChild(tr);
        });
    }


    async function loadLogs() {
        try {
            const response = await authedFetch("/admin/logs");
            if (!response || !response.ok) return;
            const logs = await response.json();
            logsContent.textContent = logs.reverse().join("\n");
        } catch (err) {
            console.error("加载日志失败:", err);
        }
    }

    async function loadLogSettings() {
        if (!toggleFullRequestLogCheckbox) return;
        try {
            const response = await authedFetch("/admin/settings/logs");
            if (!response || !response.ok) return;
            const settings = await response.json();
            isSyncingLogToggle = true;
            toggleFullRequestLogCheckbox.checked = !!settings.show_full_response_body;
            if (fullRequestLogStatus) {
                fullRequestLogStatus.textContent = settings.show_full_response_body ? "当前：已开启" : "当前：已关闭";
            }
        } catch (err) {
            console.error("加载日志设置失败:", err);
        } finally {
            isSyncingLogToggle = false;
        }
    }

    async function updateLogSettings(enabled) {
        if (!toggleFullRequestLogCheckbox) return;
        try {
            const response = await authedFetch("/admin/settings/logs", {
                method: "PUT",
                body: JSON.stringify({ show_full_response_body: !!enabled })
            });
            if (!response || !response.ok) {
                throw new Error(`HTTP error! status: ${response?.status}`);
            }
            const updated = await response.json();
            isSyncingLogToggle = true;
            toggleFullRequestLogCheckbox.checked = !!updated.show_full_response_body;
            if (fullRequestLogStatus) {
                fullRequestLogStatus.textContent = updated.show_full_response_body ? "当前：已开启" : "当前：已关闭";
            }
        } catch (err) {
            console.error("更新日志设置失败:", err);
            alert(`更新日志设置失败: ${err.message}`);
            await loadLogSettings();
        } finally {
            isSyncingLogToggle = false;
        }
    }

    function resetForm() {
        configForm.reset();
        configIdInput.value = "";
        formTitle.textContent = "添加新配置";
        configSchemeInput.disabled = false;
        configMaxRetriesInput.value = "0";
        configInjectionPositionInput.value = "prepend";
        configUserAgentModeInput.value = "aggregator";
        configCustomUserAgentInput.value = "";
        updateCustomUserAgentVisibility();
        resetModelPicker("先查询后选择模型");
        configStreamModeStrategyInput.value = "passthrough";
        configRequestOverridesInput.value = "{}";
        renderInjectedMessagesEditor([]);
        cancelButton.classList.add("hidden");
    }

    // [重构] 填充表单
    function populateFormForEdit(config, schemeName) {
        formTitle.textContent = "编辑配置项";
        configIdInput.value = config.id;
        configSchemeInput.value = schemeName;
        configSchemeInput.disabled = true; // 编辑时不允许修改方案
        configPriorityInput.value = config.priority;
        configUrlInput.value = config.url;
        configKeyInput.value = config.api_key;
        configModelInput.value = config.model;
        configFailureThresholdInput.value = config.consecutive_failure_threshold;
        configDisableDurationInput.value = config.disable_duration_seconds;
        configMaxRetriesInput.value = config.max_retries ?? 0;
        configRequestOverridesInput.value = JSON.stringify(config.request_overrides || {}, null, 2);
        configInjectionPositionInput.value = config.injection_position || "prepend";
        configUserAgentModeInput.value = config.user_agent_mode || "aggregator";
        configCustomUserAgentInput.value = config.custom_user_agent || "";
        updateCustomUserAgentVisibility();
        resetModelPicker("可查询并选择该 URL 下的模型");
        configStreamModeStrategyInput.value = config.stream_mode_strategy || "passthrough";
        renderInjectedMessagesEditor(config.injected_messages || [], config.injection_position || "prepend");
        cancelButton.classList.remove("hidden");
        configForm.scrollIntoView({ behavior: "smooth" });
    }

    // [重构] 处理表单提交
    async function handleFormSubmit(e) {
        e.preventDefault();

        const configId = configIdInput.value;
        const isEditing = !!configId;

        let requestOverrides = {};
        const overridesText = (configRequestOverridesInput.value || "").trim();
        if (overridesText) {
            try {
                requestOverrides = JSON.parse(overridesText);
            } catch (err) {
                alert(`请求参数强制覆盖 JSON 格式错误: ${err.message}`);
                return;
            }
            if (requestOverrides === null || Array.isArray(requestOverrides) || typeof requestOverrides !== "object") {
                alert("请求参数强制覆盖必须是 JSON 对象，例如 {\"temperature\":0.2}");
                return;
            }
        }

        const retryRaw = configMaxRetriesInput.value;
        const retryParsed = retryRaw === "" ? 0 : parseInt(retryRaw, 10);
        const maxRetries = Number.isNaN(retryParsed) || retryParsed < 0 ? 0 : retryParsed;

        const data = {
            priority: parseInt(configPriorityInput.value, 10),
            url: configUrlInput.value,
            api_key: configKeyInput.value,
            model: configModelInput.value || null,
            max_retries: maxRetries,
            request_overrides: requestOverrides,
            injection_position: configInjectionPositionInput.value || "prepend",
            user_agent_mode: configUserAgentModeInput.value || "aggregator",
            custom_user_agent: configCustomUserAgentInput.value || null,
            stream_mode_strategy: configStreamModeStrategyInput.value || "passthrough",
            injected_messages: getInjectedMessagesFromEditor(),
            consecutive_failure_threshold: configFailureThresholdInput.value ? parseInt(configFailureThresholdInput.value, 10) : null,
            disable_duration_seconds: configDisableDurationInput.value ? parseInt(configDisableDurationInput.value, 10) : null,
        };
        
        let url, method;
        if (isEditing) {
            url = `/admin/config/${configId}`;
            method = "PUT";
        } else {
            url = "/admin/config";
            method = "POST";
            data.scheme_name = configSchemeInput.value; // 仅在创建时发送 scheme_name
        }

        try {
            const response = await authedFetch(url, { method, body: JSON.stringify(data) });
            if (response.ok) {
                resetForm();
                await loadConfigs(); // 重新加载配置
                await loadStats();   // 重新加载统计
            } else {
                const error = await response.json();
                alert(`保存失败: ${error.detail || response.statusText}`);
            }
        } catch (err) {
            alert(`保存时发生错误: ${err.message}`);
        }
    }

    async function handleDeleteConfig(configId) {
        if (!confirm("确定要删除这个配置项吗？")) return;
        try {
            const response = await authedFetch(`/admin/config/${configId}`, { method: "DELETE" });
            if (response.ok) {
                await loadConfigs();
                await loadStats();
            } else {
                const error = await response.json();
                alert(`删除失败: ${error.detail || response.statusText}`);
            }
        } catch (err) {
            alert(`删除时发生错误: ${err.message}`);
        }
    }

    async function handleQueryModels() {
        const url = (configUrlInput.value || "").trim();
        const apiKey = (configKeyInput.value || "").trim();
        if (!url || !apiKey) {
            setModelQueryStatus("请先填写 API 终端 URL 和 API Key", true);
            return;
        }

        queryModelsButton.disabled = true;
        modelPickerSelect.disabled = true;
        resetModelPicker("查询中...");
        setModelQueryStatus("正在查询上游模型列表...", false);

        try {
            const response = await authedFetch("/admin/models/query", {
                method: "POST",
                body: JSON.stringify({
                    url,
                    api_key: apiKey,
                    user_agent_mode: configUserAgentModeInput.value || "aggregator",
                    custom_user_agent: configCustomUserAgentInput.value || null
                })
            });
            if (!response) return;
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(payload.detail || response.statusText);
            }

            const models = Array.isArray(payload.data) ? payload.data : [];
            renderModelOptions(models);
            if (models.length === 0) {
                setModelQueryStatus("上游返回了空模型列表", true);
            } else {
                setModelQueryStatus(`已查询到 ${models.length} 个模型，选择后会回填左侧输入框`, false);
            }
        } catch (err) {
            resetModelPicker("查询失败，请重试");
            setModelQueryStatus(`模型查询失败: ${err.message}`, true);
        } finally {
            queryModelsButton.disabled = false;
            modelPickerSelect.disabled = false;
        }
    }

    function resetModelPicker(label = "先查询后选择模型") {
        if (!modelPickerSelect) return;
        modelPickerSelect.innerHTML = "";
        const option = document.createElement("option");
        option.value = "";
        option.textContent = label;
        modelPickerSelect.appendChild(option);
    }

    function renderModelOptions(models) {
        resetModelPicker("选择查询到的模型");
        models.forEach(modelId => {
            const option = document.createElement("option");
            option.value = modelId;
            option.textContent = modelId;
            modelPickerSelect.appendChild(option);
        });
    }

    function setModelQueryStatus(message, isError) {
        if (!modelQueryStatus) return;
        modelQueryStatus.textContent = message;
        modelQueryStatus.classList.toggle("fail-text", !!isError);
    }

    function updateCustomUserAgentVisibility() {
        if (!configCustomUserAgentInput || !configUserAgentModeInput) return;
        const isCustom = configUserAgentModeInput.value === "custom";
        configCustomUserAgentInput.disabled = !isCustom;
        configCustomUserAgentInput.placeholder = isCustom ? "输入要发往上游的 User-Agent" : "仅自定义 UA 模式生效";
    }

    function formatUserAgentMode(config) {
        const mode = config.user_agent_mode || "aggregator";
        const label = USER_AGENT_MODE_LABEL_MAP[mode] || mode;
        if (mode === "custom" && config.custom_user_agent) {
            return `${label}: ${config.custom_user_agent}`;
        }
        return label;
    }

    function formatStreamModeStrategy(strategy) {
        const normalized = strategy || "passthrough";
        return STREAM_STRATEGY_LABEL_MAP[normalized] || normalized;
    }

    function formatInjectionSummary(config) {
        const messages = Array.isArray(config.injected_messages) ? config.injected_messages : [];
        if (messages.length === 0) {
            return "(无)";
        }
        const fallbackPosition = config.injection_position || "prepend";
        const counts = { prepend: 0, append: 0 };
        const rolesPreview = messages.slice(0, 3).map(m => {
            const position = m.position || fallbackPosition;
            const label = INJECTION_POSITION_LABEL_MAP[position] || INJECTION_POSITION_LABEL_MAP.prepend;
            return `${label}:${m.role}`;
        }).join(", ");
        messages.forEach(m => {
            const position = m.position || fallbackPosition;
            if (position === "append") counts.append += 1;
            else counts.prepend += 1;
        });
        const parts = [];
        if (counts.prepend) parts.push(`最前${counts.prepend}条`);
        if (counts.append) parts.push(`最后${counts.append}条`);
        const more = messages.length > 3 ? " ..." : "";
        return `${parts.join(" / ")} / ${rolesPreview}${more}`;
    }

    function formatOverridesSummary(overrides) {
        if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) {
            return "(无)";
        }
        const keys = Object.keys(overrides);
        if (keys.length === 0) {
            return "(无)";
        }
        const preview = keys.slice(0, 4).join(", ");
        return keys.length > 4 ? `${preview} ...` : preview;
    }

    function parseOverridesFromTextarea() {
        const text = (configRequestOverridesInput.value || "").trim();
        if (!text) return {};
        const parsed = JSON.parse(text);
        if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
            throw new Error("强制覆盖参数必须是 JSON 对象");
        }
        return parsed;
    }

    function applyPresetField(fieldPreset) {
        try {
            const currentObj = parseOverridesFromTextarea();
            if (!(fieldPreset.key in currentObj)) {
                currentObj[fieldPreset.key] = fieldPreset.defaultValue;
            }
            configRequestOverridesInput.value = JSON.stringify(currentObj, null, 2);
        } catch (err) {
            alert(`当前 JSON 不是合法对象，无法插入字段: ${err.message}`);
        }
    }

    function getSchemeCollapseStateMap() {
        try {
            const raw = localStorage.getItem(CONFIG_COLLAPSE_STORAGE_KEY);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch {
            return {};
        }
    }

    function setSchemeCollapsed(schemeName, isCollapsed) {
        const state = getSchemeCollapseStateMap();
        state[schemeName] = !!isCollapsed;
        localStorage.setItem(CONFIG_COLLAPSE_STORAGE_KEY, JSON.stringify(state));
    }

    function createInjectedMessageRow(message = { role: "system", content: "", position: "prepend" }, fallbackPosition = "prepend") {
        const row = document.createElement("div");
        row.className = "injected-message-row";

        const role = ALLOWED_INJECT_ROLES.includes(message?.role) ? message.role : "system";
        const position = ["prepend", "append"].includes(message?.position) ? message.position : fallbackPosition;
        const content = message?.content ?? "";

        const roleSelect = document.createElement("select");
        roleSelect.className = "injected-role-select";
        ALLOWED_INJECT_ROLES.forEach(r => {
            const option = document.createElement("option");
            option.value = r;
            option.textContent = r;
            if (r === role) option.selected = true;
            roleSelect.appendChild(option);
        });

        const positionSelect = document.createElement("select");
        positionSelect.className = "injected-position-select";
        ["prepend", "append"].forEach(p => {
            const option = document.createElement("option");
            option.value = p;
            option.textContent = INJECTION_POSITION_LABEL_MAP[p];
            if (p === position) option.selected = true;
            positionSelect.appendChild(option);
        });

        const contentInput = document.createElement("textarea");
        contentInput.className = "injected-content-input";
        contentInput.rows = 2;
        contentInput.placeholder = "输入注入内容...";
        contentInput.value = String(content);

        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "button danger injected-delete-btn";
        deleteBtn.textContent = "删除";
        deleteBtn.addEventListener("click", () => {
            row.remove();
        });

        row.appendChild(roleSelect);
        row.appendChild(positionSelect);
        row.appendChild(contentInput);
        row.appendChild(deleteBtn);

        return row;
    }

    function renderInjectedMessagesEditor(messages, fallbackPosition = "prepend") {
        if (!injectedMessagesEditor) return;
        injectedMessagesEditor.innerHTML = "";
        const list = Array.isArray(messages) ? messages : [];
        list.forEach(msg => injectedMessagesEditor.appendChild(createInjectedMessageRow(msg, fallbackPosition)));
    }

    function getInjectedMessagesFromEditor() {
        if (!injectedMessagesEditor) return [];
        const rows = injectedMessagesEditor.querySelectorAll(".injected-message-row");
        const result = [];
        rows.forEach(row => {
            const role = row.querySelector(".injected-role-select")?.value;
            const position = row.querySelector(".injected-position-select")?.value || "prepend";
            const content = row.querySelector(".injected-content-input")?.value ?? "";
            if (!ALLOWED_INJECT_ROLES.includes(role)) return;
            if (!["prepend", "append"].includes(position)) return;
            if (content.trim() === "") return;
            result.push({ role, position, content });
        });
        return result;
    }

    function renderPresetTable() {
        if (!presetTableBody) return;
        presetTableBody.innerHTML = "";

        PARAMETER_FIELD_PRESETS.forEach((item) => {
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><code>${item.key}</code></td>
                <td>${item.type}</td>
                <td>${item.description}</td>
                <td><button type="button" class="button edit-btn">加入字段</button></td>
            `;
            const addBtn = tr.querySelector("button");
            addBtn.addEventListener("click", () => applyPresetField(item));
            presetTableBody.appendChild(tr);
        });
    }

    function init() {
        loginButton.addEventListener("click", handleLogin);
        adminKeyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });
        logoutButton.addEventListener("click", () => showLogin("您已退出登录"));

        tabs.forEach(tab => {
            tab.addEventListener("click", () => {
                tabs.forEach(t => t.classList.remove("active"));
                tabContents.forEach(c => c.classList.remove("active"));
                tab.classList.add("active");
                document.getElementById(tab.dataset.tab + "-tab").classList.add("active");
            });
        });

        configForm.addEventListener("submit", handleFormSubmit);
        cancelButton.addEventListener("click", resetForm);
        addInjectedMessageButton.addEventListener("click", () => {
            injectedMessagesEditor.appendChild(createInjectedMessageRow({
                role: "system",
                content: "",
                position: configInjectionPositionInput.value || "prepend"
            }));
        });
        configUserAgentModeInput.addEventListener("change", updateCustomUserAgentVisibility);
        queryModelsButton.addEventListener("click", handleQueryModels);
        modelPickerSelect.addEventListener("change", () => {
            if (modelPickerSelect.value) {
                configModelInput.value = modelPickerSelect.value;
                setModelQueryStatus(`已选择模型: ${modelPickerSelect.value}`, false);
            }
        });
        if (toggleFullRequestLogCheckbox) {
            toggleFullRequestLogCheckbox.addEventListener("change", async (e) => {
                if (isSyncingLogToggle) return;
                await updateLogSettings(e.target.checked);
            });
        }

        renderPresetTable();
        resetForm();

        if (adminKey) {
            (async () => {
                const response = await fetch("/admin/logs", { headers: { 'Authorization': `Bearer ${adminKey}` } });
                if (response.ok) {
                    showApp();
                } else {
                    showLogin("会话已过期，请重新登录");
                }
            })();
        } else {
            showLogin();
        }
    }

    init();
});