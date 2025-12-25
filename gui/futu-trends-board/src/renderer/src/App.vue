<template>
  <n-message-provider>
    <div id="app">
      <!-- 从 URL 参数获取 code，如果有则显示图表窗口，否则显示股票列表 -->
      <SignalWindow v-if="code" :code="code" />
      <GroupList v-else />
    </div>
  </n-message-provider>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeMount } from 'vue';
import { NMessageProvider } from 'naive-ui';
import GroupList from './components/GroupList.vue';
import SignalWindow from './components/SignalWindow.vue';

const code = ref<string | null>(null);

/**
 * 从 URL 参数获取 code
 */
const updateCodeFromURL = () => {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    const codeParam = urlParams.get('code');

    if (codeParam) {
      const decodedCode = decodeURIComponent(codeParam);
      code.value = decodedCode;
    } else {
      // 检查 hash 中的参数
      if (window.location.hash) {
        const hashParams = new URLSearchParams(window.location.hash.substring(1));
        const codeFromHash = hashParams.get('code');
        if (codeFromHash) {
          const decodedCode = decodeURIComponent(codeFromHash);
          code.value = decodedCode;
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

html, body {
  width: 100%;
  height: 100%;
  overflow: hidden;
  margin: 0;
  padding: 0;
}

#app {
  width: 100%;
  height: 100%;
  overflow: hidden;
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: block !important;
  align-items: unset !important;
  justify-content: unset !important;
  flex-direction: unset !important;
  margin-bottom: 0 !important;
}
</style>
