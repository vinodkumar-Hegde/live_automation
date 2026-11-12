'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('dt', {
  config: () => ipcRenderer.invoke('capture:config'),
  joined: () => ipcRenderer.send('capture:joined'),
  failed: message => ipcRenderer.send('capture:failed', message),
  ended: () => ipcRenderer.send('capture:ended'),
  note: text => ipcRenderer.send('capture:note', text),
  partBegin: () => ipcRenderer.invoke('capture:part-begin'),
  chunk: data => ipcRenderer.send('capture:chunk', data),
  partEnd: reason => ipcRenderer.invoke('capture:part-end', reason),
  onCommand: handler => ipcRenderer.on('capture:command', (_event, action) => handler(action)),
});
