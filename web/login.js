(() => {
  const form = document.querySelector("#login-form");
  const username = document.querySelector("#login-username");
  const password = document.querySelector("#login-password");
  const submit = document.querySelector("#login-submit");
  const error = document.querySelector("#login-error");
  const toggle = document.querySelector("#toggle-password");
  if (!form || !username || !password || !submit || !error) return;

  toggle?.addEventListener("click", () => {
    const visible = password.type === "text";
    password.type = visible ? "password" : "text";
    toggle.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    error.textContent = "";
    const name = username.value.trim();
    const secret = password.value;
    if (!name || !secret) {
      error.textContent = "请输入用户名和密码";
      return;
    }
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    submit.textContent = "登录中…";
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: name, password: secret }),
        signal: AbortSignal.timeout(30_000)
      });
      let result = {};
      try { result = await response.json(); } catch {}
      if (!response.ok || !result.ok) throw new Error(result.error || "登录失败，请检查账号和密码");
      window.location.replace("/admin.html");
    } catch (loginError) {
      error.textContent = loginError?.name === "TimeoutError" ? "登录响应超时，请重试；服务器正在处理时不要重复提交。" : loginError instanceof Error ? loginError.message : "登录失败，请稍后重试";
      password.focus();
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.textContent = "登录管理台";
    }
  });
})();
