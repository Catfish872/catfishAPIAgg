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
    const configFailureThresholdInput = document.getElementById("config-failure-threshold");
    const configDisableDurationInput = document.getElementById("config-disable-duration");
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

    let adminKey = sessionStorage.getItem("catfishAdminKey");
    let statsInterval, logsInterval;
    let allSchemesCache = {}; // 缓存配置数据，用于统计显示

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

            schemeNames.sort().forEach(schemeName => {
                const configs = schemes[schemeName];
                const schemeBlock = document.createElement("div");
                schemeBlock.className = "scheme-block";
                
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
                                <td><small>${config.id}</small></td>
                                <td>
                                    <button class="button edit-btn">编辑</button>
                                    <button class="button danger delete-btn">删除</button>
                                </td>
                            </tr>
                        `;
                    });
                } else {
                    tableRows = `<tr><td colspan="7">该方案下没有配置项</td></tr>`;
                }

                schemeBlock.innerHTML = `
                    <div class="scheme-header">
                        <h3>${schemeName} <small>(Model Name)</small></h3>
                    </div>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th>优先级</th>
                                    <th>URL</th>
                                    <th>Key (遮罩)</th>
                                    <th>覆盖 Model</th>
                                    <th>熔断设置 (失败/时长)</th>
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

    function resetForm() {
        configForm.reset();
        configIdInput.value = "";
        formTitle.textContent = "添加新配置";
        configSchemeInput.disabled = false;
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
        cancelButton.classList.remove("hidden");
        configForm.scrollIntoView({ behavior: "smooth" });
    }

    // [重构] 处理表单提交
    async function handleFormSubmit(e) {
        e.preventDefault();
        
        const configId = configIdInput.value;
        const isEditing = !!configId;
        
        const data = {
            priority: parseInt(configPriorityInput.value, 10),
            url: configUrlInput.value,
            api_key: configKeyInput.value,
            model: configModelInput.value || null,
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