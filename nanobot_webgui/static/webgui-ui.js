(function () {
  function initMobileSidebar() {
    const toggle = document.getElementById('mobile-nav-toggle');
    if (!toggle || toggle.dataset.mobileSidebarReady === 'true') {
      return;
    }
    toggle.dataset.mobileSidebarReady = 'true';

    document.querySelectorAll('.sidebar .nav a').forEach((link) => {
      link.addEventListener('click', function () {
        toggle.checked = false;
      });
    });

    window.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        toggle.checked = false;
      }
    });

    window.matchMedia('(min-width: 981px)').addEventListener('change', function (event) {
      if (event.matches) {
        toggle.checked = false;
      }
    });
  }

  function activateTab(groupName, nextTab) {
    document.querySelectorAll('[data-tab-group="' + groupName + '"] [data-tab-target]').forEach((button) => {
      const active = button.dataset.tabTarget === nextTab;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    document.querySelectorAll('[data-tab-panel-group="' + groupName + '"]').forEach((panel) => {
      panel.classList.toggle('is-active', panel.dataset.tabPanel === nextTab);
    });
  }

  function initSectionTabs() {
    document.querySelectorAll('[data-tab-group]').forEach((tabGroup) => {
      if (tabGroup.dataset.tabsReady === 'true') {
        return;
      }
      const groupName = tabGroup.dataset.tabGroup;
      const buttons = tabGroup.querySelectorAll('[data-tab-target]');
      const defaultTab =
        tabGroup.dataset.defaultTab ||
        (buttons.length ? buttons[0].dataset.tabTarget : null);

      if (!groupName || !defaultTab) {
        return;
      }

      tabGroup.dataset.tabsReady = 'true';
      activateTab(groupName, defaultTab);

      buttons.forEach((button) => {
        button.addEventListener('click', function () {
          activateTab(groupName, button.dataset.tabTarget);
        });
      });
    });
  }

  function setActionFeedbackVisible(message) {
    const feedback = document.getElementById('action-feedback');
    if (!feedback) {
      return;
    }
    const text = feedback.querySelector('.action-feedback-text');
    if (text) {
      text.textContent = message || 'Working...';
    }
    feedback.classList.add('is-visible');
  }

  function initAsyncFormFeedback() {
    document.querySelectorAll('form[data-loading-label]').forEach((form) => {
      if (form.dataset.feedbackReady === 'true') {
        return;
      }
      form.dataset.feedbackReady = 'true';

      form.addEventListener('submit', function () {
        const button = form.querySelector('button[type="submit"]');
        if (!button || button.disabled) {
          return;
        }
        if (!button.dataset.originalLabel) {
          button.dataset.originalLabel = button.textContent;
        }
        button.disabled = true;
        button.textContent = form.dataset.loadingLabel || 'Working...';
        setActionFeedbackVisible(form.dataset.feedbackMessage || form.dataset.loadingLabel || 'Working...');
      });
    });
  }

  function setChatComposerBusy(form, busy) {
    if (!form) {
      return;
    }
    const uploadTrigger = form.querySelector('[data-testid="chat-upload-trigger"]');
    form.classList.toggle('is-busy', busy);
    if (uploadTrigger) {
      uploadTrigger.classList.toggle('is-disabled', busy);
      uploadTrigger.setAttribute('aria-disabled', busy ? 'true' : 'false');
    }
  }

  function initChatComposerState() {
    document.querySelectorAll('form[data-chat-async="message"]').forEach((form) => {
      if (form.dataset.chatBusyReady === 'true') {
        return;
      }
      form.dataset.chatBusyReady = 'true';

      form.addEventListener('submit', function () {
        setChatComposerBusy(form, true);
      });

      form.addEventListener('htmx:afterRequest', function () {
        setChatComposerBusy(form, false);
      });

      form.addEventListener('htmx:sendError', function () {
        setChatComposerBusy(form, false);
      });

      form.addEventListener('htmx:responseError', function () {
        setChatComposerBusy(form, false);
      });
    });
  }

  function initCopyButtons() {
    document.querySelectorAll('[data-copy-text]').forEach((button) => {
      if (button.dataset.copyReady === 'true') {
        return;
      }
      button.dataset.copyReady = 'true';
      button.addEventListener('click', async function () {
        const text = button.dataset.copyText || '';
        if (!text) {
          return;
        }
        const originalLabel = button.textContent;
        try {
          await navigator.clipboard.writeText(text);
          button.textContent = 'Copied';
          setTimeout(() => {
            button.textContent = originalLabel;
          }, 1200);
        } catch (_error) {
          button.textContent = 'Failed';
          setTimeout(() => {
            button.textContent = originalLabel;
          }, 1200);
        }
      });
    });
  }

  function scorePassword(value) {
    let score = 0;
    if (value.length >= 8) score += 1;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1;
    if (/\d/.test(value)) score += 1;
    if (/[^A-Za-z0-9]/.test(value)) score += 1;
    if (value.length >= 14) score += 1;
    if (score <= 1) return { bars: 1, label: 'Password strength: weak', tone: 'weak' };
    if (score === 2) return { bars: 2, label: 'Password strength: fair', tone: 'fair' };
    if (score === 3) return { bars: 3, label: 'Password strength: good', tone: 'good' };
    if (score === 4) return { bars: 4, label: 'Password strength: strong', tone: 'strong' };
    return { bars: 4, label: 'Password strength: very strong', tone: 'very-strong' };
  }

  function initPasswordStrengthIndicators() {
    document.querySelectorAll('[data-password-strength]').forEach((container) => {
      if (container.dataset.passwordStrengthReady === 'true') {
        return;
      }
      container.dataset.passwordStrengthReady = 'true';

      const inputId = container.dataset.passwordInput || '';
      const input = inputId ? document.getElementById(inputId) : null;
      const bars = Array.from(container.querySelectorAll('[data-strength-bar]'));
      const label = container.querySelector('[data-strength-label]');

      if (!input || !bars.length || !label) {
        return;
      }

      const render = () => {
        const value = input.value || '';
        if (!value) {
          bars.forEach((bar) => {
            bar.className = 'password-strength-bar';
          });
          label.textContent = 'Use at least 8 characters, upper/lower case, number, and symbol.';
          return;
        }

        const result = scorePassword(value);
        bars.forEach((bar, index) => {
          bar.className = 'password-strength-bar';
          if (index < result.bars) {
            bar.classList.add('is-active', result.tone);
          }
        });
        label.textContent = result.label;
      };

      input.addEventListener('input', render);
      render();
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initMobileSidebar();
    initSectionTabs();
    initAsyncFormFeedback();
    initChatComposerState();
    initCopyButtons();
    initPasswordStrengthIndicators();
  });

  document.body.addEventListener('htmx:afterSwap', function () {
    initSectionTabs();
    initAsyncFormFeedback();
    initChatComposerState();
    initCopyButtons();
    initPasswordStrengthIndicators();
  });
})();
