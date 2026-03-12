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

  document.addEventListener('DOMContentLoaded', function () {
    initMobileSidebar();
    initSectionTabs();
    initAsyncFormFeedback();
  });

  document.body.addEventListener('htmx:afterSwap', function () {
    initSectionTabs();
    initAsyncFormFeedback();
  });
})();
