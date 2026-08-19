<template>
  <n-card :title="$t('browserPool.title')">
    <template #header-extra>
      <n-space>
        <n-tag>{{ $t('browserPool.capacity') }}: {{ pool?.capacity ?? 0 }}</n-tag>
        <n-tag type="warning">
          {{ $t('browserPool.busy') }}: {{ pool?.busy ?? 0 }}
        </n-tag>
        <n-button size="small" :loading="loading" @click="refreshData(true)">
          {{ $t('browserPool.refreshRemote') }}
        </n-button>
      </n-space>
    </template>

    <n-alert class="mb-3" type="info" :show-icon="false">
      {{ $t('browserPool.quotaNotice') }}
    </n-alert>

    <n-empty v-if="!pool?.accounts.length" :description="$t('browserPool.noAccounts')" />
    <n-grid v-else cols="1 m:2" responsive="screen" :x-gap="12" :y-gap="12">
      <n-grid-item v-for="account in pool.accounts" :key="account.id">
        <n-card size="small" embedded>
          <template #header>
            <n-space align="center">
              <span>{{ account.name }}</span>
              <n-tag size="small" :type="account.logged_in ? 'success' : 'error'">
                {{ account.logged_in ? $t('browserPool.ready') : $t('browserPool.notReady') }}
              </n-tag>
              <n-tag v-if="account.busy" size="small" type="warning">
                {{ $t('browserPool.busy') }}
              </n-tag>
            </n-space>
          </template>

          <n-descriptions label-placement="left" :column="1" size="small">
            <n-descriptions-item :label="$t('browserPool.accountId')">
              {{ account.id }}
            </n-descriptions-item>
            <n-descriptions-item :label="$t('browserPool.weight')">
              {{ account.weight }}
            </n-descriptions-item>
            <n-descriptions-item :label="$t('browserPool.successFailure')">
              {{ account.successful_requests }} / {{ account.failed_requests }}
            </n-descriptions-item>
            <n-descriptions-item v-if="account.last_error" :label="$t('browserPool.lastError')">
              <n-text type="error">
                {{ account.last_error }}
              </n-text>
            </n-descriptions-item>
          </n-descriptions>

          <n-divider>{{ $t('browserPool.quota') }}</n-divider>
          <n-empty v-if="Object.keys(account.quota).length === 0" size="small" :description="$t('browserPool.quotaUnknown')" />
          <n-space v-else vertical>
            <div v-for="(quota, model) in account.quota" :key="model" class="flex justify-between gap-4">
              <span>{{ model }}</span>
              <span>
                {{ quota.remaining_estimate ?? '?' }} / {{ quota.limit ?? '?' }}
                · {{ $t('browserPool.usedByCws') }} {{ quota.used_by_cws }}
              </span>
            </div>
          </n-space>

          <template v-if="account.remote_quota_hint">
            <n-divider>{{ $t('browserPool.remoteHint') }}</n-divider>
            <n-code :code="account.remote_quota_hint" language="text" word-wrap />
          </template>
        </n-card>
      </n-grid-item>
    </n-grid>
  </n-card>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';

import { BrowserPoolStatus, getBrowserPoolStatus } from '@/api/system';

const pool = ref<BrowserPoolStatus>();
const loading = ref(false);
let timer: number | undefined;

const refreshData = async (refreshQuota = false) => {
  loading.value = true;
  try {
    pool.value = (await getBrowserPoolStatus(true, refreshQuota)).data;
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  refreshData();
  timer = window.setInterval(() => refreshData(), 10_000);
});

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>
