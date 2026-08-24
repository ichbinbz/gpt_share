import axios from 'axios';

import {
  AskLogAggregation,
  ConfigModel,
  CredentialsModel,
  LogFilterOptions,
  OpenaiWebAccountsCheckResponse,
  RequestLogAggregation,
  SystemInfo,
} from '@/types/schema';

import ApiUrl from './url';

export type BrowserPoolQuotaStatus = {
  limit: number | null;
  window_seconds: number | null;
  used_by_cws: number;
  remaining_estimate: number | null;
  reset_at: number | null;
  source: 'local_cws_estimate' | 'unknown';
};

export type BrowserPoolAccountStatus = {
  id: string;
  name: string;
  enabled: boolean;
  weight: number;
  busy: boolean;
  connected: boolean;
  logged_in: boolean;
  cooldown_until: number | null;
  successful_requests: number;
  failed_requests: number;
  last_error: string | null;
  quota: Record<string, BrowserPoolQuotaStatus>;
  remote_quota_hint: string | null;
  remote_quota_checked_at: number | null;
};

export type BrowserPoolStatus = {
  transport: 'browser' | 'legacy_http';
  capacity: number;
  busy: number;
  accounts: BrowserPoolAccountStatus[];
};

export type CodexQuotaWindow = {
  name: 'primary' | 'secondary';
  used_percent: number;
  remaining_percent: number;
  window_seconds: number | null;
  reset_at: number | null;
  reset_after_seconds: number | null;
};

export type CodexAccountQuota = {
  alias: string;
  email: string | null;
  available: boolean;
  plan_type: string | null;
  usage_score: number | null;
  active_leases: number;
  access_token_expires_at: number | null;
  token_refresh_required: boolean;
  last_token_refresh_at: string | null;
  last_token_refresh_reason: string | null;
  credits_balance: number | null;
  credits_unlimited: boolean;
  windows: CodexQuotaWindow[];
  error: string | null;
};

export type CodexAccountQuotaStatus = {
  broker_version?: string | null;
  configured: boolean;
  reachable: boolean;
  accounts: CodexAccountQuota[];
  fetched_at: number;
  error: string | null;
};

export type CodexDeviceUsage = {
  device_token_id: string;
  device_label: string;
  employee_name: string | null;
  employee_id: string | null;
  enabled: boolean;
  session_count: number;
  lease_count: number;
  report_count: number;
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  user_message_count: number;
  user_text_characters: number;
  user_text_tokens_estimated: number;
  weekly: CodexUsagePeriod;
  monthly: CodexUsagePeriod;
  usage_timezone: string;
  week_start_date: string | null;
  month_start_date: string | null;
  first_reported_at: number | null;
  last_reported_at: number | null;
  first_seen_at: number | null;
  last_seen_at: number | null;
  client_ip: string | null;
  source_ip: string | null;
};

export type CodexUsagePeriod = {
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  user_message_count: number;
  user_text_characters: number;
  user_text_tokens_estimated: number;
};

export type CodexDeviceUsageStatus = {
  broker_version?: string | null;
  configured: boolean;
  reachable: boolean;
  devices: CodexDeviceUsage[];
  fetched_at: number;
  error: string | null;
};

export function getSystemInfoApi() {
  return axios.get<SystemInfo>(ApiUrl.SystemInfo);
}

export function getRequestStatisticsApi(granularity: number) {
  return axios.get<RequestLogAggregation[]>(ApiUrl.SystemRequestStatistics, {
    params: { granularity },
  });
}

export function getAskStatisticsApi(granularity: number) {
  return axios.get<AskLogAggregation[]>(ApiUrl.SystemAskStatistics, {
    params: { granularity },
  });
}

export function getSystemConfig() {
  return axios.get<ConfigModel>(ApiUrl.SystemConfig);
}

export function updateSystemConfig(config: ConfigModel) {
  return axios.put<ConfigModel>(ApiUrl.SystemConfig, config);
}

export function getSystemCredentials() {
  return axios.get<CredentialsModel>(ApiUrl.SystemCredentials);
}

export function updateSystemCredentials(credentials: CredentialsModel) {
  return axios.put<CredentialsModel>(ApiUrl.SystemCredentials, credentials);
}

export function runActionSyncOpenaiWebConversations() {
  return axios.post(ApiUrl.SystemActionSyncOpenaiWebConversations);
}

export function SystemCheckOpenaiWebAccount() {
  return axios.get<OpenaiWebAccountsCheckResponse>(ApiUrl.SystemCheckOpenaiWebAccount);
}

export function getBrowserPoolStatus(refresh = false, refreshQuota = false) {
  return axios.get<BrowserPoolStatus>(ApiUrl.SystemOpenaiWebBrowserPool, {
    params: { refresh, refresh_quota: refreshQuota },
  });
}

export function getCodexAccountQuotas(refresh = false) {
  return axios.get<CodexAccountQuotaStatus>(ApiUrl.SystemCodexAccountQuotas, {
    params: { refresh },
  });
}

export function getCodexDeviceUsage() {
  return axios.get<CodexDeviceUsageStatus>(ApiUrl.SystemCodexDeviceUsage);
}
