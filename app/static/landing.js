(function () {
  var THEME_KEY = "colorMode";
  var LIGHT = "light";
  var DARK = "dark";

  function byId(id) {
    return document.getElementById(id);
  }

  function normalizeTheme(theme) {
    return theme === LIGHT ? LIGHT : DARK;
  }

  function storedTheme() {
    try {
      return normalizeTheme(localStorage.getItem(THEME_KEY));
    } catch (_) {
      return normalizeTheme(document.documentElement.getAttribute("data-theme"));
    }
  }

  function applyTheme(theme, persist) {
    var next = normalizeTheme(theme);

    document.documentElement.dataset.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    document.documentElement.style.colorScheme = next;

    if (document.body) {
      document.body.dataset.theme = next;
      document.body.setAttribute("data-theme", next);
    }

    var toggle = byId("themeToggle");
    if (toggle) {
      toggle.setAttribute("aria-pressed", next === LIGHT ? "true" : "false");
      toggle.setAttribute("aria-label", next === LIGHT ? "Switch to dark theme" : "Switch to light theme");
      toggle.setAttribute("title", next === LIGHT ? "Switch to dark theme" : "Switch to light theme");
    }

    if (persist) {
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (_) {}
    }

    try {
      window.dispatchEvent(new CustomEvent("app:themechange", { detail: { theme: next } }));
    } catch (_) {}

    return next;
  }

  function setHidden(el, hidden) {
    if (!el) return;
    el.hidden = hidden;
  }

  function setNotice(el, text) {
    if (!el) return;
    el.textContent = text || "";
    setHidden(el, !text);
  }

  function safeUrl(url) {
    var text = typeof url === "string" ? url : "";
    var backslash = String.fromCharCode(92);

    return (
      text.indexOf("/") === 0 &&
      text.indexOf("//") !== 0 &&
      text.indexOf(backslash) < 0 &&
      text.indexOf(String.fromCharCode(13)) < 0 &&
      text.indexOf(String.fromCharCode(10)) < 0
    );
  }

  function firstAllowedPage(data) {
    var permissions = new Set(
      String(data.permissions || "")
        .split(",")
        .map(function (value) {
          return value.trim();
        })
        .filter(Boolean)
    );

    var pages = [
      ["/dashboard", "page_dashboard"],
      ["/pos", "page_pos"],
      ["/farm/", "page_farm"],
      ["/production/", "page_production"],
      ["/inventory/", "page_inventory"],
      ["/products/", "page_products"],
      ["/customers-mgmt/", "page_customers"],
      ["/suppliers/", "page_suppliers"],
      ["/receive/", "page_receive_products"],
      ["/import", "page_import"],
      ["/reports/", "page_reports"],
      ["/b2b/", "page_b2b"],
      ["/hr/", "page_hr"],
      ["/accounting/", "page_accounting"],
      ["/expenses/", "page_expenses"],
      ["/carbon/", "page_carbon"]
    ];

    if (data.role === "admin") return "/dashboard";

    var match = pages.find(function (page) {
      return permissions.has(page[1]);
    });

    return match ? match[0] : "/home";
  }

  function enhanceAnchorScrolling() {
    document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
      anchor.addEventListener("click", function (event) {
        var targetId = anchor.getAttribute("href");
        if (!targetId || targetId === "#") return;

        var target = document.querySelector(targetId);
        if (!target) return;

        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme(), false);
    enhanceAnchorScrolling();

    var themeToggle = byId("themeToggle");
    var overlay = byId("loginOverlay");
    var modal = overlay ? overlay.querySelector(".login-modal") : null;
    var closeButton = byId("modalClose");
    var loginForm = byId("loginForm");
    var emailInput = byId("emailInp");
    var passwordInput = byId("pwdInp");
    var togglePassword = byId("togglePassword");
    var submitButton = byId("submitBtn");
    var errorEl = byId("errEl");
    var noticeEl = byId("noticeEl");
    var previousFocus = null;
    var closeTimer = null;

    function focusableModalElements() {
      if (!modal) return [];

      return Array.prototype.slice.call(
        modal.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter(function (el) {
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      });
    }

    function openModal() {
      if (!overlay) return;

      clearTimeout(closeTimer);
      previousFocus = document.activeElement;
      overlay.hidden = false;
      document.body.classList.add("modal-open");

      window.requestAnimationFrame(function () {
        overlay.classList.add("open");
      });

      setTimeout(function () {
        if (emailInput) emailInput.focus();
      }, 80);
    }

    function closeModal() {
      if (!overlay) return;

      overlay.classList.remove("open");
      document.body.classList.remove("modal-open");

      closeTimer = setTimeout(function () {
        overlay.hidden = true;
      }, 180);

      if (previousFocus && typeof previousFocus.focus === "function") {
        previousFocus.focus();
      }
    }

    if (themeToggle) {
      themeToggle.addEventListener("click", function () {
        var current = normalizeTheme(document.documentElement.getAttribute("data-theme"));
        applyTheme(current === DARK ? LIGHT : DARK, true);
      });
    }

    document.querySelectorAll("[data-open-login]").forEach(function (button) {
      button.addEventListener("click", openModal);
    });

    if (closeButton) {
      closeButton.addEventListener("click", closeModal);
    }

    if (overlay) {
      overlay.addEventListener("click", function (event) {
        if (event.target === overlay) closeModal();
      });
    }

    document.addEventListener("keydown", function (event) {
      if (!overlay || overlay.hidden) return;

      if (event.key === "Escape") {
        event.preventDefault();
        closeModal();
        return;
      }

      if (event.key !== "Tab") return;

      var focusables = focusableModalElements();
      if (!focusables.length) return;

      var first = focusables[0];
      var last = focusables[focusables.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    if (togglePassword && passwordInput) {
      togglePassword.addEventListener("click", function () {
        var showing = passwordInput.type === "text";
        passwordInput.type = showing ? "password" : "text";
        togglePassword.setAttribute("aria-pressed", showing ? "false" : "true");
        togglePassword.setAttribute("aria-label", showing ? "Show password" : "Hide password");
        passwordInput.focus();
      });
    }

    if (new URLSearchParams(window.location.search).get("reason") === "expired") {
      setHidden(noticeEl, false);
      openModal();
    }

    if (loginForm) {
      loginForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        setNotice(errorEl, "");

        var email = emailInput ? emailInput.value.trim() : "";
        var password = passwordInput ? passwordInput.value : "";

        if (!email || !password) {
          setNotice(errorEl, "Please enter both email and password.");
          return;
        }

        if (submitButton) {
          submitButton.disabled = true;
          submitButton.classList.add("loading");
        }

        try {
          var response = await fetch("/auth/login", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email: email, password: password })
          });

          var data = {};

          try {
            data = await response.json();
          } catch (_) {}

          if (!response.ok) {
            setNotice(errorEl, data.detail || "Invalid email or password.");

            if (submitButton) {
              submitButton.disabled = false;
              submitButton.classList.remove("loading");
            }

            return;
          }

          var next = new URLSearchParams(window.location.search).get("next");
          window.location.href = safeUrl(next) ? next : firstAllowedPage(data);
        } catch (_) {
          setNotice(errorEl, "Connection error. Please try again.");

          if (submitButton) {
            submitButton.disabled = false;
            submitButton.classList.remove("loading");
          }
        }
      });
    }
  });
})();