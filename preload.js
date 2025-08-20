// preload.js
const { contextBridge, ipcRenderer } = require('electron');

// Expose a secure API to the renderer process (your index.html)
contextBridge.exposeInMainWorld('electron', {
    // Function to get the dynamic port from the main process
    getFastApiPort: () => ipcRenderer.invoke('get-fastapi-port'),
    
    // Function to trigger the 'open directory' dialog in the main process
    openDialog: () => ipcRenderer.invoke('dialog:openDirectory')
});
