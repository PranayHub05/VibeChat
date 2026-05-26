document.addEventListener('DOMContentLoaded', () => {

  let ws = null;
  let currentUser = '';
  let currentRoom = '';
  let sidebarOpen = false;
  let intentionalClose = false;
  let reconnectAttempts = 0;
  const MAX_RECONNECT = 5;
  const RECONNECT_DELAY = 3000;

  const landingScreen     = document.getElementById('landing-screen');
  const chatScreen        = document.getElementById('chat-screen');
  const usernameInput     = document.getElementById('username-input');
  const usernameError     = document.getElementById('username-error');
  const roomCodeInput     = document.getElementById('room-code-input');
  const roomCodeError     = document.getElementById('room-code-error');
  const createBtn         = document.getElementById('create-btn');
  const joinBtn           = document.getElementById('join-btn');
  const roomCodeDisplay   = document.getElementById('room-code-display');
  const copyCodeBtn       = document.getElementById('copy-code-btn');
  const leaveBtn          = document.getElementById('leave-btn');
  const messagesContainer = document.getElementById('messages');
  const messageInput      = document.getElementById('message-input');
  const sendBtn           = document.getElementById('send-btn');
  const userList           = document.getElementById('user-list');
  const onlineCount       = document.getElementById('online-count');
  const userCountBadge    = document.getElementById('user-count-badge');
  const sidebarToggle     = document.getElementById('sidebar-toggle');
  const sidebar           = document.getElementById('sidebar');
  const sidebarOverlay    = document.getElementById('sidebar-overlay');
  const copyToast         = document.getElementById('copy-toast');

  function showScreen(screenId) {
    [landingScreen, chatScreen].forEach(s => s.classList.remove('active'));
    document.getElementById(screenId).classList.add('active');
  }

  showScreen('landing-screen');

  function escapeHtml(str) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(str).replace(/[&<>"']/g, c => map[c]);
  }

  function formatTime(isoString) {
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return '';
      return d.getHours().toString().padStart(2, '0') + ':' +
             d.getMinutes().toString().padStart(2, '0');
    } catch {
      return '';
    }
  }

  function showError(element, inputEl, message) {
    element.textContent = message;
    inputEl.classList.add('input-error-state', 'shake');
    inputEl.addEventListener('animationend', () => inputEl.classList.remove('shake'), { once: true });
    setTimeout(() => {
      element.textContent = '';
      inputEl.classList.remove('input-error-state');
    }, 3000);
  }

  function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  roomCodeInput.addEventListener('input', () => {
    roomCodeInput.value = roomCodeInput.value.replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  });

  createBtn.addEventListener('click', async () => {
    const username = usernameInput.value.trim();
    if (!username) {
      showError(usernameError, usernameInput, 'Please enter a username.');
      return;
    }

    createBtn.disabled = true;
    try {
      const res = await fetch('/api/rooms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showError(usernameError, usernameInput, err.detail || 'Failed to create room.');
        return;
      }

      const data = await res.json();
      currentUser = username;
      currentRoom = data.code;
      connectWebSocket();
      showScreen('chat-screen');
    } catch (e) {
      showError(usernameError, usernameInput, 'Network error. Please try again.');
    } finally {
      createBtn.disabled = false;
    }
  });

  joinBtn.addEventListener('click', async () => {
    const username = usernameInput.value.trim();
    const code = roomCodeInput.value.trim().toUpperCase();

    if (!username) {
      showError(usernameError, usernameInput, 'Please enter a username.');
      return;
    }
    if (!code) {
      showError(roomCodeError, roomCodeInput, 'Please enter a room code.');
      return;
    }

    joinBtn.disabled = true;
    try {
      const res = await fetch(`/api/rooms/${encodeURIComponent(code)}`);

      if (!res.ok) {
        showError(roomCodeError, roomCodeInput, 'Network error. Please try again.');
        return;
      }

      const roomData = await res.json();
      if (!roomData.exists) {
        showError(roomCodeError, roomCodeInput, 'Room not found. Check the code.');
        return;
      }

      currentUser = username;
      currentRoom = code;
      connectWebSocket();
      showScreen('chat-screen');
    } catch (e) {
      showError(roomCodeError, roomCodeInput, 'Network error. Please try again.');
    } finally {
      joinBtn.disabled = false;
    }
  });

  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws/${encodeURIComponent(currentRoom)}?username=${encodeURIComponent(currentUser)}`;

    ws = new WebSocket(url);
    intentionalClose = false;
    reconnectAttempts = 0;

    ws.onopen = () => {
      messagesContainer.innerHTML = '';
      roomCodeDisplay.textContent = currentRoom;
      reconnectAttempts = 0;
    };

    ws.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (data.type) {
        case 'chat':
          appendChatMessage(data);
          break;
        case 'system':
          appendSystemMessage(data);
          break;
        case 'user_list':
          updateUserList(data.users);
          break;
        case 'history':
          if (Array.isArray(data.messages)) {
            data.messages.forEach(msg => {
              if (msg.type === 'chat') appendChatMessage(msg, true);
              else if (msg.type === 'system') appendSystemMessage(msg, true);
            });
          }
          scrollToBottom();
          break;
        default:
          break;
      }
    };

    ws.onclose = () => {
      if (intentionalClose) return;

      appendSystemMessage({ content: 'Connection lost. Reconnecting…' });

      if (reconnectAttempts < MAX_RECONNECT) {
        reconnectAttempts++;
        setTimeout(() => connectWebSocket(), RECONNECT_DELAY);
      } else {
        appendSystemMessage({ content: 'Unable to reconnect. Please reload the page.' });
      }
    };

    ws.onerror = (err) => {
      console.error('WebSocket error:', err);
    };
  }

  function appendChatMessage(data, skipScroll) {
    const isOwn = data.username === currentUser;
    const wrapper = document.createElement('div');
    wrapper.className = `message ${isOwn ? 'own-message' : 'other-message'}`;

    const nameEl = document.createElement('span');
    nameEl.className = 'message-username';
    nameEl.textContent = escapeHtml(data.username || '');
    wrapper.appendChild(nameEl);

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = data.content || '';
    wrapper.appendChild(bubble);

    if (data.timestamp) {
      const timeEl = document.createElement('span');
      timeEl.className = 'message-time';
      timeEl.textContent = formatTime(data.timestamp);
      wrapper.appendChild(timeEl);
    }

    messagesContainer.appendChild(wrapper);
    if (!skipScroll) scrollToBottom();
  }

  function appendSystemMessage(data, skipScroll) {
    const div = document.createElement('div');
    div.className = 'system-message';
    div.textContent = data.content || '';
    messagesContainer.appendChild(div);
    if (!skipScroll) scrollToBottom();
  }

  function updateUserList(users) {
    userList.innerHTML = '';
    const count = Array.isArray(users) ? users.length : 0;
    onlineCount.textContent = count;
    userCountBadge.textContent = count;

    if (!Array.isArray(users)) return;

    users.forEach(username => {
      const item = document.createElement('div');
      item.className = 'user-item';

      const dot = document.createElement('span');
      dot.className = 'presence-dot';
      item.appendChild(dot);

      const name = document.createElement('span');
      name.className = 'user-item-name';
      name.textContent = escapeHtml(username);
      item.appendChild(name);

      if (username === currentUser) {
        const you = document.createElement('span');
        you.className = 'user-item-you';
        you.textContent = '(you)';
        item.appendChild(you);
      }

      userList.appendChild(item);
    });
  }

  function sendMessage() {
    const text = messageInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    ws.send(JSON.stringify({ content: text }));
    messageInput.value = '';
    messageInput.focus();
    sendBtn.disabled = true;
  }

  sendBtn.addEventListener('click', sendMessage);

  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  messageInput.addEventListener('input', () => {
    sendBtn.disabled = messageInput.value.trim().length === 0;
  });

  copyCodeBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(currentRoom).then(() => {
      copyToast.classList.remove('show');
      void copyToast.offsetWidth;
      copyToast.classList.add('show');
      setTimeout(() => copyToast.classList.remove('show'), 1600);
    }).catch(() => {
      const tmp = document.createElement('input');
      tmp.value = currentRoom;
      document.body.appendChild(tmp);
      tmp.select();
      document.execCommand('copy');
      document.body.removeChild(tmp);
      copyToast.classList.add('show');
      setTimeout(() => copyToast.classList.remove('show'), 1600);
    });
  });

  leaveBtn.addEventListener('click', () => {
    intentionalClose = true;
    if (ws) {
      ws.close();
      ws = null;
    }
    currentUser = '';
    currentRoom = '';
    messagesContainer.innerHTML = '';
    userList.innerHTML = '';
    onlineCount.textContent = '0';
    userCountBadge.textContent = '0';
    roomCodeDisplay.textContent = '------';
    showScreen('landing-screen');
  });

  sidebarToggle.addEventListener('click', () => {
    sidebarOpen = !sidebarOpen;
    sidebar.classList.toggle('open', sidebarOpen);
    sidebarOverlay.classList.toggle('visible', sidebarOpen);
  });

  sidebarOverlay.addEventListener('click', () => {
    sidebarOpen = false;
    sidebar.classList.remove('open');
    sidebarOverlay.classList.remove('visible');
  });

  usernameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (roomCodeInput.value.trim()) {
        joinBtn.click();
      } else {
        createBtn.click();
      }
    }
  });

  roomCodeInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      joinBtn.click();
    }
  });

});
