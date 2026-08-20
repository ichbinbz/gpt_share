<template>
  <n-card :title="$t('codexQuota.title')">
    <template #header-extra>
      <n-space>
        <n-tag>{{ $t('codexQuota.accounts') }}: {{ status?.accounts.length ?? 0 }}</n-tag>
        <n-tag type="success">
          {{ $t('codexQuota.available') }}: {{ availableCount }}
        </n-tag>
        <n-tag type="info">
          {{ $t('codexQuota.activeLeases') }}: {{ activeLeaseCount }}
        </n-tag>
        <n-button size="small" :loading="loading" @click="refreshData(true)">
          {{ $t('codexQuota.refresh') }}
        </n-button>
      </n-space>
    </template>

    <n-alert v-if="status?.error" type="error" class="mb-3">
      {{ status.error }}
    </n-alert>
    <n-alert v-else type="info" :show-icon="false" class="mb-3">
      {{ $t('codexQuota.notice') }}
      <template v-if="status?.fetched_at">
        · {{ $t('codexQuota.updatedAt') }} {{ formatTimestamp(status.fetched_at) }}
      </template>
    </n-alert>

    <n-empty v-if="!status?.accounts.length" :description="$t('codexQuota.noAccounts')" />
    <n-grid v-else cols="1 m:2" responsive="screen" :x-gap="12" :y-gap="12">
      <n-grid-item v-for="account in status.accounts" :key="account.alias">
        <n-card size="small" embedded>
          <template #header>
            <n-space align="center">
              <div>
                <strong>{{ account.email || account.alias }}</strong>
                <div v-if="account.email" class="text-xs opacity-60">{{ account.alias }}</div>
              </div>
              <n-tag size="small" :type="account.available ? 'success' : 'error'">
                {{ account.available ? $t('codexQuota.ready') : $t('codexQuota.unavailable') }}
              </n-tag>
              <n-tag size="small">
                {{ account.plan_type?.toUpperCase() || $t('codexQuota.unknownPlan') }}
              </n-tag>
            </n-space>
          </template>

          <n-descriptions label-placement="left" :column="2" size="small">
            <n-descriptions-item :label="$t('codexQuota.usageScore')">
              {{ formatPercent(account.usage_score) }}
            </n-descriptions-item>
            <n-descriptions-item :label="$t('codexQuota.activeLeases')">
              {{ account.active_leases }}
            </n-descriptions-item>
          </n-descriptions>

          <n-alert v-if="account.error" type="error" class="mt-3">
            {{ account.error }}
          </n-alert>
          <n-empty v-else-if="!account.windows.length" size="small" :description="$t('codexQuota.noWindow')" />
          <div v-for="window in account.windows" v-else :key="window.name" class="mt-4">
            <div class="mb-1 flex justify-between gap-3">
              <strong>{{ $t(`codexQuota.window.${window.name}`) }}</strong>
              <span>{{ $t('codexQuota.remaining') }} {{ Math.round(window.remaining_percent) }}%</span>
            </div>
            <n-progress
              type="line"
              :percentage="Math.round(window.used_percent)"
              :status="progressStatus(window.used_percent)"
              :show-indicator="false"
            />
            <div class="mt-1 flex justify-between gap-3 text-xs opacity-70">
              <span>{{ $t('codexQuota.used') }} {{ Math.round(window.used_percent) }}% · {{ formatDuration(window.window_seconds) }}</span>
              <span>{{ $t('codexQuota.resetAt') }} {{ formatReset(window) }}</span>
            </div>
          </div>
        </n-card>
      </n-grid-item>
    </n-grid>
  </n-card>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import {
  CodexAccountQuotaStatus,
  CodexQuotaWindow,
  getCodexAccountQuotas,
} from '@/api/system';

const status = ref<CodexAccountQuotaStatus>();
const loading = ref(false);
let timer: number | undefined;

const availableCount = computed(() => status.value?.accounts.filter((account) => account.available).length ?? 0);
const activeLeaseCount = computed(
  () => status.value?.accounts.reduce((total, account) => total + account.active_leases, 0) ?? 0,
);

const formatTimestamp = (timestamp: number) => new Date(timestamp * 1000).toLocaleString();
const formatPercent = (value: number | null) => (value === null ? '—' : `${Math.round(value)}%`);
const formatDuration = (seconds: number | null) => {
  if (!seconds) return '—';
  if (seconds >= 86400) return `${Math.round(seconds / 86400)}d`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 60)}m`;
};
const formatReset = (window: CodexQuotaWindow) => {
  const resetAt = window.reset_at || (window.reset_after_seconds ? Date.now() / 1000 + window.reset_after_seconds : null);
  return resetAt ? formatTimestamp(resetAt) : '—';
};
const progressStatus = (usedPercent: number): 'default' | 'warning' | 'error' => {
  if (usedPercent >= 90) return 'error';
  if (usedPercent >= 70) return 'warning';
  return 'default';
};

const refreshData = async (refresh = false) => {
  loading.value = true;
  try {
    status.value = (await getCodexAccountQuotas(refresh)).data;
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  refreshData();
  timer = window.setInterval(() => refreshData(), 30_000);
});

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>
