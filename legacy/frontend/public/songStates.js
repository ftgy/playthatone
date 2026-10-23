// songStates.js

// Solo define songStates si no está ya definido globalmente
if (typeof songStates === 'undefined') {
  var songStates = {};
}

const storedStates = localStorage.getItem('songStates');
if (storedStates) {
  try {
    const parsed = JSON.parse(storedStates);
    Object.assign(songStates, parsed);
  } catch (e) {
    console.warn("Failed to parse stored songStates.");
  }
}

const STATES = ['selected', 'voted', 'now_playing', 'played'];

function saveStates() {
  localStorage.setItem('songStates', JSON.stringify(songStates));
}

function updateSongState(id, newState) {
  if (!STATES.includes(newState) && newState !== 'deselected') return;

  const globalState = window.songCatalog?.[id]?.state;
  if (newState === 'voted' && ['now_playing', 'played'].includes(globalState)) {
    console.warn(`Cannot vote on "${id}" in state "${globalState}"`);
    return;
  }

  if (newState === 'deselected') {
    delete songStates[id];
  } else {
    songStates[id] = newState;
  }

  saveStates();
  if (typeof renderSong === 'function') renderSong(id);

    // 📢 Mensaje con el estado final
  console.log(`Estado de "${id}" actualizado a: ${getSongState(id)}`);
}

function getSongState(id) {
  return songStates[id] || 'deselected';
}

// 🔹 EXPO: ahora disponible para el iframe (ran.html)
window.getSongState = getSongState;

// ✅ Escuchar evento de reinicio de sesión por WebSocket
if (typeof io !== 'undefined') {
  const socket = io();
  socket.on("session_reset", () => {
    console.log("Reset event received. Clearing local state...");
    localStorage.removeItem('songStates');
    location.reload();
  });
}