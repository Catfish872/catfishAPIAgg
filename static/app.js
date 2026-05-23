// 确保 DOM 加载完毕后执行
document.addEventListener("DOMContentLoaded", () => {

    // --- 1. DOM 元素获取 ---
    const loginOverlay = document.getElementById("login-overlay");
    const loginButton = document.getElementById("login-button");
    const adminKeyInput = document.getElementById("admin-key-input");
    const loginError = document.getElementById("login-error");
    const rememberLoginCheckbox = document.getElementById("remember-login-checkbox");

    const topBar = document.getElementById("top-bar");
    const appContainer = document.getElementById("app-container");
    const logoutButton = document.getElementById("logout-button");
    const effectsToggleButtons = document.querySelectorAll("[data-effects-toggle]");

    const tabs = document.querySelectorAll(".tab-button");
    const tabContents = document.querySelectorAll(".tab-content");
    const themeOptions = document.querySelectorAll(".theme-option");
    const configBackToTopButton = document.getElementById("config-back-to-top");

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
    const configEndpointPresetInput = document.getElementById("config-endpoint-preset");
    const imageOptionsGroup = document.getElementById("image-options-group");
    const configImageUpstreamModeInput = document.getElementById("config-image-upstream-mode");
    const configImageGenerationPathInput = document.getElementById("config-image-generation-path");
    const configImageEditPathInput = document.getElementById("config-image-edit-path");
    const configImageTaskPollTimeoutInput = document.getElementById("config-image-task-poll-timeout");
    const configImageTaskPollIntervalInput = document.getElementById("config-image-task-poll-interval");
    const configImageCustomReferenceFieldInput = document.getElementById("config-image-custom-reference-field");
    const configImageCustomReferenceModeInput = document.getElementById("config-image-custom-reference-mode");
    const configImageCustomReferenceObjectUrlFieldInput = document.getElementById("config-image-custom-reference-object-url-field");
    const imageCustomOptionEls = document.querySelectorAll(".image-custom-option");
    const imageCustomObjectOptionEls = document.querySelectorAll(".image-custom-object-option");
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
    const statsByIpBody = document.getElementById("stats-by-ip-body");

    // 日志 Tab
    const logsContent = document.getElementById("logs-content");
    const toggleFullRequestLogCheckbox = document.getElementById("toggle-full-request-log");
    const fullRequestLogStatus = document.getElementById("full-request-log-status");

    const SESSION_ADMIN_KEY_STORAGE_KEY = "catfishAdminKey";
    const PERSISTENT_ADMIN_KEY_STORAGE_KEY = "catfishPersistentAdminKey";
    let adminKey = sessionStorage.getItem(SESSION_ADMIN_KEY_STORAGE_KEY) || localStorage.getItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
    let statsInterval, logsInterval;
    let allSchemesCache = {}; // 缓存配置数据，用于统计显示
    let configStatsCache = { total: {}, today: {} };
    let isSyncingLogToggle = false;

    const CONFIG_COLLAPSE_STORAGE_KEY = "catfish_config_scheme_collapsed";
    const STATS_COLLAPSE_STORAGE_KEY = "catfish_stats_sections_open";
    const IP_PATH_DETAIL_STORAGE_KEY = "catfish_ip_path_details_open";
    const THEME_STORAGE_KEY = "catfish_console_theme";
    const API_KEY_VISIBILITY_STORAGE_KEY = "catfish_show_api_keys";
    const EFFECTS_STORAGE_KEY = "catfish_frontend_effects_enabled";
    const ALLOWED_THEMES = ["gpt", "gemini", "claude", "deepseek"];
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

    let showApiKeys = localStorage.getItem(API_KEY_VISIBILITY_STORAGE_KEY) === "true";

    // --- 2. 核心功能函数 ---

    function normalizeTheme(theme) {
        return ALLOWED_THEMES.includes(theme) ? theme : "gpt";
    }

    function applyTheme(theme, shouldPersist = true) {
        const normalized = normalizeTheme(theme);
        document.documentElement.dataset.theme = normalized;
        themeOptions.forEach(option => {
            const isActive = option.dataset.themeOption === normalized;
            option.classList.toggle("active", isActive);
            option.setAttribute("aria-pressed", String(isActive));
        });
        if (shouldPersist) {
            localStorage.setItem(THEME_STORAGE_KEY, normalized);
        }
    }

    function areEffectsEnabled() {
        return document.documentElement.dataset.effects !== "off";
    }

    function applyEffectsPreference(enabled, shouldPersist = true) {
        const normalized = enabled !== false;
        document.documentElement.dataset.effects = normalized ? "on" : "off";
        effectsToggleButtons.forEach(button => {
            button.textContent = normalized ? "特效：开" : "特效：关";
            button.setAttribute("aria-pressed", String(normalized));
            button.title = normalized ? "点击关闭前端动画和高成本视觉效果" : "点击开启前端动画和视觉效果";
        });
        if (shouldPersist) {
            localStorage.setItem(EFFECTS_STORAGE_KEY, normalized ? "true" : "false");
        }
    }

    function initEffectsPreference() {
        const savedValue = localStorage.getItem(EFFECTS_STORAGE_KEY);
        applyEffectsPreference(savedValue !== "false", false);
        effectsToggleButtons.forEach(button => {
            button.addEventListener("click", () => {
                applyEffectsPreference(!areEffectsEnabled());
            });
        });
    }

    function createButtonRipple(event, target) {
        const rect = target.getBoundingClientRect();
        const size = Math.max(rect.width, rect.height);
        const ripple = document.createElement("span");
        ripple.className = "button-ripple";
        ripple.style.width = `${size}px`;
        ripple.style.height = `${size}px`;
        ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
        ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
        target.appendChild(ripple);
        window.setTimeout(() => ripple.remove(), 950);
    }

    function initButtonRippleEffects() {
        document.addEventListener("click", (event) => {
            if (!areEffectsEnabled()) return;
            const target = event.target.closest("button, .button");
            if (!target || target.disabled) return;
            createButtonRipple(event, target);
        });
    }

    function initTheme() {
        const savedTheme = normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY));
        applyTheme(savedTheme, false);
        themeOptions.forEach(option => {
            option.addEventListener("click", () => applyTheme(option.dataset.themeOption));
        });
    }

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = String(value ?? "");
        return div.innerHTML;
    }

    function maskApiKey(apiKey) {
        if (!apiKey) return "";
        if (apiKey.length <= 8) return "••••••••";
        return `${apiKey.slice(0, 4)}••••••${apiKey.slice(-4)}`;
    }

    function formatApiKeyForDisplay(apiKey) {
        return showApiKeys ? (apiKey || "") : maskApiKey(apiKey || "");
    }

    function setApiKeyVisibility(visible) {
        showApiKeys = !!visible;
        localStorage.setItem(API_KEY_VISIBILITY_STORAGE_KEY, String(showApiKeys));
        loadConfigs();
    }

    function persistAdminKey(key, remember) {
        if (!key) return;
        sessionStorage.setItem(SESSION_ADMIN_KEY_STORAGE_KEY, key);
        if (remember) {
            localStorage.setItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY, key);
        } else {
            localStorage.removeItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
        }
    }

    function clearStoredAdminKey() {
        sessionStorage.removeItem(SESSION_ADMIN_KEY_STORAGE_KEY);
        localStorage.removeItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
    }

    async function authedFetch(url, options = {}) {
        if (!adminKey) {
            console.error("No admin key found");
            showLogin("会话已过期，请重新登录", { clearPersistent: false });
            return;
        }
        const headers = { ...options.headers, 'Authorization': `Bearer ${adminKey}` };
        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
        }
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) {
            showLogin("认证失败，请重新登录", { clearPersistent: true });
            return;
        }
        return response;
    }

    function showLogin(errorMsg = "", options = {}) {
        adminKey = null;
        sessionStorage.removeItem(SESSION_ADMIN_KEY_STORAGE_KEY);
        if (options.clearPersistent) {
            localStorage.removeItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
        }
        if (rememberLoginCheckbox) {
            rememberLoginCheckbox.checked = !!localStorage.getItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
        }
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
                persistAdminKey(key, !!rememberLoginCheckbox?.checked);
                showApp();
            } else {
                loginError.textContent = `登录失败 (状态: ${response.status})`;
            }
        } catch (err) {
            loginError.textContent = `登录时发生网络错误: ${err.message}`;
        }
    }

    async function loadAllData() {
        await loadConfigs();
        await loadStats();
        loadLogs();
        loadLogSettings();
    }

    function getConfigStatsMarkup(configId, totalStat = {}, todayStat = {}) {
        const totalSuccess = totalStat.success || 0;
        const totalFail = totalStat.fail || 0;
        const todaySuccess = todayStat.success || 0;
        const todayFail = todayStat.fail || 0;
        return `
            <div class="config-inline-stats" aria-label="请求统计">
                <span class="config-stat-pill total-success" title="总计成功">◆ ${totalSuccess}</span>
                <span class="config-stat-pill total-fail" title="总计失败">◆ ${totalFail}</span>
                <span class="config-stat-pill today-success" title="今日成功">● ${todaySuccess}</span>
                <span class="config-stat-pill today-fail" title="今日失败">● ${todayFail}</span>
            </div>
        `;
    }

    function renderConfigIdWithStats(configId, totalStat = {}, todayStat = {}) {
        return `
            <div class="config-id-stat-cell">
                <small class="config-id-text">${escapeHtml(configId)}</small>
                ${getConfigStatsMarkup(configId, totalStat, todayStat)}
            </div>
        `;
    }

    function refreshConfigStatsBadges() {
        if (!configSchemesContainer) return;
        configSchemesContainer.querySelectorAll("tr[data-config-id]").forEach(row => {
            const configId = row.dataset.configId;
            const cell = row.querySelector(".config-inline-stats");
            if (!configId || !cell) return;
            const totalStat = configStatsCache.total?.[configId] || { success: 0, fail: 0 };
            const todayStat = configStatsCache.today?.[configId] || { success: 0, fail: 0 };
            cell.outerHTML = getConfigStatsMarkup(configId, totalStat, todayStat);
        });
    }

    // [重构] 加载并渲染所有方案配置
    async function loadConfigs(options = {}) {
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
                        const totalStat = configStatsCache.total?.[config.id] || { success: 0, fail: 0 };
                        const todayStat = configStatsCache.today?.[config.id] || { success: 0, fail: 0 };
                        tableRows += `
                            <tr data-config-id="${config.id}" data-scheme-name="${schemeName}">
                                <td>
                                    <div class="priority-stepper" aria-label="调整优先级">
                                        <button type="button" class="priority-step-btn priority-up-btn" title="优先级 +1（数字减小）">▲</button>
                                        <span class="priority-value">${config.priority}</span>
                                        <button type="button" class="priority-step-btn priority-down-btn" title="优先级 -1（数字增大）">▼</button>
                                    </div>
                                </td>
                                <td><small>${config.url}</small></td>
                                <td><small>${formatApiKeyForDisplay(config.api_key)}</small></td>
                                <td>${config.model || '<em>(使用原始)</em>'}</td>
                                <td>
                                    ${config.consecutive_failure_threshold ? `<strong>${config.consecutive_failure_threshold}次</strong> / ${config.disable_duration_seconds}s` : '<em>(未设置)</em>'}
                                </td>
                                <td>${config.max_retries ?? 0}</td>
                                <td><small>${formatStreamModeStrategy(config.stream_mode_strategy)}</small></td>
                                <td><small>${formatEndpointPreset(config.endpoint_preset)}${formatImageMode(config)}</small></td>
                                <td><small>${formatUserAgentMode(config)}</small></td>
                                <td><small>${formatInjectionSummary(config)}</small></td>
                                <td>${renderConfigIdWithStats(config.id, totalStat, todayStat)}</td>
                                <td>
                                    <div class="config-row-actions">
                                        <button type="button" class="button edit-btn">编辑</button>
                                        <button type="button" class="button copy-btn">复制</button>
                                        <button type="button" class="button danger delete-btn">删除</button>
                                    </div>
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
                        <div class="scheme-header-actions">
                            <button type="button" class="button button-secondary api-key-visibility-btn">
                                ${showApiKeys ? "隐藏 API Key" : "显示 API Key"}
                            </button>
                            <button type="button" class="button button-secondary scheme-toggle-btn" data-scheme-name="${schemeName}">
                                ${isCollapsed ? "展开" : "收起"}
                            </button>
                        </div>
                    </div>
                    <div class="table-container scheme-content ${isCollapsed ? "hidden" : ""}">
                        <table>
                            <thead>
                                <tr>
                                    <th>优先级</th>
                                    <th>URL</th>
                                    <th>API Key</th>
                                    <th>覆盖 Model</th>
                                    <th>熔断设置 (失败/时长)</th>
                                    <th>重试次数</th>
                                    <th>流模式策略</th>
                                    <th>预设端点</th>
                                    <th>UA 模式</th>
                                    <th>注入策略</th>
                                    <th>ID / 统计</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>${tableRows}</tbody>
                        </table>
                    </div>
                `;
                configSchemesContainer.appendChild(schemeBlock);
            });

            if (options.animateConfigId) {
                animateConfigRowMove(options.animateConfigId, options.fromRect);
            }

            configSchemesContainer.querySelectorAll('.api-key-visibility-btn').forEach(btn => {
                btn.addEventListener('click', () => setApiKeyVisibility(!showApiKeys));
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
            configSchemesContainer.querySelectorAll('.priority-up-btn, .priority-down-btn').forEach(btn => {
                const row = btn.closest('tr');
                btn.addEventListener('click', async () => {
                    const configId = row.dataset.configId;
                    const schemeName = row.dataset.schemeName;
                    const config = allSchemesCache[schemeName].find(c => c.id === configId);
                    const delta = btn.classList.contains('priority-up-btn') ? -1 : 1;
                    await adjustConfigPriority(config, delta, btn);
                });
            });
            configSchemesContainer.querySelectorAll('.copy-btn').forEach(btn => {
                const row = btn.closest('tr');
                btn.addEventListener('click', () => {
                    const configId = row.dataset.configId;
                    const schemeName = row.dataset.schemeName;
                    populateFormForCopy(allSchemesCache[schemeName].find(c => c.id === configId), schemeName);
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
            configStatsCache = {
                total: stats.by_config_id || {},
                today: stats.today?.by_config_id || {}
            };

            statTotalSuccess.textContent = stats.total.success || 0;
            statTotalFail.textContent = stats.total.fail || 0;
            statTodaySuccess.textContent = stats.today.success || 0;
            statTodayFail.textContent = stats.today.fail || 0;

            const allConfigsFlat = Object.values(allSchemesCache).flat();

            // 渲染历史总计
            renderStatsTable(statsByConfigBody, allConfigsFlat, stats.by_config_id, true);
            // 渲染今日统计
            renderStatsTable(statsTodayByConfigBody, allConfigsFlat, stats.today.by_config_id, false);
            // 渲染 IP 请求与封禁统计
            renderIpStatsTable(statsByIpBody, stats.by_ip || {});
            refreshConfigStatsBadges();

        } catch (err) {
            console.error("加载统计失败:", err);
        }
    }
    
    // [新增] 渲染统计表格的辅助函数
    function renderStatsTable(tbody, configs, statsData, isTotal) {
        tbody.innerHTML = "";
        if (configs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${isTotal ? 7 : 4}">没有配置项</td></tr>`;
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
                const isBlocked = Boolean(configStat.disabled_until);
                const disabledUntil = isBlocked ? new Date(configStat.disabled_until).toLocaleString() : '<em>-</em>';
                const unblockButton = isBlocked
                    ? `<button type="button" class="button button-secondary unblock-config-btn" data-config-id="${config.id}">解除禁用</button>`
                    : '<em>-</em>';
                rowHTML += `
                    <td>${configStat.consecutive_fails || 0}</td>
                    <td><small>${disabledUntil}</small></td>
                    <td>${unblockButton}</td>
                `;
            }
            tr.innerHTML = rowHTML;
            tbody.appendChild(tr);
        });
    }

    function formatDateTime(value) {
        if (!value) return "-";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
    }

    function getJsonStorageMap(storageKey) {
        try {
            const raw = localStorage.getItem(storageKey);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch {
            return {};
        }
    }

    function setJsonStorageMapValue(storageKey, key, value) {
        const state = getJsonStorageMap(storageKey);
        state[key] = value;
        localStorage.setItem(storageKey, JSON.stringify(state));
    }

    function getIpPathDetailStateMap() {
        return getJsonStorageMap(IP_PATH_DETAIL_STORAGE_KEY);
    }

    function setIpPathDetailOpen(ip, isOpen) {
        setJsonStorageMapValue(IP_PATH_DETAIL_STORAGE_KEY, ip, !!isOpen);
    }

    function renderIpPathDetails(paths) {
        const entries = Object.entries(paths || {}).sort((a, b) => {
            const aSeen = new Date(a[1]?.last_seen_at || 0).getTime() || 0;
            const bSeen = new Date(b[1]?.last_seen_at || 0).getTime() || 0;
            return bSeen - aSeen;
        });

        if (entries.length === 0) {
            return `<div class="ip-path-empty">最近 1 个月暂无路径明细</div>`;
        }

        const rows = entries.map(([path, item]) => `
            <tr>
                <td><code>${escapeHtml(path)}</code></td>
                <td>${item?.total || 0}</td>
                <td class="success-text">${item?.success || 0}</td>
                <td class="fail-text">${item?.fail || 0}</td>
                <td><small>${escapeHtml(formatDateTime(item?.last_seen_at))}</small></td>
                <td><small>${escapeHtml(item?.last_fail_reason || "-")}</small></td>
            </tr>
        `).join("");

        return `
            <div class="ip-path-detail-panel">
                <div class="ip-path-detail-title">最近 1 个月路径明细（仅记录 URL 路径，不含查询参数）</div>
                <div class="table-container ip-path-detail-table-wrap">
                    <table class="ip-path-detail-table">
                        <thead>
                            <tr>
                                <th>路径</th>
                                <th>总请求</th>
                                <th class="success-text">成功</th>
                                <th class="fail-text">失败</th>
                                <th>最后访问</th>
                                <th>最后失败原因</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>
        `;
    }

    function renderIpStatsTable(tbody, ipStatsData) {
        if (!tbody) return;
        tbody.innerHTML = "";
        const detailStateMap = getIpPathDetailStateMap();
        const entries = Object.entries(ipStatsData || {}).sort((a, b) => {
            const aBanned = a[1]?.is_banned ? 1 : 0;
            const bBanned = b[1]?.is_banned ? 1 : 0;
            if (aBanned !== bBanned) return bBanned - aBanned;
            const aSeen = new Date(a[1]?.last_seen_at || 0).getTime() || 0;
            const bSeen = new Date(b[1]?.last_seen_at || 0).getTime() || 0;
            return bSeen - aSeen;
        });

        if (entries.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9">暂无 IP 请求统计</td></tr>`;
            return;
        }

        entries.forEach(([ip, stat]) => {
            const isBanned = !!stat?.is_banned;
            const safeIp = escapeHtml(ip);
            const isDetailOpen = !!detailStateMap[ip];
            const pathCount = stat?.paths && typeof stat.paths === "object" ? Object.keys(stat.paths).length : 0;
            const tr = document.createElement("tr");
            tr.classList.toggle("ip-banned-row", isBanned);
            tr.dataset.ip = ip;
            const statusBadge = isBanned
                ? `<span class="status-badge status-badge-danger">已封禁</span><small>${escapeHtml(formatDateTime(stat.banned_at))}</small>`
                : `<span class="status-badge status-badge-ok">正常</span>`;
            const actionButton = isBanned
                ? `<button type="button" class="button button-secondary unblock-ip-btn" data-ip="${safeIp}">解除封禁</button>`
                : '<em>-</em>';
            tr.innerHTML = `
                <td>
                    <div class="ip-cell-with-toggle">
                        <button type="button" class="ip-path-toggle-btn" data-ip="${safeIp}" aria-expanded="${isDetailOpen}">${isDetailOpen ? "收起" : "路径"}</button>
                        <small>${safeIp}</small>
                        <span class="ip-path-count">${pathCount}</span>
                    </div>
                </td>
                <td>${stat?.total || 0}</td>
                <td class="success-text">${stat?.success || 0}</td>
                <td class="fail-text">${stat?.fail || 0}</td>
                <td>${stat?.consecutive_fails || 0}</td>
                <td><div class="status-cell">${statusBadge}</div></td>
                <td><small>${escapeHtml(formatDateTime(stat?.last_seen_at))}</small></td>
                <td><small>${escapeHtml(stat?.last_fail_reason || "-")}</small></td>
                <td>${actionButton}</td>
            `;
            tbody.appendChild(tr);

            const detailTr = document.createElement("tr");
            detailTr.className = `ip-path-detail-row ${isDetailOpen ? "" : "hidden"}`;
            detailTr.dataset.ip = ip;
            detailTr.innerHTML = `<td colspan="9">${renderIpPathDetails(stat?.paths || {})}</td>`;
            tbody.appendChild(detailTr);
        });
    }

    async function unblockConfig(configId) {
        if (!configId) return;
        try {
            const response = await authedFetch(`/admin/stats/config/${encodeURIComponent(configId)}/unblock`, {
                method: "POST"
            });
            if (!response) return;
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || `HTTP ${response.status}`);
            }
            await loadStats();
        } catch (err) {
            console.error("解除熔断失败:", err);
            alert(`解除禁用失败: ${err.message}`);
        }
    }

    async function unblockIp(ip) {
        if (!ip) return;
        try {
            const response = await authedFetch(`/admin/stats/ip/${encodeURIComponent(ip)}/unblock`, {
                method: "POST"
            });
            if (!response) return;
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || `HTTP ${response.status}`);
            }
            await loadStats();
        } catch (err) {
            console.error("解除 IP 封禁失败:", err);
            alert(`解除 IP 封禁失败: ${err.message}`);
        }
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
        configEndpointPresetInput.value = "chat_completions";
        configUserAgentModeInput.value = "aggregator";
        configCustomUserAgentInput.value = "";
        updateCustomUserAgentVisibility();
        updateImageOptionsVisibility();
        resetModelPicker("先查询后选择模型");
        configStreamModeStrategyInput.value = "passthrough";
        configRequestOverridesInput.value = "{}";
        configImageUpstreamModeInput.value = "generation_reference_images_array";
        configImageGenerationPathInput.value = "/images/generations";
        configImageEditPathInput.value = "/images/edits";
        configImageTaskPollTimeoutInput.value = "300";
        configImageTaskPollIntervalInput.value = "2";
        configImageCustomReferenceFieldInput.value = "";
        configImageCustomReferenceModeInput.value = "array";
        configImageCustomReferenceObjectUrlFieldInput.value = "image_url";
        updateImageOptionsVisibility();
        renderInjectedMessagesEditor([]);
        cancelButton.classList.add("hidden");
    }

    function populateConfigForm(config, schemeName, options = {}) {
        const isCopy = options.mode === "copy";
        formTitle.textContent = isCopy ? "复制配置项为新配置" : "编辑配置项";
        configIdInput.value = isCopy ? "" : config.id;
        configSchemeInput.value = schemeName;
        configSchemeInput.disabled = !isCopy; // 编辑时不允许修改方案；复制时作为新配置允许调整方案
        configPriorityInput.value = config.priority;
        configUrlInput.value = config.url;
        configKeyInput.value = config.api_key;
        configModelInput.value = config.model;
        configFailureThresholdInput.value = config.consecutive_failure_threshold;
        configDisableDurationInput.value = config.disable_duration_seconds;
        configMaxRetriesInput.value = config.max_retries ?? 0;
        configRequestOverridesInput.value = JSON.stringify(config.request_overrides || {}, null, 2);
        configInjectionPositionInput.value = config.injection_position || "prepend";
        configEndpointPresetInput.value = config.endpoint_preset || "chat_completions";
        configUserAgentModeInput.value = config.user_agent_mode || "aggregator";
        configCustomUserAgentInput.value = config.custom_user_agent || "";
        updateCustomUserAgentVisibility();
        configImageUpstreamModeInput.value = config.image_upstream_mode || "generation_reference_images_array";
        configImageGenerationPathInput.value = config.image_generation_path || "/images/generations";
        configImageEditPathInput.value = config.image_edit_path || "/images/edits";
        configImageTaskPollTimeoutInput.value = config.image_task_poll_timeout_seconds ?? 300;
        configImageTaskPollIntervalInput.value = config.image_task_poll_interval_seconds ?? 2;
        configImageCustomReferenceFieldInput.value = config.image_custom_reference_field || "";
        configImageCustomReferenceModeInput.value = config.image_custom_reference_mode || "array";
        configImageCustomReferenceObjectUrlFieldInput.value = config.image_custom_reference_object_url_field || "image_url";
        updateImageOptionsVisibility();
        resetModelPicker("可查询并选择该 URL 下的模型");
        configStreamModeStrategyInput.value = config.stream_mode_strategy || "passthrough";
        renderInjectedMessagesEditor(config.injected_messages || [], config.injection_position || "prepend");
        cancelButton.classList.remove("hidden");
        configForm.scrollIntoView({ behavior: areEffectsEnabled() ? "smooth" : "auto", block: "start" });
    }

    // [重构] 填充表单
    function populateFormForEdit(config, schemeName) {
        populateConfigForm(config, schemeName, { mode: "edit" });
    }

    function populateFormForCopy(config, schemeName) {
        populateConfigForm(config, schemeName, { mode: "copy" });
    }

    function buildConfigUpdatePayload(config, overrides = {}) {
        return {
            priority: overrides.priority ?? config.priority,
            url: overrides.url ?? config.url,
            api_key: overrides.api_key ?? config.api_key,
            model: overrides.model ?? config.model ?? null,
            max_retries: overrides.max_retries ?? config.max_retries ?? 0,
            request_overrides: overrides.request_overrides ?? config.request_overrides ?? {},
            injection_position: overrides.injection_position ?? config.injection_position ?? "prepend",
            endpoint_preset: overrides.endpoint_preset ?? config.endpoint_preset ?? "chat_completions",
            user_agent_mode: overrides.user_agent_mode ?? config.user_agent_mode ?? "aggregator",
            custom_user_agent: overrides.custom_user_agent ?? config.custom_user_agent ?? null,
            stream_mode_strategy: overrides.stream_mode_strategy ?? config.stream_mode_strategy ?? "passthrough",
            image_upstream_mode: overrides.image_upstream_mode ?? config.image_upstream_mode ?? "generation_reference_images_array",
            image_generation_path: overrides.image_generation_path ?? config.image_generation_path ?? "/images/generations",
            image_edit_path: overrides.image_edit_path ?? config.image_edit_path ?? "/images/edits",
            image_custom_reference_field: overrides.image_custom_reference_field ?? config.image_custom_reference_field ?? null,
            image_custom_reference_mode: overrides.image_custom_reference_mode ?? config.image_custom_reference_mode ?? "array",
            image_custom_reference_object_url_field: overrides.image_custom_reference_object_url_field ?? config.image_custom_reference_object_url_field ?? "image_url",
            image_task_poll_timeout_seconds: overrides.image_task_poll_timeout_seconds ?? config.image_task_poll_timeout_seconds ?? 300,
            image_task_poll_interval_seconds: overrides.image_task_poll_interval_seconds ?? config.image_task_poll_interval_seconds ?? 2,
            injected_messages: overrides.injected_messages ?? config.injected_messages ?? [],
            consecutive_failure_threshold: overrides.consecutive_failure_threshold ?? config.consecutive_failure_threshold ?? null,
            disable_duration_seconds: overrides.disable_duration_seconds ?? config.disable_duration_seconds ?? null,
        };
    }

    function animateConfigRowMove(configId, fromRect) {
        if (!configId || !fromRect || !areEffectsEnabled()) return;
        const targetRow = configSchemesContainer.querySelector(`tr[data-config-id="${CSS.escape(configId)}"]`);
        if (!targetRow) return;

        const toRect = targetRow.getBoundingClientRect();
        const deltaY = fromRect.top - toRect.top;
        const deltaX = fromRect.left - toRect.left;
        if (Math.abs(deltaY) < 1 && Math.abs(deltaX) < 1) return;

        const ghostTable = document.createElement("table");
        const ghostBody = document.createElement("tbody");
        const ghostRow = targetRow.cloneNode(true);
        ghostTable.className = "config-row-move-ghost";
        ghostTable.style.left = `${fromRect.left}px`;
        ghostTable.style.top = `${fromRect.top}px`;
        ghostTable.style.width = `${fromRect.width}px`;
        ghostTable.style.height = `${fromRect.height}px`;
        ghostRow.querySelectorAll("button").forEach(button => button.disabled = true);
        ghostBody.appendChild(ghostRow);
        ghostTable.appendChild(ghostBody);
        document.body.appendChild(ghostTable);

        targetRow.classList.add("config-row-move-target");
        requestAnimationFrame(() => {
            ghostTable.style.transform = `translate(${-deltaX}px, ${-deltaY}px)`;
            ghostTable.style.opacity = "0";
            targetRow.classList.remove("config-row-move-target");
        });

        ghostTable.addEventListener("transitionend", () => {
            ghostTable.remove();
        }, { once: true });
    }

    async function adjustConfigPriority(config, delta, triggerButton) {
        if (!config) return;
        const currentPriority = Number.parseInt(config.priority, 10);
        const nextPriority = Math.max(1, (Number.isNaN(currentPriority) ? 1 : currentPriority) + delta);
        if (nextPriority === currentPriority) return;

        const currentRow = triggerButton ? triggerButton.closest('tr') : null;
        const fromRect = currentRow ? currentRow.getBoundingClientRect() : null;
        if (triggerButton) triggerButton.disabled = true;
        try {
            const data = buildConfigUpdatePayload(config, { priority: nextPriority });
            const response = await authedFetch(`/admin/config/${config.id}`, { method: "PUT", body: JSON.stringify(data) });
            if (!response.ok) throw new Error((await response.json()).detail || "更新优先级失败");
            await loadConfigs({ animateConfigId: config.id, fromRect });
        } catch (err) {
            alert(`更新优先级失败: ${err.message}`);
            if (triggerButton) triggerButton.disabled = false;
        }
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

        const data = buildConfigUpdatePayload({}, {
            priority: parseInt(configPriorityInput.value, 10),
            url: configUrlInput.value,
            api_key: configKeyInput.value,
            model: configModelInput.value || null,
            max_retries: maxRetries,
            request_overrides: requestOverrides,
            injection_position: configInjectionPositionInput.value || "prepend",
            endpoint_preset: configEndpointPresetInput.value || "chat_completions",
            user_agent_mode: configUserAgentModeInput.value || "aggregator",
            custom_user_agent: configCustomUserAgentInput.value || null,
            stream_mode_strategy: configStreamModeStrategyInput.value || "passthrough",
            image_upstream_mode: configImageUpstreamModeInput.value || "generation_reference_images_array",
            image_generation_path: configImageGenerationPathInput.value || "/images/generations",
            image_edit_path: configImageEditPathInput.value || "/images/edits",
            image_custom_reference_field: configImageCustomReferenceFieldInput.value || null,
            image_custom_reference_mode: configImageCustomReferenceModeInput.value || "array",
            image_custom_reference_object_url_field: configImageCustomReferenceObjectUrlFieldInput.value || "image_url",
            image_task_poll_timeout_seconds: configImageTaskPollTimeoutInput.value ? parseInt(configImageTaskPollTimeoutInput.value, 10) : 300,
            image_task_poll_interval_seconds: configImageTaskPollIntervalInput.value ? parseFloat(configImageTaskPollIntervalInput.value) : 2,
            injected_messages: getInjectedMessagesFromEditor(),
            consecutive_failure_threshold: configFailureThresholdInput.value ? parseInt(configFailureThresholdInput.value, 10) : null,
            disable_duration_seconds: configDisableDurationInput.value ? parseInt(configDisableDurationInput.value, 10) : null,
        });
        
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

    function updateImageOptionsVisibility() {
        if (!imageOptionsGroup || !configEndpointPresetInput || !configImageUpstreamModeInput) return;
        const isImagesPreset = configEndpointPresetInput.value === "images_generations";
        imageOptionsGroup.classList.toggle("hidden", !isImagesPreset);
        const isCustom = configImageUpstreamModeInput.value === "custom";
        const isObjectArray = isCustom && configImageCustomReferenceModeInput && configImageCustomReferenceModeInput.value === "object_array";
        imageCustomOptionEls.forEach(el => el.classList.toggle("hidden", !isCustom));
        imageCustomObjectOptionEls.forEach(el => el.classList.toggle("hidden", !isObjectArray));
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

    function formatImageMode(config) {
        if ((config.endpoint_preset || "chat_completions") !== "images_generations") return "";
        const mode = config.image_upstream_mode || "generation_reference_images_array";
        const labelMap = {
            openai_edit_image: "OpenAI Edit multipart",
            generation_images_array: "Gen + images[]",
            generation_ref_assets_array: "Gen + ref_assets[]",
            generation_reference_images_array: "Gen + reference_images[]",
            custom: "自定义图片模式"
        };
        return ` / ${labelMap[mode] || mode}`;
    }

    function formatEndpointPreset(preset) {
        const normalized = preset || "chat_completions";
        if (normalized === "images_generations") return "Images Generations (/images/generations)";
        return "Chat Completions (/chat/completions)";
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
        return getJsonStorageMap(CONFIG_COLLAPSE_STORAGE_KEY);
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

    function initStatsCollapseState() {
        const state = getJsonStorageMap(STATS_COLLAPSE_STORAGE_KEY);
        document.querySelectorAll(".stats-collapse-card[data-stats-collapse-key]").forEach(details => {
            const key = details.dataset.statsCollapseKey;
            if (Object.prototype.hasOwnProperty.call(state, key)) {
                details.open = !!state[key];
            }
            details.addEventListener("toggle", () => {
                setJsonStorageMapValue(STATS_COLLAPSE_STORAGE_KEY, key, details.open);
            });
        });
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
        initEffectsPreference();
        initButtonRippleEffects();
        initTheme();
        initStatsCollapseState();
        loginButton.addEventListener("click", handleLogin);
        adminKeyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });
        logoutButton.addEventListener("click", () => {
            clearStoredAdminKey();
            showLogin("您已退出登录", { clearPersistent: true });
        });

        function updateConfigBackToTopVisibility() {
            if (!configBackToTopButton) return;
            const activeTab = document.querySelector(".tab-button.active");
            const isConfigTab = activeTab && activeTab.dataset.tab === "config";
            configBackToTopButton.classList.toggle("hidden", !isConfigTab);
        }

        tabs.forEach(tab => {
            tab.addEventListener("click", () => {
                tabs.forEach(t => t.classList.remove("active"));
                tabContents.forEach(c => c.classList.remove("active"));
                tab.classList.add("active");
                document.getElementById(tab.dataset.tab + "-tab").classList.add("active");
                updateConfigBackToTopVisibility();
            });
        });

        if (configBackToTopButton) {
            configBackToTopButton.addEventListener("click", () => {
                window.scrollTo({ top: 0, behavior: areEffectsEnabled() ? "smooth" : "auto" });
            });
            updateConfigBackToTopVisibility();
        }

        statsByConfigBody.addEventListener("click", async (e) => {
            const button = e.target.closest(".unblock-config-btn");
            if (!button) return;
            button.disabled = true;
            button.textContent = "解除中...";
            await unblockConfig(button.dataset.configId);
        });

        if (statsByIpBody) {
            statsByIpBody.addEventListener("click", async (e) => {
                const toggleButton = e.target.closest(".ip-path-toggle-btn");
                if (toggleButton) {
                    const ip = toggleButton.dataset.ip;
                    const detailRow = statsByIpBody.querySelector(`.ip-path-detail-row[data-ip="${CSS.escape(ip)}"]`);
                    if (!detailRow) return;
                    const willOpen = detailRow.classList.contains("hidden");
                    detailRow.classList.toggle("hidden", !willOpen);
                    toggleButton.textContent = willOpen ? "收起" : "路径";
                    toggleButton.setAttribute("aria-expanded", String(willOpen));
                    setIpPathDetailOpen(ip, willOpen);
                    return;
                }

                const button = e.target.closest(".unblock-ip-btn");
                if (!button) return;
                button.disabled = true;
                button.textContent = "解除中...";
                await unblockIp(button.dataset.ip);
            });
        }

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
        configEndpointPresetInput.addEventListener("change", updateImageOptionsVisibility);
        configImageUpstreamModeInput.addEventListener("change", updateImageOptionsVisibility);
        configImageCustomReferenceModeInput.addEventListener("change", updateImageOptionsVisibility);
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
                    sessionStorage.setItem(SESSION_ADMIN_KEY_STORAGE_KEY, adminKey);
                    if (rememberLoginCheckbox) {
                        rememberLoginCheckbox.checked = !!localStorage.getItem(PERSISTENT_ADMIN_KEY_STORAGE_KEY);
                    }
                    showApp();
                } else {
                    showLogin("会话已过期，请重新登录", { clearPersistent: true });
                }
            })();
        } else {
            showLogin();
        }
    }

    init();
});