<template>
  <n-card :title="$t('codexEmployeeUsage.title')">
    <template #header-extra>
      <n-space>
        <n-tag>{{ $t('codexEmployeeUsage.employees') }}: {{ status?.devices.length ?? 0 }}</n-tag>
        <n-tag type="info">{{ $t('codexEmployeeUsage.total') }}: {{ formatTokens(totalTokens) }}</n-tag>
        <n-button size="small" :loading="loading" @click="refreshData">
          {{ $t('codexEmployeeUsage.refresh') }}
        </n-button>
      </n-space>
    </template>

    <n-alert v-if="status?.error" type="error" class="mb-3">
      {{ status.error }}
    </n-alert>
    <n-alert v-else type="info" :show-icon="false" class="mb-3">
      {{ $t('codexEmployeeUsage.notice') }}
      <template v-if="status?.fetched_at">
        · {{ $t('codexEmployeeUsage.updatedAt') }} {{ formatTimestamp(status.fetched_at) }}
      </template>
    </n-alert>

    <n-empty v-if="!status?.devices.length" :description="$t('codexEmployeeUsage.empty')" />
    <div v-else class="overflow-x-auto">
      <n-table striped size="small" :single-line="false">
        <thead>
          <tr>
            <th>{{ $t('codexEmployeeUsage.employeeToken') }}</th>
            <th>{{ $t('codexEmployeeUsage.status') }}</th>
            <th>{{ $t('codexEmployeeUsage.clientIp') }}</th>
            <th>{{ $t('codexEmployeeUsage.sourceIp') }}</th>
            <th>{{ $t('codexEmployeeUsage.userInput') }}</th>
            <th>{{ $t('codexEmployeeUsage.periodUsage') }}</th>
            <th>{{ $t('codexEmployeeUsage.input') }}</th>
            <th>{{ $t('codexEmployeeUsage.cachedInput') }}</th>
            <th>{{ $t('codexEmployeeUsage.output') }}</th>
            <th>{{ $t('codexEmployeeUsage.reasoning') }}</th>
            <th>{{ $t('codexEmployeeUsage.total') }}</th>
            <th>{{ $t('codexEmployeeUsage.sessions') }}</th>
            <th>{{ $t('codexEmployeeUsage.lastReport') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="device in status.devices" :key="device.device_token_id">
            <td>
              <strong>{{ device.employee_name || device.device_label }}</strong>
              <div v-if="device.employee_name" class="font-mono text-xs opacity-70">{{ device.device_label }}</div>
              <div class="font-mono text-xs opacity-60">{{ device.device_token_id.slice(0, 12) }}</div>
            </td>
            <td>
              <n-tag size="small" :type="deviceStatus(device).type">
                {{ $t(`codexEmployeeUsage.${deviceStatus(device).key}`) }}
              </n-tag>
            </td>
            <td class="font-mono">{{ device.client_ip || '—' }}</td>
            <td class="font-mono">{{ device.source_ip || '—' }}</td>
            <td class="whitespace-nowrap text-xs">
              <div>{{ $t('codexEmployeeUsage.week') }}: {{ formatUserInput(device.weekly) }}</div>
              <div>{{ $t('codexEmployeeUsage.month') }}: {{ formatUserInput(device.monthly) }}</div>
              <div>{{ $t('codexEmployeeUsage.lifetime') }}: {{ formatUserInput(device) }}</div>
            </td>
            <td class="whitespace-nowrap text-xs">
              <div>{{ $t('codexEmployeeUsage.week') }}: {{ formatTokens(device.weekly.total_tokens) }}</div>
              <div>{{ $t('codexEmployeeUsage.month') }}: {{ formatTokens(device.monthly.total_tokens) }}</div>
              <div>{{ $t('codexEmployeeUsage.lifetime') }}: {{ formatTokens(device.total_tokens) }}</div>
            </td>
            <td>{{ formatTokens(device.input_tokens) }}</td>
            <td>{{ formatTokens(device.cached_input_tokens) }}</td>
            <td>{{ formatTokens(device.output_tokens) }}</td>
            <td>{{ formatTokens(device.reasoning_output_tokens) }}</td>
            <td><strong>{{ formatTokens(device.total_tokens) }}</strong></td>
            <td>{{ formatTokens(device.session_count) }}</td>
            <td>{{ device.last_seen_at ? formatTimestamp(device.last_seen_at) : '—' }}</td>
          </tr>
        </tbody>
      </n-table>
    </div>
  </n-card>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';

import { CodexDeviceUsage, CodexDeviceUsageStatus, getCodexDeviceUsage } from '@/api/system';

const status = ref<CodexDeviceUsageStatus>();
const loading = ref(false);
const { t } = useI18n();
let timer: number | undefined;

const totalTokens = computed(
  () => status.value?.devices.reduce((total, device) => total + device.total_tokens, 0) ?? 0,
);
const formatTokens = (value: number) => new Intl.NumberFormat().format(value || 0);
const formatUserInput = (
  value: Pick<CodexDeviceUsage, 'user_message_count' | 'user_text_characters' | 'user_text_tokens_estimated'>,
) =>
  `${formatTokens(value.user_message_count)} ${t('codexEmployeeUsage.messages')} · ` +
  `${formatTokens(value.user_text_characters)} ${t('codexEmployeeUsage.characters')} · ` +
  `${formatTokens(value.user_text_tokens_estimated)} ${t('codexEmployeeUsage.estimatedTokens')}`;
const formatTimestamp = (timestamp: number) => new Date(timestamp * 1000).toLocaleString();
const deviceStatus = (device: CodexDeviceUsage): { key: string; type: 'success' | 'warning' | 'error' } => {
  if (!device.enabled) return { key: 'revoked', type: 'error' };
  const ageSeconds = Date.now() / 1000 - (device.last_seen_at ?? device.last_reported_at ?? 0);
  return ageSeconds <= 900 ? { key: 'online', type: 'success' } : { key: 'stale', type: 'warning' };
};

const refreshData = async () => {
  loading.value = true;
  try {
    status.value = (await getCodexDeviceUsage()).data;
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  refreshData();
  timer = window.setInterval(refreshData, 30_000);
});

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>
