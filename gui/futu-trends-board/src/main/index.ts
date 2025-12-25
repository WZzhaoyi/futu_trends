import { app, shell, BrowserWindow, ipcMain, dialog, globalShortcut, Menu } from 'electron'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import { loadConfig, getConfig, getConfigPath } from './configManager'
import { getFutuStockList, getChartData } from './dataService'

// 存储已打开的图表窗口
const chartWindows = new Map<string, BrowserWindow>()
// 存储主窗口引用
let mainWindow: BrowserWindow | null = null

function createWindow(): void {
  // Create the browser window.
  mainWindow = new BrowserWindow({
    width: 900,
    height: 700,
    minWidth: 600,
    minHeight: 400,
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false
    }
  })

  mainWindow.on('ready-to-show', () => {
    if (mainWindow) {
      mainWindow.show()
    }
  })

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // 添加右键菜单支持
  mainWindow.webContents.on('context-menu', () => {
    const contextMenu = Menu.buildFromTemplate([
      {
        label: '打开开发者工具',
        accelerator: 'F12',
        click: () => {
          if (mainWindow) {
            mainWindow.webContents.toggleDevTools()
          }
        }
      },
      { type: 'separator' },
      { label: '刷新', role: 'reload' },
      { label: '强制刷新', role: 'forceReload' }
    ])
    contextMenu.popup()
  })

  // HMR for renderer base on electron-vite cli.
  // Load the remote URL for development or the local html file for production.
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

// This method will be called when Electron has finished
// initialization and is ready to create browser windows.
// Some APIs can only be used after this event occurs.
app.whenReady().then(() => {
  // 加载配置文件
  loadConfig()

  // Set app user model id for windows
  electronApp.setAppUserModelId('com.electron')

  // 注册全局快捷键 - 打开开发者工具
  // F12 和 Ctrl+Shift+I 都可以打开控制台
  globalShortcut.register('F12', () => {
    const focusedWindow = BrowserWindow.getFocusedWindow()
    if (focusedWindow) {
      focusedWindow.webContents.toggleDevTools()
    }
  })

  globalShortcut.register('CommandOrControl+Shift+I', () => {
    const focusedWindow = BrowserWindow.getFocusedWindow()
    if (focusedWindow) {
      focusedWindow.webContents.toggleDevTools()
    }
  })

  // Default open or close DevTools by F12 in development
  // and ignore CommandOrControl + R in production.
  // see https://github.com/alex8088/electron-toolkit/tree/master/packages/utils
  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  // IPC handlers
  ipcMain.on('ping', () => console.log('pong'))

  // 获取配置
  ipcMain.handle('get-config', () => {
    return getConfig()
  })

  // 选择配置文件并加载
  ipcMain.handle('select-config-file', async (event) => {
    // 获取窗口引用
    const window = BrowserWindow.fromWebContents(event.sender) || mainWindow
    
    if (!window) {
      console.error('[Main] No window available for file dialog')
      return null
    }

    // 确保窗口可见并获得焦点
    if (!window.isVisible()) window.show()
    if (window.isMinimized()) window.restore()
    window.focus()

    try {
      // 显示文件选择对话框
      const result = await dialog.showOpenDialog(window, {
        title: '选择配置文件',
        filters: [
          { name: '配置文件', extensions: ['ini', 'conf', 'config'] },
          { name: '所有文件', extensions: ['*'] }
        ],
        properties: ['openFile']
      })

      if (result.canceled || result.filePaths.length === 0) {
        console.log('[Main] Config file selection canceled by user')
        return null
      }

      // 加载选中的配置文件
      const selectedPath = result.filePaths[0]
      console.log('[Main] Config file selected:', selectedPath)
      
      // 保存旧配置路径用于回退
      const oldConfigPath = getConfigPath()
      // const oldConfig = getConfig() // 保留但不使用
      
      try {
        // 加载配置到主进程（会更新全局的 currentConfig 和 currentConfigPath）
        const config = loadConfig(selectedPath)
        
        // 验证配置是否有效
        if (!config) {
          console.error('[Main] Failed to load config: config is null or undefined')
          // 回退到旧配置
          if (oldConfigPath) {
            loadConfig(oldConfigPath)
          }
          throw new Error('配置文件加载失败，内容为空')
        }
        
        console.log('[Main] Config loaded successfully from:', selectedPath)
        console.log('[Main] Config summary:', {
          DATA_SOURCE: config.DATA_SOURCE || 'not set',
          FUTU_HOST: config.FUTU_HOST || 'not set',
          FUTU_PORT: config.FUTU_PORT || 'not set',
          FUTU_WS_PORT: config.FUTU_WS_PORT || 'not set',
          FUTU_GROUP: config.FUTU_GROUP || 'not set',
          FUTU_CODE_LIST: config.FUTU_CODE_LIST ? `${config.FUTU_CODE_LIST.split(',').length} codes` : 'not set',
          EMA_PERIOD: config.EMA_PERIOD || 'not set'
        })
        
        return { path: selectedPath, config }
      } catch (parseError) {
        console.error('[Main] Error parsing config file:', parseError)
        // 回退到旧配置
        if (oldConfigPath) {
          console.log('[Main] Reverting to previous config:', oldConfigPath)
          loadConfig(oldConfigPath)
        }
        throw new Error(`配置文件解析失败: ${parseError instanceof Error ? parseError.message : '未知错误'}`)
      }
    } catch (error) {
      console.error('[Main] Error in config file selection:', error)
      throw error
    }
  })

  // 重新加载配置（使用当前路径或指定路径）
  ipcMain.handle('reload-config', (_event, configPath?: string) => {
    try {
      console.log('[Main] Reloading config from:', configPath || 'current path')
      const config = loadConfig(configPath)
      
      if (!config) {
        console.error('[Main] Failed to reload config: config is null or undefined')
        throw new Error('配置重新加载失败')
      }
      
      console.log('[Main] Config reloaded successfully')
      return config
    } catch (error) {
      console.error('[Main] Error reloading config:', error)
      throw error
    }
  })

  // 打开图表窗口
  ipcMain.handle('open-chart-window', async (_event, stockCode) => {
    return openChartWindow(stockCode)
  })

  // 设置窗口标题
  ipcMain.handle('set-window-title', async (event, title) => {
    const window = BrowserWindow.fromWebContents(event.sender)
    if (window) {
      window.setTitle(`Futu Trends - ${title}`)
    }
  })

  // 获取股票列表
  ipcMain.handle('get-stock-list', async () => {
    try {
      console.log('[Main] IPC: get-stock-list')
      return await getFutuStockList()
    } catch (error) {
      console.error('[Main] Error getting stock list:', error)
      throw error
    }
  })

  // 获取图表数据（包含K线和指标）
  ipcMain.handle('get-chart-data', async (_event, stockCode: string, maxCount?: number) => {
    try {
      console.log('[Main] IPC: get-chart-data', { stockCode, maxCount })
      return await getChartData(stockCode, maxCount)
    } catch (error) {
      console.error('[Main] Error getting chart data:', error)
      throw error
    }
  })

  createWindow()

  app.on('activate', function () {
    // On macOS it's common to re-create a window in the app when the
    // dock icon is clicked and there are no other windows open.
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// Quit when all windows are closed, except on macOS. There, it's common
// for applications and their menu bar to stay active until the user quits
// explicitly with Cmd + Q.
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// 在应用退出前清理全局快捷键
app.on('will-quit', () => {
  globalShortcut.unregisterAll()
})

// 打开图表窗口
function openChartWindow(stockCode: string): Promise<number> {
  return new Promise((resolve, reject) => {
    try {
      // 检查是否已经打开了该股票的图表窗口
      const existingWindow = chartWindows.get(stockCode)
      if (existingWindow && !existingWindow.isDestroyed()) {
        existingWindow.focus()
        resolve(existingWindow.id)
        return
      }

      // 创建新的图表窗口
      const chartWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        show: false,
        autoHideMenuBar: true,
        title: `Futu Trends - ${stockCode}`,
        webPreferences: {
          preload: join(__dirname, '../preload/index.js'),
          sandbox: false
        }
      })

      // 存储窗口引用
      chartWindows.set(stockCode, chartWindow)

      chartWindow.on('ready-to-show', () => {
        chartWindow.show()
        resolve(chartWindow.id)
      })

      chartWindow.on('closed', () => {
        chartWindows.delete(stockCode)
      })

      chartWindow.webContents.setWindowOpenHandler((details) => {
        shell.openExternal(details.url)
        return { action: 'deny' }
      })

      // 添加右键菜单支持
      chartWindow.webContents.on('context-menu', () => {
        const contextMenu = Menu.buildFromTemplate([
          {
            label: '打开开发者工具',
            accelerator: 'F12',
            click: () => {
              chartWindow.webContents.toggleDevTools()
            }
          },
          { type: 'separator' },
          { label: '刷新', role: 'reload' },
          { label: '强制刷新', role: 'forceReload' }
        ])
        contextMenu.popup()
      })

      // 加载图表页面
      const chartUrl = is.dev && process.env['ELECTRON_RENDERER_URL']
        ? `${process.env['ELECTRON_RENDERER_URL']}?code=${encodeURIComponent(stockCode)}`
        : `file://${join(__dirname, '../renderer/index.html')}?code=${encodeURIComponent(stockCode)}`

      if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
        chartWindow.loadURL(chartUrl)
      } else {
        chartWindow.loadFile(join(__dirname, '../renderer/index.html'), {
          search: `?code=${encodeURIComponent(stockCode)}`
        })
      }

      // 监听窗口关闭事件
      chartWindow.on('closed', () => {
        chartWindows.delete(stockCode)
      })

    } catch (error) {
      reject(error)
    }
  })
}

// In this file you can include the rest of your app's specific main process
// code. You can also put them in separate files and require them here.
