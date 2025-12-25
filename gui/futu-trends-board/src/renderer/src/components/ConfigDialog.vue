<template>
  <n-modal
    v-model:show="visible"
    preset="card"
    :style="{ width: '700px', maxHeight: '80vh' }"
    title="应用配置"
    :bordered="false"
    :segmented="{ content: 'soft', footer: 'soft' }"
  >
    <div class="config-dialog">
      <!-- 顶部操作按钮 -->
      <div class="dialog-actions">
        <n-button size="small" @click="handleImportConfig" :loading="importing">
          <template #icon>
            <span>📁</span>
          </template>
          从文件导入
        </n-button>
        <n-button size="small" @click="handleResetToDefault">
          <template #icon>
            <span>🔄</span>
          </template>
          恢复默认值
        </n-button>
      </div>

      <!-- 配置表单 -->
      <n-form
        ref="formRef"
        :model="formData"
        :rules="rules"
        label-placement="left"
        label-width="150"
        require-mark-placement="left"
        size="medium"
      >
        <!-- 必选配置 -->
        <n-divider title-placement="left">
          <span style="color: #f5222d">必选配置</span>
        </n-divider>

        <n-form-item label="数据源" path="DATA_SOURCE" required>
          <n-select
            v-model:value="formData.DATA_SOURCE"
            :options="dataSourceOptions"
            placeholder="选择数据源"
          />
        </n-form-item>

        <n-form-item label="K线类型" path="FUTU_PUSH_TYPE" required>
          <n-select
            v-model:value="formData.FUTU_PUSH_TYPE"
            :options="klineTypeOptions"
            placeholder="选择K线类型"
          />
        </n-form-item>

        <!-- 富途配置 -->
        <n-divider title-placement="left">富途数据源配置（可选）</n-divider>

        <n-form-item label="富途主机地址" path="FUTU_HOST">
          <n-input
            v-model:value="formData.FUTU_HOST"
            placeholder="127.0.0.1"
          />
        </n-form-item>

        <n-form-item label="富途端口" path="FUTU_PORT">
          <n-input-number
            v-model:value="formData.FUTU_PORT"
            :show-button="false"
            placeholder="11111"
            style="width: 100%"
          />
        </n-form-item>

        <n-form-item label="WebSocket 端口" path="FUTU_WS_PORT">
          <n-input-number
            v-model:value="formData.FUTU_WS_PORT"
            :show-button="false"
            placeholder="33334"
            style="width: 100%"
          />
        </n-form-item>

        <n-form-item label="WebSocket 密钥" path="FUTU_WS_KEY">
          <n-input
            v-model:value="formData.FUTU_WS_KEY"
            type="password"
            show-password-on="click"
            placeholder="留空则不使用密钥"
          />
        </n-form-item>

        <n-form-item label="自选股分组" path="FUTU_GROUP">
          <n-input
            v-model:value="formData.FUTU_GROUP"
            placeholder="例如：CNE"
          />
        </n-form-item>

        <n-form-item label="股票代码列表" path="FUTU_CODE_LIST">
          <n-input
            v-model:value="formData.FUTU_CODE_LIST"
            type="textarea"
            :rows="3"
            placeholder="用逗号分隔，例如：SH.510300,SH.000985,SH.000902"
          />
        </n-form-item>

        <!-- 技术指标配置 -->
        <n-divider title-placement="left">技术指标配置（可选）</n-divider>

        <n-form-item label="EMA 周期" path="EMA_PERIOD">
          <n-input-number
            v-model:value="formData.EMA_PERIOD"
            :min="1"
            :max="500"
            placeholder="240"
            style="width: 100%"
          />
        </n-form-item>

        <!-- 数据库配置 -->
        <n-divider title-placement="left">参数数据库路径（可选）</n-divider>

        <n-form-item label="KD 参数数据库">
          <n-input
            v-model:value="formData.KD_PARAMS_DB"
            placeholder="留空使用默认参数"
          />
        </n-form-item>

        <n-form-item label="MACD 参数数据库">
          <n-input
            v-model:value="formData.MACD_PARAMS_DB"
            placeholder="留空使用默认参数"
          />
        </n-form-item>

        <n-form-item label="RSI 参数数据库">
          <n-input
            v-model:value="formData.RSI_PARAMS_DB"
            placeholder="留空使用默认参数"
          />
        </n-form-item>

        <!-- 其他配置 -->
        <n-divider title-placement="left">其他配置（可选）</n-divider>

        <n-form-item label="HTTP 代理">
          <n-input
            v-model:value="formData.PROXY"
            placeholder="http://127.0.0.1:7890"
          />
        </n-form-item>

        <n-form-item label="数据缓存目录">
          <n-input
            v-model:value="formData.DATA_DIR"
            placeholder="./data/detect"
          />
        </n-form-item>

        <n-form-item label="深色模式">
          <n-switch v-model:value="formData.DARK_MODE" />
        </n-form-item>
      </n-form>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <n-space justify="end">
          <n-button @click="handleCancel">取消</n-button>
          <n-button type="primary" @click="handleSave" :loading="saving">
            保存并应用
          </n-button>
        </n-space>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  NModal,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSelect,
  NSwitch,
  NButton,
  NSpace,
  NDivider,
  useMessage,
  type FormInst,
  type FormRules
} from 'naive-ui'

interface ConfigData {
  DATA_SOURCE: string
  FUTU_HOST: string
  FUTU_PORT: number | null
  FUTU_WS_PORT: number | null
  FUTU_WS_KEY: string
  FUTU_GROUP: string
  FUTU_CODE_LIST: string
  FUTU_PUSH_TYPE: string
  EMA_PERIOD: number | null
  KD_PARAMS_DB: string
  MACD_PARAMS_DB: string
  RSI_PARAMS_DB: string
  PROXY: string
  DATA_DIR: string
  DARK_MODE: boolean
}

// Props & Emits
const props = defineProps<{
  show: boolean
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'save': [config: ConfigData]
}>()

// State
const visible = ref(props.show)
const message = useMessage()
const formRef = ref<FormInst | null>(null)
const saving = ref(false)
const importing = ref(false)

// 数据源选项
const dataSourceOptions = [
  { label: '富途 (Futu)', value: 'futu' },
  { label: 'Yahoo Finance', value: 'yfinance' },
  { label: 'AkShare', value: 'akshare' }
]

// K线类型选项
const klineTypeOptions = [
  { label: '1分钟', value: 'K_1M' },
  { label: '5分钟', value: 'K_5M' },
  { label: '15分钟', value: 'K_15M' },
  { label: '30分钟', value: 'K_30M' },
  { label: '60分钟', value: 'K_60M' },
  { label: '4小时', value: 'K_240M' },
  { label: '日K', value: 'K_DAY' },
  { label: '周K', value: 'K_WEEK' },
  { label: '月K', value: 'K_MON' }
]

// 表单数据
const formData = ref<ConfigData>({
  DATA_SOURCE: 'yfinance',
  FUTU_HOST: '127.0.0.1',
  FUTU_PORT: 11111,
  FUTU_WS_PORT: 33334,
  FUTU_WS_KEY: '',
  FUTU_GROUP: '',
  FUTU_CODE_LIST: 'SH.510300,SH.000985,SH.000902',
  FUTU_PUSH_TYPE: 'K_DAY',
  EMA_PERIOD: 240,
  KD_PARAMS_DB: '',
  MACD_PARAMS_DB: '',
  RSI_PARAMS_DB: '',
  PROXY: '',
  DATA_DIR: './data/detect',
  DARK_MODE: true
})

// 表单验证规则
const rules: FormRules = {
  DATA_SOURCE: [
    { required: true, message: '请选择数据源', trigger: 'change' }
  ],
  FUTU_PUSH_TYPE: [
    { required: true, message: '请选择K线类型', trigger: 'change' }
  ]
}

// Watch props.show
watch(
  () => props.show,
  (newVal) => {
    visible.value = newVal
    if (newVal) {
      loadCurrentConfig()
    }
  }
)

// Watch visible
watch(visible, (newVal) => {
  emit('update:show', newVal)
  // 关闭对话框时重置状态
  if (!newVal) {
    saving.value = false
  }
})

// 加载当前配置
const loadCurrentConfig = async () => {
  try {
    if (window.electronAPI && typeof window.electronAPI.getConfig === 'function') {
      const config = await window.electronAPI.getConfig()
      
      // 合并配置，保留表单默认值用于未设置的字段
      formData.value = {
        DATA_SOURCE: config.DATA_SOURCE || 'yfinance',
        FUTU_HOST: config.FUTU_HOST || '127.0.0.1',
        FUTU_PORT: config.FUTU_PORT ? Number(config.FUTU_PORT) : 11111,
        FUTU_WS_PORT: config.FUTU_WS_PORT ? Number(config.FUTU_WS_PORT) : 33334,
        FUTU_WS_KEY: config.FUTU_WS_KEY || '',
        FUTU_GROUP: config.FUTU_GROUP || '',
        FUTU_CODE_LIST: config.FUTU_CODE_LIST || 'SH.510300,SH.000985,SH.000902',
        FUTU_PUSH_TYPE: config.FUTU_PUSH_TYPE || 'K_DAY',
        EMA_PERIOD: config.EMA_PERIOD ? Number(config.EMA_PERIOD) : 240,
        KD_PARAMS_DB: config.KD_PARAMS_DB || '',
        MACD_PARAMS_DB: config.MACD_PARAMS_DB || '',
        RSI_PARAMS_DB: config.RSI_PARAMS_DB || '',
        PROXY: config.PROXY || '',
        DATA_DIR: config.DATA_DIR || './data/detect',
        DARK_MODE: config.DARK_MODE === 'True' || config.DARK_MODE === true
      }
      
      console.log('[ConfigDialog] Current config loaded:', formData.value)
    }
  } catch (error) {
    console.error('[ConfigDialog] Failed to load config:', error)
    message.error('加载配置失败')
  }
}

// 从文件导入配置
const handleImportConfig = async () => {
  if (!window.electronAPI || typeof window.electronAPI.selectConfigFile !== 'function') {
    message.warning('此功能仅在 Electron 环境中可用')
    return
  }

  importing.value = true
  try {
    const result = await window.electronAPI.selectConfigFile()
    
    if (result && result.config) {
      // 将导入的配置填充到表单
      formData.value = {
        DATA_SOURCE: result.config.DATA_SOURCE || formData.value.DATA_SOURCE,
        FUTU_HOST: result.config.FUTU_HOST || formData.value.FUTU_HOST,
        FUTU_PORT: result.config.FUTU_PORT ? Number(result.config.FUTU_PORT) : formData.value.FUTU_PORT,
        FUTU_WS_PORT: result.config.FUTU_WS_PORT ? Number(result.config.FUTU_WS_PORT) : formData.value.FUTU_WS_PORT,
        FUTU_WS_KEY: result.config.FUTU_WS_KEY || formData.value.FUTU_WS_KEY,
        FUTU_GROUP: result.config.FUTU_GROUP || formData.value.FUTU_GROUP,
        FUTU_CODE_LIST: result.config.FUTU_CODE_LIST || formData.value.FUTU_CODE_LIST,
        FUTU_PUSH_TYPE: result.config.FUTU_PUSH_TYPE || formData.value.FUTU_PUSH_TYPE,
        EMA_PERIOD: result.config.EMA_PERIOD ? Number(result.config.EMA_PERIOD) : formData.value.EMA_PERIOD,
        KD_PARAMS_DB: result.config.KD_PARAMS_DB || formData.value.KD_PARAMS_DB,
        MACD_PARAMS_DB: result.config.MACD_PARAMS_DB || formData.value.MACD_PARAMS_DB,
        RSI_PARAMS_DB: result.config.RSI_PARAMS_DB || formData.value.RSI_PARAMS_DB,
        PROXY: result.config.PROXY || formData.value.PROXY,
        DATA_DIR: result.config.DATA_DIR || formData.value.DATA_DIR,
        DARK_MODE: result.config.DARK_MODE === 'True' || result.config.DARK_MODE === true
      }
      
      message.success(`已从 ${result.path} 导入配置`)
      console.log('[ConfigDialog] Config imported from file:', result.path)
    }
  } catch (error) {
    console.error('[ConfigDialog] Failed to import config:', error)
    message.error('导入配置失败')
  } finally {
    importing.value = false
  }
}

// 恢复默认值
const handleResetToDefault = () => {
  formData.value = {
    DATA_SOURCE: 'yfinance',
    FUTU_HOST: '127.0.0.1',
    FUTU_PORT: 11111,
    FUTU_WS_PORT: 33334,
    FUTU_WS_KEY: '',
    FUTU_GROUP: '',
    FUTU_CODE_LIST: 'SH.510300,SH.000985,SH.000902',
    FUTU_PUSH_TYPE: 'K_DAY',
    EMA_PERIOD: 240,
    KD_PARAMS_DB: '',
    MACD_PARAMS_DB: '',
    RSI_PARAMS_DB: '',
    PROXY: '',
    DATA_DIR: './data/detect',
    DARK_MODE: true
  }
  message.info('已恢复默认配置')
}

// 保存配置
const handleSave = async () => {
  if (!formRef.value) return

  try {
    await formRef.value.validate()
    
    saving.value = true
    
    // 转换为纯 JavaScript 对象（移除响应式代理）
    const plainConfig: ConfigData = {
      DATA_SOURCE: formData.value.DATA_SOURCE,
      FUTU_HOST: formData.value.FUTU_HOST,
      FUTU_PORT: formData.value.FUTU_PORT,
      FUTU_WS_PORT: formData.value.FUTU_WS_PORT,
      FUTU_WS_KEY: formData.value.FUTU_WS_KEY,
      FUTU_GROUP: formData.value.FUTU_GROUP,
      FUTU_CODE_LIST: formData.value.FUTU_CODE_LIST,
      FUTU_PUSH_TYPE: formData.value.FUTU_PUSH_TYPE,
      EMA_PERIOD: formData.value.EMA_PERIOD,
      KD_PARAMS_DB: formData.value.KD_PARAMS_DB,
      MACD_PARAMS_DB: formData.value.MACD_PARAMS_DB,
      RSI_PARAMS_DB: formData.value.RSI_PARAMS_DB,
      PROXY: formData.value.PROXY,
      DATA_DIR: formData.value.DATA_DIR,
      DARK_MODE: formData.value.DARK_MODE
    }
    
    emit('save', plainConfig)
    // saving 状态在对话框关闭时重置
  } catch (error) {
    console.error('[ConfigDialog] Validation failed:', error)
    message.error('请检查必填项')
    saving.value = false
  }
}

// 取消
const handleCancel = () => {
  visible.value = false
}
</script>

<style scoped>
.config-dialog {
  max-height: calc(80vh - 120px);
  overflow-y: auto;
  padding-right: 8px;
}

.dialog-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  padding-bottom: 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.dialog-footer {
  padding-top: 8px;
}

/* 优化滚动条 */
.config-dialog::-webkit-scrollbar {
  width: 8px;
}

.config-dialog::-webkit-scrollbar-track {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 4px;
}

.config-dialog::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.2);
  border-radius: 4px;
}

.config-dialog::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.3);
}
</style>

