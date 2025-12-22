import { app, BrowserWindow, ipcMain, dialog } from 'electron';
import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

// API 配置
export const DEFAULT_API_PORT = 8001;
export const API_TIMEOUT = 5000;
export const MAX_KLINE_COUNT = 1000;

let pythonService: ChildProcess | null = null;
let mainWindow: BrowserWindow | null = null;
let actualApiPort: number = DEFAULT_API_PORT;
let apiBase: string = `http://127.0.0.1:${DEFAULT_API_PORT}`;
let currentConfigPath: string | null = null;

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 400,
    height: 700,
    autoHideMenuBar: true, // 隐藏菜单栏
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // 开发环境加载 Vite 开发服务器
  // 检查是否是开发模式（通过检查是否有 dist 目录或环境变量）
  const isDev = process.env.NODE_ENV === 'development' || !process.env.NODE_ENV;
  
  if (isDev) {
    // 等待 Vite 服务器启动
    setTimeout(() => {
      mainWindow?.loadURL('http://localhost:5173');
    }, 1000);
  } else {
    mainWindow.loadFile(path.join(__dirname, '../../dist/index.html'));
  }
}

// 检查 Python 服务是否就绪
async function waitForPythonService(maxWaitTime: number = API_TIMEOUT): Promise<boolean> {
  const http = require('http');
  const startTime = Date.now();
  
  return new Promise((resolve) => {
    const checkService = () => {
      // 使用当前的实际端口
      const url = apiBase + '/';
      const req = http.get(url, { timeout: API_TIMEOUT }, (res: any) => {
        if (res.statusCode === 200) {
          console.log(`[Electron] Python service is ready on port ${actualApiPort}`);
          resolve(true);
        } else {
          if (Date.now() - startTime < maxWaitTime) {
            setTimeout(checkService, 500);
          } else {
            console.warn('[Electron] Python service startup timeout, but continuing to start window');
            resolve(false);
          }
        }
      });
      
      req.on('error', () => {
        if (Date.now() - startTime < maxWaitTime) {
          setTimeout(checkService, 500);
        } else {
          console.warn('[Electron] Python 服务启动超时，但继续启动窗口');
          resolve(false);
        }
      });
      
      req.on('timeout', () => {
        req.destroy();
        if (Date.now() - startTime < maxWaitTime) {
          setTimeout(checkService, 500);
        } else {
          console.warn('[Electron] Python 服务启动超时，但继续启动窗口');
          resolve(false);
        }
      });
    };
    
    // 等待 2 秒后开始检查（给服务启动时间）
    setTimeout(checkService, 2000);
  });
}

function startPythonService(configPath?: string | null): void {
  // 启动 Python FastAPI 服务
  const pythonPath = process.env.PYTHON_PATH || 'python';
  const apiPath = path.join(__dirname, '../../backend/api.py');
  
  // 配置文件路径（相对于项目根目录）
  // __dirname 在编译后是 gui/electron/electron/
  // 需要回到项目根目录: ../../../ = futu_trends/
  const projectRoot = path.resolve(__dirname, '../../../');
  
  let configFullPath: string | null = null;
  
  // 如果提供了配置文件路径，使用它
  if (configPath) {
    if (path.isAbsolute(configPath)) {
      configFullPath = configPath;
    } else {
      configFullPath = path.join(projectRoot, configPath);
    }
    // 保存当前配置文件路径
    currentConfigPath = configFullPath;
  } else if (currentConfigPath && fs.existsSync(currentConfigPath)) {
    // 使用之前保存的配置文件路径
    configFullPath = currentConfigPath;
  } else {
    // 尝试多个可能的配置文件路径
    const possibleConfigPaths = [
      path.join(projectRoot, 'env', 'signal_window.ini')
    ];
    
    if (process.env.CONFIG_PATH) {
      // 如果 CONFIG_PATH 是相对路径，相对于项目根目录
      if (path.isAbsolute(process.env.CONFIG_PATH)) {
        configFullPath = process.env.CONFIG_PATH;
      } else {
        configFullPath = path.join(projectRoot, process.env.CONFIG_PATH);
      }
    } else {
      // 尝试找到存在的配置文件
      for (const possiblePath of possibleConfigPaths) {
        if (fs.existsSync(possiblePath)) {
          configFullPath = possiblePath;
          break;
        }
      }
    }
    
    // 保存找到的配置文件路径
    if (configFullPath) {
      currentConfigPath = configFullPath;
    }
  }
  
  const args = [apiPath];
  if (configFullPath && fs.existsSync(configFullPath)) {
    args.push('--config', configFullPath);
    console.log(`[Electron] Starting Python service: ${pythonPath} ${apiPath} --config=${configFullPath}`);
  } else {
    console.warn('[Electron] Warning: Config file not found, using default configuration');
    console.log(`[Electron] Starting Python service: ${pythonPath} ${apiPath}`);
    currentConfigPath = null;
  }
  
  pythonService = spawn(pythonPath, args, {
    cwd: path.join(__dirname, '../..'),
  });

  pythonService.stdout?.on('data', (data: Buffer) => {
    const output = data.toString();
    
    // 提取端口信息
    const portMatch = output.match(/API_PORT=(\d+)/);
    if (portMatch) {
      actualApiPort = parseInt(portMatch[1], 10);
      apiBase = `http://127.0.0.1:${actualApiPort}`;
      console.log(`[Electron] API port detected: ${actualApiPort}`);
      
      // 通知所有窗口更新 API 地址
      BrowserWindow.getAllWindows().forEach((win) => {
        win.webContents.send('api-port-changed', actualApiPort);
      });
    }
    
    // 检查服务是否已启动
    if (output.includes('Uvicorn running on')) {
      console.log('[Electron] Python service started');
    }
    console.log(`[Python] ${output}`);
  });

  pythonService.stderr?.on('data', (data: Buffer) => {
    const output = data.toString();
    
    // 提取端口信息（也可能在 stderr 中）
    const portMatch = output.match(/API_PORT=(\d+)/);
    if (portMatch) {
      actualApiPort = parseInt(portMatch[1], 10);
      apiBase = `http://127.0.0.1:${actualApiPort}`;
      console.log(`[Electron] API port detected: ${actualApiPort}`);
      
      // 通知所有窗口更新 API 地址
      BrowserWindow.getAllWindows().forEach((win) => {
        win.webContents.send('api-port-changed', actualApiPort);
      });
    }
    
    // Python logging module outputs to stderr, including INFO/WARNING/ERROR
    // Check for log levels: INFO, WARNING, ERROR, DEBUG
    const isInfoLog = /-\s*(INFO|DEBUG)\s*-/.test(output);
    const isWarningLog = /-\s*WARNING\s*-/.test(output);
    const isErrorLog = /-\s*ERROR\s*-/.test(output);
    const isUvicornLog = output.includes('INFO:') || output.includes('Uvicorn running on');
    
    if (isInfoLog || isUvicornLog) {
      // INFO and DEBUG are normal logs
      console.log(`[Python] ${output}`);
    } else if (isWarningLog) {
      // WARNING logs
      console.warn(`[Python] ${output}`);
    } else if (isErrorLog) {
      // ERROR logs
      console.error(`[Python] ${output}`);
    } else {
      // Unknown stderr output, treat as error
      console.error(`[Python Error] ${output}`);
    }
  });

  pythonService.on('close', (code) => {
    console.log(`[Electron] Python service exited with code: ${code}`);
  });

  pythonService.on('error', (error) => {
    console.error(`[Electron] Failed to start Python service: ${error.message}`);
  });
}

// 停止 Python 服务
function stopPythonService(): Promise<void> {
  return new Promise((resolve) => {
    if (!pythonService) {
      resolve();
      return;
    }
    
    console.log('[Electron] Stopping Python service...');
    
    const service = pythonService;
    let resolved = false;
    
    const cleanup = () => {
      if (!resolved) {
        resolved = true;
        pythonService = null;
        console.log('[Electron] Python service stopped');
        resolve();
      }
    };
    
    // 监听进程退出
    service.once('close', cleanup);
    service.once('exit', cleanup);
    
    // 尝试优雅关闭（SIGTERM）
    try {
      if (process.platform === 'win32') {
        // Windows: kill() 会发送终止信号
        service.kill();
      } else {
        // Unix: 发送 SIGTERM 信号
        service.kill('SIGTERM');
      }
    } catch (error) {
      console.error('[Electron] Error killing Python service:', error);
      cleanup();
      return;
    }
    
    // 如果 3 秒后还没关闭，强制杀死
    setTimeout(() => {
      if (service === pythonService && pythonService) {
        console.log('[Electron] Force killing Python service');
        try {
          if (process.platform === 'win32') {
            pythonService.kill();
          } else {
            pythonService.kill('SIGKILL');
          }
        } catch (error) {
          console.error('[Electron] Error force killing Python service:', error);
        }
        cleanup();
      }
    }, 3000);
  });
}

// 重启 Python 服务
async function restartPythonService(configPath?: string | null): Promise<void> {
  console.log('[Electron] Restarting Python service...');
  await stopPythonService();
  // 等待一小段时间确保端口释放
  await new Promise(resolve => setTimeout(resolve, 1000));
  startPythonService(configPath);
  // 等待服务启动
  await waitForPythonService(30000);
}

app.whenReady().then(async () => {
  // 启动 Python 服务
  startPythonService();
  
  // 等待 Python 服务就绪（最多等待 30 秒）
  console.log('[Electron] Waiting for Python service to start...');
  await waitForPythonService(30000);
  
  // 创建窗口
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    if (pythonService) {
      pythonService.kill();
    }
    app.quit();
  }
});

// IPC 处理：打开新图表窗口
ipcMain.handle('open-chart-window', (event, code: string) => {
  const chartWindow = new BrowserWindow({
    width: 1200,
    height: 900,
    autoHideMenuBar: true, // 隐藏菜单栏
    title: code || 'Chart', // 设置窗口标题
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  const isDev = process.env.NODE_ENV === 'development' || !process.env.NODE_ENV;
  
  // 确保 code 不为空
  if (!code || code === 'undefined') {
    console.error('[Electron] Invalid code parameter:', code);
    return 0;
  }
  
  // URL 编码 code 参数
  const encodedCode = encodeURIComponent(code);
  console.log('[Electron] Opening chart window, code:', code, 'encoded:', encodedCode);
  
  if (isDev) {
    chartWindow.loadURL(`http://localhost:5173?code=${encodedCode}`);
  } else {
    chartWindow.loadFile(path.join(__dirname, '../../dist/index.html'), {
      query: { code: encodedCode },
    });
  }

  return chartWindow.id;
});

// IPC 处理：设置窗口标题
ipcMain.handle('set-window-title', (event, title: string) => {
  const window = BrowserWindow.fromWebContents(event.sender);
  if (window) {
    window.setTitle(title || 'Chart');
  }
});

// IPC 处理：获取 API 端口
ipcMain.handle('get-api-port', () => {
  return actualApiPort;
});

// IPC 处理：选择配置文件
ipcMain.handle('select-config-file', async () => {
  if (!mainWindow) {
    console.error('[Electron] Main window not found');
    return null;
  }
  const result = await dialog.showOpenDialog(mainWindow, {
    title: '选择配置文件',
    filters: [
      { name: '配置文件', extensions: ['ini', 'conf', 'config'] },
      { name: '所有文件', extensions: ['*'] }
    ],
    properties: ['openFile']
  });
  
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  
  const selectedPath = result.filePaths[0];
  console.log(`[Electron] Config file selected: ${selectedPath}`);
  return selectedPath;
});

// IPC 处理：重启后端服务
ipcMain.handle('restart-backend', async (event, configPath?: string | null) => {
  try {
    await restartPythonService(configPath);
    return { success: true, message: '后端服务重启成功' };
  } catch (error: any) {
    console.error('[Electron] Failed to restart backend:', error);
    return { success: false, message: `重启失败: ${error.message}` };
  }
});

