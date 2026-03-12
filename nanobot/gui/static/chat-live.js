(function () {
  let statusResetTimer = null;
  let lastRequestKind = 'message';
  let lastRequestHadAttachment = false;

  function byTestId(id) {
    return document.querySelector('[data-testid="' + id + '"]');
  }

  function getChatShell() {
    return byTestId('chat-live-shell');
  }

  function getChatHistory() {
    return byTestId('chat-history');
  }

  function setIndicatorMessage(message, state) {
    const indicator = byTestId('chat-live-indicator');
    if (indicator) {
      indicator.textContent = message;
      if (state) {
        indicator.dataset.state = state;
      }
    }
  }

  function scheduleReadyState() {
    if (statusResetTimer) {
      clearTimeout(statusResetTimer);
    }
    statusResetTimer = window.setTimeout(function () {
      setIndicatorMessage('Ready', 'ready');
    }, 1800);
  }

  function setBusyState(form, busy) {
    const buttons = form.querySelectorAll('button');
    buttons.forEach((button) => {
      if (busy) {
        if (!button.dataset.originalLabel) {
          button.dataset.originalLabel = button.textContent || '';
        }
        button.disabled = true;
      } else {
        button.disabled = false;
        if (button.dataset.originalLabel) {
          button.textContent = button.dataset.originalLabel;
        }
      }
    });
  }

  function messageForKind(kind) {
    switch (kind) {
      case 'upload':
        return 'Uploading file and sending...';
      case 'clear':
        return 'Clearing chat history...';
      case 'template':
        return 'Sending template prompt...';
      case 'quick':
        return 'Sending quick prompt...';
      default:
        return lastRequestHadAttachment ? 'Uploading file and sending...' : 'Sending message...';
    }
  }

  function buttonLabelForKind(kind) {
    switch (kind) {
      case 'upload':
        return 'Uploading...';
      case 'clear':
        return 'Clearing...';
      default:
        return lastRequestHadAttachment ? 'Uploading...' : 'Sending...';
    }
  }

  function successMessageForKind(kind) {
    switch (kind) {
      case 'upload':
        return 'File sent to chat.';
      case 'clear':
        return 'Chat history cleared.';
      case 'template':
        return 'Template sent.';
      case 'quick':
        return 'Prompt sent.';
      default:
        return lastRequestHadAttachment ? 'File sent to chat.' : 'Message sent.';
    }
  }

  function scrollChatToBottom() {
    const history = getChatHistory();
    if (!history) {
      return;
    }
    history.scrollTop = history.scrollHeight;
  }

  function focusComposer() {
    const textarea = byTestId('chat-message');
    if (textarea) {
      textarea.focus({ preventScroll: true });
      const valueLength = textarea.value.length;
      textarea.setSelectionRange(valueLength, valueLength);
    }
  }

  function enhanceForm(form) {
    const kind = form.dataset.chatAsync || 'message';
    const submitButton = form.querySelector('button[type="submit"]');
    if (!submitButton) {
      return;
    }
    if (!submitButton.dataset.originalLabel) {
      submitButton.dataset.originalLabel = submitButton.textContent || '';
    }
    submitButton.textContent = buttonLabelForKind(kind);
  }

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    const form = event.detail.elt.closest('form');
    if (!form || !form.dataset.chatAsync) {
      return;
    }
    lastRequestKind = form.dataset.chatAsync || 'message';
    const attachment = form.querySelector('input[type="file"]');
    lastRequestHadAttachment = Boolean(attachment && attachment.files && attachment.files.length > 0);
    setIndicatorMessage(messageForKind(form.dataset.chatAsync), 'busy');
    setBusyState(form, true);
    enhanceForm(form);
  });

  document.body.addEventListener('htmx:responseError', function () {
    setIndicatorMessage('Chat request failed.', 'error');
  });

  document.body.addEventListener('htmx:afterSwap', function (event) {
    if (!(event.target instanceof HTMLElement) || event.target.id !== 'chat-live-shell') {
      return;
    }
    scrollChatToBottom();
    focusComposer();
    setIndicatorMessage(successMessageForKind(lastRequestKind), 'success');
    scheduleReadyState();
  });

  document.addEventListener('DOMContentLoaded', function () {
    if (getChatShell()) {
      scrollChatToBottom();
      setIndicatorMessage('Ready', 'ready');
    }
  });
})();
