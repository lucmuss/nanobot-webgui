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

  document.addEventListener('DOMContentLoaded', function () {
    initMobileSidebar();
    initSectionTabs();
  });

  document.body.addEventListener('htmx:afterSwap', function () {
    initSectionTabs();
  });
})();
