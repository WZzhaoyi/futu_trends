<template>
  <div id="app">
    <!-- 从 URL 参数获取 code，如果有则显示图表窗口，否则显示股票列表 -->
    <SignalWindow v-if="code" :code="code" />
    <GroupList v-else />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeMount, watch } from 'vue';
import GroupList from './GroupList.vue';
import SignalWindow from './SignalWindow.vue';

const code = ref<string | null>(null);

import { isValidCode } from './utils/validation';

/**
 * 从 URL 参数获取 code
 */
const updateCodeFromURL = () => {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    const codeParam = urlParams.get('code');
    
    if (codeParam) {
      const decodedCode = decodeURIComponent(codeParam);
      code.value = isValidCode(decodedCode) ? decodedCode : null;
    } else {
      // 检查 hash 中的参数
      if (window.location.hash) {
        const hashParams = new URLSearchParams(window.location.hash.substring(1));
        const codeFromHash = hashParams.get('code');
        if (codeFromHash) {
          const decodedCode = decodeURIComponent(codeFromHash);
          code.value = isValidCode(decodedCode) ? decodedCode : null;
        } else {
          code.value = null;
        }
      } else {
        code.value = null;
      }
    }
  } catch (error) {
    console.error('[App.vue] Failed to read URL parameters:', error);
    code.value = null;
  }
};

onBeforeMount(() => {
  updateCodeFromURL();
});

onMounted(() => {
  updateCodeFromURL();
  window.addEventListener('popstate', updateCodeFromURL);
});
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  width: 100%;
  height: 100vh;
  overflow: hidden;
}
</style>

