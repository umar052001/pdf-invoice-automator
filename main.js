const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const axios = require('axios');

let mainWindow;
let pythonProcess = null;
let fastapiPort = null;

/**
 * Determines the correct path to the Python executable.
 */
function getPythonExecutablePath() {
    const isPackaged = app.isPackaged;
    const platform = process.platform;
    
    if (isPackaged) {
        const executableName = platform === 'win32' ? 'main.exe' : 'main';
        return path.join(process.resourcesPath, 'backend', executableName);
    } else {
        return platform === 'win32' ? 'python' : 'python3';
    }
}

/**
 * Starts the FastAPI backend as a child process.
 */
function startPythonBackend() {
    const pythonExecutable = getPythonExecutablePath();
    const scriptArgs = app.isPackaged ? [] : [path.join(__dirname, 'main.py')];

    if (app.isPackaged && !fs.existsSync(pythonExecutable)) {
        dialog.showErrorBox(
            'Backend Error',
            `Could not find the packaged backend executable at: ${pythonExecutable}.`
        );
        app.quit();
        return;
    }

    pythonProcess = spawn(pythonExecutable, scriptArgs);

    pythonProcess.stdout.on('data', (data) => {
        const output = data.toString();
        console.log(`Python stdout: ${output}`);
        if (output.includes('FASTAPI_PORT=')) {
            fastapiPort = output.split('=')[1].trim();
            console.log(`FastAPI backend started on port: ${fastapiPort}`);
            createWindow();
        }
    });

    pythonProcess.stderr.on('data', (data) => {
        const output = data.toString();
        console.error(`Python stderr: ${output}`);
        if (!app.isPackaged && output.includes('ModuleNotFoundError')) {
             dialog.showErrorBox(
                'Backend Error',
                `A required Python module was not found. Please ensure your virtual environment is activated and you have run 'pip install -r requirements.txt'.\n\nError: ${output}`
            );
            app.quit();
        }
    });

    pythonProcess.on('close', (code) => {
        console.log(`Python process exited with code ${code}`);
    });
}

/**
 * Creates the main application window after the backend is ready.
 */
function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 800,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
        },
        show: false
    });

    const FASTAPI_URL = `http://127.0.0.1:${fastapiPort}`;

    const checkBackendReady = async () => {
        try {
            await axios.get(`${FASTAPI_URL}/health`);
            console.log('Backend is ready. Loading UI.');
            mainWindow.loadFile('index.html');
            mainWindow.once('ready-to-show', () => {
                mainWindow.show();
            });
        } catch (error) {
            console.log('Backend not ready, retrying in 1 second...');
            setTimeout(checkBackendReady, 1000);
        }
    };

    checkBackendReady();
}

// --- IPC Handlers (Communication between Main and Renderer) ---

ipcMain.handle('get-fastapi-port', () => {
    return fastapiPort;
});

// --- FIX: This is the definitive, correct handler for the dialog ---
ipcMain.handle('dialog:openDirectory', async () => {
    const { canceled, filePaths } = await dialog.showOpenDialog({
        properties: ['openDirectory']
    });
    if (!canceled && filePaths.length > 0) {
        return filePaths[0];
    }
    return null;
});


// --- Electron App Lifecycle ---

app.whenReady().then(() => {
    console.log('App is ready, starting backend...');
    startPythonBackend();
    
    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            // Window is created once the backend port is known
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('before-quit', () => {
    if (pythonProcess) {
        console.log('Terminating Python backend process...');
        pythonProcess.kill();
    }
});

