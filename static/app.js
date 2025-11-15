// 确保 DOM 加载完毕后执行
document.addEventListener("DOMContentLoaded", () => {

    // --- 1. DOM 元素获取 ---
    const loginOverlay = document.getElementById("login-overlay");
    const loginBox = document.querySelector(".login-box");
    const loginButton = document.getElementById("login-button");
    const adminKeyInput = document.getElementById("admin-key-input");
    const loginError = document.getElementById("login-error");

    const topBar = document.getElementById("top-bar");
    const appContainer = document.getElementById("app-container");
    const logoutButton = document.getElementById("logout-button");

    const tabs = document.querySelectorAll(".tab-button");
    const tabContents = document.querySelectorAll(".tab-content");

    const configTableBody = document.getElementById("config-table-body");
    const configForm = document.getElementById("config-form");
    const formTitle = document.getElementById("form-title");
    const configIdInput = document.getElementById("config-id");
    const configPriorityInput = document.getElementById("config-priority");
    const configUrlInput = document.getElementById("config-url");
    const configKeyInput = document.getElementById("config-key");
    const configModelInput = document.getElementById("config-model");
    const saveButton = document.getElementById("save-button");
    const cancelButton = document.getElementById("cancel-button");

    const statTotalSuccess = document.getElementById("stat-total-success");
    const statTotalFail = document.getElementById("stat-total-fail");
    const statTodaySuccess = document.getElementById("stat-today-success");
    const statTodayFail = document.getElementById("stat-today-fail");
    const statsByConfigBody = document.getElementById("stats-by-config-body");

    const logsContent = document.getElementById("logs-content");

    let adminKey = sessionStorage.getItem("catfishAdminKey");
    let statsInterval, logsInterval;

    // --- 2. 核心功能函数 ---

    /**
     * 带认证的 fetch 封装
     * @param {string} url - 请求 URL
     * @param {object} options - fetch 选项
     * @returns {Promise<Response>}
     */
    async function authedFetch(url, options = {}) {
        if (!adminKey) {
            console.error("No admin key found");
            showLogin("会话已过期，请重新登录");
            return;
        }

        const headers = {
            ...options.headers,
            'Authorization': `Bearer ${adminKey}`
        };

        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
        }

        const response = await fetch(url, { ...options, headers });

        if (response.status === 401) {
            // 认证失败，清除 key 并显示登录
            showLogin("认证失败，请重新登录");
            return;
        }

        return response;
    }

    /**
     * 显示登录界面
     * @param {string} errorMsg - 可选的错误消息
     */
    function showLogin(errorMsg = "") {
        adminKey = null;
        sessionStorage.removeItem("catfishAdminKey");
        loginOverlay.classList.remove("hidden");
        topBar.classList.add("hidden");
        appContainer.classList.add("hidden");
        loginError.textContent = errorMsg;
        // 停止定时刷新
        if (statsInterval) clearInterval(statsInterval);
        if (logsInterval) clearInterval(logsInterval);
    }

    /**
     * 显示主应用界面
     */
    function showApp() {
        loginOverlay.classList.add("hidden");
        topBar.classList.remove("hidden");
        appContainer.classList.remove("hidden");
        loginError.textContent = "";

        // 首次加载数据
        loadAllData();

        // 设置定时刷新
        statsInterval = setInterval(loadStats, 5000); // 5秒刷新一次统计
        logsInterval = setInterval(loadLogs, 5000); // 5秒刷新一次日志
    }

    /**
     * 处理登录
     */
    async function handleLogin() {
        const key = adminKeyInput.value;
        if (!key) {
            loginError.textContent = "请输入密钥";
            return;
        }
        
        // 尝试用这个 key 请求一个受保护的端点 (例如 /admin/logs)
        try {
            const response = await fetch("/admin/logs", {
                headers: { 'Authorization': `Bearer ${key}` }
            });

            if (response.status === 401) {
                loginError.textContent = "密钥不正确";
            } else if (response.ok) {
                // 登录成功
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

    /**
     * 加载所有数据
     */
    function loadAllData() {
        loadConfigs();
        loadStats();
        loadLogs();
    }

    /**
     * 加载 API 配置
     */
    async function loadConfigs() {
        try {
            const response = await authedFetch("/admin/config");
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const configs = await response.json();

            configTableBody.innerHTML = ""; // 清空
            if (configs.length === 0) {
                configTableBody.innerHTML = `<tr><td colspan="6">尚未添加任何配置项</td></tr>`;
                return;
            }

            configs.forEach(config => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td>${config.priority}</td>
                    <td><small>${config.url}</small></td>
                    <td><small>sk-*****${config.api_key.slice(-4)}</small></td>
                    <td>${config.model || '<em>(使用原始)</em>'}</td>
                    <td><small>${config.id}</small></td>
                    <td>
                        <button class="button edit-btn" data-id="${config.id}">编辑</button>
                        <button class="button danger delete-btn" data-id="${config.id}">删除</button>
                    </td>
                `;
                // 添加事件监听
                tr.querySelector(".edit-btn").addEventListener("click", () => populateFormForEdit(config));
                tr.querySelector(".delete-btn").addEventListener("click", () => handleDeleteConfig(config.id));
                configTableBody.appendChild(tr);
            });

        } catch (err) {
            console.error("加载配置失败:", err);
            configTableBody.innerHTML = `<tr><td colspan="6" class="fail-text">加载配置失败</td></tr>`;
        }
    }

    /**
     * 加载统计数据
     */
    async function loadStats() {
        try {
            const response = await authedFetch("/admin/stats");
            if (!response || !response.ok) return; // authedFetch 会处理 401
            const stats = await response.json();
            const allConfigs = (await (await authedFetch("/admin/config")).json()) || [];

            statTotalSuccess.textContent = stats.total.success || 0;
            statTotalFail.textContent = stats.total.fail || 0;
            statTodaySuccess.textContent = stats.today.success || 0;
            statTodayFail.textContent = stats.today.fail || 0;

            // 按配置统计
            statsByConfigBody.innerHTML = ""; // 清空
            if (allConfigs.length === 0) {
                statsByConfigBody.innerHTML = `<tr><td colspan="4">没有配置项</td></tr>`;
                return;
            }

            allConfigs.forEach(config => {
                const configStat = stats.by_config_id[config.id] || { success: 0, fail: 0 };
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td><small>${config.id}</small></td>
                    <td><small>${config.url}</small></td>
                    <td class="success-text">${configStat.success}</td>
                    <td class="fail-text">${configStat.fail}</td>
                `;
                statsByConfigBody.appendChild(tr);
            });

        } catch (err) {
            console.error("加载统计失败:", err);
            // 不在循环中提示错误，避免打扰
        }
    }

    /**
     * 加载日志
     */
    async function loadLogs() {
        try {
            const response = await authedFetch("/admin/logs");
            if (!response || !response.ok) return;
            const logs = await response.json();
            
            // 倒序显示，最新的在最上面
            logsContent.textContent = logs.reverse().join("\n");
        } catch (err) {
            console.error("加载日志失败:", err);
        }
    }

    /**
     * 重置表单
     */
    function resetForm() {
        configForm.reset();
        configIdInput.value = "";
        formTitle.textContent = "添加新配置";
        cancelButton.classList.add("hidden");
    }

    /**
     * 填充表单以便编辑
     * @param {object} config - 配置对象
     */
    function populateFormForEdit(config) {
        formTitle.textContent = "编辑配置项";
        configIdInput.value = config.id;
        configPriorityInput.value = config.priority;
        configUrlInput.value = config.url;
        configKeyInput.value = config.api_key; // 注意：这会显示 Key
        configModelInput.value = config.model;
        cancelButton.classList.remove("hidden");
        // 滚动到表单
        configForm.scrollIntoView({ behavior: "smooth" });
    }

    /**
     * 处理表单提交 (创建或更新)
     * @param {Event} e - 事件对象
     */
    async function handleFormSubmit(e) {
        e.preventDefault();
        
        const configId = configIdInput.value;
        const isEditing = !!configId;
        
        const data = {
            priority: parseInt(configPriorityInput.value, 10),
            url: configUrlInput.value,
            api_key: configKeyInput.value,
            model: configModelInput.value || null // 如果为空字符串，发送 null
        };

        const url = isEditing ? `/admin/config/${configId}` : "/admin/config";
        const method = isEditing ? "PUT" : "POST";

        try {
            const response = await authedFetch(url, {
                method: method,
                body: JSON.stringify(data)
            });

            if (response.ok) {
                resetForm();
                loadConfigs(); // 重新加载配置
                loadStats(); // 重新加载统计 (可能会有新 ID)
            } else {
                const error = await response.json();
                alert(`保存失败: ${error.detail || response.statusText}`);
            }
        } catch (err) {
            alert(`保存时发生错误: ${err.message}`);
        }
    }

    /**
     * 处理删除配置
     * @param {string} configId - 要删除的配置 ID
     */
    async function handleDeleteConfig(configId) {
        if (!confirm("确定要删除这个配置项吗？")) {
            return;
        }

        try {
            const response = await authedFetch(`/admin/config/${configId}`, {
                method: "DELETE"
            });

            if (response.ok) {
                loadConfigs(); // 重新加载
                loadStats(); // 重新加载 (清除已删除的)
            } else {
                const error = await response.json();
                alert(`删除失败: ${error.detail || response.statusText}`);
            }
        } catch (err) {
            alert(`删除时发生错误: ${err.message}`);
        }
    }

    // --- 3. 初始化和事件监听 ---

    function init() {
        // 登录/登出
        loginButton.addEventListener("click", handleLogin);
        adminKeyInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });
        logoutButton.addEventListener("click", () => showLogin("您已退出登录"));

        // 选项卡切换
        tabs.forEach(tab => {
            tab.addEventListener("click", () => {
                // 移除所有 active
                tabs.forEach(t => t.classList.remove("active"));
                tabContents.forEach(c => c.classList.remove("active"));
                // 添加 active
                tab.classList.add("active");
                document.getElementById(tab.dataset.tab + "-tab").classList.add("active");
            });
        });

        // 表单
        configForm.addEventListener("submit", handleFormSubmit);
        cancelButton.addEventListener("click", resetForm);

        // 检查初始登录状态
        if (adminKey) {
            // 尝试验证 key
            (async () => {
                const response = await fetch("/admin/logs", { // 用一个轻量级请求验证
                    headers: { 'Authorization': `Bearer ${adminKey}` }
                });
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

    // 启动应用
    init();
});