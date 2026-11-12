'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('office', {
  state: () => ipcRenderer.invoke('ui:state'),
  pair: value => ipcRenderer.invoke('ui:pair', value),
  open: target => ipcRenderer.invoke('ui:open', target),
});
