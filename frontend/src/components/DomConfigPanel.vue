<template>
  <section class="config-panel" data-testid="dom-config-panel">
    <header class="config-head">
      <div>
        <h3>配置版本与签名</h3>
        <p class="page-desc">
          选择一条解析配置查看历史版本快照；更正选择器会自动升版并把新签名推给实体抽取来源索引。
        </p>
      </div>
      <label class="filter-item">
        <span>配置ID</span>
        <input v-model="configId" data-testid="config-id" placeholder="如 1" />
      </label>
      <button class="btn" type="button" data-testid="config-load" @click="loadSnapshots">查看版本</button>
      <button class="btn" type="button" :disabled="!configId" data-testid="config-push" @click="push">
        下发来源与签名
      </button>
    </header>

    <p v-if="message" class="config-note" data-testid="config-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="config-error">{{ errorMessage }}</p>

    <table v-if="snapshots.length" class="data-table" data-testid="config-snapshots">
      <thead>
        <tr>
          <th>版本</th><th>选择器规则</th><th>输出Schema</th><th>解析签名</th><th>快照时间</th><th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="snapshot in snapshots" :key="snapshot.配置版本"
            :data-testid="`snapshot-${snapshot.配置版本}`">
          <td>{{ snapshot.配置版本 }}</td>
          <td>{{ snapshot.选择器规则 }}</td>
          <td>{{ snapshot.输出Schema }}</td>
          <td>{{ snapshot.解析签名 }}</td>
          <td>{{ snapshot.快照时间 }}</td>
          <td>{{ snapshot.配置版本 === currentVersion ? '当前版本' : '历史版本' }}</td>
        </tr>
      </tbody>
    </table>

    <form class="filter-bar" data-testid="config-revise-form" @submit.prevent="revise">
      <label class="filter-item">
        <span>新选择器规则</span>
        <input v-model="newSelector" data-testid="config-selector" placeholder="如 .product-title-v3" />
      </label>
      <button class="btn primary" type="submit" :disabled="!configId || !newSelector" data-testid="config-revise">
        更正选择器并升版
      </button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { listConfigSnapshots, pushSignature, reviseConfig, type ConfigSnapshot } from '@/api/source-index'

const configId = ref('')
const snapshots = ref<ConfigSnapshot[]>([])
const currentVersion = ref('')
const newSelector = ref('')
const message = ref('')
const errorMessage = ref('')

async function loadSnapshots() {
  errorMessage.value = ''
  try {
    const payload = await listConfigSnapshots(Number(configId.value))
    snapshots.value = payload.items
    currentVersion.value = payload.current_version
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本快照读取失败'
  }
}

async function push() {
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await pushSignature(Number(configId.value))
    message.value = payload.ok
      ? `已下发签名 ${payload.signature}，索引条目 ${payload.entry_count} 条`
      : payload.reason ?? '下发失败'
    await loadSnapshots()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下发签名失败'
  }
}

async function revise() {
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await reviseConfig(Number(configId.value), { 选择器规则: newSelector.value })
    message.value = `已从 ${payload.previous_version}（${payload.previous_signature}）升版到 ${payload.current_version}（${payload.current_signature}）`
    newSelector.value = ''
    await loadSnapshots()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更正配置失败'
  }
}
</script>

<style scoped>
.config-panel { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.config-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; justify-content: space-between; }
.config-note { color: var(--muted); font-size: 12px; margin: 6px 0; }
</style>
