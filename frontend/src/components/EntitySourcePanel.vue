<template>
  <section class="source-panel" data-testid="entity-source-panel">
    <header class="source-head">
      <div>
        <h3>实体来源索引与重放清单</h3>
        <p class="page-desc">
          按页面、字段与解析签名定位可复验的实体；选中的命中回写到重放清单，
          由 DOM 解析按清单里的配置快照复验，不会改跑当前选择器。
        </p>
      </div>
      <button class="btn" type="button" data-testid="source-refresh" @click="refreshReplays">
        刷新重放清单
      </button>
    </header>

    <form class="filter-bar" data-testid="source-search-form" @submit.prevent="runSearch">
      <label class="filter-item">
        <span>页面URI</span>
        <input v-model="query.page_uri" data-testid="source-page" placeholder="如 https://shop.example.com/item/1001" />
      </label>
      <label class="filter-item">
        <span>字段</span>
        <input v-model="query.field" data-testid="source-field" placeholder="如 title" />
      </label>
      <label class="filter-item">
        <span>解析签名</span>
        <input v-model="query.signature" data-testid="source-signature" placeholder="如 660c5b358cba" />
      </label>
      <label class="filter-item">
        <span>配置版本</span>
        <input v-model="query.version" data-testid="source-version" placeholder="如 v1" />
      </label>
      <label class="check-item">
        <input v-model="query.include_unsigned" type="checkbox" data-testid="source-unsigned" />
        <span>包含无法补签名的历史记录</span>
      </label>
      <button class="btn primary" data-testid="source-search" type="submit" :disabled="searching">
        {{ searching ? '检索中…' : '检索来源索引' }}
      </button>
      <button class="btn" data-testid="source-writeback" type="button" :disabled="!hits.length" @click="writeback">
        回写选中命中到重放清单
      </button>
    </form>

    <p v-if="message" class="source-note" data-testid="source-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="source-error">{{ errorMessage }}</p>

    <table v-if="hits.length" class="data-table">
      <thead>
        <tr>
          <th>选中</th><th>实体ID</th><th>字段</th><th>页面URI</th><th>解析签名</th>
          <th>配置版本</th><th>字段签名</th><th>签名状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="hit in hits" :key="`${hit.索引ID}-${hit.实体ID}`" :data-testid="`source-hit-${hit.实体ID}`">
          <td>
            <input type="checkbox" :value="hit.实体ID" v-model="selectedIds" />
          </td>
          <td>{{ hit.实体ID }}</td>
          <td>{{ hit.字段 }}</td>
          <td class="uri-cell">{{ hit.页面URI }}</td>
          <td>{{ hit.解析签名 || '—' }}</td>
          <td>{{ hit.配置版本 || '—' }}</td>
          <td>{{ hit.字段签名 || '—' }}</td>
          <td>{{ hit.签名状态 }}</td>
        </tr>
      </tbody>
    </table>

    <div class="replay-block">
      <h4>重放清单</h4>
      <table class="data-table" data-testid="replay-table">
        <thead>
          <tr>
            <th>重放ID</th><th>页面URI</th><th>字段</th><th>解析签名</th><th>清单版本</th>
            <th>命中数</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in replays" :key="item.重放ID" :data-testid="`replay-${item.重放ID}`">
            <td>{{ item.重放ID }}</td>
            <td class="uri-cell">{{ item.页面URI }}</td>
            <td>{{ item.字段 || '—' }}</td>
            <td>{{ item.解析签名 || '—' }}</td>
            <td>{{ item.配置版本 || '—' }}</td>
            <td>{{ item.命中数 }}</td>
            <td>{{ item.状态 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`replay-submit-${item.重放ID}`"
                      @click="submitToDom(item)">
                送回DOM解析复验
              </button>
            </td>
          </tr>
          <tr v-if="!replays.length">
            <td colspan="8" class="empty-state">暂无重放清单，先检索并回写选中命中</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="lastReplay" class="replay-result" data-testid="replay-result">
      <h4>复验结果：{{ lastReplay.状态 }}</h4>
      <p v-if="lastReplay.配置已更正" class="source-note" data-testid="replay-corrected">
        {{ lastReplay.配置更正说明 }}
      </p>
      <p v-if="lastReplay.不能复验说明" class="error-text" data-testid="replay-blocked">
        {{ lastReplay.不能复验说明 }}
      </p>
      <p class="source-note">
        实际读取字段：{{ lastReplay.实际读取字段.length ? lastReplay.实际读取字段.join('、') : '无' }}
      </p>
      <ul v-if="lastReplay.无法复验明细?.length" class="reason-list" data-testid="replay-unverifiable">
        <li v-for="detail in lastReplay.无法复验明细" :key="`${detail.版本}-${detail.实体ID}`">
          {{ detail.版本 }} · {{ detail.实体ID }}：{{ detail.原因 }}
        </li>
      </ul>
      <table class="data-table">
        <thead>
          <tr><th>实体ID</th><th>字段</th><th>解析签名</th><th>结果</th><th>实际读取值</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="detail in lastReplay.字段核对" :key="`${detail.实体ID}-${detail.字段}`">
            <td>{{ detail.实体ID }}</td>
            <td>{{ detail.字段 }}</td>
            <td>{{ detail.解析签名 || '—' }}</td>
            <td>{{ detail.结果 }}</td>
            <td>{{ detail.实际读取值 || '—' }}</td>
            <td>{{ detail.原因 || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  searchAndWriteback,
  submitReplayToDom,
  listReplays,
  writebackHits,
  type ReplayItem,
  type ReplayResult,
  type SourceHit,
} from '@/api/source-index'

const query = reactive({
  page_uri: '',
  field: '',
  signature: '',
  version: '',
  include_unsigned: false,
})
const hits = ref<SourceHit[]>([])
const replays = ref<ReplayItem[]>([])
const selectedIds = ref<string[]>([])
const lastReplay = ref<ReplayResult | null>(null)
const message = ref('')
const errorMessage = ref('')
const searching = ref(false)

async function refreshReplays() {
  try {
    const payload = await listReplays()
    replays.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重放清单读取失败'
  }
}

async function runSearch() {
  searching.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await searchAndWriteback(query)
    hits.value = payload.hits ?? []
    selectedIds.value = hits.value.map((hit) => hit.实体ID)
    message.value = hits.value.length
      ? `查询范围内命中 ${hits.value.length} 条实体`
      : '查询范围内没有可复验的实体命中'
    await refreshReplays()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源索引检索失败'
  } finally {
    searching.value = false
  }
}

async function writeback() {
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await writebackHits(query, selectedIds.value)
    if (!payload.ok) {
      throw new Error(payload.reason ?? '回写失败')
    }
    lastReplay.value = null
    message.value = `已把选中的 ${payload.total ?? selectedIds.value.length} 条命中回写到重放清单` +
      ((payload.replays?.length ?? 0) > 1 ? `（按来源版本拆成 ${payload.replays?.length} 份清单）` : '')
    await refreshReplays()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回写失败'
  }
}

async function submitToDom(item: ReplayItem) {
  errorMessage.value = ''
  try {
    lastReplay.value = await submitReplayToDom(item.重放ID)
    await refreshReplays()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '送回DOM解析失败'
  }
}

onMounted(refreshReplays)
</script>

<style scoped>
.source-panel { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.source-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.source-note { color: var(--muted); font-size: 12px; margin: 6px 0; }
.check-item { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); }
.replay-block { margin-top: 16px; }
.replay-result { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.reason-list { color: #b42318; font-size: 12px; margin: 6px 0; }
.uri-cell { word-break: break-all; }
</style>
